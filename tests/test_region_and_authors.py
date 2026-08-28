"""The marketplace, and the author check that decides "we already own this".

Both are the same bug seen from two sides: on 2026-08-27 the Discover shelf
offered Demon World Boba Shop to the account that already had all five books.
`api.audible.com` answers 200 with an empty product for its Canadian-exclusive
ASIN, and even given the right store, "RC Joshua" and "R. C. Joshua" did not
match each other.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("region")

from app import audible, config, engine

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


# --- the marketplace ------------------------------------------------------

check("default region", config.AUDIBLE_REGION, "ca")
check("ca host", audible._host(), "api.audible.ca")
check("ca base", audible._base(), "https://api.audible.ca/1.0/catalog")

saved = config.AUDIBLE_REGION
try:
    config.AUDIBLE_REGION = "us"
    check("us host", audible._host(), "api.audible.com")
    config.AUDIBLE_REGION = "uk"
    check("uk host", audible._host(), "api.audible.co.uk")
    # An unmet region must still resolve to something answerable rather than
    # building "https://None/..." and failing on every call.
    config.AUDIBLE_REGION = "zz"
    check("unknown region falls back", audible._host(), "api.audible.com")
finally:
    config.AUDIBLE_REGION = saved

# --- initials -------------------------------------------------------------

check("spaced initials", engine._norm_author("R. C. Joshua"), "rc joshua")
check("joined initials", engine._norm_author("RC Joshua"), "rc joshua")
check("three initials", engine._norm_author("J.R.R. Tolkien"), "jrr tolkien")
check("three spaced", engine._norm_author("J R R Tolkien"), "jrr tolkien")
check("ordinary name untouched", engine._norm_author("Becky Chambers"), "becky chambers")
# A one-letter word that is not an initial run still joins, which is accepted:
# it only ever compares an author against an author.
check("single surname", engine._norm_author("Virlyce"), "virlyce")

# --- the shelf's own question --------------------------------------------

library = [
    # Book one, tagged as this library actually has it.
    {"Id": "1", "Name": "Demon World Boba Shop", "AlbumArtist": "RC Joshua",
     "ProviderIds": {"Audible": "B0DCHQ9QT7"}},
    {"Id": "2", "Name": "Demon World Boba Shop, Book 2", "AlbumArtist": "R. C. Joshua"},
]
asins, by_title = engine._owned_index(library)

# What Audible hands back for book one, under a different edition's ASIN and
# the punctuated spelling of the author.
candidate = {"asin": "B0OTHEREDITION", "title": "Demon World Boba Shop",
             "authors": ["R. C. Joshua"]}
check("owned despite initials and a different ASIN",
      engine._already_owned(candidate, asins, by_title), True)

check("owned by ASIN alone",
      engine._already_owned({"asin": "B0DCHQ9QT7", "title": "Something Else",
                             "authors": ["Nobody"]}, asins, by_title), True)

# The check must still say no to a book that is genuinely not here, or the
# shelf goes empty.
check("book six is not owned",
      engine._already_owned({"asin": "B0NEW", "title": "Demon World Boba Shop 6",
                             "authors": ["R. C. Joshua"]}, asins, by_title), False)

# Same title, different author: two unrelated books, and suppressing the
# second would hide it forever.
check("same title different author is not owned",
      engine._already_owned({"asin": "B0OTHER", "title": "Demon World Boba Shop",
                             "authors": ["Someone Else"]}, asins, by_title), False)

if failures:
    print("FAIL")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("test_region_and_authors: all checks passed")
