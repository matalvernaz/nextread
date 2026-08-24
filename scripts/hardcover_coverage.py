#!/usr/bin/env python3
"""Does Hardcover actually know Matt's books?

The open question left over from the source survey. Open Library holds barely
half of this library and carries no rating counts at all; Audible has everything
but cannot consume a rating vector. Hardcover is newer and community-driven, so
it may cover indie audiobooks -- but it needs a key, so it could not be tested.

Run:
    HARDCOVER_TOKEN=... python3 scripts/hardcover_coverage.py

Get a token from hardcover.app -> account settings -> Hardcover API -> New API Key.
Nothing is written; this only reads.
"""
import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://api.hardcover.app/v1/graphql"

# Deliberately mixed. The mainstream titles prove the harness works; the indie
# and Audible-first ones are the actual question.
TITLES = [
    ("The Goblin Emperor", "mainstream"),
    ("Children of Time", "mainstream"),
    ("The Long Way to a Small, Angry Planet", "mainstream"),
    ("A Crucible of Souls", "indie-ish"),
    ("Dark Lord of the Farmstead", "indie / Audible-first"),
    ("Soulstone Bakery", "indie / Audible-first"),
    ("Irrelevant Jack", "indie / Audible-first"),
    ("Watcher of the Void", "indie / Audible-first"),
    ("Master Class", "indie / Audible-first"),
    ("Beware of Chicken", "Royal Road origin"),
]

QUERY = """
query Search($q: String!) {
  search(query: $q, query_type: "Book", per_page: 3) {
    results
  }
}
"""


def search(token: str, title: str) -> list[dict]:
    body = json.dumps({"query": QUERY, "variables": {"q": title}}).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message", "unknown GraphQL error"))
    results = ((payload.get("data") or {}).get("search") or {}).get("results") or {}
    # Hardcover returns the raw search-engine document; hits live under `hits`.
    if isinstance(results, dict):
        return [h.get("document", h) for h in (results.get("hits") or [])]
    return []


def main() -> int:
    token = os.environ.get("HARDCOVER_TOKEN")
    if not token:
        print("No token set. Get one from hardcover.app -> settings -> Hardcover API,")
        print("then re-run with:  HARDCOVER_TOKEN=... python3 scripts/hardcover_coverage.py")
        return 0

    found = rated = 0
    print(f"{'title':<40} {'kind':<22} found  ratings")
    print("-" * 82)
    for title, kind in TITLES:
        try:
            hits = search(token, title)
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as exc:
            print(f"{title[:40]:<40} {kind:<22} ERROR  {exc}")
            continue
        top = hits[0] if hits else None
        # A loose containment check: search engines fuzzy-match hard, and a hit on
        # an unrelated book would otherwise be counted as coverage.
        hit = bool(top) and title.split()[0].lower() in (top.get("title") or "").lower()
        count = (top or {}).get("ratings_count") or (top or {}).get("users_count") or 0
        found += hit
        rated += bool(hit and count)
        print(f"{title[:40]:<40} {kind:<22} {str(hit):<6} {count}")

    print("-" * 82)
    print(f"coverage: {found}/{len(TITLES)} found, {rated} of those carrying any rating count")
    print()
    if rated >= len(TITLES) * 0.6:
        print("VERDICT: worth wiring in as a second signal -- it has the books AND ratings.")
    elif found >= len(TITLES) * 0.6:
        print("VERDICT: has the books but not the ratings. Useful for metadata, not taste.")
    else:
        print("VERDICT: same gap as Open Library. Audible stays the only graph with this")
        print("         library in it, and the rating-driven half stays local.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
