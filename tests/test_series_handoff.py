"""Nextread and Listenarr must choose the same primary audiobook series.

Audible sometimes lists an unnumbered franchise before the numbered sequence.
Filing on the first row loses the book number and puts the import in the wrong
series directory even though the provider returned the right coordinate.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("series-handoff")

from app import audible, listenarr

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


search_result = {
    "asin": "B071F6R3FF",
    "title": "Annihilation",
    "series": [
        {"name": "Star Wars: Legends"},
        {
            "name": "Star Wars: The Old Republic - Legends",
            "position": "4",
            "asin": "SERIES-ASIN",
        },
    ],
}
metadata = listenarr._to_add_metadata(search_result, region="ca")
check("numbered search series is primary",
      metadata["series"], "Star Wars: The Old Republic - Legends")
check("numbered search position survives", metadata["seriesNumber"], "4")
check("all search memberships survive", len(metadata["seriesMemberships"]), 2)
check("search primary flags",
      [row["isPrimary"] for row in metadata["seriesMemberships"]], [False, True])

unnumbered = listenarr._to_add_metadata({
    "asin": "B0TEST",
    "title": "A Book",
    "series": [{"name": "First"}, {"name": "Second"}],
})
check("first series remains fallback when none is numbered",
      unnumbered["series"], "First")

thin = audible._thin({
    "asin": "B0THIN",
    "title": "Annihilation",
    "series": [
        {"title": "Star Wars: Legends"},
        {"title": "Star Wars: The Old Republic - Legends", "sequence": "4"},
    ],
})
check("sims retain the numbered series",
      thin["series"], "Star Wars: The Old Republic - Legends")
check("sims retain the series position", thin["series_position"], "4")

saved_product = audible.product
try:
    audible.product = lambda asin: {
        "title": "Annihilation",
        "_region": "ca",
        "series": [
            {"title": "Star Wars: Legends"},
            {
                "title": "Star Wars: The Old Republic - Legends",
                "sequence": "4",
                "asin": "SERIES-ASIN",
            },
        ],
    }
    direct = listenarr._from_audible_product("B071F6R3FF")
finally:
    audible.product = saved_product

check("numbered direct-product series is primary",
      (direct or {}).get("series"), "Star Wars: The Old Republic - Legends")
check("direct-product memberships survive",
      len((direct or {}).get("seriesMemberships") or []), 2)
check("direct-product primary flags",
      [row["isPrimary"] for row in (direct or {}).get("seriesMemberships") or []],
      [False, True])

if failures:
    print("FAIL")
    for failure in failures:
        print("  " + failure)
    sys.exit(1)
print("test_series_handoff: all checks passed")
