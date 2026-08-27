"""Application logging.

Worth its own module because of what this app does: a request for a book is
asynchronous and crosses three services, and no single screen ever shows the
whole of one. Somebody taps "want", Listenarr searches minutes later, a
download finishes later still, an hourly job tags the file, and Jellyfin
notices it after that. When a book does not arrive, the log is the only place
that says which of those steps happened.

Two standing rules:

* Never log an access token. Log `fingerprint()` of one instead -- enough to
  tell two callers apart in a log, useless to anybody who reads it.
* Log the soft failures loudest. The paths that return an empty list when
  Listenarr is unreachable are the ones that degrade invisibly, and an
  invisible degradation is the thing that costs a day to find.
"""
import hashlib
import logging

from . import config

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"


def configure() -> None:
    """Attach a handler if the host process has not already provided one.

    Under uvicorn the root logger is already configured, so this only sets the
    level; run directly, it also gives the app somewhere to write.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
    logging.getLogger("nextread").setLevel(config.LOG_LEVEL)


def get(name: str) -> logging.Logger:
    """A logger for one module, under the shared `nextread` parent."""
    return logging.getLogger(f"nextread.{name}")


def fingerprint(token: str) -> str:
    """A short, stable, non-reversible stand-in for a token in a log line."""
    return hashlib.sha256(token.encode()).hexdigest()[:8]
