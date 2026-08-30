"""The v2 ranker invariants through one complete, side-effect-free run."""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("ranker-v2")
harness.discard(DB_PATH)

from app import config, engine, jellyfin, listenarr, store

store.init()


def book(iid, title, *, played=False, series=None, position=None, asin=None):
    row = {
        "Id": iid,
        "Name": title,
        "Overview": "kindly heroes build a magical home together",
        "Genres": ["Fantasy"],
        "People": [{"Type": "Author", "Name": "A. Writer"}],
        "UserData": {"Played": played},
        "ProviderIds": {},
    }
    if series:
        row["SeriesName"] = series
        row["IndexNumber"] = position
    if asin:
        row["ProviderIds"]["Audible"] = asin
    return row


library = [
    book("v1", "Saga One", played=True, series="Saga", position=1, asin="SEED"),
    book("v2", "Saga Two", series="Saga", position=2),
    book("v3", "Saga Three", series="Saga", position=3),
    book("other", "Another Book"),
]

similar = [
    {
        "asin": "BAD-SIX",
        "title": "Unknown Six",
        "authors": ["Other Author"],
        "narrators": [],
        "series": "Unknown Saga",
        "series_position": "6",
        "description": "kindly heroes build a magical home together",
    },
    {
        "asin": "EDITION-CA",
        "title": "New Adventure: An Audiobook",
        "authors": ["A. Writer"],
        "narrators": ["A Narrator"],
        "series": None,
        "series_position": None,
        "description": "kindly heroes build a magical home together",
    },
    {
        "asin": "EDITION-US",
        "title": "New Adventure",
        "authors": ["A Writer"],
        "narrators": ["A Narrator"],
        "series": None,
        "series_position": None,
        "description": "kindly heroes build a magical home together",
    },
]

saved = {
    "books": jellyfin.books,
    "seed_sims": engine._seed_sims,
    "queued": listenarr.queued_asins,
    "playlist": jellyfin.set_playlist,
    "max_shelf": config.MAX_SHELF,
}
playlist_writes = []
try:
    jellyfin.books = lambda uid: library
    engine._seed_sims = lambda seed: similar
    listenarr.queued_asins = lambda: set()
    jellyfin.set_playlist = (
        lambda uid, name, ids: playlist_writes.append((uid, name, ids)) or "playlist")
    config.MAX_SHELF = 10

    user = jellyfin.User(id="user", name="Listener")
    result = engine.run(user, update_playlist=False)
finally:
    jellyfin.books = saved["books"]
    engine._seed_sims = saved["seed_sims"]
    listenarr.queued_asins = saved["queued"]
    jellyfin.set_playlist = saved["playlist"]
    config.MAX_SHELF = saved["max_shelf"]

assert playlist_writes == [], "a read-only shelf request must not write Jellyfin"
assert [row["id"] for row in result["own"]] == ["v2", "other"], result["own"]
assert result["own"][0]["why"][0].startswith("next in Saga")
assert "v3" not in {row["id"] for row in result["own"]}

discover_asins = {row["asin"] for row in result["discover"]}
assert "BAD-SIX" not in discover_asins, "do not start an unfamiliar series at book six"
assert discover_asins == {"EDITION-CA"}, "alternate editions must consume one slot"

all_rows = result["own"] + result["discover"]
assert all(row["why"] for row in all_rows), "every recommendation must be explainable"
assert all(row["recommendation_id"].startswith(f"{result['run_id']}:") for row in all_rows)
with store.db() as conn:
    snapshots = conn.execute(
        "SELECT surface,item_key,ranker_version FROM recommendation_items "
        "WHERE run_id=? ORDER BY surface,rank",
        (result["run_id"],),
    ).fetchall()
assert len(snapshots) == len(all_rows)
assert {row["ranker_version"] for row in snapshots} == {engine.RANKER_VERSION}

harness.discard(DB_PATH)
print("ranker v2 integration checks passed")
