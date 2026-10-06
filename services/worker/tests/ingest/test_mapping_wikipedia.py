"""Wikipedia bouts into our vocabulary, and the seed's own definitions as the yardstick."""

import dataclasses

import pytest

from cageops_scraper.parsers.wikipedia_models import WikiBout, WikiFighter
from cageops_worker.ingest.errors import MappingError
from cageops_worker.ingest.mapping_wikipedia import (
    DuplicateIndex,
    load_distinct_titles,
    map_bout,
    map_event,
    normalize_wikipedia_method,
    wikipedia_weight_class,
)
from cageops_worker.seed.normalize import UnknownValueError


def mapped(page, who):
    bout = next(b for b in page.bouts if who in (b.first.name, b.second.name))
    return map_bout(bout)


# -- method text -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "method", "decision_type"),
    [
        ("Decision (unanimous) (30–27, 30–27, 30–27)", "win", "decision", "unanimous"),
        ("Decision (split) (28–29, 29–28, 29–28)", "win", "decision", "split"),
        ("Decision (majority) (29–28, 29–28, 28–28)", "win", "decision", "majority"),
        ("Technical Decision (unanimous) (20–18, 20–18, 20–18)", "win", "decision", "unanimous"),
        ("Technical Decision (split)", "win", "decision", "split"),
        ("KO (punch)", "win", "ko_tko", None),
        ("TKO (punches)", "win", "ko_tko", None),
        ("TKO (doctor stoppage)", "win", "ko_tko", None),
        ("TKO (corner stoppage)", "win", "ko_tko", None),
        ("TKO (retirement)", "win", "ko_tko", None),
        ("Submission (rear-naked choke)", "win", "submission", None),
        ("Technical Submission (guillotine choke)", "win", "submission", None),
        ("DQ (illegal knee)", "win", "dq", None),
        ("Disqualification (eye poke)", "win", "dq", None),
        ("Draw (split) (28–29, 29–28, 28–28)", "draw", "decision", "split"),
        ("Draw (majority) (29–28, 28–28, 28–28)", "draw", "decision", "majority"),
        ("Draw (unanimous) (28–28, 28–28, 28–28)", "draw", "decision", "unanimous"),
        ("NC (accidental eye poke)", "no_contest", "other", None),
        ("No Contest (overturned)", "no_contest", "other", None),
        ("No Contest (overturned by the commission)", "no_contest", "other", None),
    ],
)
def test_the_method_text_maps_the_way_the_seed_stores_the_same_results(
    text, kind, method, decision_type
):
    got = normalize_wikipedia_method(text)
    assert (got.kind, got.method, got.decision_type) == (kind, method, decision_type)


@pytest.mark.parametrize(
    "text", ["", "Forfeit", "Majority", "Technical Draw", "Could not continue"]
)
def test_an_unknown_method_raises_instead_of_guessing(text):
    with pytest.raises(UnknownValueError):
        normalize_wikipedia_method(text)


# -- weight classes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Lightweight", "Lightweight"),
        ("Light Heavyweight", "Light Heavyweight"),
        ("Women’s Featherweight", "Women's Featherweight"),  # the curly apostrophe
        ("Women's Strawweight", "Women's Strawweight"),
        ("Catchweight (150.5 lb)", "Catch Weight"),
        ("Catchweight", "Catch Weight"),
        ("Open Weight", "Open Weight"),
    ],
)
def test_weight_classes(raw, expected):
    assert wikipedia_weight_class(raw) == expected


def test_an_unknown_weight_class_raises():
    with pytest.raises(UnknownValueError):
        wikipedia_weight_class("Super Duper Weight")


# -- whole bouts, from the saved pages ---------------------------------------------------------


def test_a_decision_a_ko_and_a_catch_weight_from_ufc_332(wiki_page):
    page = wiki_page("UFC_332")
    won = mapped(page, "Natália Silva")
    assert (won.outcome, won.first_won, won.method, won.decision_type) == (
        "win",
        True,
        "decision",
        "unanimous",
    )
    assert (won.finish_round, won.finish_time_sec, won.weight_class) == (
        5,
        300,
        "Women's Flyweight",
    )
    assert (won.gender, won.gender_guessed) == ("F", False)
    ko = mapped(page, "Damian Pinas")
    assert (ko.method, ko.finish_round, ko.finish_time_sec) == ("ko_tko", 1, 75)
    catch = mapped(page, "Esteban Ribovics")
    assert (catch.weight_class, catch.gender, catch.gender_guessed) == ("Catch Weight", "M", True)


def test_a_draw_a_no_contest_and_a_dq(wiki_page):
    draw = mapped(wiki_page("UFC_323"), "Bogdan Guskov")
    assert (draw.outcome, draw.method, draw.decision_type, draw.finish_round) == (
        "draw",
        "decision",
        "majority",
        3,
    )
    nc = mapped(wiki_page("UFC_321"), "Ciryl Gane")
    assert (nc.outcome, nc.method, nc.finish_time_sec) == ("no_contest", "other", 275)
    dq = mapped(wiki_page("UFC_259"), "Aljamain Sterling")
    assert (dq.outcome, dq.method, dq.first_won) == ("win", "dq", True)


def test_the_winner_is_always_the_first_fighter_on_a_def_row(wiki_page):
    yan = mapped(wiki_page("UFC_323"), "Petr Yan")
    assert yan.first.name == "Petr Yan" and yan.first_won  # the champion is the loser here


# -- title fights: match how the seed defines them ----------------------------------------------


def test_title_fights_follow_the_seeds_definition_on_ufc_249(wiki_page):
    """The seed has both UFC 249 title bouts as is_title_fight = true: the interim one (no (c)
    marker, only the footnote) and Cejudo's defence."""
    page = wiki_page("UFC_249")
    assert mapped(page, "Justin Gaethje").is_title_fight  # interim
    assert mapped(page, "Henry Cejudo").is_title_fight
    assert not mapped(page, "Greg Hardy").is_title_fight
    assert sum(map_bout(b).is_title_fight for b in page.bouts) == 2


def test_vacant_defences_and_title_no_contests_count_too(wiki_page):
    assert mapped(wiki_page("UFC_332"), "Natália Silva").is_title_fight  # vacant
    assert mapped(wiki_page("UFC_321"), "Ciryl Gane").is_title_fight  # a title fight that ended NC
    page = wiki_page("UFC_259")
    assert sum(map_bout(b).is_title_fight for b in page.bouts) == 3  # the seed says 3


def test_the_bmf_belt_is_not_a_title_fight_because_the_seed_says_so():
    """UFC 244's main event (Masvidal vs. Diaz, the BMF title) is is_title_fight = false in the
    seed, so a BMF footnote must map to false."""
    bmf = WikiBout(
        weight_class_raw="Welterweight",
        first=WikiFighter(
            name="Jorge Masvidal", link_title="Jorge_Masvidal", champion_marker=False
        ),
        second=WikiFighter(name="Nate Diaz", link_title="Nate_Diaz", champion_marker=False),
        versus="def.",
        method_raw="TKO (doctor stoppage)",
        round=3,
        time_raw="5:00",
        note_kind="bmf",
    )
    assert map_bout(bmf).is_title_fight is False


# -- impossible combinations ----------------------------------------------------------------------


def test_a_def_row_with_a_draw_method_is_a_mapping_error(wiki_page):
    bout = wiki_page("UFC_323").bouts[0]
    bad = bout.model_copy(update={"method_raw": "Draw (majority)"})
    with pytest.raises(MappingError, match="doesn't fit"):
        map_bout(bad)
    vs_with_a_winner = bout.model_copy(update={"versus": "vs."})
    with pytest.raises(MappingError, match="doesn't fit"):
        map_bout(vs_with_a_winner)


def test_a_round_outside_one_to_five_is_a_mapping_error(wiki_page):
    with pytest.raises(MappingError, match="round 9"):
        map_bout(wiki_page("UFC_323").bouts[0].model_copy(update={"round": 9}))


def test_the_event_row_carries_the_article_id(wiki_page):
    page = wiki_page("UFC_323")
    assert map_event(page) == {
        "name": "UFC 323: Dvalishvili vs. Yan 2",
        "event_date": page.event_date,
        "wikipedia_article_id": 80893887,
    }


def test_no_prose_is_in_what_we_map():
    fields = {
        f.name
        for f in dataclasses.fields(
            __import__(
                "cageops_worker.ingest.mapping_wikipedia", fromlist=["MappedWikiBout"]
            ).MappedWikiBout
        )
    }
    assert not fields & {"notes", "method_raw", "method_detail", "scorecards"}  # fmt: skip


# -- similar names ---------------------------------------------------------------------------------

KNOWN = {1: "Zhang Weili", 2: "Jose Aldo", 3: "Michael Johnson", 4: "Sean Brady"}


def hits(known, name):
    return [(fid, why) for fid, _, why in DuplicateIndex(known).similar(name)]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Weili Zhang", [1]),  # words swapped
        ("José Aldo Jr", [2]),  # one name's words are all in the other
        ("Michael Johnston", [3]),  # one letter off
        ("Micheal Johnson", [3]),  # two neighbouring letters swapped
        ("Michael Jonson", [3]),  # one letter missing
        ("Mikael Johnson", [3]),  # the same surname and first initial
        ("Sean Strickland", []),
        ("Tom Aspinall", []),
        ("Jose Aldo", []),  # an exact match is resolved before this check
    ],
)
def test_similar_names(name, expected):
    assert [fid for fid, _ in hits(KNOWN, name)] == expected


# Real spelling variants found on six saved cards, against the seed's names for the same people.
SEED = {
    1: "JunYong Park", 2: "Aleksei Oleinik", 3: "Michelle Waterson-Gomez", 4: "Livinha Souza",
    5: "Jose Delgado", 6: "Mizuki", 7: "Bia Mesquita", 8: "Edmen Shahbazyan", 9: "Yoel Romero",
}  # fmt: skip


@pytest.mark.parametrize(
    ("wikipedia_name", "seed_id", "why"),
    [
        ("Park Jun-yong", 1, "the same letters spaced differently"),
        ("Michelle Waterson", 3, "one name's words are all in the other"),
        ("Lívia Renata Souza", 4, "the same surname and first initial"),  # nickname Livinha
        ("Jose Miguel Delgado", 5, "one name's words are all in the other"),
        ("Mizuki Inoue", 6, "one name's words are all in the other"),
        ("Beatriz Mesquita", 7, "the same surname and first initial"),  # nickname Bia
    ],
)
def test_the_real_spelling_variants_the_first_version_missed_are_flagged(
    wikipedia_name, seed_id, why
):
    assert hits(SEED, wikipedia_name) == [(seed_id, why)]


@pytest.mark.parametrize("new_fighter", ["Leon Shahbazyan", "Anthony Romero", "Lucas Armand"])
def test_a_different_person_with_a_shared_surname_or_nothing_in_common_is_not_flagged(new_fighter):
    assert hits(SEED, new_fighter) == []  # Edmen's brother is a different fighter


def test_a_transliteration_is_a_known_miss_and_stays_visible_in_the_stub_list():
    assert hits(SEED, "Alexey Oleynik") == []


def test_one_typo_in_a_short_name_is_caught_though_its_ratio_is_under_0_9():
    assert hits({1: "Petr Yan"}, "Petr Yen") == [(1, "one letter off")]
    assert hits({1: "Petr Yan"}, "Petr Yann") == [(1, "one letter off")]


def test_a_single_word_name_is_not_a_word_swap():
    assert DuplicateIndex({1: "Bonfim"}).similar("Bonfim") == []


def test_the_distinct_fighters_file_is_read(tmp_path):
    path = tmp_path / "distinct.csv"
    path.write_text("link_title,reason\nMichael_Johnston,different person\n")
    assert load_distinct_titles(path) == frozenset({"Michael_Johnston"})
    assert load_distinct_titles() == frozenset()  # the committed file starts empty
