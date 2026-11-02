"""rrotor.js, the page's client, run in node against a fake `fetch` and a bare `document` (rrotor_harness.cjs beside
this file): calls and their outcomes, a waiting run continued, the app read once, `ready()` at once for an app of
run_as app, and the sign-in card's run for a viewer's app (the sign-in started, the device code continued, the run
polled until done), a sign-in that fails. Skipped where there is no node (the repo has no JavaScript toolchain;
CI's runner has node)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from rrotord import apps

HARNESS = Path(__file__).with_name("rrotor_harness.cjs")  # .cjs: CommonJS whatever a package.json above says


def test_the_client_calls_continues_and_signs_in():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node here: rrotor.js is checked in CI's runner")
    done = subprocess.run(
        [node, str(HARNESS), str(apps.STATIC / apps.CLIENT)], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr
