"""Loading, verifying and hot-reloading the active model (D-036).

The models are ~10-tree boosters trained on random data: these tests are about which model is
served and when, not about prediction quality. No trainer import.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cageops_api.main import create_app
from cageops_api.model_manager import ModelUnavailable
from cageops_common.config import Settings
from cageops_common.inference.features import NUMERIC_FEATURES

V1, V2, V3 = "20300101-000001", "20300101-000002", "20300101-000003"
N = len(NUMERIC_FEATURES)


@pytest.fixture
def make_app(db, model_dir):
    from cageops_api.config import ApiSettings

    def build(poll=False, poll_interval_s=30.0, retry_s=300.0):
        return create_app(
            settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
            api_settings=ApiSettings(
                model_artifact_dir=model_dir,
                model_poll_interval_s=poll_interval_s,
                model_reload_retry_s=retry_s,
            ),
            engine=db,
            configure_logs=False,
            poll=poll,
        )

    return build


def sample(app, name, **labels):
    value = app.state.metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def inputs(rows=5, seed=1):
    rng = np.random.default_rng(seed)
    a, b = rng.normal(size=(rows, N)), rng.normal(size=(rows, N))
    return a, b, ["orthodox"] * rows, ["southpaw"] * rows, ["Lightweight"] * rows


def predict(serving):
    a, b, sa, sb, wc = inputs()
    return serving.model.win_loss.predict(a, b, sa, sb, wc)


# -- startup ------------------------------------------------------------------------------------


def test_it_loads_the_active_version_and_is_ready(make_app, registry, caplog):
    registry.add(V1, active=True)
    app = make_app()

    with caplog.at_level("INFO"), TestClient(app) as client:
        body = client.get("/readyz").json()

    assert client.get("/healthz").status_code == 200
    assert body["ready"] is True and body["model_version"] == V1
    loaded = next(r for r in caplog.records if r.msg == "model_loaded")
    assert loaded.model_version == V1
    assert loaded.features_output_sha256.startswith("5b23d187")
    assert loaded.data_cutoff == "2024-09-28"
    assert set(loaded.artifacts_sha256) == {"win_loss.txt", "method.txt"}
    assert sample(app, "api_model_loaded") == 1
    assert sample(app, "api_model_reload_total", result="success") == 1
    assert sample(
        app, "api_model_info", version=V1, features_output_sha256="5b23d187" + "0" * 56
    ) == 1  # fmt: skip


def test_the_bundle_is_found_by_version_not_by_the_stored_artifact_path(make_app, registry, db):
    registry.add(V1, active=True)  # the row's artifact_path points somewhere that does not exist
    with db.connect() as conn:
        path = conn.execute(text("SELECT artifact_path FROM model_versions")).scalar_one()
    assert "relative/to/somewhere/else" in path

    app = make_app()
    with TestClient(app) as client:
        assert client.get("/readyz").json()["ready"] is True


def test_no_active_version_means_not_ready_and_no_predictions(make_app, registry):
    registry.add(V1, active=False)
    app = make_app()

    with TestClient(app) as client:
        response = client.get("/readyz")

        assert response.status_code == 503
        assert response.json()["ready"] is False
        assert "no active model" in response.json()["reason"]
        with pytest.raises(ModelUnavailable):
            app.state.model_manager.require()
    assert sample(app, "api_model_loaded") == 0
    assert sample(app, "api_model_active_in_db") == 0


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("tamper", "does not match its recorded sha256"),
        ("delete", "is missing"),
        ("no_manifest", "no manifest.json"),
    ],
)
def test_a_version_that_fails_verification_is_not_served_and_not_replaced_by_another_file(
    make_app, registry, model_dir, caplog, damage, expected
):
    registry.add(V1, active=True)
    registry.add(V2)  # a perfectly good bundle that is NOT active: must never be picked up
    bundle = model_dir / V1
    if damage == "tamper":
        with (bundle / "win_loss.txt").open("a") as fh:
            fh.write("\n# tampered\n")
    elif damage == "delete":
        (bundle / "method.txt").unlink()
    else:
        (bundle / "manifest.json").unlink()
    app = make_app()

    with caplog.at_level("ERROR"), TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert expected in response.json()["reason"] and V1 in response.json()["reason"]
    assert app.state.model_manager.current is None
    failure = next(r for r in caplog.records if r.msg == "model_reload_failed")
    assert failure.model_version == V1 and expected in failure.reason
    assert sample(app, "api_model_reload_total", result="failure") == 1


def test_a_missing_artifact_directory_is_not_ready_rather_than_a_crash(
    make_app, registry, model_dir
):
    registry.add(V1, active=True)
    import shutil

    shutil.rmtree(model_dir / V1)
    app = make_app()

    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503


def test_an_unreachable_database_at_startup_starts_the_app_not_ready(make_app):
    from cageops_common.db.session import make_engine

    app = create_app(
        settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
        engine=make_engine("postgresql://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1}),
        configure_logs=False,
        poll=False,
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200  # liveness is not about the database
        assert client.get("/readyz").status_code == 503


def test_the_503_for_a_missing_model_has_the_error_shape(make_app, registry):
    app = make_app()

    @app.get("/_needs_model")
    def needs_model():
        app.state.model_manager.require()
        return {}

    with TestClient(app) as client:
        response = client.get("/_needs_model")

    assert response.status_code == 503
    assert response.json()["code"] == "model_unavailable"


# -- hot reload ---------------------------------------------------------------------------------


def test_activating_a_new_version_switches_without_a_restart(make_app, registry, tiny):
    registry.add(V1, seed=1, active=True)
    registry.add(V2, seed=2)
    app = make_app()
    with TestClient(app):
        manager = app.state.model_manager
        before = predict(manager.require())
        assert manager.refresh() == "unchanged"

        registry.activate(V2)
        assert manager.refresh() == "loaded"

        serving = manager.require()
        assert serving.version == V2
        assert not np.array_equal(predict(serving), before)
        assert np.array_equal(
            predict(serving), tiny(2)[0].predict(*inputs())
        )  # exactly the v2 model's output
    assert sample(app, "api_model_reload_total", result="success") == 2
    assert (
        sample(app, "api_model_info", version=V2, features_output_sha256="5b23d187" + "0" * 56) == 1
    )
    # the old version's series is gone, not left at 1
    assert (
        app.state.metrics.registry.get_sample_value(
            "api_model_info", {"version": V1, "features_output_sha256": "5b23d187" + "0" * 56}
        )
        is None
    )


def test_a_bad_new_version_keeps_the_old_one_serving_and_counts_the_failure(
    make_app, registry, model_dir, caplog
):
    registry.add(V1, seed=1, active=True)
    registry.add(V2, seed=2)
    with (model_dir / V2 / "method.txt").open("a") as fh:
        fh.write("\n# tampered\n")
    app = make_app()
    with TestClient(app) as client:
        manager = app.state.model_manager
        before = predict(manager.require())

        registry.activate(V2)
        with caplog.at_level("ERROR"):
            assert manager.refresh() == "failed"

        assert manager.require().version == V1
        assert np.array_equal(predict(manager.require()), before)
        assert client.get("/readyz").json() == {
            "ready": True, "model_version": V1, "database": "ok",
        }  # fmt: skip
    failure = next(r for r in caplog.records if r.msg == "model_reload_failed")
    assert failure.model_version == V2 and failure.still_serving == V1
    assert "method.txt" in failure.reason
    assert sample(app, "api_model_reload_total", result="failure") == 1
    assert sample(app, "api_model_reload_total", result="success") == 1


def test_a_failed_version_is_not_retried_until_the_back_off_has_passed(
    make_app, registry, model_dir, monkeypatch, tiny
):
    registry.add(V1, seed=1, active=True)
    registry.add(V2, seed=2)
    (model_dir / V2 / "method.txt").write_text("broken")
    app = make_app(retry_s=300)
    with TestClient(app):
        manager = app.state.model_manager
        now = [1000.0]
        monkeypatch.setattr(manager, "_clock", lambda: now[0])
        registry.activate(V2)
        assert manager.refresh() == "failed"
        now[0] += 60
        assert manager.refresh() == "backing_off"  # no re-hash, no new log line, no new count
        assert sample(app, "api_model_reload_total", result="failure") == 1

        # the operator fixes the files; once the back-off ends it is picked up
        import shutil

        shutil.rmtree(model_dir / V2)
        win, method = tiny(2)
        from datetime import date

        from cageops_common.inference.bundle import write_bundle

        (model_dir / V2).mkdir()
        manifest = write_bundle(
            model_dir / V2, version=V2, data_cutoff=date(2024, 9, 28),
            win_model=win, method_model=method,
        )  # fmt: skip
        assert manifest.files_sha256 == _config(registry, V2)["artifacts_sha256"]
        now[0] += 300
        assert manager.refresh() == "loaded"
        assert manager.require().version == V2


def _config(registry, version):
    with registry.engine.connect() as conn:
        return conn.execute(
            text("SELECT config FROM model_versions WHERE version = :v"), {"v": version}
        ).scalar_one()


def test_losing_the_active_row_keeps_the_verified_model_serving(make_app, registry, caplog):
    registry.add(V1, active=True)
    app = make_app()
    with TestClient(app) as client:
        manager = app.state.model_manager
        registry.deactivate_all()
        with caplog.at_level("WARNING"):
            assert manager.refresh() == "no_active"
            assert manager.refresh() == "no_active"

        assert manager.require().version == V1
        assert client.get("/readyz").json()["ready"] is True
        assert sample(app, "api_model_active_in_db") == 0
        warnings = [r for r in caplog.records if "no active version" in r.getMessage()]
        assert len(warnings) == 1  # said once, not every poll

        registry.activate(V1)
        assert manager.refresh() == "unchanged"
        assert sample(app, "api_model_active_in_db") == 1


def test_a_database_that_stops_answering_turns_ready_off_after_a_few_polls_but_keeps_the_model(
    make_app, registry, monkeypatch
):
    registry.add(V1, active=True)
    app = make_app(poll_interval_s=10)
    with TestClient(app) as client:
        manager = app.state.model_manager
        now = [5000.0]
        monkeypatch.setattr(manager, "_clock", lambda: now[0])
        manager._last_db_ok = now[0]
        assert client.get("/readyz").json()["ready"] is True

        now[0] += 31  # more than 3 polls of 10 s with no successful round trip
        body = client.get("/readyz")

        assert body.status_code == 503
        assert body.json()["model_version"] == V1  # still loaded, just not provably ready
        assert "database has not answered" in body.json()["reason"]


def test_the_background_poller_picks_up_a_new_version_by_itself(make_app, registry):
    registry.add(V1, seed=1, active=True)
    registry.add(V2, seed=2)
    app = make_app(poll=True, poll_interval_s=0.05)
    with TestClient(app):
        manager = app.state.model_manager
        assert manager.require().version == V1
        registry.activate(V2)
        deadline = threading.Event()
        for _ in range(100):
            if manager.require().version == V2:
                break
            deadline.wait(0.05)

        assert manager.require().version == V2


# -- in-flight requests -------------------------------------------------------------------------


def test_a_request_started_before_a_switch_finishes_on_the_old_model(make_app, registry, tiny):
    registry.add(V1, seed=1, active=True)
    registry.add(V2, seed=2)
    app = make_app()
    started, proceed = threading.Event(), threading.Event()

    @app.get("/_slow")
    def slow():
        serving = app.state.model_manager.require()  # the one copy this request will use
        started.set()
        proceed.wait(10)
        return {"version": serving.version, "p": float(predict(serving)[0])}

    @app.get("/_fast")
    def fast():
        serving = app.state.model_manager.require()
        return {"version": serving.version, "p": float(predict(serving)[0])}

    with TestClient(app) as client:
        result = {}
        worker = threading.Thread(target=lambda: result.update(client.get("/_slow").json()))
        worker.start()
        assert started.wait(10)

        registry.activate(V2)
        assert app.state.model_manager.refresh() == "loaded"
        new = client.get("/_fast").json()  # a request after the switch sees the new model
        proceed.set()
        worker.join(10)

    assert new["version"] == V2
    assert result["version"] == V1  # the in-flight request finished on the old one
    expected_v1 = float(tiny(1)[0].predict(*inputs())[0])
    expected_v2 = float(tiny(2)[0].predict(*inputs())[0])
    assert result["p"] == expected_v1 and new["p"] == expected_v2
    assert expected_v1 != expected_v2


def test_hammering_predictions_while_switching_never_mixes_two_models(make_app, registry, tiny):
    """Every response is internally consistent: the version it reports is the version whose
    output it carries, across many switches and many concurrent readers."""
    registry.add(V1, seed=1, active=True)
    registry.add(V2, seed=2)
    registry.add(V3, seed=3)
    expected = {
        V1: predict_for(tiny, 1),
        V2: predict_for(tiny, 2),
        V3: predict_for(tiny, 3),
    }
    app = make_app()
    stop = threading.Event()
    errors: list[str] = []
    seen: set[str] = set()

    def reader(manager):
        while not stop.is_set():
            serving = manager.require()
            got = predict(serving)
            seen.add(serving.version)
            if not np.array_equal(got, expected[serving.version]):
                errors.append(f"{serving.version}: output belongs to another model")

    with TestClient(app):
        manager = app.state.model_manager
        threads = [threading.Thread(target=reader, args=(manager,)) for _ in range(6)]
        for t in threads:
            t.start()
        for version in (V2, V3, V1, V2, V3, V1):
            registry.activate(version)
            assert manager.refresh() == "loaded"
        stop.set()
        for t in threads:
            t.join(10)

    assert errors == []
    assert seen == {V1, V2, V3}


def predict_for(tiny, seed):
    return tiny(seed)[0].predict(*inputs())
