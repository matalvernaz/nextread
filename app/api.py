"""The JSON API, for clients that cannot sign in through a browser.

Everything here is reachable without the SSO proxy in front of it, because a
native app has no way to complete an oauth2-proxy flow. That makes this module
the only place in the app that authenticates a caller itself, and the only
place where getting authentication wrong exposes somebody else's shelf.

Two rules follow from that and are load-bearing:

* Identity comes from introspecting the caller's own Jellyfin access token, and
  a failure to introspect is a rejection. The HTML resolver's fallback to
  ``JELLYFIN_USER`` must never be reachable from here.
* A GET has no side effects. Shelves are read without writing a playlist.
"""
import hashlib
import time
from threading import Lock

from fastapi import APIRouter, Body, Depends, Header, HTTPException

from . import config, jellyfin, logs, shelves, wants

log = logs.get("api")

router = APIRouter(prefix="/api/v1")

# Introspection results, keyed by a digest of the token rather than the token.
# Short-lived on purpose: expiry is the only thing that makes a token revoked in
# Jellyfin stop working here.
_tokens: dict[str, tuple[float, jellyfin.User]] = {}
_tokens_guard = Lock()


def _cached_user(digest: str) -> jellyfin.User | None:
    with _tokens_guard:
        entry = _tokens.get(digest)
    if entry and time.monotonic() - entry[0] <= config.TOKEN_CACHE_SECONDS:
        return entry[1]
    return None


def caller(authorization: str | None = Header(default=None),
           x_emby_token: str | None = Header(default=None)) -> jellyfin.User:
    """The authenticated account behind this request.

    Accepts the token either inside the usual Jellyfin handshake header --
    ``MediaBrowser Token="...", Client="...", Device="..."`` -- or as the
    ``X-Emby-Token`` header some clients send instead. Never from the query
    string: this fork rejects ``?api_key=`` outright, and a token in a URL ends
    up in access logs.
    """
    token = jellyfin.token_from_header(authorization) or (x_emby_token or "").strip()
    if not token:
        log.warning("api call with no access token")
        raise HTTPException(status_code=401, detail="No Jellyfin access token.")

    digest = hashlib.sha256(token.encode()).hexdigest()
    if (user := _cached_user(digest)) is not None:
        log.debug("caller %s (cached)", user.key)
        return user
    try:
        user = jellyfin.user_from_token(token)
    except jellyfin.TokenRejected as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except jellyfin.JellyfinUnavailable as exc:
        # Serving anything at all here would mean guessing at authorisation.
        raise HTTPException(
            status_code=503, detail="Jellyfin is unreachable.") from exc
    with _tokens_guard:
        _tokens[digest] = (time.monotonic(), user)
    return user


@router.get("/capabilities")
def capabilities(user: jellyfin.User = Depends(caller)) -> dict:
    """What this server supports, and what this account may do.

    Deliberately reports configured support rather than live reachability. A
    reachability probe would flap, would make a client's first screen wait on a
    downstream timeout, and still could not promise the next request will
    succeed -- so the POST stays authoritative about its own outcome.
    """
    remaining = wants.allowance(user)
    log.info("capabilities user=%s keyholder=%s remaining=%s",
             user.key, user.is_admin, remaining)
    return {
        "version": config.API_VERSION,
        "user": {"id": user.id, "name": user.name, "keyholder": user.is_admin},
        "libraryIds": jellyfin.library_ids(),
        "playlistName": config.PLAYLIST_NAME,
        "want": {
            "supported": True,
            "dailyCap": None if user.is_admin else config.WANT_DAILY_CAP,
            "remainingToday": remaining,
        },
        "states": [wants.ON_ITS_WAY, wants.STILL_LOOKING, wants.IN_LIBRARY],
    }


@router.get("/shelves")
def get_shelves(user: jellyfin.User = Depends(caller)) -> dict:
    """Both shelves, plus this account's outstanding requests and their state.

    ``owned`` carries Jellyfin item ids rather than rendered rows: the client
    hydrates them through its ordinary item request, so resume position,
    downloads and play-on-activation all keep working. Only the unowned half
    has to be described here, because it has no library item to describe.
    """
    data = shelves.result(user, update_playlist=False)
    log.info("shelves served user=%s owned=%d suggestions=%d",
             user.key, len(data["own"]), len(data["discover"]))
    return {
        "version": config.API_VERSION,
        "owned": [{"id": row["id"], "title": row["title"], "reason": row["why"]}
                  for row in data["own"]],
        "suggestions": [_suggestion(row) for row in data["discover"]],
        "requests": wants.states(user.key, data["owned_asins"]),
    }


@router.post("/want")
def post_want(user: jellyfin.User = Depends(caller),
              asin: str = Body(..., embed=True),
              title: str = Body("", embed=True)) -> dict:
    """Ask for one book. Repeating it is free and does not spend the allowance."""
    log.info("api want user=%s asin=%s", user.key, asin)
    try:
        state, message = wants.want(user, asin, title)
    except wants.Denied as denied:
        raise HTTPException(status_code=409, detail=str(denied)) from denied
    shelves.forget_asin(asin)
    return {"asin": asin, "state": state, "message": message,
            "remainingToday": wants.allowance(user)}


@router.post("/dismiss")
def post_dismiss(user: jellyfin.User = Depends(caller),
                 asin: str = Body(..., embed=True)) -> dict:
    """Never offer this book to this account again."""
    wants.dismiss(user, asin)
    shelves.invalidate(user.key)
    return {"asin": asin, "dismissed": True}


def _suggestion(row: dict) -> dict:
    """One unowned recommendation, as little of it as a client needs to show."""
    return {
        "asin": row.get("asin"),
        "title": row.get("title") or "",
        "authors": row.get("authors") or [],
        "narrators": row.get("narrators") or [],
        "series": row.get("series"),
        "runtimeMinutes": row.get("runtime_min"),
        "description": row.get("description") or "",
        "reason": row.get("why") or [],
        "state": "available",
    }
