from datetime import date

import pytest

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.fighter import parse_fighter
from cageops_scraper.parsers.models import FighterPage

BASE = "http://ufcstats.com/fighter-details/"
BURNS = "23024fdfc966410a"
RAHIKI = "6eedb757f13b9978"
NILSON = "53e533db1b8e9712"
MITCHELL = "6cbb7661c3258617"


@pytest.fixture
def parse(fixture_html):
    return lambda fighter_id: parse_fighter(
        fixture_html(f"fighter_{fighter_id}"), BASE + fighter_id
    )


def test_a_full_bio_matches_the_seed_exactly(parse):
    burns = parse(BURNS)

    assert (burns.ufcstats_id, burns.name, burns.nickname) == (BURNS, "Gilbert Burns", "Durinho")
    # The Phase 1a seed holds 177.8 cm, 180.34 cm, Orthodox, 1986-07-20 for him (silver file).
    assert (burns.height_cm, burns.reach_cm, burns.stance, burns.dob) == (
        177.8,
        180.34,
        "Orthodox",
        date(1986, 7, 20),
    )


def test_a_bio_the_site_filled_in_after_our_seed(parse):
    """The seed has NULL reach and DOB for him; the page now publishes both."""
    rahiki = parse(RAHIKI)

    assert (rahiki.name, rahiki.nickname) == ("Marwan Rahiki", "Freaky")
    assert (rahiki.height_cm, rahiki.reach_cm) == (172.72, 182.88)  # 5' 8", 72"
    assert (rahiki.stance, rahiki.dob) == ("Orthodox", date(2002, 5, 10))


def test_a_bio_with_nothing_filled_in_is_all_none_not_zero(parse):
    nilson = parse(NILSON)  # the stance line is empty, the others show "--"

    assert nilson.name == "Jack Nilson"
    assert nilson.height_cm is None and nilson.reach_cm is None
    assert nilson.stance is None and nilson.dob is None


def test_a_bio_with_only_a_stance(parse):
    mitchell = parse(MITCHELL)

    assert mitchell.name == "Felix Lee Mitchell"
    assert (mitchell.height_cm, mitchell.reach_cm, mitchell.dob) == (None, None, None)
    assert mitchell.stance == "Orthodox"


def test_a_blank_reach_cell_is_none_even_when_the_neighbours_have_values(fixture_html):
    html = fixture_html(f"fighter_{BURNS}").replace('71"', "--", 1)

    burns = parse_fighter(html, BASE + BURNS)

    assert burns.reach_cm is None
    assert burns.height_cm == 177.8 and burns.dob == date(1986, 7, 20)


# -- no career rates, no record: they would leak the future --------------------


def test_the_model_has_exactly_the_bio_fields_and_no_career_rates():
    assert set(FighterPage.model_fields) == {
        "ufcstats_id",
        "name",
        "nickname",
        "height_cm",
        "reach_cm",
        "stance",
        "dob",
    }


def test_career_numbers_on_the_page_do_not_appear_in_the_result(parse):
    values = {str(v) for v in parse(BURNS).model_dump().values()}

    # Burns' page shows SLpM 3.16, Str. Acc. 48%, SApM 3.68, TD Avg. 2.04, record 22-10-0
    assert not values & {"3.16", "48%", "3.68", "2.04", "22-10-0", "Record: 22-10-0"}


# -- things that must fail loudly --------------------


def test_a_missing_bio_label_is_a_layout_error(fixture_html):
    html = fixture_html(f"fighter_{BURNS}").replace("Reach:", "Arm span:")

    with pytest.raises(ParseError, match="no reach"):
        parse_fighter(html, BASE + BURNS)


def test_an_unknown_height_format_is_an_error_not_a_guess(fixture_html):
    html = fixture_html(f"fighter_{BURNS}").replace("5' 10\"", "178 cm", 1)

    with pytest.raises(ParseError, match="height"):
        parse_fighter(html, BASE + BURNS)


def test_a_missing_name_is_a_layout_error(fixture_html):
    html = fixture_html(f"fighter_{BURNS}").replace("b-content__title-highlight", "b-moved")

    with pytest.raises(ParseError, match="fighter name"):
        parse_fighter(html, BASE + BURNS)


@pytest.mark.parametrize(
    "fixture", ["fight_32054bf2b36b0e47", "event_c3ac8d0da7b05772", "events_upcoming"]
)
def test_other_page_types_are_rejected(fixture_html, fixture):
    with pytest.raises(ParseError):
        parse_fighter(fixture_html(fixture), BASE + BURNS)


def test_the_challenge_page_is_rejected_with_its_url(fixture_html):
    with pytest.raises(ParseError, match="bot-challenge") as excinfo:
        parse_fighter(fixture_html("challenge_2026-10-03"), BASE + BURNS)

    assert excinfo.value.url == BASE + BURNS
