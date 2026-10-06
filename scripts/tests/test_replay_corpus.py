"""The synthetic corpus must be a faithful stand-in for the saved pages: every page parses with the
real parsers, links agree across pages, and the numbers add up."""

from datetime import timedelta

import pytest
from replay_corpus import DEFAULT_END, Corpus

from cageops_scraper.parsers.event import parse_event
from cageops_scraper.parsers.events import parse_events_list
from cageops_scraper.parsers.fight import parse_fight
from cageops_scraper.parsers.fighter import parse_fighter

BASE = "http://ufcstats.com"


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    return Corpus(events=52)


def test_the_corpus_is_deterministic():
    a, b = Corpus(events=5), Corpus(events=5)

    assert a.paths() == b.paths()
    assert all(a.render(p) == b.render(p) for p in a.paths())


def test_every_path_renders_and_unknown_paths_do_not(corpus):
    assert all(corpus.render(path) is not None for path in corpus.paths())
    assert corpus.render("/fight-details/0000000000000000") is None
    assert corpus.render("/event-details/not-an-id") is None
    # an event id used as a fight id is not a page
    assert corpus.render(f"/fight-details/{corpus.event_id(0)}") is None


def test_a_year_is_52_events_and_about_680_requests(corpus):
    assert corpus.events == 52
    assert corpus.expected_requests() == 52 * 13 + 4  # 13 pages per event, 3 list pages + 1 empty
    assert len(set(corpus.paths())) == len(corpus.paths())  # no two pages share a path


def test_ids_are_unique_across_events(corpus):
    ids = [
        i
        for k in range(52)
        for i in (corpus.event_id(k), *corpus.fight_ids(k), *corpus.fighter_ids(k))
    ]

    assert len(ids) == len(set(ids)) == 52 * 13


def test_the_events_list_pages(corpus):
    pages = [
        parse_events_list(corpus.render(p), BASE + p) for p in corpus.paths() if "completed" in p
    ]

    assert [len(p) for p in pages] == [25, 25, 3, 0]  # 52 events + the next-event row, then empty
    assert pages[0][0].is_next_marker and not any(r.is_next_marker for r in pages[1])
    dates = [r.event_date for page in pages for r in page if not r.is_next_marker]
    assert dates == sorted(dates, reverse=True)  # newest first, like the real list
    assert dates[0] == DEFAULT_END and dates[-1] == DEFAULT_END - timedelta(weeks=51)
    assert pages[0][0].event_date > DEFAULT_END  # the marker row is in the future


def test_event_pages_carry_their_own_date_id_and_four_bouts(corpus):
    for k in (0, 1, 25, 51):
        event_id = corpus.event_id(k)
        event = parse_event(
            corpus.render(f"/event-details/{event_id}"), f"{BASE}/event-details/{event_id}"
        )

        assert event.event_date == corpus.event_date(k)
        assert event.name == corpus.event_name(k)
        assert {b.fight_id for b in event.bouts} == set(corpus.fight_ids(k))
        fighters = {f.ufcstats_id for b in event.bouts for f in b.fighters}
        assert fighters == set(corpus.fighter_ids(k))


def test_fight_pages_point_back_at_their_own_event_and_fighters(corpus):
    k = 7
    for fight_id in corpus.fight_ids(k):
        fight = parse_fight(
            corpus.render(f"/fight-details/{fight_id}"), f"{BASE}/fight-details/{fight_id}"
        )

        assert fight.event_id == corpus.event_id(k)
        assert {f.ufcstats_id for f in fight.fighters} <= set(corpus.fighter_ids(k))
        assert fight.anomalies == []


def test_every_fighter_page_parses(corpus):
    for fighter_id in corpus.fighter_ids(3):
        parsed = parse_fighter(
            corpus.render(f"/fighter-details/{fighter_id}"), f"{BASE}/fighter-details/{fighter_id}"
        )

        assert parsed.name


def test_no_synthetic_page_links_to_another_synthetic_event(corpus):
    """Links stay inside the event they belong to."""
    other = {i for k in range(1, 52) for i in (corpus.event_id(k), *corpus.fight_ids(k))}
    html = corpus.render(f"/event-details/{corpus.event_id(0)}")

    assert not any(i in html for i in other)
