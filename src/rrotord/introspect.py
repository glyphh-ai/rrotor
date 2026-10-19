"""A Glyphh sign-in on a hosted rrotor. Claude connects to the runtime's address and signs in once, to Glyphh: the
server is the runtime's OAuth authorization server (`/.well-known/oauth-protected-resource` here names it) and issues
a token bound to this runtime's address. rrotor cannot read that token; it asks the Cloud whether it is good for this
runtime (`POST /v1/runtime/introspect`, the runtime's own token as auth; docs/architecture.md, section 6) and keeps
the answer a minute, good or bad, so a chatty client costs the Cloud one call a minute. No Cloud, no answer: a
runtime key still works without one. The answer's `sub`, the subject signed in, is who the audit log names."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from rrotord.link.credentials import Credentials, digest

log = logging.getLogger("rrotor.introspect")

Post = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[tuple[int, Any]]]

TTL = 60.0
KEEP = 1000  # answers kept; past it the oldest tenth goes


class Introspector:
    def __init__(
        self,
        server: str,
        credentials: Credentials,
        post: Post,
        ttl: float = TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = server.rstrip("/") + "/v1/runtime/introspect"
        self.credentials = credentials
        self.post = post
        self.ttl = ttl
        self.clock = clock
        self.answers: dict[str, tuple[bool, str | None, float]] = {}  # the token's SHA-256: (active, sub, good until)

    async def active(self, token: str) -> bool:
        """Whether the Cloud says this token is a live sign-in for this runtime."""
        return (await self.check(token))[0]

    async def check(self, token: str) -> tuple[bool, str | None]:
        """Whether the token is a live sign-in for this runtime, and its subject (None when the Cloud names none)."""
        key = digest(token)
        now = self.clock()
        held = self.answers.get(key)
        if held is not None and held[2] > now:
            return held[0], held[1]
        active, sub = await self._ask(token)
        if len(self.answers) >= KEEP:
            for old in sorted(self.answers, key=lambda k: self.answers[k][2])[: KEEP // 10]:
                del self.answers[old]
        self.answers[key] = (active, sub, now + self.ttl)
        return active, sub

    async def _ask(self, token: str) -> tuple[bool, str | None]:
        own = self.credentials.token
        if not own:
            return False, None
        try:
            status, body = await self.post(self.url, {"Authorization": f"Bearer {own}"}, {"token": token})
        except Exception as err:  # noqa: BLE001  (whatever the Cloud or the network did: not signed in, for now)
            log.warning("introspection failed", extra={"error": type(err).__name__})
            return False, None
        if status != 200 or not isinstance(body, dict) or body.get("active") is not True:
            return False, None
        sub = body.get("sub")
        return True, sub if isinstance(sub, str) else None

    def forget(self) -> None:
        self.answers.clear()
