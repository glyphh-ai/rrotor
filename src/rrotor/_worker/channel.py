"""JSON lines over the socket the runner hands the worker as fd 3: one message per line, each with `op`."""

from __future__ import annotations

import json
import socket
import threading
from typing import Any, BinaryIO


class Channel:
    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader, self._writer = reader, writer
        self._lock = threading.Lock()

    @classmethod
    def from_fd(cls, fd: int) -> Channel:
        sock = socket.socket(fileno=fd)
        return cls(sock.makefile("rb"), sock.makefile("wb"))

    def send(self, op: str, **data: Any) -> None:
        line = json.dumps({"op": op, **data}, separators=(",", ":")).encode() + b"\n"
        with self._lock:
            self._writer.write(line)
            self._writer.flush()

    def receive(self) -> dict[str, Any] | None:
        """The next message; None once the runner has gone."""
        line = self._reader.readline()
        if not line:
            return None
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("a message must be an object")
        return message
