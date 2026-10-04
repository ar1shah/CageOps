import threading

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

    assert get(base, "/__stats").json() == {"mode": "ok", "requests": 2}

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
