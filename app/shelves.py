"""Building and caching one user's shelves.

Separated from the request handlers because two surfaces read shelves -- the
HTML pages and the JSON API -- and they must share one cache, one lock per
user, and one answer to who owes the playlist a write.
"""
import time
from threading import Lock

from . import engine, jellyfin, logs

log = logs.get("shelves")

# Recomputing needs Jellyfin plus (on a cold SQLite cache) Audible. Each user has
# an independent entry and lock: one slow first load must not leak or overwrite
# another user's result, and simultaneous loads for one user should compute once.
_cache_locks: dict[str, Lock] = {}
_cache_guard = Lock()
CACHE_TTL_SECONDS = 3600


# Each entry is (computed_at, data, playlist_written). The flag exists because
# the JSON API reads shelves without writing a playlist: without it, one API
# read would seed a cache entry that every later web request then served, and
# the playlist would quietly stop being updated at all.
_cache: dict[str, tuple[float, dict, bool]] = {}


# Search must not pay for a shelf. A cold shelf build is 14.6 seconds over this
# library and calls Audible; this is one Jellyfin listing, and it answers the
# only question search asks of the library -- "do we already have this".
OWNED_TTL_SECONDS = 900
_owned_cache: dict[str, tuple[float, tuple[set, dict]]] = {}


def owned_index(user: jellyfin.User) -> tuple[set, dict]:
    """ASINs owned, and normalised-title -> author-set, for one account.

    Cached separately from the shelves, and briefly: a book bought since the
    last search should stop being offered without waiting an hour for the
    shelf's own entry to age out.
    """
    with _cache_guard:
        entry = _owned_cache.get(user.key)
        if entry and time.monotonic() - entry[0] <= OWNED_TTL_SECONDS:
            return entry[1]
    index = engine._owned_index(jellyfin.books(jellyfin.user_id(user.name)))
    with _cache_guard:
        _owned_cache[user.key] = (time.monotonic(), index)
    return index


def _lock_for(user_key: str) -> Lock:
    with _cache_guard:
        return _cache_locks.setdefault(user_key, Lock())


def _fresh_entry(user_key: str) -> tuple[dict, bool] | None:
    with _cache_guard:
        entry = _cache.get(user_key)
    if entry and time.monotonic() - entry[0] <= CACHE_TTL_SECONDS:
        return entry[1], entry[2]
    return None


def result(user: jellyfin.User, force: bool = False,
            update_playlist: bool = True) -> dict:
    """This user's shelves, computing them only when the cache cannot answer.

    `update_playlist=False` is the API's read: it must not have side effects on
    a GET. A cached entry computed that way still owes the playlist its write,
    so a later web request pays it from the cached ids rather than recomputing.
    """
    if not force and (cached := _fresh_entry(user.key)) is not None:
        data, written = cached
        log.debug("shelves cache hit user=%s playlist_written=%s", user.key, written)
        if update_playlist and not written:
            write_playlist(user, data)
        return data
    with _lock_for(user.key):
        if not force and (cached := _fresh_entry(user.key)) is not None:
            data, written = cached
            if update_playlist and not written:
                write_playlist(user, data)
            return data
        log.info("shelves computing user=%s force=%s update_playlist=%s",
                 user.key, force, update_playlist)
        started = time.monotonic()
        data = engine.run(user, update_playlist=update_playlist)
        with _cache_guard:
            _cache[user.key] = (time.monotonic(), data, update_playlist)
        log.info("shelves computed user=%s own=%d unowned=%d in %.1fs",
                 user.key, len(data.get("own") or []),
                 len(data.get("discover") or []), time.monotonic() - started)
        return data


def write_playlist(user: jellyfin.User, data: dict) -> None:
    """Settle a cached result's outstanding playlist write, without recomputing."""
    if not data.get("own"):
        return
    log.info("settling deferred playlist write user=%s items=%d",
             user.key, len(data["own"]))
    jellyfin.set_playlist(user.id, data["playlist_name"],
                          [r["id"] for r in data["own"]])
    with _cache_guard:
        entry = _cache.get(user.key)
        if entry is not None:
            _cache[user.key] = (entry[0], entry[1], True)


def invalidate(user_key: str | None = None) -> None:
    with _cache_guard:
        if user_key is None:
            _cache.clear()
        else:
            _cache.pop(user_key, None)


def forget_asin(asin: str) -> None:
    """Drop one book from every cached unowned shelf.

    Listenarr is shared, so a book one person requests should stop being
    offered to everybody. This used to clear the whole cache, which was cheap
    when requests arrived from one browser and is not now that ten accounts can
    make one from a phone: every request would push every user through a cold
    Jellyfin and Audible recompute. Removing the row costs nothing and has the
    same visible effect.
    """
    dropped = []
    with _cache_guard:
        for key, (at, data, written) in list(_cache.items()):
            discover = data.get("discover") or []
            kept = [row for row in discover if row.get("asin") != asin]
            if len(kept) != len(discover):
                _cache[key] = (at, {**data, "discover": kept}, written)
                dropped.append(key)
    log.info("asin=%s removed from %d cached shelves %s", asin, len(dropped), dropped)
