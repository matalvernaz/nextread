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

## Ratings

Jellyfin stores a 0-10 rating at `POST /UserItems/{id}/UserData` with
`{"Rating": n}`. (The sibling `.../Rating` route is a decoy — it writes a thumb.)

Ratings feed the engine through a **ramp**, not a switch. Below
`MIN_RATINGS_FOR_SIGNED_MODE` they are ignored entirely; above it their influence
grows over `RATINGS_RAMP_SPAN` more ratings. A hard gate was tried first and was
wrong: at the threshold every unrated seed dropped from parity with a rated one to
`NEUTRAL_WEIGHT` in a single pass, so half the shelf reordered the moment one
rating landed.

Once ratings are in effect they are **signed**. A high score amplifies a seed; a
low one inverts it, so its vocabulary is pushed away rather than merely not
pulled. Without that, finishing a book you resented reads identically to loving
it. Disliked seeds also stop contributing Audible votes and stop lending their
author and genre any affinity.

`IGNORED_RATING_ITEM_IDS` exists because Jellyfin has no route that clears a
rating. One bad rating is permanent, so it is excluded by id instead.

## Two similarity channels

- **Audible `/sims`**, keyed on ASIN. Strong, but it takes one ASIN and returns
  neighbours: it can never consume a rating vector, and it is blind to the 76% of
  this library that carries no ASIN.
- **Local TF-IDF over descriptions** (`app/textmodel.py`). Unigrams plus bigrams
  — "system" is generic, "system apocalypse" is a fingerprint. This is the half
  that consumes the whole rating vector, and the only half that reaches the
  ASIN-less majority. A lexical baseline, not embeddings; `vectorise` is the seam
  to swap if that ever changes.

Titles are excluded from the vectors on purpose: a title is a near-unique proper
noun, so idf hands it top weight and the profile comes out as series names
("potter", "farmstead") rather than themes.

Keyword discovery exists and is **off** — see the reasoning in `app/config.py`.
It failed on real data twice and needs more ratings, not more code.

## Configuration

All via environment — see `app/config.py`. Required: `JELLYFIN_TOKEN`.

## Deploy

Stack lives at `/opt/stacks/nextread` inside the `dockge` Incus container.

    docker build -t nextread:local .
    docker compose up -d

Access level is data, not config: the `nextread` entry in
`/opt/stacks/keycloak-invite/catalog.json` renders the `access-nextread@file`
middleware. Without it Traefik silently drops the router.
