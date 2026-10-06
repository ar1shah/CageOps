"""The dead-letter queue scenarios from the Phase 1b done-when list: force a failure, see it in the
list, inspect it, and replay it successfully."""

import json
import logging
from datetime import date

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

from cageops_common.logs import configure_logging
from cageops_worker.ingest import dlq, jobs
from cageops_worker.ingest.config import IngestSettings
from cageops_worker.ingest.queue import claim_state
from cageops_worker.ingest.runs import enqueue_backfill

from .conftest import BASE, FIXTURES

BURNS, RAHIKI, NILSON = "23024fdfc966410a", "6eedb757f13b9978", "53e533db1b8e9712"


def url(fighter_id):
    return f"{BASE}/fighter-details/{fighter_id}"


def queue_fighter(ctx, fighter_id):
    jobs._enqueue(
        ctx, "fetch_fighter", f"fighter-{fighter_id}", "run", url=url(fighter_id),
        fighter_url=url(fighter_id), force=False,
    )  # fmt: skip


def serve(site, html, fighter_id):
    site.add(f"/fighter-details/{fighter_id}", html(f"fighter_{fighter_id}"))


def requests_to(site, fighter_id):
    return site.requests.count(url(fighter_id))


def scalar(db, sql, **params):
    with db.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


# -- forcing a failure -----------------------------------------------------------------


def test_a_site_that_keeps_failing_is_retried_then_dead_lettered_with_a_reason(ctx, site, run_jobs):
    site.add(f"/fighter-details/{BURNS}", "oops", status=500)
    retries = sample("ingest_jobs_total", job_type="fetch_fighter", outcome="retry")
    dead = sample("ingest_dead_letters_total", job_type="fetch_fighter", reason="retries_exhausted")
    queue_fighter(ctx, BURNS)

    run_jobs()

    [letter] = dlq.list_failed(ctx.queue)
    assert (letter.reason, letter.last_reason) == ("retries_exhausted", "fetch_failed")
    assert (letter.attempts, letter.error_class) == (
        6,
        "RetryableFetchError",
    )  # first try + 5 retries
    assert letter.job_type == "fetch_fighter" and letter.url == url(BURNS)
    assert requests_to(site, BURNS) == 6  # a 500 is never cached, so every retry hit the site
    assert sample("ingest_jobs_total", job_type="fetch_fighter", outcome="retry") == retries + 5
    assert (
        sample("ingest_dead_letters_total", job_type="fetch_fighter", reason="retries_exhausted")
        == dead + 1
    )


def test_a_404_is_dead_lettered_after_exactly_one_attempt(ctx, site, run_jobs):
    queue_fighter(ctx, "f" * 16)  # the fake site has no such page

    run_jobs()

    [letter] = dlq.list_failed(ctx.queue)
    assert (letter.reason, letter.attempts, letter.error_class) == ("not_found", 1, "NotFound")
    assert requests_to(site, "f" * 16) == 1


def test_a_bug_is_dead_lettered_at_once_with_its_traceback_not_retried(
    ctx, site, html, run_jobs, monkeypatch
):
    serve(site, html, BURNS)

    def broken(*args, **kwargs):
        raise KeyError("outcome")

    monkeypatch.setattr(jobs, "parse_fighter", broken)
    queue_fighter(ctx, BURNS)

    run_jobs()

    [letter] = dlq.list_failed(ctx.queue)
    assert (letter.reason, letter.attempts, letter.error_class) == (
        "unexpected_error",
        1,
        "KeyError",
    )
    assert "KeyError" in dlq.inspect(ctx.queue, letter.job_id).traceback


def test_a_job_that_overruns_its_timeout_is_stopped_and_dead_lettered(ctx, site, html, run_jobs):
    serve(site, html, BURNS)
    site.delay = 3  # the site takes 3 s; the job may take 1
    ctx.settings = IngestSettings(
        _env_file=None, ingest_retry_base_s=0, ingest_job_timeout_s=1, ingest_max_retries=0
    )
    queue_fighter(ctx, BURNS)

    run_jobs()

    [letter] = dlq.list_failed(ctx.queue)
    assert letter.last_reason == "timeout" and letter.error_class == "JobTimeoutException"


# -- looking at it ---------------------------------------------------------------------


def test_list_and_summary_group_dead_letters_by_reason(ctx, site, run_jobs):
    site.add(f"/fighter-details/{BURNS}", "oops", status=500)
    queue_fighter(ctx, BURNS)
    queue_fighter(ctx, "e" * 16)
    queue_fighter(ctx, "f" * 16)
    run_jobs()

    assert dict(dlq.summary(ctx.queue)) == {"retries_exhausted": 1, "not_found": 2}
    assert {letter.job_id for letter in dlq.list_failed(ctx.queue, reason="not_found")} == {
        f"fighter-{'e' * 16}",
        f"fighter-{'f' * 16}",
    }
    assert dlq.list_failed(ctx.queue, reason="parse_error") == []


def test_inspect_shows_the_arguments_and_the_traceback(ctx, run_jobs):
    queue_fighter(ctx, "f" * 16)
    run_jobs()

    detail = dlq.inspect(ctx.queue, f"fighter-{'f' * 16}")

    assert detail.func_name == "cageops_worker.ingest.jobs.fetch_fighter"
    assert detail.kwargs["fighter_url"] == url("f" * 16)
    assert "NotFound" in detail.traceback and "Traceback" in detail.traceback
    assert detail.letter.run_id == "run"


def test_inspecting_something_that_is_not_dead_lettered_says_so(ctx):
    with pytest.raises(dlq.DeadLetterNotFound, match="not in the dead-letter queue"):
        dlq.inspect(ctx.queue, "fighter-nope")


def test_the_dead_letter_log_line_carries_the_job_id_and_url(ctx, run_jobs, capsys):
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    configure_logging("INFO")
    try:
        queue_fighter(ctx, "f" * 16)
        run_jobs()
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])

    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    [line] = [x for x in lines if x["msg"] == "job dead-lettered"]
    assert line["job_id"] == f"fighter-{'f' * 16}" and line["url"] == url("f" * 16)
    assert (line["reason"], line["level"]) == ("not_found", "ERROR")


# -- replaying it ----------------------------------------------------------------------


def test_fix_the_site_replay_and_the_job_succeeds(ctx, site, html, run_jobs, db):
    site.add(f"/fighter-details/{BURNS}", "oops", status=500)
    queue_fighter(ctx, BURNS)
    run_jobs()
    assert len(dlq.list_failed(ctx.queue)) == 1
    serve(site, html, BURNS)  # the cause is fixed

    result = dlq.replay(ctx.queue, ctx.settings, f"fighter-{BURNS}")
    run_jobs()

    assert result.enqueued
    assert dlq.list_failed(ctx.queue) == []  # gone from the DLQ
    assert scalar(db, "SELECT reach_cm FROM fighters") == 180.34  # and it did its work
    assert claim_state(ctx.redis, f"fighter-{BURNS}") is None  # succeeded, so the claim is released


def test_a_replay_gets_a_fresh_retry_budget(ctx, site, run_jobs):
    site.add(f"/fighter-details/{BURNS}", "oops", status=500)
    queue_fighter(ctx, BURNS)
    run_jobs()

    dlq.replay(ctx.queue, ctx.settings, f"fighter-{BURNS}")  # the site is still broken
    run_jobs()

    [letter] = dlq.list_failed(ctx.queue)
    assert (letter.reason, letter.attempts) == (
        "retries_exhausted",
        6,
    )  # not 1: RQ's own requeue would
    assert requests_to(site, BURNS) == 12  # have left it with no retries


def test_replay_all_can_filter_by_reason(ctx, site, html, run_jobs):
    site.add(f"/fighter-details/{BURNS}", "oops", status=500)
    queue_fighter(ctx, BURNS)
    queue_fighter(ctx, "f" * 16)  # no such page: a 404
    run_jobs()
    serve(site, html, BURNS)

    report = dlq.replay_all(ctx.queue, ctx.settings, reason="retries_exhausted")
    run_jobs()

    assert report.replayed == [f"fighter-{BURNS}"]
    remaining = [letter.job_id for letter in dlq.list_failed(ctx.queue)]
    assert remaining == [f"fighter-{'f' * 16}"]  # the 404 was not touched


def test_a_replay_that_cant_be_enqueued_puts_the_dead_letter_back(ctx, run_jobs, monkeypatch):
    queue_fighter(ctx, "f" * 16)
    run_jobs()
    job_id = f"fighter-{'f' * 16}"

    def boom(*args, **kwargs):
        raise ConnectionError("redis hiccup")

    monkeypatch.setattr(ctx.queue, "enqueue", boom)
    with pytest.raises(ConnectionError):
        dlq.replay(ctx.queue, ctx.settings, job_id)

    assert [letter.job_id for letter in dlq.list_failed(ctx.queue)] == [job_id]  # nothing lost
    assert claim_state(ctx.redis, job_id) == "dead_lettered"


def test_purge_removes_the_entry_and_frees_the_id(ctx, run_jobs):
    queue_fighter(ctx, "f" * 16)
    run_jobs()
    job_id = f"fighter-{'f' * 16}"

    assert dlq.purge(ctx.queue, job_id) is True
    assert dlq.purge(ctx.queue, job_id) is False  # already gone
    assert dlq.list_failed(ctx.queue) == []
    queue_fighter(ctx, "f" * 16)  # the id can be queued again
    assert ctx.queue.job_ids == [job_id]


# -- a site that blocks us -------------------------------------------------------------


def test_a_bot_challenge_stops_everything_dead_letters_the_rest_and_replays_after_a_reset(
    ctx, site, html, run_jobs, db
):
    challenge = (FIXTURES / "challenge_2026-10-03.html").read_text(encoding="utf-8")
    site.add(f"/fighter-details/{BURNS}", challenge)  # the first request gets the challenge page
    serve(site, html, RAHIKI)
    serve(site, html, NILSON)
    for fighter_id in (BURNS, RAHIKI, NILSON):
        queue_fighter(ctx, fighter_id)

    run_jobs()

    assert {letter.reason for letter in dlq.list_failed(ctx.queue)} == {"source_blocked"}
    assert len(dlq.list_failed(ctx.queue)) == 3
    assert {letter.attempts for letter in dlq.list_failed(ctx.queue)} == {
        1
    }  # no retries burned on a block
    assert requests_to(site, BURNS) == 1  # the one request that found the challenge
    assert requests_to(site, RAHIKI) == requests_to(site, NILSON) == 0  # nothing sent after it
    assert ctx.fetcher.breaker.state().reason == "browser_challenge"
    with pytest.raises(Exception, match="source blocked"):
        enqueue_backfill(ctx, date(2026, 4, 18))  # and no new work is queued

    # a person looks, resets the breaker, and the site is serving normally again
    ctx.fetcher.breaker.reset()
    serve(site, html, BURNS)
    report = dlq.replay_all(ctx.queue, ctx.settings, reason="source_blocked")
    run_jobs()

    assert len(report.replayed) == 3
    assert dlq.list_failed(ctx.queue) == []
    assert scalar(db, "SELECT count(*) FROM fighters") == 3


def test_jobs_started_before_the_block_keep_their_data(ctx, site, html, run_jobs, db):
    serve(site, html, RAHIKI)  # succeeds before the challenge
    challenge = (FIXTURES / "challenge_2026-10-03.html").read_text(encoding="utf-8")
    site.add(f"/fighter-details/{NILSON}", challenge)
    queue_fighter(ctx, RAHIKI)
    queue_fighter(ctx, NILSON)

    run_jobs()

    assert scalar(db, "SELECT count(*) FROM fighters") == 1  # Rahiki was written before the block
    assert [letter.reason for letter in dlq.list_failed(ctx.queue)] == ["source_blocked"]
