"""Requesting a book, and reporting what has become of the request.

One code path, used by both the HTML form and the JSON API. Everything that
guards an acquisition -- the daily allowance, the duplicate check, the ledger
entry, the immediate search -- lives here, so a second caller cannot be added
later that quietly skips half of it.
"""
import time

from . import config, jellyfin, listenarr, logs, store

log = logs.get("wants")

#: A request whose book has not arrived and has not been waiting long.
ON_ITS_WAY = "on_its_way"
#: Waiting long enough that calling it an arrival would be a lie. Not a
#: failure: the book stays monitored and Listenarr's sweep keeps retrying.
STILL_LOOKING = "still_looking"
#: The book is in Jellyfin. The row becomes an ordinary library item.
IN_LIBRARY = "in_library"

DAY_SECONDS = 24 * 3600


class Denied(Exception):
    """The request was refused before anything was acquired."""


def allowance(user: jellyfin.User) -> int | None:
    """Requests this account has left today, or None when it is not capped."""
    if user.is_admin:
        return None
    used = store.requests_since(user.key, time.time() - DAY_SECONDS)
    return max(0, config.WANT_DAILY_CAP - used)


def want(user: jellyfin.User, asin: str, title: str = "") -> tuple[str, str]:
    """Ask for one book. Returns (state, message). Raises Denied if refused.

    Ordered so that nothing is charged against the allowance until Listenarr
    has actually accepted the book, and so that a repeated tap is free: the
    ledger entry is keyed on (account, ASIN), and a second one neither restarts
    the clock nor spends another request.
    """
    already = _request_row(user.key, asin)
    if already is not None and already["fulfilled_at"] is None:
        state = _state(already)
        log.info("want repeat user=%s asin=%s state=%s (no allowance spent)",
                 user.key, asin, state)
        return state, "Already on its way"

    remaining = allowance(user)
    if remaining is not None and remaining <= 0:
        log.warning("want denied user=%s asin=%s reason=daily-cap cap=%d",
                    user.key, asin, config.WANT_DAILY_CAP)
        raise Denied(
            f"That is {config.WANT_DAILY_CAP} books today. "
            "The allowance frees up again as the day rolls on.")

    log.info("want user=%s asin=%s title=%r remaining=%s",
             user.key, asin, title, "uncapped" if remaining is None else remaining)
    result = listenarr.add(asin)
    if not result.ok:
        log.warning("want refused user=%s asin=%s reason=%s", user.key, asin, result.message)
        raise Denied(result.message)

    store.record_request(user.key, asin, title)
    log.info("want accepted user=%s asin=%s audiobook_id=%s listenarr=%r",
             user.key, asin, result.audiobook_id, result.message)

    if result.audiobook_id is None:
        # Nothing to hand the queue, so the book waits for the 6-hourly sweep.
        log.warning("want asin=%s has no Listenarr id; immediate search skipped, "
                    "the sweep will pick it up", asin)
    elif listenarr.enqueue_search(result.audiobook_id):
        log.info("search queued asin=%s audiobook_id=%s", asin, result.audiobook_id)
    else:
        log.warning("search queue refused asin=%s audiobook_id=%s; "
                    "the book stays monitored for the 6-hourly sweep",
                    asin, result.audiobook_id)
    return ON_ITS_WAY, result.message


def dismiss(user: jellyfin.User, asin: str) -> None:
    """Never offer this book to this account again."""
    log.info("dismiss user=%s asin=%s", user.key, asin)
    store.dismiss(user.key, asin)


def states(user_key: str, owned_asins: set) -> list[dict]:
    """This account's requests, each with its current state.

    Arrival is a set membership test against the ASINs already on disk, which
    the engine builds on every run anyway. There is no status to fetch from
    Listenarr and nothing to poll: a book has either reached the library or it
    has not.
    """
    rows = store.requests_for(user_key)
    arrived = {r["asin"] for r in rows
               if r["fulfilled_at"] is None and r["asin"] in owned_asins}
    if arrived:
        log.info("requests fulfilled user=%s asins=%s", user_key, sorted(arrived))
    store.fulfil_requests(user_key, arrived)

    out = []
    for row in rows:
        if row["asin"] in arrived:
            row = {**row, "fulfilled_at": time.time()}
        out.append({
            "asin": row["asin"],
            "title": row["title"] or "",
            "requested_at": row["requested_at"],
            "state": _state(row),
        })
    return out


def _request_row(user_key: str, asin: str) -> dict | None:
    for row in store.requests_for(user_key):
        if row["asin"] == asin:
            return row
    return None


def _state(row: dict) -> str:
    if row["fulfilled_at"] is not None:
        return IN_LIBRARY
    waited = time.time() - row["requested_at"]
    if waited > config.STILL_LOOKING_AFTER_HOURS * 3600:
        return STILL_LOOKING
    return ON_ITS_WAY
