"""Jellyfin client. Jellyfin is the single library of record for Nextread.

Auth note: this audiobook fork REJECTS `?api_key=` with a 401.
Only the `Authorization: MediaBrowser Token="..."` header works.
"""
from dataclasses import dataclass

import httpx

from . import config

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


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str

    @property
    def key(self) -> str:
        """Stable-enough local scope shared with the SSO username."""
        return self.name.casefold()


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.JELLYFIN_URL, headers=_HEADERS, timeout=_TIMEOUT)


def user(name: str | None = None) -> User:
    """Resolve an SSO username to the matching Jellyfin account."""
    name = name or config.JELLYFIN_USER
    with _client() as c:
        users = c.get("/Users").raise_for_status().json()
    for u in users:
        if u["Name"].casefold() == name.casefold():
            return User(id=u["Id"], name=u["Name"])
    raise LookupError(f"no Jellyfin user named {name!r}")


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


def set_playlist(uid: str, name: str, item_ids: list[str]) -> str:
    """Create or update a playlist in place so its id survives between runs.

    A playlist (not a collection) because collections are server-global and this
    server has six users -- recommendations are per-person.
    """
    pid = find_playlist(uid, name)
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
