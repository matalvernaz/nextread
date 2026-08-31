"""Whether the route clients actually look for is really there.

Standing this service up on its own hostname is only half a deployment. The
other half is a proxy rule at the Jellyfin origin, and leaving that out has no
symptom worth the name: a client probes the Jellyfin origin, gets Jellyfin's
own 404, concludes the service is not installed and says nothing, because most
servers do not run one. The feature is simply missing, with nothing in any log.

Warn-only, and deliberately not part of the health check. A proxy drops an
unhealthy container from its load balancer, so failing health over a missing
same-origin route would take down the address that still works and turn half a
misconfiguration into all of one.
"""
import time
from threading import Thread

import httpx

from . import config, logs

log = logs.get("selfcheck")

# Traefik and its like take twenty-five to thirty seconds to register a
# recreated container, so a route probed sooner reads as missing when it is
# only late.
FIRST_DELAY_SECONDS = 60
INTERVAL_SECONDS = 3600
TIMEOUT_SECONDS = 10.0


def check(base_url: str) -> str | None:
    """None when the route answers, otherwise a sentence naming what is wrong."""
    url = f"{base_url.rstrip('/')}/api/v1/info"
    try:
        resp = httpx.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=True)
    except httpx.HTTPError as exc:
        return f"{url} could not be reached ({exc.__class__.__name__})"
    if resp.status_code == 404:
        return (f"{url} answers 404. The proxy rule serving this service at the "
                "Jellyfin origin is missing, and no client will ever find it.")
    if resp.status_code != 200:
        return f"{url} answered {resp.status_code} rather than 200."
    try:
        named = resp.json().get("service")
    except ValueError:
        return f"{url} answered 200 but not with JSON."
    if named != config.SERVICE_NAME:
        return f"{url} answered 200 but belongs to {named!r}, not this service."
    return None


def _loop(base_url: str) -> None:
    time.sleep(FIRST_DELAY_SECONDS)
    while True:
        problem = check(base_url)
        if problem:
            log.error("same-origin route: %s", problem)
        else:
            log.info("same-origin route: %s answers", base_url)
        time.sleep(INTERVAL_SECONDS)


def watch() -> None:
    """Start the check, if this deployment said where to look.

    Unset is the ordinary case for somebody running only the browser pages, and
    it costs them nothing: no thread, no requests, no log line.
    """
    if not config.PUBLIC_URL:
        return
    Thread(target=_loop, args=(config.PUBLIC_URL,),
           name="same-origin-check", daemon=True).start()
