"""How a run's worker process starts: in the sandbox (bwrap, seccomp, rlimits; the worker applies Landlock)."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rrotord.sandbox import bwrap
from rrotord.sandbox.seccomp import Filter


@dataclass(frozen=True, slots=True)
class Spec:
    mounts: bwrap.Mounts
    limits: bwrap.Limits
    channel_fd: int


class Launcher(Protocol):
    def sandbox(self, mounts: bwrap.Mounts) -> dict[str, Any] | None:
        """What the worker is told to lock itself to (its start message's `sandbox`)."""
        ...

    def paths(self, mounts: bwrap.Mounts) -> dict[str, str | None]:
        """Where the worker finds the program, its folder and its data folder (its start message's `program`, `files`
        and `data`, the last None when the manifest asks for none)."""
        ...

    async def spawn(self, spec: Spec) -> asyncio.subprocess.Process: ...


class SandboxLauncher:
    def __init__(self, seccomp: Filter | None = None) -> None:
        self._seccomp = seccomp

    def sandbox(self, mounts: bwrap.Mounts) -> dict[str, Any] | None:
        return {"landlock": bwrap.landlock_rules(mounts), "proxy_socket": bwrap.PROXY_SOCKET if mounts.proxy else None}

    def paths(self, mounts: bwrap.Mounts) -> dict[str, str | None]:
        return {"program": bwrap.PROGRAM, "files": bwrap.FILES, "data": bwrap.DATA if mounts.data else None}

    async def spawn(self, spec: Spec) -> asyncio.subprocess.Process:
        seccomp = (self._seccomp or Filter()).build()
        try:
            argv = bwrap.command(spec.mounts, spec.limits, seccomp, spec.channel_fd)
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(spec.channel_fd, seccomp),
                start_new_session=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                cwd=Path("/"),
            )
        finally:
            os.close(seccomp)
