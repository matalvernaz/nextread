"""The recommendation engine.

Two surfaces, deliberately different in kind:

* **Own shelf** -- books already on disk and unplayed, ranked for "what next".
  Written back to Jellyfin as a playlist, so the existing client shows it with
  no app change.
* **Discover shelf** -- books not on disk, from Audible's similar-products
  graph. These can only be surfaced here, because a Jellyfin playlist can hold
  only items that exist in the library.

Signal, in order of strength: series continuation, Audible similarity votes,
author overlap, narrator overlap, genre affinity.

Ratings are deliberately NOT used yet. The column exists and is writable, but
the only value in it today is a test stamp, so treating it as taste would poison
the output. Fold `Rating` into `_seed_weight` once real ones accrue.
"""
from collections import Counter, defaultdict

from . import audible, config, jellyfin, listenarr, store

# Relative weights. Series continuation dominates on purpose: if he is five
# books into something and owns the sixth, that is the answer.
W_SERIES_NEXT = 100.0
W_SIMS_VOTE = 12.0
W_AUTHOR = 9.0
W_NARRATOR = 4.0
W_GENRE = 2.0
W_RECENT = 1.5

# A book added to the library in the last this-many days gets a nudge -- new
# arrivals are usually the ones he actually meant to get to.
RECENT_DAYS = 90


def _played(item: dict) -> bool:
    ud = item.get("UserData") or {}
    return bool(ud.get("Played")) or (ud.get("PlaybackPositionTicks") or 0) > 0


def _asin(item: dict) -> str | None:
    return (item.get("ProviderIds") or {}).get("Audible")

def _people(item: dict, kind: str) -> list[str]:
    return [p["Name"] for p in (item.get("People") or []) if p.get("Type") == kind and p.get("Name")]


def _authors(item: dict) -> list[str]:
    """Authors from People, falling back to AlbumArtist.

    Not interchangeable: 84 of this library's books carry no Author person but do
    carry an AlbumArtist, and reading only People silently drops them from both
    the taste profile and the owned-check.
    """
    names = _people(item, "Author")
    if names:
        return names
    artist = item.get("AlbumArtist")
    return [artist] if artist else []


def _norm(text: str) -> str:
    """Lowercase, punctuation-stripped, whitespace-collapsed."""
    return " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in text).lower().split())


def _title_keys(title: str) -> set[str]:
    """Normalised forms a title might be matched under.

    Editions differ: the same book is "Dark Lord of the Farmstead" on Audible and
    "Dark Lord of the Farmstead: A High Fantasy Slice-of-Life LitRPG" in the
    library, under two different ASINs. The subtitle-stripped form bridges that.
    Trailing volume numbers are deliberately NOT stripped -- "Master Class" and
    "Master Class 2" are different books and must not collide.
    """
    keys = set()
    full = _norm(title)
    if full:
        keys.add(full)
    for sep in (":", " - ", " \u2014 "):
        if sep in title:
            head = _norm(title.split(sep, 1)[0])
            # Two words minimum, or short titles collide across unrelated books.
            if head and len(head.split()) >= 2:
                keys.add(head)
    return keys


def _owned_index(library: list[dict]) -> tuple[set[str], dict[str, set[str]]]:
    """ASINs owned, and normalised-title -> author-set for everything else.

    ASIN alone is not enough: 76% of this library carries no Audible ASIN at all,
    so an ASIN-only check would recommend three quarters of the collection back.
    """
    asins = {_asin(i) for i in library if _asin(i)}
    by_title: dict[str, set[str]] = defaultdict(set)
    for item in library:
        authors = {_norm(a) for a in _authors(item)}
        for key in _title_keys(item.get("Name") or ""):
            by_title[key] |= authors
    return asins, by_title


def _already_owned(cand: dict, asins: set[str], by_title: dict[str, set[str]]) -> bool:
    """True when a candidate is a book already on disk under any edition.

    Title agreement alone would over-suppress, so an author must agree too --
    except where the library row has no author at all, which is the one case
    where the title has to stand on its own.
    """
    if cand["asin"] in asins:
        return True
    cand_authors = {_norm(a) for a in cand.get("authors") or []}
    for key in _title_keys(cand.get("title") or ""):
        if key not in by_title:
            continue
        owners = by_title[key]
        if not owners or (cand_authors & owners):
            return True
    return False


def _taste(seeds: list[dict]) -> dict:
    """Build a taste profile from HIS play history only.

    Not from library ownership: this server has six users and the collection is
    household-wide, so "we own it" is not evidence he likes it.
    """
    genres: Counter = Counter()
    authors: Counter = Counter()
    narrators: Counter = Counter()
    series: dict[str, float] = {}
    for item in seeds:
        for g in item.get("Genres") or []:
            genres[g] += 1
        for a in _authors(item):
            authors[a] += 1
        for n in _people(item, "Narrator"):
            narrators[n] += 1
        name = item.get("SeriesName")
        if name:
            idx = item.get("IndexNumber") or 0
            series[name] = max(series.get(name, 0), idx)
    return {"genres": genres, "authors": authors, "narrators": narrators, "series": series}


def _score_owned(item: dict, taste: dict, votes: Counter) -> tuple[float, list[str]]:
    """Score an owned, unplayed book. Returns (score, human-readable reasons)."""
    score = 0.0
    why: list[str] = []

    name = item.get("SeriesName")
    if name and name in taste["series"]:
        idx = item.get("IndexNumber") or 0
        if idx > taste["series"][name]:
            score += W_SERIES_NEXT / max(1.0, idx - taste["series"][name])
            why.append(f"next in {name}, a series you're partway through")

    asin = _asin(item)
    if asin and votes.get(asin):
        score += W_SIMS_VOTE * votes[asin]
        why.append(f"Audible lists it alongside {votes[asin]} book(s) you've listened to")

    shared_authors = [a for a in _authors(item) if a in taste["authors"]]
    if shared_authors:
        score += W_AUTHOR * len(shared_authors)
        why.append("by " + ", ".join(shared_authors[:2]) + ", who you've listened to")

    shared_narrators = [n for n in _people(item, "Narrator") if n in taste["narrators"]]
    if shared_narrators:
        score += W_NARRATOR * len(shared_narrators)
        why.append("narrated by " + shared_narrators[0])

    shared_genres = [g for g in (item.get("Genres") or []) if g in taste["genres"]]
    if shared_genres:
        score += W_GENRE * sum(taste["genres"][g] for g in shared_genres)
        why.append(", ".join(shared_genres[:3]))

    return score, why


def _score_candidate(cand: dict, taste: dict, votes: Counter) -> tuple[float, list[str]]:
    """Score a book he does NOT own. Less metadata to work with than an owned book."""
    score = W_SIMS_VOTE * votes.get(cand["asin"], 0)
    why: list[str] = []
    n = votes.get(cand["asin"], 0)
    if n:
        why.append(f"Audible lists it alongside {n} book(s) you've listened to")

    shared = [a for a in cand.get("authors") or [] if a in taste["authors"]]
    if shared:
        score += W_AUTHOR * len(shared)
        why.append("by " + ", ".join(shared[:2]) + ", who you've listened to")

    shared_n = [n for n in cand.get("narrators") or [] if n in taste["narrators"]]
    if shared_n:
        score += W_NARRATOR * len(shared_n)
        why.append("narrated by " + shared_n[0])

    return score, why


def run(update_playlist: bool = True) -> dict:
    """One full recommendation pass. Returns both shelves plus run stats."""
    run_id = store.start_run()
    uid = jellyfin.user_id()
    library = jellyfin.books(uid)

    seeds = [i for i in library if _played(i)]
    taste = _taste(seeds)

    # Audible similarity votes, seeded only from books he has actually listened to.
    votes: Counter = Counter()
    seed_of: dict[str, list[str]] = defaultdict(list)
    unowned: dict[str, dict] = {}
    owned_asins, owned_titles = _owned_index(library)

    for seed in seeds:
        asin = _asin(seed)
        if not asin:
            continue
        for sim in audible.sims(asin):
            votes[sim["asin"]] += 1
            seed_of[sim["asin"]].append(seed.get("Name") or "")
            if not _already_owned(sim, owned_asins, owned_titles):
                unowned.setdefault(sim["asin"], sim)

    # --- own shelf: on disk, unplayed, ranked ---
    own = []
    for item in library:
        if _played(item):
            continue
        score, why = _score_owned(item, taste, votes)
        if score <= 0:
            continue
        own.append({
            "id": item["Id"],
            "title": item.get("Name") or "",
            "authors": _authors(item),
            "series": item.get("SeriesName"),
            "score": round(score, 1),
            "why": why,
        })
    own.sort(key=lambda r: -r["score"])
    own = own[: config.MAX_SHELF]

    # --- discover shelf: not on disk ---
    suppressed = store.suppressed_asins() | listenarr.queued_asins()
    discover = []
    for asin, cand in unowned.items():
        if asin in suppressed:
            continue
        score, why = _score_candidate(cand, taste, votes)
        if score <= 0:
            continue
        discover.append({**cand, "score": round(score, 1), "why": why,
                         "because_of": seed_of[asin][:3]})
    discover.sort(key=lambda r: -r["score"])
    discover = discover[: config.MAX_SHELF]

    playlist_id = None
    if update_playlist and own:
        playlist_id = jellyfin.set_playlist(
            uid, config.PLAYLIST_NAME, [r["id"] for r in own]
        )

    store.finish_run(run_id, len(seeds), len(own), len(discover),
                     note=f"playlist={playlist_id or 'skipped'}")
    return {
        "seeds": len(seeds),
        "library": len(library),
        "own": own,
        "discover": discover,
        "playlist_id": playlist_id,
    }
