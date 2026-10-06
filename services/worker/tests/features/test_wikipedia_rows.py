"""Wikipedia results in the feature table (D-029): they count as fights but carry no stats."""

from datetime import date
from pathlib import Path

from sqlalchemy import text

from cageops_scraper.parsers.wikipedia_event import parse_event
from cageops_worker.ingest.mapping_wikipedia import map_bout
from cageops_worker.ingest.store_wikipedia import write_wikipedia_event

FIXTURE = (
    Path(__file__).parents[3] / "scraper" / "tests" / "fixtures" / "wikipedia" / "UFC_323.html"
)
ROW = {"sig_landed": 40, "sig_att": 80, "td_landed": 1, "td_att": 3, "sub_att": 1, "knockdowns": 0}


def test_a_wikipedia_fight_counts_in_the_history_but_not_in_the_rates(world):
    yan = world.fighter(1, "Petr Yan", dob=date(1993, 4, 11))
    other = world.fighter(2, "Opponent One")
    world.fight(yan, other, date(2025, 3, 1), winner=yan, stats={yan: ROW, other: ROW})
    world.fight(yan, other, date(2025, 6, 1), winner=other, stats={yan: ROW, other: ROW})
    world.sql(
        "SELECT setval(pg_get_serial_sequence('fighters', 'id'), (SELECT max(id) FROM fighters))"
    )
    page = parse_event(FIXTURE.read_text(encoding="utf-8"), "https://en.wikipedia.org/wiki/UFC_323")
    with world.engine.begin() as conn:
        write_wikipedia_event(conn, page, [map_bout(b) for b in page.bouts])

    world.build(today=date(2026, 1, 1))

    with world.engine.connect() as conn:
        fight_id = conn.execute(
            text(
                "SELECT f.id FROM fights f JOIN fighters a ON a.id = f.fighter_a_id"
                " JOIN fighters b ON b.id = f.fighter_b_id"
                " WHERE f.result_source = 'wikipedia'"
                " AND (a.name = 'Petr Yan' OR b.name = 'Petr Yan')"
            )
        ).scalar_one()
    row = world.features()[(fight_id, yan)]
    assert row["prior_ufc_fights"] == 2 and row["win_streak"] == 0  # lost the last one
    assert row["n_fights_career"] == 2 and row["n_fights_with_stats_career"] == 2
    assert row["is_title_fight"] is True  # the Dvalishvili rematch, a title fight

    # The next fight Yan has is a Wikipedia one, so it is history for a later fight.
    world.fight(yan, other, date(2026, 6, 1), winner=yan, stats={yan: ROW, other: ROW})
    world.build(today=date(2026, 7, 1))
    later = [r for k, r in world.features().items() if k[1] == yan and r["prior_ufc_fights"] == 3]
    [row3] = later
    assert row3["n_fights_career"] == 3
    assert row3["n_fights_with_stats_career"] == 2  # the Wikipedia fight has no stats
    assert row3["n_fights_with_duration_career"] == 2  # and no trusted duration (no round count)
    assert row3["win_streak"] == 1  # it was a win for Yan: the streak counts it


def test_a_stub_fighter_has_no_bio_so_age_and_size_are_null(world):
    page = parse_event(FIXTURE.read_text(encoding="utf-8"), "https://en.wikipedia.org/wiki/UFC_323")
    with world.engine.begin() as conn:
        write_wikipedia_event(conn, page, [map_bout(b) for b in page.bouts])

    world.build(today=date(2026, 1, 1))

    rows = list(world.features().values())
    assert len(rows) == 28
    assert all(r["age_days"] is None and r["height_cm"] is None for r in rows)
    assert all(r["prior_ufc_fights"] == 0 for r in rows)  # a card of strangers: no history yet
