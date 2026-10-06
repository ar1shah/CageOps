import json
import logging

from cageops_common.logs import JsonFormatter, log_context


def _format(msg: str, **extra) -> dict:
    logger = logging.getLogger("t")
    record = logger.makeRecord("t", logging.INFO, "f", 1, msg, (), None, extra=extra)
    return json.loads(JsonFormatter().format(record))


def test_every_line_has_job_id_and_url_even_outside_a_job():
    line = _format("starting")

    assert line["msg"] == "starting"
    assert line["level"] == "INFO"
    assert line["job_id"] is None and line["url"] is None


def test_context_fields_are_added_and_removed_when_the_block_exits():
    with log_context(job_id="j1", url="http://x/a"):
        inside = _format("fetching")
        with log_context(url="http://x/b"):
            nested = _format("fetching")
    after = _format("done")

    assert (inside["job_id"], inside["url"]) == ("j1", "http://x/a")
    assert (nested["job_id"], nested["url"]) == ("j1", "http://x/b")
    assert after["job_id"] is None


def test_extra_fields_are_logged():
    line = _format("fetched", status=200, elapsed_ms=812)

    assert line["status"] == 200 and line["elapsed_ms"] == 812
