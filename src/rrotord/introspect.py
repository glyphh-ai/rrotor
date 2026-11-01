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
from dataclasses import dataclass
from typing import Any

from rrotord.link.credentials import Credentials, digest

log = logging.getLogger("rrotor.introspect")

Post = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[tuple[int, Any]]]

TTL = 60.0
KEEP = 1000  # answers kept; past it the oldest tenth goes


@dataclass(frozen=True, slots=True)
class Identity:
    """Who a token is, as the Cloud's introspection says: live or not, the subject, their role (`admin` or `member`)
    and their groups."""

    active: bool
    sub: str | None = None
    role: str | None = None
    groups: tuple[str, ...] = ()

    @property
    def roles(self) -> set[str]:
        """What an app's `roles` is matched against: the role and the groups, lower-cased."""
        return {r.lower() for r in (self.role, *self.groups) if r}

    def actor(self) -> dict[str, Any]:
        """The audit log's actor for this sign-in (rrotord/audit.py): the subject, and the role and groups the Cloud
        named, when it named any."""
        actor: dict[str, Any] = {"type": "sign-in", "sub": self.sub}
        if self.role is not None:
            actor["role"] = self.role
        if self.groups:
            actor["groups"] = list(self.groups)
        return actor


NOBODY = Identity(False)


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
        self.answers: dict[str, tuple[Identity, float]] = {}  # the token's SHA-256: (who, good until)

    async def active(self, token: str) -> bool:
        """Whether the Cloud says this token is a live sign-in for this runtime."""
        return (await self.who(token)).active

    async def check(self, token: str) -> tuple[bool, str | None]:
        """Whether the token is a live sign-in for this runtime, and its subject (None when the Cloud names none)."""
        found = await self.who(token)
        return found.active, found.sub

    async def who(self, token: str) -> Identity:
        """Whether the token is a live sign-in for this runtime, and who: the subject, their role in the org and
        their groups (the identity provider's, docs/architecture.md, 5.8), as the Cloud names them."""
        key = digest(token)
        now = self.clock()
        held = self.answers.get(key)
        if held is not None and held[1] > now:
            return held[0]
        found = await self._ask(token)
        if len(self.answers) >= KEEP:
            for old in sorted(self.answers, key=lambda k: self.answers[k][1])[: KEEP // 10]:
                del self.answers[old]
        self.answers[key] = (found, now + self.ttl)
        return found

    async def _ask(self, token: str) -> Identity:
        own = self.credentials.token
        if not own:
            return NOBODY
        try:
            status, body = await self.post(self.url, {"Authorization": f"Bearer {own}"}, {"token": token})
        except Exception as err:  # noqa: BLE001  (whatever the Cloud or the network did: not signed in, for now)
            log.warning("introspection failed", extra={"error": type(err).__name__})
            return NOBODY
        if status != 200 or not isinstance(body, dict) or body.get("active") is not True:
            return NOBODY
        sub, role, groups = body.get("sub"), body.get("role"), body.get("groups")
        return Identity(
            True,
            sub if isinstance(sub, str) else None,
            role if isinstance(role, str) else None,
            tuple(str(g) for g in groups) if isinstance(groups, list) else (),
        )

    def forget(self) -> None:
        self.answers.clear()
