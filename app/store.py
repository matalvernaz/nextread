"""SQLite cache. Holds only a derived taste model and a recommendations cache.

Deliberately NOT a catalogue: no shelves, no reading state, no metadata of
record. Jellyfin owns all of that. Delete this file and you lose a cache.
"""
import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sims (
    asin        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
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
    with db() as conn:
        conn.executescript(SCHEMA)


def get_sims(asin: str):
    """Return cached sims for an ASIN, or None if absent or stale."""
    cutoff = time.time() - config.SIMS_TTL_HOURS * 3600
    with db() as conn:
        row = conn.execute(
            "SELECT payload FROM sims WHERE asin=? AND fetched_at>?", (asin, cutoff)
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def put_sims(asin: str, payload) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO sims(asin,payload,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(asin) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (asin, json.dumps(payload), time.time()),
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
