"""Direct search: owned books marked rather than hidden, and blurbs on demand."""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
os.environ.setdefault("DB_PATH", "/tmp/nextread-test-search.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])

from app import engine, jellyfin, listenarr, search, shelves, store, wants

store.init()
matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)

LIBRARY = [
    {"Id": "1", "Name": "Demon World Boba Shop", "AlbumArtist": "RC Joshua",
     "ProviderIds": {"Audible": "B0DCHQ9QT7"}},
]
shelves.owned_index = lambda user: engine._owned_index(LIBRARY)

HITS = [
    # The one already on disk, under a different edition's ASIN and the other
    # spelling of the author.
    {"asin": "B0EDITION2", "title": "Demon World Boba Shop",
     "authors": [{"name": "R. C. Joshua"}], "narrators": [{"name": "A Reader"}],
     "lengthMinutes": 700},
    {"asin": "B0UNOWNED", "title": "Something Else Entirely",
     "authors": [{"name": "Another Author"}], "lengthMinutes": 500},
    # A row with no ASIN cannot be requested and must not reach the client as a
    # tappable result.
    {"title": "No Identifier Here", "authors": []},
]
listenarr.audible_search = lambda query, limit=25: list(HITS)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


results = search.search(matt, "boba")
check("asin-less row dropped", len(results), 2)
by_asin = {r["asin"]: r for r in results}
check("owned marked, not hidden", by_asin["B0EDITION2"]["owned"], True)
check("owned still listed", "B0EDITION2" in by_asin, True)
check("unowned is unowned", by_asin["B0UNOWNED"]["owned"], False)
check("authors flattened", by_asin["B0EDITION2"]["authors"], ["R. C. Joshua"])
check("narrators flattened", by_asin["B0EDITION2"]["narrators"], ["A Reader"])
check("runtime carried", by_asin["B0EDITION2"]["runtimeMinutes"], 700)
check("no blurb in a search row", "description" in by_asin["B0UNOWNED"], False)

check("empty query asks nothing", search.search(matt, "   "), [])

# An outstanding request is flagged; one that has arrived is described by
# `owned` instead, so the two never both claim the book.
store.record_request(matt.key, "B0UNOWNED", "Something Else Entirely")
check("outstanding request flagged",
      {r["asin"]: r["requested"] for r in search.search(matt, "boba")}["B0UNOWNED"], True)

# --- summaries ------------------------------------------------------------

search.store_backed_product = lambda asin: {
    "title": "Demon World Boba Shop",
    "authors": [{"name": "RC Joshua"}],
    "runtime_length_min": 700,
    "merchandising_summary": "short",
    "publisher_summary": "the longer one",
}
got = search.summary("B0DCHQ9QT7")
check("prefers the longer blurb", got["summary"], "the longer one")
check("summary carries the title", got["title"], "Demon World Boba Shop")

search.store_backed_product = lambda asin: None
check("no product is empty text, not a crash", search.summary("B0MISSING")["summary"], "")

if failures:
    print("FAIL")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("test_search_and_summary: all checks passed")
