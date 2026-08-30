"""Scoring maths, on synthetic items only.

Deliberately no live Jellyfin writes: a rating cannot be cleared through the API,
so every real test rating is permanent. The POST path is verified separately; what
needs pinning here is that the arithmetic does what the comments claim.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine, textmodel

FAILURES = []


def check(label, got, expected):
    ok = got == expected
    print(("  PASS  " if ok else "  FAIL  ") + f"{label}: got {got!r}, expected {expected!r}")
    if not ok:
        FAILURES.append(label)


def check_true(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + f"{label} {detail}")
    if not cond:
        FAILURES.append(label)


def item(iid, rating=None, played=True, name="", overview=""):
    ud = {"Played": played, "PlaybackPositionTicks": 0}
    if rating is not None:
        ud["Rating"] = rating
    return {"Id": iid, "Name": name, "Overview": overview, "UserData": ud,
            "Genres": [], "People": []}


print("--- seed weights, unsigned mode (below the floor) ---")
check("a 10 weighs the same as a 1", engine._seed_weight(item("a", 10), 0.0),
      engine._seed_weight(item("b", 1), 0.0))
check("unrated weighs the same too", engine._seed_weight(item("c"), 0.0), 1.0)

print("--- the ramp: no single rating may reorder a shelf ---")
# The bug this replaced: a hard gate meant that at the threshold every unrated
# seed dropped from parity with a rated one to NEUTRAL_WEIGHT in one pass.
below = engine.rating_blend(engine.config.MIN_RATINGS_FOR_SIGNED_MODE - 1)
at = engine.rating_blend(engine.config.MIN_RATINGS_FOR_SIGNED_MODE)
check("below the floor the ramp is fully off", below, 0.0)
check_true("at the floor it has only just begun", 0 < at < 0.2, f"({at:.3f})")
check_true("it reaches full strength eventually",
           engine.rating_blend(engine.config.MIN_RATINGS_FOR_SIGNED_MODE
                               + engine.config.RATINGS_RAMP_SPAN) == 1.0)
unrated_below = engine._seed_weight(item("u"), below)
unrated_at = engine._seed_weight(item("u"), at)
check_true("an unrated seed barely moves as the floor is crossed",
           abs(unrated_below - unrated_at) < 0.1,
           f"({unrated_below:.3f} -> {unrated_at:.3f})")
check_true("whereas the old hard switch moved it by 0.65",
           abs(1.0 - engine.NEUTRAL_WEIGHT) > 0.6)

print("--- a rating we refuse to trust reads as unrated everywhere ---")
bad_id = min(engine.config.IGNORED_RATING_ITEM_IDS)
bad = item(bad_id, 1)
check("its rating is not visible", engine._rating(bad), None)
check("so it weighs as unrated, not as a 1",
      engine._seed_weight(bad, 1.0), engine.NEUTRAL_WEIGHT)
check("and unplayed it is not a seed at all",
      engine._is_seed(item(bad_id, 1, played=False)), False)
check_true("the id is matched with dashes stripped and case ignored",
           engine._rating({"Id": bad_id.upper(), "UserData": {"Rating": 1}}) is None)

print("--- bigrams ---")
counts = textmodel.tokenise("a wholesome slice of life dungeon core story")
# The tokeniser's three-character minimum drops "of" before bigrams are formed,
# so the phrase bridges to `slice_life` -- which is the wanted key, not a loss.
check_true("'slice of life' bridges its function word", "slice_life" in counts,
           f"(got {[k for k in counts if '_' in k][:4]})")
check_true("'dungeon core' is captured", "dungeon_core" in counts)
check_true("unigrams are still there", "dungeon" in counts)

print("--- seed weights, signed mode ---")
check_true("a 9 outweighs a 7",
           engine._seed_weight(item("a", 9), 1.0) > engine._seed_weight(item("b", 7), 1.0))
check_true("a 2 is negative", engine._seed_weight(item("a", 2), 1.0) < 0)
check_true("a 5 contributes nothing", engine._seed_weight(item("a", 5), 1.0) == 0.0)
check("unrated-but-finished holds a defined neutral",
      engine._seed_weight(item("a"), 1.0), engine.NEUTRAL_WEIGHT)
check_true("that neutral is positive, so the unrated do not drop out",
           engine.NEUTRAL_WEIGHT > 0)

print("--- a rated book is a seed even with no play state ---")
check("rated but unplayed is a seed", engine._is_seed(item("a", 8, played=False)), True)
check("unrated and unplayed is not", engine._is_seed(item("b", None, played=False)), False)

print("--- partial listens contribute in proportion to engagement ---")
partial = item("partial", played=False)
partial["UserData"]["PlayedPercentage"] = 25.0
check("a quarter-listened book has quarter strength",
      engine._engagement_weight(partial, 0.0), 0.25)
finished = item("finished", played=True)
check("a completed book has full strength",
      engine._engagement_weight(finished, 0.0), 1.0)
rated_unplayed = item("rated", 9, played=False)
check("one early rating does not bypass the rating floor",
      engine._engagement_weight(rated_unplayed, 0.0), 0.0)
check("an explicit rating has full engagement once the ramp is active",
      engine._engagement_weight(rated_unplayed, 1.0), 1.0)

print("--- dead library ASINs resolve to the matching audio edition ---")
seed = item("seed", name="Dark Lord of the Farmstead: Part 3")
seed["People"] = [{"Type": "Author", "Name": "John Broadway"}]
rows = [
    {"asin": "part-four", "title": "Dark Lord of the Farmstead: Part 4",
     "authors": [{"name": "John Broadway"}]},
    {"asin": "part-three", "title": "Dark Lord of the Farmstead: Part 3",
     "authors": [{"name": "John Broadway"}]},
    {"asin": "wrong-author", "title": "Dark Lord of the Farmstead: Part 3",
     "authors": [{"name": "Someone Else"}]},
]
check("the exact volume wins over a shared subtitle-stripped key",
      engine._matching_audible_asin(seed, rows), "part-three")

print("--- ASIN-less seeds resolve through title and author ---")
asinless = item("no-asin", name="Demon World Boba Shop")
asinless["People"] = [{"Type": "Author", "Name": "RC Joshua"}]
saved_sims = engine.audible.sims
saved_search = engine.listenarr.audible_search
saved_get_alias = engine.store.get_audible_alias
saved_put_alias = engine.store.put_audible_alias
aliases = []
try:
    engine.audible.sims = lambda asin: (
        [{"asin": "NEIGHBOUR", "title": "A Neighbour"}]
        if asin == "RESOLVED" else [])
    engine.listenarr.audible_search = lambda query: [{
        "asin": "RESOLVED",
        "title": "Demon World Boba Shop",
        "authors": [{"name": "R. C. Joshua"}],
    }]
    engine.store.get_audible_alias = lambda source: None
    engine.store.put_audible_alias = lambda source, asin: aliases.append((source, asin))
    resolved_sims = engine._seed_sims(asinless)
finally:
    engine.audible.sims = saved_sims
    engine.listenarr.audible_search = saved_search
    engine.store.get_audible_alias = saved_get_alias
    engine.store.put_audible_alias = saved_put_alias
check("the missing identifier no longer drops the seed", resolved_sims[0]["asin"],
      "NEIGHBOUR")
check("the resolved edition is cached against the Jellyfin item",
      aliases, [("item:no-asin", "RESOLVED")])

print("--- series sequencing exposes one immediate next volume ---")
volume_one = item("v1", played=True, name="Book One")
volume_two = item("v2", played=False, name="Book Two")
volume_three = item("v3", played=False, name="Book Three")
for position, volume in enumerate((volume_one, volume_two, volume_three), start=1):
    volume["SeriesName"] = "One Series"
    volume["IndexNumber"] = position
next_ids, blocked_ids = engine._series_plan(
    [volume_one, volume_two, volume_three])
check("only volume two is next", next_ids, {"v2"})
check("volume three is withheld", blocked_ids, {"v3"})
volume_two["UserData"]["PlayedPercentage"] = 25.0
next_ids, blocked_ids = engine._series_plan(
    [volume_one, volume_two, volume_three])
check("a partial volume remains current", next_ids, set())
check("later volumes stay withheld while it is current", blocked_ids, {"v2", "v3"})
next_ids, blocked_ids = engine._series_plan([volume_one, volume_three])
check("a missing volume is not skipped over", next_ids, set())
check("the later owned volume stays withheld", blocked_ids, {"v3"})

volume_two["UserData"]["PlayedPercentage"] = 25.0
volume_three["UserData"]["Played"] = True
volume_four = item("v4", played=False, name="Book Four")
volume_four["SeriesName"] = "One Series"
volume_four["IndexNumber"] = 4
out_of_order = [volume_one, volume_two, volume_three, volume_four]
next_ids, blocked_ids = engine._series_plan(out_of_order)
check("a later completion does not leapfrog the current volume", next_ids, set())
check_true("everything after the partial volume waits",
           {"v2", "v3", "v4"} <= blocked_ids, blocked_ids)
check("the discover frontier also stops before the partial volume",
      engine._series_frontiers(out_of_order), {"One Series": 1.0})

stale_one = item("stale-1", played=False, name="Stale One")
proven_two = item("proven-2", played=True, name="Proven Two")
proven_three = item("proven-3", played=True, name="Proven Three")
proven_four = item("proven-4", played=False, name="Proven Four")
for position, volume in enumerate(
    (stale_one, proven_two, proven_three, proven_four), start=1
):
    volume["SeriesName"] = "Imported Series"
    volume["IndexNumber"] = position
next_ids, _ = engine._series_plan(
    [stale_one, proven_two, proven_three, proven_four])
check("zero progress does not erase later proven completion",
      next_ids, {"proven-4"})

fresh_one = item("fresh-1", played=False, name="Fresh One")
fresh_two = item("fresh-2", played=False, name="Fresh Two")
for position, volume in enumerate((fresh_one, fresh_two), start=1):
    volume["SeriesName"] = "Fresh Series"
    volume["IndexNumber"] = position
next_ids, blocked_ids = engine._series_plan([fresh_one, fresh_two])
check("an unread series makes no false next-volume claim", next_ids, set())
check("an unread series can offer book one but not book two",
      blocked_ids, {"fresh-2"})

print("--- Audible votes carry the seed's weight ---")
cand = {"asin": "candidate", "authors": [], "narrators": []}
score, why = engine._score_candidate(
    cand,
    {"authors": {}, "narrators": {}},
    {"candidate": 0.25},
    0.0,
    ["A Quarter-Listened Seed"],
)
check("a quarter-strength seed casts a quarter vote",
      score, engine.W_SIMS_VOTE * 0.25)
check_true("the reason names the actual source title",
           "A Quarter-Listened Seed" in why[0], why[0])
check("the first Audible neighbour keeps a full vote",
      engine._similarity_vote(1.0, 1), 1.0)
check_true("the tenth neighbour is weaker than the first",
           engine._similarity_vote(1.0, 10) < engine._similarity_vote(1.0, 1))

affinity_cand = {
    "asin": "affinity", "authors": ["Partial Author"],
    "narrators": ["Partial Narrator"],
}
affinity_score, _ = engine._score_candidate(
    affinity_cand,
    {"authors": {"Partial Author": 0.25},
     "narrators": {"Partial Narrator": 0.5}},
    {},
    0.0,
)
check("author and narrator bonuses retain their affinity weights",
      affinity_score, engine.W_AUTHOR * 0.25 + engine.W_NARRATOR * 0.5)

canonical_score, canonical_why = engine._score_candidate(
    {"asin": "canonical", "authors": ["RC Joshua"], "narrators": []},
    {"authors": {"R. C. Joshua": 1.0}, "narrators": {}},
    {},
    0.0,
)
check("author affinity uses the same initials normalisation as ownership",
      canonical_score, engine.W_AUTHOR)
check_true("the canonical match is explainable", "RC Joshua" in canonical_why[0])

saturated_score, _ = engine._score_candidate(
    {"asin": "saturated", "authors": ["Prolific Author"], "narrators": []},
    {"authors": {"Prolific Author": 50.0}, "narrators": {}},
    {},
    0.0,
)
check("a prolific author cannot grow without bound", saturated_score,
      engine.W_AUTHOR * engine.MAX_AFFINITY_WEIGHT)

owned = item("owned", played=False, name="Owned Candidate")
owned["ProviderIds"] = {"Audible": "OWNED"}
owned["Genres"] = ["Science Fiction & Fantasy"]
owned_score, owned_why = engine._score_owned(
    owned,
    {"authors": {}, "narrators": {},
     "genres": {"Science Fiction & Fantasy": 3}, "series_next": set()},
    {"OWNED": 1},
    0.0,
    ["A Real Source"],
)
check_true("owned Audible reasons name their source book",
           "A Real Source" in owned_why[0], owned_why[0])
check_true("a broad catalogue category is not presented as an explanation",
           all("Science Fiction" not in reason for reason in owned_why), owned_why)

print("--- diversity protects the top of a ranked shelf ---")
clustered = [
    {"asin": "a1", "title": "A1", "authors": ["Same"], "series": "Saga", "score": 10},
    {"asin": "a2", "title": "A2", "authors": ["Same"], "series": "Saga", "score": 9},
    {"asin": "a3", "title": "A3", "authors": ["Same"], "series": "Saga", "score": 8},
    {"asin": "b1", "title": "B1", "authors": ["Other"], "series": "Other", "score": 7},
]
diverse = engine._diversify(clustered, 4)
check("another series reaches the top before the cluster repeats",
      [row["asin"] for row in diverse[:2]], ["a1", "b1"])

print("--- editions and reading order are shelf invariants ---")
editions = engine._dedupe_works([
    {"asin": "CA", "title": "A Fine Book: An Audiobook", "authors": ["A. Writer"],
     "score": 8},
    {"asin": "US", "title": "A Fine Book", "authors": ["A Writer"], "score": 10},
    {"asin": "OTHER", "title": "A Fine Book", "authors": ["Someone Else"],
     "score": 9},
])
check("alternate editions consume one slot", sorted(row["asin"] for row in editions),
      ["OTHER", "US"])
separate_volumes = engine._dedupe_works([
    {"asin": "ONE", "title": "Shared Saga: Book One", "authors": ["A. Writer"],
     "score": 10},
    {"asin": "TWO", "title": "Shared Saga: Book Two", "authors": ["A. Writer"],
     "score": 9},
])
check("a shared subtitle head does not collapse distinct volumes",
      sorted(row["asin"] for row in separate_volumes), ["ONE", "TWO"])
reading_taste = {"series": {"Known Saga": 2.0}}
check("a series may start at book one",
      engine._is_reading_order_candidate(
          {"series": "Unknown Saga", "series_position": "1"}, reading_taste), True)
check("an unfamiliar sequel is rejected",
      engine._is_reading_order_candidate(
          {"series": "Unknown Saga", "series_position": "6"}, reading_taste), False)
check("the immediate next known volume is eligible",
      engine._is_reading_order_candidate(
          {"series": "Known Saga", "series_position": "3"}, reading_taste), True)
check("a future known volume still skips too far",
      engine._is_reading_order_candidate(
          {"series": "Known Saga", "series_position": "4"}, reading_taste), False)

print("--- disliked books must not lend their author any affinity ---")
liked = item("liked", 9, name="Good One")
liked["People"] = [{"Type": "Author", "Name": "Wanted Author"}]
liked["Genres"] = ["Cultivation"]
hated = item("hated", 1, name="Bad One")
hated["People"] = [{"Type": "Author", "Name": "Unwanted Author"}]
hated["Genres"] = ["Sports"]
weights = {"liked": engine._seed_weight(liked, 1.0), "hated": engine._seed_weight(hated, 1.0)}
taste = engine._taste([liked, hated], weights)
check_true("the liked author is in the profile", "Wanted Author" in taste["authors"])
check_true("the hated author is NOT", "Unwanted Author" not in taste["authors"])
check_true("the hated genre is NOT", "Sports" not in taste["genres"])

print("--- text profile: a hated book's vocabulary is pushed away ---")
docs = {
    "loved":  "dungeon cultivation qi meridians immortal sect",
    "hated":  "regency ballroom courtship duchess bonnet",
    "candA":  "dungeon cultivation sect immortal qi",
    "candB":  "regency courtship duchess ballroom bonnet",
}
freqs = {k: textmodel.tokenise(v) for k, v in docs.items()}
idf = textmodel.build_idf(freqs)
vecs = {k: textmodel.vectorise(c, idf) for k, c in freqs.items()}
profile = textmodel.taste_vector([(vecs["loved"], 1.5), (vecs["hated"], -1.2)])
a = textmodel.similarity(vecs["candA"], profile)
b = textmodel.similarity(vecs["candB"], profile)
check_true("the book like the loved one scores positive", a > 0, f"({a:.3f})")
check_true("the book like the hated one scores negative", b < 0, f"({b:.3f})")
check_true("and the first outranks the second", a > b, f"({a:.3f} > {b:.3f})")

print("--- cosine sanity ---")
check_true("identical vectors score ~1",
           abs(textmodel.similarity(vecs["candA"], vecs["candA"]) - 1.0) < 1e-9)
check("an empty vector scores 0", textmodel.similarity({}, vecs["candA"]), 0.0)

print()
if FAILURES:
    print("FAILED:", len(FAILURES), "->", FAILURES)
    sys.exit(1)
print("all scoring checks passed")
