"""Audible catalogue client -- the recommendation engine.

`/1.0/catalog/products/{asin}/sims` returns Audible's own similar-products list
and needs no key or account. Responses are cached in SQLite; this endpoint is
never called on a page load.
"""
import httpx

from . import config, store

_BASE = "https://api.audible.com/1.0/catalog"
_RESPONSE_GROUPS = "product_desc,contributors,product_attrs,media"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _thin(product: dict) -> dict:
    """Keep only what the shelf needs. Full payloads are large and mostly noise."""
    return {
        "asin": product.get("asin"),
        "title": (product.get("title") or "").strip(),
        "subtitle": (product.get("subtitle") or "").strip(),
        "authors": [a.get("name", "") for a in (product.get("authors") or []) if a.get("name")],
        "narrators": [n.get("name", "") for n in (product.get("narrators") or []) if n.get("name")],
        "runtime_min": product.get("runtime_length_min"),
        "release_date": product.get("release_date"),
        "publisher": product.get("publisher_name"),
    }


def sims(asin: str) -> list[dict]:
    """Similar products for one ASIN, from cache when fresh.

    Returns an empty list on any failure -- a dead seed must not fail a whole run.
    """
    cached = store.get_sims(asin)
    if cached is not None:
        return cached

    params = {"response_groups": _RESPONSE_GROUPS, "num_results": config.SIMS_PER_SEED}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            resp = c.get(f"{_BASE}/products/{asin}/sims", params=params)
            resp.raise_for_status()
            products = resp.json().get("similar_products") or []
    except (httpx.HTTPError, ValueError):
        return []

    thinned = [_thin(p) for p in products if p.get("asin")]
    store.put_sims(asin, thinned)
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
