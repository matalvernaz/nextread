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

## Requesting a book

Asking for an unowned book is one action with one code path, shared by the web
form and the JSON API, so a second caller cannot be added later that skips half
the guards.

What happens on a request: Listenarr is checked for the ASIN, the book is added
monitored, the request is written to this app's ledger, and a search is **queued**
with Listenarr rather than awaited. The queue has a single paced consumer, which
is what stops ten accounts tapping at once from becoming ten simultaneous
indexer hits. If the queue refuses the job -- or the Listenarr in front of you
is too old to have the route -- nothing is lost: the book is monitored, so the
6-hourly sweep still acquires it.

Afterwards the request has one of three states, and all three are derived
rather than fetched:

| State | Means |
|---|---|
| `on_its_way` | asked for, not yet on disk |
| `still_looking` | waiting longer than `STILL_LOOKING_AFTER_HOURS`. Not a failure: the book stays monitored and keeps being retried |
| `in_library` | its ASIN is now among the library's, so it is an ordinary item |

Arrival is a set-membership test against the ASINs already on disk, which the
engine builds on every run anyway. There is no status to poll and nothing to
subscribe to. It follows that the round trip depends on the imported file
carrying its Audible ASIN through to Jellyfin -- if that tag is lost, a book
arrives and its row stays `still_looking` for ever.

`WANT_DAILY_CAP` bounds how many books a non-keyholder may request per rolling
day; Jellyfin administrators are not capped. It exists because opening requests
to every account **and** searching immediately removes both of the brakes this
app used to have (keyholder-only access, and a six-hour wait for the sweep).
A repeated request for the same book is free: the ledger is keyed on
(account, ASIN), so a second tap neither restarts the clock nor spends another
day's allowance.

Requests are suppressed globally, dismissals only for the person who made them.
Listenarr is shared, so a book one listener asks for is acquired once and stops
being offered to everybody else.

## JSON API

For clients that cannot complete a browser sign-in -- which is every native app.
`GET /api/v1/capabilities`, `GET /api/v1/shelves`, `POST /api/v1/want`,
`POST /api/v1/dismiss`.

**Authentication is the caller's own Jellyfin access token**, sent as
`Authorization: MediaBrowser Token="..."` or `X-Emby-Token`, never in a query
string. Nextread introspects it with `GET /Users/Me`, which answers 200 only for
a real user token: a service API key has no user context and gets 400, an
unknown token gets 401. Anything that is not a 200 is a rejection, and Jellyfin
being unreachable is a 503 rather than a guess.

This path is reachable **without** the SSO middleware, so it deliberately does
not share the HTML resolver's fallback to `JELLYFIN_USER`. That fallback on a
bypassed route would hand any caller the owner's shelf and his allowance.

Two things follow for the proxy in front of it: the `/api/` router needs its own
Traefik rule with **explicit priorities pinned on both it and the main router**
(priority defaults to rule length, so lengthening the main rule can silently
swallow a bypass), and `/`, `/want` and `/dismiss` must keep their SSO chain.

`GET /shelves` has no side effects. It returns owned picks as Jellyfin **item
ids** rather than rendered rows, so a client hydrates them through its ordinary
item request and keeps resume position, downloads and play-on-activation. Only
the unowned half is described in full, because it has no library item to
describe.

## Logging

Every acquisition is asynchronous and crosses three services, and no screen ever
shows the whole of one. `app/logs.py` is where the level is set (`LOG_LEVEL`,
default INFO) and it holds the two standing rules: never log an access token
(log `fingerprint()` of it), and log the *soft* failures loudest. The paths that
return an empty list when Listenarr is unreachable are the ones that degrade
invisibly -- a missing suppression list looks exactly like a good shelf, and a
failed Audible search quietly thins the unowned half rather than emptying it.

## Configuration

All configuration comes from the environment; see `app/config.py`.

- `JELLYFIN_TOKEN` is required and needs to list users, read their user data,
  and update playlists.
- `AUTH_USER_HEADER` defaults to `X-Auth-Request-Preferred-Username`.
- `JELLYFIN_USER` is empty by default and is the identity assumed when the
  forward-auth header is absent. Unset, the HTML pages refuse rather than guess,
  which is what you want behind a proxy that is supposed to set that header; set
  it for direct access without one. It is also the owner of migrated
  single-user state and the only account whose playlist keeps the unsuffixed
  `PLAYLIST_NAME`.
- `PLAYLIST_NAME` defaults to `Next Read`.
- `LIBRARY_IDS` limits the audiobook libraries included in every user's model.
- `WANT_DAILY_CAP` (default 3) bounds requests per non-keyholder per day.
- `STILL_LOOKING_AFTER_HOURS` (default 12) is when a request stops claiming to
  be arriving. Two sweep cycles.
- `TOKEN_CACHE_SECONDS` (default 60) is how long an introspected access token
  stays trusted. Short because expiry is the only thing that makes a token
  revoked in Jellyfin stop working here.
- `LOG_LEVEL` (default INFO).

The database migration is automatic and transactional. Existing submitted
books, dismissals, and run history are assigned to `JELLYFIN_USER`; subsequent
dismissals and runs are scoped to the authenticated account.

## Serving it where clients find it themselves

A client that already knows where Jellyfin is should not have to be told where
this is. The convention, which EchoFin implements and any other client can:

    https://<your-jellyfin-origin>/nextread/api/v1/...

Serve **only** `/nextread/api` there, and strip the `/nextread` prefix before it
reaches this app. Nothing changes in Jellyfin: no plugin, no setting, no
restart. It is one rule in whatever already terminates TLS for Jellyfin.

**Do not serve the HTML pages at that origin.** They take their identity from
the forward-auth header, so they belong behind whatever authentication your
Jellyfin host does *not* apply. The JSON API is safe there because it
authenticates every request by introspecting the caller's own Jellyfin access
token; it never consults `JELLYFIN_USER`.

Traefik, as labels on this container:

```yaml
- "traefik.http.routers.nextread-jellyfin.rule=Host(`jellyfin.example.com`) && PathPrefix(`/nextread/api`)"
- traefik.http.routers.nextread-jellyfin.entrypoints=websecure
# Must outrank Jellyfin's own Host() router, whose rule is shorter.
- traefik.http.routers.nextread-jellyfin.priority=200
- traefik.http.routers.nextread-jellyfin.middlewares=nextread-strip
- traefik.http.middlewares.nextread-strip.stripprefix.prefixes=/nextread
- traefik.http.routers.nextread-jellyfin.service=nextread
- traefik.http.routers.nextread-jellyfin.tls=true
```

nginx, in the Jellyfin server block:

```nginx
location /nextread/api/ {
    proxy_pass http://nextread:8080/api/;
    proxy_set_header Host $host;
}
```

Caddy, in the Jellyfin site block:

```
handle_path /nextread/api/* {
    reverse_proxy nextread:8080 {
        rewrite /api{uri}
    }
}
```

### Checking it

    curl -s -o /dev/null -w '%{http_code}\n' https://jellyfin.example.com/nextread/api/v1/capabilities

**401 is the passing answer** — the request reached this app, the prefix came
off, and it declined a caller with no token. A **404** means the rule did not
take: the proxy is answering, not this. Two known ways to get one: a rule that
does not outrank Jellyfin's own, and a Traefik docker provider that has not
registered the container yet, which takes 25-30 seconds.

Confirm you have not shadowed anything of Jellyfin's either:

    curl -s -o /dev/null -w '%{http_code}\n' https://jellyfin.example.com/api/v1/capabilities   # expect 404

### What a client should do

1. Ask `<origin>/nextread/api/v1/capabilities` with the user's Jellyfin token
   in an `Authorization: MediaBrowser Token="..."` header.
2. Treat any failure as "not installed" and say nothing. Most servers do not
   run this, and an error surface for an absent optional service is noise.
3. Check `version` against what it understands, and `libraryIds` for the
   library it is showing.
4. Offer a manual address as an override, for a service that runs somewhere
   its Jellyfin does not front.

Deriving the address rather than storing one also keeps the traffic on
whichever route the client is already using, so a client on the same LAN as the
server does not leave the network to reach this.

## Deploy

Stack lives at `/opt/stacks/nextread` inside the `dockge` Incus container.

    docker build -t nextread:local .
    docker compose up -d

Access level is data, not config: the `nextread` entry in
`/opt/stacks/keycloak-invite/catalog.json` renders the `access-nextread@file`
middleware. Multi-user operation requires that policy to target `sso` (the
**Members** level). Without the access middleware Traefik silently drops the
router.
