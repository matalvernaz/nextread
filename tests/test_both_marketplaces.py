"""This library spans two Audible stores, so one region is never enough.

Measured 2026-08-28: B0CWW1L8NL ("I Ran Away to Evil") is on audible.ca and
answers `200` with an EMPTY product on .com; B0HC7V8ZR4 ("Unicorn Breeder") is
the other way round. Whichever single region were configured, half the library
would look like books that do not exist -- and an empty product is what let a
request be filled with a completely different book.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("regions")

harness.discard(DB_PATH)

from app import audible, config, listenarr, store

store.init()
failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


check("both stores configured by default", config.AUDIBLE_REGIONS, ["ca", "us"])
check("preferred is the first", config.AUDIBLE_REGION, "ca")
check("ca host", audible._host("ca"), "api.audible.ca")
check("us host", audible._host("us"), "api.audible.com")

# An empty product is how a store says "not sold here" -- 200, no error.
check("an empty product is not a product", audible._has_product({"asin": "X"}), False)
check("a titled product is", audible._has_product({"title": "A Book"}), True)
check("nothing is not a product", audible._has_product(None), False)

# --- metadata: the exact ASIN, from whichever store has it -----------------

class FakeResponse:
    def __init__(self, results): self._results = results
    def raise_for_status(self): return self
    def json(self): return {"results": self._results}


class FakeClient:
    """Answers per region, so 'only the second store has it' is expressible."""
    def __init__(self, by_region): self.by_region = by_region; self.asked = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None):
        region = (params or {}).get("region")
        self.asked.append(region)
        return FakeResponse(self.by_region.get(region, []))


US_ONLY = {"asin": "B0HC7V8ZR4", "title": "Unicorn Breeder",
           "authors": [{"name": "Virgil Knightley"}]}
CA_ONLY = {"asin": "B0CWW1L8NL", "title": "I Ran Away to Evil",
           "authors": [{"name": "Mystic Neptune"}]}

client = FakeClient({"ca": [], "us": [US_ONLY]})
listenarr._client = lambda: client
meta = listenarr.audible_metadata("B0HC7V8ZR4")
check("a US-only book is found", (meta or {}).get("title"), "Unicorn Breeder")
check("both stores were asked", client.asked, ["ca", "us"])
check("filed under the store that had it", (meta or {}).get("region"), "us")

client = FakeClient({"ca": [CA_ONLY], "us": [US_ONLY]})
listenarr._client = lambda: client
meta = listenarr.audible_metadata("B0CWW1L8NL")
check("the preferred store is tried first", client.asked, ["ca"])
check("and answers", (meta or {}).get("title"), "I Ran Away to Evil")
check("filed under ca", (meta or {}).get("region"), "ca")

# The safeguard must survive the change: neither store having this ASIN is a
# refusal, never a substitution.
client = FakeClient({"ca": [US_ONLY], "us": [US_ONLY]})
listenarr._client = lambda: client
check("no store has it, so nothing is substituted",
      listenarr.audible_metadata("B0NOSUCHASIN"), None)

# --- search: merged, not first-store-wins ----------------------------------

client = FakeClient({"ca": [CA_ONLY], "us": [US_ONLY]})
listenarr._client = lambda: client
rows = listenarr.audible_search("litrpg")
check("both stores' hits are offered", sorted(r["asin"] for r in rows),
      ["B0CWW1L8NL", "B0HC7V8ZR4"])
check("the preferred store's hit leads", rows[0]["asin"], "B0CWW1L8NL")

# A book both stores sell is one book.
client = FakeClient({"ca": [CA_ONLY], "us": [CA_ONLY]})
listenarr._client = lambda: client
check("sold in both, listed once", len(listenarr.audible_search("cozy")), 1)

if failures:
    print("FAIL")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("test_both_marketplaces: all checks passed")
