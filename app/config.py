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
