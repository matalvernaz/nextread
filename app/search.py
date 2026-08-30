"""Direct catalogue search, for the book somebody already has in mind.

The shelves answer "what should I read next". This answers "do we have, or can
we get, *this*" -- which no amount of similarity ranking can, because the whole
point is that the listener names the book.

Audible's own `?keywords=` returns nothing unauthenticated, so the query goes
through Listenarr, whose provider is authenticated. That is the same route the
keyword-discovery channel already uses; it borrows Listenarr's search, never its
library, so Jellyfin remains the single library of record.

Owned books are *marked*, not hidden. On the Discover shelf an owned book is
noise and is filtered out. In a search it is the answer: somebody typing a title
wants to know it is already here, and silence would read as "we cannot get it".
"""
import html
import re

from . import config, engine, jellyfin, listenarr, logs, shelves, wants

log = logs.get(__name__)

# Where one block of the blurb ends and the next begins. Substituted before the
# rest of the tags are dropped, or eight paragraphs arrive as one unbroken
# sentence with nothing in it for a screen reader to pause on.
_BLOCK_END = re.compile(r"(?i)</p\s*>|<br\s*/?>|</li\s*>|</div\s*>|</h[1-6]\s*>")
_TAG = re.compile(r"<[^>]+>")
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*")


def search(user: jellyfin.User, query: str, limit: int | None = None) -> list[dict]:
    """Catalogue hits for a free-text query, each flagged owned or requested.

    No descriptions: a blurb is one Audible request per book and a query of
    twenty-five would pay twenty-five of them for text nobody has asked to read.
    They come from `summary()` when a listener opens one.
    """
    query = (query or "").strip()
    if not query:
        return []
    rows = listenarr.audible_search(query, limit=limit or config.SEARCH_LIMIT)
    asins, by_title = shelves.owned_index(user)
    # Outstanding only. A request that has arrived is described by `owned`, and
    # showing both would have a book claim to be on order and on the shelf.
    requested = {r["asin"] for r in wants.states(user.key, (asins, by_title))
                 if r["state"] != wants.IN_LIBRARY}
    results = []
    for row in rows:
        asin = row.get("asin")
        if not asin:
            continue
        hit = {
            "asin": asin,
            "title": (row.get("title") or "").strip(),
            "authors": [a.get("name", "") for a in (row.get("authors") or []) if a.get("name")],
            "narrators": [n.get("name", "") for n in (row.get("narrators") or []) if n.get("name")],
            "runtimeMinutes": row.get("lengthMinutes"),
        }
        hit["owned"] = engine._already_owned(
            {"asin": asin, "title": hit["title"], "authors": hit["authors"]},
            asins, by_title)
        hit["requested"] = asin in requested
        results.append(hit)
    log.info("search user=%s query=%r hits=%d owned=%d",
             user.key, query, len(results), sum(1 for r in results if r["owned"]))
    return results


def summary(asin: str) -> dict:
    """One book's blurb, fetched when somebody asks to hear it.

    Audible gives a short `merchandising_summary` and a longer
    `publisher_summary`; the longer one is preferred here because this is the
    surface where somebody has explicitly asked for more, and the short one is
    what the shelf row already implies.
    """
    product = store_backed_product(asin) or {}
    text = _plain(product.get("publisher_summary")
                  or product.get("merchandising_summary") or "")
    return {
        "asin": asin,
        "title": (product.get("title") or "").strip(),
        "authors": [a.get("name", "") for a in (product.get("authors") or []) if a.get("name")],
        "runtimeMinutes": product.get("runtime_length_min"),
        "summary": text,
    }


def _plain(markup: str) -> str:
    """Audible's blurb as something a screen reader can read aloud.

    Both summaries come back as HTML -- paragraphs, bold, italics -- and neither
    surface that shows one renders any of it: the search page assigns it to
    `textContent` and EchoFin draws it in a `Text`. So the markup was being
    spoken as words. Entities are unescaped last, so an `&lt;` that was text in
    the blurb stays text rather than becoming a tag one pass too late to strip.
    """
    text = _TAG.sub("", _BLOCK_END.sub("\n\n", markup))
    text = html.unescape(text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def store_backed_product(asin: str):
    """Indirection so a test can stand in for the network without a fixture DB."""
    from . import audible
    return audible.product(asin)
