import re

import pytest

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.event import parse_event
from cageops_scraper.parsers.fight import parse_fight
from cageops_scraper.parsers.models import FightPage, FightStats

BASE = "http://ufcstats.com/fight-details/"
KO = "32054bf2b36b0e47"  # Burns vs Malott
DECISION = "b5299e5b946015e5"  # Phillips vs Jourdain
SUBMISSION = "9fab4b0ad082f670"  # Valentin vs Leblanc
DRAW = "552f7cdaf93e1055"  # Vologdin vs Castaneda
NO_CONTEST = "6390c8e74630473e"  # Aspinall vs Gane
NO_STATS = "635fbf57001897c7"  # Santos vs Marscucci, 1998

BURNS, MALOTT = "23024fdfc966410a", "dd6103dd7127db1d"
ALL = [KO, DECISION, SUBMISSION, DRAW, NO_CONTEST, NO_STATS]


@pytest.fixture
def parse(fixture_html):
    return lambda fight_id: parse_fight(fixture_html(f"fight_{fight_id}"), BASE + fight_id)


def stats(rows: list[FightStats], fighter: str, rnd: int | None) -> FightStats:
    return next(r for r in rows if r.fighter_id == fighter and r.round == rnd)


# -- the result block --------------------------------------------------------------------------


def test_ko_result_block(parse):
    page = parse(KO)

    assert (page.fight_id, page.event_id) == (KO, "c3ac8d0da7b05772")
    assert page.bout_title_raw == "Welterweight Bout" and not page.is_title_fight
    assert page.method_raw == "KO/TKO"
    assert page.details_raw == "Punches to Head On Ground"
    assert (page.round, page.finish_time_sec) == (3, 128)
    assert (page.time_format_raw, page.scheduled_rounds) == ("5 Rnd (5-5-5-5-5)", 5)
    assert page.referee == "Herb Dean"


def test_decision_keeps_the_judges_scores(parse):
    page = parse(DECISION)

    assert page.method_raw == "Decision - Unanimous"
    assert (
        page.details_raw == "Junichiro Kamijo 28 - 29. Sal D'amato 28 - 29. Jason Rodgers 28 - 29."
    )
    assert (page.round, page.finish_time_sec) == (3, 300)
    assert (page.scheduled_rounds, page.referee) == (3, "Jerin Valel")


def test_submission_result_block(parse):
    page = parse(SUBMISSION)

    assert (page.method_raw, page.details_raw) == ("Submission", "Rear Naked Choke")
    assert (page.round, page.finish_time_sec, page.referee) == (1, 142, "Chris Desautels")


def test_draw_has_both_fighters_marked_d(parse):
    page = parse(DRAW)

    assert [f.result for f in page.fighters] == ["D", "D"]
    assert page.method_raw == "Decision - Majority"
    assert page.bout_title_raw == "Catch Weight Bout"
    assert page.details_raw == "Laura Baldwin 27 - 29. Jason Rodgers 28 - 28. Mike Bell 28 - 28."


def test_no_contest_title_fight(parse):
    page = parse(NO_CONTEST)

    assert [f.result for f in page.fighters] == ["NC", "NC"]
    assert page.bout_title_raw == "UFC Heavyweight Title Bout" and page.is_title_fight
    assert (page.method_raw, page.details_raw) == ("Could Not Continue", "Eye poke by Gane")
    assert (page.round, page.finish_time_sec, page.scheduled_rounds) == (1, 275, 5)


def test_a_1998_one_round_plus_overtime_fight(parse):
    page = parse(NO_STATS)

    assert (page.round, page.finish_time_sec) == (1, 627)  # 10:27, not the seed's 27
    assert (page.time_format_raw, page.scheduled_rounds) == ("1 Rnd + OT (12-3)", 1)
    assert page.details_raw == "to"  # the site's own text, kept as is


# -- who is who: order and the red corner --------------------------------------------------------


@pytest.mark.parametrize(
    ("fight_id", "seed_red_corner"),
    [
        # ufcstats ids of fighter_a/red in the Phase 1a seed (fights.red_fighter_id)
        (KO, BURNS),
        (DECISION, "60425d07ef4b91a7"),  # Kyler Phillips
        (SUBMISSION, "cd0dfd9846bd03b9"),  # Julien Leblanc
        (DRAW, "5fa2974cbd18e05c"),  # John Castaneda
        (NO_CONTEST, "399afbabc02376b5"),  # Tom Aspinall
        (NO_STATS, "e8efeb9cf33b1941"),  # Cesar Marscucci
    ],
)
def test_the_first_fighter_on_a_fight_page_is_the_seeds_red_corner(
    parse, fight_id, seed_red_corner
):
    assert parse(fight_id).fighters[0].ufcstats_id == seed_red_corner


def test_fight_page_order_is_not_winner_first(parse):
    page = parse(KO)

    assert [(f.name, f.result) for f in page.fighters] == [
        ("Gilbert Burns", "L"),
        ("Mike Malott", "W"),
    ]


@pytest.mark.parametrize("fight_id", [KO, DECISION, SUBMISSION, DRAW])
def test_event_page_and_fight_page_agree_on_the_result(fixture_html, parse, fight_id):
    """The event page's winner-first order is a convenience; the fight page's W/L badge is the
    authority. They must agree, and the fighter sets must match."""
    event = parse_event(
        fixture_html("event_c3ac8d0da7b05772"), "http://ufcstats.com/event-details/c3ac8d0da7b05772"
    )
    bout = next(b for b in event.bouts if b.fight_id == fight_id)
    page = parse(fight_id)

    assert {f.ufcstats_id for f in bout.fighters} == {f.ufcstats_id for f in page.fighters}
    badges = {f.ufcstats_id: f.result for f in page.fighters}
    if bout.outcome == "win":
        assert badges[bout.winner_id] == "W"
        assert [r for fid, r in badges.items() if fid != bout.winner_id] == ["L"]
    else:
        assert bout.outcome == "draw" and set(badges.values()) == {"D"}
    assert page.event_id == event.ufcstats_id


# -- the stat tables -----------------------------------------------------------------------------


def test_ko_totals_match_the_page(parse):
    page = parse(KO)
    burns, malott = stats(page.totals, BURNS, None), stats(page.totals, MALOTT, None)

    assert len(page.totals) == 2
    assert (burns.knockdowns, burns.sig_strikes_landed, burns.sig_strikes_att) == (0, 40, 92)
    assert (burns.total_strikes_landed, burns.total_strikes_att) == (42, 94)
    assert (burns.takedowns_landed, burns.takedowns_att) == (0, 7)
    assert (burns.submission_att, burns.reversals, burns.ctrl_sec) == (0, 0, 1)
    assert (malott.knockdowns, malott.sig_strikes_landed, malott.sig_strikes_att) == (2, 56, 121)
    assert (malott.takedowns_landed, malott.takedowns_att) == (
        0,
        0,
    )  # published as "0 of 0": real zeros
    assert malott.ctrl_sec == 14


def test_ko_strike_breakdown_comes_from_the_second_table(parse):
    burns = stats(parse(KO).totals, BURNS, None)

    assert (burns.head_landed, burns.head_att) == (16, 60)
    assert (burns.body_landed, burns.body_att) == (10, 12)
    assert (burns.leg_landed, burns.leg_att) == (14, 20)
    assert (burns.distance_landed, burns.distance_att) == (39, 89)
    assert (burns.clinch_landed, burns.clinch_att) == (1, 3)
    assert (burns.ground_landed, burns.ground_att) == (0, 0)


def test_per_round_rows_come_out_in_round_order_despite_the_malformed_table(parse):
    page = parse(KO)

    assert [(r.round, r.fighter_id) for r in page.rounds] == [
        (1, BURNS), (1, MALOTT), (2, BURNS), (2, MALOTT), (3, BURNS), (3, MALOTT)
    ]  # fmt: skip
    r1, r3 = stats(page.rounds, BURNS, 1), stats(page.rounds, BURNS, 3)
    assert (r1.sig_strikes_landed, r1.sig_strikes_att, r1.takedowns_att) == (11, 38, 4)
    assert (r3.sig_strikes_landed, r3.sig_strikes_att, r3.takedowns_att) == (6, 17, 1)
    assert (r3.head_landed, r3.head_att, r3.distance_att) == (2, 11, 16)
    assert stats(page.rounds, MALOTT, 3).knockdowns == 2  # the finish
    assert stats(page.rounds, MALOTT, 3).ctrl_sec == 14


@pytest.mark.parametrize("fight_id", [KO, DECISION, SUBMISSION, DRAW, NO_CONTEST])
def test_per_round_sig_strikes_add_up_to_the_totals_on_every_fixture(parse, fight_id):
    page = parse(fight_id)

    assert page.has_round_stats and page.rounds and page.anomalies == []
    for total in page.totals:
        landed = [r.sig_strikes_landed for r in page.rounds if r.fighter_id == total.fighter_id]
        assert sum(landed) == total.sig_strikes_landed


def test_round_counts_follow_the_fight(parse):
    assert len(parse(KO).rounds) == 2 * 3  # finished in round 3
    assert len(parse(DECISION).rounds) == 2 * 3
    assert len(parse(SUBMISSION).rounds) == 2 * 1
    assert len(parse(NO_CONTEST).rounds) == 2 * 1


def test_a_fight_with_no_round_by_round_stats_has_empty_stats_not_zeros(parse):
    page = parse(NO_STATS)

    assert page.has_round_stats is False
    assert page.totals == [] and page.rounds == [] and page.anomalies == []


def test_a_blank_cell_is_none_and_the_neighbouring_values_are_untouched(fixture_html):
    html = fixture_html(f"fight_{KO}").replace("0:14", "--", 1)  # Malott's whole-fight control time

    page = parse_fight(html, BASE + KO)

    assert stats(page.totals, MALOTT, None).ctrl_sec is None  # not 0
    assert stats(page.totals, BURNS, None).ctrl_sec == 1
    assert stats(page.rounds, MALOTT, 3).ctrl_sec == 14  # the per-round cell was not edited
    assert page.anomalies == []  # a missing value is not a mismatch


# -- anomalies warn, layout changes raise ----------------------------------------


def test_round_sums_that_dont_match_are_recorded_not_raised(fixture_html):
    html = fixture_html(f"fight_{KO}").replace("11 of 38", "12 of 38", 1)  # Burns, round 1

    page = parse_fight(html, BASE + KO)

    assert page.anomalies == [f"round_sig_strikes_sum_mismatch:fighter={BURNS}"]
    assert stats(page.rounds, BURNS, 1).sig_strikes_landed == 12  # the page's value is kept as is


def test_an_unrecognized_time_format_is_an_anomaly(fixture_html):
    html = fixture_html(f"fight_{KO}").replace("5 Rnd (5-5-5-5-5)", "Five rounds")

    page = parse_fight(html, BASE + KO)

    assert page.scheduled_rounds is None
    assert page.anomalies == ["unrecognized_time_format:Five rounds"]


def test_no_time_limit_is_not_an_anomaly(fixture_html):
    html = fixture_html(f"fight_{KO}").replace("5 Rnd (5-5-5-5-5)", "No Time Limit")

    page = parse_fight(html, BASE + KO)

    assert page.scheduled_rounds is None and page.anomalies == []


def test_a_fixed_header_typo_changes_nothing(fixture_html, parse):
    html = re.sub(r"(Td %\s*</th>\s*<th[^>]*>\s*)Td %", r"\1Td", fixture_html(f"fight_{KO}"))

    assert html != fixture_html(f"fight_{KO}")
    assert parse_fight(html, BASE + KO) == parse(KO)


def test_cosmetic_header_changes_change_nothing(fixture_html, parse):
    html = fixture_html(f"fight_{KO}").replace("Sig. str. %", "SIG STR PCT").replace("Ctrl", "CTRL")

    assert parse_fight(html, BASE + KO) == parse(KO)


def test_a_removed_stats_column_is_a_layout_error(fixture_html):
    html = re.sub(r"<th[^>]*>\s*Ctrl\s*</th>", "", fixture_html(f"fight_{KO}"), count=1)

    with pytest.raises(ParseError, match="matches neither"):
        parse_fight(html, BASE + KO)


def test_a_missing_result_block_is_a_layout_error(fixture_html):
    html = fixture_html(f"fight_{KO}").replace("b-fight-details__content", "b-moved")

    with pytest.raises(ParseError, match="result block"):
        parse_fight(html, BASE + KO)


def test_an_unknown_result_badge_is_an_error(fixture_html):
    html = re.sub(r"(person-status[^>]*>\s*)L\b", r"\1X", fixture_html(f"fight_{KO}"), count=1)

    with pytest.raises(ParseError, match="unknown result badge 'X'"):
        parse_fight(html, BASE + KO)


def test_a_stats_row_naming_a_stranger_is_an_error(fixture_html):
    html = fixture_html(f"fight_{KO}").replace(
        BURNS, "ffffffffffffffff", 3
    )  # skip the persons block

    with pytest.raises(ParseError):
        parse_fight(html, BASE + KO)


def test_no_tables_and_no_notice_is_a_layout_error(fixture_html):
    html = fixture_html(f"fight_{NO_STATS}").replace(
        "Round-by-round stats not currently available.", ""
    )

    with pytest.raises(ParseError, match="no stat tables"):
        parse_fight(html, BASE + NO_STATS)


@pytest.mark.parametrize(
    "fixture", ["event_c3ac8d0da7b05772", "fighter_23024fdfc966410a", "events_completed"]
)
def test_other_page_types_are_rejected(fixture_html, fixture):
    with pytest.raises(ParseError):
        parse_fight(fixture_html(fixture), BASE + KO)


def test_the_challenge_page_is_rejected_with_its_url(fixture_html):
    with pytest.raises(ParseError, match="bot-challenge") as excinfo:
        parse_fight(fixture_html("challenge_2026-10-03"), BASE + KO)

    assert excinfo.value.url == BASE + KO


# -- point-in-time: a fight page must not carry a date ------------------------------


def test_the_fight_models_have_no_date_field():
    """No field is named like a date and none is typed as one, so a fight's date can't come from
    a fight page. (The event page's `event_date` is the only source.)"""
    for model in (FightPage, FightStats):
        for name, field in model.model_fields.items():
            assert "date" not in name
            assert "date" not in str(field.annotation).lower()
