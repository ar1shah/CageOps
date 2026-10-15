"""`features stats-gap`: which fighters' rates miss fights, and why (read-only)."""

import io
import json
from datetime import date

from sqlalchemy import text

from cageops_worker.features.cli import main
from cageops_worker.features.stats_gap import stats_gap

ROW = {"sig_landed": 40, "sig_att": 80, "td_landed": 1, "td_att": 3, "sub_att": 1, "knockdowns": 0}
WIKI_START = date(2026, 5, 17)
ROADMAP_QUERY = (
    "SELECT count(DISTINCT p) FROM (SELECT fighter_a_id AS p FROM fights f JOIN events e"
    " ON e.id = f.event_id WHERE e.event_date >= :s AND f.status = 'completed'"
    " UNION SELECT fighter_b_id FROM fights f JOIN events e ON e.id = f.event_id"
    " WHERE e.event_date >= :s AND f.status = 'completed') x"
)


def league(world):
    for i in range(1, 8):
        world.fighter(i)
    # 1 v 2 and 3 v 4: ufcstats fights WITH stats
    world.fight(1, 2, date(2025, 1, 1), winner=1, stats={1: ROW, 2: ROW})
    world.fight(3, 4, date(2025, 2, 1), winner=3, stats={3: ROW, 4: ROW})
    # 5 v 6: an old ufcstats fight with no stats (the 1990s kind)
    world.fight(5, 6, date(1996, 1, 1), winner=5)
    # 1 v 3 and 2 v 7: Wikipedia-period results, no stats
    wiki_a = world.fight(1, 3, date(2026, 6, 6), winner=1)
    wiki_b = world.fight(2, 7, date(2026, 7, 4), winner=7)
    world.sql(
        "UPDATE fights SET result_source = 'wikipedia' WHERE id IN (:a, :b)", a=wiki_a, b=wiki_b
    )
    # 6 also has a Wikipedia fight, so it is "both"
    wiki_c = world.fight(6, 4, date(2026, 8, 1), winner=4)
    world.sql("UPDATE fights SET result_source = 'wikipedia' WHERE id = :a", a=wiki_c)
    world.fight(1, 2, date(2026, 12, 1), status="scheduled")  # not completed: never counted


def test_counts_fights_and_fighters_by_the_source_of_the_stat_less_result(world):
    league(world)

    report = stats_gap(world.engine, WIKI_START)

    assert report["completed_fights"] == 6
    assert report["fights_without_stats"] == {
        "total": 4,
        "result_source_wikipedia": 3,
        "result_source_ufcstats": 1,
    }
    # fighters 1, 3, 2, 7, 4, 6 are in a Wikipedia fight; 5 and 6 in the old ufcstats one
    assert report["fighters_with_any_gap"] == {
        "total": 7,
        "wikipedia_only": 5,  # 1, 2, 3, 4, 7
        "ufcstats_only": 1,  # 5
        "both": 1,  # 6
    }
    assert report["history_through"] == "2026-08-01"


def test_since_reproduces_the_roadmap_query_exactly(world):
    league(world)

    report = stats_gap(world.engine, WIKI_START)

    with world.engine.connect() as conn:
        expected = conn.execute(text(ROADMAP_QUERY), {"s": WIKI_START}).scalar_one()
    assert expected == 6  # fighters 1, 2, 3, 4, 6, 7 fought on or after the date
    assert report["since"]["fighters_with_a_completed_fight"] == expected


def test_the_cli_prints_text_and_json_and_changes_nothing(world):
    league(world)
    with world.engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM fight_features")).scalar_one()

    out = io.StringIO()
    assert main(["stats-gap", "--since", "2026-05-17"], engine=world.engine, out=out) == 0
    assert "fighters whose rates miss at least one fight: 7" in out.getvalue()

    out = io.StringIO()
    assert main(["stats-gap", "--json"], engine=world.engine, out=out) == 0
    assert json.loads(out.getvalue())["fighters_with_any_gap"]["total"] == 7
    assert "since" not in json.loads(out.getvalue())

    with world.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM fight_features")).scalar_one() == before


def test_a_fight_where_only_one_side_has_stats_counts_as_having_none(world):
    """The builder needs both sides (`own and opp`), so the gap test does too."""
    world.fighter(1)
    world.fighter(2)
    world.fight(1, 2, date(2025, 1, 1), winner=1, stats={1: ROW})

    report = stats_gap(world.engine)

    assert report["fights_without_stats"]["total"] == 1
    assert report["fighters_with_any_gap"]["total"] == 2
