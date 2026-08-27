"""Audible catalogue client -- the recommendation engine.

`/1.0/catalog/products/{asin}/sims` returns Audible's own similar-products list
and needs no key or account. Responses are cached in SQLite; repeated page loads
reuse the engine's in-memory result.
"""
import httpx

from . import config, store

_BASE = "https://api.audible.com/1.0/catalog"
_RESPONSE_GROUPS = "product_desc,contributors,product_attrs,media"
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
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            resp = c.get(f"{_BASE}/products/{asin}/sims", params=params)
            resp.raise_for_status()
            products = resp.json().get("similar_products") or []
    except (httpx.HTTPError, ValueError):
        return []

    thinned = [_thin(p) for p in products if p.get("asin")]
    store.put_sims(asin, axis, thinned)
    return thinned


def product(asin: str) -> dict | None:
    """Full-ish metadata for one ASIN, used when handing a pick to Listenarr."""
    params = {"response_groups": "contributors,product_attrs,product_desc,media"}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            resp = c.get(f"{_BASE}/products/{asin}", params=params)
            resp.raise_for_status()
            return resp.json().get("product")
    except (httpx.HTTPError, ValueError):
        return None
