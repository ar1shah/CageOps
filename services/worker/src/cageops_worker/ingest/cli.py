"""The ingestion command line: `uv run python -m cageops_worker.ingest <command>` (D-024).

The CLI is the order window: it puts tickets on the rail (backfill, scrape-upcoming), shows the
state of the kitchen (status, dlq, breaker) and starts a cook (worker). It never fetches a page
itself. Every command is a function that takes a wired context and returns an exit code, so tests
call them directly.

Exit codes: 0 ok, 1 error, 2 bad usage (argparse), 3 refused because the site is blocking us,
robots.txt says no, or the platform can't run the worker. Callers that run once (scripts, CronJobs)
can tell "don't just retry" from "something broke".
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from typing import IO, Any

from pydantic import ValidationError
from redis.exceptions import RedisError

from cageops_common.config import get_settings
from cageops_common.db.session import make_engine
from cageops_common.logs import configure_logging
from cageops_scraper.errors import SourceBlocked
from cageops_worker.ingest import dlq
from cageops_worker.ingest.config import UnsupportedPlatform
from cageops_worker.ingest.context import IngestContext, build_context
from cageops_worker.ingest.observe import Observer, probe_redis
from cageops_worker.ingest.queue import EnqueueResult
from cageops_worker.ingest.runs import (
    StartedRun,
    ensure_not_blocked,
    months_before,
    start_backfill,
    start_upcoming,
)
from cageops_worker.ingest.upsert import read_counts
from cageops_worker.ingest.worker import run_worker

PROG = "python -m cageops_worker.ingest"
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3


# -- the parser (also used by a test that checks every command in RUNBOOK.md) ----------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description="CageOps ingestion: queue, work, inspect."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    backfill = commands.add_parser("backfill", help="queue every completed event since a date")
    window = backfill.add_mutually_exclusive_group(required=True)
    window.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD")
    window.add_argument("--months", type=int, metavar="N", help="since N calendar months ago")
    backfill.add_argument(
        "--force", action="store_true", help="re-fetch pages already cached (still rate limited)"
    )

    upcoming = commands.add_parser("scrape-upcoming", help="queue a read of the upcoming events")
    upcoming.add_argument("--force", action="store_true", help="re-fetch pages already cached")

    worker = commands.add_parser("worker", help="run one worker (it holds if the site blocks us)")
    worker.add_argument("--burst", action="store_true", help="exit when the queue is empty")
    worker.add_argument("--max-jobs", type=int, metavar="N", help="exit after N jobs")
    worker.add_argument(
        "--metrics-port", type=int, metavar="PORT", help="/healthz and /metrics port (0 = off)"
    )

    status = commands.add_parser("status", help="queue, dead letters, breaker, and a run's rows")
    status.add_argument("--run", metavar="RUN_ID", help="also show inserted/updated/unchanged")
    status.add_argument("--json", action="store_true")

    letters = commands.add_parser("dlq", help="the dead-letter queue").add_subparsers(
        dest="dlq_command", required=True, metavar="action"
    )
    listing = letters.add_parser("list", help="every dead letter")
    listing.add_argument("--reason", help="only this reason (not_found, retries_exhausted, ...)")
    listing.add_argument("--json", action="store_true")
    inspect = letters.add_parser("inspect", help="one dead letter in full")
    inspect.add_argument("job_id")
    inspect.add_argument("--json", action="store_true")
    replay = letters.add_parser("replay", help="send dead letters around again (fresh retries)")
    target = replay.add_mutually_exclusive_group(required=True)
    target.add_argument("job_id", nargs="?")
    target.add_argument("--all", action="store_true")
    replay.add_argument("--reason", help="with --all: only dead letters with this reason")
    purge = letters.add_parser("purge", help="give up on one dead letter")
    purge.add_argument("job_id")

    breaker = commands.add_parser("breaker", help="the circuit breaker").add_subparsers(
        dest="breaker_command", required=True, metavar="action"
    )
    breaker.add_parser("status", help="open or closed, and why")
    reset = breaker.add_parser("reset", help="close it after checking the site yourself")
    reset.add_argument("--yes", action="store_true", help="required: you looked at the site")
    return parser


# -- helpers --------------------------------------------------------------------------------


class Printer:
    def __init__(self, out: IO[str]):
        self.out = out

    def __call__(self, text: str = "") -> None:
        print(text, file=self.out)

    def json(self, data: Any) -> None:
        print(json.dumps(data, indent=2, default=str), file=self.out)


def _refused(say: Printer, exc: SourceBlocked) -> int:
    say(
        f"Refused: the circuit breaker is open ({exc}). The site has blocked automated access, so "
        "nothing is queued or replayed. Look at the site yourself; if it is fine again, run:\n"
        f"  {PROG} breaker reset --yes"
    )
    return EXIT_REFUSED


def _describe_fetching(ctx: IngestContext, force: bool) -> str:
    text = (
        f"It will fetch from {ctx.source.name} ({ctx.source.base_url}), at most one request every "
        f"{ctx.fetcher.interval_ms} ms in total however many workers run (a longer robots.txt "
        "Crawl-delay overrides that)."
    )
    if force:
        text += (
            "\n--force: pages already in the cache are fetched AGAIN, so expect many more requests."
        )
    return text


def _after_queueing(say: Printer, started: StartedRun, what: str) -> int:
    run_id, result = started.run_id, started.discovery
    if result.enqueued:
        say(f"Queued {what} as run {run_id}.")
        say(f"Start a worker:   {PROG} worker")
        say(f"Watch it:         {PROG} status --run {run_id}")
        return EXIT_OK
    where = (
        "is in the dead-letter queue (see `dlq list`)"
        if result.reason == "dead_lettered"
        else "is already queued or running"
    )
    say(f"Nothing queued: the same discovery job {where}.")
    return EXIT_OK


# -- commands -------------------------------------------------------------------------------


def cmd_backfill(ctx: IngestContext, args: argparse.Namespace, say: Printer) -> int:
    since = args.since or months_before(ctx.today(), args.months)
    if since > ctx.today():
        say(f"error: --since {since} is in the future")
        return EXIT_ERROR
    say(f"Backfill: every completed event from {since} to {ctx.today()}, then each event's fights")
    say("and fighters. " + _describe_fetching(ctx, args.force))
    try:
        started = start_backfill(ctx, since, force=args.force)
    except SourceBlocked as exc:
        return _refused(say, exc)
    return _after_queueing(say, started, f"a backfill since {since}")


def cmd_scrape_upcoming(ctx: IngestContext, args: argparse.Namespace, say: Printer) -> int:
    say("Upcoming: the upcoming-events list and each listed event's card. ")
    say(_describe_fetching(ctx, args.force))
    try:
        started = start_upcoming(ctx, force=args.force)
    except SourceBlocked as exc:
        return _refused(say, exc)
    return _after_queueing(say, started, "a read of the upcoming events")


def make_observer(ctx: IngestContext) -> Observer:
    """An observer with its own short-timeout Redis and Postgres connections (see observe.py)."""
    settings = get_settings()
    timeout = ctx.settings.worker_probe_timeout_s
    return Observer(
        probe_redis(settings.redis_url, timeout),
        make_engine(settings.database_url, connect_args={"connect_timeout": max(1, int(timeout))}),
        ctx.settings.ingest_queue,
        ctx.source.name,
    )


def cmd_worker(ctx: IngestContext, args: argparse.Namespace, say: Printer) -> int:
    return run_worker(
        ctx, make_observer(ctx), burst=args.burst, max_jobs=args.max_jobs, port=args.metrics_port
    )


def cmd_status(ctx: IngestContext, args: argparse.Namespace, say: Printer) -> int:
    snap = Observer(ctx.redis, ctx.engine, ctx.settings.ingest_queue, ctx.source.name).refresh()
    if not snap.redis_ok:
        say("error: cannot reach Redis")
        return EXIT_ERROR
    depth = snap.queue_depth or {}
    idle = not any(depth.get(k) for k in ("queued", "scheduled", "started"))
    run: dict[str, dict[str, int]] | None = None
    if args.run:
        run = {}
        for key, count in sorted(read_counts(ctx.redis, args.run).items()):
            table, action = key.split(":", 1)
            run.setdefault(table, {})[action] = count

    if args.json:
        say.json(
            {
                "source": ctx.source.name,
                "queue": depth,
                "queue_idle": idle,
                "dead_letters": snap.dead_letters,
                "breaker": dataclasses.asdict(snap.breaker) if snap.breaker else None,
                "postgres_ok": snap.postgres_ok,
                "run": {"run_id": args.run, "rows": run} if args.run else None,
            }
        )
        return EXIT_OK

    say(f"source {ctx.source.name}  ({ctx.source.base_url})")
    say("queue  " + "  ".join(f"{k} {v}" for k, v in depth.items()) + f"   idle: {idle}")
    letters = snap.dead_letters or {}
    say("dead letters  " + (", ".join(f"{r}: {n}" for r, n in letters.items()) or "none"))
    if snap.breaker:
        say(f"breaker  OPEN since {snap.breaker.detected_at}: {snap.breaker.reason}")
        say(f"         {snap.breaker.url}")
    else:
        say("breaker  closed")
    if not snap.postgres_ok:
        say("postgres  UNREACHABLE")
    if run is not None:
        say(f"run {args.run}")
        for table, actions in run.items():
            cells = ", ".join(f"{a} {n}" for a, n in actions.items())
            say(f"  {table:<18} {cells}")
        if not run:
            say("  no rows recorded (nothing has run yet, or the id is wrong)")
    return EXIT_OK


def cmd_dlq(ctx: IngestContext, args: argparse.Namespace, say: Printer) -> int:
    action = args.dlq_command
    if action == "list":
        letters = dlq.list_failed(ctx.queue, args.reason)
        if args.json:
            say.json([dataclasses.asdict(letter) for letter in letters])
            return EXIT_OK
        if not letters:
            say("no dead letters" + (f" with reason {args.reason}" if args.reason else ""))
            return EXIT_OK
        say(f"{'JOB ID':<34} {'TYPE':<16} {'REASON':<18} {'TRIES':>5}  URL")
        for letter in letters:
            say(
                f"{letter.job_id:<34} {letter.job_type:<16} {letter.reason:<18} "
                f"{letter.attempts if letter.attempts is not None else '-':>5}  {letter.url or ''}"
            )
        return EXIT_OK

    if action == "inspect":
        try:
            detail = dlq.inspect(ctx.queue, args.job_id)
        except dlq.DeadLetterNotFound as exc:
            say(f"error: {exc}")
            return EXIT_ERROR
        if args.json:
            say.json(dataclasses.asdict(detail))
            return EXIT_OK
        letter = detail.letter
        say(f"job        {letter.job_id}  ({letter.job_type}, run {letter.run_id})")
        say(f"url        {letter.url}")
        say(
            f"reason     {letter.reason}"
            + (f"  (last: {letter.last_reason})" if letter.last_reason else "")
        )
        say(f"attempts   {letter.attempts}   failed at {letter.failed_at}")
        say(f"error      {letter.error_class}: {letter.error}")
        say(f"call       {detail.func_name} args={list(detail.args)} kwargs={detail.kwargs}")
        say("traceback")
        say(detail.traceback.rstrip() or "  (none recorded)")
        return EXIT_OK

    if action == "purge":
        gone = dlq.purge(ctx.queue, args.job_id)
        say(
            f"purged {args.job_id}"
            if gone
            else f"error: {args.job_id} is not in the dead-letter queue"
        )
        return EXIT_OK if gone else EXIT_ERROR

    # replay
    if args.reason and not args.all:
        say("error: --reason only goes with --all")
        return EXIT_USAGE
    try:
        ensure_not_blocked(ctx)
    except SourceBlocked as exc:
        return _refused(say, exc)
    if args.all:
        report = dlq.replay_all(ctx.queue, ctx.settings, args.reason)
    else:
        try:
            result: EnqueueResult = dlq.replay(ctx.queue, ctx.settings, args.job_id)
        except dlq.DeadLetterNotFound as exc:
            say(f"error: {exc}")
            return EXIT_ERROR
        report = dlq.ReplayReport(
            [args.job_id] if result.enqueued else [], [] if result.enqueued else [args.job_id]
        )
    say(f"replayed {len(report.replayed)}" + "".join(f"\n  {j}" for j in report.replayed))
    if report.skipped:
        say(f"skipped {len(report.skipped)} (already queued again)")
    return EXIT_OK


def cmd_breaker(ctx: IngestContext, args: argparse.Namespace, say: Printer) -> int:
    breaker = ctx.fetcher.breaker
    state = breaker.state()
    if args.breaker_command == "status":
        if state is None:
            say(f"breaker closed for {ctx.source.name}")
        else:
            say(f"breaker OPEN for {ctx.source.name} since {state.detected_at}: {state.reason}")
            say(f"first seen at {state.url}")
        return EXIT_OK
    if not args.yes:
        say("error: refusing to reset without --yes. Check the site first: a blocked site that")
        say("is hit again may block us harder.")
        return EXIT_USAGE
    if state is None:
        say("breaker was already closed")
        return EXIT_OK
    logging.getLogger(__name__).warning(
        "circuit breaker reset by hand",
        extra={"source": ctx.source.name, "was_reason": state.reason, "was_url": state.url},
    )
    breaker.reset()
    say(
        f"breaker reset for {ctx.source.name} (it was open: {state.reason}). Workers resume within "
        "their poll interval."
    )
    return EXIT_OK


COMMANDS = {
    "backfill": cmd_backfill,
    "scrape-upcoming": cmd_scrape_upcoming,
    "worker": cmd_worker,
    "status": cmd_status,
    "dlq": cmd_dlq,
    "breaker": cmd_breaker,
}


# -- entry point ----------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    ctx: IngestContext | None = None,
    out: IO[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)  # exits 2 on bad usage
    say = Printer(out or sys.stdout)
    configure_logging(get_settings_level(), stream=sys.stderr)
    try:
        context = ctx or build_context()
        return COMMANDS[args.command](context, args, say)
    except UnsupportedPlatform as exc:
        say(f"error: {exc}")
        return EXIT_REFUSED
    except ValidationError as exc:
        say("error: configuration is missing or invalid (see .env.example):")
        for problem in exc.errors():
            say(f"  {'.'.join(str(p) for p in problem['loc'])}: {problem['msg']}")
        return EXIT_ERROR
    except RedisError as exc:
        say(f"error: Redis problem: {exc}")
        return EXIT_ERROR


def get_settings_level() -> str:
    try:
        return get_settings().log_level
    except ValidationError:
        return "INFO"
