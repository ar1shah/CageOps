"""Liveness, readiness, the request id, the access log and the latency histogram."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from cageops_api.main import create_app
from cageops_common.config import Settings
from cageops_common.db.session import make_engine


@pytest.fixture
def app():
    # A database that is not there: nothing in these tests may need one.
    dead = make_engine("postgresql://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1})
    return create_app(
        settings=Settings(database_url="postgresql://u:p@127.0.0.1:1/x", redis_url="redis://x"),
        engine=dead,
        configure_logs=False,
        poll=False,
    )


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def sample(app, name: str, **labels) -> float:
    value = app.state.metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def test_healthz_is_ok_and_does_no_io(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_503_with_a_reason_while_there_is_no_model_and_no_database(client):
    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert "database unreachable" in response.json()["reason"]


def test_the_docs_say_this_is_not_betting_advice_and_list_the_ops_routes(client):
    spec = client.get("/openapi.json").json()

    assert "not betting advice" in spec["info"]["description"].lower()
    assert {"/healthz", "/readyz"} <= set(spec["paths"])
    assert client.get("/docs").status_code == 200


# -- request id ---------------------------------------------------------------------------------


def test_a_response_always_carries_a_request_id(client):
    request_id = client.get("/healthz").headers["x-request-id"]

    assert re.fullmatch(r"[0-9a-f]{32}", request_id)


def test_a_well_formed_inbound_request_id_is_kept(client):
    response = client.get("/healthz", headers={"X-Request-ID": "abc-12345-DEF"})

    assert response.headers["x-request-id"] == "abc-12345-DEF"


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "x" * 65,
        "has space in it",
        "inject\\nline",
        "semi;colon-and-more",
        "ünïcode-ünïcode".encode("latin-1"),  # a header may carry bytes outside ASCII
    ],
)
def test_a_malformed_inbound_request_id_is_replaced_not_echoed(client, bad):
    response = client.get("/healthz", headers={"X-Request-ID": bad})

    sent = bad.decode("latin-1") if isinstance(bad, bytes) else bad
    assert response.headers["x-request-id"] != sent
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


def test_the_request_id_is_on_the_access_line_with_the_fields_a_search_needs(client, access_lines):
    client.get("/healthz", headers={"X-Request-ID": "trace-0000-0001"})

    (line,) = access_lines.all()
    assert line["request_id"] == "trace-0000-0001"
    assert (line["method"], line["route"], line["status"]) == ("GET", "/healthz", 200)
    assert line["duration_ms"] >= 0 and line["msg"] == "request"
    assert line["client_ip"]


# -- metrics ------------------------------------------------------------------------------------


def test_the_histogram_is_labelled_by_route_template_and_status(app, client):
    client.get("/healthz")
    client.get("/healthz")
    client.get("/readyz")

    assert sample(
        app,
        "api_request_duration_seconds_count",
        method="GET", route="/healthz", status="200",
    ) == 2  # fmt: skip
    assert sample(
        app,
        "api_request_duration_seconds_count",
        method="GET", route="/readyz", status="503",
    ) == 1  # fmt: skip


def test_an_unknown_path_is_one_series_not_one_per_url(app, client):
    for path in ("/wp-login.php", "/.env", "/fighters/../etc/passwd", "/a/b/c/1", "/a/b/c/2"):
        assert client.get(path).status_code == 404

    assert sample(
        app,
        "api_request_duration_seconds_count",
        method="GET", route="unmatched", status="404",
    ) == 5  # fmt: skip
    exposed = client.get("/metrics").text
    assert "wp-login" not in exposed and "passwd" not in exposed


def test_the_in_flight_gauge_returns_to_zero(app, client):
    client.get("/healthz")

    assert sample(app, "api_in_flight_requests") == 0


def test_metrics_exposes_prometheus_text_and_is_not_in_the_docs(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE api_request_duration_seconds histogram" in response.text
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_two_apps_in_one_process_do_not_share_metrics():
    settings = Settings(database_url="postgresql://u:p@localhost/x", redis_url="redis://x")
    dead = make_engine("postgresql://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1})
    first = create_app(settings=settings, engine=dead, configure_logs=False, poll=False)
    second = create_app(settings=settings, engine=dead, configure_logs=False, poll=False)
    with TestClient(first) as c:
        c.get("/healthz")

    assert sample(first, "api_request_duration_seconds_count", method="GET",
                  route="/healthz", status="200") == 1  # fmt: skip
    assert sample(second, "api_request_duration_seconds_count", method="GET",
                  route="/healthz", status="200") == 0  # fmt: skip
