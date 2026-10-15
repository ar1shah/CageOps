"""The Redis prediction cache: what is in the key, that a hit is exact, and that it fails open.

A real Redis (database 15, emptied around each test) and the same league the /predict tests use.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import text

from cageops_api.cache import KEY_PREFIX, cache_key
from cageops_api.config import ApiSettings
from cageops_api.main import create_app
from cageops_common.config import Settings

V1, V2 = "20300101-000001", "20300101-000002"
NOON_ET = datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
ANN, BOB, CAL, DAN, EVE, FAY, GUS, HAL, IVY = range(1, 10)

BASE = dict(
    model_version="v1", build_id=38, inputs_sha12="abc123def456", fighter_a_id=1, fighter_b_id=2,
    weight_class=None, scheduled_rounds=3, is_title_fight=False, top_n=5, as_of=date(2026, 10, 15),
)  # fmt: skip


# -- the key ------------------------------------------------------------------------------------


def test_the_key_is_prefixed_versioned_and_readable():
    key = cache_key(**BASE)

    assert key == ("pred:v1:v1:38:abc123def456:1-2:auto:3:0:top5:asof=2026-10-15:src=adhoc")
    assert key.startswith(KEY_PREFIX)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"model_version": "v2"}, "a new model must not see the old one's answers"),
        ({"build_id": 39}, "a features rebuild changes every number"),
        ({"inputs_sha12": "000000000000"}, "a result ingested since the build changes them too"),
        ({"fighter_b_id": 3}, "a different matchup"),
        ({"weight_class": "Welterweight"}, "a requested division"),
        ({"scheduled_rounds": 5}, "the schedule"),
        ({"is_title_fight": True}, "the title flag"),
        ({"top_n": 3}, "the entry holds a different number of contributions"),
        ({"as_of": date(2026, 10, 16)}, "age and days-since-last-fight move with the date"),
    ],
)
def test_the_key_changes_with_every_input_the_answer_depends_on(change, why):
    assert cache_key(**{**BASE, **change}) != cache_key(**BASE), why


def test_a_pair_and_its_reverse_share_one_key():
    forward = cache_key(**{**BASE, "fighter_a_id": 7, "fighter_b_id": 3})
    backward = cache_key(**{**BASE, "fighter_a_id": 3, "fighter_b_id": 7})

    assert forward == backward and ":3-7:" in forward


def test_the_date_is_the_real_date_never_the_word_today():
    assert "today" not in cache_key(**BASE)
    assert "asof=2026-10-15" in cache_key(**BASE)


def test_divisions_are_slugged_so_punctuation_cannot_split_or_merge_entries():
    a = cache_key(**{**BASE, "weight_class": "Women's Strawweight"})
    b = cache_key(**{**BASE, "weight_class": "women's  strawweight"})

    assert a == b and ":women-s-strawweight:" in a


# -- the endpoint -------------------------------------------------------------------------------


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
    welter = "Welterweight"
    names = {ANN: "Ann Alpha", BOB: "Bob Bravo", CAL: "Cal Charlie", DAN: "Dan Delta",
             HAL: "Hal Hotel", IVY: "Ivy India"}  # fmt: skip
    for fighter_id, name in names.items():
        league.fighter(fighter_id, name)
    league.fight(ANN, HAL, date(2025, 1, 1), winner=ANN, stats=True)
    league.fight(ANN, BOB, date(2025, 6, 1), winner=BOB, stats=True)
    league.fight(BOB, HAL, date(2025, 9, 1), winner=BOB, stats=True)
    league.fight(CAL, HAL, date(2025, 2, 1), winner=CAL, stats=True, weight_class=welter)
    league.fight(DAN, IVY, date(2026, 6, 6), winner=DAN, stats=False, source="wikipedia")
    league.current(ANN, n_fights=2, last_fight=date(2025, 6, 1))
    league.current(BOB, n_fights=2, last_fight=date(2025, 9, 1))
    league.current(HAL, n_fights=3, last_fight=date(2025, 9, 1))
    league.current(CAL, n_fights=1, last_fight=date(2025, 2, 1))
    league.current(DAN, n_fights=1, n_with_stats=0, last_fight=date(2026, 6, 6))
    league.current(IVY, n_fights=1, n_with_stats=0, last_fight=date(2026, 6, 6))
    # a features build the watch can see: the report carries the live input fingerprint
    from cageops_common.db.fingerprint import features_input_fingerprint

    with league.engine.begin() as conn:
        fingerprint = features_input_fingerprint(conn)
        conn.execute(
            text("UPDATE load_runs SET report = CAST(:r AS jsonb) WHERE id = :i"),
            {"r": f'{{"inputs_sha256": "{fingerprint}"}}', "i": league.run_id},
        )
    registry.add(V1, seed=1, active=True)
    return league


def build_app(db, model_dir, clock, client, *, enabled=True, ttl=21600):
    return create_app(
        settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
        api_settings=ApiSettings(
            model_artifact_dir=model_dir, cache_enabled=enabled, cache_ttl_s=ttl
        ),
        engine=db, redis_client=client, configure_logs=False, poll=False, clock=clock,
    )  # fmt: skip


@pytest.fixture
def app(db, model_dir, clock, redis_client, world):
    return build_app(db, model_dir, clock, redis_client)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def predict(client, a, b, **extra):
    return client.post("/predict", json={"fighter_a_id": a, "fighter_b_id": b, **extra})


def metric(app, name, **labels):
    value = app.state.metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def db_stage_count(app):
    return metric(app, "api_predict_stage_seconds_count", stage="db")


def test_the_first_request_misses_and_computes_the_second_hits_without_the_database(app, client):
    first = predict(client, ANN, BOB)
    computed = db_stage_count(app)
    second = predict(client, ANN, BOB)

    assert first.status_code == second.status_code == 200
    assert first.headers["x-cache"] == "miss" and second.headers["x-cache"] == "hit"
    assert second.json() == first.json()
    assert db_stage_count(app) == computed  # the hit never touched Postgres
    assert metric(app, "api_cache_hits_total") == 1
    assert metric(app, "api_cache_misses_total") == 1
    assert metric(app, "api_predictions_total", endpoint="predict") == 2  # counted either way


def test_a_pair_and_its_reverse_share_one_entry_and_the_flip_is_exact(
    db, model_dir, clock, redis_client, app, client
):
    forward = predict(client, ANN, HAL)
    reverse = predict(client, HAL, ANN)

    assert forward.headers["x-cache"] == "miss" and reverse.headers["x-cache"] == "hit"
    assert len(redis_client.keys(f"{KEY_PREFIX}*")) == 1
    # and the flipped hit is what a fresh computation of B-vs-A gives, to the last digit
    with TestClient(build_app(db, model_dir, clock, None, enabled=False)) as uncached:
        fresh_reverse = predict(uncached, HAL, ANN)
        fresh_forward = predict(uncached, ANN, HAL)
    assert fresh_reverse.headers["x-cache"] == "bypass"
    assert reverse.json() == fresh_reverse.json()
    assert forward.json() == fresh_forward.json()


def test_a_cached_answer_is_identical_to_a_computed_one(db, model_dir, clock, client):
    cached = predict(client, ANN, BOB)  # miss: computed, stored
    hit = predict(client, ANN, BOB)
    with TestClient(build_app(db, model_dir, clock, None, enabled=False)) as uncached:
        fresh = predict(uncached, ANN, BOB)

    assert hit.json() == cached.json() == fresh.json()


def test_each_context_field_is_its_own_entry(app, client):
    predict(client, ANN, BOB)

    for extra in ({"weight_class": "Lightweight"}, {"is_title_fight": True},
                  {"scheduled_rounds": 5}):  # fmt: skip
        assert predict(client, ANN, BOB, **extra).headers["x-cache"] == "miss", extra
        assert predict(client, ANN, BOB, **extra).headers["x-cache"] == "hit", extra


def test_errors_are_never_cached(app, client, redis_client):
    assert predict(client, ANN, 9999).status_code == 404
    assert predict(client, ANN, CAL).status_code == 422  # cross-division
    assert redis_client.keys(f"{KEY_PREFIX}*") == []
    assert predict(client, ANN, CAL).status_code == 422  # still computed, still refused


def test_an_entry_expires_after_the_ttl(db, model_dir, clock, redis_client, world):
    app = build_app(db, model_dir, clock, redis_client, ttl=90)
    with TestClient(app) as client:
        predict(client, ANN, BOB)

    (key,) = redis_client.keys(f"{KEY_PREFIX}*")
    assert 0 < redis_client.ttl(key) <= 90


def test_a_new_model_version_is_not_served_the_old_models_entry(app, client, registry):
    old = predict(client, ANN, BOB)
    registry.add(V2, seed=2, active=True)
    assert app.state.model_manager.refresh() == "loaded"

    new = predict(client, ANN, BOB)

    assert old.headers["x-cache"] == "miss" and new.headers["x-cache"] == "miss"
    assert old.json()["model_version"] == V1 and new.json()["model_version"] == V2
    assert new.json()["win_probability"] != old.json()["win_probability"]
    assert predict(client, ANN, BOB).headers["x-cache"] == "hit"


def test_a_features_rebuild_is_a_new_key(app, client, world):
    predict(client, ANN, BOB)
    assert predict(client, ANN, BOB).headers["x-cache"] == "hit"

    with world.engine.begin() as conn:  # what `features rebuild` leaves: a newer features run
        conn.execute(
            text(
                "INSERT INTO load_runs (source, file_path, sha256, report) VALUES"
                " ('features', 'db:completed_fights', repeat('d', 64),"
                " (SELECT report FROM load_runs WHERE id = :i))"
            ),
            {"i": world.run_id},
        )
    state = app.state.features_watch.refresh()

    assert state is not None and state.build_id > world.run_id
    assert predict(client, ANN, BOB).headers["x-cache"] == "miss"


def test_a_result_ingested_after_the_build_is_a_new_key_and_the_answer_says_stale(
    app, client, world
):
    first = predict(client, ANN, BOB).json()["data_quality"]
    assert first["features_current"] is True
    assert predict(client, ANN, BOB).headers["x-cache"] == "hit"

    world.fight(ANN, IVY, date(2026, 9, 1), winner=ANN, stats=True)  # a new completed fight
    state = app.state.features_watch.refresh()
    assert state is not None and state.matches_build is False
    assert metric(app, "api_features_inputs_match_build") == 0

    after = predict(client, ANN, BOB)

    assert after.headers["x-cache"] == "miss"  # not the cached "features are current"
    assert after.json()["data_quality"]["features_current"] is False
    assert after.json()["data_quality"]["fighter_a"]["missing_fights"] == 1


def test_a_corrupt_entry_is_a_miss_that_is_recomputed_and_replaced(app, client, redis_client):
    predict(client, ANN, BOB)
    (key,) = redis_client.keys(f"{KEY_PREFIX}*")
    redis_client.set(key, b"{not json")

    again = predict(client, ANN, BOB)

    assert again.status_code == 200 and again.headers["x-cache"] == "miss"
    assert metric(app, "api_redis_errors_total", op="cache_decode") == 1
    assert predict(client, ANN, BOB).headers["x-cache"] == "hit"


def test_a_disabled_cache_computes_every_time(db, model_dir, clock, redis_client, world):
    app = build_app(db, model_dir, clock, redis_client, enabled=False)
    with TestClient(app) as client:
        first, second = predict(client, ANN, BOB), predict(client, ANN, BOB)

    assert first.headers["x-cache"] == second.headers["x-cache"] == "bypass"
    assert redis_client.keys(f"{KEY_PREFIX}*") == []


# -- the date ------------------------------------------------------------------------------------


def test_an_entry_written_before_midnight_new_york_is_not_served_after_it(app, client, clock):
    clock.now = datetime(2026, 10, 16, 3, 59, 59, tzinfo=UTC)  # 23:59:59 on the 15th in New York
    before = predict(client, ANN, BOB)
    assert predict(client, ANN, BOB).headers["x-cache"] == "hit"

    clock.now = datetime(2026, 10, 16, 4, 0, 1, tzinfo=UTC)  # 00:00:01 on the 16th
    after = predict(client, ANN, BOB)

    assert before.json()["matchup"]["as_of"] == "2026-10-15"
    assert after.headers["x-cache"] == "miss"
    assert after.json()["matchup"]["as_of"] == "2026-10-16"


def test_the_boundary_is_new_york_midnight_not_utc_midnight(app, client, clock):
    clock.now = datetime(2026, 10, 15, 20, 0, tzinfo=UTC)  # 16:00 on the 15th in New York
    predict(client, ANN, BOB)

    clock.now = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)  # 23:30 on the 15th: UTC says the 16th
    assert predict(client, ANN, BOB).headers["x-cache"] == "hit"

    clock.now = datetime(2026, 10, 16, 4, 30, tzinfo=UTC)  # 00:30 on the 16th in New York
    assert predict(client, ANN, BOB).headers["x-cache"] == "miss"


# -- failing open --------------------------------------------------------------------------------


def dead_redis(timeout=0.05) -> redis.Redis:
    return redis.Redis(
        host="127.0.0.1", port=1, socket_timeout=timeout, socket_connect_timeout=timeout,
        retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
    )  # fmt: skip


def test_redis_down_still_returns_the_prediction_and_counts_the_failure(
    db, model_dir, clock, world, caplog
):
    app = build_app(db, model_dir, clock, dead_redis())
    with caplog.at_level("WARNING"), TestClient(app) as client:
        response = predict(client, ANN, BOB)
        ready = client.get("/readyz")

    assert response.status_code == 200
    assert response.headers["x-cache"] == "bypass"
    assert response.json()["win_probability"]["fighter_a"] > 0
    assert ready.status_code == 200  # Redis is never a readiness condition
    assert metric(app, "api_redis_errors_total", op="cache_get") == 1
    assert metric(app, "api_redis_errors_total", op="cache_set") == 0  # no write attempted
    warning = next(r for r in caplog.records if r.getMessage().startswith("redis unavailable"))
    assert warning.op == "cache_get"


def test_after_a_failure_redis_is_left_alone_for_a_few_seconds_then_tried_again(
    db, model_dir, clock, world, redis_client
):
    app = build_app(db, model_dir, clock, dead_redis())
    with TestClient(app) as client:
        cache = app.state.cache
        now = [100.0]
        cache._clock = lambda: now[0]
        predict(client, ANN, BOB)
        errors = metric(app, "api_redis_errors_total", op="cache_get")
        for _ in range(5):
            predict(client, ANN, BOB)  # inside the skip window: no attempt, no timeout paid

        assert metric(app, "api_redis_errors_total", op="cache_get") == errors == 1
        assert metric(app, "api_cache_bypassed_total") == 6

        now[0] += 6  # past the 5 s window
        cache.client = redis_client  # Redis is back
        assert predict(client, ANN, BOB).headers["x-cache"] == "miss"
        assert predict(client, ANN, BOB).headers["x-cache"] == "hit"


def test_a_redis_that_dies_between_the_read_and_the_write_does_not_fail_the_request(
    db, model_dir, clock, world, redis_client, monkeypatch
):
    app = build_app(db, model_dir, clock, redis_client)

    def boom(*a, **k):
        raise redis.exceptions.ConnectionError("gone")

    with TestClient(app) as client:
        monkeypatch.setattr(redis_client, "set", boom)
        response = predict(client, ANN, BOB)

    assert response.status_code == 200 and response.headers["x-cache"] == "miss"
    assert metric(app, "api_redis_errors_total", op="cache_set") == 1


def test_no_features_build_yet_means_no_caching_and_no_failure(
    db, model_dir, clock, redis_client, world
):
    with db.begin() as conn:
        conn.execute(text("UPDATE load_runs SET source = 'seed' WHERE source = 'features'"))
    app = build_app(db, model_dir, clock, redis_client)
    with TestClient(app) as client:
        response = predict(client, ANN, BOB)

    assert response.status_code == 200 and response.headers["x-cache"] == "bypass"
    assert redis_client.keys(f"{KEY_PREFIX}*") == []


def test_the_cache_metrics_are_exposed(app, client):
    predict(client, ANN, BOB)
    predict(client, ANN, BOB)

    text_ = client.get("/metrics").text
    for name in ("api_cache_hits_total", "api_cache_misses_total", "api_cache_bypassed_total",
                 "api_redis_errors_total", "api_features_build_id",
                 "api_features_inputs_match_build"):  # fmt: skip
        assert name in text_, name


# -- the division in the key is the model's own spelling -----------------------------------------


def test_two_spellings_of_one_division_share_one_entry(app, client, redis_client):
    first = predict(client, ANN, BOB, weight_class="Lightweight")
    second = predict(client, ANN, BOB, weight_class="  lightweight ")
    third = predict(client, ANN, BOB, weight_class="LIGHTWEIGHT")
    fourth = predict(client, ANN, BOB, weight_class="Lightweight\n")  # trailing whitespace

    assert [r.status_code for r in (first, second, third, fourth)] == [200, 200, 200, 200]
    assert first.headers["x-cache"] == "miss"
    assert second.headers["x-cache"] == third.headers["x-cache"] == "hit"
    assert fourth.headers["x-cache"] == "hit"
    assert len(redis_client.keys(f"{KEY_PREFIX}*")) == 1
    assert second.json() == first.json() == third.json()
    assert first.json()["matchup"]["weight_class"] == "Lightweight"  # the model's spelling


def test_a_misspelling_is_a_422_even_when_a_neighbouring_spelling_is_cached(app, client):
    assert predict(client, ANN, BOB, weight_class="Lightweight").status_code == 200

    for bad in ("Lightweigh", "Light weight", "Lightweight!", "Light-weight"):
        response = predict(client, ANN, BOB, weight_class=bad)
        assert response.status_code == 422, bad
        assert response.json()["code"] == "unknown_weight_class"
        assert "weight_classes" in response.json()


@pytest.mark.parametrize(
    "hostile", ["Lightweight:5", "*", "Light\nweight:", "pred:v1:*", "a" * 41, "../Lightweight"]
)
def test_nothing_a_caller_types_can_reach_the_key(app, client, redis_client, hostile):
    predict(client, ANN, BOB, weight_class="Lightweight")  # one legitimate entry

    response = predict(client, ANN, BOB, weight_class=hostile)

    assert response.status_code == 422
    keys = [k.decode() for k in redis_client.keys("*")]
    assert len(keys) == 1 and all(k.startswith(KEY_PREFIX) for k in keys)
    assert hostile not in keys[0]
    assert keys[0].count(":") == cache_key(**BASE).count(":")  # the shape never changes


def test_an_unspecified_division_is_a_different_entry_from_an_explicit_one(app, client):
    predict(client, ANN, BOB)

    assert predict(client, ANN, BOB, weight_class="Lightweight").headers["x-cache"] == "miss"
    assert predict(client, ANN, BOB, weight_class="lightweight").headers["x-cache"] == "hit"
