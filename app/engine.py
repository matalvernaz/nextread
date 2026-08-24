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

from . import audible, config, jellyfin, listenarr, store, textmodel

# Relative weights. Series continuation dominates on purpose: if he is five
# books into something and owns the sixth, that is the answer.
W_SERIES_NEXT = 100.0
W_SIMS_VOTE = 12.0
W_AUTHOR = 9.0
W_NARRATOR = 4.0
W_GENRE = 2.0
W_RECENT = 1.5
# Text similarity is scaled to sit alongside the others: cosine returns 0..1, and
# a strong thematic match should carry about as much as a shared author.
W_TEXT = 45.0

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
# arrivals are usually the ones he actually meant to get to.
RECENT_DAYS = 90


def _rating(item: dict) -> float | None:
    """This listener's score, or None -- including for a rating we refuse to trust.

    A known-bad rating reads as unrated everywhere: as a seed weight, in the
    ramp's rating count, and in the decision to treat the book as a seed at all.
    """
    if (item.get("Id") or "").replace("-", "").lower() in config.IGNORED_RATING_ITEM_IDS:
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


def _seed_weight(item: dict, blend: float) -> float:
    """How hard one finished book should pull, given its rating and the ramp.

    At blend 0 every finished book counts the same, so no rating steers anything.
    At blend 1 the rating table applies in full.
    """
    if blend <= 0:
        return 1.0
    score = _rating(item)
    # The 0.0 threshold catches every valid score; the default covers a value
    # outside the server's 0-10 range rather than raising StopIteration.
    target = NEUTRAL_WEIGHT if score is None else next(
        (w for threshold, w in _RATING_WEIGHTS if score >= threshold), NEUTRAL_WEIGHT)
    return 1.0 + (target - 1.0) * blend


def _played(item: dict) -> bool:
    ud = item.get("UserData") or {}
    return bool(ud.get("Played")) or (ud.get("PlaybackPositionTicks") or 0) > 0


def _is_seed(item: dict) -> bool:
    """A book that says something about this listener's taste.

    A rating counts even with no play state behind it: an explicit score is a
    stronger statement than "the file was opened", and a book rated but never
    marked played would otherwise contribute nothing at all.
    """
    return _played(item) or _rating(item) is not None


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


def _taste(seeds: list[dict], weights: dict[str, float] | None = None) -> dict:
    """Build a taste profile from HIS play history only.

    Not from library ownership: this server has six users and the collection is
    household-wide, so "we own it" is not evidence he likes it.
    """
    genres: Counter = Counter()
    authors: Counter = Counter()
    narrators: Counter = Counter()
    series: dict[str, float] = {}
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
        # Series position is tracked regardless of rating: "where am I up to"
        # is a fact, not a preference.
        name = item.get("SeriesName")
        if name:
            idx = item.get("IndexNumber") or 0
            series[name] = max(series.get(name, 0), idx)
    return {"genres": genres, "authors": authors, "narrators": narrators, "series": series}


def _score_owned(
    item: dict, taste: dict, votes: Counter, text: float
) -> tuple[float, list[str]]:
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

    score += W_TEXT * text
    return score, why


def _score_candidate(
    cand: dict, taste: dict, votes: Counter, text: float
) -> tuple[float, list[str]]:
    """Score a book not on disk. Less metadata to work with than an owned book."""
    score = W_SIMS_VOTE * votes.get(cand["asin"], 0)
    why: list[str] = []
    n = votes.get(cand["asin"], 0)
    if n:
        why.append(f"Audible lists it alongside {n} book(s) you've listened to")
    if cand.get("found_by"):
        why.append(f"found searching \u201c{cand['found_by']}\u201d")

    shared = [a for a in cand.get("authors") or [] if a in taste["authors"]]
    if shared:
        score += W_AUTHOR * len(shared)
        why.append("by " + ", ".join(shared[:2]) + ", who you've listened to")

    shared_n = [n for n in cand.get("narrators") or [] if n in taste["narrators"]]
    if shared_n:
        score += W_NARRATOR * len(shared_n)
        why.append("narrated by " + shared_n[0])

    score += W_TEXT * text
    return score, why


def _candidate_description(asin: str) -> str:
    """Blurb for a keyword hit, via the cached Audible product lookup."""
    product = audible.product(asin) or {}
    return (product.get("merchandising_summary")
            or product.get("publisher_summary") or "").strip()


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
            }
            if not owned_check(cand):
                found.setdefault(asin, cand)
    return found


def run(update_playlist: bool = True) -> dict:
    """One full recommendation pass. Returns both shelves plus run stats."""
    run_id = store.start_run()
    uid = jellyfin.user_id()
    library = jellyfin.books(uid)

    seeds = [i for i in library if _is_seed(i)]

    # Signed mode -- where a bad rating pushes rather than merely failing to pull
    # -- needs enough ratings that no single one can steer the result. Counted
    # across the whole library, not just the seeds, so the figure is the honest
    # "how many ratings exist".
    rating_count = sum(1 for i in library if _rating(i) is not None)
    blend = rating_blend(rating_count)
    weights = {i["Id"]: _seed_weight(i, blend) for i in seeds}
    taste = _taste(seeds, weights)

    owned_asins, owned_titles = _owned_index(library)

    def owned_check(cand: dict) -> bool:
        return _already_owned(cand, owned_asins, owned_titles)

    # Audible similarity votes, seeded only from books actually listened to.
    votes: Counter = Counter()
    seed_of: dict[str, list[str]] = defaultdict(list)
    unowned: dict[str, dict] = {}

    for seed in seeds:
        asin = _asin(seed)
        if not asin:
            continue
        # A seed with no positive weight must not promote its neighbours: a book
        # scored 2 was previously still lending every similar title a full vote.
        if weights[seed["Id"]] <= 0:
            continue
        for sim in audible.sims(asin):
            votes[sim["asin"]] += 1
            seed_of[sim["asin"]].append(seed.get("Name") or "")
            if not owned_check(sim):
                unowned.setdefault(sim["asin"], sim)

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
        if _is_seed(item):
            continue
        # Floored at zero on this shelf. A negative cosine is real evidence, but
        # W_TEXT is large enough that it could cancel a genuine author match and
        # then trip the `score <= 0` drop below -- silently removing a book by an
        # author he likes because its blurb shares words with one he rated low.
        # The discover shelf keeps the negative, where filtering is the point.
        text = max(0.0, text_score(item["Id"]))
        score, why = _score_owned(item, taste, votes, text)
        if score <= 0:
            continue
        if text > 0.05:
            terms = textmodel.describing_terms(vectors.get(item["Id"], {}), profile)
            if terms:
                why.append("reads like what you've enjoyed: " + ", ".join(terms))
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

    def rank(pool: dict[str, dict]) -> list[dict]:
        out = []
        for asin, cand in pool.items():
            if asin in suppressed:
                continue
            text = text_score(f"asin:{asin}")
            score, why = _score_candidate(cand, taste, votes, text)
            if score <= 0:
                continue
            if text > 0.05:
                terms = textmodel.describing_terms(vectors.get(f"asin:{asin}", {}), profile)
                if terms:
                    why.append("reads like what you've enjoyed: " + ", ".join(terms))
            out.append({**cand, "score": round(score, 1), "why": why,
                        "because_of": seed_of.get(asin, [])[:3]})
        out.sort(key=lambda r: -r["score"])
        return out

    # Keyword picks are capped: the channel is broad by nature and must not drown
    # the ones traceable to a specific book already listened to.
    sims_picks = rank({k: v for k, v in unowned.items()})
    keyword_only = {k: v for k, v in keyword_found.items() if k not in unowned}
    keyword_picks = rank(keyword_only)
    # Cap the keyword channel's share, but give back any of it that goes unused --
    # reserving the room unconditionally shrank the shelf to 30 whenever the
    # channel was off, which is its default.
    keyword_cap = int(config.MAX_SHELF * config.KEYWORD_SHELF_SHARE)
    keyword_selected = keyword_picks[:keyword_cap]
    discover = (sims_picks[: config.MAX_SHELF - len(keyword_selected)]
                + keyword_selected)
    discover.sort(key=lambda r: -r["score"])

    playlist_id = None
    if update_playlist and own:
        playlist_id = jellyfin.set_playlist(
            uid, config.PLAYLIST_NAME, [r["id"] for r in own]
        )

    store.finish_run(run_id, len(seeds), len(own), len(discover),
                     note=(f"playlist={playlist_id or 'skipped'} ratings={rating_count} "
                           f"blend={blend:.2f} queries={','.join(queries)}"))
    return {
        "seeds": len(seeds),
        "library": len(library),
        "ratings": rating_count,
        "blend": round(blend, 3),
        "own": own,
        "discover": discover,
        "keyword_picks": len(keyword_selected),
        "queries": queries,
        "playlist_id": playlist_id,
    }
