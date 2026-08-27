"""Nextread -- audiobook recommendations from your own listening history.

Reads Jellyfin (the single library of record), recommends via Audible's
similar-products graph, writes owned picks back as a Jellyfin playlist and hands
approved unowned picks to Listenarr.

The UI is deliberately plain server-rendered HTML with real forms. Every core
action works with no JavaScript, which is the most robust shape for a screen
reader.
"""
import time
from threading import Lock
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, engine, jellyfin, listenarr, store

app = FastAPI(title="Nextread", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Recomputing needs Jellyfin plus (on a cold SQLite cache) Audible. Each user has
# an independent entry and lock: one slow first load must not leak or overwrite
# another user's result, and simultaneous loads for one user should compute once.
_cache: dict[str, tuple[float, dict]] = {}
_cache_locks: dict[str, Lock] = {}
_cache_guard = Lock()
CACHE_TTL_SECONDS = 3600


@app.on_event("startup")
def _startup() -> None:
    store.init()


def _viewer(request: Request) -> jellyfin.User:
    """Resolve the forward-auth identity; never take identity from user input."""
    username = (request.headers.get(config.AUTH_USER_HEADER)
                or config.JELLYFIN_USER).strip()
    try:
        return jellyfin.user(username)
    except LookupError as exc:
        raise HTTPException(
            status_code=403,
            detail=f"Signed-in user {username!r} has no matching Jellyfin account.",
        ) from exc


def _lock_for(user_key: str) -> Lock:
    with _cache_guard:
        return _cache_locks.setdefault(user_key, Lock())


def _fresh_entry(user_key: str) -> dict | None:
    with _cache_guard:
        entry = _cache.get(user_key)
    if entry and time.monotonic() - entry[0] <= CACHE_TTL_SECONDS:
        return entry[1]
    return None


def _result(user: jellyfin.User, force: bool = False) -> dict:
    if not force and (cached := _fresh_entry(user.key)) is not None:
        return cached
    with _lock_for(user.key):
        if not force and (cached := _fresh_entry(user.key)) is not None:
            return cached
        data = engine.run(user)
        with _cache_guard:
            _cache[user.key] = (time.monotonic(), data)
        return data


def _invalidate(user_key: str | None = None) -> None:
    with _cache_guard:
        if user_key is None:
            _cache.clear()
        else:
            _cache.pop(user_key, None)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index(request: Request, msg: str = "", err: str = ""):
    user = _viewer(request)
    data = _result(user)
    last = store.last_run(user.key)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user_name": user.name,
            "own": data["own"],
            "discover": data["discover"],
            "seeds": data["seeds"],
            "library": data["library"],
            "ratings": data["ratings"],
            "rating_floor": config.MIN_RATINGS_FOR_SIGNED_MODE,
            "playlist_name": data["playlist_name"],
            "msg": msg,
            "err": err,
            "last_run": last["finished_at"] if last else None,
        },
    )


@app.post("/want")
def want(request: Request, asin: str = Form(...), title: str = Form("")):
    """Hand one recommendation to Listenarr to acquire."""
    user = _viewer(request)
    ok, message = listenarr.add(asin)
    if ok:
        store.mark_submitted(asin, title, user.key)
        # Listenarr is shared, so a newly queued book disappears for everyone.
        _invalidate()
        return RedirectResponse(f"/?msg={quote(f'{title} sent to Listenarr')}", status_code=303)
    return RedirectResponse(f"/?err={quote(f'{title}: {message}')}", status_code=303)


@app.post("/dismiss")
def dismiss(request: Request, asin: str = Form(...), title: str = Form("")):
    """Never show this recommendation again."""
    user = _viewer(request)
    store.dismiss(user.key, asin)
    _invalidate(user.key)
    return RedirectResponse(f"/?msg={quote(f'{title} hidden')}", status_code=303)


@app.post("/refresh")
def refresh(request: Request, background: BackgroundTasks):
    """Recompute this user's shelves and rewrite only their playlist."""
    user = _viewer(request)
    background.add_task(_result, user, True)
    return RedirectResponse(
        "/?msg=" + quote("Refresh started. Reload in a moment to see the new list."),
        status_code=303,
    )
