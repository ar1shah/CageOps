"""The gauges that describe Redis rather than this process (see QueueCollector)."""

from __future__ import annotations

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from cageops_worker.ingest.observe import Observer


class QueueCollector(Collector):
    """Queue depth, dead letters by reason and the breaker, from the observer's latest snapshot.

    Every worker reports the same numbers (they are facts about Redis), so dashboards should take
    max() across workers, not sum(). A value the observer couldn't read is left out, not zero.
    """

    def __init__(self, observer: Observer):
        self._observer = observer

    def collect(self):
        snap = self._observer.snapshot()
        age = self._observer.age()

        depth = GaugeMetricFamily("ingest_queue_depth", "Jobs by state", labels=["queue", "state"])
        for state, count in (snap.queue_depth or {}).items():
            depth.add_metric([self._observer.queue_name, state], count)
        yield depth

        letters = GaugeMetricFamily(
            "ingest_dead_letters_current",
            "Jobs sitting in the dead-letter queue now, by reason",
            labels=["reason"],
        )
        for reason, count in (snap.dead_letters or {}).items():
            letters.add_metric([reason], count)
        yield letters

        breaker = GaugeMetricFamily(
            "scraper_breaker_open",
            "1 while the circuit breaker is open (the site blocked us; nothing is fetched)",
            labels=["source"],
        )
        if snap.breaker_open is not None:
            breaker.add_metric([self._observer.source_name], 1.0 if snap.breaker_open else 0.0)
        yield breaker

        if age is not None:
            age_gauge = GaugeMetricFamily(
                "ingest_observer_age_seconds", "Age of the snapshot behind the gauges above"
            )
            age_gauge.add_metric([], age)
            yield age_gauge
