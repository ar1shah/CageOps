"""Write one Wikipedia event article into the shared tables (D-029).

The rules, in the order they bite:
- Fighters: by the article link (stable across display-name edits), then by name through the same
  NameResolver the seed uses. A name that matches nobody becomes a stub, unless it looks like an
  existing fighter spelled differently: then the whole event goes to the dead-letter queue (nothing
  is written) for a person to add an alias or confirm a new fighter, and then replay.
- Fights are identified by (event, fighter pair), never by a name. An existing fight with the same
  pair within a day of the article date is the same fight; its event is the article's event.
- Events are identified by the article id, so renaming the article changes nothing.
- Precedence: a ufcstats result is never replaced. A scheduled ufcstats bout with no result is
  filled in. A Wikipedia result is updated. This writer never cancels anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cageops_common.db.models import Fight, Fighter
from cageops_scraper.parsers.wikipedia_models import WikiEventPage, WikiFighter
from cageops_worker.ingest.errors import MappingError
from cageops_worker.ingest.mapping_wikipedia import (
    DuplicateIndex,
    MappedWikiBout,
    load_distinct_titles,
)
from cageops_worker.ingest.store import RESULT_COLUMNS
from cageops_worker.ingest.upsert import UpsertCounts, upsert
from cageops_worker.seed.resolver import ALIASES_CSV, NameResolver, load_aliases

log = logging.getLogger(__name__)
FIGHTS = Fight.__table__
FIGHTERS = Fighter.__table__
MATCH_WINDOW = timedelta(days=1)  # a source may date a card a day off (time zones, late starts)
COMPARED = ("outcome", "winner_id", "method", "decision_type", "finish_round", "finish_time_sec")


@dataclass
class WikiWriteResult:
    event: UpsertCounts = field(default_factory=UpsertCounts)
    fighters: UpsertCounts = field(default_factory=UpsertCounts)
    fights: UpsertCounts = field(default_factory=UpsertCounts)
    already_covered: int = 0  # bouts whose result ufcstats already holds
    skipped: bool = False  # every bout was already covered
    stubs: list[dict[str, str | None]] = field(default_factory=list)  # fighters we had to create
    anomalies: list[str] = field(default_factory=list)


# -- fighters -------------------------------------------------------------------------------


def _name_key(name: str) -> str:
    return name.strip().replace(" ", "_")


def _resolve_fighter(
    conn: Connection,
    resolver: NameResolver,
    index: DuplicateIndex,
    distinct: frozenset[str],
    fighter: WikiFighter,
    bout: MappedWikiBout,
    page: WikiEventPage,
    result: WikiWriteResult,
) -> int:
    link = fighter.link_title
    if link:
        known = conn.execute(
            sa.select(FIGHTERS.c.id).where(FIGHTERS.c.wikipedia_title == link)
        ).scalar_one_or_none()
        if known is not None:
            return known
    resolution = resolver.resolve(
        fighter.name, source="wikipedia", weight_class=bout.weight_class, on=page.event_date
    )
    if resolution.fighter_id is not None:
        if link:  # remember the link, so a later edit of the display name still finds them
            _remember_link(conn, resolution.fighter_id, link, result)
        return resolution.fighter_id
    where = f"{fighter.name!r} (link {link!r}) on {page.name!r}"
    if resolution.status == "ambiguous":
        raise MappingError(
            f"ambiguous fighter {where}: several fighters share this name and the date and "
            "weight class don't tell them apart; add an alias row for the right one, then replay"
        )
    key = link or _name_key(fighter.name)
    if key not in distinct:
        similar = index.similar(fighter.name)
        if similar:
            candidates = ", ".join(f"{name} (id {fid}: {why})" for fid, name, why in similar[:3])
            raise MappingError(
                f"suspect duplicate fighter {where}: no exact match, but it looks like "
                f"{candidates}. If it is one of them, add an alias row; if it is a new fighter, "
                f"add {key!r} to wikipedia_distinct_fighters.csv. Then replay"
            )
    return _create_stub(conn, fighter, key, result)


def _remember_link(conn: Connection, fighter_id: int, link: str, result: WikiWriteResult) -> None:
    updated = conn.execute(
        text(
            "UPDATE fighters SET wikipedia_title = :t WHERE id = :i AND wikipedia_title IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM fighters WHERE wikipedia_title = :t)"
        ),
        {"t": link, "i": fighter_id},
    ).rowcount
    if updated:
        result.fighters.updated += 1


def _create_stub(conn: Connection, fighter: WikiFighter, key: str, result: WikiWriteResult) -> int:
    stmt = (
        pg_insert(FIGHTERS)
        .values(name=fighter.name, wikipedia_title=key)
        .on_conflict_do_nothing(index_elements=["wikipedia_title"])
        .returning(FIGHTERS.c.id)
    )
    created = conn.execute(stmt).scalar_one_or_none()
    if created is not None:
        result.fighters.inserted += 1
        result.stubs.append({"name": fighter.name, "link_title": fighter.link_title})
        return created
    result.fighters.unchanged += 1  # another job created it a moment ago
    return conn.execute(
        sa.select(FIGHTERS.c.id).where(FIGHTERS.c.wikipedia_title == key)
    ).scalar_one()


# -- events -----------------------------------------------------------------------------------


def _existing_fights(
    conn: Connection, page: WikiEventPage, pairs: list[tuple[int, int]]
) -> dict[int, tuple[int, int]]:
    """bout index -> (fight id, event id) for bouts we already hold, near the article date."""
    found: dict[int, tuple[int, int]] = {}
    for i, (first, second) in enumerate(pairs):
        low, high = sorted((first, second))
        rows = conn.execute(
            text(
                "SELECT f.id, f.event_id FROM fights f JOIN events e ON e.id = f.event_id"
                " WHERE f.fighter_a_id = :a AND f.fighter_b_id = :b"
                " AND e.event_date BETWEEN :lo AND :hi"
            ),
            {
                "a": low,
                "b": high,
                "lo": page.event_date - MATCH_WINDOW,
                "hi": page.event_date + MATCH_WINDOW,
            },
        ).all()
        if len(rows) > 1:
            raise MappingError(f"{page.name!r}: bout {i + 1} matches {len(rows)} fights we hold")
        if rows:
            found[i] = (rows[0][0], rows[0][1])
    return found


def _decide_event(
    conn: Connection,
    page: WikiEventPage,
    matches: dict[int, tuple[int, int]],
    result: WikiWriteResult,
) -> int:
    by_article = conn.execute(
        text(
            "SELECT id, ufcstats_id, name, event_date FROM events WHERE wikipedia_article_id = :a"
        ),
        {"a": page.article_id},
    ).one_or_none()
    matched_events = {event_id for _, event_id in matches.values()}
    if by_article is not None:
        if matched_events - {by_article.id}:
            raise MappingError(
                f"{page.name!r} (article {page.article_id}): its bouts match fights in other events"
            )
        if by_article.ufcstats_id is None and (
            (by_article.name, by_article.event_date) != (page.name, page.event_date)
        ):  # we created this event: a renamed or re-dated article updates it
            conn.execute(
                text("UPDATE events SET name = :n, event_date = :d WHERE id = :i"),
                {"n": page.name, "d": page.event_date, "i": by_article.id},
            )
            result.event.updated += 1
        else:
            result.event.unchanged += 1  # a ufcstats event keeps its own name and date
        return by_article.id
    if len(matched_events) > 1:
        raise MappingError(
            f"{page.name!r}: its bouts match fights in {len(matched_events)} different events"
        )
    if matched_events:
        (event_id,) = matched_events  # a ufcstats event: attach the article to it
        attached = conn.execute(
            text(
                "UPDATE events SET wikipedia_article_id = :a WHERE id = :i"
                " AND wikipedia_article_id IS NULL"
            ),
            {"a": page.article_id, "i": event_id},
        ).rowcount
        if not attached:
            raise MappingError(f"{page.name!r}: its event already belongs to another article")
        result.event.updated += 1
        return event_id
    near = conn.execute(
        text("SELECT id FROM events WHERE event_date BETWEEN :lo AND :hi"),
        {"lo": page.event_date - MATCH_WINDOW, "hi": page.event_date + MATCH_WINDOW},
    ).scalars().all()  # fmt: skip
    if near:
        result.anomalies.append(f"event_date_collision:article={page.article_id}:events={near}")
    event_id = conn.execute(
        text(
            "INSERT INTO events (wikipedia_article_id, name, event_date)"
            " VALUES (:a, :n, :d) RETURNING id"
        ),
        {"a": page.article_id, "n": page.name, "d": page.event_date},
    ).scalar_one()
    result.event.inserted += 1
    return event_id


# -- fights -------------------------------------------------------------------------------------


def _differences(existing: Any, new: dict[str, Any]) -> list[str]:
    """The compared result fields on which two results disagree (a field one side doesn't know,
    like a decision type, is not a disagreement)."""
    diffs = []
    for column in COMPARED:
        old, now = existing._mapping[column], new[column]
        known_on_both = column in ("outcome", "winner_id", "method") or None not in (old, now)
        if known_on_both and old != now:
            diffs.append(f"{column}={old}->{now}")
    return diffs


def write_wikipedia_event(
    conn: Connection, page: WikiEventPage, bouts: list[MappedWikiBout]
) -> WikiWriteResult:
    """All of one article's bouts, in the caller's transaction. Raises MappingError (writing
    nothing that survives the rollback) when a name needs a person's review."""
    result = WikiWriteResult()
    # The reviewed alias file is the way a person resolves a held name: edit it, then replay. Only
    # this source's rows are loaded, so an edit made for another source can't stop this job.
    load_aliases(conn, ALIASES_CSV, sources=("wikipedia",))
    names = dict(conn.execute(text("SELECT id, name FROM fighters")).all())
    resolver = NameResolver.from_db(conn)
    index, distinct = DuplicateIndex(names), load_distinct_titles()

    pairs: list[tuple[int, int]] = []
    for bout in bouts:
        first, second = (
            _resolve_fighter(conn, resolver, index, distinct, f, bout, page, result)
            for f in (bout.first, bout.second)
        )
        if first == second:
            raise MappingError(f"{page.name!r}: {bout.first.name!r} resolves to both fighters")
        pairs.append((first, second))
    if len({frozenset(p) for p in pairs}) != len(pairs):
        raise MappingError(f"{page.name!r}: the same two fighters appear in two bouts")

    event_id = _decide_event(conn, page, _existing_fights(conn, page, pairs), result)
    for bout, (first, second) in zip(bouts, pairs, strict=True):
        _write_bout(conn, event_id, bout, first, second, result)

    held = {frozenset(p) for p in pairs}
    for fight_id, a, b in conn.execute(
        text(
            "SELECT id, fighter_a_id, fighter_b_id FROM fights"
            " WHERE event_id = :e AND status = 'scheduled'"
        ),
        {"e": event_id},
    ):
        if frozenset((a, b)) not in held:  # reported, never cancelled by this writer
            result.anomalies.append(f"ufcstats_scheduled_without_wikipedia_match:fight={fight_id}")
    result.skipped = result.already_covered == len(bouts)
    return result


def _write_bout(
    conn: Connection,
    event_id: int,
    bout: MappedWikiBout,
    first: int,
    second: int,
    result: WikiWriteResult,
) -> None:
    low, high = sorted((first, second))
    new = {
        "outcome": bout.outcome,
        "winner_id": first if bout.outcome == "win" else None,
        "method": bout.method,
        "decision_type": bout.decision_type,
        "method_detail": None,  # prose: never stored
        "finish_round": bout.finish_round,
        "finish_time_sec": bout.finish_time_sec,
    }
    existing = conn.execute(
        sa.select(
            FIGHTS.c.id,
            FIGHTS.c.status,
            FIGHTS.c.result_source,
            FIGHTS.c.weight_class,
            FIGHTS.c.gender,
            *(FIGHTS.c[c] for c in COMPARED),
        ).where(
            FIGHTS.c.event_id == event_id,
            FIGHTS.c.fighter_a_id == low,
            FIGHTS.c.fighter_b_id == high,
        )
    ).one_or_none()

    if existing is not None:
        if existing.status == "cancelled":
            result.anomalies.append(f"wikipedia_result_for_cancelled_bout:fight={existing.id}")
            return
        if existing.status == "completed" and existing.result_source == "ufcstats":
            result.already_covered += 1
            if diffs := _differences(existing, new):
                result.anomalies.append(
                    f"source_disagreement:fight={existing.id}:{','.join(diffs)}"
                )
            return
        if existing.status == "completed" and (diffs := _differences(existing, new)):
            result.anomalies.append(f"result_changed:fight={existing.id}:{','.join(diffs)}")

    if bout.gender_guessed:
        result.anomalies.append(f"gender_assumed:{bout.weight_class or 'unknown weight class'}")
    row = {
        "event_id": event_id,
        "fighter_a_id": low,
        "fighter_b_id": high,
        **new,
        "status": "completed",
        "result_source": "wikipedia",
        "is_title_fight": bout.is_title_fight,
        # a fight we already hold keeps its weight class and gender; a guess never overwrites
        "weight_class": (existing.weight_class if existing else None) or bout.weight_class,
        "gender": existing.gender if existing else bout.gender,
        "has_round_stats": False,
    }
    result.fights += upsert_result(conn, row)


def upsert_result(conn: Connection, row: dict[str, Any]) -> UpsertCounts:
    """Insert a fight, or fill / update the one with this (event, pair). The `only_if` is a second
    guard behind the checks in _write_bout, for a result that lands between our read and write: it
    never touches a ufcstats result or a cancelled bout, only a scheduled bout or our own result."""
    return upsert(
        conn,
        FIGHTS,
        [row],
        key=["event_id", "fighter_a_id", "fighter_b_id"],
        replace=[*RESULT_COLUMNS, "status", "result_source", "is_title_fight"],
        fill=["weight_class", "gender"],
        sticky_true=["has_round_stats"],
        only_if=sa.or_(FIGHTS.c.status == "scheduled", FIGHTS.c.result_source == "wikipedia"),
    )
