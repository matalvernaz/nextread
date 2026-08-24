"""Scoring maths, on synthetic items only.

Deliberately no live Jellyfin writes: a rating cannot be cleared through the API,
so every real test rating is permanent. The POST path is verified separately; what
needs pinning here is that the arithmetic does what the comments claim.
"""
import sys, os
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
bad_id = sorted(engine.config.IGNORED_RATING_ITEM_IDS)[0]
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
