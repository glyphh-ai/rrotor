"""How the nodes find each other (docs/specs/cluster.md, 4): a static list, the same on every node
(`RROTOR_CLUSTER_PEERS`, Compose and Terraform), or a name whose A and AAAA records are the members
(`RROTOR_CLUSTER_DNS`, Kubernetes' headless Service with `publishNotReadyAddresses: true`), resolved at most every
10 seconds and dialled on `RROTOR_CLUSTER_PORT`. Only how they are found: membership is the head's."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("rrotor.cluster")
RESOLVE_EVERY = 10.0

Resolve = Callable[[str, int], Awaitable[list[Any]]]


async def getaddrinfo(name: str, port: int) -> list[Any]:
    return await asyncio.get_running_loop().getaddrinfo(name, port, type=socket.SOCK_STREAM)


def joined(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class Discovery:
    def __init__(
        self,
        peers: tuple[str, ...],
        dns: str | None,
        port: int,
        resolve: Resolve = getaddrinfo,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.peers, self.dns, self.port, self.resolve, self.clock = peers, dns, port, resolve, clock
        self.found: list[str] = []
        self.at: float | None = None

    async def addresses(self) -> list[str]:
        """Where the members may be, host:port each: the list as given, or the name's addresses as last resolved (a
        failure logged and the last answer kept)."""
        if self.dns is None:
            return list(self.peers)
        now = self.clock()
        if self.at is not None and now - self.at < RESOLVE_EVERY:
            return list(self.found)
        self.at = now
        try:
            infos = await self.resolve(self.dns, self.port)
        except OSError as err:
            log.warning("cluster: the peers' name did not resolve", extra={"dns": self.dns, "reason": str(err)})
            return list(self.found)
        self.found = sorted({joined(str(info[4][0]), self.port) for info in infos})
        return list(self.found)
