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

from . import config, jellyfin, logs, search, shelves, wants

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
        # Expired entries are dropped here rather than left to accumulate: the
        # only thing that ever reads them again is this same lookup, so nothing
        # else would clear a rotated token's row for the life of the process.
        cutoff = time.monotonic() - config.TOKEN_CACHE_SECONDS
        for stale in [k for k, (at, _) in _tokens.items() if at <= cutoff]:
            del _tokens[stale]
        _tokens[digest] = (time.monotonic(), user)
    return user


@router.get("/info")
def info() -> dict:
    """That this service is here, answered without a token.

    It exists so absence and malfunction stop being the same answer. A client
    looks for this service at the Jellyfin origin, which most servers do not
    serve it from, so a 404 there has to mean "not installed" -- and it also
    means a missing proxy rule, a stopped container, a rejected token and a
    version this client cannot read, none of which the client can tell apart
    while every route needs credentials first.

    Deliberately says nothing about anybody, and deliberately does not list
    features: `/capabilities` negotiates those, per account, and a second list
    here would be one to keep in step for no gain.
    """
    return {"service": config.SERVICE_NAME, "protocol": config.API_VERSION}


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
        # Named blocks rather than a version bump: a client that predates either
        # route asks for neither, and one that postdates a server without them
        # hides its own control instead of failing a tap.
        "search": {"supported": True, "limit": config.SEARCH_LIMIT},
        "summary": {"supported": True},
        "cancel": {"supported": True},
        "dismiss": {
            "supported": True,
            "undo": True,
            "days": config.DISMISS_TTL_DAYS,
        },
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
        "runId": data.get("run_id"),
        "rankerVersion": data.get("ranker_version"),
        "owned": [{
            "id": row["id"],
            "title": row["title"],
            "reason": row["why"],
            "recommendationId": row.get("recommendation_id"),
            "source": row.get("source"),
        }
                  for row in data["own"]],
        "suggestions": [_suggestion(row) for row in data["discover"]],
        # The index rather than the shelf's own view of what is owned: a book
        # arrives under the ASIN the other marketplace issued for it, so
        # arrival is decided on title and author too.
        "requests": wants.states(user.key, shelves.owned_index(user)),
    }


@router.get("/search")
def get_search(q: str = "", user: jellyfin.User = Depends(caller)) -> dict:
    """Catalogue hits for a title the listener already has in mind.

    Owned books are marked, not dropped: on the shelf an owned book is noise,
    but to somebody typing its title it is the answer.
    """
    return {
        "version": config.API_VERSION,
        "query": q.strip(),
        "results": search.search(user, q),
    }


@router.get("/summary")
def get_summary(asin: str, user: jellyfin.User = Depends(caller)) -> dict:
    """One book's blurb, for a book that is not in the library to describe it.

    Its own request rather than a field on every row: a blurb is one Audible
    call per book, and a shelf or a search would pay for twenty-five of them to
    show text nobody has opened.
    """
    found = search.summary(asin)
    if not found["summary"]:
        # Not a 404: the book exists, the blurb does not, and a client that
        # cannot tell those apart shows the wrong message.
        log.info("summary empty asin=%s", asin)
    return {"version": config.API_VERSION, **found}


@router.post("/want")
def post_want(user: jellyfin.User = Depends(caller),
              asin: str = Body(..., embed=True),
              title: str = Body("", embed=True),
              recommendation_id: str | None = Body(
                  None, embed=True, alias="recommendationId")) -> dict:
    """Ask for one book. Repeating it is free and does not spend the allowance."""
    log.info("api want user=%s asin=%s", user.key, asin)
    try:
        state, message = wants.want(user, asin, title, recommendation_id)
    except wants.Denied as denied:
        raise HTTPException(status_code=409, detail=str(denied)) from denied
    shelves.forget_asin(asin)
    return {"asin": asin, "state": state, "message": message,
            "remainingToday": wants.allowance(user)}


@router.post("/cancel")
def post_cancel(user: jellyfin.User = Depends(caller),
                asin: str = Body(..., embed=True)) -> dict:
    """Take one book off this account's list, and stop looking for it.

    Not a DELETE: the ASIN is the marketplace's, not this app's, and putting it
    in a path would need it escaped by every client that has one. It also does
    more than erase a row -- it calls an acquisition off -- and `cancel` says so
    where a method alone would not.
    """
    removed, message = wants.cancel(user, asin)
    if not removed:
        raise HTTPException(status_code=404, detail=message)
    return {"asin": asin, "removed": True, "message": message,
            "remainingToday": wants.allowance(user)}


@router.post("/dismiss")
def post_dismiss(user: jellyfin.User = Depends(caller),
                 asin: str = Body(..., embed=True),
                 recommendation_id: str | None = Body(
                     None, embed=True, alias="recommendationId")) -> dict:
    """Hide this book for the configured cooling-off period."""
    wants.dismiss(user, asin, recommendation_id)
    shelves.invalidate(user.key)
    return {"asin": asin, "dismissed": True, "days": config.DISMISS_TTL_DAYS}


@router.post("/restore")
def post_restore(user: jellyfin.User = Depends(caller),
                 asin: str = Body(..., embed=True),
                 recommendation_id: str | None = Body(
                     None, embed=True, alias="recommendationId")) -> dict:
    """Undo a dismissal made by this account."""
    if not wants.restore(user, asin, recommendation_id):
        raise HTTPException(status_code=404, detail="That suggestion is not hidden.")
    shelves.invalidate(user.key)
    return {"asin": asin, "restored": True}


def _suggestion(row: dict) -> dict:
    """One unowned recommendation, as little of it as a client needs to show."""
    return {
        "asin": row.get("asin"),
        "title": row.get("title") or "",
        "authors": row.get("authors") or [],
        "narrators": row.get("narrators") or [],
        "series": row.get("series"),
        "seriesPosition": row.get("series_position"),
        "runtimeMinutes": row.get("runtime_min"),
        "description": row.get("description") or "",
        "reason": row.get("why") or [],
        "recommendationId": row.get("recommendation_id"),
        "source": row.get("source"),
        "state": "available",
    }
