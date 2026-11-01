"""Secrets in a run: the declared ones' values reach the worker and nothing persisted (masked in the run's log), an
undeclared name is refused in the program, a declared one not set stops the run before `load`."""

from __future__ import annotations

import json

import pytest

from rrotord.vault import Vault
from tests.daemon.runner.test_engine import Env
from tests.fakes import install, manifest, outcome

READS = """
from rrotor import Result

def start(ctx, inputs):
    key = ctx.secrets["API_KEY"]
    print("the key is " + key)
    ctx.log("using " + key, detail=[key, {"k": key}, 3])
    return Result.done({"length": len(key), "other": ctx.secrets.get("OTHER")})
"""

SNEAKS = """
from rrotor import Result

def start(ctx, inputs):
    return Result.done(ctx.secrets["NOT_DECLARED"])
"""

LOADS = """
from rrotor import Result

def load(ctx):
    raise RuntimeError("load ran")

def start(ctx, inputs):
    return Result.done()
"""

VALUE = "sk-live-0123456789abcdef"


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    yield e
    await e.runner.shutdown()


def deploy(env: Env, source: str, secrets: list[str]) -> None:
    install(env.programs, manifest("sample", secrets=secrets), source)


async def test_a_declared_secret_reaches_the_program_and_is_masked_in_its_log(env, tmp_path):
    vault = Vault(tmp_path / "data")
    env.runner.vault = vault
    vault.set("sample", "API_KEY", VALUE)
    vault.set("sample", "OTHER", "abc")  # shorter than the mask's minimum: read, not masked
    deploy(env, READS, ["API_KEY", "OTHER"])
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert (run.status, outcome(run)["data"]) == ("done", {"length": len(VALUE), "other": "abc"}), run.result
    persisted = (env.records.folder(run.id) / "log.jsonl").read_text() + (
        env.records.folder(run.id) / "run.json"
    ).read_text()
    assert VALUE not in persisted
    lines = [line for line in env.records.lines(run.id) if "line" in line or "message" in line]
    assert {"stream": "stdout", "line": "the key is [secret]"} in [
        {k: v for k, v in line.items() if k != "at"} for line in lines
    ]
    logged = next(line for line in lines if line.get("message", "").startswith("using"))
    assert logged["message"] == "using [secret]" and logged["detail"] == {"detail": ["[secret]", {"k": "[secret]"}, 3]}
    assert env.runner.hidden == {}


async def test_a_declared_secret_not_set_stops_the_run_before_load(env, tmp_path):
    env.runner.vault = Vault(tmp_path / "data")
    env.runner.vault.set("sample", "API_KEY", VALUE)
    deploy(env, LOADS, ["API_KEY", "ANALYTICS_KEY", "OTHER_KEY"])
    run = await env.runner.call(program="sample", function="start", inputs={})
    result = outcome(run)
    assert (run.status, result["step"], result["data"]["code"]) == ("failed", "secrets", "E_SECRET_MISSING")
    assert result["data"]["missing"] == ["ANALYTICS_KEY", "OTHER_KEY"]
    assert "the secrets ANALYTICS_KEY, OTHER_KEY" in result["data"]["reason"] and "set them" in result["data"]["reason"]
    assert env.launcher.specs == []  # no worker started: load never ran


async def test_without_a_vault_every_declared_secret_is_missing(env):
    deploy(env, LOADS, ["API_KEY"])
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert outcome(run)["data"]["missing"] == ["API_KEY"] and "set it" in outcome(run)["data"]["reason"]


async def test_a_secret_the_key_does_not_open_fails_the_run(env, tmp_path):
    env.runner.vault = Vault(tmp_path / "data")
    env.runner.vault.set("sample", "API_KEY", VALUE)
    path = tmp_path / "data" / "vault" / "sample.json"
    path.write_text(json.dumps({"API_KEY": "AAAA"}))
    deploy(env, LOADS, ["API_KEY"])
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert (outcome(run)["step"], outcome(run)["data"]["code"]) == ("secrets", "E_VAULT")


async def test_an_undeclared_name_is_refused_in_the_program(env, tmp_path):
    env.runner.vault = Vault(tmp_path / "data")
    env.runner.vault.set("sample", "NOT_DECLARED", VALUE)  # set, but the manifest does not declare it
    deploy(env, SNEAKS, [])
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert (run.status, outcome(run)["data"]["code"]) == ("failed", "E_SECRET_UNDECLARED")


async def test_a_resumed_run_in_a_new_worker_gets_its_secrets_again(env, tmp_path):
    env.runner.vault = Vault(tmp_path / "data")
    env.runner.vault.set("sample", "API_KEY", VALUE)
    source = (
        "from rrotor import Result\n\ndef start(ctx, inputs):\n"
        "    ok = ctx.ask_approval('write', 'go?')\n    return Result.done(ctx.secrets['API_KEY'] == "
        f"{VALUE!r} and ok)\n"
    )
    install(
        env.programs, manifest("sample", secrets=["API_KEY"], functions={"start": {"approvals": ["write"]}}), source
    )
    run = await env.runner.call(program="sample", function="start", inputs={})
    assert run.status == "needs_approval"
    await env.runner.shutdown()
    restarted = env.make()
    restarted.vault = env.runner.vault
    run = await restarted.resume(run.id, {"approved": True})
    assert outcome(run)["data"] is True
    env.runner = restarted
