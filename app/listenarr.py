"""Listenarr client -- a write-only sink, plus one read of queue *state*.

Listenarr is an acquisition work queue here, not a catalogue: its library holds
only what it has bought (85 rows against Jellyfin's 1028), so Nextread never
uses it to answer "what do I have". It does ask "what is already on order", to
avoid recommending a book that is mid-acquisition.
"""
import httpx

from . import config

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
    except (httpx.HTTPError, ValueError):
        return set()
    return {r["asin"] for r in rows if r.get("asin")}


def _names(values) -> list[str]:
    """Flatten Listenarr's `[{name: ...}]` search shape to the plain list the add DTO wants."""
    out = []
    for v in values or []:
        name = v.get("name") if isinstance(v, dict) else v
        if name:
            out.append(name)
    return out


def _to_add_metadata(result: dict) -> dict:
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
        "region": "us",
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
    try:
        with _client() as c:
            resp = c.get(f"{_API}/search/audible", params={"query": asin})
            resp.raise_for_status()
            results = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError):
        return None
    if not results:
        return None
    exact = next((r for r in results if (r.get("asin") or "").upper() == asin.upper()), None)
    return _to_add_metadata(exact or results[0])


def exists(asin: str) -> bool:
    """True when Listenarr already has a row for this ASIN.

    Checked before every add: duplicate Audible editions are a known live
    nuisance and the add endpoint is not assumed to dedupe.
    """
    try:
        with _client() as c:
            resp = c.get(f"{_API}/library/by-asin/{asin}")
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def add(asin: str, monitored: bool = True) -> tuple[bool, str]:
    """Hand one book to Listenarr to acquire. Returns (ok, message).

    `AutoSearch` stays False on purpose -- it is an inline await, so True would
    block this request on serialised indexer searches. The 6-hourly
    AutomaticSearchService sweep picks up anything monitored with a profile.

    `SearchResult` is left unset: supplying one bypasses release scoring entirely.
    """
    if exists(asin):
        return False, "Already in Listenarr"

    meta = audible_metadata(asin)
    if not meta:
        return False, "No Audible metadata found for that ASIN"

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
        return False, f"Listenarr unreachable: {exc}"

    if resp.status_code >= 400:
        return False, f"Listenarr said {resp.status_code}: {resp.text[:180]}"
    return True, "Sent to Listenarr"


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
