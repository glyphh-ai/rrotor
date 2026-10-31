"""A run that needs parts: refused while one is missing (never by the licence); else each is mounted read-only and on
the model's PYTHONPATH, so the model imports from it."""

from __future__ import annotations

from rrotord.parts import Parts
from rrotord.sandbox import bwrap
from tests.daemon.runner.test_engine import Env
from tests.fakes import keys, outcome, part_archive

USES_PART = """
from rrotor import Result
import kernel

def start(ctx, inputs):
    return Result.done({"value": kernel.VALUE})
"""


def _ready(root, sock) -> tuple[Env, Parts]:
    store = Parts(root, keys().public, "arm64")
    env = Env(root, sock)
    env.runner = env.make()
    env.runner.parts = store
    return env, store


async def test_a_missing_part_names_itself_and_the_mod_that_brings_it(tmp_path, short_dir):
    env, _ = _ready(tmp_path, short_dir)
    env.deploy(USES_PART, needs=["tools"], databases=["postgres"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert outcome(run)["data"]["code"] == "E_PART_MISSING"
    assert (
        "needs the parts postgres, tools, not installed (from the rrotor, tools mods)" in outcome(run)["data"]["reason"]
    )


async def test_an_installed_part_is_mounted_and_importable_with_no_licence_key(tmp_path, short_dir):
    env, store = _ready(tmp_path, short_dir)
    data, sha, sig = part_archive(tmp_path)
    made = store.install("tools", "1.0.0", data, sha, sig)
    env.deploy(USES_PART, needs=["tools"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert run.result == {"status": "done", "data": {"value": 7}, "step": None, "prompt": None}
    spec = env.launcher.specs[-1]
    assert spec.mounts.parts == (("tools", made.path),) and spec.mounts.pythonpath == ("/opt/parts/tools/lib",)
    argv = bwrap.command(spec.mounts, spec.limits, 9, 3, exists=lambda p: False)
    assert argv[argv.index("--ro-bind", argv.index(str(made.path)) - 1) + 2] == "/opt/parts/tools"
    assert "PYTHONPATH" in argv and argv[argv.index("PYTHONPATH") + 1] == "/opt/rrotor:/opt/parts/tools/lib"
    read = bwrap.landlock_rules(spec.mounts)["read"]
    assert isinstance(read, list) and "/opt/parts/tools" in read


async def test_without_a_parts_store_every_part_is_missing(tmp_path, short_dir):
    env = Env(tmp_path, short_dir)
    env.deploy(USES_PART, needs=["tools"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert outcome(run)["data"]["code"] == "E_PART_MISSING"


async def test_a_base_part_needs_no_plan(tmp_path, short_dir):
    env, store = _ready(tmp_path, short_dir)
    data, sha, sig = part_archive(tmp_path, name="postgres", mod="rrotor", files={"lib/kernel.py": "VALUE = 7\n"})
    store.install("postgres", "1.0.0", data, sha, sig)
    env.deploy(USES_PART, databases=["postgres"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert outcome(run)["status"] == "done"
