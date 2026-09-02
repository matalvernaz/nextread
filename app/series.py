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
import math
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


def _text(value) -> str:
    """A string field out of a catalogue row, whatever actually arrived in it.

    Listenarr relays Audible's JSON as it comes, and a field that is usually a
    string is occasionally a number or null. Every field read off a row goes
    through here so a malformed row is skipped rather than raising past the
    route as a 500.
    """
    return value.strip() if isinstance(value, str) else ""


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
    if math.isnan(number) or math.isinf(number):
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
        if _text(membership.get("asin")).upper() == series_asin.upper():
            return _position(membership.get("position"))
    for membership in memberships:
        position = _position(membership.get("position"))
        if position is not None:
            return position
    return None


def _identity(position: str | None, title: str) -> str:
    """What makes two catalogue rows one book.

    The series position where there is one -- Audible lists both marketplaces'
    editions of a book as two rows at the same position -- and the title
    otherwise, because an unnumbered companion volume is listed twice the same
    way and would be asked for twice.
    """
    if position is not None:
        return f"#{position}"
    return f"title:{engine._norm(title)}"


def _tokens(text: str) -> set[str]:
    return set(engine._norm(text).split())


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
        # Folded, as every ASIN here is: the product cache is keyed by what it
        # was asked for, and Jellyfin hands back whatever spelling it was given.
        asin = (engine._asin(book) or "").upper()
        if not asin:
            continue
        product = audible.product(asin)
        if not product:
            continue
        region = product.get("_region") or config.AUDIBLE_REGION
        primary_name, _ = audible._primary_series(product)
        primary = engine._norm(primary_name or "")
        for membership in product.get("series") or []:
            if not isinstance(membership, dict):
                continue
            series_asin = _text(membership.get("asin")).upper()
            if not series_asin:
                continue
            title = engine._norm(
                _text(membership.get("title")) or _text(membership.get("name")))
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
    the torrented half of a library. Three readings of the name are tried in
    turn, each accepted only when exactly one series answers to it: the name
    as the library spells it; a series whose name contains every word of it,
    which is how "Harry Potter (Stephen Fry)" finds "Harry Potter (Narrated by
    Stephen Fry)" and "Wheel of Time" finds "The Wheel of Time"; and the name
    with its trailing qualifier dropped. Two matches at any step is a guess
    about which edition somebody meant, and a guess here acquires the wrong
    narrator's books, so it refuses rather than trying a looser reading.
    """
    full = name.strip()
    unqualified = _QUALIFIER.sub("", name).strip()
    rows_by_asin: dict[str, dict] = {}
    for query in dict.fromkeys([full, unqualified]):
        if not query:
            continue
        rows = listenarr.series_candidates(query)
        if rows is None:
            raise Unavailable("Listenarr did not answer.")
        for row in rows:
            asin = _text(row.get("asin")).upper()
            if asin and asin not in rows_by_asin:
                rows_by_asin[asin] = row
    if not rows_by_asin:
        return None

    wanted = engine._norm(full)
    wanted_tokens = _tokens(full)
    rules = [
        lambda listed: listed == wanted,
        lambda listed: bool(wanted_tokens) and wanted_tokens <= set(listed.split()),
        lambda listed: bool(unqualified) and listed == engine._norm(unqualified),
    ]
    for rule in rules:
        matches = [(asin, row) for asin, row in rows_by_asin.items()
                   if rule(engine._norm(_text(row.get("name"))))]
        if len(matches) == 1:
            asin, row = matches[0]
            return asin, (_text(row.get("region")) or config.AUDIBLE_REGION)
        if len(matches) > 1:
            log.info("series name %r matches %d Audible series; refusing to guess",
                     full, len(matches))
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

    "On order" is an unfulfilled request from anybody in the household, and
    nothing else. A book this listener hid on the Discover shelf is left out
    and said to be, not folded in with the ones on their way.
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
    # Folded to upper case, as the catalogue rows are below. Jellyfin and the
    # ledger keep whatever spelling they were handed.
    asins = {a.upper() for a in asins if isinstance(a, str)}
    # Within one series a title alone is evidence. The library's own copy can
    # carry a shorter title than Audible's listing -- "Side Jobs" against
    # "Side Jobs: Stories from the Dresden Files" -- under an author tag padded
    # with the series name, which the household-wide check rightly refuses to
    # take for the same author. Measured on the live library, 2026-09-02.
    member_titles = {
        key for book in members for key in engine._title_keys(book.get("Name") or "")
    }
    ordered = {a.upper() for a in store.ordered_asins()}
    hidden = {a.upper() for a in store.dismissed_asins(user.key)}

    # Two passes. A row is judged on its own first -- owned by id or by title
    # and author, on order, or hidden -- and only then by what it shares an
    # identity with, because the two editions of one book can arrive in either
    # order and the second must not be planned for as a gap when the first
    # turns out to be owned.
    owned_keys = {
        _identity(position, "") for book in members
        if (position := _position(book.get("IndexNumber"))) is not None
    }
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        asin = _text(row.get("asin")).upper()
        if not asin or asin in seen:
            continue
        seen.add(asin)
        title = _text(row.get("title"))
        if not title:
            # Nothing to recognise it by and nothing to ask for it under.
            log.info("series row %s has no title; skipped", asin)
            continue
        position = _row_position(row, series_asin)
        candidate = {
            "asin": asin,
            "title": title,
            "authors": [_text(a.get("name")) for a in (row.get("authors") or [])
                        if isinstance(a, dict) and _text(a.get("name"))],
            "position": position,
            "key": _identity(position, title),
        }
        candidate["owned"] = (engine._already_owned(candidate, asins, by_title)
                              or bool(engine._title_keys(title) & member_titles))
        candidate["ordered"] = not candidate["owned"] and asin in ordered
        candidate["hidden"] = (not candidate["owned"] and not candidate["ordered"]
                               and asin in hidden)
        if candidate["owned"]:
            owned_keys.add(candidate["key"])
        candidates.append(candidate)
    ordered_keys = {c["key"] for c in candidates if c["ordered"]}
    hidden_keys = {c["key"] for c in candidates if c["hidden"]}

    have: list[dict] = []
    on_order: list[dict] = []
    left_out: list[dict] = []
    missing: list[dict] = []
    missing_keys: set[str] = set()
    for candidate in candidates:
        key = candidate["key"]
        if candidate["owned"] or key in owned_keys:
            have.append(candidate)
        elif candidate["ordered"] or key in ordered_keys:
            on_order.append(candidate)
        elif candidate["hidden"] or key in hidden_keys:
            left_out.append(candidate)
        elif key in missing_keys:
            # The other edition of a gap already planned for. One book, one
            # request.
            continue
        else:
            missing.append(candidate)
            missing_keys.add(key)

    log.info("series plan user=%s series=%r asin=%s region=%s listed=%d have=%d "
             "on_order=%d hidden=%d missing=%d", user.key, name, series_asin, region,
             len(seen), len(have), len(on_order), len(left_out), len(missing))
    return {
        "series": name,
        "seriesAsin": series_asin,
        "region": region,
        "have": have,
        "onOrder": on_order,
        "leftOut": left_out,
        "missing": missing,
        "rows": {_text(row.get("asin")).upper(): row
                 for row in rows if isinstance(row, dict)},
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
        except wants.AllowanceExhausted:
            # Another request on this account landed between the check above
            # and the attempt. The cap held; only the accounting is owed.
            cap_hit = True
            break
        except wants.Denied as denied:
            # One book Listenarr would not take is not a reason to stop asking
            # for the others.
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
    left_out_count = _distinct_books(planned["leftOut"])
    return {
        "series": name,
        "seriesAsin": planned["seriesAsin"],
        "ownedCount": owned_count,
        "onOrderCount": on_order_count,
        "leftOutCount": left_out_count,
        "requested": [{"asin": c["asin"], "title": c["title"]} for c in requested],
        "failed": [{"asin": c["asin"], "title": c["title"], "reason": c["reason"]}
                   for c in failed],
        "heldBackCount": held_back,
        "message": sentence(
            name, owned_count=owned_count, on_order=on_order_count,
            left_out=left_out_count,
            requested=[c["title"] for c in requested],
            failed=[c["title"] for c in failed],
            held_back=held_back, cap_hit=cap_hit, missing=len(missing)),
    }


def _distinct_books(candidates: list[dict]) -> int:
    """Books, counting the two editions Audible lists of one book once."""
    return len({c["key"] for c in candidates})


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


def sentence(name: str, *, owned_count: int, on_order: int, left_out: int,
             requested: list[str], failed: list[str], held_back: int,
             cap_hit: bool, missing: int) -> str:
    """What to say about the outcome, in full, because the row that would have
    carried it is on another screen and the tap has nothing else to show for
    itself."""
    parts: list[str] = []
    if not missing:
        if not on_order and not left_out:
            return (f"You already have every book Audible lists in {name}: "
                    f"{_plural(owned_count, 'book')}.")
        parts.append(f"You have {_plural(owned_count, 'book')} of {name}.")
        if on_order and not left_out:
            parts.append(
                f"The {_plural(on_order, 'book') if on_order != 1 else 'one'} you do not "
                f"have {'are' if on_order != 1 else 'is'} already being looked for.")
        elif on_order:
            parts.append(f"Another {_plural(on_order, 'book')} "
                         f"{'is' if on_order == 1 else 'are'} already being looked for.")
        if left_out:
            parts.append(f"{_plural(left_out, 'book')} you hid "
                         f"{'was' if left_out == 1 else 'were'} left out.")
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
    if left_out:
        parts.append(f"{_plural(left_out, 'book')} you hid "
                     f"{'was' if left_out == 1 else 'were'} left out.")
    return " ".join(parts)
