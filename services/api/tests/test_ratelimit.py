"""Per-client rate limiting: the token bucket, whom it believes about an address, and the
frontend's server-to-server path (D-043).

Unit tests for the address rules need nothing. The rest use a real Redis (database 15, emptied
around each test) and send requests to `/fighters/{id}`: any 200 or 404 is "let through", so no
data is needed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from datetime import UTC, datetime

import pytest
import redis
from fastapi.testclient import TestClient

from cageops_api.config import ApiSettings
from cageops_api.main import create_app
from cageops_api.metrics import ApiMetrics
from cageops_api.ratelimit import (
    RateLimitConfig,
    bucket_address,
    classify,
    parse_ip,
    public_ip,
)
from cageops_common.config import Settings

KEY = "k" * 40
OTHER_KEY = "z" * 40
NET_10 = (ipaddress.ip_network("10.0.0.0/8"),)


# -- addresses: pure ----------------------------------------------------------------------------


def test_an_untrusted_peer_is_its_own_address_whatever_it_claims():
    assert str(public_ip("203.0.113.9", "1.2.3.4", ())) == "203.0.113.9"
    assert str(public_ip("203.0.113.9", "1.2.3.4", NET_10)) == "203.0.113.9"  # not a trusted proxy


def test_a_trusted_proxy_is_believed_up_to_the_first_address_that_is_not_trusted():
    # the proxy appended the address it saw; the left side is whatever the caller typed
    assert str(public_ip("10.0.0.5", "6.6.6.6, 198.51.100.7", NET_10)) == "198.51.100.7"
    # a chain of our own proxies is skipped
    assert str(public_ip("10.0.0.5", "198.51.100.7, 10.0.0.9", NET_10)) == "198.51.100.7"


def test_a_garbage_entry_ends_the_walk_at_the_last_address_believed():
    assert str(public_ip("10.0.0.5", "1.2.3.4, not-an-ip", NET_10)) == "10.0.0.5"
    assert str(public_ip("10.0.0.5", "", NET_10)) == "10.0.0.5"
    assert str(public_ip("10.0.0.5", None, NET_10)) == "10.0.0.5"


def test_a_peer_that_is_not_an_address_has_no_address():
    assert public_ip("testclient", "1.2.3.4", NET_10) is None
    assert public_ip(None, None, ()) is None


def test_ipv4_mapped_ipv6_is_the_ipv4_address():
    assert parse_ip("::ffff:203.0.113.9") == ipaddress.ip_address("203.0.113.9")


def test_an_ipv6_client_is_its_whole_64():
    one = parse_ip("2001:db8:1:2:aaaa::1")
    two = parse_ip("2001:db8:1:2:bbbb::9")
    other = parse_ip("2001:db8:1:3::1")

    assert bucket_address(one) == bucket_address(two) == "2001:db8:1:2::/64"
    assert bucket_address(other) != bucket_address(one)
    assert bucket_address(parse_ip("203.0.113.9")) == "203.0.113.9"


def config(**overrides) -> RateLimitConfig:
    base = dict(
        enabled=True, per_minute=120, burst=40, trusted_proxies=(), frontend_keys=(KEY.encode(),),
        user_per_minute=300, user_burst=100, key_per_minute=6000, key_burst=600,
    )  # fmt: skip
    return RateLimitConfig(**{**base, **overrides})


def test_a_valid_key_makes_the_forwarded_address_the_users_bucket_plus_the_key_ceiling():
    headers = {b"x-api-key": KEY.encode(), b"x-cageops-client-ip": b"198.51.100.7"}

    client = classify(headers, "76.76.21.21", config(), ApiMetrics())

    assert client.klass == "frontend" and client.ip == "198.51.100.7"
    assert [(b.key, b.scope) for b in client.buckets] == [
        ("rl:fe-user:198.51.100.7", "user"),
        ("rl:fe-key", "key"),
    ]


def test_the_forwarded_address_is_ignored_without_a_valid_key():
    metrics = ApiMetrics()
    for headers in (
        {b"x-cageops-client-ip": b"198.51.100.7"},
        {b"x-cageops-client-ip": b"198.51.100.7", b"x-api-key": OTHER_KEY.encode()},
    ):
        client = classify(headers, "203.0.113.9", config(), metrics)
        assert client.klass == "public" and client.ip == "203.0.113.9"
        assert [b.key for b in client.buckets] == ["rl:ip:203.0.113.9"]


def test_no_configured_key_means_no_frontend_class_even_if_one_is_sent():
    headers = {b"x-api-key": b"", b"x-cageops-client-ip": b"198.51.100.7"}

    assert (
        classify(headers, "203.0.113.9", config(frontend_keys=()), ApiMetrics()).klass == "public"
    )
    assert (
        classify(
            {b"x-api-key": KEY.encode()}, "203.0.113.9", config(frontend_keys=()), ApiMetrics()
        ).klass
        == "public"
    )
    assert classify(headers, "203.0.113.9", config(), ApiMetrics()).klass == "public"


# -- through the app ----------------------------------------------------------------------------


@pytest.fixture
def make_app(db, model_dir, redis_client):
    def build(*, client=("203.0.113.9", 50000), redis_override=None, **settings):
        app = create_app(
            settings=Settings(database_url="postgresql://x", redis_url="redis://x"),
            api_settings=ApiSettings(
                model_artifact_dir=model_dir,
                cache_enabled=False,
                rate_limit_per_minute=settings.pop("rate_limit_per_minute", 60),
                rate_limit_burst=settings.pop("rate_limit_burst", 3),
                **settings,
            ),
            engine=db,
            redis_client=redis_override or redis_client,
            configure_logs=False,
            poll=False,
            clock=lambda: datetime(2026, 10, 15, 16, 0, tzinfo=UTC),
        )
        return app, TestClient(app, client=client)

    return build


def hit(client, path="/fighters/1", **headers):
    return client.get(path, headers=headers)


def counter(app, name, **labels):
    value = app.state.metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


def test_a_client_can_burst_then_is_told_when_to_come_back(make_app):
    app, client = make_app(rate_limit_burst=3, rate_limit_per_minute=6)  # 0.1 token a second
    with client:
        codes = [hit(client).status_code for _ in range(3)]
        refused = hit(client)

    assert codes == [404, 404, 404]  # let through (no such fighter), not limited
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate_limited"
    assert 8 <= int(refused.headers["retry-after"]) <= 10  # one token at 0.1 a second
    assert refused.json()["retry_after_s"] == int(refused.headers["retry-after"])
    assert counter(app, "api_ratelimited_total", client_class="public", scope="ip") == 1


def test_the_bucket_refills(make_app):
    app, client = make_app(rate_limit_burst=2, rate_limit_per_minute=600)  # 10 tokens a second
    with client:
        assert [hit(client).status_code for _ in range(2)] == [404, 404]
        assert hit(client).status_code == 429
        time.sleep(0.35)

        assert hit(client).status_code == 404  # three tokens' worth of waiting


def test_clients_have_separate_buckets(make_app, redis_client):
    app, first = make_app(rate_limit_burst=1, client=("203.0.113.9", 1))
    with first:
        assert hit(first).status_code == 404
        assert hit(first).status_code == 429
    second = TestClient(app, client=("203.0.113.10", 1))
    with second:
        assert hit(second).status_code == 404


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
def test_ops_endpoints_are_never_limited(make_app, path):
    app, client = make_app(rate_limit_burst=1)
    with client:
        statuses = [hit(client, path).status_code for _ in range(6)]

    assert 429 not in statuses
    assert counter(app, "api_ratelimited_total", client_class="public", scope="ip") == 0


def test_everything_else_including_docs_is_limited(make_app):
    app, client = make_app(rate_limit_burst=1)
    with client:
        assert hit(client, "/openapi.json").status_code == 200
        assert hit(client, "/openapi.json").status_code == 429
        assert hit(client, "/no/such/path").status_code == 429  # even a scanner's 404


def test_a_refusal_is_labelled_with_its_route_template_and_logged_with_the_client_class(
    make_app, access_lines
):
    app, client = make_app(rate_limit_burst=1)
    with client:
        hit(client, "/fighters/1")
        assert hit(client, "/fighters/2").status_code == 429

    assert counter(
        app, "api_request_duration_seconds_count",
        method="GET", route="/fighters/{fighter_id}", status="429",
    ) == 1  # fmt: skip
    refused = access_lines.all()[-1]
    assert refused["status"] == 429 and refused["client_class"] == "public"
    assert refused["client_ip"] == "203.0.113.9" and refused["request_id"]


def test_a_spoofed_forwarded_for_from_an_untrusted_peer_buys_no_new_bucket(make_app):
    app, client = make_app(rate_limit_burst=2)
    with client:
        codes = [
            hit(client, **{"X-Forwarded-For": f"198.51.100.{i}"}).status_code for i in range(4)
        ]

    assert codes == [404, 404, 429, 429]


def test_behind_a_trusted_proxy_each_forwarded_client_has_its_own_bucket(make_app):
    app, client = make_app(
        rate_limit_burst=1, trusted_proxy_cidrs="10.0.0.0/8", client=("10.0.0.5", 1)
    )
    with client:
        a1 = hit(client, **{"X-Forwarded-For": "198.51.100.1"}).status_code
        a2 = hit(client, **{"X-Forwarded-For": "198.51.100.1"}).status_code
        b1 = hit(client, **{"X-Forwarded-For": "198.51.100.2"}).status_code
        # a caller prepending addresses cannot change the one the proxy appended
        forged = hit(client, **{"X-Forwarded-For": "6.6.6.6, 198.51.100.1"}).status_code

    assert (a1, a2, b1, forged) == (404, 429, 404, 429)


def test_an_ipv6_client_is_limited_as_a_64(make_app):
    app, client = make_app(rate_limit_burst=1, client=("2001:db8:1:2::aaaa", 1))
    with client:
        assert hit(client).status_code == 404
        assert hit(client).status_code == 429
    sibling = TestClient(app, client=("2001:db8:1:2:ffff::1", 1))
    with sibling:
        assert hit(sibling).status_code == 429  # same /64
    elsewhere = TestClient(app, client=("2001:db8:1:3::1", 1))
    with elsewhere:
        assert hit(elsewhere).status_code == 404


# -- the frontend --------------------------------------------------------------------------------


FRONTEND = dict(
    frontend_api_key=KEY, rate_limit_burst=2, rate_limit_per_minute=60,
    frontend_user_burst=5, frontend_user_rate_per_minute=60,
    frontend_key_burst=100, frontend_key_rate_per_minute=6000,
)  # fmt: skip


def as_frontend(ip: str | None = None, key: str = KEY) -> dict[str, str]:
    headers = {"X-API-Key": key}
    if ip is not None:
        headers["X-CageOps-Client-IP"] = ip
    return headers


def test_a_valid_key_gets_the_higher_limit(make_app):
    app, client = make_app(**FRONTEND)
    with client:
        public = [hit(client).status_code for _ in range(3)]
    app2, client2 = make_app(**FRONTEND, client=("76.76.21.21", 1))
    with client2:
        frontend = [hit(client2, **as_frontend("198.51.100.7")).status_code for _ in range(5)]
        sixth = hit(client2, **as_frontend("198.51.100.7"))

    assert public == [404, 404, 429]  # burst 2
    assert frontend == [404] * 5  # user burst 5
    assert sixth.status_code == 429
    assert counter(app2, "api_ratelimited_total", client_class="frontend", scope="user") == 1


def test_a_wrong_or_missing_key_is_a_normal_client(make_app):
    app, client = make_app(**FRONTEND)
    with client:
        wrong = [hit(client, **as_frontend("198.51.100.7", key=OTHER_KEY)).status_code
                 for _ in range(3)]  # fmt: skip
    assert wrong == [404, 404, 429]  # the public burst of 2, not the frontend's 5
    assert counter(app, "api_ratelimit_ignored_headers_total", reason="api_key_invalid") == 3
    assert counter(app, "api_ratelimited_total", client_class="public", scope="ip") == 1


def test_a_forwarded_address_without_a_valid_key_cannot_dodge_or_frame(make_app):
    app, client = make_app(**FRONTEND)
    with client:
        # claiming a different user each time buys nothing: still one public client
        codes = [
            hit(client, **{"X-CageOps-Client-IP": f"198.51.100.{i}"}).status_code
            for i in range(1, 5)
        ]
    assert codes == [404, 404, 429, 429]
    assert counter(
        app, "api_ratelimit_ignored_headers_total", reason="client_ip_without_valid_key"
    ) == 4  # fmt: skip

    # and it cannot spend another address's bucket: that address is untouched
    other = TestClient(app, client=("76.76.21.21", 1))
    with other:
        assert [hit(other, **as_frontend("198.51.100.1")).status_code for _ in range(5)] == [
            404
        ] * 5


def test_forwarded_users_have_independent_buckets(make_app):
    app, client = make_app(**{**FRONTEND, "frontend_user_burst": 2})
    with client:
        a = [hit(client, **as_frontend("198.51.100.1")).status_code for _ in range(3)]
        b = [hit(client, **as_frontend("198.51.100.2")).status_code for _ in range(2)]

    assert a == [404, 404, 429] and b == [404, 404]


def test_the_key_ceiling_holds_however_many_addresses_a_caller_claims(make_app):
    app, client = make_app(
        **{**FRONTEND, "frontend_key_burst": 3, "frontend_key_rate_per_minute": 0.6}
    )
    with client:
        codes = [hit(client, **as_frontend(f"198.51.100.{i}")).status_code for i in range(1, 6)]

    assert codes == [404, 404, 404, 429, 429]  # a new address each time, one key
    assert counter(app, "api_ratelimited_total", client_class="frontend", scope="key") == 2


def test_a_malformed_forwarded_address_leaves_only_the_key_ceiling(make_app):
    app, client = make_app(**{**FRONTEND, "frontend_user_burst": 1})
    with client:
        codes = [hit(client, **as_frontend("not-an-ip")).status_code for _ in range(3)]

    assert codes == [404, 404, 404]  # no per-user bucket to exhaust
    assert counter(app, "api_ratelimit_ignored_headers_total", reason="client_ip_malformed") == 3


def test_a_refusal_by_one_bucket_does_not_charge_the_other(make_app, redis_client):
    app, client = make_app(**{**FRONTEND, "frontend_user_burst": 3, "frontend_key_burst": 1,
                              "frontend_key_rate_per_minute": 0.6})  # fmt: skip
    with client:
        assert hit(client, **as_frontend("198.51.100.1")).status_code == 404
        refused = hit(client, **as_frontend("198.51.100.1"))

    assert refused.status_code == 429
    tokens = float(redis_client.hget("rl:fe-user:198.51.100.1", "tokens"))
    assert tokens == pytest.approx(2.0, abs=0.01)  # charged once, by the request that passed


def test_a_short_key_is_not_a_key(make_app, caplog):
    with caplog.at_level("WARNING"):
        app, client = make_app(**{**FRONTEND, "frontend_api_key": "short"})
        with client:
            codes = [hit(client, **as_frontend("198.51.100.1", key="short")).status_code
                     for _ in range(3)]  # fmt: skip

    assert codes == [404, 404, 429]  # public limits
    assert any("shorter than 32" in r.getMessage() for r in caplog.records)
    assert "short" not in " ".join(r.getMessage() for r in caplog.records).replace("shorter", "")


def test_a_previous_key_still_works_during_a_rotation(make_app):
    app, client = make_app(**{**FRONTEND, "frontend_api_key_previous": OTHER_KEY})
    with client:
        old = [hit(client, **as_frontend("198.51.100.1", key=OTHER_KEY)).status_code
               for _ in range(5)]  # fmt: skip

    assert old == [404] * 5


def test_the_key_never_appears_in_logs_metrics_or_responses(make_app, access_lines, caplog):
    app, client = make_app(**{**FRONTEND, "frontend_user_burst": 1})
    with caplog.at_level(logging.DEBUG), client:
        ok = hit(client, **as_frontend("198.51.100.1"))
        refused = hit(client, **as_frontend("198.51.100.1"))
        metrics_text = client.get("/metrics").text
    everything = "\n".join(
        [
            access_lines.raw(),
            metrics_text,
            ok.text,
            refused.text,
            json.dumps(dict(refused.headers)),
            "\n".join(r.getMessage() for r in caplog.records),
            repr(app.state.api_settings),
        ]
    )

    assert refused.status_code == 429
    assert KEY not in everything
    assert "k" * 20 not in everything


# -- failing open --------------------------------------------------------------------------------


def dead_redis() -> redis.Redis:
    return redis.Redis(
        host="127.0.0.1", port=1, socket_timeout=0.05, socket_connect_timeout=0.05,
        retry=redis.retry.Retry(redis.backoff.NoBackoff(), 0),
    )  # fmt: skip


def test_redis_down_allows_the_request_and_counts_it(make_app):
    app, client = make_app(rate_limit_burst=1, redis_override=dead_redis())
    with client:
        codes = [hit(client).status_code for _ in range(5)]
        ready = hit(client, "/readyz").status_code

    assert 429 not in codes  # nobody is refused because Redis is down
    assert ready in (200, 503)  # readiness is about the model and the database, never Redis
    assert counter(app, "api_redis_errors_total", op="ratelimit") == 1  # then the guard backs off


def test_disabled_means_no_limiting_and_no_redis_use(make_app, redis_client):
    app, client = make_app(rate_limit_enabled=False, rate_limit_burst=1)
    with client:
        assert [hit(client).status_code for _ in range(5)] == [404] * 5

    assert redis_client.keys("rl:*") == []
