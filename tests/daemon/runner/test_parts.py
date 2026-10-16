"""A run that needs parts: refused while one is missing or its mod is not in the plan; else each is mounted read-only
and on the model's PYTHONPATH, so the model imports from it."""

from __future__ import annotations

from rrotord.licence import Licence
from rrotord.parts import Parts
from rrotord.sandbox import bwrap
from tests.daemon.runner.test_engine import Env
from tests.fakes import keys, part_archive

USES_PART = """
from rrotor import Result
import kernel

def start(ctx, inputs):
    return Result.done({"value": kernel.VALUE})
"""


def _ready(root, sock, mods: list[str]) -> tuple[Env, Parts, Licence]:
    store = Parts(root, keys().public, "arm64")
    lic = Licence(root, keys().public)
    given = keys().licence(mods)
    lic.set(given["payload"], given["signature"])
    env = Env(root, sock)
    env.runner = env.make()
    env.runner.parts, env.runner.licence = store, lic
    return env, store, lic


async def test_a_missing_part_names_itself_and_the_mod_that_brings_it(tmp_path, short_dir):
    env, _, _ = _ready(tmp_path, short_dir, ["ada"])
    env.deploy(USES_PART, needs=["ada"], databases=["postgres"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert run.result["data"]["code"] == "E_PART_MISSING"
    assert "needs the parts postgres, ada, not installed (from the ada, rrotor mods)" in run.result["data"]["reason"]


async def test_a_part_not_in_the_plan_refuses_the_run(tmp_path, short_dir):
    env, store, _ = _ready(tmp_path, short_dir, ["Other"])
    data, sha, sig = part_archive(tmp_path)
    store.install("ada", "1.0.0", data, sha, sig)
    env.deploy(USES_PART, needs=["ada"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert (
        run.result["data"]["code"] == "E_NOT_IN_PLAN"
        and "ada is not in this machine's plan" in run.result["data"]["reason"]
    )


async def test_an_installed_licensed_part_is_mounted_and_importable(tmp_path, short_dir):
    env, store, _ = _ready(tmp_path, short_dir, ["ada"])
    data, sha, sig = part_archive(tmp_path)
    made = store.install("ada", "1.0.0", data, sha, sig)
    env.deploy(USES_PART, needs=["ada"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert run.result == {"status": "done", "data": {"value": 7}, "step": None, "prompt": None}
    spec = env.launcher.specs[-1]
    assert spec.mounts.parts == (("ada", made.path),) and spec.mounts.pythonpath == ("/opt/parts/ada/lib",)
    argv = bwrap.command(spec.mounts, spec.limits, 9, 3, exists=lambda p: False)
    assert argv[argv.index("--ro-bind", argv.index(str(made.path)) - 1) + 2] == "/opt/parts/ada"
    assert "PYTHONPATH" in argv and argv[argv.index("PYTHONPATH") + 1] == "/opt/rrotor:/opt/parts/ada/lib"
    assert "/opt/parts/ada" in bwrap.landlock_rules(spec.mounts)["read"]


async def test_without_a_parts_store_every_part_is_missing(tmp_path, short_dir):
    env = Env(tmp_path, short_dir)
    env.deploy(USES_PART, needs=["ada"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert run.result["data"]["code"] == "E_PART_MISSING"


async def test_a_base_part_needs_no_plan(tmp_path, short_dir):
    env, store, _ = _ready(tmp_path, short_dir, ["other"])
    data, sha, sig = part_archive(tmp_path, name="postgres", mod="rrotor", files={"lib/kernel.py": "VALUE = 7\n"})
    store.install("postgres", "1.0.0", data, sha, sig)
    env.deploy(USES_PART, databases=["postgres"])
    run = await env.runner.call(model="sample", function="start", inputs={})
    assert run.result["status"] == "done"
