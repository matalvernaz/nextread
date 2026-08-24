"""Text similarity over book descriptions -- the rating-driven half of the engine.

Why this exists: Audible's `/sims` takes one ASIN and returns neighbours. It
cannot consume a rating vector, so no amount of rating will make it score a
candidate against *everything* the listener has rated. And no external book graph
can stand in -- this library is largely indie, Audible-exclusive progression
fantasy, absent from Open Library and carrying no ratings anywhere else. So the
rating-driven similarity has to be computed locally, from text we already hold.

TF-IDF and cosine, in plain Python. Honest about what that is: a lexical
baseline, not semantic embeddings. It suits this corpus unusually well because
the discriminating vocabulary of the genre is literal -- "cultivation",
"dungeon", "system", "harem", "necromancer" -- and those are exactly the terms
IDF rewards. `vectorise` is the single seam to swap for real embeddings later.
"""
import math
import re
from collections import Counter

# Words too common to carry signal, plus the boilerplate that infests publisher
# blurbs ("bestselling", "gripping") and would otherwise link unrelated books.
_STOPWORDS = frozenset("""
a about above after again against all am an and any are as at be because been
before being below between both but by can cannot could did do does doing down
during each few for from further had has have having he her here hers herself him
himself his how i if in into is it its itself me more most my myself no nor not
of off on once only or other ought our ours ourselves out over own same she
should so some such than that the their theirs them themselves then there these
they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours yourself yourselves
audiobook audible narrator narrated listen listener edition unabridged series
book books novel story new york times bestselling bestseller gripping stunning
acclaimed author authors read reader one two three first second next also
accompanying purchase reissue whispersync includes included bonus content
previously published release released available now get free trial ebook print
paperback hardcover kindle amazon com www http https note please may might must
volume part chapter prologue epilogue
""".split())

_TOKEN = re.compile(r"[a-z][a-z'\-]{2,}")

# A term appearing in nearly every document discriminates nothing; one appearing
# once is noise or a typo. Both ends are trimmed.
MAX_DOC_FREQUENCY = 0.35
MIN_DOC_COUNT = 2


def tokenise(text: str) -> Counter:
    """Term frequencies for one document: unigrams plus adjacent bigrams.

    Bigrams are what make this work on genre fiction. "system", "dungeon" and
    "slice" are each generic to the point of uselessness; "system apocalypse",
    "dungeon core" and "slice of life" are the actual fingerprints. They are
    formed from the pre-stopword stream, and short function words never reach it
    at all (the token pattern needs three characters), so "slice of life" bridges
    to `slice_life` rather than being lost.
    """
    words = _TOKEN.findall((text or "").lower())
    kept = [w for w in words if w not in _STOPWORDS]
    counts = Counter(kept)
    for a, b in zip(words, words[1:]):
        # Either half being a stopword kills the pair. Skipping only when BOTH
        # were stopwords let through "the_golden" and "that_brings", which carry
        # no more signal than "golden" and "brings" alone while diluting the
        # vector. "slice of life" is unaffected: "of" is under the token
        # pattern's three-character minimum, so it never enters this stream.
        if a in _STOPWORDS or b in _STOPWORDS:
            continue
        counts[f"{a}_{b}"] += 1
    return counts


def build_idf(term_frequencies: dict[str, Counter]) -> dict[str, float]:
    """Inverse document frequency across the whole corpus.

    Owned books and unowned candidates are vectorised against ONE shared idf, or
    their scores would not be comparable and the two shelves could not be ranked
    against the same taste profile.
    """
    total = max(1, len(term_frequencies))
    doc_count: Counter = Counter()
    for counts in term_frequencies.values():
        doc_count.update(counts.keys())
    # The ceiling must never fall below the floor. On a small corpus
    # total * MAX_DOC_FREQUENCY goes under MIN_DOC_COUNT, every term fails both
    # tests at once, and the model returns empty vectors that score zero against
    # everything -- silently, with no error anywhere.
    ceiling = max(float(MIN_DOC_COUNT), total * MAX_DOC_FREQUENCY)
    return {
        term: math.log(total / n)
        for term, n in doc_count.items()
        if MIN_DOC_COUNT <= n <= ceiling
    }


def vectorise(counts: Counter, idf: dict[str, float]) -> dict[str, float]:
    """One L2-normalised sparse TF-IDF vector.

    Normalising here is what lets `similarity` be a plain dot product, and stops
    a long blurb outranking a short one on length alone.
    """
    raw = {
        term: (1.0 + math.log(n)) * idf[term]
        for term, n in counts.items()
        if term in idf
    }
    norm = math.sqrt(sum(v * v for v in raw.values()))
    if norm == 0:
        return {}
    return {term: v / norm for term, v in raw.items()}


def similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity of two normalised sparse vectors."""
    if not a or not b:
        return 0.0
    # Iterate the shorter side; the vectors are sparse and often lopsided.
    if len(b) < len(a):
        a, b = b, a
    return sum(weight * b.get(term, 0.0) for term, weight in a.items())


def taste_vector(weighted: list[tuple[dict[str, float], float]]) -> dict[str, float]:
    """A single signed profile vector from rated books and their weights.

    Negative weights are the point: a book the listener disliked pushes its own
    vocabulary DOWN, so the profile encodes what to avoid as well as what to
    chase. Without that, finishing a book you resented reads identically to
    loving it.
    """
    profile: dict[str, float] = {}
    for vector, weight in weighted:
        if weight == 0:
            continue
        for term, value in vector.items():
            profile[term] = profile.get(term, 0.0) + value * weight
    norm = math.sqrt(sum(v * v for v in profile.values()))
    if norm == 0:
        return {}
    return {term: v / norm for term, v in profile.items()}


def describing_terms(
    vector: dict[str, float], profile: dict[str, float], limit: int = 4
) -> list[str]:
    """The terms doing most of the work in one match, for the "why" line.

    A score with no explanation is not arguable, and every other signal on the
    shelf states its reasoning.
    """
    shared = [
        (term, weight * profile[term])
        for term, weight in vector.items()
        if profile.get(term, 0.0) > 0
    ]
    shared.sort(key=lambda pair: -pair[1])
    return [term for term, _ in shared[:limit]]
