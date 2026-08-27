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
assert "Recommendations for <strong>alex</strong>." in html
assert "0 of your ratings detected; rating-based tuning begins at 5." in html
print("template checks passed")
