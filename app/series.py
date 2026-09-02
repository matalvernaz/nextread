"""Filling the gaps in a series the library already holds part of.

EchoFin's series screen lists the books of one series this library has, in
reading order, and the natural question on it is "where are the others".
Listenarr can monitor a whole series, but its library is only what it has
bought, so monitoring would re-acquire every book already on the shelf -- the
architecture rule at the top of `listenarr.py` exists to stop exactly that.
This module answers the question against Jellyfin instead: which of the books
Audible files under this series are not here, and asks for only those, one at
a time, through the same path a single request takes. Every guard on that path
-- the allowance, the duplicate check, the ledger, the immediate search --
applies to each book as if it had been asked for on its own.
"""
import re

from . import audible, config, engine, jellyfin, listenarr, logs, shelves, store, wants

log = logs.get("series")


class NotASeries(LookupError):
    """Nothing in the caller's library is filed under that name."""


class Unresolvable(Exception):
    """The series is real here but cannot be matched to one Audible series."""


class Unavailable(Exception):
    """Listenarr would not answer, so nothing can be asked for."""


#: How many titles a spoken sentence names before it counts the rest.
NAMED_TITLES = 5

# A trailing qualifier a library adds to tell editions apart -- "(Jim Dale)",
# "(Full-Cast Editions)" -- which Audible's own series name may or may not
# carry. Tried as written first, then without it.
_QUALIFIER = re.compile(r"\s*\([^()]*\)\s*$")


def _same_series(item: dict, name: str) -> bool:
    """The rule a client groups by: the name, ignoring case and punctuation."""
    return engine._norm(item.get("SeriesName") or "") == engine._norm(name)


def _position(value) -> str | None:
    """A series position as a comparable string, or None when there is none.

    Jellyfin holds an integer and Audible a string that may be "3", "3.0" or
    "3.5"; comparing them as text would make book three two different books.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return text.casefold()
    return str(int(number)) if number == int(number) else str(number)


def _row_position(row: dict, series_asin: str) -> str | None:
    """The position Audible files this row at, within the series asked about.

    A book can sit in several series -- a franchise label and the numbered
    sequence -- so the membership is matched by the series' own ASIN before
    falling back to whichever one carries a number.
    """
    memberships = [m for m in (row.get("series") or []) if isinstance(m, dict)]
    for membership in memberships:
        if (membership.get("asin") or "").upper() == series_asin.upper():
            return _position(membership.get("position"))
    for membership in memberships:
        position = _position(membership.get("position"))
        if position is not None:
            return position
    return None


def _series_from_members(members: list[dict], name: str) -> tuple[str, str] | None:
    """The Audible series behind these books, from one that carries an ASIN.

    The membership whose name is the library's spelling wins; failing that,
    the one that matches once a trailing qualifier is dropped; failing that,
    the numbered series the book primarily belongs to, on the grounds that a
    book on this screen is in this series whatever Audible calls it. Returns
    (series ASIN, marketplace it was found in).
    """
    wanted = engine._norm(name)
    unqualified = engine._norm(_QUALIFIER.sub("", name))
    best: tuple[int, str, str] | None = None
    for book in members:
        asin = engine._asin(book)
        if not asin:
            continue
        product = audible.product(asin)
        if not product:
            continue
        region = product.get("_region") or config.AUDIBLE_REGION
        primary_name, _ = audible._primary_series(product)
        primary = engine._norm(primary_name or "")
        for membership in product.get("series") or []:
            series_asin = membership.get("asin")
            if not series_asin:
                continue
            title = engine._norm(membership.get("title") or membership.get("name") or "")
            if not title:
                continue
            if title == wanted:
                rank = 0
            elif title == unqualified:
                rank = 1
            elif title == primary:
                rank = 2
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, series_asin, region)
        if best is not None and best[0] == 0:
            break
    if best is None:
        return None
    return best[1], best[2]


def _series_by_name(name: str) -> tuple[str, str] | None:
    """Audible's own series search, trusted only when it is unambiguous.

    The fallback for a series none of whose books carries an Audible id --
    the torrented half of a library. Exactly one series answering to the name
    is an identification; two is a guess about which edition somebody meant,
    and a guess here acquires the wrong narrator's books, so it refuses.
    """
    for candidate in dict.fromkeys([name.strip(), _QUALIFIER.sub("", name).strip()]):
        if not candidate:
            continue
        rows = listenarr.series_candidates(candidate)
        if rows is None:
            raise Unavailable("Listenarr did not answer.")
        wanted = engine._norm(candidate)
        exact = {
            row["asin"].upper(): row for row in rows
            if engine._norm(row.get("name") or "") == wanted
        }
        if len(exact) == 1:
            row = next(iter(exact.values()))
            return row["asin"], (row.get("region") or config.AUDIBLE_REGION)
        if len(exact) > 1:
            log.info("series name %r matches %d Audible series; refusing to guess",
                     candidate, len(exact))
            return None
    return None


def plan(user: jellyfin.User, name: str, anchor_item_id: str | None = None) -> dict:
    """What asking for the rest of a series would do, decided without doing it.

    Owned is judged three ways, and the third is the one that matters: by
    ASIN, by title and author as everywhere else, and by *position*. Audible
    files both marketplaces' editions of one book at the same position --
    the Philosopher's and the Sorcerer's Stone are two rows, both book one --
    so without the position rule a library holding every book of the series
    would be asked to acquire all of them again under their other titles.
    """
    library = jellyfin.books(user.id)
    members = [book for book in library if _same_series(book, name)]
    if not members:
        raise NotASeries(f"None of the books in your library is filed under {name}.")
    if anchor_item_id and all(book.get("Id") != anchor_item_id for book in members):
        raise NotASeries(f"That book is not filed under {name} in your library.")

    resolved = _series_from_members(members, name) or _series_by_name(name)
    if resolved is None:
        raise Unresolvable(
            f"Could not tell which Audible series {name} is. None of these books "
            "carries an Audible id that names one, and the name alone is not "
            "enough to pick an edition.")
    series_asin, region = resolved

    rows = listenarr.series_books(series_asin, region)
    if rows is None:
        raise Unavailable("Listenarr did not answer.")
    if not rows:
        raise Unresolvable(f"Audible lists no books under {name}.")

    asins, by_title = engine._owned_index(library)
    suppressed = store.suppressed_asins(user.key)
    # Two passes. A row is judged on its own first -- owned by id or by title
    # and author, or already on order -- and only then by position, because
    # the two editions of one book can arrive in either order and the second
    # must not be planned for as a gap when the first turns out to be owned.
    owned_positions = {
        position for book in members
        if (position := _position(book.get("IndexNumber"))) is not None
    }
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        asin = (row.get("asin") or "").upper()
        if not asin or asin in seen:
            continue
        seen.add(asin)
        position = _row_position(row, series_asin)
        candidate = {
            "asin": asin,
            "title": (row.get("title") or "").strip(),
            "authors": [a.get("name", "") for a in (row.get("authors") or [])
                        if isinstance(a, dict) and a.get("name")],
            "position": position,
        }
        candidate["owned"] = engine._already_owned(candidate, asins, by_title)
        candidate["ordered"] = not candidate["owned"] and asin in suppressed
        if candidate["owned"] and position is not None:
            owned_positions.add(position)
        candidates.append(candidate)
    ordered_positions = {
        c["position"] for c in candidates if c["ordered"] and c["position"] is not None
    }
    have: list[dict] = []
    on_order: list[dict] = []
    missing: list[dict] = []
    missing_positions: set[str] = set()
    for candidate in candidates:
        position = candidate["position"]
        if candidate["owned"] or (position is not None and position in owned_positions):
            have.append(candidate)
        elif candidate["ordered"] or (position is not None and position in ordered_positions):
            on_order.append(candidate)
        elif position is not None and position in missing_positions:
            # The other marketplace's edition of a gap already planned for.
            # One book, one request.
            continue
        else:
            missing.append(candidate)
            if position is not None:
                missing_positions.add(position)

    log.info("series plan user=%s series=%r asin=%s region=%s listed=%d have=%d "
             "on_order=%d missing=%d", user.key, name, series_asin, region,
             len(seen), len(have), len(on_order), len(missing))
    return {
        "series": name,
        "seriesAsin": series_asin,
        "region": region,
        "have": have,
        "onOrder": on_order,
        "missing": missing,
        "rows": {(row.get("asin") or "").upper(): row for row in rows},
    }


def want_series(user: jellyfin.User, name: str,
                anchor_item_id: str | None = None) -> dict:
    """Ask for the books of one series the library does not hold, bounded.

    Each book goes through `wants.want`, so a repeat is free, the ledger sees
    it, and Listenarr is handed the search. Bounded twice: by the tap limit,
    so one activation cannot become forty acquisitions, and for a capped
    account by what is left of the day. What was not asked for is counted,
    and the sentence says why.
    """
    planned = plan(user, name, anchor_item_id)
    missing = planned["missing"]
    limit = config.SERIES_WANT_LIMIT
    cap_hit = False
    requested: list[dict] = []
    failed: list[dict] = []
    for candidate in missing:
        if len(requested) + len(failed) >= limit:
            break
        remaining = wants.allowance(user)
        if remaining is not None and remaining <= 0:
            cap_hit = True
            break
        metadata = listenarr.metadata_from_search_row(
            planned["rows"][candidate["asin"]], region=planned["region"])
        try:
            wants.want(user, candidate["asin"], candidate["title"], metadata=metadata)
        except wants.Denied as denied:
            # One book Listenarr would not take is not a reason to stop asking
            # for the others; the allowance was checked before the attempt.
            failed.append({**candidate, "reason": str(denied)})
            continue
        requested.append(candidate)
        shelves.forget_asin(candidate["asin"])

    held_back = len(missing) - len(requested) - len(failed)
    if cap_hit and not requested and not failed:
        log.warning("series want denied user=%s series=%r reason=daily-cap",
                    user.key, name)
    log.info("series want user=%s series=%r requested=%d failed=%d held_back=%d cap_hit=%s",
             user.key, name, len(requested), len(failed), held_back, cap_hit)
    owned_count = _distinct_books(planned["have"])
    on_order_count = _distinct_books(planned["onOrder"])
    return {
        "series": name,
        "seriesAsin": planned["seriesAsin"],
        "ownedCount": owned_count,
        "onOrderCount": on_order_count,
        "requested": [{"asin": c["asin"], "title": c["title"]} for c in requested],
        "failed": [{"asin": c["asin"], "title": c["title"], "reason": c["reason"]}
                   for c in failed],
        "heldBackCount": held_back,
        "message": sentence(
            name, owned_count=owned_count, on_order=on_order_count,
            requested=[c["title"] for c in requested],
            failed=[c["title"] for c in failed],
            held_back=held_back, cap_hit=cap_hit, missing=len(missing)),
    }


def _distinct_books(have: list[dict]) -> int:
    """Books owned, counting the two editions Audible lists at one position once."""
    positions = {c["position"] for c in have if c["position"] is not None}
    unpositioned = sum(1 for c in have if c["position"] is None)
    return len(positions) + unpositioned


def _named(titles: list[str]) -> str:
    """Up to `NAMED_TITLES` titles, spoken, then a count of the rest."""
    shown = [t for t in titles[:NAMED_TITLES] if t]
    rest = len(titles) - len(titles[:NAMED_TITLES])
    text = ", ".join(shown)
    if rest > 0:
        text += f" and {rest} more"
    return text


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def sentence(name: str, *, owned_count: int, on_order: int, requested: list[str],
             failed: list[str], held_back: int, cap_hit: bool, missing: int) -> str:
    """What to say about the outcome, in full, because the row that would have
    carried it is on another screen and the tap has nothing else to show for
    itself."""
    parts: list[str] = []
    if not missing:
        if on_order:
            parts.append(
                f"You have {_plural(owned_count, 'book')} of {name}. "
                f"The {_plural(on_order, 'book') if on_order != 1 else 'one'} you do not have "
                f"{'are' if on_order != 1 else 'is'} already being looked for.")
        else:
            parts.append(f"You already have every book Audible lists in {name}: "
                         f"{_plural(owned_count, 'book')}.")
        return " ".join(parts)

    if requested:
        parts.append(f"Asked for {_plural(len(requested), 'book')} from {name}: "
                     f"{_named(requested)}.")
    if failed:
        parts.append(f"Could not ask for {_named(failed)}.")
    if held_back:
        if cap_hit:
            if requested or failed:
                parts.append(f"That is today's allowance; {_plural(held_back, 'book')} "
                             "can wait until tomorrow.")
            else:
                parts.append(f"You have used today's requests. {_plural(held_back, 'book')} "
                             f"of {name} {'is' if held_back == 1 else 'are'} still missing.")
        else:
            parts.append(f"{held_back} more {'book' if held_back == 1 else 'books'} "
                         "not asked for yet. Use this again for the next batch.")
    if on_order:
        parts.append(f"Another {_plural(on_order, 'book')} "
                     f"{'is' if on_order == 1 else 'are'} already being looked for.")
    return " ".join(parts)
