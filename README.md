# Nextread

Per-user audiobook recommendations built from Jellyfin listening history.

- **Identifies the listener through SSO** — the trusted Keycloak forward-auth
  proxy supplies a username, which is matched to a Jellyfin account. There is no
  user selector to spoof and no second login.
- **Reads Jellyfin** — the single library of record. Books, ASINs, genres,
  series, ratings, and play state come from the signed-in user's account.
- **Recommends via Audible** — `/1.0/catalog/products/{asin}/sims`, which needs
  no key or account. Responses are cached in SQLite, while rendered results use
  a one-hour in-memory cache.
  When Jellyfin holds a sibling Kindle or alternate-edition ASIN that returns no
  neighbours, an exact title-and-author match through Listenarr resolves and
  caches the audiobook ASIN instead.
- **Writes back two ways** — owned-and-unplayed picks become a Jellyfin
  playlist for that user (so existing clients show it with no app change);
  unowned picks appear on this app's own page, and approving one hands it to
  Listenarr.

Listenarr is treated as an acquisition work queue, not a catalogue: its library
holds only what it has bought. Nextread reads its queue state solely to avoid
recommending a book that is already on order.

## Users and isolation

Traefik's `sso` middleware writes `X-Auth-Request-Preferred-Username`. Nextread
matches that value case-insensitively against Jellyfin's users and returns `403`
when no account matches. Identity never comes from a form, cookie, query string,
or client-chosen user id.

Each Jellyfin user has independent recommendation results, rating counts,
listening history, refresh cache, dismissals, run history, and playlist contents.
The account named by `JELLYFIN_USER` keeps the original `Next Read` playlist;
other accounts use `Next Read — <username>`, preventing one user's refresh from
overwriting another's playlist on servers where playlists are globally visible.

Audible responses and edition aliases are metadata, so they remain shared cache
entries. Listenarr is also intentionally shared: when one person requests a
book, it is acquired once and suppressed from everyone else's unowned shelf.

Nextread trusts the configured identity header. Do not expose its container
directly or put it behind a proxy that lets clients supply that header. In this
deployment, set Nextread to **Members** in the Accounts app so Traefik both
authenticates every visitor and forwards `X-Auth-Request-Preferred-Username`.

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
rating. Those ids apply only to `JELLYFIN_USER`; another listener's rating on the
same book remains valid.

Listening progress is a separate confidence signal. A completed book carries
full weight; a partial listen contributes in proportion to its played percentage,
so sampling two minutes does not count like finishing twenty hours. An explicit
rating is introduced through the same ramp rather than bypassing its safety floor.

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

All configuration comes from the environment; see `app/config.py`.

- `JELLYFIN_TOKEN` is required and needs to list users, read their user data,
  and update playlists.
- `AUTH_USER_HEADER` defaults to `X-Auth-Request-Preferred-Username`.
- `JELLYFIN_USER` defaults to `matt`. It is the fallback for direct development,
  the owner of migrated single-user state, and the only account whose playlist
  keeps the unsuffixed `PLAYLIST_NAME`.
- `PLAYLIST_NAME` defaults to `Next Read`.
- `LIBRARY_IDS` limits the audiobook libraries included in every user's model.

The database migration is automatic and transactional. Existing submitted
books, dismissals, and run history are assigned to `JELLYFIN_USER`; subsequent
dismissals and runs are scoped to the authenticated account.

## Deploy

Stack lives at `/opt/stacks/nextread` inside the `dockge` Incus container.

    docker build -t nextread:local .
    docker compose up -d

Access level is data, not config: the `nextread` entry in
`/opt/stacks/keycloak-invite/catalog.json` renders the `access-nextread@file`
middleware. Multi-user operation requires that policy to target `sso` (the
**Members** level). Without the access middleware Traefik silently drops the
router.
