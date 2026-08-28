"""Listenarr client -- a write-only sink, plus one read of queue *state*.

Listenarr is an acquisition work queue here, not a catalogue: its library holds
only what it has bought (85 rows against Jellyfin's 1028), so Nextread never
uses it to answer "what do I have". It does ask "what is already on order", to
avoid recommending a book that is mid-acquisition.
"""
from typing import NamedTuple

import httpx

from . import config, logs

log = logs.get("listenarr")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_API = "/api/v1"


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.LISTENARR_URL, timeout=_TIMEOUT)


def _csrf(client: httpx.Client) -> str:
    """Every state-changing Listenarr call needs this token AND its cookie.

    Without both you get `400 Invalid or missing CSRF token`. The cookie rides on
    the shared client's jar.
    """
    resp = client.get(f"{_API}/antiforgery/token")
    resp.raise_for_status()
    return resp.json()["token"]


def queued_asins() -> set[str]:
    """ASINs Listenarr already knows about -- owned or merely wanted.

    Used purely as a suppression list for the recommendation surface.
    """
    try:
        with _client() as c:
            rows = c.get(f"{_API}/library").raise_for_status().json()
    except (httpx.HTTPError, ValueError) as exc:
        # Failing soft here means every book already on order becomes
        # recommendable again. Silent, and it looks exactly like a good shelf.
        log.warning("queue-state read failed (%s); suppression list is empty "
                    "this pass, so books already on order may be re-offered", exc)
        return set()
    asins = {r["asin"] for r in rows if r.get("asin")}
    log.debug("queue state: %d ASINs on order", len(asins))
    return asins


def _names(values) -> list[str]:
    """Flatten Listenarr's `[{name: ...}]` search shape to the plain list the add DTO wants."""
    out = []
    for v in values or []:
        name = v.get("name") if isinstance(v, dict) else v
        if name:
            out.append(name)
    return out


def _to_add_metadata(result: dict, region: str | None = None) -> dict:
    """Map a search result onto `AudibleBookMetadata`.

    These are two different DTOs and the difference is not cosmetic: the search
    endpoint returns authors/narrators/genres as OBJECTS, while
    `AudibleBookMetadata` declares them as `List<string>`. Posting the search
    shape straight through fails deserialisation, which surfaces as a misleading
    `400 The request field is required` rather than a field-level error.
    """
    series = (result.get("series") or [{}])[0] if result.get("series") else {}
    release = result.get("releaseDate") or ""
    return {
        "asin": result.get("asin"),
        "source": "Audible",
        # The store this was actually found in, not the preferred one: the
        # library spans both, and telling Listenarr the wrong one sends its own
        # lookups somewhere that does not carry the book.
        "region": region or config.AUDIBLE_REGION,
        "title": result.get("title"),
        "authors": _names(result.get("authors")),
        "narrators": _names(result.get("narrators")),
        "genres": _names(result.get("genres")),
        "imageUrl": result.get("imageUrl"),
        "language": result.get("language"),
        "publisher": result.get("publisher"),
        "publishedDate": release or None,
        "publishYear": release[:4] or None,
        "series": series.get("name"),
        "seriesNumber": series.get("position"),
        "seriesMemberships": [
            {
                "seriesName": s.get("name"),
                "seriesNumber": s.get("position"),
                "seriesAsin": s.get("asin"),
                "isPrimary": idx == 0,
                "sortOrder": idx,
            }
            for idx, s in enumerate(result.get("series") or [])
            if s.get("name")
        ],
        "runtime": result.get("lengthMinutes"),
        "bookFormat": result.get("bookFormat"),
    }


def audible_metadata(asin: str) -> dict | None:
    """Metadata for one ASIN, in the shape `POST /library/add` expects.

    Sourced from Listenarr's own Audible lookup rather than Audible directly, so
    the fields match whatever its provider currently returns.
    """
    # Every configured marketplace, in order, until one returns this exact
    # ASIN. A book sold only in the other store answers with nothing here, and
    # asking one store alone is what made a real book look unidentifiable.
    results: list[dict] = []
    exact = None
    found_region = config.AUDIBLE_REGION
    for region in config.AUDIBLE_REGIONS:
        try:
            with _client() as c:
                resp = c.get(
                    f"{_API}/search/audible", params={"query": asin, "region": region})
                resp.raise_for_status()
                found = resp.json().get("results") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("metadata lookup failed asin=%s region=%s (%s)", asin, region, exc)
            continue
        results = results or found
        exact = next(
            (r for r in found if (r.get("asin") or "").upper() == asin.upper()), None)
        if exact is not None:
            found_region = region
            break
    if not results:
        log.warning("metadata lookup found nothing asin=%s", asin)
        return None
    if exact is None or not (_to_add_metadata(exact).get("title") or "").strip():
        # Listenarr's search could not identify this ASIN -- either it returned
        # nothing that IS this ASIN, or it returned it with no title. Audible's
        # own product endpoint can, and does for books its search cannot:
        # B0HC7V8ZR4 ("Unicorn Breeder") comes back titleless from the search in
        # both stores while the product lookup has it on .com. Without this,
        # such a book cannot be asked for at all.
        direct = _from_audible_product(asin)
        if direct is not None:
            log.info("identified asin=%s from Audible directly, region=%s",
                     asin, direct.get("region"))
            return direct
    if exact is None:
        # NEVER the first result instead. This is an identifier lookup: a
        # result that is not this ASIN is a different book, and handing it to
        # the add makes the library acquire something nobody asked for.
        #
        # It did exactly that on 2026-08-28. A request for "I Ran Away to Evil"
        # was added with an empty title, Listenarr searched on what it had, and
        # downloaded Dark Tower III: The Waste Lands, which then imported under
        # `Unknown Author/Unknown Title` because it matched nothing either.
        log.warning(
            "metadata lookup returned %d result(s) for asin=%s and none of them "
            "is that ASIN; refusing rather than substituting %r",
            len(results), asin, (results[0].get("title") or "")[:60])
        return None
    metadata = _to_add_metadata(exact, region=found_region)
    if not (metadata.get("title") or "").strip():
        # A title is what the acquisition searches on. Without one it searches
        # on nothing and takes whatever scores first, which is the same failure
        # by a different route.
        log.warning("metadata for asin=%s carries no title; refusing the add", asin)
        return None
    return metadata


def _from_audible_product(asin: str) -> dict | None:
    """Add-metadata built from Audible's own product record.

    The identity path of last resort, and the only one that never guesses: the
    product endpoint is addressed BY the ASIN, so what it returns either is
    that book or is nothing. `audible.product` already walks every configured
    marketplace and reports which one had it.
    """
    from . import audible

    found = audible.product(asin)
    if not found:
        return None
    title = (found.get("title") or "").strip()
    if not title:
        return None
    series = (found.get("series") or [{}])[0] if found.get("series") else {}
    release = found.get("release_date") or ""
    runtime = found.get("runtime_length_min")
    return {
        "asin": asin,
        "source": "Audible",
        "region": found.get("_region") or config.AUDIBLE_REGION,
        "title": title,
        "subtitle": (found.get("subtitle") or "").strip() or None,
        "authors": _names(found.get("authors")),
        "narrators": _names(found.get("narrators")),
        "genres": [],
        "language": found.get("language"),
        "publisher": found.get("publisher_name"),
        "publishedDate": release or None,
        "publishYear": release[:4] or None,
        "series": series.get("title") or series.get("name"),
        "seriesNumber": series.get("sequence"),
        "seriesMemberships": [],
        "runtime": runtime * 60 if isinstance(runtime, int) else None,
    }


def audible_search(query: str, limit: int = 25) -> list[dict]:
    """Free-text Audible catalogue search, via Listenarr.

    Audible's own `/catalog/products?keywords=` returns nothing unauthenticated;
    Listenarr's provider is authenticated, so this is the only route to keyword
    and genre discovery without standing up Audible credentials of our own.
    """
    # Every configured marketplace, merged, first store's ordering kept and
    # later ones appended. Not "first store with any hit": a title search that
    # stopped at the preferred region would never surface the half of this
    # library that only the other store sells.
    #
    # Region stated rather than left to Listenarr's own default. Its
    # `DefaultSearchRegion` is a setting, so a reset of its database would put
    # these searches back on the US store without a word.
    results: list[dict] = []
    seen: set[str] = set()
    failures = 0
    try:
        for region in config.AUDIBLE_REGIONS:
            try:
                with _client() as c:
                    resp = c.get(
                        f"{_API}/search/audible",
                        params={"query": query, "region": region})
                    resp.raise_for_status()
                    found = resp.json().get("results") or []
            except (httpx.HTTPError, ValueError):
                failures += 1
                continue
            for row in found:
                asin = (row.get("asin") or "").upper()
                # An ASIN sold in both stores is one book, listed once.
                if asin and asin in seen:
                    continue
                if asin:
                    seen.add(asin)
                results.append(row)
        if failures == len(config.AUDIBLE_REGIONS):
            raise httpx.HTTPError("every marketplace failed")
        results = results[:limit]
    except (httpx.HTTPError, ValueError) as exc:
        # This is also the ASIN resolver for the three quarters of the library
        # with no Audible id of its own, so losing it quietly thins the unowned
        # shelf rather than emptying it -- which is why it is logged loudly.
        log.warning("Audible search failed query=%r (%s); ASIN resolution and "
                    "keyword discovery are blind this pass", query, exc)
        return []
    log.debug("Audible search query=%r hits=%d", query, len(results))
    return results


class AddResult(NamedTuple):
    """Outcome of handing one book to Listenarr.

    `ok` means the book is now in Listenarr's library and will be acquired --
    including the case where it was already there, because the caller's
    question is "is this book coming?" and the answer is yes either way.

    `title` and `authors` are what the book turned out to be once the ASIN was
    resolved. The request ledger keeps them so it can recognise the book on
    arrival: the ASIN asked for belongs to the marketplace it was found in, and
    the tagger writes whichever the other store issued for the same edition.
    """
    ok: bool
    message: str
    audiobook_id: int | None
    title: str = ""
    authors: tuple[str, ...] = ()


def find_by_asin(asin: str) -> dict | None:
    """Listenarr's row for this ASIN, or None.

    Checked before every add: duplicate Audible editions are a known live
    nuisance and the add endpoint is not assumed to dedupe.
    """
    try:
        with _client() as c:
            resp = c.get(f"{_API}/library/by-asin/{asin}")
    except httpx.HTTPError as exc:
        log.warning("by-asin lookup failed asin=%s (%s); treating as absent, "
                    "so the add may hit a duplicate", asin, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def add(asin: str, monitored: bool = True) -> AddResult:
    """Hand one book to Listenarr to acquire.

    `AutoSearch` stays False on purpose -- it is an inline await, so True would
    block this request on serialised indexer searches. Immediate acquisition
    comes from `enqueue_search` instead, which hands the job to Listenarr's own
    paced queue; the 6-hourly AutomaticSearchService sweep remains the fallback
    for anything that queue never manages to satisfy.

    `SearchResult` is left unset: supplying one bypasses release scoring entirely.
    """
    existing = find_by_asin(asin)
    if existing is not None:
        title, authors = _identity(existing)
        return AddResult(
            True, "Already in Listenarr", _audiobook_id(existing), title, authors)

    meta = audible_metadata(asin)
    if not meta:
        return AddResult(
            False,
            "That book could not be identified well enough to ask for it. "
            "Nothing was added.",
            None)

    body = {
        "metadata": meta,
        "monitored": monitored,
        "qualityProfileId": config.LISTENARR_QUALITY_PROFILE_ID,
        "autoSearch": False,
    }
    try:
        with _client() as c:
            token = _csrf(c)
            resp = c.post(
                f"{_API}/library/add",
                json=body,
                headers={"X-XSRF-TOKEN": token},
            )
    except httpx.HTTPError as exc:
        log.error("add failed asin=%s: Listenarr unreachable (%s)", asin, exc)
        return AddResult(False, f"Listenarr unreachable: {exc}", None)

    # 409 is Listenarr saying the book is already in its library, and it returns
    # the existing row with it. Two people asking for the same book at the same
    # moment both pass the by-asin check, so one of them lands here; the book is
    # coming either way, which is what the caller asked.
    if resp.status_code >= 400 and resp.status_code != 409:
        log.error("add rejected asin=%s status=%d body=%s",
                  asin, resp.status_code, resp.text[:180])
        return AddResult(False, f"Listenarr said {resp.status_code}: {resp.text[:180]}", None)
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    message = "Already in Listenarr" if resp.status_code == 409 else "Sent to Listenarr"
    return AddResult(True, message, _audiobook_id(payload.get("audiobook")),
                     (meta.get("title") or "").strip(),
                     tuple(meta.get("authors") or ()))


def _identity(row) -> tuple[str, tuple[str, ...]]:
    """Title and authors out of an audiobook row, whichever shape they arrive in."""
    if not isinstance(row, dict):
        return "", ()
    title = (row.get("title") or row.get("Title") or "").strip()
    return title, tuple(_names(row.get("authors") or row.get("Authors")))


def _audiobook_id(row) -> int | None:
    """The numeric id out of an audiobook row, whatever case it arrives in."""
    if not isinstance(row, dict):
        return None
    value = row.get("id", row.get("Id"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def enqueue_search(audiobook_id: int) -> bool:
    """Ask Listenarr to search for one book now, without waiting for the sweep.

    The work is queued, never awaited: Listenarr drains the queue through a
    single paced consumer, which is exactly what stops ten accounts tapping at
    once from becoming ten simultaneous indexer hits. A False here is not a
    failure worth surfacing -- the book is monitored, so the 6-hourly sweep
    still acquires it. That also covers a Listenarr too old to have the route.
    """
    try:
        with _client() as c:
            token = _csrf(c)
            resp = c.post(
                f"{_API}/download/queue-search",
                json={"audiobookId": audiobook_id, "reason": "nextread"},
                headers={"X-XSRF-TOKEN": token},
            )
    except httpx.HTTPError as exc:
        log.warning("search queue unreachable audiobook_id=%s (%s)", audiobook_id, exc)
        return False
    if resp.status_code >= 400:
        log.warning("search queue rejected audiobook_id=%s status=%d body=%s",
                    audiobook_id, resp.status_code, resp.text[:180])
        return False
    return True


def delete(audiobook_id: int) -> bool:
    """Remove a Listenarr row without touching files. Used to clean up test adds."""
    try:
        with _client() as c:
            token = _csrf(c)
            resp = c.delete(
                f"{_API}/library/{audiobook_id}",
                params={"deleteFiles": "false", "deleteFolder": "false"},
                headers={"X-XSRF-TOKEN": token},
            )
    except httpx.HTTPError:
        return False
    return resp.status_code < 400
