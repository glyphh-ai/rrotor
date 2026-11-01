"""JSON logs on stdout, one object per line. Secrets never go in: anything that looks like a token is masked, and
no code logs a token, a key or a program's inputs.

Every line carries `at`, `level`, `logger`, `message` and its own fields, and, added here: `node_id` (and, in a
cluster, `cluster_id` and `role`), the run's `run_id`, `program`, `version` and `function` while a run is being
handled (`bind`), and `trace_id` and `span_id` while a span is being recorded (docs/specs/cluster.md, 7). A drain
sends the same line, masked the same way (`masked`)."""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import json
import logging
import re
import sys
from collections.abc import Iterator
from typing import Any

from opentelemetry import trace

MASK = re.compile(r"(?i)(bearer\s+|token[\"']?\s*[:=]\s*[\"']?|x-access-token:)[A-Za-z0-9._~+/=-]{8,}")
STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
IDENTITY: dict[str, Any] = {}  # this node's: node_id (cluster_id and role once in a cluster)
BOUND: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("rrotor_log_bound", default=None)


def mask(text: str) -> str:
    return MASK.sub(lambda m: m.group(1) + "***", text)


def identify(**given: Any) -> None:
    """This node's identity, on every line from now on (a field given as None is left off)."""
    IDENTITY.clear()
    IDENTITY.update({k: v for k, v in given.items() if v is not None})


@contextlib.contextmanager
def bind(**given: Any) -> Iterator[None]:
    """Fields on every line logged inside the block, in this task and the tasks it starts: a run's id, program,
    version and function."""
    token = BOUND.set({**(BOUND.get() or {}), **{k: v for k, v in given.items() if v is not None}})
    try:
        yield
    finally:
        BOUND.reset(token)


def tracing() -> dict[str, str]:
    """The current span's ids, while one is being recorded."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": trace.format_trace_id(context.trace_id), "span_id": trace.format_span_id(context.span_id)}


def fields(record: logging.LogRecord) -> dict[str, Any]:
    """A record's line as a dict, its message and error masked: what stdout prints, and what a drain sends."""
    line: dict[str, Any] = {
        "at": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
        "level": record.levelname.lower(),
        "logger": record.name,
        "message": mask(record.getMessage()),
        **IDENTITY,
        **(BOUND.get() or {}),
    }
    for key, value in record.__dict__.items():
        if key not in STANDARD:
            line[key] = value
    line.update(tracing())
    if record.exc_info:
        line["error"] = mask(logging.Formatter().formatException(record.exc_info))
    return line


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask(json.dumps(fields(record), default=str))


def masked(record: logging.LogRecord) -> dict[str, Any]:
    """The line exactly as stdout prints it, masked as a whole, read back: what a drain sends."""
    line: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return line


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
