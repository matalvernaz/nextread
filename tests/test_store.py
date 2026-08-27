"""SQLite migration and user-state isolation checks."""
import os
import sqlite3
import sys
import tempfile
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

    run_id = store.start_run("alex")
    store.finish_run(run_id, 1, 2, 3, "alex-run")
    assert store.last_run("alex")["note"] == "alex-run"
    assert store.last_run("matt")["note"] == "legacy"

print("store migration and isolation checks passed")
