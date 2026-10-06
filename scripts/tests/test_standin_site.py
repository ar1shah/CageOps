import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from standin_site import DEFAULT_FIXTURES, REAL_HOST, make_server

BURNS_EVENT = "c3ac8d0da7b05772"
UPCOMING_EVENT = "7f98d9d5a10fa25c"


@pytest.fixture
def site():
    server, state = make_server(0)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, state
    server.shutdown()
    server.server_close()


def get(base: str, path: str) -> httpx.Response:
    return httpx.get(base + path, timeout=3)


def test_robots_allows_everything(site):
    base, _ = site

    response = get(base, "/robots.txt")

    assert response.status_code == 200 and "Allow: /" in response.text


@pytest.mark.parametrize(
    ("path", "fixture"),
    [
        ("/statistics/events/completed", "events_completed"),
        ("/statistics/events/upcoming", "events_upcoming"),
        (f"/event-details/{BURNS_EVENT}", f"event_{BURNS_EVENT}"),
        (f"/event-details/{UPCOMING_EVENT}", f"event_upcoming_{UPCOMING_EVENT}"),
        ("/fight-details/32054bf2b36b0e47", "fight_32054bf2b36b0e47"),
        ("/fighter-details/23024fdfc966410a", "fighter_23024fdfc966410a"),
    ],
)
def test_every_fixture_is_served_at_the_real_sites_path(site, path, fixture):
    base, _ = site
    saved = (DEFAULT_FIXTURES / f"{fixture}.html").read_text(encoding="utf-8")
    expected = REAL_HOST.sub(base, saved)  # only the links differ

    response = get(base, path)

    assert response.status_code == 200 and response.text == expected


def test_a_page_with_no_fixture_is_a_404(site):
    base, _ = site

    assert get(base, "/fight-details/0000000000000000").status_code == 404


def test_the_second_list_page_is_empty_so_a_crawl_stops(site):
    base, _ = site

    page_two = get(base, "/statistics/events/completed?page=2").text

    assert "<tbody></tbody>" in page_two


def test_fail_mode_answers_500_for_content_but_not_robots(site):
    base, _ = site
    httpx.post(base + "/__mode/fail")

    assert get(base, f"/event-details/{BURNS_EVENT}").status_code == 500
    assert get(base, "/robots.txt").status_code == 200

    httpx.post(base + "/__mode/ok")
    assert get(base, f"/event-details/{BURNS_EVENT}").status_code == 200


def test_challenge_mode_serves_the_saved_challenge_page(site):
    base, _ = site
    challenge = (DEFAULT_FIXTURES / "challenge_2026-10-03.html").read_text(encoding="utf-8")
    httpx.post(base + "/__mode/challenge")

    response = get(base, f"/event-details/{BURNS_EVENT}")

    assert response.status_code == 200 and response.text == challenge


def test_the_request_counter_counts_content_pages_and_resets(site):
    base, _ = site
    get(base, "/robots.txt")  # not counted
    get(base, f"/event-details/{BURNS_EVENT}")
    get(base, "/fight-details/32054bf2b36b0e47")

    stats = get(base, "/__stats").json()
    assert stats["mode"] == "ok" and stats["requests"] == 2 and stats["distinct_pages"] == 2

    httpx.post(base + "/__reset")
    assert get(base, "/__stats").json()["requests"] == 0


def test_an_unknown_mode_is_rejected(site):
    base, _ = site

    response = httpx.post(base + "/__mode/explode")

    assert response.status_code == 404


def test_links_inside_pages_point_back_at_the_stand_in_not_the_real_site(site):
    base, _ = site

    page = get(base, f"/event-details/{BURNS_EVENT}").text

    assert f"{base}/fight-details/" in page
    assert "ufcstats.com" not in page


# -- the replay server: synthetic corpus and simulated latency ------------------------------


@pytest.fixture
def replay():
    server, state = make_server(0, synthetic_events=5, latency_ms=0.0)
    state.quiet = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()
    server.server_close()


def test_the_synthetic_events_replace_the_saved_completed_list(replay):
    base, state = replay

    first = get(base, "/statistics/events/completed").text
    second = get(base, "/statistics/events/completed?page=2").text

    assert "UFC Replay Night 05" in first and "Burns vs. Malott" not in first
    assert "<tbody>" in second and "Replay Night" not in second  # past the end: no events
    assert state.corpus is not None


def test_every_synthetic_page_is_served_with_links_pointing_back_at_the_server(replay):
    base, state = replay
    event_id = state.corpus.event_id(0)

    page = get(base, f"/event-details/{event_id}")

    assert page.status_code == 200
    assert f"{base}/fight-details/" in page.text and "ufcstats.com" not in page.text
    for fight_id in state.corpus.fight_ids(0):
        assert get(base, f"/fight-details/{fight_id}").status_code == 200
    for fighter_id in state.corpus.fighter_ids(0):
        assert get(base, f"/fighter-details/{fighter_id}").status_code == 200


def test_the_upcoming_pages_stay_as_saved(replay):
    base, _ = replay

    assert get(base, "/statistics/events/upcoming").status_code == 200
    assert get(base, "/event-details/7f98d9d5a10fa25c").status_code == 200


def test_a_page_that_does_not_exist_is_404_in_corpus_mode(replay):
    base, _ = replay

    assert get(base, "/fight-details/0000000000000000").status_code == 404


def test_distinct_pages_counts_each_path_once(replay):
    base, state = replay
    for _ in range(3):
        get(base, f"/event-details/{state.corpus.event_id(1)}")
    get(base, f"/event-details/{state.corpus.event_id(2)}")

    stats = get(base, "/__stats").json()

    assert stats["requests"] == 4 and stats["distinct_pages"] == 2


def test_latency_makes_each_response_take_about_that_long():
    server, state = make_server(0, synthetic_events=2, latency_ms=150, jitter=0.0)
    state.quiet = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        started = time.monotonic()
        httpx.get(base + f"/event-details/{state.corpus.event_id(0)}", timeout=3)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()

    assert 0.14 <= elapsed < 0.6


def test_jitter_is_bounded_and_repeatable_for_a_seed():
    def delays(seed):
        _, state = make_server(0, synthetic_events=1, latency_ms=250, jitter=0.2, seed=seed)
        return [state.delay_s() for _ in range(200)]

    first, again, other = delays(7), delays(7), delays(8)

    assert first == again and first != other
    assert all(0.2 <= d <= 0.3 for d in first)  # 250 ms +/- 20%
    assert max(first) - min(first) > 0.05  # it actually varies


def test_responses_overlap_like_a_real_site():
    """8 requests of 200 ms each, sent at once, finish in well under 8 x 200 ms."""
    server, state = make_server(0, synthetic_events=2, latency_ms=200, jitter=0.0)
    state.quiet = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    path = f"/event-details/{state.corpus.event_id(0)}"
    try:
        with ThreadPoolExecutor(8) as pool:
            started = time.monotonic()
            responses = list(pool.map(lambda _: httpx.get(base + path, timeout=5), range(8)))
            elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()

    assert all(r.status_code == 200 for r in responses)
    assert elapsed < 0.8  # sequential would be 1.6 s


def test_fail_and_challenge_modes_still_work_with_the_synthetic_corpus(replay):
    base, state = replay
    path = f"/event-details/{state.corpus.event_id(0)}"

    httpx.post(base + "/__mode/fail")
    assert get(base, path).status_code == 500
    httpx.post(base + "/__mode/challenge")
    challenge = (DEFAULT_FIXTURES / "challenge_2026-10-03.html").read_text(encoding="utf-8")
    assert get(base, path).text == challenge
    httpx.post(base + "/__mode/ok")
    assert get(base, path).status_code == 200


def test_robots_is_not_counted_and_not_delayed():
    _, state = make_server(0, synthetic_events=1, latency_ms=500)

    status, _ = state.respond("/robots.txt")

    assert status == 200 and state.requests == 0
