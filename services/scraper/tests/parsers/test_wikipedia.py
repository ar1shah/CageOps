"""The Wikipedia parsers, on saved pages (trimmed copies of real articles) and on small pages built
by hand for the cases the real ones don't contain. No network."""

from datetime import date
from pathlib import Path

import pytest

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.wikipedia_event import note_kind, parse_event
from cageops_scraper.parsers.wikipedia_events import parse_year_list
from cageops_scraper.parsers.wikipedia_models import WikiBout

FIXTURES = Path(__file__).parents[1] / "fixtures" / "wikipedia"
URL = "https://en.wikipedia.org/wiki/UFC_332"


def saved(name: str):
    return parse_event((FIXTURES / f"{name}.html").read_text(encoding="utf-8"), URL)


def bout(page, winner: str) -> WikiBout:
    return next(b for b in page.bouts if winner in (b.first.name, b.second.name))


# -- the "{year} in UFC" list ----------------------------------------------------------------


@pytest.fixture(scope="module")
def year_list():
    return parse_year_list((FIXTURES / "2026_in_UFC.html").read_text(encoding="utf-8"), URL)


def test_the_past_events_table_lists_every_event_newest_first(year_list):
    assert len(year_list) == 34  # bonus-winner rows under a numbered event are not events
    first, last = year_list[0], year_list[-1]
    assert (first.title, first.name, first.event_date) == (
        "UFC_332",
        "UFC 332: Silva vs. Wang",
        date(2026, 10, 3),
    )
    assert (last.title, last.event_date) == ("UFC_324", date(2026, 1, 24))
    dates = [e.event_date for e in year_list]
    assert dates == sorted(dates, reverse=True)


def test_fight_night_events_link_to_their_numbered_article(year_list):
    kape = next(e for e in year_list if "Kape" in e.name)
    assert (kape.title, kape.event_date) == ("UFC_Fight_Night_279", date(2026, 6, 20))
    assert all(e.title for e in year_list)


def test_the_gap_the_seed_leaves_is_eighteen_events(year_list):
    gap = [e for e in year_list if e.event_date >= date(2026, 5, 17)]
    assert len(gap) == 18 and gap[-1].name == "UFC Fight Night: Song vs. Figueiredo"


YEAR_PAGE = (
    '<html><body><div class="mw-heading"><h3 id="Past_events">Past events</h3></div>'
    '<table class="wikitable"><tr><th>#</th><th>Event</th><th>Date</th></tr>{rows}</table>'
    "</body></html>"
)


def test_an_event_with_no_article_has_no_title():
    row = "<tr><td>1</td><td>UFC Obscure</td><td>Jan 1, 2026</td></tr>"
    [event] = parse_year_list(YEAR_PAGE.format(rows=row), URL)
    assert (event.title, event.name) == (None, "UFC Obscure")


@pytest.mark.parametrize(
    "html",
    [
        "<html><body><h3>Nothing</h3></body></html>",  # no Past events heading
        YEAR_PAGE.replace("<th>#</th>", "<th>No.</th>").format(rows=""),  # header changed
        YEAR_PAGE.format(rows="<tr><td>1</td><td>UFC X</td><td>soon</td></tr>"),  # bad date
        "",
    ],
)
def test_a_year_list_that_changed_shape_is_a_parse_error(html):
    with pytest.raises(ParseError):
        parse_year_list(html, URL)


# -- an event article: the page ------------------------------------------------------------------


def test_ufc_332_name_date_and_ids():
    page = saved("UFC_332")
    assert (page.name, page.event_date) == ("UFC 332: Silva vs. Wang", date(2026, 10, 3))
    assert (page.article_id, page.revision_id) == (83826247, 1378760261)
    assert (page.canonical_title, len(page.bouts), page.anomalies) == ("UFC_332", 14, ())


def test_a_redirect_resolves_to_the_real_article():
    """/wiki/UFC_Fight_Night_279 is served as the article whose real title is the matchup."""
    page = saved("UFC_Fight_Night_279")
    assert page.canonical_title == "UFC_Fight_Night:_Kape_vs._Horiguchi"
    assert (page.article_id, page.event_date) == (82772799, date(2026, 6, 20))


@pytest.mark.parametrize(
    ("name", "bouts"),
    [
        ("UFC_332", 14),
        ("UFC_Fight_Night_279", 12),
        ("UFC_323", 14),
        ("UFC_321", 13),
        ("UFC_259", 15),
    ],
)
def test_bout_counts_match_what_the_seed_holds_for_the_same_cards(name, bouts):
    assert len(saved(name).bouts) == bouts  # 323, 321, 259 are in the seed with these counts


# -- an event article: bouts ---------------------------------------------------------------------


def test_a_decision_keeps_its_scorecards_in_the_method_text_only_in_memory():
    won = bout(saved("UFC_332"), "Natália Silva")
    assert won.first.name == "Natália Silva" and won.versus == "def."
    assert won.method_raw == "Decision (unanimous) (48–47, 48–47, 49–46)"
    assert (won.round, won.time_raw, won.weight_class_raw) == (5, "5:00", "Women's Flyweight")


def test_ko_tko_and_submission_variants_are_read_as_written():
    page = saved("UFC_332")
    assert bout(page, "Payton Talbott").method_raw == "TKO (punches)"
    assert bout(page, "Damian Pinas").method_raw == "KO (punch)"
    assert bout(saved("UFC_Fight_Night_279"), "Christian Rodriguez").method_raw == (
        "Technical Submission (guillotine choke)"
    )
    assert bout(saved("UFC_259"), "Amanda Nunes").method_raw == "Submission (triangle armbar)"


def test_a_draw_uses_vs_and_a_no_contest_says_nc_and_a_dq_says_dq():
    draw = bout(saved("UFC_323"), "Bogdan Guskov")
    nc = bout(saved("UFC_321"), "Ciryl Gane")
    dq = bout(saved("UFC_259"), "Aljamain Sterling")
    assert (draw.versus, draw.method_raw) == ("vs.", "Draw (majority) (29–28, 28–28, 28–28)")
    assert (nc.versus, nc.method_raw, nc.round, nc.time_raw) == (
        "vs.",
        "NC (accidental eye poke)",
        1,
        "4:35",
    )
    assert (dq.versus, dq.method_raw) == ("def.", "DQ (illegal knee)")


def test_catch_weights_and_curly_apostrophes_come_through_as_written():
    assert bout(saved("UFC_249"), "Calvin Kattar").weight_class_raw == "Catchweight (150.5 lb)"
    assert bout(saved("UFC_259"), "Amanda Nunes").weight_class_raw == "Women’s Featherweight"


def test_a_fighters_link_title_is_the_article_target_not_the_display_name():
    page = saved("UFC_332")
    silva = bout(page, "Natália Silva").first
    assert (silva.name, silva.link_title) == ("Natália Silva", "Natália_Silva_(fighter)")
    assert bout(saved("UFC_323"), "Jan Błachowicz").first.link_title == "Jan_Błachowicz"


# -- title fights, read from the markers and the footnotes ---------------------------------------


def test_a_champion_defending_is_marked_and_the_footnote_says_championship():
    cejudo = bout(saved("UFC_249"), "Henry Cejudo")
    assert cejudo.first.champion_marker and not cejudo.second.champion_marker
    assert cejudo.first.name == "Henry Cejudo"  # "(c)" is not part of the name
    assert cejudo.note_kind == "championship"


def test_an_interim_title_fight_has_no_marker_only_the_footnote():
    gaethje = bout(saved("UFC_249"), "Justin Gaethje")
    assert not gaethje.first.champion_marker and not gaethje.second.champion_marker
    assert gaethje.note_kind == "championship"  # "For the interim UFC Lightweight Championship"


def test_the_champion_can_be_on_the_losing_side():
    yan = bout(saved("UFC_323"), "Petr Yan")
    assert (yan.first.name, yan.second.name, yan.second.champion_marker) == (
        "Petr Yan",
        "Merab Dvalishvili",
        True,
    )


def test_a_vacant_title_fight_has_no_marker_and_is_still_a_title_fight():
    dern = bout(saved("UFC_321"), "Mackenzie Dern")
    assert not dern.first.champion_marker and dern.note_kind == "championship"


def test_a_footnote_about_a_point_deduction_is_not_a_title_fight():
    barnett = bout(saved("UFC_321"), "Chris Barnett")  # footnoted: "...deducted one point..."
    assert barnett.note_kind is None
    assert saved("UFC_321").anomalies == ()


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["For the UFC Bantamweight Championship ."], "championship"),
        (["For the interim UFC Lightweight Championship ."], "championship"),
        (["For the vacant UFC Women's Strawweight Championship ."], "championship"),
        (["For the UFC BMF Championship ."], "bmf"),  # the seed does not count BMF as a title fight
        (["Abdelwahab was deducted one point in round 1."], None),
        ([], None),
    ],
)
def test_note_kind(texts, expected):
    assert note_kind(texts) == expected


# -- hand-built pages: name cleaning and layout failures -------------------------------------------

SCRIPT = '<script>RLCONF={"wgArticleId":1234,"wgRevisionId":5678};</script>'
HEAD = (
    f"<html><head><title>UFC Test</title>"
    f'<link rel="canonical" href="https://en.wikipedia.org/wiki/UFC_Test">{SCRIPT}</head><body>'
    '<table class="infobox"><tr><th class="infobox-above" colspan="2">UFC Test: A vs. B</th></tr>'
    '<tr><th>Date</th><td>Jan 1, 2026<span style="display:none">(<span class="bday">2026-01-01'
    "</span>)</span></td></tr></table>"
    '<div class="mw-heading mw-heading2"><h2 id="Results">Results</h2></div>'
)
HEADER = (
    '<table class="toccolours"><tr><th colspan="8">Main card</th></tr><tr><th>Weight class</th>'
    "<th></th><th></th><th></th><th>Method</th><th>Round</th><th>Time</th><th>Notes</th></tr>"
)


def page(rows: str, footnotes: str = "") -> str:
    ol = f'<ol class="references">{footnotes}</ol>' if footnotes else ""
    return f"{HEAD}{HEADER}{rows}</table>{ol}</body></html>"


def row(first="A", second="B", token="def.", method="KO (punch)", notes="", rnd="1", clock="1:00"):
    return (
        f"<tr><td>Lightweight</td><td>{first}</td><td>{token}</td><td>{second}</td>"
        f"<td>{method}</td><td>{rnd}</td><td>{clock}</td><td>{notes}</td></tr>"
    )


def test_markers_and_footnote_brackets_are_not_part_of_a_name():
    html = page(
        row(
            first='<a href="/wiki/Jon_Jones">Jon Jones</a> (c) [a]', second="Stipe Miocic (ic)[ 1 ]"
        )
    )
    [one] = parse_event(html, URL).bouts
    assert (one.first.name, one.second.name) == ("Jon Jones", "Stipe Miocic")
    assert (one.first.champion_marker, one.second.champion_marker) == (True, True)


def test_a_red_link_gives_its_title_and_no_link_gives_none():
    red = '<a href="/w/index.php?title=New_Fighter&amp;action=edit&amp;redlink=1">New Fighter</a>'
    [one] = parse_event(page(row(first=red, second="Plain Name")), URL).bouts
    assert (one.first.link_title, one.second.link_title) == ("New_Fighter", None)


def test_a_marker_without_a_championship_note_is_reported_not_guessed():
    [one] = (page_ := parse_event(page(row(first="Champ (c)")), URL)).bouts
    assert one.note_kind is None
    assert page_.anomalies == ("title_marker_without_note:Champ vs B",)


def test_footnote_text_that_says_championship_is_followed_from_the_marker():
    note = (
        '<li id="cite_note-9"><span class="reference-text">For the UFC Welterweight '
        "Championship.</span></li>"
    )
    cell = '<sup><a href="#cite_note-9">[a]</a></sup>'
    [one] = parse_event(page(row(first="Champ (c)", notes=cell), note), URL).bouts
    assert one.note_kind == "championship"


def test_the_notes_text_is_never_part_of_the_parsed_model():
    assert set(WikiBout.model_fields) == {
        "weight_class_raw", "first", "second", "versus", "method_raw", "round", "time_raw",
        "note_kind",
    }  # fmt: skip


@pytest.mark.parametrize(
    "html",
    [
        "",
        page(row()).replace('"wgArticleId":1234,', ""),  # no article id
        page(row()).replace('"wgRevisionId":5678', '"x":1'),  # no revision id
        page(row()).replace('<h2 id="Results">', '<h2 id="Result">'),  # no Results heading
        page(""),  # a results table with no bouts
        page(row()).replace("<th>Method</th>", "<th>How</th>"),  # a header changed
        page(row(token="beat")),  # not def. / vs.
        page(row(method="")),  # blank method
        page("<tr><td>Lightweight</td><td>A</td></tr>"),  # a bout row with too few cells
        page(row()).replace("2026-01-01", "soon"),  # the infobox has no ISO date
        page(row()).replace(
            '<link rel="canonical" href="https://en.wikipedia.org/wiki/UFC_Test">', ""
        ),
    ],
)
def test_a_page_that_changed_shape_is_a_parse_error(html):
    with pytest.raises(ParseError):
        parse_event(html, URL)
