"""Structured JSON logging: one JSON object per line, with job_id and url on every line.

Code sets context once with `log_context(job_id=..., url=...)`, and every log line inside that
block carries those fields without passing them around. It works through a ContextVar, which
is like a sticky note attached to the current thread of execution: anything running inside the
block can read it, and it is removed when the block exits.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

# Always present on every line (null outside a job), so log queries never need "if exists".
ALWAYS = ("job_id", "url")

_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})  # noqa: B039

# Attributes every LogRecord has. Anything else on a record came from `extra=` and is logged.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach fields to every log line emitted inside this block (nested blocks add to it)."""
    token = _context.set(_context.get() | fields)
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        line |= dict.fromkeys(ALWAYS)
        line |= _context.get()
        line |= {k: v for k, v in vars(record).items() if k not in _STANDARD}
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Send every logger's output to `stream` (default stdout) as JSON lines. Containers collect
    stdout; a command-line tool passes stderr so its own output on stdout stays clean."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
