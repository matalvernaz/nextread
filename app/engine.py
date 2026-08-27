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
from collections import Counter, defaultdict

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


def _rating(item: dict, user_key: str | None = None) -> float | None:
    """This listener's score, or None -- including for a rating we refuse to trust.

    A known-bad rating reads as unrated everywhere: as a seed weight, in the
    ramp's rating count, and in the decision to treat the book as a seed at all.
    """
    user_key = (user_key or config.JELLYFIN_USER).casefold()
    ignored = (config.IGNORED_RATING_ITEM_IDS
               if user_key == config.JELLYFIN_USER.casefold() else ())
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


def _seed_weight(item: dict, blend: float, user_key: str | None = None) -> float:
    """How hard one seed should pull, given its rating and the ramp.

    At blend 0 the score itself has no effect. At blend 1 the rating table applies
    in full; listening progress is applied separately.
    """
    if blend <= 0:
        return 1.0
    score = _rating(item, user_key)
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
    item: dict, blend: float, user_key: str | None = None
) -> float:
    """Progress strength, with explicit ratings introduced by the same ramp."""
    progress = _listening_progress(item)
    if _rating(item, user_key) is None:
        return progress
    return progress + (1.0 - progress) * blend


def _played(item: dict) -> bool:
    return _listening_progress(item) > 0


def _is_seed(item: dict, user_key: str | None = None) -> bool:
    """A book that says something about this listener's taste.

    A rating keeps an unplayed book eligible; the ratings ramp decides when that
    explicit signal gains influence.
    """
    return _played(item) or _rating(item, user_key) is not None


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
    authors = {_norm(a) for a in _authors(item)}
    matches = []
    for position, row in enumerate(rows):
        asin = row.get("asin")
        candidate_title = row.get("title") or ""
        if not asin or not candidate_title:
            continue
        candidate_authors = {
            _norm((a.get("name") or "") if isinstance(a, dict) else str(a))
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
    """Audible neighbours, resolving a dead library ASIN to its audio edition."""
    source_asin = _asin(item)
    if not source_asin:
        return []
    products = audible.sims(source_asin)
    if products:
        return products

    tried = {source_asin}
    cached_alias = store.get_audible_alias(source_asin)
    if cached_alias:
        tried.add(cached_alias)
        products = audible.sims(cached_alias)
        if products:
            return products

    title = item.get("Name") or ""
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
            store.put_audible_alias(source_asin, resolved)
            return products
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
    item: dict, taste: dict, votes: Counter, text: float, similarity_sources: int = 0
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
        count = similarity_sources or 1
        noun = "book" if count == 1 else "books"
        why.append(f"Audible lists it alongside {count} {noun} you've listened to")

    shared_authors = [a for a in _authors(item) if a in taste["authors"]]
    if shared_authors:
        score += W_AUTHOR * sum(taste["authors"][a] for a in shared_authors)
        why.append("by " + ", ".join(shared_authors[:2]) + ", who you've listened to")

    shared_narrators = [n for n in _people(item, "Narrator") if n in taste["narrators"]]
    if shared_narrators:
        score += W_NARRATOR * sum(taste["narrators"][n] for n in shared_narrators)
        why.append("narrated by " + shared_narrators[0])

    shared_genres = [g for g in (item.get("Genres") or []) if g in taste["genres"]]
    if shared_genres:
        score += W_GENRE * sum(taste["genres"][g] for g in shared_genres)
        why.append(", ".join(shared_genres[:3]))

    score += W_TEXT * text
    return score, why


def _score_candidate(
    cand: dict, taste: dict, votes: Counter, text: float,
    similarity_titles: list[str] | None = None,
) -> tuple[float, list[str]]:
    """Score a book not on disk. Less metadata to work with than an owned book."""
    score = W_SIMS_VOTE * votes.get(cand["asin"], 0)
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

    shared = [a for a in cand.get("authors") or [] if a in taste["authors"]]
    if shared:
        score += W_AUTHOR * sum(taste["authors"][a] for a in shared)
        why.append("by " + ", ".join(shared[:2]) + ", who you've listened to")

    shared_n = [n for n in cand.get("narrators") or [] if n in taste["narrators"]]
    if shared_n:
        score += W_NARRATOR * sum(taste["narrators"][n] for n in shared_n)
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


def _playlist_name(user: jellyfin.User) -> str:
    """Keep the legacy user's playlist stable; make every other name unique."""
    if user.key == config.JELLYFIN_USER.casefold():
        return config.PLAYLIST_NAME
    return f"{config.PLAYLIST_NAME} — {user.name}"


def run(user: jellyfin.User, update_playlist: bool = True) -> dict:
    """Build one user's shelves and update only that user's playlist."""
    run_id = store.start_run(user.key)
    library = jellyfin.books(user.id)

    seeds = [i for i in library if _is_seed(i, user.key)]

    # Signed mode -- where a bad rating pushes rather than merely failing to pull
    # -- needs enough ratings that no single one can steer the result. Counted
    # across the whole library, not just the seeds, so the figure is the honest
    # "how many ratings exist".
    rating_count = sum(1 for i in library if _rating(i, user.key) is not None)
    blend = rating_blend(rating_count)
    weights = {
        i["Id"]: (_seed_weight(i, blend, user.key)
                  * _engagement_weight(i, blend, user.key))
        for i in seeds
    }
    taste = _taste(seeds, weights)

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
        for sim in similar:
            votes[sim["asin"]] += weight
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
        if _is_seed(item, user.key):
            continue
        # Floored at zero on this shelf. A negative cosine is real evidence, but
        # W_TEXT is large enough that it could cancel a genuine author match and
        # then trip the `score <= 0` drop below -- silently removing a book by an
        # author they like because its blurb shares words with one they rated low.
        # The discover shelf keeps the negative, where filtering is the point.
        text = max(0.0, text_score(item["Id"]))
        asin = _asin(item)
        score, why = _score_owned(
            item, taste, votes, text, len(seed_of.get(asin, [])) if asin else 0)
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
    suppressed = store.suppressed_asins(user.key) | listenarr.queued_asins()

    def rank(pool: dict[str, dict]) -> list[dict]:
        out = []
        for asin, cand in pool.items():
            if asin in suppressed:
                continue
            text = text_score(f"asin:{asin}")
            score, why = _score_candidate(
                cand, taste, votes, text, seed_of.get(asin, []))
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
    playlist_name = _playlist_name(user)
    if update_playlist and own:
        playlist_id = jellyfin.set_playlist(
            user.id, playlist_name, [r["id"] for r in own]
        )

    log.info("run user=%s library=%d seeds=%d ratings=%d blend=%.2f "
             "own=%d unowned=%d playlist=%s",
             user.key, len(library), len(seeds), rating_count, blend,
             len(own), len(discover), playlist_id or "skipped")
    store.finish_run(run_id, len(seeds), len(own), len(discover),
                     note=(f"playlist={playlist_id or 'skipped'} ratings={rating_count} "
                           f"blend={blend:.2f} sim_seeds={similarity_seeds} "
                           f"queries={','.join(queries)}"))
    return {
        "user_name": user.name,
        # Every ASIN on disk. A requested book has arrived when its ASIN turns
        # up here, which is the whole of the arrival check -- no status to poll.
        "owned_asins": owned_asins,
        "seeds": len(seeds),
        "library": len(library),
        "ratings": rating_count,
        "blend": round(blend, 3),
        "similarity_seeds": similarity_seeds,
        "own": own,
        "discover": discover,
        "keyword_picks": len(keyword_selected),
        "queries": queries,
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
    }
