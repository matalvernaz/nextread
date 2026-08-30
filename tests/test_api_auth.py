"""The JSON API fails closed.

This route bypasses the SSO proxy, so these are the checks that stand between a
stranger and somebody else's shelf. The one that matters most is the last: the
HTML resolver falls back to JELLYFIN_USER when it sees no identity, and that
fallback must be unreachable from here.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("api")

harness.discard(DB_PATH)

from fastapi.testclient import TestClient

from app import api, config, jellyfin, listenarr, main, shelves, store, wants

store.init()

matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)
kadija = jellyfin.User(id="user-kadija", name="kadija")
TOKENS = {"matt-token": matt, "kadija-token": kadija}

introspections = []


def fake_introspect(token: str) -> jellyfin.User:
    introspections.append(token)
    if token == "jellyfin-down":
        raise jellyfin.JellyfinUnavailable("connection refused")
    if token not in TOKENS:
        raise jellyfin.TokenRejected("Jellyfin answered 401")
    return TOKENS[token]


jellyfin.user_from_token = fake_introspect
jellyfin.library_ids = lambda: ["lib-audio", "lib-graphic"]
shelves.engine.run = lambda user, update_playlist=True: {
    "user_name": user.name, "own": [], "discover": [], "owned_index": (set(), {}),
    "playlist_name": "Next Read",
}
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(True, "Sent", 1)
listenarr.enqueue_search = lambda audiobook_id: True

client = TestClient(main.app)


def auth(token: str) -> dict:
    return {"Authorization": f'MediaBrowser Token="{token}", Client="EchoFin"'}


# --- rejection --------------------------------------------------------------
assert client.get("/api/v1/capabilities").status_code == 401, "no token"
assert client.get("/api/v1/capabilities",
                  headers={"Authorization": 'MediaBrowser Client="EchoFin"'}
                  ).status_code == 401, "handshake with no Token= field"
assert client.get("/api/v1/capabilities", headers=auth("nonsense")).status_code == 401
assert client.get("/api/v1/shelves", headers=auth("nonsense")).status_code == 401
assert client.post("/api/v1/want", headers=auth("nonsense"),
                   json={"asin": "A"}).status_code == 401

# Jellyfin unreachable is 503, never a guess at who the caller might be.
assert client.get("/api/v1/capabilities",
                  headers=auth("jellyfin-down")).status_code == 503

# THE ONE THAT MATTERS: the SSO header alone must not authenticate an API call.
# On the bypassed route a client can set any header it likes, and the HTML
# resolver's fallback to JELLYFIN_USER would hand over the owner's account.
spoofed = client.get("/api/v1/capabilities",
                     headers={config.AUTH_USER_HEADER: config.JELLYFIN_USER})
assert spoofed.status_code == 401, spoofed.status_code


# --- acceptance -------------------------------------------------------------
caps = client.get("/api/v1/capabilities", headers=auth("kadija-token"))
assert caps.status_code == 200, caps.text
body = caps.json()
assert body["version"] == config.API_VERSION
assert body["user"]["name"] == "kadija"
assert body["user"]["keyholder"] is False
assert body["want"]["dailyCap"] == config.WANT_DAILY_CAP
assert body["libraryIds"] == ["lib-audio", "lib-graphic"]
assert body["dismiss"] == {
    "supported": True, "undo": True, "days": config.DISMISS_TTL_DAYS}

keyholder = client.get("/api/v1/capabilities", headers=auth("matt-token")).json()
assert keyholder["user"]["keyholder"] is True
assert keyholder["want"]["dailyCap"] is None
assert keyholder["want"]["remainingToday"] is None

# An X-Emby-Token carries the same weight; a query string never does.
assert client.get("/api/v1/capabilities",
                  headers={"X-Emby-Token": "matt-token"}).status_code == 200
assert client.get("/api/v1/capabilities?api_key=matt-token").status_code == 401


# --- introspection is cached, but not for long -------------------------------
before = len(introspections)
client.get("/api/v1/capabilities", headers=auth("matt-token"))
assert len(introspections) == before, "a repeat call must reuse the cached identity"
api._tokens.clear()
client.get("/api/v1/capabilities", headers=auth("matt-token"))
assert len(introspections) == before + 1, "an expired entry must re-introspect"


# --- the cap answers on the API path too ------------------------------------
for n in range(config.WANT_DAILY_CAP):
    resp = client.post("/api/v1/want", headers=auth("kadija-token"),
                       json={"asin": f"API{n}", "title": f"Book {n}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == wants.ON_ITS_WAY
assert client.post("/api/v1/want", headers=auth("kadija-token"),
                   json={"asin": "APIX", "title": "Over"}).status_code == 409

# Optional recommendation ids survive the camel-case wire contract and are
# accepted only when the row belongs to the caller and the same ASIN.
run_id = store.start_run(matt.key)
recommendation_id = f"{run_id}:discover:1:TRACKED"
store.record_recommendations(run_id, matt.key, "discover", [{
    "asin": "TRACKED", "score": 12, "source": "audible_sims",
    "why": ["alongside a book"], "recommendation_id": recommendation_id,
}], "2")
tracked = client.post(
    "/api/v1/want", headers=auth("matt-token"),
    json={"asin": "TRACKED", "title": "Tracked", "recommendationId": recommendation_id},
)
assert tracked.status_code == 200, tracked.text
with store.db() as conn:
    feedback = conn.execute(
        "SELECT recommendation_id FROM feedback_events "
        "WHERE user_key=? AND asin=? AND action='want' ORDER BY id DESC LIMIT 1",
        (matt.key, "TRACKED"),
    ).fetchone()
assert feedback["recommendation_id"] == recommendation_id

hidden_id = f"{run_id}:discover:2:HIDE-ME"
store.record_recommendations(run_id, matt.key, "discover", [{
    "asin": "HIDE-ME", "score": 11, "source": "audible_sims",
    "why": [], "recommendation_id": hidden_id,
}], "2")
hidden = client.post(
    "/api/v1/dismiss", headers=auth("matt-token"),
    json={"asin": "HIDE-ME", "recommendationId": hidden_id},
)
assert hidden.status_code == 200, hidden.text
restored = client.post(
    "/api/v1/restore", headers=auth("matt-token"),
    json={"asin": "HIDE-ME", "recommendationId": hidden_id},
)
assert restored.status_code == 200, restored.text
assert client.post(
    "/api/v1/restore", headers=auth("matt-token"),
    json={"asin": "HIDE-ME"},
).status_code == 404

# A GET must not have written a playlist.
written = []
shelves.jellyfin.set_playlist = lambda uid, name, ids: written.append(uid)
shelves.invalidate()
assert client.get("/api/v1/shelves", headers=auth("matt-token")).status_code == 200
assert written == [], "the API's shelf read must not write a playlist"

harness.discard(DB_PATH)
print("api auth checks passed")
