"""Identity, cache, and naming checks for the multi-user request path."""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from starlette.requests import Request

from app import engine, jellyfin, main


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
    assert main._viewer(request()) == matt
    try:
        main._viewer(request("missing"))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("an unmatched SSO account must be rejected")
finally:
    main.jellyfin.user = real_user


calls = []
real_run = main.engine.run
main.engine.run = lambda user: calls.append(user.key) or {"user_name": user.name}
main._invalidate()
try:
    assert main._result(matt)["user_name"] == "matt"
    assert main._result(alex)["user_name"] == "Alex"
    assert main._result(matt)["user_name"] == "matt"
    assert calls == ["matt", "alex"]

    main._invalidate("matt")
    main._result(matt)
    main._result(alex)
    assert calls == ["matt", "alex", "matt"]
finally:
    main.engine.run = real_run
    main._invalidate()


assert engine._playlist_name(matt) == main.config.PLAYLIST_NAME
assert engine._playlist_name(alex) == f"{main.config.PLAYLIST_NAME} — Alex"

bad_id = min(main.config.IGNORED_RATING_ITEM_IDS)
bad_rating = {"Id": bad_id, "UserData": {"Rating": 1}}
assert engine._rating(bad_rating, "matt") is None
assert engine._rating(bad_rating, "alex") == 1

print("multi-user request checks passed")
