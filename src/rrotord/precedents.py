"""The runtime's precedents: the total across its Ada models, each store counted as the cap counts it
(`rrotor.ada.count`), for the link's `usage`, the heartbeat, the licence's view and the cap itself (the runner hands
each Ada run the other models' total). The stores are read again every nine minutes, on the minute's usage tick, so
a change goes up within ten; the total is reported when it differs from the one last reported, and once after every
start (docs/architecture.md, section 6). A total over the licence key's cap is warned of once per change."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable

from rrotor import ada
from rrotord.models import Models

log = logging.getLogger("rrotor")
READ_EVERY = 540.0


def total(models: Models, skip: str | None = None) -> int:
    """The precedents in every model one of whose versions needs Ada (but `skip`); raises sqlite3.Error when a store
    does not read."""
    found = 0
    for name in models.names():
        if name != skip and any("ada" in v.manifest.needs for v in models.versions(name)):
            found += ada.count(models.data(name) / ada.STORE)
    return found


class Precedents:
    def __init__(
        self,
        models: Models,
        clock: Callable[[], float] = time.monotonic,
        cap: Callable[[], int | None] = lambda: None,
    ) -> None:
        self.models, self.clock, self.cap = models, clock, cap
        self.warned: int | None = None  # the total last warned of as over the cap
        self.read_at: float | None = None
        self.total: int | None = None  # the last reading; None until one has read
        self.reported: int | None = None

    def count(self) -> int | None:
        """The runtime's total; None when a store does not read."""
        try:
            return total(self.models)
        except sqlite3.Error as err:
            log.warning("a precedent store did not read", extra={"reason": str(err)})
            return None

    def current(self) -> int | None:
        """The last reading; read now when none has yet (the heartbeat at boot comes before the first tick)."""
        if self.total is None:
            self.total = self.count()
        return self.total

    def _over(self, found: int) -> None:
        """A store over the licence key's cap (after a downgrade) is kept, and not grown: warned of once per change."""
        cap = self.cap()
        over = cap is not None and found > cap
        if over and found != self.warned:
            log.warning(
                "the precedents are over the licence key's cap: kept, but records that grow a store are refused",
                extra={"precedents": found, "cap": cap},
            )
        self.warned = found if over else None

    def due(self) -> bool:
        """Whether the total is to be reported now: the stores read again when their time has come (a reading that
        fails keeps the one before), and the total not yet reported."""
        now = self.clock()
        if self.read_at is None or now - self.read_at >= READ_EVERY:
            self.read_at = now
            found = self.count()
            if found is not None:
                self.total = found
                self._over(found)
        return self.total is not None and self.total != self.reported
