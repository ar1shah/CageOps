"""How well do Wikipedia results agree with the seed's ufcstats results for the same fights?

Four saved cards (UFC 249, 259, 321, 323), compared bout by bout with what the seed holds. The
expected differences are listed in full below, so a new one is a failure that shows what changed."""

import json
from pathlib import Path

import pytest

from cageops_scraper.parsers.wikipedia_event import parse_event
from cageops_worker.ingest.mapping_wikipedia import map_bout
from cageops_worker.seed.normalize import normalize_name

FIXTURES = Path(__file__).parents[3] / "scraper" / "tests" / "fixtures" / "wikipedia"
GOLDEN = json.loads((Path(__file__).parents[1] / "fixtures" / "wikipedia_vs_seed.json").read_text())

# (card, first fighter, field) -> (seed value, wikipedia value)
ALLOWED_DIFFERENCES = {
    # ufcstats files a catch-weight bout under its nominal division; Wikipedia says catchweight.
    ("UFC_249", "Calvin Kattar", "weight_class"): ("Featherweight", "Catch Weight"),
    ("UFC_259", "Askar Askarov", "weight_class"): ("Flyweight", "Catch Weight"),
    ("UFC_321", "Mitch Raposo", "weight_class"): ("Flyweight", "Catch Weight"),
    ("UFC_323", "Brunno Ferreira", "weight_class"): ("Middleweight", "Catch Weight"),
    ("UFC_323", "Mairon Santos", "weight_class"): ("Featherweight", "Catch Weight"),
    # a real disagreement between the two sources about the clock
    ("UFC_259", "Trevin Jones", "finish_time_sec"): (47, 40),
}
FIELDS = (
    "outcome", "winner", "method", "decision_type", "finish_round", "finish_time_sec",
    "is_title_fight", "weight_class", "gender",
)  # fmt: skip


def wikipedia_view(page, bout):
    mapped = map_bout(bout)
    winner = normalize_name(bout.first.name) if mapped.outcome == "win" else None
    return {
        "outcome": mapped.outcome, "winner": winner, "method": mapped.method,
        "decision_type": mapped.decision_type, "finish_round": mapped.finish_round,
        "finish_time_sec": mapped.finish_time_sec, "is_title_fight": mapped.is_title_fight,
        "weight_class": mapped.weight_class, "gender": mapped.gender,
    }  # fmt: skip


@pytest.fixture(scope="module")
def comparison():
    seed = {(f["card"], f["first"], f["second"]): f["seed"] for f in GOLDEN["fights"]}
    pages = {
        card: parse_event((FIXTURES / f"{card}.html").read_text(encoding="utf-8"), "u")
        for card in ("UFC_249", "UFC_259", "UFC_321", "UFC_323")
    }
    compared, differences = 0, {}
    for card, page in pages.items():
        for bout in page.bouts:
            want = seed.get((card, bout.first.name, bout.second.name))
            if want is None:
                continue
            compared += 1
            got = wikipedia_view(page, bout)
            for field in FIELDS:
                if got[field] != want[field]:
                    differences[(card, bout.first.name, field)] = (want[field], got[field])
    return pages, compared, differences


def test_forty_seven_bouts_are_compared_and_every_difference_is_one_we_expect(comparison):
    _, compared, differences = comparison
    assert compared == 47
    assert differences == ALLOWED_DIFFERENCES


def test_every_outcome_winner_method_round_and_title_flag_agrees(comparison):
    _, compared, differences = comparison
    fields_that_differ = {field for _, _, field in differences}
    assert fields_that_differ == {"weight_class", "finish_time_sec"}
    # So of the 47: all agree on who won, how, in which round, and whether it was a title fight.
    assert compared - len(differences) >= 41


def test_the_title_fights_match_the_seed_exactly(comparison):
    """UFC 249: the interim (footnote only) and the defence; UFC 259: three; UFC 321: a vacant title
    fight and a title fight that ended in a no contest; UFC 323: two."""
    pages, _, _ = comparison
    seed = {(f["card"], f["first"], f["second"]): f["seed"] for f in GOLDEN["fights"]}
    titles = {
        (card, b.first.name)
        for card, page in pages.items()
        for b in page.bouts
        if map_bout(b).is_title_fight
    }
    seed_titles = {(card, first) for (card, first, _), s in seed.items() if s["is_title_fight"]}
    assert titles >= seed_titles and len(seed_titles) == 9
    assert titles - seed_titles == set()


def test_six_bouts_have_a_fighter_spelled_differently_so_a_name_match_alone_would_miss_them(
    comparison,
):
    """11% of these bouts (6 of 53) have a name that differs from the seed's. They are why a new
    name is checked against existing fighters before a stub is created."""
    pages, _, _ = comparison
    seed_pairs = {(f["card"], f["first"], f["second"]) for f in GOLDEN["fights"]}
    missing = [
        [card, b.first.name, b.second.name]
        for card, page in pages.items()
        for b in page.bouts
        if (card, b.first.name, b.second.name) not in seed_pairs
    ]
    assert sorted(missing) == sorted([m[:3] for m in GOLDEN["unmatched_by_name"]])
    assert sum(len(p.bouts) for p in pages.values()) == 53
