"""Identity, cache, and naming checks for the multi-user request path."""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from starlette.requests import Request

from app import engine, jellyfin, main, shelves


def request(username: str | None = None) -> Request:
    headers = []
    if username is not None:
        headers.append((main.config.AUTH_USER_HEADER.lower().encode(), username.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


matt = jellyfin.User(id="user-matt", name="matt")
alex = jellyfin.User(id="user-alex", name="Alex")
users = {u.key: u for u in (matt, alex)}

real_user = main.jellyfin.user
main.jellyfin.user = lambda name: users[name.casefold()]
try:
    assert main._viewer(request("ALEX")) == alex

    # An install that sets a fallback gets one: this is how direct access
    # without a proxy works, and it is what this deployment configures.
    main.config.JELLYFIN_USER = "matt"
    assert main._viewer(request()) == matt

    # One that does not must refuse rather than guess. The default is empty on
    # purpose -- a default that names somebody would have a fresh install
    # elsewhere silently resolving a person who does not exist there, and
    # serving one account's shelf to whoever asked.
    main.config.JELLYFIN_USER = ""
    try:
        main._viewer(request())
    except HTTPException as exc:
        assert exc.status_code == 403, exc.status_code
    else:
        raise AssertionError("no header and no fallback must be refused")
    main.config.JELLYFIN_USER = "matt"

    try:
        main._viewer(request("missing"))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("an unmatched SSO account must be rejected")
finally:
    main.jellyfin.user = real_user


calls = []
real_run = shelves.engine.run
shelves.engine.run = (
    lambda user, update_playlist=True:
    calls.append(user.key) or {"user_name": user.name, "own": []})
shelves.invalidate()
try:
    assert shelves.result(matt)["user_name"] == "matt"
    assert shelves.result(alex)["user_name"] == "Alex"
    assert shelves.result(matt)["user_name"] == "matt"
    assert calls == ["matt", "alex"]

    shelves.invalidate("matt")
    shelves.result(matt)
    shelves.result(alex)
    assert calls == ["matt", "alex", "matt"]
finally:
    shelves.engine.run = real_run
    shelves.invalidate()


# An API read must not write a playlist, and must not let a later web read skip
# the write either -- the cache entry is shared between the two surfaces.
written = []
real_run = shelves.engine.run
real_set = shelves.jellyfin.set_playlist
shelves.engine.run = (
    lambda user, update_playlist=True: {
        "user_name": user.name,
        "own": [{"id": "item-1"}],
        "playlist_name": "Next Read",
        "discover": [],
    })
shelves.jellyfin.set_playlist = (
    lambda uid, name, ids: written.append((uid, name, tuple(ids))) or "playlist-1")
shelves.invalidate()
try:
    shelves.result(matt, update_playlist=False)
    assert written == [], "a GET on the API path must not write a playlist"
    shelves.result(matt)
    assert written == [("user-matt", "Next Read", ("item-1",))], written
    shelves.result(matt)
    assert len(written) == 1, "a settled entry must not write again"
finally:
    shelves.engine.run = real_run
    shelves.jellyfin.set_playlist = real_set
    shelves.invalidate()


# One person's request removes that book from everybody's shelf, and nothing
# else: clearing every cache would push every account through a cold recompute.
shelves.invalidate()
with shelves._cache_guard:
    shelves._cache["matt"] = (
        shelves.time.monotonic(),
        {"discover": [{"asin": "A1"}, {"asin": "A2"}], "own": []}, True)
    shelves._cache["alex"] = (
        shelves.time.monotonic(),
        {"discover": [{"asin": "A1"}], "own": []}, True)
shelves.forget_asin("A1")
assert [r["asin"] for r in shelves._cache["matt"][1]["discover"]] == ["A2"]
assert shelves._cache["alex"][1]["discover"] == []
assert set(shelves._cache) == {"matt", "alex"}, "no entry may be evicted"
shelves.invalidate()


assert engine._playlist_name(matt) == main.config.PLAYLIST_NAME
assert engine._playlist_name(alex) == f"{main.config.PLAYLIST_NAME} — Alex"

bad_id = min(main.config.IGNORED_RATING_ITEM_IDS)
bad_rating = {"Id": bad_id, "UserData": {"Rating": 1}}
assert engine._rating(bad_rating, "matt") is None
assert engine._rating(bad_rating, "alex") == 1

print("multi-user request checks passed")
