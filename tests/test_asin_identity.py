"""An ASIN lookup must never answer with a different book.

On 2026-08-28 a request for "I Ran Away to Evil: A Cozy LitRPG Rom-Com" put a
record in Listenarr with an empty title. Listenarr searched on what it had,
scored "The Waste Lands" as ACCEPTABLE at 100, and downloaded Dark Tower III --
which then imported under `Unknown Author/Unknown Title` because it matched
nothing either. The listener got a book from a series they were not reading,
and their daily request allowance was spent on it.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
os.environ.setdefault("DB_PATH", "/tmp/nextread-test-asin.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import listenarr

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


class FakeResponse:
    def __init__(self, results): self._results = results
    def raise_for_status(self): return self
    def json(self): return {"results": self._results}


class FakeClient:
    def __init__(self, results): self._results = results; self.params = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None): self.params = params; return FakeResponse(self._results)


def lookup(results):
    client = FakeClient(results)
    listenarr._client = lambda: client
    return listenarr.audible_metadata("B0CWW1L8NL"), client


WANTED = {"asin": "B0CWW1L8NL", "title": "I Ran Away to Evil: A Cozy LitRPG Rom-Com",
          "authors": [{"name": "Mystic Neptune"}]}
OTHER = {"asin": "B019NNT1G8", "title": "The Waste Lands",
         "authors": [{"name": "Stephen King"}]}

# The exact match is used, and the region is stated so this and the catalogue
# search cannot resolve to different marketplaces.
meta, client = lookup([WANTED])
check("exact match used", (meta or {}).get("title"), WANTED["title"])
check("region sent", (client.params or {}).get("region"), listenarr.config.AUDIBLE_REGION)
check("asin sent as the query", (client.params or {}).get("query"), "B0CWW1L8NL")

# Found among others, still the right one.
meta, _ = lookup([OTHER, WANTED])
check("exact match found among others", (meta or {}).get("title"), WANTED["title"])

# The one that mattered: results, but none of them is this ASIN.
meta, _ = lookup([OTHER])
check("a different book is refused, not substituted", meta, None)

# A result carrying the right ASIN but no title is refused too: the title is
# what the acquisition searches on, and an empty one takes whatever scores
# first -- the same failure by another route.
meta, _ = lookup([{"asin": "B0CWW1L8NL", "title": ""}])
check("a titleless match is refused", meta, None)

meta, _ = lookup([])
check("no results is refused", meta, None)

if failures:
    print("FAIL")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("test_asin_identity: all checks passed")
