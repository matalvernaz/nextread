"""Nextread -- audiobook recommendations from your own listening history.

Reads Jellyfin (the single library of record), recommends via Audible's
similar-products graph, writes owned picks back as a Jellyfin playlist and hands
approved unowned picks to Listenarr.

The UI is deliberately plain server-rendered HTML with real forms. Every core
action works with no JavaScript, which is the most robust shape for a screen
reader.
"""
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api, config, jellyfin, logs, shelves, store, wants

log = logs.get("main")

app = FastAPI(title="Nextread", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api.router)


@app.on_event("startup")
def _startup() -> None:
    logs.configure()
    store.init()
    log.info("nextread up: libraries=%d cap=%s/day still-looking-after=%dh",
             len(jellyfin.library_ids()), config.WANT_DAILY_CAP,
             config.STILL_LOOKING_AFTER_HOURS)


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


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index(request: Request, msg: str = "", err: str = ""):
    user = _viewer(request)
    data = shelves.result(user)
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
            "requests": wants.states(user.key, data["owned_asins"]),
        },
    )


@app.post("/want")
def want(request: Request, asin: str = Form(...), title: str = Form("")):
    """Ask for one recommendation. Same path the JSON API uses."""
    user = _viewer(request)
    try:
        wants.want(user, asin, title)
    except wants.Denied as denied:
        return RedirectResponse(f"/?err={quote(f'{title}: {denied}')}", status_code=303)
    # Listenarr is shared, so a newly requested book stops being offered to
    # everybody -- but only that book, not everybody's whole shelf.
    shelves.forget_asin(asin)
    return RedirectResponse(
        f"/?msg={quote(f'{title} is on its way')}", status_code=303)


@app.post("/dismiss")
def dismiss(request: Request, asin: str = Form(...), title: str = Form("")):
    """Never show this recommendation again."""
    user = _viewer(request)
    wants.dismiss(user, asin)
    shelves.invalidate(user.key)
    return RedirectResponse(f"/?msg={quote(f'{title} hidden')}", status_code=303)


@app.post("/refresh")
def refresh(request: Request, background: BackgroundTasks):
    """Recompute this user's shelves and rewrite only their playlist."""
    user = _viewer(request)
    background.add_task(shelves.result, user, True)
    return RedirectResponse(
        "/?msg=" + quote("Refresh started. Reload in a moment to see the new list."),
        status_code=303,
    )
