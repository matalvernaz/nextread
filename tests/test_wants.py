"""The request path: the daily allowance, repeat taps, and state derivation."""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import harness

DB_PATH = harness.use("wants")

harness.discard(DB_PATH)

from app import config, jellyfin, listenarr, store, wants

store.init()

matt = jellyfin.User(id="user-matt", name="matt", is_admin=True)
kadija = jellyfin.User(id="user-kadija", name="kadija")

added = []
searched = []
listenarr.add = (
    lambda asin, monitored=True:
    added.append(asin) or listenarr.AddResult(True, "Sent to Listenarr", 42))
listenarr.enqueue_search = lambda audiobook_id: searched.append(audiobook_id) or True


# --- the allowance ----------------------------------------------------------
assert wants.allowance(matt) is None, "a keyholder is not capped"
assert wants.allowance(kadija) == config.WANT_DAILY_CAP

for n in range(config.WANT_DAILY_CAP):
    state, _ = wants.want(kadija, f"ASIN{n}", f"Book {n}")
    assert state == wants.ON_ITS_WAY, state
assert wants.allowance(kadija) == 0

try:
    wants.want(kadija, "ONE-TOO-MANY", "Over")
except wants.Denied as denied:
    assert str(config.WANT_DAILY_CAP) in str(denied)
else:
    raise AssertionError("the cap must refuse the next request")
assert "ONE-TOO-MANY" not in added, "a refused request must not reach Listenarr"

# The keyholder is unaffected by somebody else's spent allowance.
assert wants.want(matt, "KEYHOLDER-BOOK", "Fine")[0] == wants.ON_ITS_WAY


# --- repeating a request is free -------------------------------------------
before = len(added)
state, message = wants.want(kadija, "ASIN0", "Book 0")
assert state == wants.ON_ITS_WAY
assert len(added) == before, "a repeat tap must not add to Listenarr again"
assert store.requests_since(kadija.key, 0) == config.WANT_DAILY_CAP, \
    "a repeat tap must not spend another day's allowance"


# --- an immediate search is asked for, and its failure is not the user's ----
assert searched, "a successful add must queue a search"
listenarr.enqueue_search = lambda audiobook_id: False
assert wants.want(matt, "SEARCH-FAILS", "Still fine")[0] == wants.ON_ITS_WAY, \
    "a queue that refuses the job still leaves the book monitored for the sweep"


# --- states -----------------------------------------------------------------
rows = {r["asin"]: r for r in wants.states(kadija.key, (set(), {}))}
assert rows["ASIN0"]["state"] == wants.ON_ITS_WAY

# Old enough to stop claiming to be arriving.
stale = time.time() - (config.STILL_LOOKING_AFTER_HOURS * 3600) - 60
with store.db() as conn:
    conn.execute("UPDATE requests SET requested_at=? WHERE user_key=? AND asin=?",
                 (stale, kadija.key, "ASIN1"))
rows = {r["asin"]: r for r in wants.states(kadija.key, (set(), {}))}
assert rows["ASIN1"]["state"] == wants.STILL_LOOKING, rows["ASIN1"]

# Arrival by ASIN, and it sticks.
rows = {r["asin"]: r for r in wants.states(kadija.key, ({"ASIN2"}, {}))}
assert rows["ASIN2"]["state"] == wants.IN_LIBRARY
rows = {r["asin"]: r for r in wants.states(kadija.key, (set(), {}))}
assert rows["ASIN2"]["state"] == wants.IN_LIBRARY, "a fulfilled request stays fulfilled"

# A fulfilled request stops blocking a re-request; an unfulfilled one short-circuits.
assert wants.want(kadija, "ASIN1", "Book 1")[1] == "Already on its way"


# --- arrival under the other marketplace's ASIN -----------------------------
# The live failure this check exists for: "Splinter Angel: Book 1" was asked for
# as B0FMS8SNXH, the store it was found in, and imported tagged B0FMS7YS1C, the
# one the other store issues for the same edition. Under an ASIN-only test the
# request sat at "on its way" while the book played from the library.
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(
    True, "Sent to Listenarr", 43, "Splinter Angel: Book 1", ("Avaritiabona",))
listenarr.enqueue_search = lambda audiobook_id: True
wants.want(matt, "B0FMS8SNXH", "Splinter Angel: Book 1")

library = ({"B0FMS7YS1C"}, {"splinter angel book 1": {"avaritiabona"}})
rows = {r["asin"]: r for r in wants.states(matt.key, library)}
assert rows["B0FMS8SNXH"]["state"] == wants.IN_LIBRARY, rows["B0FMS8SNXH"]

# An author that disagrees is a different book with the same title, and must not
# fulfil the request.
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(
    True, "Sent to Listenarr", 44, "Splinter Angel: Book 1", ("Somebody Else",))
wants.want(matt, "B0IMPOSTOR", "Splinter Angel: Book 1")
rows = {r["asin"]: r for r in wants.states(matt.key, library)}
assert rows["B0IMPOSTOR"]["state"] != wants.IN_LIBRARY, rows["B0IMPOSTOR"]

# A row written before authors were kept has none to agree with, so the title
# decides. Without this the requests that were already stuck stay stuck.
with store.db() as conn:
    conn.execute("INSERT INTO requests(user_key,asin,title,requested_at) "
                 "VALUES(?,?,?,?)",
                 (matt.key, "B0LEGACY", "Splinter Angel: Book 1", time.time()))
rows = {r["asin"]: r for r in wants.states(matt.key, library)}
assert rows["B0LEGACY"]["state"] == wants.IN_LIBRARY, rows["B0LEGACY"]

# The subtitle-stripped form bridges an edition that carries one and one that
# does not -- the same allowance `_title_keys` makes for the owned check.
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(
    True, "Sent to Listenarr", 45, "Second Ascension: Book One", ("Reece Brooks",))
wants.want(matt, "B0GMRVTV5R", "Second Ascension: Book One")
rows = {r["asin"]: r for r in wants.states(
    matt.key, (set(), {"second ascension": {"reece brooks"}}))}
assert rows["B0GMRVTV5R"]["state"] == wants.IN_LIBRARY, rows["B0GMRVTV5R"]

# Two shapes the shelf spells differently, both live on 2026-09-04 with the book
# already playing from the library: a qualifier in parentheses that the tagger
# drops, and a series named before the colon that the tagger does not repeat.
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(
    True, "Sent to Listenarr", 46,
    "The House of Hades (Heroes of Olympus Book 4)", ("Rick Riordan",))
wants.want(matt, "B079XJZT89", "The House of Hades (Heroes of Olympus Book 4)")
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(
    True, "Sent to Listenarr", 47,
    "The Heroes of Olympus: The Demigod Diaries", ("Rick Riordan",))
wants.want(matt, "B071YTGJTR", "The Heroes of Olympus: The Demigod Diaries")
riordan = (set(), {"the house of hades": {"rick riordan"},
                   "the demigod diaries": {"rick riordan"}})
rows = {r["asin"]: r for r in wants.states(matt.key, riordan)}
assert rows["B079XJZT89"]["state"] == wants.IN_LIBRARY, rows["B079XJZT89"]
assert rows["B071YTGJTR"]["state"] == wants.IN_LIBRARY, rows["B071YTGJTR"]

# The wider keys still need the author to agree where both sides have one.
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(
    True, "Sent to Listenarr", 48, "Some Series: The Demigod Diaries", ("Not Riordan",))
wants.want(matt, "B0OTHERDD", "Some Series: The Demigod Diaries")
rows = {r["asin"]: r for r in wants.states(matt.key, riordan)}
assert rows["B0OTHERDD"]["state"] != wants.IN_LIBRARY, rows["B0OTHERDD"]

# A tail that is only a volume label is not a title to be found under.
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(
    True, "Sent to Listenarr", 49, "Nameless Saga: Book 1", ("Nobody",))
wants.want(matt, "B0VOLONLY", "Nameless Saga: Book 1")
rows = {r["asin"]: r for r in wants.states(matt.key, (set(), {"book 1": set()}))}
assert rows["B0VOLONLY"]["state"] != wants.IN_LIBRARY, rows["B0VOLONLY"]


# --- suppression is global, dismissal is personal ---------------------------
assert "ASIN0" in store.suppressed_asins("someone-else"), \
    "Listenarr is shared, so one person's request suppresses it for everyone"
wants.dismiss(kadija, "PERSONAL")
assert "PERSONAL" in store.suppressed_asins(kadija.key)
assert "PERSONAL" not in store.suppressed_asins("someone-else")

# --- cancelling a request ---------------------------------------------------
deleted = []
listenarr.find_by_asin = lambda asin: {"id": 99, "title": "Whatever"}
listenarr.delete = lambda audiobook_id: deleted.append(audiobook_id) or True
listenarr.add = lambda asin, monitored=True: listenarr.AddResult(
    True, "Sent to Listenarr", 99)

removed, message = wants.cancel(matt, "NEVER-ASKED-FOR")
assert not removed and "not on your list" in message, message
assert not deleted, "cancelling something nobody asked for must not delete anything"

wants.want(matt, "CANCEL-ME", "Regretted")
spent = store.requests_since(matt.key, 0)
removed, message = wants.cancel(matt, "CANCEL-ME")
assert removed and deleted == [99], (removed, deleted)
assert "called off" in message, message
assert not any(r["asin"] == "CANCEL-ME" for r in store.requests_for(matt.key))
assert store.requests_since(matt.key, 0) == spent - 1, \
    "a cancelled request must not go on spending the day's allowance"

# Cancelling does NOT dismiss: a book abandoned for taking too long is not a
# book somebody has stopped wanting, and the shelf may offer it again.
assert "CANCEL-ME" not in store.suppressed_asins(matt.key)

# Listenarr's row is the household's. Somebody else still waiting on the book
# keeps the acquisition running, and only this account's row goes.
deleted.clear()
wants.want(matt, "SHARED-BOOK", "Both of us")
# Recorded rather than requested: kadija spent her allowance above, and what
# `cancel` reads is the ledger row, not how it got there.
store.record_request(kadija.key, "SHARED-BOOK", "Both of us")
removed, message = wants.cancel(matt, "SHARED-BOOK")
assert removed and not deleted, (removed, deleted)
assert "still being looked for" in message, message
assert any(r["asin"] == "SHARED-BOOK" for r in store.requests_for(kadija.key)), \
    "cancelling one account's request must not touch another's"

# A Listenarr that will not answer must not strand the row on screen. The row
# goes and the message says the search could not be stopped.
listenarr.delete = lambda audiobook_id: False
wants.want(matt, "LISTENARR-DOWN", "Stuck")
removed, message = wants.cancel(matt, "LISTENARR-DOWN")
assert removed, "an unreachable Listenarr must still clear the row"
assert "may still turn up" in message, message
assert not any(r["asin"] == "LISTENARR-DOWN" for r in store.requests_for(matt.key))

harness.discard(DB_PATH)
print("want path checks passed")
