"""The request path: the daily allowance, repeat taps, and state derivation."""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("JELLYFIN_TOKEN", "test-token")
os.environ.setdefault("DB_PATH", "/tmp/nextread-test-wants.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])

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


# --- suppression is global, dismissal is personal ---------------------------
assert "ASIN0" in store.suppressed_asins("someone-else"), \
    "Listenarr is shared, so one person's request suppresses it for everyone"
wants.dismiss(kadija, "PERSONAL")
assert "PERSONAL" in store.suppressed_asins(kadija.key)
assert "PERSONAL" not in store.suppressed_asins("someone-else")

os.remove(os.environ["DB_PATH"])
print("want path checks passed")
