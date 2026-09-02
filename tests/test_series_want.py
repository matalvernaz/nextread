"""Asking for the rest of a series: what counts as owned, what gets asked for,
and what the sentence says about it."""
import os
import sys
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("series-want")

harness.discard(DB_PATH)

from app import audible, config, jellyfin, listenarr, series, store, wants

store.init()

matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)
kadija = jellyfin.User(id="user-kadija", name="kadija")

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, wanted {want!r}")


def book(item_id, name, series_name, index, asin=None, author="Jim Butcher"):
    item = {"Id": item_id, "Name": name, "SeriesName": series_name,
            "IndexNumber": index, "People": [{"Name": author, "Type": "Author"}]}
    if asin:
        item["ProviderIds"] = {"Audible": asin}
    return item


def row(asin, title, position, series_asin, series_name, author="Jim Butcher"):
    return {"asin": asin, "title": title, "authors": [{"name": author}],
            "narrators": [{"name": "A Reader"}], "lengthMinutes": 600,
            "releaseDate": "2020-01-01",
            "series": [{"asin": series_asin, "name": series_name, "position": position}]}


HP = "Harry Potter (Full-Cast Editions)"
LIBRARY = [
    book("j1", "Storm Front", "The Dresden Files", 1, asin="B0DF01"),
    # Owned with no Audible id: recognised by title and author.
    book("j2", "Fool Moon", "The Dresden Files", 2),
    book("j4", "Summer Knight", "The Dresden Files", 4, asin="B0DF04"),
    book("h1", "Harry Potter and the Philosopher's Stone (Full-Cast Edition)", HP, 1,
         asin="B0HP01", author="J.K. Rowling"),
    book("h2", "Harry Potter and the Chamber of Secrets (Full-Cast Edition)", HP, 2,
         asin="B0HP02", author="J.K. Rowling"),
    # The torrented half of a library: a series none of whose books carries an id.
    book("d1", "Harry Potter and the Sorcerer's Stone, Book 1 (Jim Dale)",
         "Harry Potter (Jim Dale)", 1, author="J.K. Rowling"),
    book("l1", "Long Saga 1", "Long Saga", 1, asin="B0LS01", author="Saga Author"),
    book("x1", "Lone Book", None, None),
]

PRODUCTS = {
    "B0DF01": {"title": "Storm Front", "_region": "ca",
               "series": [{"title": "The Dresden Files", "sequence": "1", "asin": "SER-DF"}]},
    "B0DF04": {"title": "Summer Knight", "_region": "ca",
               "series": [{"title": "The Dresden Files", "sequence": "4", "asin": "SER-DF"}]},
    # Audible files it under a franchise label too; the numbered one wins.
    "B0HP01": {"title": "Harry Potter and the Philosopher's Stone (Full-Cast Edition)",
               "_region": "ca",
               "series": [{"title": "Wizarding World", "asin": "SER-WW"},
                          {"title": HP, "sequence": "1", "asin": "SER-HPFC"}]},
    "B0HP02": {"title": "Harry Potter and the Chamber of Secrets (Full-Cast Edition)",
               "_region": "ca",
               "series": [{"title": HP, "sequence": "2", "asin": "SER-HPFC"}]},
    "B0LS01": {"title": "Long Saga 1", "_region": "us",
               "series": [{"title": "Long Saga", "sequence": "1", "asin": "SER-LS"}]},
}

DF = "The Dresden Files"
SERIES_BOOKS = {
    "SER-DF": [
        row("B0DF01", "Storm Front", "1", "SER-DF", DF),
        row("B0DF02X", "Fool Moon", "2", "SER-DF", DF),
        # The same book from both marketplaces: one gap, not two.
        row("B0DF03", "Grave Peril", "3", "SER-DF", DF),
        row("B0DF03US", "Grave Peril", "3", "SER-DF", DF),
        row("B0DF04", "Summer Knight", "4", "SER-DF", DF),
        row("B0DF05", "Death Masks", "5", "SER-DF", DF),
        row("B0DF06", "Blood Rites", "6", "SER-DF", DF),
        row("B0DFSS", "Side Jobs", None, "SER-DF", DF),
    ],
    "SER-HPFC": [
        row("B0HP01", "Harry Potter and the Philosopher's Stone (Full-Cast Edition)", "1",
            "SER-HPFC", HP, "J.K. Rowling"),
        # The other marketplace's title for the same book, at the same position.
        row("B0HP01US", "Harry Potter and the Sorcerer's Stone (Full-Cast Edition)", "1",
            "SER-HPFC", HP, "J.K. Rowling"),
        row("B0HP02", "Harry Potter and the Chamber of Secrets (Full-Cast Edition)", "2",
            "SER-HPFC", HP, "J.K. Rowling"),
        row("B0HP02US", "Harry Potter and the Chamber of Secrets (Full-Cast Edition)", "2",
            "SER-HPFC", HP, "J.K. Rowling"),
    ],
    "SER-LS": [row(f"B0LS0{n}", f"Long Saga {n}", str(n), "SER-LS", "Long Saga",
                   "Saga Author") for n in range(1, 8)],
    "SER-HP-ONE": [
        row("B0HPUK1", "Harry Potter and the Philosopher's Stone, Book 1", "1",
            "SER-HP-ONE", "Harry Potter", "J.K. Rowling"),
    ],
}
CANDIDATES = {
    "Harry Potter": [
        {"asin": "B0716WMLJB", "name": "Harry Potter", "region": "ca"},
        {"asin": "B0711PTW7B", "name": "Harry Potter", "region": "ca"},
        {"asin": "B0FJMNG7KH", "name": HP, "region": "ca"},
    ],
}

jellyfin.books = lambda uid: [dict(item) for item in LIBRARY]
audible.product = lambda asin: PRODUCTS.get(asin)
listenarr.series_books = lambda asin, region=None: SERIES_BOOKS.get(asin)
listenarr.series_candidates = lambda name, region=None: CANDIDATES.get(name, [])

added = []
metadata_seen = []
searched = []
refused = set()


def fake_add(asin, monitored=True, metadata=None):
    metadata_seen.append(metadata)
    if asin in refused:
        return listenarr.AddResult(False, "Listenarr said no", None)
    added.append(asin)
    return listenarr.AddResult(True, "Sent to Listenarr", len(added),
                               (metadata or {}).get("title") or "", ())


listenarr.add = fake_add
listenarr.enqueue_search = lambda audiobook_id: searched.append(audiobook_id) or True


# --- the plan: owned by id, by title, by position; one gap per position ------
planned = series.plan(matt, DF, anchor_item_id="j2")
check("series resolved through a member's id", planned["seriesAsin"], "SER-DF")
check("marketplace follows the member", planned["region"], "ca")
check("owned by id, title and author, id", sorted(c["asin"] for c in planned["have"]),
      ["B0DF01", "B0DF02X", "B0DF04"])
check("one request per position, unnumbered kept",
      [c["title"] for c in planned["missing"]],
      ["Grave Peril", "Death Masks", "Blood Rites", "Side Jobs"])
check("nothing on order yet", planned["onOrder"], [])

# --- asking, uncapped -------------------------------------------------------
outcome = series.want_series(matt, DF, anchor_item_id="j1")
check("every gap asked for", [c["title"] for c in outcome["requested"]],
      ["Grave Peril", "Death Masks", "Blood Rites", "Side Jobs"])
check("the first edition of a doubled position is the one asked for",
      "B0DF03" in added and "B0DF03US" not in added, True)
check("owned count is books, not rows", outcome["ownedCount"], 3)
check("nothing held back", outcome["heldBackCount"], 0)
check("a search is queued per book", len(searched), 4)
check("metadata rides along", all(m is not None for m in metadata_seen), True)
check("metadata names the series", metadata_seen[0]["series"], DF)
check("metadata names the store", metadata_seen[0]["region"], "ca")
check("the sentence names the books",
      outcome["message"],
      "Asked for 4 books from The Dresden Files: Grave Peril, Death Masks, "
      "Blood Rites, Side Jobs.")
ledger = {r["asin"] for r in store.requests_for(matt.key)}
check("the ledger has every request", {"B0DF03", "B0DF05", "B0DF06", "B0DFSS"} <= ledger, True)

# --- asking again: everything is on order, nothing is re-added ---------------
before = len(added)
again = series.want_series(matt, DF)
check("a second tap adds nothing", len(added), before)
check("the gaps read as on order", again["onOrderCount"], 4)
check("the sentence says so", again["message"],
      "You have 3 books of The Dresden Files. The 4 books you do not have are "
      "already being looked for.")

# --- the other marketplace's edition at the same position is owned ----------
full = series.want_series(matt, HP, anchor_item_id="h1")
check("no gap where both editions exist", full["requested"], [])
check("two books, not four rows", full["ownedCount"], 2)
check("the complete sentence", full["message"],
      "You already have every book Audible lists in Harry Potter (Full-Cast "
      "Editions): 2 books.")

# --- the cap, and the per-tap limit -----------------------------------------
capped = series.want_series(kadija, "Long Saga", anchor_item_id="l1")
check("a capped account stops at its allowance",
      len(capped["requested"]), config.WANT_DAILY_CAP)
check("the rest is counted", capped["heldBackCount"], 6 - config.WANT_DAILY_CAP)
check("the sentence explains the stop",
      "today's allowance" in capped["message"], True)
check("allowance spent", wants.allowance(kadija), 0)

exhausted = series.want_series(kadija, "Long Saga")
check("nothing asked for on an exhausted allowance", exhausted["requested"], [])
check("the sentence says the allowance is gone and what is missing",
      exhausted["message"],
      "You have used today's requests. 3 books of Long Saga are still missing. "
      "Another 3 books are already being looked for.")

saved_limit = config.SERIES_WANT_LIMIT
config.SERIES_WANT_LIMIT = 2
limited = series.want_series(matt, "Long Saga")
check("the tap limit holds", len(limited["requested"]), 2)
check("what it did not get to is counted", limited["heldBackCount"], 1)
check("and the sentence says to tap again",
      limited["message"],
      "Asked for 2 books from Long Saga: Long Saga 5, Long Saga 6. 1 more book "
      "not asked for yet. Use this again for the next batch. Another 3 books "
      "are already being looked for.")
config.SERIES_WANT_LIMIT = saved_limit

# --- a book Listenarr will not take does not stop the others ----------------
refused.add("B0LS07")
partial = series.want_series(matt, "Long Saga")
check("the refused book is reported", [c["title"] for c in partial["failed"]],
      ["Long Saga 7"])
check("and the sentence says so", partial["message"],
      "Could not ask for Long Saga 7. Another 5 books are already being looked for.")
refused.clear()

# --- resolving by name, only when it is unambiguous -------------------------
try:
    series.plan(matt, "Harry Potter (Jim Dale)")
except series.Unresolvable as exc:
    check("an ambiguous name refuses rather than guesses",
          "Could not tell which Audible series" in str(exc), True)
else:
    failures.append("two Audible series called Harry Potter must not be guessed between")

CANDIDATES["Harry Potter"] = [
    {"asin": "SER-HP-ONE", "name": "Harry Potter", "region": "ca"},
    {"asin": "B0FJMNG7KH", "name": HP, "region": "ca"},
]
named = series.plan(matt, "Harry Potter (Jim Dale)", anchor_item_id="d1")
check("a unique name resolves once the qualifier is dropped",
      named["seriesAsin"], "SER-HP-ONE")
check("the one book at position 1 is owned by position", named["missing"], [])

# --- what is not a series here ----------------------------------------------
for label, name, anchor in [("unknown name", "Nothing Like This", None),
                            ("anchor from another series", DF, "h1"),
                            ("a book with no series", "Lone Book", "x1")]:
    try:
        series.plan(matt, name, anchor_item_id=anchor)
    except series.NotASeries:
        pass
    else:
        failures.append(f"{label} must be refused as not a series")

# --- Listenarr not answering is an outage, not an answer --------------------
listenarr.series_books = lambda asin, region=None: None
try:
    series.plan(matt, DF)
except series.Unavailable:
    pass
else:
    failures.append("no answer from Listenarr must read as unavailable")
listenarr.series_books = lambda asin, region=None: []
try:
    series.plan(matt, DF)
except series.Unresolvable as exc:
    check("an empty series says so", "lists no books" in str(exc), True)
else:
    failures.append("an empty listing must be refused, not asked for")

# --- positions compare as numbers -------------------------------------------
check("3 and 3.0 are one position", series._position("3.0"), series._position(3))
check("a half position survives", series._position("3.5"), "3.5")
check("no position is None", series._position(""), None)

harness.discard(DB_PATH)
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("ok")
