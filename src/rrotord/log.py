"""JSON logs on stdout, one object per line. Secrets never go in: anything that looks like a token is masked, and
no code logs a token, a key or a program's inputs."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sys
from typing import Any

MASK = re.compile(r"(?i)(bearer\s+|token[\"']?\s*[:=]\s*[\"']?|x-access-token:)[A-Za-z0-9._~+/=-]{8,}")
STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def mask(text: str) -> str:
    return MASK.sub(lambda m: m.group(1) + "***", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "at": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": mask(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in STANDARD:
                line[key] = value
        if record.exc_info:
            line["error"] = mask(self.formatException(record.exc_info))
        return mask(json.dumps(line, default=str))


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
