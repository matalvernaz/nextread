"""SQLite migration and user-state isolation checks."""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, store

with tempfile.TemporaryDirectory() as tmp:
    config.DB_PATH = str(Path(tmp) / "nextread.db")
    config.JELLYFIN_USER = "Matt"

    db = sqlite3.connect(config.DB_PATH)
    db.executescript("""
        CREATE TABLE submitted (
            asin TEXT PRIMARY KEY, title TEXT, submitted_at REAL NOT NULL
        );
        CREATE TABLE dismissed (
            asin TEXT PRIMARY KEY, dismissed_at REAL NOT NULL
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at REAL NOT NULL,
            finished_at REAL,
            seeds INTEGER,
            owned INTEGER,
            unowned INTEGER,
            note TEXT
        );
        INSERT INTO submitted VALUES ('GLOBAL', 'Already wanted', 1);
        INSERT INTO dismissed VALUES ('MATT-HIDDEN', 1);
        INSERT INTO runs VALUES (1, 1, 2, 3, 4, 5, 'legacy');
    """)
    db.commit()
    db.close()

    store.init()

    # The migrated dismissal is made current so this test can distinguish user
    # scoping from the separate expiry behaviour below.
    with store.db() as conn:
        conn.execute("UPDATE dismissed SET dismissed_at=?", (time.time(),))

    db = sqlite3.connect(config.DB_PATH)
    assert db.execute("SELECT user_key FROM submitted").fetchone()[0] == "matt"
    assert db.execute("SELECT user_key FROM dismissed").fetchone()[0] == "matt"
    assert db.execute("SELECT user_key FROM runs").fetchone()[0] == "matt"
    db.close()

    assert store.suppressed_asins("matt") == {"GLOBAL", "MATT-HIDDEN"}
    assert store.suppressed_asins("alex") == {"GLOBAL"}

    store.dismiss("alex", "ALEX-HIDDEN")
    assert store.suppressed_asins("alex") == {"GLOBAL", "ALEX-HIDDEN"}
    assert "ALEX-HIDDEN" not in store.suppressed_asins("matt")

    with store.db() as conn:
        conn.execute(
            "UPDATE dismissed SET dismissed_at=? WHERE user_key=? AND asin=?",
            (time.time() - (config.DISMISS_TTL_DAYS + 1) * 86400,
             "alex", "ALEX-HIDDEN"),
        )
    assert "ALEX-HIDDEN" not in store.suppressed_asins("alex"), \
        "a dismissal must expire rather than suppress a book forever"

    store.dismiss("alex", "UNDO-ME")
    assert store.undismiss("alex", "UNDO-ME")
    assert "UNDO-ME" not in store.suppressed_asins("alex")

    run_id = store.start_run("alex")
    store.finish_run(run_id, 1, 2, 3, "alex-run")
    assert store.last_run("alex")["note"] == "alex-run"
    assert store.last_run("matt")["note"] == "legacy"

    recommendation = {
        "asin": "ATTRIBUTED",
        "score": 42,
        "source": "audible_sims",
        "why": ["because"],
        "recommendation_id": f"{run_id}:discover:1:ATTRIBUTED",
    }
    store.record_recommendations(
        run_id, "alex", "discover", [recommendation], "2")
    store.record_feedback(
        "alex", "ATTRIBUTED", "want", recommendation["recommendation_id"])
    store.record_feedback(
        "matt", "ATTRIBUTED", "dismiss", recommendation["recommendation_id"])
    with store.db() as conn:
        events = conn.execute(
            "SELECT user_key,recommendation_id FROM feedback_events ORDER BY id"
        ).fetchall()
    assert events[-2]["recommendation_id"] == recommendation["recommendation_id"]
    assert events[-1]["recommendation_id"] is None, \
        "another account must not be able to forge attribution"

    old = time.time() - (config.ATTRIBUTION_RETENTION_DAYS + 1) * 86400
    with store.db() as conn:
        conn.execute(
            "UPDATE recommendation_items SET created_at=? "
            "WHERE recommendation_id=?",
            (old, recommendation["recommendation_id"]),
        )
    store.prune_attribution()
    with store.db() as conn:
        snapshot = conn.execute(
            "SELECT 1 FROM recommendation_items WHERE recommendation_id=?",
            (recommendation["recommendation_id"],),
        ).fetchone()
    assert snapshot is not None, \
        "a snapshot must survive while retained feedback still points to it"

    with store.db() as conn:
        conn.execute(
            "UPDATE feedback_events SET occurred_at=? WHERE recommendation_id=?",
            (old, recommendation["recommendation_id"]),
        )
    store.prune_attribution()
    with store.db() as conn:
        event = conn.execute(
            "SELECT 1 FROM feedback_events WHERE recommendation_id=?",
            (recommendation["recommendation_id"],),
        ).fetchone()
        snapshot = conn.execute(
            "SELECT 1 FROM recommendation_items WHERE recommendation_id=?",
            (recommendation["recommendation_id"],),
        ).fetchone()
    assert event is None and snapshot is None, \
        "expired feedback must release its old snapshot"

# The 2026-09-02 rekey: every user-scoped table moves off casefolded display
# names and onto Jellyfin account ids. Renaming an account used to empty that
# listener's shelf, requests and history; recreating a name inherited a
# stranger's.
with tempfile.TemporaryDirectory() as tmp:
    config.DB_PATH = str(Path(tmp) / "rekey.db")
    store.init()
    assert store.user_key_scheme() == "name", "an unmigrated database says so"
    store.record_request("renamed", "B0RENAME", "Theirs", ())
    store.record_request("gone-away", "B0ORPHAN", "Orphan", ())
    store.put_shelf("renamed", {"discover": []})

    moved = store.rekey_users({"renamed": "user-renamed"})
    assert moved >= 2, moved
    with store.db() as conn:
        keys = {r["user_key"] for r in conn.execute("SELECT user_key FROM requests")}
    assert "user-renamed" in keys and "renamed" not in keys, keys
    # Left alone rather than discarded: the account may be renamed back, and a
    # listener's own history is not worth losing to tidy a key.
    assert "gone-away" in keys, keys
    assert store.get_shelf("user-renamed") is not None
    assert store.user_key_scheme() == "id"
    assert store.rekey_users({"renamed": "user-renamed"}) == 0, "runs once"


print("store migration and isolation checks passed")
