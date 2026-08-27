"""SQLite caches plus the small amount of user-scoped UI state.

Deliberately not a catalogue: Jellyfin remains authoritative for books, play
state, ratings, and playlists. Dismissals and request/run history live here.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

# Bumped whenever the shape of a cached sims payload changes. A stale entry is
# worse than a miss: it looks fresh and silently scores zero on fields that were
# not being kept when it was written.
SIMS_SCHEMA_VERSION = 3

_SUBMITTED_SCHEMA = """
CREATE TABLE IF NOT EXISTS submitted (
    asin         TEXT PRIMARY KEY,
    title        TEXT,
    user_key     TEXT NOT NULL,
    submitted_at REAL NOT NULL
);
"""

_REQUESTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    user_key     TEXT NOT NULL,
    asin         TEXT NOT NULL,
    title        TEXT,
    requested_at REAL NOT NULL,
    fulfilled_at REAL,
    PRIMARY KEY (user_key, asin)
);
"""

_DISMISSED_SCHEMA = """
CREATE TABLE IF NOT EXISTS dismissed (
    user_key    TEXT NOT NULL,
    asin        TEXT NOT NULL,
    dismissed_at REAL NOT NULL,
    PRIMARY KEY (user_key, asin)
);
"""

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key    TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    seeds       INTEGER,
    owned       INTEGER,
    unowned     INTEGER,
    note        TEXT
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sims (
    cache_key   TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audible_aliases (
    source_asin  TEXT PRIMARY KEY,
    audible_asin TEXT NOT NULL,
    resolved_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shelves (
    user_key    TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    computed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    asin        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_vectors (
    item_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    built_at    REAL NOT NULL
);
""" + _SUBMITTED_SCHEMA + _REQUESTS_SCHEMA + _DISMISSED_SCHEMA + _RUNS_SCHEMA


@contextmanager
def db():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    """Create the cache, dropping the sims table outright on a version change.

    `CREATE TABLE IF NOT EXISTS` will not reshape an existing table, so a version
    bump has to DROP: v1 keyed on `asin` alone, which collides once one ASIN has a
    neighbour set per similarity axis. v3 drops for a different reason -- the
    rows are correct for the marketplace they were fetched from and wrong for
    this one.
    """
    with db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute(
            "SELECT value FROM meta WHERE key='sims_schema_version'").fetchone()
        if row is None or int(row["value"]) != SIMS_SCHEMA_VERSION:
            # It is a cache; refetching costs one request per seed per axis.
            conn.execute("DROP TABLE IF EXISTS sims")
            conn.execute("DROP TABLE IF EXISTS doc_vectors")
            # v3: every one of these was filled from the US catalogue, where a
            # Canadian exclusive answers with an empty product. Kept and they
            # would go on standing in for the real thing for the whole 168-hour
            # TTL, so the region change would look like it had done nothing.
            conn.execute("DROP TABLE IF EXISTS audible_aliases")
            conn.execute("DROP TABLE IF EXISTS products")
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('sims_schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SIMS_SCHEMA_VERSION),))
        conn.executescript(SCHEMA)
        _migrate_user_scope(conn)
        _migrate_requests(conn)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_user_scope(conn: sqlite3.Connection) -> None:
    """Assign legacy single-user rows to the configured fallback user."""
    legacy_user = config.JELLYFIN_USER.casefold()
    migrations = (
        (
            "submitted", _SUBMITTED_SCHEMA,
            (
                "INSERT INTO submitted(asin,title,user_key,submitted_at) "
                "SELECT asin,title,?,submitted_at FROM submitted_single_user"
            ),
        ),
        (
            "dismissed", _DISMISSED_SCHEMA,
            (
                "INSERT INTO dismissed(user_key,asin,dismissed_at) "
                "SELECT ?,asin,dismissed_at FROM dismissed_single_user"
            ),
        ),
        (
            "runs", _RUNS_SCHEMA,
            (
                "INSERT INTO runs(id,user_key,started_at,finished_at,seeds,owned,unowned,note) "
                "SELECT id,?,started_at,finished_at,seeds,owned,unowned,note "
                "FROM runs_single_user"
            ),
        ),
    )
    for table, schema, copy_sql in migrations:
        if "user_key" in _columns(conn, table):
            continue
        old_table = f"{table}_single_user"
        conn.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
        conn.executescript(schema)
        conn.execute(copy_sql, (legacy_user,))
        conn.execute(f"DROP TABLE {old_table}")


def _migrate_requests(conn: sqlite3.Connection) -> None:
    """Seed the per-user request ledger from the older `submitted` table.

    `submitted` keys on the ASIN alone with a single attribution column, so it
    is last-writer-wins and cannot answer "how many did this account ask for
    today". `requests` replaces it. The old table is left in place, unread, as
    the rollback: nothing else in this app writes it any more.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key='requests_migrated'").fetchone()
    if row is not None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO requests(user_key,asin,title,requested_at) "
        "SELECT user_key,asin,title,submitted_at FROM submitted")
    conn.execute("INSERT INTO meta(key,value) VALUES('requests_migrated','1')")


def _sims_key(asin: str, axis: str) -> str:
    """Cache key. The axis is part of it: one ASIN has a different neighbour set
    per `similarity_type`, and keying on the ASIN alone collides across axes."""
    return f"{asin}:{axis}"


def get_shelf(user_key: str) -> tuple[dict, float] | None:
    """The last shelf computed for this account, and when, or None.

    Kept because the in-memory cache dies with the process and rebuilding costs
    twelve seconds, nine of which is one Jellyfin listing. A restart used to
    hand that bill to whoever opened the screen next.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT payload, computed_at FROM shelves WHERE user_key=?",
            (user_key,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["payload"])
    except ValueError:
        return None
    # Sets do not survive JSON, and this one is membership-tested per request.
    data["owned_asins"] = set(data.get("owned_asins") or [])
    return data, row["computed_at"]


def put_shelf(user_key: str, data: dict) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO shelves(user_key,payload,computed_at) VALUES(?,?,?) "
            "ON CONFLICT(user_key) DO UPDATE SET payload=excluded.payload, "
            "computed_at=excluded.computed_at",
            (user_key, json.dumps(data, default=list), time.time()))


def forget_shelf(user_key: str | None = None) -> None:
    """Drop the persisted shelf, so an invalidation is not undone from disk."""
    with db() as conn:
        if user_key is None:
            conn.execute("DELETE FROM shelves")
        else:
            conn.execute("DELETE FROM shelves WHERE user_key=?", (user_key,))


def get_product(asin: str):
    """One cached Audible product, or None if absent or stale.

    `_candidate_description` has always described this lookup as cached and it
    was not, which cost one live request per blurb. It matters more now that a
    summary can be opened on demand: without this, reading the same book's
    description twice asks Audible twice.
    """
    cutoff = time.time() - config.PRODUCT_TTL_HOURS * 3600
    with db() as conn:
        row = conn.execute(
            "SELECT payload FROM products WHERE asin=? AND fetched_at>?",
            (asin, cutoff),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def put_product(asin: str, payload) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO products(asin,payload,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(asin) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (asin, json.dumps(payload), time.time()))


def get_sims(asin: str, axis: str):
    """Return cached sims for an (ASIN, axis), or None if absent or stale."""
    cutoff = time.time() - config.SIMS_TTL_HOURS * 3600
    with db() as conn:
        row = conn.execute(
            "SELECT payload FROM sims WHERE cache_key=? AND fetched_at>?",
            (_sims_key(asin, axis), cutoff),
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def put_sims(asin: str, axis: str, payload) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO sims(cache_key,payload,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (_sims_key(asin, axis), json.dumps(payload), time.time()),
        )


def get_audible_alias(source_asin: str) -> str | None:
    """A recently resolved audiobook ASIN for a dead library identifier."""
    cutoff = time.time() - config.SIMS_TTL_HOURS * 3600
    with db() as conn:
        row = conn.execute(
            "SELECT audible_asin FROM audible_aliases "
            "WHERE source_asin=? AND resolved_at>?",
            (source_asin, cutoff),
        ).fetchone()
    return row["audible_asin"] if row else None


def put_audible_alias(source_asin: str, audible_asin: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO audible_aliases(source_asin,audible_asin,resolved_at) "
            "VALUES(?,?,?) ON CONFLICT(source_asin) DO UPDATE SET "
            "audible_asin=excluded.audible_asin,resolved_at=excluded.resolved_at",
            (source_asin, audible_asin, time.time()),
        )


def get_vectors(kind: str) -> dict:
    """Cached sparse TF-IDF vectors, keyed by Jellyfin item id."""
    with db() as conn:
        rows = conn.execute(
            "SELECT item_id, payload FROM doc_vectors WHERE kind=?", (kind,)
        ).fetchall()
    return {r["item_id"]: json.loads(r["payload"]) for r in rows}


def put_vectors(kind: str, vectors: dict) -> None:
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM doc_vectors WHERE kind=?", (kind,))
        conn.executemany(
            "INSERT INTO doc_vectors(item_id,kind,payload,built_at) VALUES(?,?,?,?)",
            [(k, kind, json.dumps(v), now) for k, v in vectors.items()],
        )


def record_request(user_key: str, asin: str, title: str) -> bool:
    """Log that this account asked for a book. True when it is a new request.

    Idempotent on purpose: a second tap on the same book must not restart the
    "still looking" clock or spend another day's allowance.
    """
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO requests(user_key,asin,title,requested_at) "
            "VALUES(?,?,?,?)",
            (user_key, asin, title, time.time()),
        )
    return cur.rowcount > 0


def requests_for(user_key: str) -> list[dict]:
    """Every book this account has asked for, newest first."""
    with db() as conn:
        rows = conn.execute(
            "SELECT asin,title,requested_at,fulfilled_at FROM requests "
            "WHERE user_key=? ORDER BY requested_at DESC",
            (user_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def requests_since(user_key: str, cutoff: float) -> int:
    """How many requests this account has made since `cutoff`. Drives the cap."""
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM requests WHERE user_key=? AND requested_at>?",
            (user_key, cutoff),
        ).fetchone()
    return int(row["n"])


def fulfil_requests(user_key: str, asins: set) -> None:
    """Stop the clock on requests whose book has since reached the library."""
    if not asins:
        return
    with db() as conn:
        conn.executemany(
            "UPDATE requests SET fulfilled_at=? "
            "WHERE user_key=? AND asin=? AND fulfilled_at IS NULL",
            [(time.time(), user_key, a) for a in asins],
        )


def dismiss(user_key: str, asin: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dismissed(user_key,asin,dismissed_at) VALUES(?,?,?)",
            (user_key, asin, time.time()),
        )


def suppressed_asins(user_key: str) -> set:
    """Global acquisitions plus books dismissed by this user.

    Requests are suppressed for everyone, not just the person who made them:
    Listenarr is shared, so a book one listener asks for is acquired once and
    should not still be offered to the other nine as if it were unowned.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT asin FROM requests UNION SELECT asin FROM dismissed WHERE user_key=?",
            (user_key,),
        ).fetchall()
    return {r["asin"] for r in rows}


def start_run(user_key: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO runs(user_key,started_at) VALUES(?,?)",
            (user_key, time.time()),
        )
        return cur.lastrowid


def finish_run(run_id: int, seeds: int, owned: int, unowned: int, note: str = "") -> None:
    with db() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, seeds=?, owned=?, unowned=?, note=? WHERE id=?",
            (time.time(), seeds, owned, unowned, note, run_id),
        )


def last_run(user_key: str):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE user_key=? AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (user_key,),
        ).fetchone()
