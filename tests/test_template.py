"""Focused rendering checks for the server-rendered recommendation page."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


root = Path(__file__).resolve().parents[1]
environment = Environment(
    loader=FileSystemLoader(root / "app/templates"),
    autoescape=select_autoescape(),
)
html = environment.get_template("index.html").render(
    user_name="alex",
    own=[],
    discover=[{
        "asin": "A1",
        "title": "Example & Book",
        "authors": ["A. Writer"],
        "narrators": [],
        "runtime_min": 61,
        "why": ["Audible recommends it alongside “A Source Book”"],
        "description": "<p>A <strong>useful</strong> summary &amp; ending.</p>",
    }],
    seeds=1,
    library=2,
    ratings=0,
    rating_floor=5,
    playlist_name="Next Read",
    msg="",
    err="",
)

assert '<details class="summary">' in html
assert 'aria-label="Summary of Example &amp; Book"' in html
assert "A useful summary &amp; ending." in html
assert "<strong>useful</strong>" not in html
assert "A Source Book" in html
assert "Discover, for <strong>alex</strong>." in html
assert "0 of your ratings detected; rating-based tuning begins at 5." in html
# The way in to search has to be on the page somebody lands on, above both
# shelves: arriving with a title in mind should not mean walking past forty
# suggestions to type it.
assert 'action="/search"' in html
assert 'role="search"' in html
assert html.index('action="/search"') < html.index('id="own"')

# Every outstanding request offers a way out of itself, and a book that has
# arrived does not -- there is nothing left to stop looking for.
requests_html = environment.get_template("index.html").render(
    user_name="alex",
    own=[], discover=[], seeds=1, library=2, ratings=0, rating_floor=5,
    playlist_name="Next Read", msg="", err="",
    requests=[
        {"asin": "A1", "title": "Still Coming & Waiting", "state": "still_looking"},
        {"asin": "A2", "title": "Turned Up", "state": "in_library"},
    ],
)
assert 'action="/cancel"' in requests_html
assert "Stop looking — Still Coming &amp; Waiting" in requests_html
assert "Stop looking — Turned Up" not in requests_html

search_html = environment.get_template("search.html").render(
    user_name="alex",
    query="boba",
    results=[
        {"asin": "A1", "title": "Owned & Here", "authors": ["A. Writer"],
         "narrators": [], "runtimeMinutes": 61, "owned": True, "requested": False},
        {"asin": "A2", "title": "Not Here Yet", "authors": [], "narrators": [],
         "runtimeMinutes": None, "owned": False, "requested": True},
    ],
    msg="",
    err="",
)
assert "2 results for “boba”" in search_html
# Owned is stated and offers no Want button: asking for a book already on the
# shelf spends the daily allowance on nothing.
assert "Already in your library." in search_html
assert "Want it — Owned &amp; Here" not in search_html
assert "Want it — Not Here Yet" in search_html
assert "You have already asked for this one." in search_html
# The blurb is fetched on open, so the row must not carry one.
assert 'data-asin="A1"' in search_html
assert "/summary?asin=" in search_html
# A search with no query explains itself rather than showing an empty list.
empty = environment.get_template("search.html").render(
    user_name="alex", query="", results=[], msg="", err="")
assert "Type a title or an author above." in empty

print("template checks passed")
