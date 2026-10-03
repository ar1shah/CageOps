"""Parse a fight page (/fight-details/<id>): the result, and the totals and per-round stats."""

from __future__ import annotations

import re
from collections import defaultdict

from bs4 import NavigableString, Tag

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.common import (
    cell_text,
    check_table,
    load_soup,
    must_find,
    optional_text,
    paragraphs,
    parse_clock,
    parse_context,
    parse_int,
    parse_of,
    parse_scheduled_rounds,
    squash,
    ufcstats_id,
)
from cageops_scraper.parsers.models import FightFighter, FightPage, FightStats

_STATUSES = {"W", "L", "D", "NC"}
_NO_STATS_NOTICE = "not currently available"

# Column shape of the two kinds of stat table (and which headers we spot-check).
TOTALS_COLUMNS = (
    10  # Fighter, KD, Sig. str., Sig. str. %, Total str., Td, Td %, Sub. att, Rev., Ctrl
)
TOTALS_KEYS = {0: "fighter", 1: "kd", 2: "sig str", -1: "ctrl"}
STRIKES_COLUMNS = 9  # Fighter, Sig. str, Sig. str. %, Head, Body, Leg, Distance, Clinch, Ground
STRIKES_KEYS = {0: "fighter", 3: "head", -1: "ground"}

# Stat table column -> the FightStats fields it fills. Cells are read by position.
_BREAKDOWN = ("head", "body", "leg", "distance", "clinch", "ground")


def parse_fight(html: str, url: str) -> FightPage:
    with parse_context(url):
        soup = load_soup(html)
        container = must_find(soup, "the fight details block", "div", class_="b-fight-details")
        event_link = must_find(
            must_find(soup, "the event title", "h2", class_="b-content__title"),
            "the event link",
            "a",
            href=True,
        )
        fighters = _parse_fighters(container)
        title = must_find(container, "the bout title", "i", class_="b-fight-details__fight-title")
        bout_title = squash(title.get_text(" "))
        labels = _parse_result_block(container)

        method = optional_text(labels.get("method"))
        if method is None:
            raise ParseError("layout changed: the result block has no method")
        time_format = optional_text(labels.get("time format"))
        scheduled_rounds = parse_scheduled_rounds(time_format)
        anomalies: list[str] = []
        if (
            scheduled_rounds is None
            and time_format is not None
            and time_format.lower() != "no time limit"
        ):
            anomalies.append(f"unrecognized_time_format:{time_format}")

        totals, rounds = _parse_stats(container, {f.ufcstats_id for f in fighters})
        anomalies += _round_sum_anomalies(totals, rounds)
        return FightPage(
            fight_id=ufcstats_id(url),
            event_id=ufcstats_id(str(event_link["href"])),
            fighters=fighters,
            bout_title_raw=bout_title,
            is_title_fight="title" in bout_title.lower(),
            method_raw=method,
            details_raw=optional_text(labels.get("details")),
            round=parse_int(labels.get("round")),
            finish_time_sec=parse_clock(labels.get("time")),
            time_format_raw=time_format,
            scheduled_rounds=scheduled_rounds,
            referee=optional_text(labels.get("referee")),
            has_round_stats=bool(rounds),
            totals=totals,
            rounds=rounds,
            anomalies=anomalies,
        )


# -- the header: who fought and how it ended ---------------------------------------------------


def _parse_fighters(container: Tag) -> tuple[FightFighter, FightFighter]:
    people = container.find_all("div", class_="b-fight-details__person")
    if len(people) != 2:
        raise ParseError(f"layout changed: expected 2 fighters, found {len(people)}")
    parsed = []
    for person in people:
        link = must_find(person, "a fighter link", "a", href=True)
        status = squash(
            must_find(
                person, "a result badge", "i", class_="b-fight-details__person-status"
            ).get_text()
        )
        if status not in _STATUSES:
            raise ParseError(f"unknown result badge {status!r}")
        parsed.append(
            FightFighter(
                ufcstats_id=ufcstats_id(str(link["href"])), name=cell_text(link), result=status
            )
        )
    return parsed[0], parsed[1]


def _parse_result_block(container: Tag) -> dict[str, str]:
    """The labelled values of the result block, keyed by lowercased label: 'method', 'round',
    'time', 'time format', 'referee', 'details'."""
    content = must_find(container, "the result block", "div", class_="b-fight-details__content")
    values: dict[str, str] = {}
    for label in content.find_all("i", class_="b-fight-details__label"):
        key = squash(label.get_text()).rstrip(":").lower()
        holder = label.parent
        value = squash(holder.get_text(" ").replace(label.get_text(), "", 1))
        if not value:  # the label sits alone; its value follows it in the paragraph (Details:)
            after = [
                str(s) if isinstance(s, NavigableString) else s.get_text(" ")
                for s in holder.next_siblings
            ]
            value = squash(" ".join(after))
        values[key] = value
    for required in ("method", "round", "time"):
        if required not in values:
            raise ParseError(f"layout changed: the result block has no {required!r}")
    return values


# -- the stat tables -----------------------------------------------------------------------------


def _parse_stats(
    container: Tag, fighter_ids: set[str]
) -> tuple[list[FightStats], list[FightStats]]:
    tables = container.find_all("table")
    if not tables:
        if _NO_STATS_NOTICE not in container.get_text().lower():
            raise ParseError("layout changed: no stat tables and no 'not available' notice")
        return [], []

    # (round or None, fighter id) -> the fields collected for that fighter in that scope
    collected: dict[tuple[int | None, str], dict[str, int | None]] = defaultdict(dict)
    for table in tables:
        kind = _table_kind(table)
        current_round: int | None = None
        for element in table.find_all(["th", "tr"]):
            if element.name == "th":
                match = re.fullmatch(r"round(\d+)", re.sub(r"\W", "", cell_text(element).lower()))
                if match:
                    current_round = int(match.group(1))
                continue
            cells = element.find_all("td")
            if not cells:
                continue  # the header row
            _read_row(kind, cells, current_round, fighter_ids, collected)

    stats = [
        FightStats(fighter_id=fid, round=rnd, **fields) for (rnd, fid), fields in collected.items()
    ]
    totals = [s for s in stats if s.round is None]
    rounds = sorted(
        (s for s in stats if s.round is not None), key=lambda s: (s.round, s.fighter_id)
    )
    return totals, rounds


def _table_kind(table: Tag) -> str:
    for kind, columns, keys in (
        ("totals", TOTALS_COLUMNS, TOTALS_KEYS),
        ("strikes", STRIKES_COLUMNS, STRIKES_KEYS),
    ):
        try:
            check_table(table, f"{kind} table", columns, keys)
            return kind
        except ParseError:
            continue
    raise ParseError(
        "layout changed: a stats table matches neither the totals nor the strikes shape"
    )


def _read_row(kind, cells, rnd, fighter_ids, collected) -> None:
    expected = TOTALS_COLUMNS if kind == "totals" else STRIKES_COLUMNS
    if len(cells) != expected:
        raise ParseError(
            f"layout changed: a {kind} row has {len(cells)} cells, expected {expected}"
        )
    ids = [ufcstats_id(str(a["href"])) for a in cells[0].find_all("a", href=True)]
    if len(ids) != 2 or not set(ids) <= fighter_ids:
        raise ParseError(f"a {kind} row names fighters {ids} that aren't on this fight")
    columns = [paragraphs(cell) for cell in cells]
    if any(len(c) != 2 for c in columns[1:]):
        raise ParseError(f"layout changed: a {kind} cell doesn't hold one value per fighter")

    for i, fighter_id in enumerate(ids):
        text = [c[i] for c in columns[1:]]  # this fighter's cell text, columns 1..n
        fields = collected[(rnd, fighter_id)]
        if kind == "totals":
            fields["knockdowns"] = parse_int(text[0])
            fields["sig_strikes_landed"], fields["sig_strikes_att"] = parse_of(text[1])
            # text[2] is Sig. str. % (derivable, not stored)
            fields["total_strikes_landed"], fields["total_strikes_att"] = parse_of(text[3])
            fields["takedowns_landed"], fields["takedowns_att"] = parse_of(text[4])
            # text[5] is Td % (derivable, not stored)
            fields["submission_att"] = parse_int(text[6])
            fields["reversals"] = parse_int(text[7])
            fields["ctrl_sec"] = parse_clock(text[8])
        else:
            # text[0] Sig. str and text[1] Sig. str. % repeat the totals table
            for name, cell in zip(_BREAKDOWN, text[2:], strict=True):
                fields[f"{name}_landed"], fields[f"{name}_att"] = parse_of(cell)


def _round_sum_anomalies(totals: list[FightStats], rounds: list[FightStats]) -> list[str]:
    """Per-round significant strikes should add up to the fight total (the same invariant the seed
    loader reports). A mismatch is recorded, not fatal: it may be the site's own rounding."""
    found = []
    for total in totals:
        landed = [r.sig_strikes_landed for r in rounds if r.fighter_id == total.fighter_id]
        if not landed or total.sig_strikes_landed is None or None in landed:
            continue  # nothing to compare
        if sum(landed) != total.sig_strikes_landed:
            found.append(f"round_sig_strikes_sum_mismatch:fighter={total.fighter_id}")
    return found
