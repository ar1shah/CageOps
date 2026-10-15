"""POST /predict: probabilities, mirroring, contributions, context, and data quality.

The league (fighters, fights, `fighter_features_current` rows) is written straight into the
tables, and the model is a ~10-tree booster. What these tests pin is behaviour, not accuracy.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from unittest import mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from cageops_api.config import ApiSettings
from cageops_api.main import create_app
from cageops_common.config import Settings
from cageops_common.inference.asof import date_relative_features
from cageops_common.inference.features import NUMERIC_FEATURES

V1, V2 = "20300101-000001", "20300101-000002"
NOON_ET = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)

ANN, BOB, CAL, DAN, EVE, FAY, GUS, HAL, IVY = 1, 2, 3, 4, 5, 6, 7, 8, 9


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(NOON_ET)


@pytest.fixture
def world(league, registry):
    """Nine fighters, each there to show one thing:

    ANN, BOB, HAL  lightweights with complete stats
    CAL            a welterweight, complete
    DAN            2 fights, the latest a Wikipedia result with no stats (rates incomplete)
    EVE            a debut: no fights
    FAY            her features were built before her last fight (stale)
    GUS            exists, has no feature row
    IVY            DAN's opponent
    """
    names = {ANN: "Ann Alpha", BOB: "Bob Bravo", CAL: "Cal Charlie", DAN: "Dan Delta",
             EVE: "Eve Echo", FAY: "Fay Foxtrot", GUS: "Gus Golf", HAL: "Hal Hotel",
             IVY: "Ivy India"}  # fmt: skip
    for fighter_id, name in names.items():
        league.fighter(fighter_id, name, stance="Southpaw" if fighter_id % 3 == 0 else "Orthodox")
    welter = "Welterweight"
    league.fight(ANN, HAL, date(2025, 1, 1), winner=ANN, stats=True)
    league.fight(ANN, BOB, date(2025, 6, 1), winner=BOB, stats=True)
    league.fight(BOB, HAL, date(2025, 9, 1), winner=BOB, stats=True)
    league.fight(CAL, FAY, date(2025, 2, 1), winner=CAL, stats=True, weight_class=welter)
    league.fight(CAL, FAY, date(2025, 12, 1), winner=FAY, stats=True, weight_class=welter)
    league.fight(DAN, IVY, date(2026, 1, 10), winner=DAN, stats=True)
    league.fight(DAN, IVY, date(2026, 6, 6), winner=DAN, stats=False, source="wikipedia")

    league.current(ANN, n_fights=2, last_fight=date(2025, 6, 1))
    league.current(BOB, n_fights=2, last_fight=date(2025, 9, 1))
    league.current(HAL, n_fights=2, last_fight=date(2025, 9, 1))
    league.current(CAL, n_fights=2, last_fight=date(2025, 12, 1))
    league.current(DAN, n_fights=2, n_with_stats=1, last_fight=date(2026, 6, 6))
    league.current(IVY, n_fights=2, n_with_stats=1, last_fight=date(2026, 6, 6))
    league.current(EVE, n_fights=0, last_fight=None)
    league.current(FAY, n_fights=1, last_fight=date(2025, 2, 1))  # built before 2025-12-01
    registry.add(V1, seed=1, active=True)
    return league


@pytest.fixture
def app(db, model_dir, clock):
    return create_app(
        settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
        api_settings=ApiSettings(model_artifact_dir=model_dir),
        engine=db,
        configure_logs=False,
        poll=False,
        clock=clock,
    )


@pytest.fixture
def client(app, world):
    with TestClient(app) as test_client:
        yield test_client


def predict(client, a, b, **extra):
    return client.post("/predict", json={"fighter_a_id": a, "fighter_b_id": b, **extra})


def counter(app, name, **labels):
    value = app.state.metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


# -- the numbers --------------------------------------------------------------------------------


def test_the_two_win_probabilities_sum_to_one_and_method_probabilities_too(client):
    body = predict(client, ANN, BOB).json()

    p = body["win_probability"]
    assert p["fighter_a"] + p["fighter_b"] == pytest.approx(1.0, abs=1e-12)
    assert 0 < p["fighter_a"] < 1
    m = body["method_probabilities"]
    assert m["ko_tko"] + m["submission"] + m["decision"] == pytest.approx(1.0, abs=1e-9)
    assert body["model_version"] == V1
    assert (body["fighter_a"], body["fighter_b"]) == (
        {"id": ANN, "name": "Ann Alpha"},
        {"id": BOB, "name": "Bob Bravo"},
    )


def test_swapping_the_fighters_returns_the_exact_mirror_image(client):
    forward = predict(client, ANN, HAL).json()
    backward = predict(client, HAL, ANN).json()

    assert backward["fighter_a"] == forward["fighter_b"]
    assert backward["fighter_b"] == forward["fighter_a"]
    assert backward["win_probability"]["fighter_a"] == forward["win_probability"]["fighter_b"]
    assert backward["win_probability"]["fighter_b"] == forward["win_probability"]["fighter_a"]
    assert backward["method_probabilities"] == forward["method_probabilities"]
    assert backward["matchup"] == forward["matchup"]
    assert len(forward["top_features"]) == len(backward["top_features"]) > 0
    for f, b in zip(forward["top_features"], backward["top_features"], strict=True):
        assert b["feature"] == f["feature"]
        assert (b["value_a"], b["value_b"]) == (f["value_b"], f["value_a"])
        assert b["contribution"] == -f["contribution"]
        assert b["favors"] != f["favors"]
    assert backward["data_quality"]["fighter_a"] == forward["data_quality"]["fighter_b"]
    assert backward["data_quality"]["fighter_b"] == forward["data_quality"]["fighter_a"]


def test_the_headline_is_the_models_own_prediction_for_these_inputs(client, tiny, world):
    """Recompute the answer independently of the API's code path: same rows, same functions."""
    from cageops_common.inference.rows import rows_to_arrays, with_date_relative

    with world.engine.connect() as conn:
        from cageops_api import queries

        rows = queries.current_feature_rows(conn, [ANN, BOB])
    as_of = date(2026, 10, 15)
    values, stances = rows_to_arrays([with_date_relative(rows[i], as_of) for i in (ANN, BOB)])
    win, method = tiny(1)
    expected = win.predict(values[0:1], values[1:2], [stances[0]], [stances[1]], ["Lightweight"])
    expected_method = method.predict(
        values[0:1], values[1:2], [stances[0]], [stances[1]], ["Lightweight"]
    )[0]

    body = predict(client, ANN, BOB).json()

    assert body["win_probability"]["fighter_a"] == float(expected[0])
    assert body["method_probabilities"]["ko_tko"] == float(expected_method[0])
    assert body["method_probabilities"]["submission"] == float(expected_method[1])
    assert body["method_probabilities"]["decision"] == float(expected_method[2])


def test_top_features_are_sorted_by_size_and_their_direction_matches_their_sign(client):
    body = predict(client, ANN, BOB).json()

    top = body["top_features"]
    assert 0 < len(top) <= 5
    sizes = [abs(c["contribution"]) for c in top]
    assert sizes == sorted(sizes, reverse=True)
    for c in top:
        assert c["favors"] == ("fighter_a" if c["contribution"] > 0 else "fighter_b")
        assert c["kind"] in ("numeric", "categorical")
        if c["kind"] == "numeric":
            assert c["feature"] in NUMERIC_FEATURES
        else:
            assert c["feature"] in ("stance_matchup", "weight_class")


def test_no_response_carries_odds_or_anything_that_reads_like_a_pick(client):
    text = json.dumps(predict(client, ANN, BOB).json()).lower()

    for word in ("odds", "bet ", "wager", "pick", "moneyline", "parlay"):
        assert word not in text
    assert "not betting advice" in text


def test_the_disclaimer_is_exactly_this_sentence(client):
    body = predict(client, ANN, BOB).json()

    assert (
        body["disclaimer"] == "A statistical estimate based on past UFC fights. Not betting advice."
    )


# -- as-of date ---------------------------------------------------------------------------------


def test_date_relative_features_are_derived_for_todays_date_from_stored_inputs(app, client):
    seen = {}
    from cageops_api import predict as predict_module

    real = predict_module.rows_to_arrays

    def spy(rows):
        seen["rows"] = rows
        return real(rows)

    with mock.patch.object(predict_module, "rows_to_arrays", spy):
        body = predict(client, ANN, BOB).json()

    assert body["matchup"]["as_of"] == "2026-10-15"
    ann, bob = seen["rows"]  # canonical order: smaller id first
    expected = date_relative_features(date(2026, 10, 15), date(1990, 1, 1), date(2025, 6, 1))
    assert ann["age_days"] == expected["age_days"]
    assert ann["days_since_last_fight"] == expected["days_since_last_fight"] == 501
    assert bob["days_since_last_fight"] == (date(2026, 10, 15) - date(2025, 9, 1)).days


def test_asked_a_day_later_the_same_stored_row_is_one_day_older(client, clock):
    from cageops_api import predict as predict_module

    captured: list[list[dict]] = []
    real = predict_module.rows_to_arrays

    def spy(rows):
        captured.append(rows)
        return real(rows)

    with mock.patch.object(predict_module, "rows_to_arrays", spy):
        predict(client, ANN, BOB)
        clock.now += timedelta(days=1)
        later = predict(client, ANN, BOB).json()

    first, second = captured
    assert second[0]["age_days"] == first[0]["age_days"] + 1
    assert second[0]["days_since_last_fight"] == first[0]["days_since_last_fight"] + 1
    assert later["matchup"]["as_of"] == "2026-10-16"


def test_today_is_the_new_york_date_not_the_utc_date(client, clock):
    clock.now = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)  # 23:30 on the 15th in New York

    assert predict(client, ANN, BOB).json()["matchup"]["as_of"] == "2026-10-15"

    clock.now = datetime(2026, 10, 16, 4, 30, tzinfo=UTC)  # 00:30 on the 16th

    assert predict(client, ANN, BOB).json()["matchup"]["as_of"] == "2026-10-16"


# -- data quality -------------------------------------------------------------------------------


def test_complete_current_fighters_are_reported_as_both_and_carry_no_caveat(app, client):
    q = predict(client, ANN, BOB).json()["data_quality"]

    assert q["features_current"] is True and q["rates_complete"] is True
    assert q["known_effect"] is None and q["stale_note"] is None
    assert q["fighter_a"] == {
        "n_prior_fights": 2, "has_ufc_history": True, "features_current": True,
        "missing_fights": 0, "rates_complete": True,
        "stats_gap_fights": {"career": 0, "last3": 0, "last5": 0},
    }  # fmt: skip
    assert q["features_built_as_of"] == "2026-10-15"
    assert counter(app, "api_predictions_with_stats_gap_total") == 0


def test_a_fighter_whose_recent_fight_has_no_stats_is_current_but_not_rates_complete(app, client):
    response = predict(client, DAN, ANN)

    assert response.status_code == 200  # a gap never fails the prediction
    body = response.json()
    q = body["data_quality"]
    assert q["fighter_a"]["features_current"] is True  # every completed fight is counted
    assert q["fighter_a"]["rates_complete"] is False  # but one of them has no per-round stats
    assert q["fighter_a"]["stats_gap_fights"] == {"career": 1, "last3": 1, "last5": 1}
    assert q["fighter_b"]["rates_complete"] is True
    assert q["features_current"] is True and q["rates_complete"] is False
    assert q["known_effect"] and q["stale_note"] is None
    assert body["win_probability"]["fighter_a"] + body["win_probability"]["fighter_b"] == (
        pytest.approx(1.0)
    )
    assert counter(app, "api_predictions_with_stats_gap_total") == 1
    assert counter(app, "api_predictions_features_stale_total") == 0


def test_the_known_effect_quotes_only_the_measured_numbers(client):
    note = predict(client, DAN, ANN).json()["data_quality"]["known_effect"]

    assert "0.6878" in note and "0.6566" in note and "223" in note and "330" in note
    assert "2026-05-17" in note and "within noise" in note
    import re

    figures = set(re.findall(r"\d+(?:\.\d+)?", note.replace("2026-05-17", "")))
    assert figures == {"0.6878", "223", "0.6566", "330"}  # no invented number


def test_a_fighter_with_a_completed_fight_the_features_lack_is_flagged_not_called_fresh(
    app, client
):
    response = predict(client, CAL, FAY)

    assert response.status_code == 200
    q = response.json()["data_quality"]
    assert q["features_current"] is False
    assert q["fighter_a"]["features_current"] is True  # CAL
    assert q["fighter_b"]["features_current"] is False  # FAY: row counts 1, she has 2
    assert q["fighter_b"]["missing_fights"] == 1
    assert "Fay Foxtrot" in q["stale_note"]
    assert counter(app, "api_predictions_features_stale_total") == 1


def test_a_fight_dated_today_becomes_history_at_midnight_new_york(world, client, clock):
    """features_current compares against the live count of fights strictly before today, so a
    result for a fight dated today does not make the features stale until the date rolls over."""
    world.fight(ANN, HAL, date(2026, 10, 15), winner=ANN, stats=True)  # today's card

    today = predict(client, ANN, BOB).json()["data_quality"]["fighter_a"]
    assert today["features_current"] is True and today["missing_fights"] == 0

    clock.now = datetime(2026, 10, 16, 4, 30, tzinfo=UTC)  # 00:30 on the 16th in New York
    tomorrow = predict(client, ANN, BOB).json()["data_quality"]["fighter_a"]
    assert tomorrow["features_current"] is False and tomorrow["missing_fights"] == 1


def test_a_debut_is_predicted_and_not_called_rates_complete(client):
    response = predict(client, EVE, ANN, weight_class="Lightweight")

    assert response.status_code == 200
    q = response.json()["data_quality"]["fighter_a"]
    assert q["n_prior_fights"] == 0 and q["has_ufc_history"] is False
    assert q["features_current"] is True  # nothing to be missing
    assert q["rates_complete"] is False  # 0 of 0 lacking stats is not "complete"
    assert response.json()["data_quality"]["rates_complete"] is False


def test_two_debuts_still_get_an_answer(world, client):
    world.fighter(20, "Debut One")
    world.fighter(21, "Debut Two")
    world.current(20, n_fights=0)
    world.current(21, n_fights=0)

    response = predict(client, 20, 21, weight_class="Lightweight")

    assert response.status_code == 200
    assert response.json()["win_probability"]["fighter_a"] == pytest.approx(0.5, abs=0.2)


# -- context ------------------------------------------------------------------------------------


def test_the_division_defaults_from_both_fighters_recent_bouts(client):
    m = predict(client, ANN, BOB).json()["matchup"]

    assert m == {
        "weight_class": "Lightweight", "weight_class_source": "both_fighters_recent_bouts",
        "scheduled_rounds": 3, "is_title_fight": False, "as_of": "2026-10-15",
        "context_used_by_model": ["weight_class"],
    }  # fmt: skip


def test_fighters_from_different_divisions_must_be_told_which_class(client):
    response = predict(client, ANN, CAL)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "weight_class_required"
    assert "Ann Alpha most recently fought at Lightweight" in body["detail"]
    assert "Cal Charlie most recently fought at Welterweight" in body["detail"]
    assert body["recent_divisions"] == {str(ANN): "Lightweight", str(CAL): "Welterweight"}

    ok = predict(client, ANN, CAL, weight_class="Welterweight")
    assert ok.status_code == 200
    assert ok.json()["matchup"]["weight_class_source"] == "request"


def test_a_fighter_with_no_division_on_record_must_be_told_which_class(client):
    response = predict(client, EVE, ANN)

    assert response.status_code == 422
    assert "Eve Echo has no standard-division bout on record" in response.json()["detail"]


def test_a_weight_class_the_model_does_not_know_is_422_and_lists_the_known_ones(client):
    response = predict(client, ANN, BOB, weight_class="Super Heavyweight")

    assert response.status_code == 422
    assert response.json()["code"] == "unknown_weight_class"
    assert response.json()["weight_classes"] == ["Lightweight", "Welterweight"]


def test_a_title_fight_defaults_to_five_rounds(client):
    m = predict(client, ANN, BOB, is_title_fight=True).json()["matchup"]

    assert (m["is_title_fight"], m["scheduled_rounds"]) == (True, 5)


def test_title_and_schedule_are_echoed_but_cannot_change_the_numbers(client):
    plain = predict(client, ANN, BOB).json()
    title = predict(client, ANN, BOB, is_title_fight=True, scheduled_rounds=5).json()

    assert title["win_probability"] == plain["win_probability"]
    assert title["method_probabilities"] == plain["method_probabilities"]
    assert title["matchup"]["context_used_by_model"] == ["weight_class"]


# -- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"fighter_a_id": 1, "fighter_b_id": 1},
        {"fighter_a_id": 1, "fighter_b_id": 2, "scheduled_rounds": 4},
        {"fighter_a_id": 1, "fighter_b_id": 2, "is_title_fight": True, "scheduled_rounds": 3},
        {"fighter_a_id": 0, "fighter_b_id": 2},
        {"fighter_a_id": 1},
        {"fighter_a_id": 1, "fighter_b_id": 2, "odds": 1.5},
        {"fighter_a_id": "x", "fighter_b_id": 2},
    ],
)
def test_bad_requests_are_422_with_the_error_shape(client, payload):
    response = client.post("/predict", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["errors"]


def test_an_unknown_fighter_is_404_and_says_which(client):
    one = predict(client, ANN, 9999)
    both = predict(client, 8888, 9999)

    assert one.status_code == 404 and one.json()["code"] == "fighter_not_found"
    assert one.json()["detail"] == "no fighter with fighter_b_id 9999"
    assert "fighter_a_id 8888" in both.json()["detail"]
    assert "fighter_b_id 9999" in both.json()["detail"]


def test_a_fighter_with_no_feature_row_is_503_features_unavailable(client):
    response = predict(client, ANN, GUS)

    assert response.status_code == 503
    assert response.json()["code"] == "features_unavailable"
    assert str(GUS) in response.json()["detail"]


def test_no_model_is_503_model_unavailable(db, model_dir, clock, league):
    app = create_app(
        settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
        api_settings=ApiSettings(model_artifact_dir=model_dir),
        engine=db, configure_logs=False, poll=False, clock=clock,
    )  # fmt: skip
    with TestClient(app) as client:
        response = predict(client, ANN, BOB)

    assert response.status_code == 503
    assert response.json()["code"] == "model_unavailable"


# -- serving details ----------------------------------------------------------------------------


def test_a_new_active_version_changes_the_model_version_and_the_numbers(
    app, client, registry, tiny
):
    before = predict(client, ANN, BOB).json()
    registry.add(V2, seed=2, active=True)
    assert app.state.model_manager.refresh() == "loaded"

    after = predict(client, ANN, BOB).json()

    assert before["model_version"] == V1 and after["model_version"] == V2
    assert after["win_probability"] != before["win_probability"]


def test_a_prediction_is_counted_and_timed_by_stage(app, client):
    predict(client, ANN, BOB)
    predict(client, ANN, HAL)

    assert counter(app, "api_predictions_total", endpoint="predict") == 2
    for stage in ("db", "assemble", "model", "build", "serialize"):
        assert counter(app, "api_predict_stage_seconds_count", stage=stage) == 2, stage


def test_the_number_of_contributed_features_follows_the_setting(db, model_dir, clock, world):
    app = create_app(
        settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
        api_settings=ApiSettings(model_artifact_dir=model_dir, predict_top_features=2),
        engine=db, configure_logs=False, poll=False, clock=clock,
    )  # fmt: skip
    with TestClient(app) as client:
        assert len(predict(client, ANN, BOB).json()["top_features"]) == 2


def test_the_prediction_core_survives_json_and_orients_both_ways(client):
    """The cache (2b-2) stores the core as JSON; it must round-trip and orient exactly."""
    from cageops_api.predict import PredictionCore, orient

    forward = predict(client, ANN, HAL).json()
    # rebuild the oriented response from a JSON round trip of the core is covered by `orient`
    # being a pure function of the core; check that function directly on a hand-built core
    core = PredictionCore.model_validate_json(
        PredictionCore(
            lo={"id": 1, "name": "L"}, hi={"id": 2, "name": "H"}, p_lo=0.7,
            method={"ko_tko": 0.2, "submission": 0.3, "decision": 0.5}, model_version="v",
            contributions=[
                {"feature": "reach_cm", "kind": "numeric", "value_lo": 180.0,
                 "value_hi": 170.0, "contribution_lo": 0.4},
            ],
            quality_lo=_quality(True, True), quality_hi=_quality(False, True),
            weight_class="Lightweight", weight_class_source="request", scheduled_rounds=3,
            is_title_fight=False, as_of=date(2026, 10, 15), features_built_as_of=date(2026, 10, 15),
            features_build_id=1, stale_names=["H"],
        ).model_dump_json()
    )  # fmt: skip

    a_first, b_first = orient(core, 1), orient(core, 2)

    assert a_first.win_probability.fighter_a == 0.7
    assert b_first.win_probability.fighter_a == 1 - 0.7
    assert b_first.win_probability.fighter_b == 0.7  # exactly p_lo, not 1 - (1 - p_lo)
    assert a_first.top_features[0].contribution == -b_first.top_features[0].contribution
    assert a_first.data_quality.fighter_b.features_current is False
    assert b_first.data_quality.fighter_a.features_current is False
    assert forward["win_probability"]["fighter_a"] > 0


def _quality(current: bool, complete: bool):
    from cageops_api.schemas import FighterQuality, StatsGap

    return FighterQuality(
        n_prior_fights=2, has_ufc_history=True, features_current=current,
        missing_fights=0 if current else 1,
        stats_gap_fights=StatsGap(career=0, last3=0, last5=0), rates_complete=complete,
    )  # fmt: skip


def test_a_request_id_and_the_templated_route_reach_the_access_log(client, access_lines):
    predict(client, ANN, BOB)

    line = next(item for item in access_lines.all() if item["route"] == "/predict")
    assert line["method"] == "POST" and line["status"] == 200 and line["request_id"]
    assert np.isfinite(line["duration_ms"])
