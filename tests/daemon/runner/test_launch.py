import asyncio
import os
from pathlib import Path
from typing import Any, cast

import pytest

from rrotord.runner.launch import Launcher, SandboxLauncher, Spec
from rrotord.sandbox import bwrap

MOUNTS = bwrap.Mounts(Path("/s"), Path("/v"), Path("/k"), Path("/f"), Path("/p.sock"))


class Seccomp:
    def __init__(self) -> None:
        self.fds: list[int] = []

    def build(self) -> int:
        fd = os.open(os.devnull, os.O_RDONLY)
        self.fds.append(fd)
        return fd


def closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_sandbox_tells_the_worker_its_landlock_rules_and_proxy():
    box = SandboxLauncher().sandbox(MOUNTS)
    assert box is not None and box["proxy_socket"] == "/run/rrotor/proxy.sock"
    assert box["landlock"]["tcp_ports"] == [3128]
    no_proxy = SandboxLauncher().sandbox(bwrap.Mounts(Path("/s"), Path("/v"), Path("/k"), Path("/f"), None))
    assert no_proxy is not None and no_proxy["proxy_socket"] is None


def test_paths_are_the_sandboxs():
    assert SandboxLauncher().paths(MOUNTS) == {"program": "/program", "files": "/files"}


def test_the_launcher_interface_has_no_behaviour():
    me = cast(Any, object())
    assert Launcher.sandbox(me, MOUNTS) is None
    assert Launcher.paths(me, MOUNTS) is None


async def test_spawn_runs_bwrap_with_the_filter_and_closes_it(monkeypatch):
    seen: dict[str, Any] = {}

    async def exec_(*argv: str, **kw: Any) -> str:
        seen["argv"], seen["kw"] = argv, kw
        return "proc"

    monkeypatch.setattr("rrotord.runner.launch.asyncio.create_subprocess_exec", exec_)
    seccomp = Seccomp()
    proc = await SandboxLauncher(seccomp).spawn(Spec(MOUNTS, bwrap.Limits(256, 10), 9))  # type: ignore[arg-type]
    fd = seccomp.fds[0]
    assert cast(Any, proc) == "proc" and seen["argv"][0] == "prlimit" and str(fd) in seen["argv"]
    assert seen["kw"]["pass_fds"] == (9, fd) and seen["kw"]["start_new_session"]
    assert closed(fd)


async def test_spawn_that_fails_still_closes_the_filter(monkeypatch):
    async def exec_(*argv: str, **kw: Any) -> None:
        raise FileNotFoundError("prlimit")

    monkeypatch.setattr("rrotord.runner.launch.asyncio.create_subprocess_exec", exec_)
    seccomp = Seccomp()
    with pytest.raises(FileNotFoundError):
        await SandboxLauncher(seccomp).spawn(Spec(MOUNTS, bwrap.Limits(256, 10), 9))  # type: ignore[arg-type]
    assert closed(seccomp.fds[0])
    assert asyncio  # the module's asyncio is the one patched
