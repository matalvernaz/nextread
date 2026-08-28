"""Where a test's database lives, and the guard that keeps it away from the real one.

These tests can only run where the dependencies are, which is inside the
`nextread` container -- and that container's environment sets `DB_PATH` to the
live database. Every file here used `os.environ.setdefault("DB_PATH", ...)`,
which defers to an environment that already has one, so the suite ran against
production and the `os.remove` at the end of a file deleted it. It happened on
2026-08-28 and cost the request ledger, the sims cache and the run history.

So: set, never defaulted, and nothing is deleted that is not demonstrably a
test database. Run the suite against the image with no `/data` mounted and the
question cannot arise at all -- see the README.
"""
import os
import tempfile

_PREFIX = "nextread-test-"


def use(name: str) -> str:
    """Point this test at its own database, whatever the environment says."""
    path = os.path.join(tempfile.gettempdir(), f"{_PREFIX}{name}.db")
    os.environ["DB_PATH"] = path
    return path


def discard(path: str) -> None:
    """Delete a test database, and refuse anything that is not one."""
    if not os.path.basename(path).startswith(_PREFIX):
        raise SystemExit(f"refusing to delete {path}: that is not a test database")
    if os.path.exists(path):
        os.remove(path)
