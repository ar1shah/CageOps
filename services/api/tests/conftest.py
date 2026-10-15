"""Shared fixtures for the API tests. Nothing here imports the trainer."""

from __future__ import annotations

import io
import json
import logging

import pytest

from cageops_common.logs import JsonFormatter


@pytest.fixture
def access_lines():
    """The JSON access-log lines emitted while a test runs, as dicts."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("cageops_api.access")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    class Lines:
        def all(self) -> list[dict]:
            return [json.loads(line) for line in stream.getvalue().splitlines() if line]

        def raw(self) -> str:
            return stream.getvalue()

    yield Lines()
    logger.removeHandler(handler)
    logger.setLevel(previous)
