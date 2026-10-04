"""The command line, driven through main() against real Postgres and Redis and the fake site."""

import io
import json
import logging
import sys
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from cageops_common.logs import configure_logging
from cageops_scraper.config import ScraperSettings
from cageops_worker.ingest import cli
from cageops_worker.ingest.cli import build_parser, main
from cageops_worker.ingest.runs import months_before

BURNS = "c3ac8d0da7b05772"
FIGHT = "32054bf2b36b0e47"  # one fight on the Burns card
TRIP = ("js_challenge", "http://ufcstats.com/statistics/events/completed")
SINCE = "2026-04-18"


@pytest.fixture(autouse=True)
def keep_logging_config():
    """main() reconfigures the root logger; put it back so other tests are unaffected."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def run(ctx):
    """run("dlq", "list") -> (exit code, what it printed)."""

    def go(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = main(list(argv), ctx=ctx, out=out)
        return code, out.getvalue()

    return go


def trip(ctx) -> None:
    ctx.fetcher.breaker.trip(*TRIP, ctx.fetcher._clock())


def run_id_from(output: str) -> str:
    line = next(line for line in output.splitlines() if line.startswith("Queued"))
    return line.split("run ")[1].rstrip(".")


def queued_job(ctx):
    (job,) = ctx.queue.jobs
    return job


# -- backfill and scrape-upcoming -------------------------------------------------------------


def test_backfill_queues_one_discovery_job_and_says_what_it_will_fetch(run, ctx):
    code, out = run("backfill", "--since", SINCE)

    assert code == 0
    assert ctx.queue.count == 1
    job = queued_job(ctx)
    assert job.id == f"discover-completed-p1-since-{SINCE}"
    assert job.kwargs["since"] == SINCE and job.kwargs["force"] is False
    assert "ufcstats.com" in out and "1000 ms" not in out.replace("0 ms", "")  # interval is shown
    assert "worker" in out and "status --run" in out  # next steps


def test_backfill_twice_queues_it_once(run, ctx):
    run("backfill", "--since", SINCE)

    code, out = run("backfill", "--since", SINCE)

    assert code == 0 and ctx.queue.count == 1
    assert "Nothing queued" in out and "already queued" in out


def test_months_means_calendar_months_before_today(run, ctx):
    code, _ = run("backfill", "--months", "6")  # today is pinned to 2026-10-03

    assert code == 0
    assert queued_job(ctx).kwargs["since"] == "2026-04-03"


@pytest.mark.parametrize(
    ("today", "months", "expected"),
    [
        (date(2026, 10, 3), 6, date(2026, 4, 3)),
        (date(2026, 3, 31), 1, date(2026, 2, 28)),  # clamped to the month's last day
        (date(2024, 3, 31), 1, date(2024, 2, 29)),  # leap year
        (date(2026, 1, 15), 13, date(2024, 12, 15)),  # across a year boundary
        (date(2026, 10, 3), 0, date(2026, 10, 3)),
    ],
)
def test_months_before(today, months, expected):
    assert months_before(today, months) == expected


def test_force_is_passed_through_and_warned_about(run, ctx):
    code, out = run("backfill", "--since", SINCE, "--force")

    assert code == 0
    assert queued_job(ctx).kwargs["force"] is True
    assert "--force" in out and "AGAIN" in out


def test_a_since_date_in_the_future_is_an_error(run, ctx):
    code, out = run("backfill", "--since", "2026-12-01")

    assert code == 1 and ctx.queue.count == 0 and "future" in out


def test_backfill_needs_since_or_months_and_not_both():
    for argv in (["backfill"], ["backfill", "--since", SINCE, "--months", "3"]):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(argv)
        assert exc.value.code == 2


def test_scrape_upcoming_queues_the_upcoming_read(run, ctx):
    code, out = run("scrape-upcoming")

    assert code == 0 and queued_job(ctx).id == "discover-upcoming"
    assert "upcoming" in out


def test_with_the_breaker_open_nothing_is_queued_and_the_exit_code_is_3(run, ctx):
    trip(ctx)

    for argv in (["backfill", "--since", SINCE], ["scrape-upcoming"]):
        code, out = run(*argv)
        assert code == 3 and "circuit breaker is open" in out and "breaker reset --yes" in out

    assert ctx.queue.count == 0


# -- status ---------------------------------------------------------------------------------


def test_status_shows_the_queue_and_a_closed_breaker(run, ctx):
    run("backfill", "--since", SINCE)

    code, out = run("status")

    assert code == 0
    assert "queued 1" in out and "dead letters  none" in out and "breaker  closed" in out


def test_status_shows_an_open_breaker(run, ctx):
    trip(ctx)

    _, out = run("status")

    assert "breaker  OPEN" in out and "js_challenge" in out


def test_status_run_reports_inserted_and_a_rerun_inserts_nothing(run, ctx, run_jobs, site):
    """The done-when item: a second backfill adds no rows and fetches no pages."""
    _, out = run("backfill", "--since", SINCE)
    first = run_id_from(out)
    run_jobs()
    code, _ = run("status", "--run", first, "--json")
    rows = json.loads(run("status", "--run", first, "--json")[1])["run"]["rows"]

    assert code == 0
    assert rows["fights"]["inserted"] > 0 and rows["events"]["inserted"] == 1

    requests_before = len(site.requests)
    _, out = run("backfill", "--since", SINCE)
    second = run_id_from(out)
    run_jobs()
    rerun = json.loads(run("status", "--run", second, "--json")[1])

    assert len(site.requests) == requests_before  # served from the cache
    for table, actions in rerun["run"]["rows"].items():
        assert "inserted" not in actions and "updated" not in actions, table
    assert rerun["run"]["rows"]["fights"]["unchanged"] == rows["fights"]["inserted"]
    assert rerun["queue_idle"] is True


def test_status_text_lists_each_table_of_a_run(run, ctx, run_jobs):
    _, out = run("backfill", "--since", SINCE)
    run_id = run_id_from(out)
    run_jobs()

    _, out = run("status", "--run", run_id)

    assert f"run {run_id}" in out and "fights" in out and "inserted" in out
    assert "idle: True" in out


def test_status_json_parses(run, ctx):
    code, out = run("status", "--json")

    data = json.loads(out)
    assert code == 0
    assert data["source"] == "ufcstats" and data["queue_idle"] is True and data["run"] is None


# -- the dead-letter queue from the terminal ----------------------------------------------


def dead_ids(run, reason=None):
    argv = ["dlq", "list", "--json"] + (["--reason", reason] if reason else [])
    return {letter["job_id"] for letter in json.loads(run(*argv)[1])}


def fail_fight(site, fight_id=FIGHT):
    site.add(f"/fight-details/{fight_id}", "Internal Server Error", status=500)


def heal_fight(site, html, fight_id=FIGHT):
    site.add(f"/fight-details/{fight_id}", html(f"fight_{fight_id}"))


def test_force_a_failure_then_list_inspect_and_replay_it(run, ctx, run_jobs, site, html, db):
    fail_fight(site)
    run("backfill", "--since", SINCE)
    run_jobs()  # the fight job fails 6 times (retries are instant) and dead-letters

    code, out = run("dlq", "list")
    assert code == 0
    assert f"fight-{FIGHT}" in out and "retries_exhausted" in out
    assert "6" in out  # attempts

    code, out = run("dlq", "inspect", f"fight-{FIGHT}")
    assert code == 0
    assert f"/fight-details/{FIGHT}" in out and "retries_exhausted" in out
    assert "fetch_failed" in out  # what kept failing
    assert "Traceback" in out or "RetryableFetchError" in out

    heal_fight(site, html)  # fix the cause
    code, out = run("dlq", "replay", f"fight-{FIGHT}")
    assert code == 0 and f"fight-{FIGHT}" in out and "replayed 1" in out
    run_jobs()

    assert f"fight-{FIGHT}" not in dead_ids(run)
    with db.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM fights WHERE ufcstats_id = :u"), {"u": FIGHT}
            ).scalar_one()
            == 1
        )


def test_dlq_list_filters_by_reason_and_replay_all_honours_the_filter(
    run, ctx, run_jobs, site, html
):
    fail_fight(site)  # retries_exhausted
    run("backfill", "--since", SINCE)
    run_jobs()  # (the fake site has no pages for most fighters, so there are not_found letters too)
    not_found = dead_ids(run, "not_found")
    assert not_found and f"fight-{FIGHT}" not in not_found
    assert dead_ids(run, "retries_exhausted") == {f"fight-{FIGHT}"}

    code, out = run("dlq", "replay", "--all", "--reason", "retries_exhausted")

    assert code == 0 and "replayed 1" in out
    assert dead_ids(run, "not_found") == not_found  # untouched
    assert f"fight-{FIGHT}" not in dead_ids(run, "retries_exhausted")


def test_dlq_list_json_has_the_fields_an_operator_needs(run, ctx, run_jobs, site):
    fail_fight(site)
    run("backfill", "--since", SINCE)
    run_jobs()

    letter = next(
        item
        for item in json.loads(run("dlq", "list", "--json")[1])
        if item["job_id"] == f"fight-{FIGHT}"
    )

    assert letter["job_id"] == f"fight-{FIGHT}" and letter["job_type"] == "fetch_fight"
    assert letter["reason"] == "retries_exhausted" and letter["attempts"] == 6
    assert letter["url"].endswith(f"/fight-details/{FIGHT}")


def test_dlq_inspect_json_includes_the_traceback(run, ctx, run_jobs, site):
    fail_fight(site)
    run("backfill", "--since", SINCE)
    run_jobs()

    data = json.loads(run("dlq", "inspect", f"fight-{FIGHT}", "--json")[1])

    assert data["letter"]["error_class"] == "RetryableFetchError"
    assert data["func_name"].endswith("fetch_fight") and data["traceback"]


def test_an_unknown_job_id_is_an_error_everywhere(run):
    for argv in (["dlq", "inspect", "nope"], ["dlq", "replay", "nope"], ["dlq", "purge", "nope"]):
        code, out = run(*argv)
        assert code == 1 and "nope" in out


def test_purge_gives_up_on_a_dead_letter(run, ctx, run_jobs, site):
    fail_fight(site)
    run("backfill", "--since", SINCE)
    run_jobs()

    code, out = run("dlq", "purge", f"fight-{FIGHT}")

    assert code == 0 and "purged" in out
    assert f"fight-{FIGHT}" not in dead_ids(run)


def test_replay_refuses_while_the_breaker_is_open(run, ctx, run_jobs, site):
    fail_fight(site)
    run("backfill", "--since", SINCE)
    run_jobs()
    trip(ctx)

    code, out = run("dlq", "replay", "--all")

    assert code == 3 and "circuit breaker is open" in out
    assert f"fight-{FIGHT}" in dead_ids(run)  # still there, untouched
    assert ctx.queue.count == 0


def test_replay_needs_a_target_and_reason_needs_all(run):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["dlq", "replay"])
    assert exc.value.code == 2

    code, out = run("dlq", "replay", "some-job", "--reason", "not_found")
    assert code == 2 and "--all" in out


# -- the breaker ----------------------------------------------------------------------------


def test_breaker_status_and_reset(run, ctx):
    assert "closed" in run("breaker", "status")[1]
    trip(ctx)
    assert "OPEN" in run("breaker", "status")[1]

    code, out = run("breaker", "reset")  # without --yes
    assert code == 2 and "--yes" in out
    assert ctx.fetcher.breaker.state() is not None  # still open

    code, out = run("breaker", "reset", "--yes")
    assert code == 0 and "reset" in out
    assert ctx.fetcher.breaker.state() is None
    assert "already closed" in run("breaker", "reset", "--yes")[1]


def test_a_reset_is_logged_as_a_warning(run, ctx, capsys):
    trip(ctx)

    run("breaker", "reset", "--yes")

    logged = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line]
    (entry,) = [line for line in logged if "reset by hand" in line["msg"]]
    assert entry["level"] == "WARNING" and entry["was_reason"] == "js_challenge"


# -- start-up problems and logging -------------------------------------------------------


def test_native_windows_is_refused_with_exit_3(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    out = io.StringIO()

    code = main(["status"], out=out)

    assert code == 3 and "WSL2" in out.getvalue()


def test_a_bad_configuration_is_a_readable_error_not_a_traceback(monkeypatch):
    def bad_config():
        ScraperSettings(scraper_user_agent="you@example.com", _env_file=None)

    monkeypatch.setattr(cli, "build_context", bad_config)
    out = io.StringIO()

    code = main(["status"], out=out)

    assert code == 1 and "configuration" in out.getvalue()
    assert isinstance(ValidationError, type)


def test_job_log_lines_are_json_with_job_id_and_url(ctx, run_jobs):
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    from cageops_worker.ingest.runs import enqueue_backfill

    enqueue_backfill(ctx, date.fromisoformat(SINCE))
    run_jobs()

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    job_lines = [line for line in lines if line["job_id"]]
    assert job_lines
    assert all(line["url"] for line in job_lines if line["job_id"].startswith("discover"))
    assert {"ts", "level", "logger", "msg"} <= set(job_lines[0])
