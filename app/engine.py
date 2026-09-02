"""The recommendation engine.

Two surfaces, deliberately different in kind:

* **Own shelf** -- books already on disk and unplayed, ranked for "what next".
  Written back to Jellyfin as a playlist, so the existing client shows it with
  no app change.
* **Discover shelf** -- books not on disk, from Audible's similar-products
  graph. These can only be surfaced here, because a Jellyfin playlist can hold
  only items that exist in the library.

Signal, in order of strength: series continuation, Audible similarity votes,
description similarity, author overlap, narrator overlap, genre affinity.

Partial listens contribute in proportion to progress rather than counting like
completed books. Ratings are signed and ramped once enough exist to avoid letting
one early score reorder the whole shelf.
"""
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import audible, config, jellyfin, listenarr, logs, store, textmodel

log = logs.get("engine")

# Relative weights. Series continuation dominates on purpose: if a listener is
# five books into something and owns the sixth, that is the answer.
W_SERIES_NEXT = 100.0
W_SIMS_VOTE = 12.0
W_AUTHOR = 9.0
W_NARRATOR = 4.0
W_GENRE = 2.0
W_RECENT = 1.5
# Text similarity is scaled to sit alongside the others: cosine returns 0..1, and
# a strong thematic match should carry about as much as a shared author.
W_TEXT = 45.0

# Guardrails against one prolific author, narrator, or similarity cluster
# overwhelming every other signal. Affinity still grows with repeated positive
# evidence, but only until it is established.
MAX_AFFINITY_WEIGHT = 3.0
MAX_SIMILARITY_WEIGHT = 3.0

# Position one in Audible's neighbour list is evidence; position ten is weaker
# evidence. The old flat vote treated them as identical.
SIMILARITY_RANK_OFFSET = 1

# Only a finished volume advances a series. A partial listen remains the current
# book and blocks later volumes rather than making all of them look like "next".
SERIES_COMPLETION_PROGRESS = 0.9

# Explanations describe stable signals, not the TF-IDF model's incidental
# vocabulary. Weak lexical overlap affects ordering but does not earn a claim.
TEXT_REASON_THRESHOLD = 0.12
MAX_REASONS = 2

# Multipliers in the greedy diversity penalty. Repetition has to earn its place
# with a stronger base score; it is not forbidden, which would trade a relevant
# shelf for forty merely different books.
SERIES_REPEAT_PENALTY = 0.8
AUTHOR_REPEAT_PENALTY = 0.35

RANKER_VERSION = "2"

GENERIC_GENRE_REASONS = {
    "audiobooks",
    "children s audiobooks",
    "fiction",
    "literature fiction",
    "science fiction fantasy",
    "teen young adult",
}

EDITION_TITLE_SUFFIXES = {
    "abridged",
    "abridged edition",
    "an audiobook",
    "audio edition",
    "audiobook",
    "the audiobook",
    "unabridged",
    "unabridged edition",
}

# Rating -> seed weight. Jellyfin's scale is 0-10.
#
# Unrated-but-finished stays weakly positive rather than dropping to zero: a
# listener who has rated five books has still finished forty, and letting the
# unrated fall out of the seed set the moment the first rating lands would make
# every shelf lurch for no visible reason.
NEUTRAL_WEIGHT = 0.35
_RATING_WEIGHTS = (
    (9.0, 1.5),    # 9-10  loved it
    (7.0, 1.0),    # 7-8   liked it
    (6.0, 0.4),    # 6     mild
    (5.0, 0.0),    # 5     indifferent, contributes nothing
    (3.0, -0.7),   # 3-4   disliked
    (0.0, -1.2),   # 0-2   actively bad
)

# A book added to the library in the last this-many days gets a nudge -- new
# arrivals are usually the ones the listener actually meant to get to.
RECENT_DAYS = 90


def _similarity_vote(seed_weight: float, position: int) -> float:
    """One seed's vote, discounted by its one-based Audible result position."""
    return seed_weight / math.log2(position + SIMILARITY_RANK_OFFSET)


def _rating(item: dict, user: "jellyfin.User | None" = None) -> float | None:
    """This listener's score, or None -- including for a rating we refuse to trust.

    A known-bad rating reads as unrated everywhere: as a seed weight, in the
    ramp's rating count, and in the decision to treat the book as a seed at all.
    """
    # A question about the account's NAME: the ignore list is configured for
    # the account JELLYFIN_USER names, and the database key is an id.
    ignored = (config.IGNORED_RATING_ITEM_IDS
               if user is None or user.is_configured_user else ())
    if (item.get("Id") or "").replace("-", "").lower() in ignored:
        return None
    return (item.get("UserData") or {}).get("Rating")


def rating_blend(rating_count: int) -> float:
    """How much of the rating signal is in effect: 0.0 to 1.0.

    A ramp rather than a switch. Every weight is interpolated between "ratings
    ignored" and "ratings fully applied", so no single rating landing can reorder
    a shelf -- which was the whole point of having a floor.
    """
    if rating_count < config.MIN_RATINGS_FOR_SIGNED_MODE:
        return 0.0
    progress = rating_count - config.MIN_RATINGS_FOR_SIGNED_MODE + 1
    return min(1.0, progress / max(1, config.RATINGS_RAMP_SPAN))


def _seed_weight(item: dict, blend: float, user: "jellyfin.User | None" = None) -> float:
    """How hard one seed should pull, given its rating and the ramp.

    At blend 0 the score itself has no effect. At blend 1 the rating table applies
    in full; listening progress is applied separately.
    """
    if blend <= 0:
        return 1.0
    score = _rating(item, user)
    # The 0.0 threshold catches every valid score; the default covers a value
    # outside the server's 0-10 range rather than raising StopIteration.
    target = NEUTRAL_WEIGHT if score is None else next(
        (w for threshold, w in _RATING_WEIGHTS if score >= threshold), NEUTRAL_WEIGHT)
    return 1.0 + (target - 1.0) * blend


def _listening_progress(item: dict) -> float:
    """How much of a book was consumed, normalised to 0.0 through 1.0."""
    ud = item.get("UserData") or {}
    if ud.get("Played"):
        return 1.0
    percentage = ud.get("PlayedPercentage")
    if isinstance(percentage, (int, float)):
        return min(1.0, max(0.0, percentage / 100.0))
    position = ud.get("PlaybackPositionTicks") or 0
    runtime = item.get("RunTimeTicks") or 0
    if position > 0 and runtime > 0:
        return min(1.0, position / runtime)
    return 0.0


def _engagement_weight(
    item: dict, blend: float, user: "jellyfin.User | None" = None
) -> float:
    """Progress strength, with explicit ratings introduced by the same ramp."""
    progress = _listening_progress(item)
    if _rating(item, user) is None:
        return progress
    return progress + (1.0 - progress) * blend


def _played(item: dict) -> bool:
    return _listening_progress(item) > 0


def _is_seed(item: dict, user: "jellyfin.User | None" = None) -> bool:
    """A book that says something about this listener's taste.

    A rating keeps an unplayed book eligible; the ratings ramp decides when that
    explicit signal gains influence.
    """
    return _played(item) or _rating(item, user) is not None


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


def _norm_author(name: str) -> str:
    """`_norm`, with runs of initials joined: "R. C. Joshua" == "RC Joshua".

    `_norm` strips the punctuation but leaves the gap it made, so those two
    spellings normalise to "r c joshua" and "rc joshua" and do not match. That
    is not hypothetical: book one of Demon World Boba Shop is tagged "RC Joshua"
    in this library while books two to five are "R. C. Joshua", and since an
    author has to agree for a title match to count as owned, the one spelling
    made a book already on disk get recommended back.
    """
    joined: list[str] = []
    for part in _norm(name).split():
        if len(part) == 1 and joined and len(joined[-1]) <= 2 and joined[-1].isalpha():
            joined[-1] += part
        else:
            joined.append(part)
    return " ".join(joined)


def _matched_names(names: list[str], affinity: dict, normalise=_norm) -> list[str]:
    """Candidate spellings whose canonical form exists in an affinity map."""
    known = {normalise(name) for name, weight in affinity.items() if weight > 0}
    return [name for name in names if normalise(name) in known]


def _affinity_weight(names: list[str], affinity: dict, normalise=_norm) -> float:
    """Canonical, saturated evidence for a candidate's people or genres."""
    wanted = {normalise(name) for name in names if name}
    if not wanted:
        return 0.0
    total = sum(
        weight for name, weight in affinity.items()
        if weight > 0 and normalise(name) in wanted
    )
    return min(MAX_AFFINITY_WEIGHT, total)


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


def _matching_audible_asin(item: dict, rows: list[dict]) -> str | None:
    """Best title-and-author match in Listenarr's Audible search results."""
    title = item.get("Name") or ""
    full_title = _norm(title)
    title_keys = _title_keys(title)
    authors = {_norm_author(a) for a in _authors(item)}
    matches = []
    for position, row in enumerate(rows):
        asin = row.get("asin")
        candidate_title = row.get("title") or ""
        if not asin or not candidate_title:
            continue
        candidate_authors = {
            _norm_author((a.get("name") or "") if isinstance(a, dict) else str(a))
            for a in (row.get("authors") or [])
        }
        if authors and not (authors & candidate_authors):
            continue
        exact_title = _norm(candidate_title) == full_title
        if not exact_title and not (title_keys & _title_keys(candidate_title)):
            continue
        # With no author to corroborate an edition match, require the full title.
        if not authors and not exact_title:
            continue
        matches.append((not exact_title, position, asin))
    return min(matches)[2] if matches else None


def _seed_sims(item: dict) -> list[dict]:
    """Audible neighbours, resolving missing or dead library identifiers."""
    source_asin = _asin(item)
    source_key = source_asin or f"item:{item.get('Id') or _norm(item.get('Name') or '')}"
    tried: set[str] = set()
    if source_asin:
        tried.add(source_asin)
        products = audible.sims(source_asin)
        if products:
            return products

    cached_alias = store.get_audible_alias(source_key)
    if cached_alias == "":
        return []
    if cached_alias:
        tried.add(cached_alias)
        products = audible.sims(cached_alias)
        if products:
            return products

    title = item.get("Name") or ""
    if not title:
        return []
    queries = [title]
    authors = _authors(item)
    if authors:
        queries.append(f"{title} {authors[0]}")
    for query in queries:
        resolved = _matching_audible_asin(
            item, listenarr.audible_search(query))
        if not resolved or resolved in tried:
            continue
        tried.add(resolved)
        products = audible.sims(resolved)
        if products:
            store.put_audible_alias(source_key, resolved)
            return products
    # Negative resolution is cached for the same bounded TTL as a successful
    # alias. Otherwise every ASIN-less seed pays two catalogue searches on every
    # refresh even when Audible has no exact edition to connect it to.
    store.put_audible_alias(source_key, "")
    return []


def _owned_index(library: list[dict]) -> tuple[set[str], dict[str, set[str]]]:
    """ASINs owned, and normalised-title -> author-set for everything else.

    ASIN alone is not enough: 76% of this library carries no Audible ASIN at all,
    so an ASIN-only check would recommend three quarters of the collection back.
    """
    asins = {_asin(i) for i in library if _asin(i)}
    by_title: dict[str, set[str]] = defaultdict(set)
    for item in library:
        authors = {_norm_author(a) for a in _authors(item)}
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
    cand_authors = {_norm_author(a) for a in cand.get("authors") or []}
    for key in _title_keys(cand.get("title") or ""):
        if key not in by_title:
            continue
        owners = by_title[key]
        if not owners or (cand_authors & owners):
            return True
    return False


def _taste(seeds: list[dict], weights: dict[str, float] | None = None) -> dict:
    """Build a taste profile from this user's play history only.

    Not from library ownership: this server has six users and the collection is
    household-wide, so "we own it" is not evidence this user likes it.
    """
    genres: Counter = Counter()
    authors: Counter = Counter()
    narrators: Counter = Counter()
    for item in seeds:
        # A disliked book must not add affinity for its own author or genre, so
        # only positive weight contributes to these counters. Its influence is
        # carried by the text profile, which can go negative.
        weight = (weights or {}).get(item["Id"], 1.0)
        if weight > 0:
            for g in item.get("Genres") or []:
                genres[g] += weight
            for a in _authors(item):
                authors[a] += weight
            for n in _people(item, "Narrator"):
                narrators[n] += weight
    return {"genres": genres, "authors": authors, "narrators": narrators,
            "series": {}}


def _series_position(item: dict) -> float | None:
    """A usable positive series coordinate, or None for unnumbered membership."""
    try:
        position = float(item.get("IndexNumber"))
    except (TypeError, ValueError):
        return None
    return position if position > 0 and math.isfinite(position) else None


def _numbered_series(
    library: list[dict],
) -> dict[str, tuple[str, dict[float, list[dict]]]]:
    """Numbered library volumes grouped by canonical series and position."""
    grouped: dict[str, tuple[str, dict[float, list[dict]]]] = {}
    for item in library:
        name = item.get("SeriesName") or ""
        position = _series_position(item)
        if not name or position is None:
            continue
        key = _norm(name)
        if key not in grouped:
            grouped[key] = (name, defaultdict(list))
        grouped[key][1][position].append(item)
    return grouped


def _series_frontiers(library: list[dict]) -> dict[str, float]:
    """Highest consecutively completed known position in each series."""
    frontiers: dict[str, float] = {}
    for name, positions in _numbered_series(library).values():
        ordered = sorted(positions)
        completed_positions = [
            position for position in ordered
            if any(
                _listening_progress(item) >= SERIES_COMPLETION_PROGRESS
                for item in positions[position]
            )
        ]
        if not completed_positions:
            continue
        first_completed = completed_positions[0]
        # A real partial listen is current even when a later volume was marked
        # played. Zero progress is weaker: older imports commonly lack the play
        # state for book one while books two and three prove the sequence moved.
        if any(
            0 < _listening_progress(item) < SERIES_COMPLETION_PROGRESS
            for position in ordered if position < first_completed
            for item in positions[position]
        ):
            continue
        frontier = first_completed
        for position in [
            position for position in ordered if position > first_completed
        ]:
            completed = any(
                _listening_progress(item) >= SERIES_COMPLETION_PROGRESS
                for item in positions[position]
            )
            if position - frontier > 1 or not completed:
                break
            frontier = position
        frontiers[name] = frontier
    return frontiers


def _series_plan(
    library: list[dict], user: "jellyfin.User | None" = None
) -> tuple[set[str], set[str]]:
    """Immediate next-volume ids, and later volumes that must not flood the shelf."""
    grouped = _numbered_series(library)
    frontiers = {
        _norm(name): frontier
        for name, frontier in _series_frontiers(library).items()
    }
    next_ids: set[str] = set()
    blocked_ids: set[str] = set()
    for series_key, (_, positions) in grouped.items():
        frontier = frontiers.get(series_key)
        if frontier is None:
            first = min(positions)
            first_is_current = any(_is_seed(item, user) for item in positions[first])
            # With no listening history, only book one is a defensible entry
            # point. If it is already in progress, even that row is current and
            # every later volume waits.
            if first > 1 or first_is_current:
                blocked_ids |= {
                    item["Id"] for items in positions.values() for item in items}
            else:
                blocked_ids |= {
                    item["Id"] for position, items in positions.items()
                    if position > first for item in items}
            continue

        blocked_ids |= {
            item["Id"] for position, items in positions.items()
            if position <= frontier for item in items if not _is_seed(item, user)}
        future_positions = [position for position in positions if position > frontier]
        if not future_positions:
            continue

        next_position = min(future_positions)
        # The library may own book four while book three is still missing. Four
        # is not "next" merely because it is the nearest volume on disk.
        if next_position - frontier > 1:
            blocked_ids |= {
                item["Id"] for position in future_positions
                for item in positions[position]}
            continue
        at_next = positions[next_position]
        eligible = [item for item in at_next if not _is_seed(item, user)]
        promoted = min(
            eligible,
            key=lambda item: (_norm(item.get("Name") or ""), item.get("Id") or ""),
            default=None,
        )
        if promoted is not None:
            next_ids.add(promoted["Id"])
        blocked_ids |= {
            item["Id"] for position in future_positions for item in positions[position]
            if promoted is None or item["Id"] != promoted["Id"]
        }
    return next_ids, blocked_ids


def _created_at(item: dict) -> float:
    """Jellyfin's ISO creation time as a sortable timestamp."""
    raw = item.get("DateCreated")
    if not raw:
        return 0.0
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _recently_added(item: dict) -> bool:
    created = _created_at(item)
    return bool(created) and (
        datetime.now(timezone.utc).timestamp() - created <= RECENT_DAYS * 86400)


def _curate_reasons(reasons: list[str]) -> list[str]:
    """Keep the strongest distinct explanations and avoid a paragraph per row."""
    out: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        key = _norm(reason)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(reason)
        if len(out) == MAX_REASONS:
            break
    return out


def _work_key(row: dict) -> str:
    """Edition-tolerant identity for deduplicating one ranked surface."""
    title = _edition_title_key(row.get("title") or row.get("Name") or "")
    authors = row.get("authors")
    if authors is None:
        authors = _authors(row)
    normalised_authors = sorted({_norm_author(name) for name in authors if name})
    if title and normalised_authors:
        return f"{title}|{normalised_authors[0]}"
    identifier = row.get("asin") or row.get("id") or row.get("Id") or ""
    return f"id:{identifier}"


def _edition_title_key(title: str) -> str:
    """Normalised title with only explicit audio-edition wording removed."""
    for separator in (":", " - ", " — "):
        if separator not in title:
            continue
        head, suffix = title.rsplit(separator, 1)
        if _norm(suffix) in EDITION_TITLE_SUFFIXES:
            return _norm(head)
    normalised = _norm(title)
    for suffix in EDITION_TITLE_SUFFIXES:
        ending = f" {suffix}"
        if normalised.endswith(ending):
            return normalised[:-len(ending)].strip()
    return normalised


def _dedupe_works(rows: list[dict]) -> list[dict]:
    """Keep the strongest edition when a work appears under multiple ASINs."""
    best: dict[str, dict] = {}
    for row in rows:
        key = _work_key(row)
        current = best.get(key)
        if current is None or float(row.get("score") or 0) > float(
            current.get("score") or 0
        ):
            best[key] = row
    return list(best.values())


def _candidate_series_position(candidate: dict) -> float | None:
    raw = candidate.get("series_position")
    try:
        position = float(raw)
    except (TypeError, ValueError):
        return None
    return position if position > 0 and math.isfinite(position) else None


def _is_reading_order_candidate(candidate: dict, taste: dict) -> bool:
    """Reject a numbered sequel unless it directly follows this user's frontier."""
    position = _candidate_series_position(candidate)
    series = _norm(candidate.get("series") or "")
    if position is None or not series or position <= 1:
        return True
    frontiers = {
        _norm(name): frontier for name, frontier in taste.get("series", {}).items()
    }
    frontier = frontiers.get(series)
    return frontier is not None and 0 < position - frontier <= 1


def _diversify(rows: list[dict], limit: int) -> list[dict]:
    """Greedily penalise repetition while retaining the underlying relevance."""
    remaining = sorted(
        rows,
        key=lambda row: (
            -float(row.get("score") or 0),
            _norm(row.get("title") or ""),
            row.get("id") or row.get("asin") or "",
        ),
    )
    selected: list[dict] = []
    series_counts: Counter = Counter()
    author_counts: Counter = Counter()

    while remaining and len(selected) < limit:
        def ordering(row: dict) -> tuple:
            series = _norm(row.get("series") or "") or None
            author = next(
                (_norm_author(name) for name in row.get("authors") or [] if name),
                None,
            )
            penalty = (
                1.0
                + SERIES_REPEAT_PENALTY * series_counts[series]
                + AUTHOR_REPEAT_PENALTY * author_counts[author]
            )
            adjusted = float(row.get("score") or 0) / penalty
            return (
                -adjusted,
                -float(row.get("score") or 0),
                _norm(row.get("title") or ""),
                row.get("id") or row.get("asin") or "",
            )

        chosen = min(remaining, key=ordering)
        remaining.remove(chosen)
        selected.append(chosen)
        series = _norm(chosen.get("series") or "") or None
        author = next(
            (_norm_author(name) for name in chosen.get("authors") or [] if name),
            None,
        )
        if series:
            series_counts[series] += 1
        if author:
            author_counts[author] += 1
    return selected


def _score_owned(
    item: dict,
    taste: dict,
    votes: Counter,
    text: float,
    similarity_titles: list[str] | None = None,
) -> tuple[float, list[str]]:
    """Score an owned, unplayed book. Returns (score, human-readable reasons)."""
    score = 0.0
    why: list[str] = []

    name = item.get("SeriesName")
    if item.get("Id") in taste.get("series_next", set()):
        score += W_SERIES_NEXT
        why.append(f"next in {name}, a series you're partway through")

    asin = _asin(item)
    if asin and votes.get(asin):
        score += W_SIMS_VOTE * min(MAX_SIMILARITY_WEIGHT, votes[asin])
        titles = (similarity_titles or [])[:2]
        if titles:
            quoted = [f"“{title}”" for title in titles]
            why.append("Audible lists it alongside " + " and ".join(quoted))
        else:
            why.append("Audible lists it alongside a book you've listened to")

    item_authors = _authors(item)
    shared_authors = _matched_names(
        item_authors, taste["authors"], _norm_author)
    if shared_authors:
        score += W_AUTHOR * _affinity_weight(
            item_authors, taste["authors"], _norm_author)
        why.append("by " + ", ".join(shared_authors[:2]) + ", who you've listened to")

    item_narrators = _people(item, "Narrator")
    shared_narrators = _matched_names(item_narrators, taste["narrators"])
    if shared_narrators:
        score += W_NARRATOR * _affinity_weight(item_narrators, taste["narrators"])
        why.append("narrated by " + shared_narrators[0])

    item_genres = item.get("Genres") or []
    shared_genres = _matched_names(item_genres, taste["genres"])
    if shared_genres:
        score += W_GENRE * _affinity_weight(item_genres, taste["genres"])
        informative = [
            genre for genre in shared_genres
            if _norm(genre) not in GENERIC_GENRE_REASONS
        ]
        if informative:
            label = ", ".join(informative[:2])
            noun = "a genre" if len(informative) == 1 else "genres"
            why.append(f"in {label}, {noun} you often listen to")

    score += W_TEXT * text
    if score > 0 and _recently_added(item):
        score += W_RECENT
    return score, why


def _score_candidate(
    cand: dict, taste: dict, votes: Counter, text: float,
    similarity_titles: list[str] | None = None,
) -> tuple[float, list[str]]:
    """Score a book not on disk. Less metadata to work with than an owned book."""
    score = W_SIMS_VOTE * min(
        MAX_SIMILARITY_WEIGHT, votes.get(cand["asin"], 0))
    why: list[str] = []
    if votes.get(cand["asin"]):
        titles = (similarity_titles or [])[:2]
        if titles:
            quoted = [f"“{title}”" for title in titles]
            why.append("Audible recommends it alongside " + " and ".join(quoted))
        else:
            why.append("Audible recommends it alongside a book you've listened to")
    if cand.get("found_by"):
        why.append(f"found searching \u201c{cand['found_by']}\u201d")

    candidate_authors = cand.get("authors") or []
    shared = _matched_names(candidate_authors, taste["authors"], _norm_author)
    if shared:
        score += W_AUTHOR * _affinity_weight(
            candidate_authors, taste["authors"], _norm_author)
        why.append("by " + ", ".join(shared[:2]) + ", who you've listened to")

    candidate_narrators = cand.get("narrators") or []
    shared_n = _matched_names(candidate_narrators, taste["narrators"])
    if shared_n:
        score += W_NARRATOR * _affinity_weight(
            candidate_narrators, taste["narrators"])
        why.append("narrated by " + shared_n[0])

    score += W_TEXT * text
    return score, why


def _candidate_description(asin: str) -> str:
    """Blurb for a keyword hit, via the cached Audible product lookup."""
    product = audible.product(asin) or {}
    return (product.get("publisher_summary")
            or product.get("merchandising_summary") or "").strip()


def keyword_queries(profile: dict[str, float]) -> list[str]:
    """Search terms drawn from the taste profile's most distinctive vocabulary.

    NOT from Audible's genre tags. Those are useless here, measured: this
    listener's most common tags are "Science Fiction & Fantasy" and -- via the
    full-cast Harry Potter editions -- "Children's Audiobooks", which returned
    The Gruffalo and Cinderella. The TF-IDF profile instead surfaces the terms
    that actually separate these books from the rest of the corpus.
    """
    ranked = sorted(profile.items(), key=lambda kv: -kv[1])
    terms = [t for t, w in ranked if w > 0 and len(t) > 4]
    # Phrases, not single words. One generic term ("grief") returns whatever is
    # popular for that word; three of the profile's distinctive terms together
    # behave like a search a person would actually type.
    return [" ".join(terms[i:i + 3]) for i in range(0, min(len(terms), config.KEYWORD_QUERIES_MAX * 3), 3)][
        : config.KEYWORD_QUERIES_MAX]


def _keyword_candidates(queries: list[str], owned_check) -> dict[str, dict]:
    """Books found by free-text search rather than by similarity to one book.

    The only channel that can surface something with no link at all to a finished
    book. Audible's own catalogue search needs authentication; Listenarr's does
    not, so the query goes through the service already running.
    """
    if not config.KEYWORD_PULL_ENABLED:
        return {}
    found: dict[str, dict] = {}
    for query in queries:
        for row in listenarr.audible_search(query):
            asin = row.get("asin")
            if not asin:
                continue
            cand = {
                "asin": asin,
                "title": (row.get("title") or "").strip(),
                "authors": [a.get("name", "") for a in (row.get("authors") or []) if a.get("name")],
                "narrators": [n.get("name", "") for n in (row.get("narrators") or []) if n.get("name")],
                "runtime_min": row.get("lengthMinutes"),
                # Listenarr's search result carries no blurb, so the description
                # has to come from Audible directly. Without it a keyword hit has
                # an empty text vector and can only ever score on author or
                # narrator overlap -- which made the channel look worse than its
                # queries actually were.
                "description": _candidate_description(asin),
                "found_by": query,
                "source": "keyword",
            }
            if not owned_check(cand):
                found.setdefault(asin, cand)
    return found


def _playlist_name(user: jellyfin.User) -> str:
    """Keep the legacy user's playlist stable; make every other name unique."""
    if user.is_configured_user:
        return config.PLAYLIST_NAME
    return f"{config.PLAYLIST_NAME} — {user.name}"


def run(user: jellyfin.User, update_playlist: bool = True) -> dict:
    """Build one user's shelves and update only that user's playlist."""
    run_id = store.start_run(user.key)
    library = jellyfin.books(user.id)

    seeds = [i for i in library if _is_seed(i, user)]

    # Signed mode -- where a bad rating pushes rather than merely failing to pull
    # -- needs enough ratings that no single one can steer the result. Counted
    # across the whole library, not just the seeds, so the figure is the honest
    # "how many ratings exist".
    rating_count = sum(1 for i in library if _rating(i, user) is not None)
    blend = rating_blend(rating_count)
    weights = {
        i["Id"]: (_seed_weight(i, blend, user)
                  * _engagement_weight(i, blend, user))
        for i in seeds
    }
    taste = _taste(seeds, weights)
    series_next, series_blocked = _series_plan(library, user)
    taste["series"] = _series_frontiers(library)
    taste["series_next"] = series_next

    owned_asins, owned_titles = _owned_index(library)

    def owned_check(cand: dict) -> bool:
        return _already_owned(cand, owned_asins, owned_titles)

    # Audible similarity votes, seeded only from books actually listened to.
    votes: Counter = Counter()
    seed_of: dict[str, list[str]] = defaultdict(list)
    unowned: dict[str, dict] = {}
    similarity_seeds = 0

    for seed in seeds:
        # A seed with no positive weight must not promote its neighbours: a book
        # scored 2 was previously still lending every similar title a full vote.
        weight = weights[seed["Id"]]
        if weight <= 0:
            continue
        similar = _seed_sims(seed)
        if similar:
            similarity_seeds += 1
        for position, sim in enumerate(similar, start=1):
            votes[sim["asin"]] += _similarity_vote(weight, position)
            source_title = seed.get("Name") or ""
            if source_title and source_title not in seed_of[sim["asin"]]:
                seed_of[sim["asin"]].append(source_title)
            if not owned_check(sim):
                unowned.setdefault(
                    sim["asin"], {**sim, "source": "audible_sims"})

    # --- one shared vocabulary for both shelves ---
    # Owned books and candidates must be vectorised against the same idf or their
    # scores cannot be compared against one taste profile.
    # Descriptions ONLY -- titles are deliberately excluded. A title is a near
    # unique proper noun, so idf hands it the highest weight in the corpus and it
    # swamps everything: the profile's top terms came out as "potter",
    # "farmstead", "caldan" -- series names, not themes -- and the keyword channel
    # then searched Audible for them.
    corpus: dict[str, str] = {i["Id"]: (i.get("Overview") or "") for i in library}
    # Keyword candidates are not known yet -- they come from the profile this
    # corpus produces -- so they are vectorised afterwards against the same idf.
    for asin, cand in unowned.items():
        corpus[f"asin:{asin}"] = cand.get("description") or ""

    frequencies = {k: textmodel.tokenise(v) for k, v in corpus.items()}
    idf = textmodel.build_idf(frequencies)
    vectors = {k: textmodel.vectorise(c, idf) for k, c in frequencies.items()}

    profile = textmodel.taste_vector(
        [(vectors.get(i["Id"], {}), weights[i["Id"]]) for i in seeds]
    )

    # Keyword discovery runs after the profile exists, because the profile is
    # what supplies the queries. Its candidates are vectorised against the same
    # idf by re-tokenising just the new rows.
    queries = keyword_queries(profile)
    keyword_found = _keyword_candidates(queries, owned_check)
    for asin, cand in keyword_found.items():
        key = f"asin:{asin}"
        if key not in vectors:
            vectors[key] = textmodel.vectorise(
                textmodel.tokenise(cand.get("description") or ""), idf)

    def text_score(key: str) -> float:
        return textmodel.similarity(vectors.get(key, {}), profile)

    # --- own shelf: on disk, unplayed, ranked ---
    own = []
    for item in library:
        # Excludes rated-but-unplayed too: it is already a seed, and offering it
        # back as a suggestion would be nonsense.
        if _is_seed(item, user):
            continue
        if item.get("Id") in series_blocked:
            continue
        # Floored at zero on this shelf. A negative cosine is real evidence, but
        # W_TEXT is large enough that it could cancel a genuine author match and
        # then trip the `score <= 0` drop below -- silently removing a book by an
        # author they like because its blurb shares words with one they rated low.
        # The discover shelf keeps the negative, where filtering is the point.
        text = max(0.0, text_score(item["Id"]))
        asin = _asin(item)
        score, why = _score_owned(
            item, taste, votes, text, seed_of.get(asin, []) if asin else [])
        if score <= 0:
            continue
        if text >= TEXT_REASON_THRESHOLD:
            why.append("matches themes in books you've enjoyed")
        source = (
            "series" if item.get("Id") in series_next
            else "audible_sims" if asin and votes.get(asin)
            else "affinity"
        )
        reasons = _curate_reasons(why)
        if not reasons:
            # A weak text score or a catalogue-wide genre can help order a
            # supported recommendation, but neither is enough to justify a row
            # on its own. Do not fill the shelf with claims we cannot explain.
            continue
        own.append({
            "id": item["Id"],
            "title": item.get("Name") or "",
            "authors": _authors(item),
            "series": item.get("SeriesName"),
            "score": round(score, 1),
            "why": reasons,
            "source": source,
        })
    own = _diversify(_dedupe_works(own), config.MAX_SHELF)

    # With no history there is no honest taste claim to make. A recent-arrivals
    # shelf is still useful, deterministic, and clearly labelled as neutral.
    if not seeds and not own:
        recent = [
            item for item in library
            if not _is_seed(item, user) and item.get("Id") not in series_blocked]
        recent.sort(key=lambda item: (
            -_created_at(item), _norm(item.get("Name") or ""), item.get("Id") or ""))
        own = [{
            "id": item["Id"],
            "title": item.get("Name") or "",
            "authors": _authors(item),
            "series": item.get("SeriesName"),
            "score": 0.0,
            "why": ["recently added to your library"],
            "source": "recent",
        } for item in recent[: config.MAX_SHELF]]

    # --- discover shelf: not on disk ---
    suppressed = store.suppressed_asins(user.key) | listenarr.queued_asins()

    def rank(pool: dict[str, dict]) -> list[dict]:
        out = []
        for asin, cand in pool.items():
            if asin in suppressed:
                continue
            if not _is_reading_order_candidate(cand, taste):
                continue
            text = text_score(f"asin:{asin}")
            score, why = _score_candidate(
                cand, taste, votes, text, seed_of.get(asin, []))
            if score <= 0:
                continue
            if text >= TEXT_REASON_THRESHOLD:
                why.append("matches themes in books you've enjoyed")
            out.append({**cand, "score": round(score, 1),
                        "why": _curate_reasons(why),
                        "because_of": seed_of.get(asin, [])[:3]})
        out.sort(key=lambda r: -r["score"])
        return out

    # Keyword picks are capped: the channel is broad by nature and must not drown
    # the ones traceable to a specific book already listened to.
    sims_picks = _dedupe_works(rank({k: v for k, v in unowned.items()}))
    keyword_only = {k: v for k, v in keyword_found.items() if k not in unowned}
    keyword_picks = _dedupe_works(rank(keyword_only))
    # Cap the keyword channel's share, but give back any of it that goes unused --
    # reserving the room unconditionally shrank the shelf to 30 whenever the
    # channel was off, which is its default.
    keyword_cap = int(config.MAX_SHELF * config.KEYWORD_SHELF_SHARE)
    sim_work_keys = {_work_key(row) for row in sims_picks}
    keyword_picks = [
        row for row in keyword_picks if _work_key(row) not in sim_work_keys]
    keyword_selected = _diversify(keyword_picks, keyword_cap)
    sims_selected = _diversify(
        sims_picks, config.MAX_SHELF - len(keyword_selected))
    discover_pool = sims_selected + keyword_selected
    discover = _diversify(discover_pool, config.MAX_SHELF)

    store.prune_attribution()
    for surface, rows in (("owned", own), ("discover", discover)):
        for recommendation_rank, row in enumerate(rows, start=1):
            item_key = row.get("id") if surface == "owned" else row.get("asin")
            row["recommendation_id"] = (
                f"{run_id}:{surface}:{recommendation_rank}:{item_key}")
        store.record_recommendations(
            run_id, user.key, surface, rows, RANKER_VERSION)

    playlist_id = None
    playlist_name = _playlist_name(user)
    if update_playlist:
        playlist_id = jellyfin.set_playlist(
            user.id, playlist_name, [r["id"] for r in own]
        )

    log.info("run user=%s library=%d seeds=%d ratings=%d blend=%.2f "
             "own=%d unowned=%d playlist=%s",
             user.key, len(library), len(seeds), rating_count, blend,
             len(own), len(discover), playlist_id or "skipped")
    store.finish_run(run_id, len(seeds), len(own), len(discover),
                     note=(f"playlist={playlist_id or 'skipped'} ratings={rating_count} "
                           f"blend={blend:.2f} ranker={RANKER_VERSION} "
                           f"sim_seeds={similarity_seeds} "
                           f"queries={','.join(queries)}"))
    return {
        "user_name": user.name,
        # What is on disk, as ASINs and as titles-to-authors. Handed back so the
        # caller can publish it rather than list the library again to rebuild
        # the same thing; it is dropped before the shelf is cached, being far
        # larger than the shelf and derivable from one listing.
        "owned_index": (owned_asins, owned_titles),
        "seeds": len(seeds),
        "library": len(library),
        "ratings": rating_count,
        "blend": round(blend, 3),
        "run_id": run_id,
        "ranker_version": RANKER_VERSION,
        "similarity_seeds": similarity_seeds,
        "own": own,
        "discover": discover,
        "keyword_picks": sum(
            1 for row in discover if row.get("source") == "keyword"),
        "queries": queries,
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
    }
