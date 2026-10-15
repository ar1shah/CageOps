"""Per-client rate limiting: a token bucket in Redis (D-043).

THE BUCKET. Each client has a jar that holds at most `capacity` tokens and refills at `rate`
tokens a second. A request takes one. An empty jar is a 429 with `Retry-After` set to when one token
will exist. Bursts up to `capacity` are allowed, the sustained rate is bounded by `rate`, and the
wait time is exact, which a fixed window (2x burst at the boundary) cannot give.

WHY REDIS. With several API replicas a dict in each process is several jars, so a client gets a
multiple of the allowance and a restart empties them all. Redis is one jar. The take-a-token step is
one Lua script, so read, refill and subtract are atomic across replicas, and it reads Redis `TIME`
so replica clocks do not matter.

WHO IS THE CLIENT. Two classes:

    public    one bucket per IP. `X-Forwarded-For` is believed only when the immediate peer is a
              configured trusted proxy (TRUSTED_PROXY_CIDRS), and then only up to the first
              address that is not itself a trusted proxy, reading from the right: a proxy APPENDS
              the address it saw, so the left side is whatever the caller typed.
    frontend  a request carrying a valid `X-API-Key` (our Next.js server components call from a
              few server IPs, so per-IP buckets would put every user in one). It has its own,
              larger buckets: one per end user, identified by `X-CageOps-Client-IP`, plus one
              ceiling for the whole key. The forwarded address is honoured ONLY with a valid key.

FAILING OPEN. If Redis does not answer the request is allowed (and counted). The limiter protects
capacity, not correctness; failing closed would turn a Redis blip into a total outage of a public
read-only API. Revisit for routes that cost money (the Phase 4 writeups).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace

import anyio.to_thread
import redis
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from cageops_api.metrics import ApiMetrics
from cageops_api.redis_guard import FAIL_OPEN, RedisGuard

log = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
API_KEY_HEADER = b"x-api-key"
CLIENT_IP_HEADER = b"x-cageops-client-ip"
FORWARDED_FOR_HEADER = b"x-forwarded-for"
MIN_KEY_LENGTH = 32

# KEYS: one bucket each. ARGV: capacity and refill-per-millisecond for each, in order.
# All-or-nothing: every bucket is checked before any is charged, so a request refused by one is not
# billed by another. Returns {allowed, retry_after_ms, index_of_the_bucket_that_refused}.
_LUA = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local n = #KEYS
local have = {}
local refuse, wait = 0, 0
for i = 1, n do
  local cap = tonumber(ARGV[2 * i - 1])
  local rate = tonumber(ARGV[2 * i])
  local h = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local tokens, ts = tonumber(h[1]), tonumber(h[2])
  if tokens == nil then tokens, ts = cap, now end
  tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
  have[i] = tokens
  if tokens < 1 then
    local w = math.ceil((1 - tokens) / rate)
    if w > wait then wait, refuse = w, i end
  end
end
if refuse > 0 then return {0, wait, refuse} end
for i = 1, n do
  local cap = tonumber(ARGV[2 * i - 1])
  local rate = tonumber(ARGV[2 * i])
  redis.call('HSET', KEYS[i], 'tokens', have[i] - 1, 'ts', now)
  redis.call('PEXPIRE', KEYS[i], math.ceil(cap / rate) + 1000)
end
return {1, 0, 0}
"""


@dataclass(frozen=True)
class Bucket:
    key: str
    capacity: float
    per_second: float
    scope: str  # "ip" | "user" | "key", for the metric


@dataclass(frozen=True)
class Client:
    klass: str  # "public" | "frontend"
    ip: str | None  # the address the limits are keyed on, for logs
    buckets: tuple[Bucket, ...]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: int = 0
    scope: str = ""


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool
    per_minute: float
    burst: float
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    frontend_keys: tuple[bytes, ...]  # empty: the frontend class is off
    user_per_minute: float
    user_burst: float
    key_per_minute: float
    key_burst: float


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An address, or None for anything else ("testclient", garbage, a port, an empty string)."""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped  # ::ffff:1.2.3.4 is 1.2.3.4
    return address


def bucket_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """What a limit is keyed on. One person holds a whole /64 of IPv6 addresses, so a /64 is one
    client; an IPv4 address is one client."""
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False).network_address) + "/64"
    return str(address)


def is_trusted(address, trusted: Sequence) -> bool:
    return any(address in network for network in trusted)


def public_ip(
    peer: str | None, forwarded_for: str | None, trusted: Sequence
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a public request is attributed to.

    No trusted proxies configured (the default), or the peer is not one: the peer, and the
    X-Forwarded-For header is not even read. Otherwise walk its entries from the right, skipping
    trusted proxies, and take the first that is not. An unparseable entry ends the walk at the last
    address believed."""
    address = parse_ip(peer) if peer else None
    if address is None or not trusted or not is_trusted(address, trusted):
        return address
    if not forwarded_for:
        return address
    for entry in reversed(forwarded_for.split(",")):
        hop = parse_ip(entry)
        if hop is None:
            return address
        address = hop
        if not is_trusted(hop, trusted):
            return hop
    return address


def classify(
    headers: Mapping[bytes, bytes], peer: str | None, config: RateLimitConfig, metrics: ApiMetrics
) -> Client:
    forwarded_user = headers.get(CLIENT_IP_HEADER)
    provided = headers.get(API_KEY_HEADER)
    valid = bool(
        provided
        and config.frontend_keys
        and any(hmac.compare_digest(provided, key) for key in config.frontend_keys)
    )

    if valid:
        buckets: list[Bucket] = []
        ip_text: str | None = None
        if forwarded_user is not None:
            user = parse_ip(forwarded_user.decode("latin-1"))
            if user is None:
                metrics.ratelimit_ignored.labels("client_ip_malformed").inc()
            else:
                ip_text = bucket_address(user)
                buckets.append(
                    Bucket(
                        f"rl:fe-user:{ip_text}",
                        config.user_burst,
                        config.user_per_minute / 60,
                        "user",
                    )
                )
        buckets.append(Bucket("rl:fe-key", config.key_burst, config.key_per_minute / 60, "key"))
        return Client("frontend", ip_text, tuple(buckets))

    # Not a valid key. The forwarded-user header is never believed without one; a request that
    # sent it (or sent a key that did not match) is counted, not logged.
    if forwarded_user is not None:
        metrics.ratelimit_ignored.labels("client_ip_without_valid_key").inc()
    if provided is not None:
        metrics.ratelimit_ignored.labels("api_key_invalid").inc()
    forwarded_for = headers.get(FORWARDED_FOR_HEADER)
    address = public_ip(peer, forwarded_for.decode("latin-1") if forwarded_for else None,
                        config.trusted_proxies)  # fmt: skip
    ip_text = bucket_address(address) if address is not None else None
    key = f"rl:ip:{ip_text or 'unknown'}"
    return Client("public", ip_text, (Bucket(key, config.burst, config.per_minute / 60, "ip"),))


class RateLimiter:
    def __init__(
        self,
        client: redis.Redis,
        *,
        config: RateLimitConfig,
        metrics: ApiMetrics,
        guard: RedisGuard,
    ) -> None:
        self.config = config
        self.metrics = metrics
        self.guard = guard
        self._script = client.register_script(_LUA)
        self._client = client

    def check(self, headers: Mapping[bytes, bytes], peer: str | None) -> tuple[Client, Decision]:
        client = classify(headers, peer, self.config, self.metrics)
        if not self.guard.available():
            return client, Decision(True)
        args: list[float] = []
        for bucket in client.buckets:
            args += [bucket.capacity, bucket.per_second / 1000]
        try:
            allowed, retry_ms, refused = self._script(
                keys=[b.key for b in client.buckets], args=args
            )
        except FAIL_OPEN as exc:
            self.guard.failed("ratelimit", exc)
            return client, Decision(True)
        if allowed:
            return client, Decision(True)
        scope = client.buckets[int(refused) - 1].scope
        self.metrics.ratelimited.labels(client.klass, scope).inc()
        return client, Decision(False, max(1, math.ceil(int(retry_ms) / 1000)), scope)


class RouteTemplates:
    """Path to route template ("/fighters/2" to "/fighters/{fighter_id}"), for labelling a request
    that is refused before the router has seen it. Built from the OpenAPI document, which is public
    API, rather than from router internals that FastAPI is free to reshuffle."""

    def __init__(self, paths: Mapping[str, Mapping[str, object]]) -> None:
        self._entries = [
            (re.compile("^" + re.sub(r"\{[^/{}]+\}", "[^/]+", template) + "$"), template, methods)
            for template, methods in paths.items()
        ]

    @classmethod
    def from_app(cls, app) -> RouteTemplates:
        return cls(app.openapi().get("paths", {}))

    def find(self, method: str, path: str) -> str | None:
        for pattern, template, methods in self._entries:
            if pattern.match(path) and method.lower() in methods:
                return template
        return None


class RateLimitMiddleware:
    """Sits inside the request-context middleware, so a refusal is still timed, labelled and
    logged like any other response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = scope.get("app")
        limiter: RateLimiter | None = getattr(getattr(app, "state", None), "rate_limiter", None)
        if scope["type"] != "http" or limiter is None or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {k: v for k, v in scope.get("headers", [])}
        peer = scope["client"][0] if scope.get("client") else None
        client, decision = await anyio.to_thread.run_sync(limiter.check, headers, peer)
        scope["cageops_client"] = {"class": client.klass, "ip": client.ip}

        if decision.allowed:
            await self.app(scope, receive, send)
            return
        templates: RouteTemplates | None = getattr(app.state, "route_templates", None)
        template = templates.find(scope["method"], scope["path"]) if templates else None
        if template is not None:
            scope["route"] = SimpleNamespace(path=template)  # read by the metrics label
        response = JSONResponse(
            status_code=429,
            content={
                "detail": "rate limit exceeded; retry after the time given in Retry-After",
                "code": "rate_limited",
                "retry_after_s": decision.retry_after_s,
            },
            headers={"Retry-After": str(decision.retry_after_s)},
        )
        await response(scope, receive, send)
