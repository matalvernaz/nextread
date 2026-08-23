# Nextread

Audiobook recommendations built from your own listening history.

- **Reads Jellyfin** — the single library of record. Books, ASINs, genres,
  series, and per-user play state.
- **Recommends via Audible** — `/1.0/catalog/products/{asin}/sims`, which needs
  no key or account. Responses are cached in SQLite; never called on page load.
- **Writes back two ways** — owned-and-unplayed picks become a Jellyfin
  playlist (so existing clients show them with no app change); unowned picks
  appear on this app's own page, and approving one hands it to Listenarr.

Listenarr is treated as an acquisition work queue, not a catalogue: its library
holds only what it has bought. Nextread reads its queue state solely to avoid
recommending a book that is already on order.

Ratings are not used as signal yet. Jellyfin can store them
(`POST /UserItems/{id}/UserData` with `{"Rating": 0-10}`) but no real ones exist
so far. Fold `Rating` into the seed weighting in `engine.py` once they do.

## Configuration

All via environment — see `app/config.py`. Required: `JELLYFIN_TOKEN`.

## Deploy

Stack lives at `/opt/stacks/nextread` inside the `dockge` Incus container.

    docker build -t nextread:local .
    docker compose up -d

Access level is data, not config: the `nextread` entry in
`/opt/stacks/keycloak-invite/catalog.json` renders the `access-nextread@file`
middleware. Without it Traefik silently drops the router.
