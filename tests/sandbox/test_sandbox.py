"""The real sandbox holds against a hostile program: bwrap, Landlock, seccomp and the egress proxy, as rrotor runs them.

Linux only, inside the image (bwrap, libseccomp, prlimit, Python 3.13 at /usr/bin/python3.13, git, uv), with /data
writable: `pytest -m sandbox -o addopts='' tests/sandbox`."""

from __future__ import annotations

import socket
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import rrotor
from rrotord.deploy import Deployer
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.launch import SandboxLauncher
from rrotord.runner.record import Records
from rrotord.sandbox.probe import probe
from tests.fakes import GitRepo, manifest, token

pytestmark = pytest.mark.sandbox

DECLARED = "example.com"

HOSTILE = r"""
import ctypes
import os
import socket

from rrotor import Result


def _try(fn):
    try:
        fn()
    except Exception:
        return "blocked"
    return "open"


def _read(path):
    with open(path) as f:
        return f.read()


def _write(path):
    with open(path, "w") as f:
        f.write("owned")


def _connect(host, port):
    socket.create_connection((host, port), timeout=3).close()


def _proxy(target):
    s = socket.create_connection(("127.0.0.1", 3128), timeout=15)
    try:
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        return s.recv(200).split(b"\r\n")[0].decode()
    finally:
        s.close()


def probe(ctx, inputs):
    libc = ctypes.CDLL(None, use_errno=True)
    checks = {
        "read_runtime_json": _try(lambda: _read(inputs["runtime_json"])),
        "read_data_runtime_json": _try(lambda: _read("/data/runtime.json")),
        "read_other_program": _try(lambda: os.listdir(inputs["other_files"])),
        "write_program": _try(lambda: _write("/program/owned.txt")),
        "write_own_folder": _try(lambda: _write(str(ctx.files / "mine.txt"))),
        "local_api": _try(lambda: _connect("127.0.0.1", 7777)),
        "direct_internet": _try(lambda: _connect("1.1.1.1", 443)),
        "unshare": _try(lambda: os.unshare(os.CLONE_NEWUSER)),
        "mount": "open" if libc.mount(b"none", b"/tmp", b"tmpfs", 0, None) == 0 else "blocked",
        "proxy_undeclared": _proxy("evil.example.org:443"),
        "proxy_declared_80": _proxy(inputs["host"] + ":80"),
        "proxy_declared_443": _proxy(inputs["host"] + ":443"),
    }
    return Result.done(checks)
"""


@dataclass
class Box:
    root: Path
    programs: Programs
    runner: Runner


@pytest.fixture
async def box() -> AsyncIterator[Box]:
    layers = await probe()
    assert layers.ready, layers.why()
    root = Path(tempfile.mkdtemp(prefix="sandbox-test-", dir="/data"))
    (root / "runtime.json").write_text(f'{{"token": "{token()}"}}')
    programs = Programs(root)
    deployer = Deployer(programs, Path(tempfile.mkdtemp(prefix="rr-deploy-")))
    for name, source, hosts in (("hostile", HOSTILE, [DECLARED]), ("other", "def start(ctx, inputs):\n    pass\n", [])):
        repo = GitRepo(Path(tempfile.mkdtemp(prefix="rr-repo-")) / name)
        functions: dict[str, Any] = {"probe": {}} if name == "hostile" else {"start": {}}
        sha = repo.program(manifest(name, functions=functions, hosts=hosts), source, **{"files/seed.txt": "seed"})
        await deployer.from_bundle(repo.bundle(repo.path.parent / f"{name}.bundle"), sha)
    runner = Runner(
        programs=programs,
        records=Records(root),
        proxy=EgressProxy(Path(tempfile.mkdtemp(prefix="rr-proxy-", dir="/tmp"))),
        launcher=SandboxLauncher(),
        sdk=Path(rrotor.__file__).parent,
        layers=layers,
    )
    yield Box(root, programs, runner)
    await runner.shutdown()


async def checks(box: Box) -> dict[str, Any]:
    inputs = {
        "runtime_json": str(box.root / "runtime.json"),
        "other_files": str(box.programs.files("other")),
        "host": DECLARED,
    }
    run = await box.runner.call(program="hostile", function="probe", inputs=inputs)
    assert run.status == "done", run.result
    data: dict[str, Any] = run.result["data"]  # type: ignore[index]
    return data


async def test_files_outside_its_own_folder_cannot_be_reached(box):
    found = await checks(box)
    assert found["read_runtime_json"] == "blocked"
    assert found["read_data_runtime_json"] == "blocked"
    assert found["read_other_program"] == "blocked"
    assert found["write_program"] == "blocked"
    assert found["write_own_folder"] == "open"
    assert (box.programs.files("hostile") / "mine.txt").read_text() == "owned"


async def test_the_network_is_only_the_proxy(box):
    found = await checks(box)
    assert found["local_api"] == "blocked"
    assert found["direct_internet"] == "blocked"
    assert found["proxy_undeclared"].startswith("HTTP/1.1 403")
    assert found["proxy_declared_80"].startswith("HTTP/1.1 403")


async def test_namespaces_and_mounts_are_refused(box):
    found = await checks(box)
    assert found["unshare"] == "blocked"
    assert found["mount"] == "blocked"


async def test_a_declared_host_on_443_goes_through(box):
    try:
        socket.getaddrinfo(DECLARED, 443)
    except OSError:
        pytest.skip(f"{DECLARED} does not resolve from here")
    assert (await checks(box))["proxy_declared_443"].startswith("HTTP/1.1 200")
