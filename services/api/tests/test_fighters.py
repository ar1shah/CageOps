"""GET /fighters and GET /fighters/{id}."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from cageops_api.config import ApiSettings
from cageops_api.main import create_app
from cageops_common.config import Settings

TODAY = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)  # 12:00 in New York


@pytest.fixture
def client(db, model_dir):
    app = create_app(
        settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
        api_settings=ApiSettings(
            model_artifact_dir=model_dir, cache_enabled=False, rate_limit_enabled=False
        ),
        engine=db,
        configure_logs=False,
        poll=False,
        clock=lambda: TODAY,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def cast(league):
    """Names chosen to exercise ordering and escaping."""
    names = {
        1: "Alex Pereira",
        2: "Alexander Volkov",
        3: "Alex Perez",
        4: "Max Alexander",
        5: "Ian Garry",
        6: "100% Fighter",
        7: "A_B Smith",
        8: "AXB Smith",
    }
    for fighter_id, name in names.items():
        league.fighter(fighter_id, name, dob=date(1990, 6, 15) if fighter_id == 1 else None)
    league.fight(
        1, 5, date(2024, 1, 1), winner=1, method="ko_tko", weight_class="Light Heavyweight"
    )
    league.fight(1, 2, date(2025, 3, 1), winner=2, method="decision", weight_class="Heavyweight")
    league.fight(1, 3, date(2026, 5, 1), winner=1, method="submission", weight_class="Catch Weight")
    league.fight(1, 5, date(2026, 8, 1), outcome="draw", weight_class="Light Heavyweight")
    return league


# -- search -------------------------------------------------------------------------------------


def names(response) -> list[str]:
    return [item["name"] for item in response.json()["items"]]


def test_search_is_case_insensitive_and_orders_exact_then_prefix_then_the_rest(client, cast):
    response = client.get("/fighters", params={"search": "alexander"})

    assert response.status_code == 200
    body = response.json()
    assert names(response) == ["Alexander Volkov", "Max Alexander"]  # prefix before contains
    assert (body["total"], body["limit"], body["offset"]) == (2, 20, 0)

    exact = client.get("/fighters", params={"search": "ALEX PEREZ"})
    assert names(exact) == ["Alex Perez"]


def test_an_exact_match_comes_before_longer_names_that_start_the_same(client, cast):
    response = client.get("/fighters", params={"search": "Alex Pere"})

    assert names(response) == ["Alex Pereira", "Alex Perez"]
    assert names(client.get("/fighters", params={"search": "alex perez"})) == ["Alex Perez"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [("100%", ["100% Fighter"]), ("A_B", ["A_B Smith"]), ("%ex%", [])],
)
def test_like_wildcards_in_the_query_mean_themselves(client, cast, query, expected):
    response = client.get("/fighters", params={"search": query})

    assert response.status_code == 200 and names(response) == expected
    assert response.json()["total"] == len(expected)


def test_a_backslash_in_the_query_is_just_a_character(client, cast):
    assert client.get("/fighters", params={"search": "a\\b"}).json()["total"] == 0


def test_paging_is_stable_and_total_counts_every_match(client, cast):
    first = client.get("/fighters", params={"search": "alex", "limit": 2, "offset": 0}).json()
    second = client.get("/fighters", params={"search": "alex", "limit": 2, "offset": 2}).json()
    everything = client.get("/fighters", params={"search": "alex", "limit": 50}).json()

    assert first["total"] == second["total"] == everything["total"] == 4
    paged = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert paged == [i["id"] for i in everything["items"]]  # pages concatenate to the whole list
    assert len(set(paged)) == 4


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"search": "a"},
        {"search": "ab", "limit": 0},
        {"search": "ab", "limit": 51},
        {"search": "ab", "offset": -1},
        {"search": "x" * 101},
    ],
)
def test_bad_search_parameters_are_422_with_the_error_shape(client, params):
    response = client.get("/fighters", params=params)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error" and body["detail"] and body["errors"]


def test_a_search_result_carries_division_and_record_facts(client, cast):
    (item,) = client.get("/fighters", params={"search": "Alex Pereira"}).json()["items"]

    assert item["id"] == 1
    # the most recent bout is a 2026-08-01 draw at Light Heavyweight; the Catch Weight fight
    # before it is not a division
    assert item["weight_class"] == "Light Heavyweight"
    assert item["n_ufc_fights"] == 4
    assert item["last_fight_date"] == "2026-08-01"


def test_a_fighter_with_no_fights_has_null_facts_and_zero_fights(client, cast):
    (item,) = client.get("/fighters", params={"search": "Max Alexander"}).json()["items"]

    assert (item["weight_class"], item["n_ufc_fights"], item["last_fight_date"]) == (None, 0, None)


# -- profile ------------------------------------------------------------------------------------


def test_profile_has_bio_record_and_the_last_fights_newest_first(client, cast):
    response = client.get("/fighters/1")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Alex Pereira" and body["stance"] == "Orthodox"
    assert body["date_of_birth"] == "1990-06-15"
    assert body["age_years"] == 36  # as of the pinned 2026-10-15
    assert body["weight_class"] == "Light Heavyweight"
    assert body["record"] == {
        "wins": 2, "losses": 1, "draws": 1, "no_contests": 0, "unknown_results": 0,
        "wins_by_ko_tko": 1, "wins_by_submission": 1, "wins_by_decision": 0, "wins_by_other": 0,
    }  # fmt: skip
    dates = [f["event_date"] for f in body["last_fights"]]
    assert dates == ["2026-08-01", "2026-05-01", "2025-03-01", "2024-01-01"]
    assert [f["result"] for f in body["last_fights"]] == ["draw", "win", "loss", "win"]
    assert body["last_fights"][1]["opponent"] == {"id": 3, "name": "Alex Perez"}
    assert body["last_fights"][1]["method"] == "submission"


def test_the_record_is_from_this_fighters_side_of_each_fight(client, cast):
    body = client.get("/fighters/2").json()

    assert body["record"]["wins"] == 1 and body["record"]["losses"] == 0
    assert body["last_fights"][0]["result"] == "win"
    assert body["last_fights"][0]["opponent"]["name"] == "Alex Pereira"


def test_an_unknown_fighter_is_404_with_the_error_shape(client, cast):
    response = client.get("/fighters/99999")

    assert response.status_code == 404
    assert response.json() == {"detail": "no fighter with id 99999", "code": "fighter_not_found"}


def test_a_non_integer_id_is_422(client, cast):
    assert client.get("/fighters/abc").status_code == 422


def test_age_is_whole_years_and_turns_on_the_birthday(client, league):
    league.fighter(1, "Birthday Boy", dob=date(1990, 10, 15))
    league.fighter(2, "Day Before", dob=date(1990, 10, 16))
    league.fighter(3, "No Date", dob=None)

    assert client.get("/fighters/1").json()["age_years"] == 36
    assert client.get("/fighters/2").json()["age_years"] == 35
    assert client.get("/fighters/3").json()["age_years"] is None


def test_the_templated_route_is_what_the_metrics_see(client, cast):
    client.get("/fighters/1")
    client.get("/fighters/2")
    client.get("/fighters/99999")

    text = client.get("/metrics").text
    assert 'route="/fighters/{fighter_id}",status="200"' in text
    assert 'route="/fighters/{fighter_id}",status="404"' in text
    assert '/fighters/1"' not in text and "/fighters/99999" not in text


def test_the_pool_is_visible_in_metrics(client, cast):
    client.get("/fighters/1")

    text = client.get("/metrics").text
    for name in (
        "api_db_pool_checked_out",
        "api_db_pool_size",
        "api_db_pool_overflow",
        "api_db_pool_checkout_seconds_count",
        "api_db_pool_checkout_timeouts_total",
    ):
        assert name in text, name
