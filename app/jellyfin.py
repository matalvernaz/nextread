"""Jellyfin client. Jellyfin is the single library of record for Nextread.

Auth note: this audiobook fork REJECTS `?api_key=` with a 401.
Only the `Authorization: MediaBrowser Token="..."` header works.
"""
from dataclasses import dataclass

import httpx

from . import config, logs

log = logs.get("jellyfin")

_HEADERS = {
    "Authorization": f'MediaBrowser Token="{config.JELLYFIN_TOKEN}"',
    "Accept": "application/json",
}

# Fields we need for both ranking and display. Requested explicitly because
# Jellyfin omits most of them by default.
_ITEM_FIELDS = (
    "ProviderIds,Genres,UserData,DateCreated,People,Overview,RunTimeTicks,"
    "SeriesName,IndexNumber,ParentIndexNumber,AlbumArtist"
)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# The credential check answers a health endpoint, and the container's health
# probe gives that endpoint ten seconds. The ordinary timeout would spend all
# of them waiting for a server that is merely slow.
_CREDENTIAL_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    is_admin: bool = False

    @property
    def key(self) -> str:
        """Stable-enough local scope shared with the SSO username."""
        return self.name.casefold()


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.JELLYFIN_URL, headers=_HEADERS, timeout=_TIMEOUT)


def credential_rejected() -> bool:
    """Whether Jellyfin is actively refusing this service's own API key.

    True means a person has to act: the key was revoked, or the server stopped
    accepting the form it is sent in. Everything else -- a refused connection,
    a timeout, a 5xx -- is Jellyfin having a moment, which this service cannot
    fix and which passes on its own, so it reads as False rather than flapping
    the health state on somebody else's restart.
    """
    try:
        with httpx.Client(base_url=config.JELLYFIN_URL, headers=_HEADERS,
                          timeout=_CREDENTIAL_TIMEOUT) as c:
            resp = c.get("/System/Info")
    except httpx.HTTPError:
        return False
    return resp.status_code in (401, 403)


def user(name: str | None = None) -> User:
    """Resolve an SSO username to the matching Jellyfin account."""
    name = name or config.JELLYFIN_USER
    with _client() as c:
        users = c.get("/Users").raise_for_status().json()
    for u in users:
        if u["Name"].casefold() == name.casefold():
            return _to_user(u)
    log.warning("no Jellyfin account matches signed-in user %r", name)
    raise LookupError(f"no Jellyfin user named {name!r}")


def _to_user(dto: dict) -> User:
    """A Jellyfin UserDto as this app's user. Administrator means keyholder."""
    policy = dto.get("Policy") or {}
    return User(id=dto["Id"], name=dto["Name"],
                is_admin=bool(policy.get("IsAdministrator")))


class TokenRejected(Exception):
    """The caller's Jellyfin access token is missing, malformed, or unknown."""


class JellyfinUnavailable(Exception):
    """Jellyfin could not be reached, so no token can be judged either way."""


def user_from_token(token: str) -> User:
    """The account a caller's own Jellyfin access token belongs to.

    This is the whole of the JSON API's authentication, and it deliberately has
    no fallback. `GET /Users/Me` answers 200 only for a real user token: a
    service API key carries no user context and gets 400, and an unknown token
    gets 401 (all three verified against this fork). Anything that is not a 200
    is a rejection.

    The header resolver used by the HTML pages falls back to JELLYFIN_USER when
    no identity is present. That fallback must never be reachable from here --
    the API path bypasses SSO at the proxy, so it would hand any caller the
    owner's shelf and his daily allowance.
    """
    if not token:
        raise TokenRejected("no access token")
    headers = {"Authorization": f'MediaBrowser Token="{token}"',
               "Accept": "application/json"}
    try:
        with httpx.Client(base_url=config.JELLYFIN_URL, headers=headers,
                          timeout=_TIMEOUT) as c:
            resp = c.get("/Users/Me")
    except httpx.HTTPError as exc:
        log.error("token introspection unreachable fingerprint=%s (%s)",
                  logs.fingerprint(token), exc)
        raise JellyfinUnavailable(str(exc)) from exc
    if resp.status_code != 200:
        # 401 is an unknown token; 400 is a service API key, which has no user
        # behind it. Neither may be allowed through.
        log.warning("token rejected fingerprint=%s status=%d",
                    logs.fingerprint(token), resp.status_code)
        raise TokenRejected(f"Jellyfin answered {resp.status_code}")
    try:
        user = _to_user(resp.json())
    except (ValueError, KeyError) as exc:
        log.error("token introspection returned an unreadable user fingerprint=%s",
                  logs.fingerprint(token))
        raise TokenRejected("Jellyfin returned an unreadable user") from exc
    log.info("token accepted fingerprint=%s user=%s keyholder=%s",
             logs.fingerprint(token), user.name, user.is_admin)
    return user


def token_from_header(value: str | None) -> str:
    """The Token= field of a Jellyfin `Authorization` header, or "".

    Clients send the whole handshake, not a bare token:
    `MediaBrowser Token="abc", Client="EchoFin", Device="iPhone", ...`.
    Order is not guaranteed, so the field is picked out by name.
    """
    if not value:
        return ""
    for part in value.split(","):
        key, sep, raw = part.strip().partition("=")
        if sep and key.strip().rpartition(" ")[2].casefold() == "token":
            return raw.strip().strip('"')
    return ""


def user_id(name: str | None = None) -> str:
    """Compatibility helper for scripts that only need the Jellyfin id."""
    return user(name).id


def library_ids() -> list[str]:
    """Configured audiobook library ids, or every 'books' view if unset."""
    if config.LIBRARY_IDS:
        return config.LIBRARY_IDS
    with _client() as c:
        views = c.get("/Library/VirtualFolders").raise_for_status().json()
    return [v["ItemId"] for v in views if v.get("CollectionType") == "books"]


def books(uid: str) -> list[dict]:
    """Every audiobook the client can see, with this user's play state attached.

    The fork excludes owned multi-part children by default, so this returns whole
    books rather than parts.
    """
    out: list[dict] = []
    with _client() as c:
        for lib in library_ids():
            params = {
                "parentId": lib,
                "includeItemTypes": "AudioBook",
                "recursive": "true",
                "fields": _ITEM_FIELDS,
                "userId": uid,
                "limit": 5000,
            }
            data = c.get("/Items", params=params).raise_for_status().json()
            out.extend(data.get("Items", []))
    return out


def find_playlist(uid: str, name: str) -> str | None:
    """Id of this user's playlist with the given name, or None."""
    with _client() as c:
        params = {
            "includeItemTypes": "Playlist",
            "recursive": "true",
            "userId": uid,
            "limit": 500,
        }
        data = c.get("/Items", params=params).raise_for_status().json()
    for item in data.get("Items", []):
        if item.get("Name") == name:
            return item["Id"]
    return None


def set_playlist(uid: str, name: str, item_ids: list[str]) -> str | None:
    """Create or update a playlist in place so its id survives between runs.

    A playlist (not a collection) because collections are server-global and this
    server has six users -- recommendations are per-person.
    """
    pid = find_playlist(uid, name)
    # An empty first result has nothing to create. An existing playlist still
    # has to be cleared below, or stale recommendations survive indefinitely.
    if pid is None and not item_ids:
        return None
    with _client() as c:
        if pid is None:
            body = {"Name": name, "Ids": item_ids, "UserId": uid, "MediaType": "Audio"}
            created = c.post("/Playlists", json=body).raise_for_status().json()
            return created["Id"]

        existing = c.get(
            f"/Playlists/{pid}/Items", params={"userId": uid, "limit": 5000}
        ).raise_for_status().json()
        entry_ids = [i["PlaylistItemId"] for i in existing.get("Items", []) if i.get("PlaylistItemId")]
        if entry_ids:
            c.request(
                "DELETE",
                f"/Playlists/{pid}/Items",
                params={"entryIds": ",".join(entry_ids)},
            ).raise_for_status()
        if item_ids:
            c.post(
                f"/Playlists/{pid}/Items",
                params={"ids": ",".join(item_ids), "userId": uid},
            ).raise_for_status()
    return pid
