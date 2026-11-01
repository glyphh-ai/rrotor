"""The vendors' agent CLIs the image carries, in the real sandbox: each deployed as rrotord does at boot, each runs
there; with RROTOR_VENDOR_SIGN_IN=1 (it reaches the vendors), each one's sign-in starts there and shows the address
(and device code) to open, and is cancelled.

Linux only, inside the image: `pytest -m sandbox -o addopts='' tests/sandbox`."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import rrotor
from rrotord.deploy import Deployer
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.launch import SandboxLauncher
from rrotord.runner.record import Records
from rrotord.sandbox.probe import probe

pytestmark = pytest.mark.sandbox

IMAGE = Path("/usr/local/lib/rrotor-clis")
NAMES = sorted(p.parent.name for p in IMAGE.glob("*/glyphh.json"))
SIGN_IN = {  # what each one's sign-in shows: the address to open, and whether a device code comes with it
    "claude": ("https://claude.com/cai/oauth/authorize?", False),
    "codex": ("https://auth.openai.com/codex/device", True),
    "gemini": ("https://accounts.google.com/o/oauth2/", False),
    "cursor": ("https://cursor.com/loginDeepControl?", False),
    "grok": ("https://accounts.x.ai/", True),
    "copilot": ("https://github.com/login/device", True),
    "glyphh": ("https://platform.glyphh.ai", False),
}


@pytest.fixture
async def vendors() -> AsyncIterator[Runner]:
    layers = await probe()
    assert layers.ready, layers.why()
    root = Path(tempfile.mkdtemp(prefix="sandbox-vendors-", dir="/data"))
    programs = Programs(root)
    deployer = Deployer(programs, Path(tempfile.mkdtemp(prefix="rr-deploy-")))
    for name in NAMES:
        assert await deployer.builtin(IMAGE / name) is not None
    made = Runner(
        programs=programs,
        records=Records(root),
        proxy=EgressProxy(Path(tempfile.mkdtemp(prefix="rr-proxy-", dir="/tmp"))),
        launcher=SandboxLauncher(),
        sdk=Path(rrotor.__file__).parent,
        layers=layers,
    )
    yield made
    await made.shutdown()


def test_the_image_carries_every_vendor():
    assert sorted(SIGN_IN) == NAMES


async def test_each_vendor_cli_runs_in_the_sandbox(vendors):
    listed = {c["name"]: c for c in vendors.programs.clis()}
    wrong = {}
    for name in NAMES:
        version = json.loads((IMAGE / name / "vendor.json").read_text())["version"]
        assert listed[name]["builtin"]["version"] == version and listed[name]["login"] is not None
        run = await vendors.cli_run(cli=name, args=["--version"])
        data = run.result["data"]
        shown = data.get("stdout", "") + data.get("stderr", "")
        if run.status != "done" or data["exit_code"] != 0 or version.split("-")[0] not in shown:
            wrong[name] = run.result
    assert wrong == {}


@pytest.mark.skipif(os.environ.get("RROTOR_VENDOR_SIGN_IN") != "1", reason="reaches the vendors' sign-in services")
async def test_each_vendor_sign_in_shows_its_address_in_the_sandbox(vendors):
    wrong = {}
    for name in NAMES:
        prefix, device = SIGN_IN[name]
        run = await vendors.cli_login(cli=name)
        data = run.result["data"]
        if run.status != "needs_input" or not str(data.get("url")).startswith(prefix) or bool(data["code"]) != device:
            wrong[name] = run.result
        if not run.final:
            await vendors.cancel(run.id)
    assert wrong == {}
