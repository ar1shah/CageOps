"""Every committed fixture is a real page its own parser accepts, and none is a challenge page.

This is the fixture validator: when someone adds a page to tests/fixtures/ufcstats/ it must be
listed here, so a bad save (the "Checking your browser" page, a half-saved file) fails loudly
instead of becoming a fixture that tests nothing.
"""

import json
from pathlib import Path

import pytest

from cageops_scraper.parsers.__main__ import PARSERS, guess, main
from cageops_scraper.sources import UfcStatsSource

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ufcstats"
CHALLENGE = "challenge_2026-10-03.html"
PAGES = sorted(p for p in FIXTURES.glob("*.html") if p.name != CHALLENGE)


def test_the_fixture_folder_has_all_the_page_types_we_need():
    names = {p.stem for p in PAGES}

    assert {"events_completed", "events_upcoming"} <= names
    assert {n.split("_")[0] for n in names} == {"events", "event", "fight", "fighter"}
    assert len(PAGES) == 14


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_every_fixture_parses_with_its_own_parser(page):
    kind, url = guess(page)

    result = PARSERS[kind](page.read_text(encoding="utf-8"), url)

    assert result  # a model, or a non-empty list


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_no_fixture_is_the_bot_challenge_page(page):
    html = page.read_text(encoding="utf-8")

    assert UfcStatsSource().block_reason(200, html) is None
    assert len(html) > 3000  # the challenge page is ~3 KB


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_every_fixture_is_rejected_by_the_other_parsers(page):
    """Each page type is only accepted by its own parser, so a mix-up raises instead of parsing
    something wrong."""
    from cageops_scraper.errors import ParseError

    own, url = guess(page)
    html = page.read_text(encoding="utf-8")
    for kind, parse in PARSERS.items():
        if kind == own:
            continue
        with pytest.raises(ParseError):
            parse(html, url)


def test_the_challenge_fixture_is_still_a_challenge_page():
    html = (FIXTURES / CHALLENGE).read_text(encoding="utf-8")

    assert UfcStatsSource().block_reason(200, html) == "browser_challenge"


# -- the dev tool ----------------------------------------------------------------------------------


def test_the_dev_tool_prints_json_for_a_fixture(capsys):
    assert main([str(FIXTURES / "fight_32054bf2b36b0e47.html")]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["fight_id"] == "32054bf2b36b0e47" and data["method_raw"] == "KO/TKO"


def test_the_dev_tool_handles_lists_and_reports_parse_errors(capsys):
    assert main([str(FIXTURES / "events_upcoming.html")]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 8

    assert (
        main(
            [
                str(FIXTURES / CHALLENGE),
                "--kind",
                "fight",
                "--url",
                "http://x/fight-details/" + "a" * 16,
            ]
        )
        == 1
    )
    assert "bot-challenge" in capsys.readouterr().err
