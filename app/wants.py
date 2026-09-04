"""Requesting a book, and reporting what has become of the request.

One code path, used by both the HTML form and the JSON API. Everything that
guards an acquisition -- the daily allowance, the duplicate check, the ledger
entry, the immediate search -- lives here, so a second caller cannot be added
later that quietly skips half of it.
"""
import re
import time

from . import config, engine, jellyfin, listenarr, logs, store

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


class AllowanceExhausted(Denied):
    """Refused because the day's allowance is spent, and for no other reason.

    Its own type so a caller asking for several books can tell the cap from
    Listenarr declining one of them: the first ends the batch, the second
    does not.
    """


def allowance(user: jellyfin.User) -> int | None:
    """Requests this account has left today, or None when it is not capped."""
    if user.is_admin:
        return None
    used = store.requests_since(user.key, time.time() - DAY_SECONDS)
    return max(0, config.WANT_DAILY_CAP - used)


def want(
    user: jellyfin.User,
    asin: str,
    title: str = "",
    recommendation_id: str | None = None,
    metadata: dict | None = None,
) -> tuple[str, str]:
    """Ask for one book. Returns (state, message). Raises Denied if refused.

    Ordered so that nothing is charged against the allowance until Listenarr
    has actually accepted the book, and so that a repeated tap is free: the
    ledger entry is keyed on (account, ASIN), and a second one neither restarts
    the clock nor spends another request.

    `metadata` is the add-shaped record when the caller already has it; a
    series listing does, and passing it spares Listenarr a lookup per book.
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
        raise AllowanceExhausted(
            f"That is {config.WANT_DAILY_CAP} books today. "
            "The allowance frees up again as the day rolls on.")

    log.info("want user=%s asin=%s title=%r remaining=%s",
             user.key, asin, title, "uncapped" if remaining is None else remaining)
    # Only named when there is one: the existing callers and their test
    # doubles know `add` by its two-argument shape.
    result = (listenarr.add(asin, metadata=metadata) if metadata is not None
              else listenarr.add(asin))
    if not result.ok:
        log.warning("want refused user=%s asin=%s reason=%s", user.key, asin, result.message)
        raise Denied(result.message)

    # What Listenarr resolved the ASIN to, preferred over what the caller typed:
    # it is the spelling the tagger will use when the book lands, and the
    # arrival check has to recognise it.
    store.record_request(user.key, asin, result.title or title, result.authors)
    store.record_feedback(user.key, asin, "want", recommendation_id)
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


def cancel(user: jellyfin.User, asin: str) -> tuple[bool, str]:
    """Take one book off this account's list and call its acquisition off.

    Returns (removed, message). Removing does NOT dismiss: a book abandoned
    because it has been three days coming is not a book somebody has stopped
    wanting, and the shelf is free to offer it again once Listenarr no longer
    holds it.

    Two things make this more than a delete:

    * **Listenarr's row belongs to the household, not to one account.** A book
      two people asked for is one acquisition, so it is only called off when
      nobody else is still waiting on it. Files and folder are left alone
      either way -- this cancels a search, it does not delete a book.
    * **The ledger row goes even when Listenarr will not answer.** The row is
      the thing on screen that was asked to go, and refusing would leave it
      there for as long as Listenarr stayed down, with no way to clear it. The
      message says which of the two happened rather than claiming both.
    """
    if _request_row(user.key, asin) is None:
        log.info("cancel user=%s asin=%s no-such-request", user.key, asin)
        return False, "That book is not on your list."

    others = store.outstanding_request_users(asin) - {user.key}
    if others:
        store.forget_request(user.key, asin)
        store.record_feedback(user.key, asin, "cancel")
        log.info("cancel user=%s asin=%s kept in Listenarr for %s",
                 user.key, asin, sorted(others))
        return True, ("Taken off your list. Somebody else is waiting on it, "
                      "so it is still being looked for.")

    called_off = _stop_acquiring(asin)
    store.forget_request(user.key, asin)
    store.record_feedback(user.key, asin, "cancel")
    log.info("cancel user=%s asin=%s listenarr_deleted=%s",
             user.key, asin, called_off)
    if called_off:
        return True, "Taken off your list, and the search was called off."
    return True, ("Taken off your list. Listenarr could not be told to stop, "
                  "so the book may still turn up.")


def _stop_acquiring(asin: str) -> bool:
    """Drop Listenarr's row for one book, keeping anything already on disk."""
    row = listenarr.find_by_asin(asin)
    if row is None:
        # Nothing to call off: the sweep never got a row for it, or somebody
        # has already removed one. Either way the acquisition is not running.
        return True
    audiobook_id = listenarr._audiobook_id(row)
    if audiobook_id is None:
        log.warning("cancel asin=%s: Listenarr row carries no id, cannot delete", asin)
        return False
    return listenarr.delete(audiobook_id)


def dismiss(
    user: jellyfin.User, asin: str, recommendation_id: str | None = None
) -> None:
    """Hide this book from this account for the configured cooling-off period."""
    log.info("dismiss user=%s asin=%s", user.key, asin)
    store.dismiss(user.key, asin)
    store.record_feedback(user.key, asin, "dismiss", recommendation_id)


def restore(
    user: jellyfin.User, asin: str, recommendation_id: str | None = None
) -> bool:
    """Undo a dismissal and make the book eligible again."""
    restored = store.undismiss(user.key, asin)
    if restored:
        store.record_feedback(user.key, asin, "restore", recommendation_id)
    log.info("restore user=%s asin=%s restored=%s", user.key, asin, restored)
    return restored


def states(user_key: str, owned: tuple[set, dict]) -> list[dict]:
    """This account's requests, each with its current state.

    `owned` is the library index -- ASINs, and normalised titles to author sets
    -- which the engine builds on every run anyway. There is still no status to
    fetch from Listenarr and nothing to poll: a book has either reached the
    library or it has not.
    """
    asins, by_title = owned
    rows = store.requests_for(user_key)
    arrived = {r["asin"] for r in rows
               if r["fulfilled_at"] is None and _arrived(r, asins, by_title)}
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


def _arrived(row: dict, asins: set, by_title: dict) -> bool:
    """Whether the book behind one request is now in the library.

    NOT an ASIN test, though it was one until 2026-08-28 and every request made
    through the app was stuck at "on its way" because of it. The ASIN asked for
    belongs to whichever marketplace the book was found in, and the tagger
    writes the one the other store issued for the same edition: "Splinter Angel:
    Book 1" was asked for as B0FMS8SNXH and sits in the library as B0FMS7YS1C.
    The two never meet, so the request could never be fulfilled.

    Close to `engine._already_owned` and deliberately not that call. That one
    asks "is this suggestion already on the shelf" of forty candidates against
    the whole library, where a bare title match over-suppresses and an author
    must agree. Here the title is the one that was asked for by name, so a title
    match is the evidence and an author is a guard on it -- applied when both
    sides carry one, which a row written before authors were kept does not.
    """
    if row["asin"] in asins:
        return True
    wanted = {engine._norm_author(a) for a in row.get("authors") or []}
    for key in _requested_title_keys(row.get("title") or ""):
        if key not in by_title:
            continue
        owners = by_title[key]
        if not owners or not wanted or (wanted & owners):
            return True
    return False


_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")
# A tail that is only a volume label is not a title: "Splinter Angel: Book 1"
# must not be found under "book 1".
_VOLUME_ONLY = re.compile(r"^(book|bk|vol|volume|part|no|number)\s+\S+$")


def _requested_title_keys(title: str) -> set[str]:
    """Forms a requested title may take on the shelf.

    Wider than `engine._title_keys`, on purpose. That one keeps the head of a
    colon split and nothing else, which is right for the owned check it serves
    -- a suggestion tested against three thousand titles must not over-match --
    and too narrow here, where the title was asked for by name and an author
    still has to agree. Two live shapes it could not see, both stuck at "still
    looking" on 2026-09-04 with the book already playing from the library:

    - a trailing qualifier in parentheses: "The House of Hades (Heroes of
      Olympus Book 4)" is filed as "The House of Hades";
    - a series before the colon: "The Heroes of Olympus: The Demigod Diaries"
      is filed as "The Demigod Diaries".
    """
    keys = set(engine._title_keys(title))
    bare = _TRAILING_PARENTHETICAL.sub("", title).strip()
    if bare and bare != title:
        keys |= engine._title_keys(bare)
    for text in {title, bare}:
        for sep in (":", " - ", " \u2014 "):
            if sep not in text:
                continue
            tail = engine._norm(text.split(sep, 1)[1])
            # Two words minimum, as `_title_keys` asks of a head, or short
            # titles collide across unrelated books.
            if tail and len(tail.split()) >= 2 and not _VOLUME_ONLY.match(tail):
                keys.add(tail)
    return keys


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
