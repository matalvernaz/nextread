"""The same-origin route check.

The failure it exists for is the one with no symptom: a service standing up
happily on its own hostname while the proxy rule at the Jellyfin origin is
missing, so every client concludes the service is not installed and says
nothing at all.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

harness.use("selfcheck")

import httpx

from app import config, selfcheck

BASE = "https://jellyfin.example.com/nextread"
WANTED = f"{BASE}/api/v1/info"


def answering(handler):
    """Point the check at a fake origin for the length of one call."""
    real = httpx.get

    def fake(url, **kwargs):
        assert url == WANTED, url
        return handler(httpx.Request("GET", url))

    httpx.get = fake
    try:
        return selfcheck.check(BASE)
    finally:
        httpx.get = real


ok = answering(lambda req: httpx.Response(
    200, json={"service": "nextread", "protocol": 1}, request=req))
assert ok is None, ok

missing = answering(lambda req: httpx.Response(404, text="Not Found", request=req))
assert missing is not None
assert "404" in missing and "Jellyfin origin" in missing, missing

# A 200 from somebody else is what a catch-all router or a captive portal
# looks like, and it must not read as a working route.
imposter = answering(lambda req: httpx.Response(
    200, json={"service": "jellyfin"}, request=req))
assert imposter is not None and "not this service" in imposter, imposter

challenged = answering(lambda req: httpx.Response(401, text="", request=req))
assert challenged is not None and "401" in challenged, challenged


def refuse(req):
    raise httpx.ConnectError("nothing listening", request=req)


unreachable = answering(refuse)
assert unreachable is not None and "could not be reached" in unreachable, unreachable

# Unset is the ordinary case for an install serving only the browser pages,
# and it must cost that install nothing at all.
config.PUBLIC_URL = ""
selfcheck.watch()
assert not any(t.name == "same-origin-check" for t in __import__("threading").enumerate())

print("selfcheck checks passed")
