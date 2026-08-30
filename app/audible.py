"""Audible catalogue client -- the recommendation engine.

`/1.0/catalog/products/{asin}/sims` returns Audible's own similar-products list
and needs no key or account. Responses are cached in SQLite; repeated page loads
reuse the engine's in-memory result.
"""
import httpx

from . import config, store

# Audible runs one catalogue per marketplace on its own host, and an ASIN sold
# in one is not necessarily present in another: a US lookup of a Canadian
# exclusive returns 200 with no product rather than a 404, so the failure is
# silent. Anything not listed falls back to the US host, which is Audible's
# oldest and the safest guess for a region this map has not met.
_HOSTS = {
    "us": "api.audible.com",
    "ca": "api.audible.ca",
    "uk": "api.audible.co.uk",
    "au": "api.audible.com.au",
    "de": "api.audible.de",
    "fr": "api.audible.fr",
    "it": "api.audible.it",
    "es": "api.audible.es",
    "jp": "api.audible.co.jp",
    "in": "api.audible.in",
    "br": "api.audible.com.br",
}


def _host(region: str | None = None) -> str:
    return _HOSTS.get(region or config.AUDIBLE_REGION, _HOSTS["us"])


def _base(region: str | None = None) -> str:
    return f"https://{_host(region)}/1.0/catalog"


def _has_product(payload) -> bool:
    """Whether a marketplace actually carries this book.

    A store that does not sell it answers **200 with an empty product**, not a
    404, so the absence is silent and only the missing title gives it away.
    """
    return bool(payload) and bool((payload.get("title") or "").strip())
_RESPONSE_GROUPS = "product_desc,contributors,product_attrs,media"

# One book at a time can afford the long blurb, and a shelf of neighbours cannot.
#
# `product_desc` carries only `merchandising_summary`, Audible's teaser, which
# is a couple of hundred characters and ends mid-sentence in an ellipsis. The
# whole description is `publisher_summary`, and that arrives ONLY when
# `product_extended_attrs` is asked for -- measured against api.audible.ca on
# 2026-08-29, B0FQ65NC2F answers with 205 characters under the groups above and
# 1,433 with this one added. Without it `search.summary`'s preference for the
# long form could never be satisfied and every summary was the teaser.
#
# Deliberately NOT added to `_RESPONSE_GROUPS`: that is the sims call, which
# fetches ten neighbours per seed across twenty seeds, and the long blurb is
# several times the payload for text no shelf row displays.
_PRODUCT_RESPONSE_GROUPS = (
    "contributors,product_attrs,product_desc,product_extended_attrs,media")
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Audible honours `similarity_type` and each value returns a genuinely different
# neighbour set (verified 2026-08-23). RawSimilarities is the broad
# "also listened to" list and is the only one used by default: the others pay off
# once ratings can say whether this listener follows narrators or authors, and
# ByTheSameAuthor in particular duplicates a bonus the scorer already applies.
AXIS_RAW = "RawSimilarities"
AXIS_AUTHOR = "ByTheSameAuthor"
AXIS_NARRATOR = "ByTheSameNarrator"
AXIS_SERIES = "InTheSameSeries"


def _thin(product: dict) -> dict:
    """Keep only what the shelf needs. Full payloads are large and mostly noise.

    The description is retained deliberately: without it an unowned candidate has
    no text, so the rating-driven text model could only ever re-rank books
    already on disk -- which is the half of the promise that matters least.
    """
    return {
        "asin": product.get("asin"),
        "title": (product.get("title") or "").strip(),
        "subtitle": (product.get("subtitle") or "").strip(),
        "authors": [a.get("name", "") for a in (product.get("authors") or []) if a.get("name")],
        "narrators": [n.get("name", "") for n in (product.get("narrators") or []) if n.get("name")],
        "runtime_min": product.get("runtime_length_min"),
        "release_date": product.get("release_date"),
        "publisher": product.get("publisher_name"),
        "description": (product.get("merchandising_summary")
                        or product.get("publisher_summary") or "").strip(),
    }


def sims(asin: str, axis: str = AXIS_RAW) -> list[dict]:
    """Similar products for one ASIN along one similarity axis, cached when fresh.

    Returns an empty list on any failure -- a dead seed must not fail a whole run.
    """
    cached = store.get_sims(asin, axis)
    if cached is not None:
        return cached

    params = {
        "response_groups": _RESPONSE_GROUPS,
        "num_results": config.SIMS_PER_SEED,
        "similarity_type": axis,
    }
    # Each marketplace in turn. A seed sold only in the other store returns an
    # empty neighbour list rather than an error, so "no neighbours" and "wrong
    # store" look identical from one region alone.
    thinned: list[dict] = []
    for region in config.AUDIBLE_REGIONS:
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                resp = c.get(f"{_base(region)}/products/{asin}/sims", params=params)
                resp.raise_for_status()
                products = resp.json().get("similar_products") or []
        except (httpx.HTTPError, ValueError):
            continue
        thinned = [_thin(p) for p in products if p.get("asin")]
        if thinned:
            break

    # Cached even when empty: every store was asked and none had neighbours,
    # which is an answer, and re-asking it per page load is what the cache
    # exists to stop.
    store.put_sims(asin, axis, thinned)
    return thinned


def product(asin: str) -> dict | None:
    """Full-ish metadata for one ASIN, used when handing a pick to Listenarr.

    Cached, because a summary can now be opened on demand and the same book
    read twice must not cost two requests. A miss is not cached: an absent
    product is what a wrong-marketplace lookup returns, and remembering that
    for a month would outlive the mistake.
    """
    cached = store.get_product(asin)
    if cached is not None:
        return cached
    params = {"response_groups": _PRODUCT_RESPONSE_GROUPS}
    # Every configured marketplace, in order, until one actually carries it.
    # Stopping at the first is what made a book sold only in the other store
    # look like a book that does not exist.
    for region in config.AUDIBLE_REGIONS:
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                resp = c.get(f"{_base(region)}/products/{asin}", params=params)
                resp.raise_for_status()
                found = resp.json().get("product")
        except (httpx.HTTPError, ValueError):
            continue
        if _has_product(found):
            # The region it was actually found in, so a caller handing this on
            # names the store that has it rather than the one we prefer.
            found = {**found, "_region": region}
            store.put_product(asin, found)
            return found
    # Deliberately not cached. An absence here is "no configured store sells
    # it", which a new region in the list would change, and remembering it for
    # a month would outlive that.
    return None
