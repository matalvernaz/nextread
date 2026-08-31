"""Runtime configuration, all from environment so nothing secret lands in the image."""
import os

# Jellyfin is the single library of record. Nextread never treats any other
# service as a catalogue -- see the project notes on why Listenarr is a queue.
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096")
JELLYFIN_TOKEN = os.environ["JELLYFIN_TOKEN"]
# The trusted forward-auth proxy supplies this for normal web requests. The
# fallback keeps direct/local operation possible and owns legacy single-user
# state during the multi-user database migration.
AUTH_USER_HEADER = os.environ.get(
    "AUTH_USER_HEADER", "X-Auth-Request-Preferred-Username")
# Empty by default, and deliberately not a name. This is the identity assumed
# when the forward-auth header is absent, and a default that names somebody
# means a fresh install elsewhere quietly tries to resolve a person who does
# not exist there. Unset, the pages refuse instead of guessing, which is also
# the safer reading of a missing header.
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "").strip()

# Comma-separated Jellyfin library (view) ids to treat as audiobook sources.
LIBRARY_IDS = [x.strip() for x in os.environ.get("LIBRARY_IDS", "").split(",") if x.strip()]

# Which Audible marketplace this household buys from. `ca` here, not `us`:
# measured 2026-08-27, `api.audible.com` answers 200 with an EMPTY product for
# B0DCHQ9QT7 (Demon World Boba Shop) while `api.audible.ca` returns the real
# record. This library is indie Audible-CA progression fantasy, so the US store
# is missing or mis-spelling a large part of it, and every lookup, similarity
# call and keyword search has to agree on the region or the shelf recommends
# books that are already on disk.
# Marketplaces to consult, in order. A LIST, not one value, because this
# household's library genuinely spans two: B0CWW1L8NL ("I Ran Away to Evil")
# exists on audible.ca and not on .com, while B0HC7V8ZR4 ("Unicorn Breeder")
# exists on .com and not on .ca -- measured 2026-08-28. Whichever single region
# were chosen, the other store's books would answer with an empty product, and
# an empty product is what let a request be filled with the wrong book.
#
# First is preferred: it decides ties and it is the region an item is filed
# under when both stores have it.
AUDIBLE_REGIONS = [
    r.strip().lower()
    for r in os.environ.get("AUDIBLE_REGIONS", "ca,us").split(",")
    if r.strip()
] or ["ca"]

# The preferred one, for callers that can only carry a single value.
AUDIBLE_REGION = AUDIBLE_REGIONS[0]

LISTENARR_URL = os.environ.get("LISTENARR_URL", "http://listenarr:4545")
LISTENARR_QUALITY_PROFILE_ID = int(os.environ.get("LISTENARR_QUALITY_PROFILE_ID", "1"))

DB_PATH = os.environ.get("DB_PATH", "/data/nextread.db")

# Name of the single, persistent Jellyfin playlist we keep updated in place.
# Recreating it would churn item ids and reset the client's view every run.
PLAYLIST_NAME = os.environ.get("PLAYLIST_NAME", "Next Read")

# How long a cached Audible similar-products response stays fresh. The endpoint
# is unauthenticated and must not be hit on page load.
SIMS_TTL_HOURS = int(os.environ.get("SIMS_TTL_HOURS", "168"))

# A blurb and a runtime, which change about as often as a book gets re-issued.
# Shorter than the sims TTL only because it is cheap to refetch one product.
PRODUCT_TTL_HOURS = int(os.environ.get("PRODUCT_TTL_HOURS", "720"))

# How many search hits to ask Listenarr for. Its own cap applies too; this is
# what a person can stand to hear read out in one list.
SEARCH_LIMIT = int(os.environ.get("SEARCH_LIMIT", "25"))

# Audible caps sims responses; ask for a useful spread per seed.
SIMS_PER_SEED = int(os.environ.get("SIMS_PER_SEED", "10"))

# How many recommendations each surface shows.
MAX_SHELF = int(os.environ.get("MAX_SHELF", "40"))

# Signed rating mode -- where a low score pushes a neighbourhood away rather than
# merely failing to pull it -- stays off until there are this many ratings.
#
# One rating must never steer the engine. There is a known bad datum in the
# store: a 1 written to "The Grief of Stones" while verifying the write path,
# judging the file (a 0.2 MB ebook) and not the book. Alone it would suppress an
# author this listener demonstrably likes. A floor dilutes it and guards the
# general case of a single early rating lurching every shelf.
MIN_RATINGS_FOR_SIGNED_MODE = int(os.environ.get("MIN_RATINGS_FOR_SIGNED_MODE", "5"))

# Ratings ramp in over this many more ratings rather than switching on.
#
# A hard gate produced exactly the lurch it was written to prevent: at the
# threshold every unrated seed would drop from parity with a rated one to
# NEUTRAL_WEIGHT in a single pass, so half the shelf would reorder the moment one
# rating landed. The floor above is where rating influence BEGINS; this is how
# long it takes to reach full strength.
RATINGS_RAMP_SPAN = int(os.environ.get("RATINGS_RAMP_SPAN", "15"))

# Jellyfin item ids whose rating is known to be wrong for JELLYFIN_USER and
# cannot be corrected. Other users' ratings on the same books remain valid.
#
# There is exactly one: a 1 written to "The Grief of Stones" while verifying the
# write path, judging the file (a 0.2 MB ebook mislabelled as an 8-hour
# audiobook) rather than the book. Jellyfin has no route that clears a rating, so
# this cannot be undone -- only overwritten by a real one, which removing the id
# from this list is how you'd then honour. Until then it is excluded from both
# the seed set and the rating count, because otherwise it steers the profile with
# a score its owner never gave.
IGNORED_RATING_ITEM_IDS = frozenset(
    x.strip().replace("-", "").lower()
    for x in os.environ.get(
        "IGNORED_RATING_ITEM_IDS", "7905477C-A118-4F4E-16CE-142C5175547B").split(",")
    if x.strip()
)

# Keyword search is the only channel that can surface a book with no connection
# whatever to a finished one -- and it is OFF, because measured on this data it
# does not work yet.
#
# Two derivations were tried and both produced noise. Audible's genre tags are
# far too broad ("Science Fiction & Fantasy", and "Children's Audiobooks" via the
# full-cast Harry Potter editions, which returned The Gruffalo and Cinderella).
# The TF-IDF profile's own top terms are no better at this scale: with ten seeds
# the profile is a handful of specific books' vocabulary rather than a genre
# signature, so the queries came out as proper nouns and blurb boilerplate
# ("peace gelderham grows") and matched nothing at all.
#
# What would fix it is more ratings, not more code: once the profile is built
# from dozens of scored books its top terms become genuinely generic to the
# genre. Turn this on then and re-measure.
KEYWORD_PULL_ENABLED = os.environ.get("KEYWORD_PULL_ENABLED", "false").lower() == "true"
KEYWORD_QUERIES_MAX = int(os.environ.get("KEYWORD_QUERIES_MAX", "4"))
KEYWORD_SHELF_SHARE = float(os.environ.get("KEYWORD_SHELF_SHARE", "0.25"))

# A dismissal means "not now", not an irreversible judgement. Taste changes,
# editions change, and an accidental tap must not suppress a book forever.
DISMISS_TTL_DAYS = int(os.environ.get("DISMISS_TTL_DAYS", "30"))

# Recommendation snapshots make requests and dismissals attributable to the
# ranker run that produced them. Kept long enough to compare outcomes across a
# few release cycles without turning this small SQLite database into a ledger
# with no bound.
ATTRIBUTION_RETENTION_DAYS = int(
    os.environ.get("ATTRIBUTION_RETENTION_DAYS", "180"))


# --- Requests: acquiring a book somebody asked for ---------------------------

# The JSON API's shape version. A client reads it and refuses a shape it does
# not know rather than guessing at missing fields.
SERVICE_NAME = "nextread"

API_VERSION = 1

# Where clients reach this service at the Jellyfin origin, e.g.
# "https://jellyfin.example.com/nextread". Optional, and only ever used to
# check that route is really there -- see app/selfcheck.py. Unset means the
# check does not run, which is right for an install that serves only the
# browser pages.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")

# How many books a non-keyholder may request per rolling day.
#
# This app used to be keyholder-only, and every acquisition then waited up to
# six hours for Listenarr's sweep. Opening requests to every account and
# searching immediately removes both of those brakes at once, so this is the
# one that replaces them: high enough that a listener never meets it in normal
# use, low enough that ten accounts cannot fill the disk in an evening.
# Jellyfin administrators are not capped.
WANT_DAILY_CAP = int(os.environ.get("WANT_DAILY_CAP", "3"))

# How long a request reads as "on its way" before it reads "still looking".
#
# There is no state that distinguishes "still searching" from "found nothing":
# a throttled indexer returns zero results silently rather than erroring, and
# the sweep keeps retrying a monitored book indefinitely. Two sweep cycles is
# long enough that a normal acquisition never trips this, and short enough that
# a book no indexer carries stops claiming to be arriving.
STILL_LOOKING_AFTER_HOURS = int(os.environ.get("STILL_LOOKING_AFTER_HOURS", "12"))

# How long an introspected Jellyfin access token stays trusted.
#
# Without it every API request costs a round trip to Jellyfin. Kept short
# because expiry is the only thing that makes a revoked token stop working.
TOKEN_CACHE_SECONDS = int(os.environ.get("TOKEN_CACHE_SECONDS", "60"))

# Log verbosity. INFO narrates every request and every acquisition step; DEBUG
# adds the per-candidate scoring detail, which is far too noisy to leave on.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
