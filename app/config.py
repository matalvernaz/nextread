"""Runtime configuration, all from environment so nothing secret lands in the image."""
import os

# Jellyfin is the single library of record. Nextread never treats any other
# service as a catalogue -- see the project notes on why Listenarr is a queue.
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096")
JELLYFIN_TOKEN = os.environ["JELLYFIN_TOKEN"]
JELLYFIN_USER = os.environ.get("JELLYFIN_USER", "matt")

# Comma-separated Jellyfin library (view) ids to treat as audiobook sources.
LIBRARY_IDS = [x.strip() for x in os.environ.get("LIBRARY_IDS", "").split(",") if x.strip()]

LISTENARR_URL = os.environ.get("LISTENARR_URL", "http://listenarr:4545")
LISTENARR_QUALITY_PROFILE_ID = int(os.environ.get("LISTENARR_QUALITY_PROFILE_ID", "1"))

DB_PATH = os.environ.get("DB_PATH", "/data/nextread.db")

# Name of the single, persistent Jellyfin playlist we keep updated in place.
# Recreating it would churn item ids and reset the client's view every run.
PLAYLIST_NAME = os.environ.get("PLAYLIST_NAME", "Next Read")

# How long a cached Audible similar-products response stays fresh. The endpoint
# is unauthenticated and must not be hit on page load.
SIMS_TTL_HOURS = int(os.environ.get("SIMS_TTL_HOURS", "168"))

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

# Jellyfin item ids whose rating is known to be wrong and cannot be corrected.
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
