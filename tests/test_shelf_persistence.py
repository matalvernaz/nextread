"""The shelf must never make somebody wait twice for the same twelve seconds.

Measured on the live server 2026-08-27: a cold build is 12.6s, of which 9.5s is
one Jellyfin listing of 3,352 books. It is that slow because it asks for
`People`, and it cannot stop: 414 books here carry an Author person and no
AlbumArtist, so dropping the field would lose their author outright.
"""
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
os.environ.setdefault("DB_PATH", "/tmp/nextread-test-persist.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])

from app import engine, jellyfin, shelves, store

store.init()
matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)

builds = []
# Held shut to prove the stale answer is served WHILE a rebuild is outstanding.
# Without it the fake finishes first and the test proves nothing about order.
gate = threading.Event()
gate.set()


def fake_run(user, update_playlist=True):
    gate.wait()
    builds.append(user.key)
    return {
        "own": [{"id": "i1", "title": "A Book", "why": []}],
        "discover": [{"asin": "A1", "title": "One"}, {"asin": "A2", "title": "Two"}],
        "owned_index": ({"B0OWNED"}, {"a book": {"someone"}}),
        "playlist_name": "Next Read",
        "playlist_id": None,
        "seeds": 1, "library": 2, "ratings": 0,
    }


engine.run = fake_run
shelves.write_playlist = lambda user, data: None

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


# First ever: somebody pays for it.
first = shelves.result(matt, update_playlist=False)
check("built once", len(builds), 1)
check("discover carried", len(first["discover"]), 2)

# Warm: free.
shelves.result(matt, update_playlist=False)
check("cache hit does not rebuild", len(builds), 1)

# A restart empties the process cache. The answer has to come from disk, and
# the rebuild has to happen behind it rather than in front of it.
shelves._cache.clear()
gate.clear()
restored = shelves.result(matt, update_playlist=False)
check("restored without a synchronous rebuild", len(builds), 1)
check("restored content", len(restored["discover"]), 2)
# The index is far larger than the shelf and its sets do not survive JSON, so a
# build hands it straight to the owned cache and it never reaches the payload.
check("the index is not persisted with the shelf", "owned_index" in restored, False)
check("it is published instead", shelves.owned_index(matt)[0], {"B0OWNED"})

# Now let the rebuild finish, and confirm it happened exactly once.
gate.set()
for _ in range(100):
    if len(builds) > 1:
        break
    time.sleep(0.05)
check("refreshed behind the answer", len(builds), 2)

# Invalidation must reach the disk copy, or a dismissal comes straight back.
for _ in range(100):
    if not shelves._refreshing:
        break
    time.sleep(0.05)
shelves.invalidate(matt.key)
check("persisted copy dropped too", store.get_shelf(matt.key), None)

# A withdrawn book must not survive a restart on the persisted copy.
shelves.result(matt, update_playlist=False)
shelves.forget_asin("A1")
shelves._cache.clear()
after = shelves.result(matt, update_playlist=False)
check("withdrawn book stays withdrawn across a restart",
      [r["asin"] for r in after["discover"]], ["A2"])

if failures:
    print("FAIL")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("test_shelf_persistence: all checks passed")
