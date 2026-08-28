"""Building and caching one user's shelves.

Separated from the request handlers because two surfaces read shelves -- the
HTML pages and the JSON API -- and they must share one cache, one lock per
user, and one answer to who owes the playlist a write.
"""
import time
from threading import Lock, Thread

from . import engine, jellyfin, logs, store

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
    index = engine._owned_index(jellyfin.books(user.id))
    _publish_owned(user.key, index)
    return index


def _publish_owned(user_key: str, index: tuple[set, dict]) -> None:
    """Make an index somebody has just built available to the next reader.

    A shelf build lists the library and derives this on the way past. Without
    this the next caller to want it -- a search, or the arrival check on this
    account's requests -- lists all 3,352 books again to derive the same thing,
    in front of whoever opened the screen.
    """
    with _cache_guard:
        _owned_cache[user_key] = (time.monotonic(), index)


def _lock_for(user_key: str) -> Lock:
    with _cache_guard:
        return _cache_locks.setdefault(user_key, Lock())


def _fresh_entry(user_key: str) -> tuple[dict, bool] | None:
    with _cache_guard:
        entry = _cache.get(user_key)
    if entry and time.monotonic() - entry[0] <= CACHE_TTL_SECONDS:
        return entry[1], entry[2]
    return None


# In flight, so a slow rebuild started by one request is not started again by
# the four that arrive while it runs.
_refreshing: set[str] = set()


def _stale_entry(user_key: str) -> dict | None:
    """The last shelf for this account however old, memory first then disk."""
    with _cache_guard:
        entry = _cache.get(user_key)
    if entry:
        return entry[1]
    try:
        stored = store.get_shelf(user_key)
    except Exception as exc:  # noqa: BLE001 - a cache that cannot be read is a
        # cache miss, not a failed request. Computing is slow, never wrong.
        log.warning("could not read the persisted shelf user=%s: %s", user_key, exc)
        return None
    if stored is None:
        return None
    data, computed_at = stored
    with _cache_guard:
        # Seeded as already stale: `time.monotonic()` and a wall clock cannot be
        # compared, so an age measured across a restart is not knowable. Treating
        # it as due for a refresh is the honest reading, and the refresh happens
        # behind the answer rather than in front of it.
        _cache[user_key] = (float("-inf"), data, False)
    log.info("shelves restored from disk user=%s age=%.0fs",
             user_key, max(0.0, time.time() - computed_at))
    return data


def _refresh_behind(user: jellyfin.User, update_playlist: bool) -> None:
    """Recompute out of band, so the stale answer served just now goes stale less."""
    with _cache_guard:
        if user.key in _refreshing:
            return
        _refreshing.add(user.key)

    def run() -> None:
        try:
            result(user, force=True, update_playlist=update_playlist)
        except Exception as exc:  # noqa: BLE001 - a background refresh must never
            # take the process down, and the stale answer is still being served.
            log.warning("background shelf refresh failed user=%s: %s", user.key, exc)
        finally:
            with _cache_guard:
                _refreshing.discard(user.key)

    Thread(target=run, name=f"shelf-refresh-{user.key}", daemon=True).start()


def result(user: jellyfin.User, force: bool = False,
            update_playlist: bool = True) -> dict:
    """This user's shelves, computing them only when the cache cannot answer.

    `update_playlist=False` is the API's read: it must not have side effects on
    a GET. A cached entry computed that way still owes the playlist its write,
    so a later web request pays it from the cached ids rather than recomputing.

    A stale entry is served immediately and refreshed behind the answer. The
    rebuild costs twelve seconds -- nine of them one Jellyfin listing of 3,352
    books, which is that slow because it asks for `People` and cannot stop:
    414 books here carry an Author person and no AlbumArtist. Waiting for that
    in front of the screen is the thing being fixed; an hour-old shelf is not
    worth a twelve-second wait, and this one only ever happens once per account.
    """
    if not force and (cached := _fresh_entry(user.key)) is not None:
        data, written = cached
        log.debug("shelves cache hit user=%s playlist_written=%s", user.key, written)
        if update_playlist and not written:
            write_playlist(user, data)
        return data
    if not force and (stale := _stale_entry(user.key)) is not None:
        log.info("shelves serving a stale answer while it rebuilds user=%s", user.key)
        _refresh_behind(user, update_playlist)
        if update_playlist:
            write_playlist(user, stale)
        return stale
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
        # Popped before the shelf is cached or persisted: it is larger than the
        # shelf itself, and its sets do not survive a JSON round trip.
        _publish_owned(user.key, data.pop("owned_index"))
        with _cache_guard:
            _cache[user.key] = (time.monotonic(), data, update_playlist)
        # Written after the memory cache, so a failure to persist costs the next
        # restart and nothing else.
        try:
            store.put_shelf(user.key, data)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not persist shelf user=%s: %s", user.key, exc)
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
    """Forget a shelf in memory AND on disk.

    Both, or the stale-answer path would read back from disk the very thing
    that was just invalidated: a dismissed book would reappear on the next
    load and stay until a background rebuild happened to finish.
    """
    with _cache_guard:
        if user_key is None:
            _cache.clear()
        else:
            _cache.pop(user_key, None)
    try:
        store.forget_shelf(user_key)
    except Exception as exc:  # noqa: BLE001 - the memory cache is already clear
        log.warning("could not forget the persisted shelf user=%s: %s", user_key, exc)


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
    # And on disk, for the same reason: a restart in between would otherwise
    # offer the book again to everyone it was just withdrawn from.
    for key in dropped:
        with _cache_guard:
            entry = _cache.get(key)
        if entry:
            try:
                store.put_shelf(key, entry[1])
            except Exception as exc:  # noqa: BLE001
                log.warning("could not persist the trimmed shelf user=%s: %s", key, exc)
    log.info("asin=%s removed from %d cached shelves %s", asin, len(dropped), dropped)
