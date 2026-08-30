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
SIMS_SCHEMA_VERSION = 4

# The same idea for cached Audible products, versioned apart from sims so a
# reshaped product does not throw away a similarity graph that costs one request
# per seed per axis to rebuild. v2: every row was fetched before
# `product_extended_attrs` was asked for, so none of them carries
# `publisher_summary` at all, and `PRODUCT_TTL_HOURS` would go on serving the
# teaser in its place for a month after the fix landed.
PRODUCTS_SCHEMA_VERSION = 3

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
    authors      TEXT,
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

_RECOMMENDATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendation_items (
    recommendation_id TEXT PRIMARY KEY,
    run_id            INTEGER NOT NULL,
    user_key          TEXT NOT NULL,
    surface           TEXT NOT NULL,
    item_key          TEXT NOT NULL,
    rank              INTEGER NOT NULL,
    score             REAL NOT NULL,
    source            TEXT NOT NULL,
    reasons           TEXT NOT NULL,
    ranker_version    TEXT NOT NULL,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS recommendation_items_user_run
    ON recommendation_items(user_key, run_id);
CREATE INDEX IF NOT EXISTS recommendation_items_created
    ON recommendation_items(created_at);

CREATE TABLE IF NOT EXISTS feedback_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key          TEXT NOT NULL,
    asin              TEXT NOT NULL,
    action            TEXT NOT NULL,
    recommendation_id TEXT,
    occurred_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_events_user_time
    ON feedback_events(user_key, occurred_at);
CREATE INDEX IF NOT EXISTS feedback_events_time
    ON feedback_events(occurred_at);
CREATE INDEX IF NOT EXISTS feedback_events_recommendation
    ON feedback_events(recommendation_id);
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
""" + _SUBMITTED_SCHEMA + _REQUESTS_SCHEMA + _DISMISSED_SCHEMA + _RUNS_SCHEMA \
    + _RECOMMENDATIONS_SCHEMA


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
    this one. Cached products carry their own version for the same reason and
    are dropped separately, so reshaping one cache does not cost the other.
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
        row = conn.execute(
            "SELECT value FROM meta WHERE key='products_schema_version'").fetchone()
        if row is None or int(row["value"]) != PRODUCTS_SCHEMA_VERSION:
            conn.execute("DROP TABLE IF EXISTS products")
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('products_schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(PRODUCTS_SCHEMA_VERSION),))
        conn.executescript(SCHEMA)
        _migrate_user_scope(conn)
        _migrate_requests(conn)
        _migrate_request_authors(conn)


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


def _migrate_request_authors(conn: sqlite3.Connection) -> None:
    """Add the author column arrival now agrees on, to a ledger written without it.

    Rows from before it stay NULL, and an authorless row is read as "the title
    decides" -- which is how the requests that were stuck under the ASIN-only
    check resolve on the first read after this lands.
    """
    if "authors" in _columns(conn, "requests"):
        return
    conn.execute("ALTER TABLE requests ADD COLUMN authors TEXT")


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
    """A recent resolution, empty string for a known miss, or None when stale."""
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


def record_request(user_key: str, asin: str, title: str,
                   authors: list | tuple = ()) -> bool:
    """Log that this account asked for a book. True when it is a new request.

    Idempotent on purpose: a second tap on the same book must not restart the
    "still looking" clock or spend another day's allowance.

    The title and authors are kept because the ASIN is not enough to recognise
    the book when it lands: it arrives tagged with whichever ASIN the other
    marketplace issued for the same edition.
    """
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO requests(user_key,asin,title,authors,requested_at) "
            "VALUES(?,?,?,?,?)",
            (user_key, asin, title, json.dumps(list(authors)), time.time()),
        )
    return cur.rowcount > 0


def requests_for(user_key: str) -> list[dict]:
    """Every book this account has asked for, newest first."""
    with db() as conn:
        rows = conn.execute(
            "SELECT asin,title,authors,requested_at,fulfilled_at FROM requests "
            "WHERE user_key=? ORDER BY requested_at DESC",
            (user_key,),
        ).fetchall()
    return [{**dict(r), "authors": _authors_of(r["authors"])} for r in rows]


def _authors_of(payload) -> list[str]:
    """The stored author list. Empty for a row written before the column."""
    if not payload:
        return []
    try:
        names = json.loads(payload)
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str) and n] if isinstance(names, list) else []


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


def forget_request(user_key: str, asin: str) -> bool:
    """Erase one request. True when there was one to erase.

    A delete rather than another timestamp column: an abandoned request is not
    a state the shelf has anything to say about, and leaving the row would keep
    it counting against the day's allowance for a book nobody is getting.
    """
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM requests WHERE user_key=? AND asin=?", (user_key, asin))
    return cur.rowcount > 0


def outstanding_request_users(asin: str) -> set:
    """Every account still waiting on this book.

    Listenarr holds one row per book for the whole household, so cancelling has
    to know whether anybody else is waiting on it before deleting that row.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT user_key FROM requests WHERE asin=? AND fulfilled_at IS NULL",
            (asin,),
        ).fetchall()
    return {r["user_key"] for r in rows}


def dismiss(user_key: str, asin: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dismissed(user_key,asin,dismissed_at) VALUES(?,?,?)",
            (user_key, asin, time.time()),
        )


def undismiss(user_key: str, asin: str) -> bool:
    """Remove this account's active dismissal. True when one existed."""
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM dismissed WHERE user_key=? AND asin=?", (user_key, asin))
    return cur.rowcount > 0


def suppressed_asins(user_key: str) -> set:
    """Global acquisitions plus books dismissed by this user.

    Requests are suppressed for everyone, not just the person who made them:
    Listenarr is shared, so a book one listener asks for is acquired once and
    should not still be offered to the other nine as if it were unowned.
    """
    dismissal_cutoff = time.time() - config.DISMISS_TTL_DAYS * 86400
    with db() as conn:
        rows = conn.execute(
            "SELECT asin FROM requests UNION SELECT asin FROM dismissed "
            "WHERE user_key=? AND dismissed_at>?",
            (user_key, dismissal_cutoff),
        ).fetchall()
    return {r["asin"] for r in rows}


def record_recommendations(
    run_id: int,
    user_key: str,
    surface: str,
    rows: list[dict],
    ranker_version: str,
) -> None:
    """Persist the ranked rows needed to attribute later feedback."""
    now = time.time()
    values = []
    for rank, row in enumerate(rows, start=1):
        recommendation_id = row.get("recommendation_id")
        item_key = row.get("id") if surface == "owned" else row.get("asin")
        if not recommendation_id or not item_key:
            continue
        values.append((
            recommendation_id,
            run_id,
            user_key,
            surface,
            item_key,
            rank,
            float(row.get("score") or 0),
            row.get("source") or "unknown",
            json.dumps(row.get("why") or []),
            ranker_version,
            now,
        ))
    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO recommendation_items("
            "recommendation_id,run_id,user_key,surface,item_key,rank,score,source,"
            "reasons,ranker_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )


def prune_attribution() -> None:
    """Bound attribution history without orphaning surviving feedback."""
    cutoff = time.time() - config.ATTRIBUTION_RETENTION_DAYS * 86400
    with db() as conn:
        conn.execute("DELETE FROM feedback_events WHERE occurred_at<?", (cutoff,))
        conn.execute(
            "DELETE FROM recommendation_items AS recommendation "
            "WHERE created_at<? AND NOT EXISTS ("
            "SELECT 1 FROM feedback_events AS feedback "
            "WHERE feedback.recommendation_id="
            "recommendation.recommendation_id)",
            (cutoff,),
        )


def record_feedback(
    user_key: str,
    asin: str,
    action: str,
    recommendation_id: str | None = None,
) -> None:
    """Record an outcome, accepting attribution only for this user's ASIN."""
    validated = None
    with db() as conn:
        if recommendation_id:
            row = conn.execute(
                "SELECT 1 FROM recommendation_items "
                "WHERE recommendation_id=? AND user_key=? AND item_key=?",
                (recommendation_id, user_key, asin),
            ).fetchone()
            if row:
                validated = recommendation_id
        conn.execute(
            "INSERT INTO feedback_events("
            "user_key,asin,action,recommendation_id,occurred_at) VALUES(?,?,?,?,?)",
            (user_key, asin, action, validated, time.time()),
        )


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
