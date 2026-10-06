from prometheus_client import REGISTRY

from cageops_common.logs import log_context
from cageops_scraper.anomalies import report_anomalies


def count(kind: str) -> float:
    labels = {"source": "ufcstats", "kind": kind}
    return REGISTRY.get_sample_value("scraper_parse_anomaly_total", labels) or 0.0


def test_each_anomaly_is_logged_as_a_warning_and_counted_by_kind(caplog):
    before = count("round_sig_strikes_sum_mismatch")

    with caplog.at_level("WARNING", logger="cageops_scraper.anomalies"):
        report_anomalies(
            "ufcstats",
            [
                "round_sig_strikes_sum_mismatch:fighter=aaaa",
                "round_sig_strikes_sum_mismatch:fighter=bbbb",
            ],
        )

    assert count("round_sig_strikes_sum_mismatch") == before + 2
    records = [r for r in caplog.records if r.getMessage() == "parse anomaly"]
    assert [(r.levelname, r.kind, r.detail) for r in records] == [
        (
            "WARNING",
            "round_sig_strikes_sum_mismatch",
            "round_sig_strikes_sum_mismatch:fighter=aaaa",
        ),
        (
            "WARNING",
            "round_sig_strikes_sum_mismatch",
            "round_sig_strikes_sum_mismatch:fighter=bbbb",
        ),
    ]


def test_the_log_line_carries_the_jobs_url_and_job_id(capsys):
    import logging

    from cageops_common.logs import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    configure_logging("WARNING")
    try:
        with log_context(job_id="job-1", url="http://ufcstats.com/fight-details/x"):
            report_anomalies("ufcstats", ["unrecognized_time_format:Five rounds"])
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert (
        '"job_id": "job-1"' in line and "fight-details/x" in line and '"level": "WARNING"' in line
    )


def test_nothing_to_report_does_nothing(caplog):
    before = count("anything")

    with caplog.at_level("WARNING"):
        report_anomalies("ufcstats", [])

    assert caplog.records == [] and count("anything") == before
