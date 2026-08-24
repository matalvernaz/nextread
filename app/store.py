"""SQLite cache. Holds only a derived taste model and a recommendations cache.

Deliberately NOT a catalogue: no shelves, no reading state, no metadata of
record. Jellyfin owns all of that. Delete this file and you lose a cache.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

# Bumped whenever the shape of a cached sims payload changes. A stale entry is
# worse than a miss: it looks fresh and silently scores zero on fields that were
# not being kept when it was written.
SIMS_SCHEMA_VERSION = 2

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
CREATE TABLE IF NOT EXISTS doc_vectors (
    item_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    built_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submitted (
    asin        TEXT PRIMARY KEY,
    title       TEXT,
    submitted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dismissed (
    asin        TEXT PRIMARY KEY,
    dismissed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    seeds       INTEGER,
    owned       INTEGER,
    unowned     INTEGER,
    note        TEXT
);
"""


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
    neighbour set per similarity axis.
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
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('sims_schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SIMS_SCHEMA_VERSION),))
        conn.executescript(SCHEMA)


def _sims_key(asin: str, axis: str) -> str:
    """Cache key. The axis is part of it: one ASIN has a different neighbour set
    per `similarity_type`, and keying on the ASIN alone collides across axes."""
    return f"{asin}:{axis}"


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


def mark_submitted(asin: str, title: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO submitted(asin,title,submitted_at) VALUES(?,?,?)",
            (asin, title, time.time()),
        )


def dismiss(asin: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dismissed(asin,dismissed_at) VALUES(?,?)",
            (asin, time.time()),
        )


def suppressed_asins() -> set:
    """ASINs we should never show again: already handed to Listenarr, or dismissed."""
    with db() as conn:
        rows = conn.execute(
            "SELECT asin FROM submitted UNION SELECT asin FROM dismissed"
        ).fetchall()
    return {r["asin"] for r in rows}


def start_run() -> int:
    with db() as conn:
        cur = conn.execute("INSERT INTO runs(started_at) VALUES(?)", (time.time(),))
        return cur.lastrowid


def finish_run(run_id: int, seeds: int, owned: int, unowned: int, note: str = "") -> None:
    with db() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, seeds=?, owned=?, unowned=?, note=? WHERE id=?",
            (time.time(), seeds, owned, unowned, note, run_id),
        )


def last_run():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
