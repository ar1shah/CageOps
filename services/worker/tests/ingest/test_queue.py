import random
from concurrent.futures import ThreadPoolExecutor

import pytest
from rq import Queue
from rq.job import Job

from cageops_worker.ingest.config import IngestSettings
from cageops_worker.ingest.queue import (
    DEAD_LETTERED,
    claim_key,
    claim_state,
    enqueue_unique,
    get_queue,
    keep_claim_for_dead_letter,
    release_claim,
)

FETCH_FIGHTER = "cageops_worker.ingest.jobs.fetch_fighter"


@pytest.fixture
def settings():
    return IngestSettings(_env_file=None)


@pytest.fixture
def queue(redis_client, settings):
    return get_queue(redis_client, settings)


def enqueue(queue, settings, job_id="fighter-aaaa", run_id="run-1", **kwargs):
    return enqueue_unique(
        queue, FETCH_FIGHTER, job_id=job_id, run_id=run_id, settings=settings, **kwargs
    )


def test_the_first_enqueue_wins_and_the_job_has_the_right_settings(queue, settings, redis_client):
    result = enqueue(queue, settings, args=["http://ufcstats.com/fighter-details/aaaa"],
                     meta={"url": "http://ufcstats.com/fighter-details/aaaa"})  # fmt: skip

    job = Job.fetch("fighter-aaaa", connection=redis_client)
    assert (result.enqueued, result.reason) == (True, "enqueued")
    assert queue.job_ids == ["fighter-aaaa"]
    assert job.func_name == FETCH_FIGHTER
    assert job.args == ("http://ufcstats.com/fighter-details/aaaa",)
    assert job.timeout == settings.ingest_job_timeout_s
    assert job.result_ttl == 0 and job.failure_ttl == -1
    assert job.retries_left == 5 and len(job.retry_intervals) == 5
    assert job.meta == {"job_type": "fetch_fighter", "run_id": "run-1",
                        "url": "http://ufcstats.com/fighter-details/aaaa"}  # fmt: skip


def test_a_second_enqueue_of_the_same_id_is_skipped(queue, settings):
    enqueue(queue, settings)

    again = enqueue(queue, settings, run_id="run-2")

    assert (again.enqueued, again.reason) == (False, "already_claimed")
    assert len(queue) == 1


def test_different_ids_are_independent(queue, settings):
    enqueue(queue, settings, job_id="fighter-aaaa")
    enqueue(queue, settings, job_id="fighter-bbbb")

    assert sorted(queue.job_ids) == ["fighter-aaaa", "fighter-bbbb"]


def test_eight_workers_enqueueing_the_same_fighter_at_once_queue_exactly_one_job(
    redis_client, settings
):
    """Without the claim this is what RQ does by itself: ['x', 'x', 'x', ...]."""

    def worker(_):
        local_queue = get_queue(redis_client, settings)
        return [enqueue(local_queue, settings, job_id="fighter-shared").enqueued for _ in range(10)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [flag for batch in pool.map(worker, range(8)) for flag in batch]

    assert sum(results) == 1
    assert get_queue(redis_client, settings).job_ids == ["fighter-shared"]


def test_the_claim_expires_on_its_own_as_a_safety_net(queue, settings, redis_client):
    enqueue(queue, settings)

    assert 0 < redis_client.ttl(claim_key("fighter-aaaa")) <= settings.ingest_claim_ttl_s


def test_a_released_claim_lets_a_later_run_enqueue_the_job_again(queue, settings, redis_client):
    enqueue(queue, settings)
    queue.empty()  # the job ran and succeeded
    release_claim(redis_client, "fighter-aaaa")

    again = enqueue(queue, settings, run_id="run-2")

    assert again.enqueued is True


def test_a_dead_lettered_claim_never_expires_and_blocks_a_rerun(queue, settings, redis_client):
    enqueue(queue, settings)
    keep_claim_for_dead_letter(redis_client, "fighter-aaaa")

    again = enqueue(queue, settings, run_id="run-2")

    assert (again.enqueued, again.reason) == (False, "dead_lettered")
    assert claim_state(redis_client, "fighter-aaaa") == DEAD_LETTERED
    assert redis_client.ttl(claim_key("fighter-aaaa")) == -1  # no expiry


def test_claim_state(queue, settings, redis_client):
    assert claim_state(redis_client, "fighter-aaaa") is None
    enqueue(queue, settings, run_id="run-9")
    assert claim_state(redis_client, "fighter-aaaa") == "run-9"


def test_a_failed_enqueue_releases_its_claim(settings, redis_client):
    class BrokenQueue(Queue):
        def enqueue(self, *args, **kwargs):
            raise ConnectionError("redis hiccup")

    broken = BrokenQueue(settings.ingest_queue, connection=redis_client)

    with pytest.raises(ConnectionError):
        enqueue(broken, settings)

    assert claim_state(redis_client, "fighter-aaaa") is None  # nobody is blocked by a ghost


def test_no_retries_configured_means_no_retry_object(queue, redis_client):
    no_retry = IngestSettings(_env_file=None, ingest_max_retries=0)

    enqueue(queue, no_retry)

    assert Job.fetch("fighter-aaaa", connection=redis_client).retries_left is None


def test_each_job_gets_its_own_jittered_schedule(queue, settings, redis_client):
    for i in range(5):
        enqueue(queue, settings, job_id=f"fighter-{i}", rng=random.Random(i))

    schedules = {
        tuple(Job.fetch(f"fighter-{i}", connection=redis_client).retry_intervals) for i in range(5)
    }

    assert len(schedules) > 1
    for schedule in schedules:
        assert 15 <= schedule[0] <= 30 and 240 <= schedule[4] <= 480  # equal jitter on 30, ..., 480


def test_a_zero_base_gives_instant_retries(queue, redis_client):
    instant = IngestSettings(_env_file=None, ingest_retry_base_s=0)

    enqueue(queue, instant)

    assert Job.fetch("fighter-aaaa", connection=redis_client).retry_intervals == [0] * 5
