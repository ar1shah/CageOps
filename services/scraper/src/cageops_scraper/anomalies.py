"""Report what the parsers noticed but did not treat as fatal.

Parsers are pure (no logging, no metrics), so they return suspicious values as strings on the
page model, in the form "kind:detail". The ingestion job calls report_anomalies() with them.
A layout change raises ParseError instead; this is for a value that looks off, not a page we
can't read. The job's log context supplies job_id and url on every line.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from cageops_scraper.metrics import PARSE_ANOMALY_TOTAL

log = logging.getLogger(__name__)


def report_anomalies(source: str, anomalies: Iterable[str]) -> None:
    for anomaly in anomalies:
        kind = anomaly.split(":", 1)[0]
        PARSE_ANOMALY_TOTAL.labels(source, kind).inc()
        log.warning("parse anomaly", extra={"source": source, "kind": kind, "detail": anomaly})
