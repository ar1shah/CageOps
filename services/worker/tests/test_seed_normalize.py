from decimal import Decimal

import pytest

from cageops_worker.seed.normalize import (
    Method,
    UnknownValueError,
    american_to_decimal,
    clean_text,
    normalize_name,
    normalize_result,
    parse_finish_time,
    ufcstats_id,
)
from cageops_worker.seed.weight_class import (
    MENS_P4P,
    WEIGHT_CLASSES,
    WOMENS_P4P,
    fight_weight_class,
    lookup,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("José Aldo", "jose aldo"),
        ("Jan Błachowicz", "jan blachowicz"),
        ("Kai Kara-France", "kai kara france"),
        ("Khalil Rountree Jr.", "khalil rountree jr"),
        ("Sean O'Malley", "sean omalley"),
        ("  Jiří   Procházka ", "jiri prochazka"),
        ("Klaudia Syguła", "klaudia sygula"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NULL", None),
        ("", None),
        ("  ", None),
        (None, None),
        (" Herb Dean ", "Herb Dean"),
        ("Orthodox", "Orthodox"),
    ],
)
def test_clean_text_treats_the_literal_string_null_as_missing(raw, expected):
    assert clean_text(raw) == expected


def test_ufcstats_id_from_url():
    assert ufcstats_id("http://ufcstats.com/fight-details/2556b7520536ce1d") == "2556b7520536ce1d"


# Every raw `result` spelling seen in the silver file (verified 2026-10-02), with its details.
@pytest.mark.parametrize(
    ("raw_result", "raw_details", "expected"),
    [
        ("Decision", "Unanimous", Method("decision", "unanimous", None)),
        ("Decision", "Split", Method("decision", "split", None)),
        ("Decision", "Majority", Method("decision", "majority", None)),
        ("Decision", None, Method("decision", None, None)),
        (
            "Decision - Unanimous",
            "Ben C 27 - 30. Mark C 27 - 30.",
            Method("decision", "unanimous", "Ben C 27 - 30. Mark C 27 - 30."),
        ),
        ("Decision - Split", None, Method("decision", "split", None)),
        ("Decision - Majority", None, Method("decision", "majority", None)),
        (
            "\n\n        \n KO/TKO \n",
            " Punch to Head At Distance ",
            Method("ko_tko", None, "Punch to Head At Distance"),
        ),
        ("KO/TKO", None, Method("ko_tko", None, None)),
        (
            "\n\n        \n Submission \n",
            " Rear Naked Choke ",
            Method("submission", None, "Rear Naked Choke"),
        ),
        ("Submission", "Armbar", Method("submission", None, "Armbar")),
        (
            "\n\n        \n TKO - Doctor's Stoppage \n",
            " Cut ",
            Method("ko_tko", None, "Doctor's Stoppage (Cut)"),
        ),
        ("TKO - Doctor's Stoppage", None, Method("ko_tko", None, "Doctor's Stoppage")),
        ("\n\n        \n DQ \n", " Illegal Strike ", Method("dq", None, "Illegal Strike")),
        ("Could Not Continue", None, Method("other", None, None)),
    ],
)
def test_normalize_result_covers_every_observed_value(raw_result, raw_details, expected):
    assert normalize_result(raw_result, raw_details) == expected


def test_unknown_result_fails_loudly():
    with pytest.raises(UnknownValueError):
        normalize_result("Overturned", None)


def test_unknown_bare_decision_type_fails_loudly():
    with pytest.raises(UnknownValueError):
        normalize_result("Decision", "Technical")


def test_parse_finish_time():
    assert parse_finish_time("3:33") == 213
    assert parse_finish_time("5:00") == 300
    assert parse_finish_time(None) is None
    with pytest.raises(UnknownValueError):
        parse_finish_time("3.33")


def test_american_to_decimal():
    assert american_to_decimal(150) == Decimal("2.5")
    assert american_to_decimal(-200) == Decimal("1.5")
    assert american_to_decimal(-130) == Decimal("1.769")
    with pytest.raises(ValueError):
        american_to_decimal(0)


# Every weight-class spelling observed across silver, mdabbert and the rankings file.
OBSERVED = [
    # fights (silver + mdabbert)
    "Lightweight", "Welterweight", "Middleweight", "Featherweight", "Heavyweight",
    "Bantamweight", "Light Heavyweight", "Flyweight", "Women's Strawweight",
    "Women's Flyweight", "Women's Bantamweight", "Women's Featherweight",
    "Open Weight", "Catch Weight",
    # fights (silver only): title name in the weight-class column, tournaments, unknown
    "UFC Light Heavyweight Title", "UFC Flyweight Title", "UFC Bantamweight Title",
    "UFC Middleweight Title", "UFC Interim Lightweight Title", "UFC Women's Flyweight Title",
    "UFC 2 Tournament Title", "UFC 3 Tournament Title", "UFC 4 Tournament Title",
    "UFC Heavyweight Title", "UFC Welterweight Title", "UFC Featherweight Title",
    "UFC Women's Strawweight Title", "Nieznana", "NULL",
    # rankings: pound-for-pound spellings
    "Pound-for-Pound", "Men's Pound-for-Pound", "Women's Pound-for-Pound",
    "Men's Pound-for-Pound Top Rank", "Women's Pound-for-Pound Top Rank",
    "Men's Pound-for-PoundTop Rank", "Women's Pound-for-PoundTop Rank",
]  # fmt: skip


@pytest.mark.parametrize("raw", OBSERVED)
def test_every_observed_weight_class_is_mapped(raw):
    assert lookup(raw) is not None


def test_mapping_table_has_exactly_the_observed_values():
    assert set(WEIGHT_CLASSES) == set(OBSERVED)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Pound-for-Pound", MENS_P4P),
        ("Men's Pound-for-PoundTop Rank", MENS_P4P),
        ("Women's Pound-for-Pound Top Rank", WOMENS_P4P),
    ],
)
def test_pound_for_pound_spellings_collapse_to_two_lists(raw, canonical):
    mapped = lookup(raw)

    assert mapped.canonical == canonical
    assert mapped.pound_for_pound


def test_unknown_weight_class_fails_loudly():
    with pytest.raises(UnknownValueError):
        lookup("Super Heavyweight")


def test_pound_for_pound_is_not_a_fight_weight_class():
    assert fight_weight_class("Women's Strawweight").canonical == "Women's Strawweight"
    with pytest.raises(UnknownValueError):
        fight_weight_class("Men's Pound-for-Pound")


def test_title_name_in_weight_class_column_sets_title_fight_and_division():
    mapped = fight_weight_class("UFC Middleweight Title")

    assert (mapped.canonical, mapped.title_fight) == ("Middleweight", True)
    assert fight_weight_class("UFC Interim Lightweight Title").canonical == "Lightweight"
    assert fight_weight_class("UFC 3 Tournament Title").canonical == "Open Weight"


def test_missing_or_unknown_weight_class_is_none_not_an_error():
    assert fight_weight_class(None).canonical is None
    assert fight_weight_class("Nieznana").canonical is None
    assert fight_weight_class("NULL").canonical is None  # silver uses the literal string
