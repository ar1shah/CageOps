"""GET /events/upcoming: a batch of predictions for every scheduled bout, each with its own
data quality, cached per bout, and never an error for an empty or half-built card."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from cageops_api.cache import KEY_PREFIX
from cageops_api.config import ApiSettings
from cageops_api.main import create_app
from cageops_common.config import Settings

V1 = "20300101-000001"
NOON_ET = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
CARD = date(2026, 10, 17)
LATER = date(2026, 11, 7)
ANN, BOB, CAL, DAN, EVE, FAY, HAL, IVY = range(1, 9)


@pytest.fixture
def world(league, registry):
    for fighter_id, name in {ANN: "Ann Alpha", BOB: "Bob Bravo", CAL: "Cal Charlie",
                             DAN: "Dan Delta", EVE: "Eve Echo", FAY: "Fay Foxtrot",
                             HAL: "Hal Hotel", IVY: "Ivy India"}.items():  # fmt: skip
        league.fighter(fighter_id, name)
    # history: ANN, BOB, HAL complete; DAN's latest result has no stats; FAY has a newer fight
    league.fight(ANN, HAL, date(2025, 1, 1), winner=ANN, stats=True)
    league.fight(ANN, BOB, date(2025, 6, 1), winner=BOB, stats=True)
    league.fight(CAL, HAL, date(2025, 2, 1), winner=CAL, stats=True)
    league.fight(DAN, IVY, date(2026, 6, 6), winner=DAN, source="wikipedia")
    league.fight(FAY, IVY, date(2025, 3, 1), winner=FAY, stats=True)
    league.fight(FAY, HAL, date(2026, 8, 1), winner=HAL, stats=True)  # after FAY's build

    # scheduled card: ANN v BOB, CAL v DAN, EVE v FAY (EVE a debut, FAY stale), and a later card
    ab = league.fight(ANN, BOB, CARD, status="scheduled")
    cd = league.fight(CAL, DAN, CARD, status="scheduled")
    ef = league.fight(EVE, FAY, CARD, status="scheduled", weight_class="Welterweight")
    hi = league.fight(HAL, IVY, LATER, status="scheduled")  # no features built for this one
    league.bout_features(ab, ANN, event_date=CARD, n_fights=2, last_fight=date(2025, 6, 1))
    league.bout_features(ab, BOB, event_date=CARD, n_fights=1, last_fight=date(2025, 6, 1))
    league.bout_features(cd, CAL, event_date=CARD, n_fights=1, last_fight=date(2025, 2, 1))
    league.bout_features(cd, DAN, event_date=CARD, n_fights=1, n_with_stats=0,
                         last_fight=date(2026, 6, 6))  # fmt: skip
    league.bout_features(ef, EVE, event_date=CARD, n_fights=0, weight_class="Welterweight")
    league.bout_features(ef, FAY, event_date=CARD, n_fights=1, last_fight=date(2025, 3, 1),
                         weight_class="Welterweight")  # fmt: skip
    league.bouts = {"ab": ab, "cd": cd, "ef": ef, "hi": hi}
    registry.add(V1, seed=1, active=True)
    return league


def make_app(db, model_dir, clock=None, redis_client=None, **settings):
    return create_app(
        settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
        api_settings=ApiSettings(
            model_artifact_dir=model_dir, rate_limit_enabled=False, **settings
        ),
        engine=db, redis_client=redis_client, configure_logs=False, poll=False,
        clock=clock or (lambda: NOON_ET),
    )  # fmt: skip


@pytest.fixture
def app(db, model_dir, redis_client, world):
    return make_app(db, model_dir, redis_client=redis_client)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def bouts(body) -> dict[int, dict]:
    return {b["fight_id"]: b for e in body["events"] for b in e["bouts"]}


def metric(app, name, **labels):
    value = app.state.metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def test_lists_every_scheduled_bout_grouped_by_event_with_a_prediction_each(client, world):
    response = client.get("/events/upcoming")

    assert response.status_code == 200
    body = response.json()
    assert body["as_of"] == "2026-10-15" and body["model_version"] == V1
    assert [(e["event_date"], len(e["bouts"])) for e in body["events"]] == [
        ("2026-10-17", 3),
        ("2026-11-07", 1),
    ]
    found = bouts(body)
    for key in ("ab", "cd", "ef"):
        p = found[world.bouts[key]]["prediction"]
        assert p["win_probability"]["fighter_a"] + p["win_probability"]["fighter_b"] == (
            pytest.approx(1.0)
        )
        assert set(p["method_probabilities"]) == {"ko_tko", "submission", "decision"}
        assert p["data_quality"]["features_built_as_of"] == "2026-10-17"  # the bout's own date
    first = found[world.bouts["ab"]]
    assert (first["fighter_a"]["name"], first["fighter_b"]["name"]) == ("Ann Alpha", "Bob Bravo")
    assert first["weight_class"] == "Lightweight"


def test_title_and_rounds_are_null_never_a_default(client, world):
    for bout in bouts(client.get("/events/upcoming").json()).values():
        assert bout["is_title_fight"] is None
        assert bout["scheduled_rounds"] is None


def test_each_bout_has_its_own_data_quality(client, world):
    found = bouts(client.get("/events/upcoming").json())

    complete = found[world.bouts["ab"]]["prediction"]["data_quality"]
    assert complete["features_current"] is True and complete["rates_complete"] is True

    gap = found[world.bouts["cd"]]["prediction"]["data_quality"]  # DAN's last fight has no stats
    assert gap["features_current"] is True and gap["rates_complete"] is False
    assert gap["fighter_b"]["stats_gap_fights"] == {"career": 1, "last3": 1, "last5": 1}
    assert "0.6878" in gap["known_effect"]

    mixed = found[world.bouts["ef"]]["prediction"]["data_quality"]
    assert mixed["fighter_a"]["has_ufc_history"] is False  # EVE, a debut
    assert mixed["fighter_a"]["rates_complete"] is False
    assert mixed["fighter_b"]["features_current"] is False  # FAY fought again after the build
    assert mixed["fighter_b"]["missing_fights"] == 1
    assert "Fay Foxtrot" in mixed["stale_note"]


def test_a_bout_with_no_features_is_listed_without_a_prediction_and_does_not_fail_the_list(
    client, world
):
    response = client.get("/events/upcoming")

    assert response.status_code == 200
    unbuilt = bouts(response.json())[world.bouts["hi"]]
    assert unbuilt["prediction"] is None
    assert "features rebuild" in unbuilt["unavailable_reason"]
    assert (unbuilt["fighter_a"]["name"], unbuilt["fighter_b"]["name"]) == (
        "Hal Hotel", "Ivy India"
    )  # fmt: skip


def test_the_batch_agrees_with_the_single_prediction_for_the_same_bout(client, world, tiny):
    """A bout's win probability is the model's own for that fight_features row."""
    from sqlalchemy import text as sql

    from cageops_common.inference.rows import rows_to_arrays

    fid = world.bouts["ab"]
    with world.engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                sql("SELECT * FROM fight_features WHERE fight_id = :f ORDER BY fighter_id"),
                {"f": fid},
            ).mappings()
        ]
    values, stances = rows_to_arrays(rows)
    win, method = tiny(1)
    expected = win.predict(values[0:1], values[1:2], [stances[0]], [stances[1]], ["Lightweight"])

    got = bouts(client.get("/events/upcoming").json())[fid]["prediction"]

    assert got["win_probability"]["fighter_a"] == float(expected[0])


def test_no_scheduled_bouts_is_an_empty_list_not_an_error(db, model_dir, redis_client, registry):
    registry.add(V1, seed=1, active=True)
    with TestClient(make_app(db, model_dir, redis_client=redis_client)) as client:
        response = client.get("/events/upcoming")

    assert response.status_code == 200
    assert response.json()["events"] == []


def test_past_dated_scheduled_bouts_are_not_listed(db, model_dir, redis_client, world):
    world.fight(ANN, CAL, date(2026, 9, 1), status="scheduled")  # never got a result
    with TestClient(make_app(db, model_dir, redis_client=redis_client)) as client:
        body = client.get("/events/upcoming").json()

    assert all(e["event_date"] >= "2026-10-15" for e in body["events"])


def test_cancelled_bouts_are_not_listed(db, model_dir, redis_client, world):
    world.fight(ANN, CAL, CARD, status="cancelled")
    with TestClient(make_app(db, model_dir, redis_client=redis_client)) as client:
        found = bouts(client.get("/events/upcoming").json())

    assert len(found) == 4  # the four scheduled ones only


def test_no_model_is_503(db, model_dir, redis_client, league):
    with TestClient(make_app(db, model_dir, redis_client=redis_client)) as client:
        response = client.get("/events/upcoming")

    assert response.status_code == 503 and response.json()["code"] == "model_unavailable"


# -- batching and caching -----------------------------------------------------------------------


def test_the_model_is_called_once_per_kind_for_the_whole_card(app, world, monkeypatch):
    from cageops_common.inference import model as model_module

    calls = {"win": 0, "method": 0}
    real_win, real_method = model_module.WinLossModel.predict, model_module.MethodModel.predict

    def win(self, *a, **k):
        calls["win"] += 1
        return real_win(self, *a, **k)

    def method(self, *a, **k):
        calls["method"] += 1
        return real_method(self, *a, **k)

    monkeypatch.setattr(model_module.WinLossModel, "predict", win)
    monkeypatch.setattr(model_module.MethodModel, "predict", method)
    with TestClient(app) as client:
        calls.update(win=0, method=0)  # the model's start-up smoke test calls predict too
        assert client.get("/events/upcoming").status_code == 200

    assert calls == {"win": 1, "method": 1}  # three bouts predicted, one call each
    assert metric(app, "api_predictions_total", endpoint="upcoming") == 3


def test_the_second_request_is_served_from_the_cache_without_the_database_or_the_model(
    app, client, redis_client
):
    first = client.get("/events/upcoming")
    queries_after_first = metric(app, "api_predict_stage_seconds_count", stage="model")
    second = client.get("/events/upcoming")

    assert first.json() == second.json()
    assert first.headers["x-cache"] == "miss"
    assert second.headers["x-cache"] == "partial"  # 3 hit; the unbuilt bout is never cached
    assert metric(app, "api_predict_stage_seconds_count", stage="model") == queries_after_first
    keys = [k.decode() for k in redis_client.keys(f"{KEY_PREFIX}*")]
    assert len(keys) == 3 and all(k.endswith("src=bout") for k in keys)
    assert any("asof=2026-10-17" in k for k in keys)
    assert all(":x:x:" in k for k in keys)  # rounds and title are "unknown", not a value


def test_one_pipelined_write_not_one_per_bout(app, client, redis_client, monkeypatch):
    executed = []
    real_pipeline = redis_client.pipeline

    def pipeline(*a, **k):
        pipe = real_pipeline(*a, **k)
        real_execute = pipe.execute

        def execute(*ea, **ek):
            executed.append(len(pipe.command_stack))
            return real_execute(*ea, **ek)

        pipe.execute = execute
        return pipe

    monkeypatch.setattr(redis_client, "pipeline", pipeline)
    client.get("/events/upcoming")

    assert executed == [3]


def test_a_cached_bout_and_a_fresh_one_mix_in_one_response(app, client, world):
    client.get("/events/upcoming")
    # a new bout appears on the card with features built
    extra = world.fight(BOB, CAL, CARD, status="scheduled")
    world.bout_features(extra, BOB, event_date=CARD, n_fights=1, last_fight=date(2025, 6, 1))
    world.bout_features(extra, CAL, event_date=CARD, n_fights=1, last_fight=date(2025, 2, 1))

    response = client.get("/events/upcoming")

    assert response.headers["x-cache"] == "partial"
    assert bouts(response.json())[extra]["prediction"] is not None


def test_redis_down_still_lists_every_bout(db, model_dir, world):
    import redis

    dead = redis.Redis(
        host="127.0.0.1", port=1, socket_timeout=0.05, socket_connect_timeout=0.05,
        retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
    )  # fmt: skip
    app = make_app(db, model_dir, redis_client=dead)
    with TestClient(app) as client:
        response = client.get("/events/upcoming")

    assert response.status_code == 200
    assert response.headers["x-cache"] == "bypass"
    assert len([b for b in bouts(response.json()).values() if b["prediction"]]) == 3
    assert metric(app, "api_redis_errors_total", op="cache_get") == 1


def test_a_new_model_version_is_a_new_set_of_keys(app, client, registry, redis_client):
    client.get("/events/upcoming")
    registry.add("20300101-000002", seed=2, active=True)
    assert app.state.model_manager.refresh() == "loaded"

    body = client.get("/events/upcoming").json()

    assert body["model_version"] == "20300101-000002"
    assert len(redis_client.keys(f"{KEY_PREFIX}*")) == 6  # three per version


# -- late cards ---------------------------------------------------------------------------------


def test_yesterdays_card_is_listed_while_a_bout_on_it_is_still_scheduled(
    db, model_dir, redis_client, world
):
    """A Las Vegas card ends after midnight New York time and its results arrive hours later: at
    00:30 on Sunday the Saturday card's unfinished bouts are still upcoming."""
    saturday = date(2026, 10, 17)
    finished = world.fight(ANN, CAL, saturday, winner=ANN, stats=True)  # result already in
    late = world.fight(HAL, IVY, saturday, status="scheduled")  # main event, no result yet
    world.bout_features(late, HAL, event_date=saturday, n_fights=1, last_fight=date(2025, 2, 1))
    world.bout_features(late, IVY, event_date=saturday, n_fights=1, last_fight=date(2026, 6, 6))
    too_old = world.fight(BOB, DAN, date(2026, 10, 16), status="scheduled")  # two days back

    at_0030 = datetime(2026, 10, 18, 4, 30, tzinfo=UTC)  # 00:30 on Sunday in New York
    with TestClient(make_app(db, model_dir, clock=lambda: at_0030, redis_client=redis_client)) as c:
        body = c.get("/events/upcoming").json()

    assert body["as_of"] == "2026-10-18"
    listed = bouts(body)
    assert late in listed and listed[late]["prediction"] is not None
    assert finished not in listed  # a completed bout is not "upcoming"
    assert too_old not in listed  # only one day of grace
    # the fixture's own card is dated the same Saturday and is still scheduled too, and the
    # later card (2026-11-07) is simply upcoming; nothing dated before the 17th is
    assert [e["event_date"] for e in body["events"]] == ["2026-10-17", "2026-11-07"]
    assert {world.bouts["ab"], world.bouts["cd"], world.bouts["ef"], late} <= set(listed)


def test_the_same_card_is_gone_once_its_bouts_are_all_completed(db, model_dir, redis_client, world):
    saturday = date(2026, 10, 17)
    done = world.fight(HAL, IVY, saturday, winner=HAL, stats=True)
    at_0030 = datetime(2026, 10, 18, 4, 30, tzinfo=UTC)
    with TestClient(make_app(db, model_dir, clock=lambda: at_0030, redis_client=redis_client)) as c:
        body = c.get("/events/upcoming").json()

    listed = bouts(body)
    assert done not in listed  # completed: not upcoming, whatever its date
    assert all(listed[b]["prediction"] is not None for b in (world.bouts[k] for k in ("ab", "cd")))


def test_and_a_day_later_it_is_not_listed_even_if_a_bout_was_never_resolved(
    db, model_dir, redis_client, world
):
    stuck = world.fight(HAL, IVY, date(2026, 10, 17), status="scheduled")
    monday = datetime(2026, 10, 19, 4, 30, tzinfo=UTC)  # 00:30 on Monday: two days after
    with TestClient(make_app(db, model_dir, clock=lambda: monday, redis_client=redis_client)) as c:
        listed = bouts(c.get("/events/upcoming").json())

    assert stuck not in listed


def test_at_midnight_new_york_the_boundary_is_new_york_not_utc(db, model_dir, redis_client, world):
    saturday_card = world.fight(HAL, IVY, date(2026, 10, 17), status="scheduled")
    for utc, expected in (
        (datetime(2026, 10, 18, 3, 59, tzinfo=UTC), True),  # 23:59 Saturday: today's card
        (datetime(2026, 10, 18, 4, 1, tzinfo=UTC), True),  # 00:01 Sunday: yesterday's, still due
        (datetime(2026, 10, 19, 3, 59, tzinfo=UTC), True),  # 23:59 Sunday: one day of grace left
        (datetime(2026, 10, 19, 4, 1, tzinfo=UTC), False),  # 00:01 Monday: grace over
    ):
        with TestClient(
            make_app(db, model_dir, clock=lambda utc=utc: utc, redis_client=redis_client)
        ) as c:
            listed = saturday_card in bouts(c.get("/events/upcoming").json())
        assert listed is expected, utc
