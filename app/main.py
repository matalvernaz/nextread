"""Nextread -- audiobook recommendations from your own listening history.

Reads Jellyfin (the single library of record), recommends via Audible's
similar-products graph, writes owned picks back as a Jellyfin playlist and hands
approved unowned picks to Listenarr.

The UI is deliberately plain server-rendered HTML with real forms. Every core
action works with no JavaScript, which is the most robust shape for a screen
reader.
"""
import os
from urllib.parse import quote, urlsplit

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (api, config, jellyfin, logs, search, selfcheck, shelves, store,
               wants)

log = logs.get("main")

app = FastAPI(title="Nextread", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(api.router)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

#: Additional hostnames whose pages may post to this one. Rarely needed: the
#: default is "the host this request arrived at", which covers every ordinary
#: deployment.
ALLOWED_ORIGIN_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("ALLOWED_ORIGIN_HOSTS", "").split(",") if h.strip()}


@app.middleware("http")
async def block_cross_origin_writes(request: Request, call_next):
    """Refuse a write whose page came from somewhere else.

    The browser pages ride a forward-auth cookie, and that cookie is scoped to
    the parent domain -- so a page on any sibling subdomain can post to this
    one and spend a signed-in person's daily allowance, dismiss their shelf or
    submit picks as them. SameSite does not stop a *same-site* cross-origin
    post and CORS does not apply to form submissions, so checking the origin
    of unsafe methods is the whole mitigation.

    A request carrying neither Origin nor Referer is allowed. Some privacy
    setups strip both, and native clients send neither -- which is also why
    this does not disturb the JSON API, whose callers authenticate on a token
    rather than on a cookie and so have nothing to be ridden.

    Copied from nextup, deliberately verbatim: the two services sit on the
    same parent domain behind the same forward-auth, so the exposure and the
    answer are the same.
    """
    if request.method in SAFE_METHODS:
        return await call_next(request)
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    host = urlsplit(raw).hostname if raw else None
    arrived_at = (request.headers.get("x-forwarded-host")
                  or request.url.hostname or "").split(",")[0].strip().lower()
    expected = ({arrived_at} if arrived_at else set()) | ALLOWED_ORIGIN_HOSTS
    if host and expected and host.lower() not in expected:
        log.warning("refused a write from %s (expected one of %s)",
                    host, sorted(expected))
        return PlainTextResponse(
            f"Refused: this looks like a cross-site request (from {host}).",
            status_code=403)
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    logs.configure()
    store.init()
    _rekey_users_once()
    selfcheck.watch()
    log.info("nextread up: libraries=%d cap=%s/day still-looking-after=%dh",
             len(jellyfin.library_ids()), config.WANT_DAILY_CAP,
             config.STILL_LOOKING_AFTER_HOURS)


def _rekey_users_once() -> None:
    """Move every user-scoped table off display names and onto account ids.

    Deliberately fatal when Jellyfin cannot be asked. Serving id-keyed reads
    over a name-keyed database is the failure this migration exists to
    prevent: every shelf reads empty, every request ledger reads unspent, and
    a listener asks again for a book already on its way.
    """
    if store.user_key_scheme() == "id":
        return
    try:
        names = jellyfin.all_users()
    except Exception as exc:
        raise RuntimeError(
            "cannot rekey user-scoped tables: Jellyfin did not answer") from exc
    store.rekey_users(names)


def _viewer(request: Request) -> jellyfin.User:
    """Resolve the forward-auth identity; never take identity from user input."""
    username = (request.headers.get(config.AUTH_USER_HEADER)
                or config.JELLYFIN_USER).strip()
    if not username:
        # No header and no configured fallback. Guessing here would serve one
        # account's shelf to whoever asked, which is exactly what the header is
        # for. An install that wants direct access sets JELLYFIN_USER.
        raise HTTPException(
            status_code=403,
            detail="No signed-in user. This service expects to sit behind "
                   "authentication that sets a user header.")
    try:
        return jellyfin.user(username)
    except LookupError as exc:
        raise HTTPException(
            status_code=403,
            detail=f"Signed-in user {username!r} has no matching Jellyfin account.",
        ) from exc


@app.get("/healthz")
def healthz(response: Response) -> dict:
    """Liveness, plus the one upstream fault that needs a person.

    A check that fails whenever a downstream is unreachable turns one outage
    into a restart loop, so this does not probe for reachability. A credential
    Jellyfin has stopped accepting is a different thing: every route here
    fails, it will not come right on its own, and without this the container
    reports healthy for the whole time it is useless. The share gateway this
    service borrowed its shape from ran that way for days.
    """
    if jellyfin.credential_rejected():
        log.warning("unhealthy: Jellyfin is refusing this service's API key")
        response.status_code = 503
        return {"ok": False,
                "detail": "Jellyfin is refusing this service's API key."}
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
            "requests": wants.states(user.key, shelves.owned_index(user)),
        },
    )


@app.get("/search")
def search_page(request: Request, q: str = "", msg: str = "", err: str = ""):
    """Find a book by name, rather than waiting for the shelf to offer it.

    Its own page rather than a section of the index: the index computes both
    shelves, and a search should not wait 14.6 seconds behind a cold shelf
    build to answer a title somebody already typed.
    """
    user = _viewer(request)
    query = q.strip()
    results = search.search(user, query) if query else []
    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "user_name": user.name,
            "query": query,
            "results": results,
            "msg": msg,
            "err": err,
        },
    )


@app.get("/summary")
def summary_page(request: Request, asin: str):
    """One blurb, as JSON, for the search page to fetch when it is opened.

    Separate from `/api/summary` because the two are authenticated differently:
    the API takes a Jellyfin access token, which a browser on this page does not
    have, while this side is behind the forward-auth header. `_viewer` is called
    for the same reason the rest of the page does -- not because a blurb is
    private, but so this is not an open proxy to Audible.
    """
    _viewer(request)
    return search.summary(asin)


@app.post("/want")
def want(request: Request, asin: str = Form(...), title: str = Form(""),
         recommendation_id: str | None = Form(None)):
    """Ask for one recommendation. Same path the JSON API uses."""
    user = _viewer(request)
    try:
        wants.want(user, asin, title, recommendation_id)
    except wants.Denied as denied:
        return RedirectResponse(f"/?err={quote(f'{title}: {denied}')}", status_code=303)
    # Listenarr is shared, so a newly requested book stops being offered to
    # everybody -- but only that book, not everybody's whole shelf.
    shelves.forget_asin(asin)
    return RedirectResponse(
        f"/?msg={quote(f'{title} is on its way')}", status_code=303)


@app.post("/cancel")
def cancel(request: Request, asin: str = Form(...), title: str = Form("")):
    """Stop waiting for a book that was asked for. Same path the JSON API uses."""
    user = _viewer(request)
    removed, message = wants.cancel(user, asin)
    key = "msg" if removed else "err"
    return RedirectResponse(
        f"/?{key}={quote(f'{title or asin}: {message}')}", status_code=303)


@app.post("/dismiss")
def dismiss(request: Request, asin: str = Form(...), title: str = Form(""),
            recommendation_id: str | None = Form(None)):
    """Hide this recommendation for the configured cooling-off period."""
    user = _viewer(request)
    wants.dismiss(user, asin, recommendation_id)
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
