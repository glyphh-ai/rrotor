"""The runtime's precedents: the total across its Ada models, each store counted as the cap counts it
(`rrotor.ada.count`), for the link's `usage`. The stores are read again every nine minutes, on the minute's usage
tick, so a change goes up within ten; the total is reported when it differs from the one last reported, and once
after every start (docs/architecture.md, section 6)."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable

from rrotor import ada
from rrotord.models import Models

log = logging.getLogger("rrotor")
READ_EVERY = 540.0


class Precedents:
    def __init__(self, models: Models, clock: Callable[[], float] = time.monotonic) -> None:
        self.models, self.clock = models, clock
        self.read_at: float | None = None
        self.total: int | None = None  # the last reading; None until one has read
        self.reported: int | None = None

    def count(self) -> int | None:
        """The precedents in every model one of whose versions needs Ada; None when a store does not read."""
        total = 0
        for name in self.models.names():
            if not any("ada" in v.manifest.needs for v in self.models.versions(name)):
                continue
            try:
                total += ada.count(self.models.data(name) / ada.STORE)
            except sqlite3.Error as err:
                log.warning("a precedent store did not read", extra={"model": name, "reason": str(err)})
                return None
        return total

    def due(self) -> bool:
        """Whether the total is to be reported now: the stores read again when their time has come (a reading that
        fails keeps the one before), and the total not yet reported."""
        now = self.clock()
        if self.read_at is None or now - self.read_at >= READ_EVERY:
            self.read_at = now
            found = self.count()
            if found is not None:
                self.total = found
        return self.total is not None and self.total != self.reported
