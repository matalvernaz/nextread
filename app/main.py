"""Nextread -- audiobook recommendations from your own listening history.

Reads Jellyfin (the single library of record), recommends via Audible's
similar-products graph, writes owned picks back as a Jellyfin playlist and hands
approved unowned picks to Listenarr.

The UI is deliberately plain server-rendered HTML with real forms. Every core
action works with no JavaScript, which is the most robust shape for a screen
reader.
"""
import time
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, engine, listenarr, store

app = FastAPI(title="Nextread", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Recomputing needs Jellyfin plus (on a cold cache) Audible, so the last result
# is held in memory. Audible is never called on a page load.
_cache: dict = {"at": 0.0, "data": None}
CACHE_TTL_SECONDS = 3600


@app.on_event("startup")
def _startup() -> None:
    store.init()


def _result(force: bool = False) -> dict:
    if force or _cache["data"] is None or time.time() - _cache["at"] > CACHE_TTL_SECONDS:
        _cache["data"] = engine.run()
        _cache["at"] = time.time()
    return _cache["data"]


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index(request: Request, msg: str = "", err: str = ""):
    data = _result()
    last = store.last_run()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "own": data["own"],
            "discover": data["discover"],
            "seeds": data["seeds"],
            "library": data["library"],
            "playlist_name": config.PLAYLIST_NAME,
            "msg": msg,
            "err": err,
            "last_run": last["finished_at"] if last else None,
        },
    )


@app.post("/want")
def want(asin: str = Form(...), title: str = Form("")):
    """Hand one recommendation to Listenarr to acquire."""
    ok, message = listenarr.add(asin)
    if ok:
        store.mark_submitted(asin, title)
        _cache["data"] = None
        return RedirectResponse(f"/?msg={quote(f'{title} sent to Listenarr')}", status_code=303)
    return RedirectResponse(f"/?err={quote(f'{title}: {message}')}", status_code=303)


@app.post("/dismiss")
def dismiss(asin: str = Form(...), title: str = Form("")):
    """Never show this recommendation again."""
    store.dismiss(asin)
    _cache["data"] = None
    return RedirectResponse(f"/?msg={quote(f'{title} hidden')}", status_code=303)


@app.post("/refresh")
def refresh(background: BackgroundTasks):
    """Recompute both shelves and rewrite the Jellyfin playlist."""
    background.add_task(_result, True)
    return RedirectResponse(
        "/?msg=" + quote("Refresh started. Reload in a moment to see the new list."),
        status_code=303,
    )
