"""CLI runs end to end: real worker processes (the host's Python, no sandbox) running a CLI installed from archives made
here: allow and approval patterns, env from the vault, masking, the private HOME, the working folder, caps, audit."""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import sys
from typing import Any

import pytest

from rrotord import clis
from rrotord.audit import Audit, Who, acting
from rrotord.errors import BadRequest, Forbidden, Unavailable
from rrotord.sandbox.probe import Layers
from rrotord.vault import Vault
from tests.daemon.runner.test_engine import Env
from tests.fakes import (
    ECHO,
    FakeFetch,
    cli_manifest,
    console_wheel,
    install,
    install_cli,
    manifest,
    outcome,
    sha,
    tarball,
)

URL = "https://example.com/tool.tar.gz"
TOKEN = "ghp_0123456789abcdefSECRET"


@pytest.fixture
async def env(tmp_path, short_dir):
    e = Env(tmp_path / "data", short_dir)
    e.runner.vault = Vault(tmp_path / "data")
    e.runner.audit = Audit(tmp_path / "audit", "rrotor-1", tier=lambda: "enterprise", payloads=True)
    yield e
    await e.runner.shutdown()


async def deploy(env: Env, script: str = ECHO, cli: dict[str, Any] | None = None, **extra: Any) -> None:
    archive = tarball({"tool/bin/tool": script}, {"tool/bin/tool": 0o755})
    block = {"install": {"url": URL, "sha256": sha(archive)}} | (cli or {})
    await install_cli(env.programs, cli_manifest(cli=block, **extra), FakeFetch({URL: archive}))


async def test_a_command_runs_with_its_env_and_answers_its_output_masked(env, tmp_path):
    await deploy(env, cli={"env": {"TOKEN": "secret:TOKEN"}}, secrets=["TOKEN"])
    env.runner.vault.set("tool", "TOKEN", TOKEN)
    run = await env.runner.cli_run(cli="tool", args=["pr", "list", TOKEN])
    data = outcome(run)["data"]
    assert (run.status, run.function, data["exit_code"]) == ("done", "cli", 3)
    assert data["stdout"] == "args: pr list [secret]\ntoken: [secret]\nhome: /tmp\n"
    assert data["stderr"] == "to stderr\n" and data["truncated"] is False
    assert data["files"] == {} and data["files_skipped"] == []
    assert TOKEN not in (env.records.folder(run.id) / "run.json").read_text()
    logged = [line.get("line") for line in env.records.lines(run.id) if "stream" in line]
    assert "token: [secret]" in logged and "to stderr" in logged
    events = env.runner.audit.read(None, 10)[0]
    call = next(e for e in events if e["kind"] == "run.call")
    assert call["command"] == "pr list [secret]" and call["function"] == "cli"
    assert not (env.records.folder(run.id) / "work").exists()
    assert run.inputs["stdin"] is None


async def test_arguments_the_cli_does_not_allow_are_refused_and_audited(env):
    await deploy(env, cli={"allow": ["pr *"]})
    with pytest.raises(Forbidden) as caught:
        await env.runner.cli_run(cli="tool", args=["repo", "delete", "x"])
    assert caught.value.code == "E_CLI_DENIED" and "pr *" in caught.value.message
    event = env.runner.audit.read(None, 10)[0][0]
    assert (event["outcome"], event["command"]) == ({"status": "refused", "code": "E_CLI_DENIED"}, "repo delete x")
    assert (await env.runner.cli_run(cli="tool", args=["pr", "view", "1"])).status == "done"


async def test_a_command_that_needs_approval_waits_then_runs(env):
    await deploy(env, cli={"approvals": ["pr merge *"]})
    run = await env.runner.cli_run(cli="tool", args=["pr", "merge", "7"], files={"in.txt": "kept while waiting"})
    assert (run.status, run.result["step"], run.result["data"]) == ("needs_approval", "cli", {"command": "pr merge 7"})
    assert "pr merge *" in run.result["prompt"] and env.runner.approvals()[0]["run_id"] == run.id
    assert (env.records.folder(run.id) / "work" / "in.txt").is_file()
    done = await env.runner.resume(run.id, {"approved": True})
    assert (done.status, outcome(done)["data"]["stdout"].splitlines()[0]) == ("done", "args: pr merge 7")
    declined = await env.runner.cli_run(cli="tool", args=["pr", "merge", "8"])
    ended = await env.runner.resume(declined.id, {"approved": False})
    assert (ended.status, outcome(ended)["data"]["code"]) == ("failed", "E_CLI_DECLINED")
    assert not (env.records.folder(declined.id) / "work").exists()


async def test_a_waiting_command_that_expires_or_is_cancelled_leaves_nothing(env):
    await deploy(env, cli={"approvals": ["*"]})
    waiting = await env.runner.cli_run(cli="tool", args=["x"], stdin="secret-ish input")
    cancelled = await env.runner.cancel(waiting.id)
    assert cancelled.status == "cancelled" and cancelled.inputs["stdin"] == len("secret-ish input")
    late = await env.runner.cli_run(cli="tool", args=["y"])
    gone = await env.runner.expire(dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    assert gone == [late.id] and not (env.records.folder(late.id) / "work").exists()


async def test_stdin_files_in_and_out_and_the_private_home(env):
    script = (
        '#!/bin/sh\ncat > out.txt\necho "in: $(cat d/in.txt)"\n'
        'echo x >> "$HOME/count"\necho "calls: $(wc -l < "$HOME/count" | tr -d \' \')"\n'
    )
    await deploy(env, script, cli={"home": True})
    first = await env.runner.cli_run(cli="tool", args=[], stdin="from stdin", files={"d/in.txt": "given"})
    data = outcome(first)["data"]
    assert data["stdout"] == "in: given\ncalls: 1\n" and data["files"] == {"out.txt": "from stdin"}
    second = await env.runner.cli_run(cli="tool", args=[], files={"d/in.txt": "again"})
    assert outcome(second)["data"]["stdout"] == "in: again\ncalls: 2\n"
    assert (env.programs.folder("tool") / "home" / "count").read_text() == "x\nx\n"
    spec = env.launcher.specs[-1].mounts
    assert spec.home == env.programs.folder("tool") / "home" and spec.cli == env.programs.get("tool").root / "cli"


async def test_each_signed_in_person_has_their_own_home(env):
    """A Glyphh sign-in's subject runs the CLI in homes/<subject>, theirs alone; a runtime key, the local token and
    rrotor itself share the CLI's own home; a subject that is no folder name is hashed."""
    script = '#!/bin/sh\necho x >> "$HOME/count"\necho "calls: $(wc -l < "$HOME/count" | tr -d \' \')"\n'
    await deploy(env, script, cli={"home": True})
    folder = env.programs.folder("tool")
    with acting(Who({"type": "sign-in", "sub": "user_1"})):
        one = await env.runner.cli_run(cli="tool", args=[])
        again = await env.runner.cli_run(cli="tool", args=[])
    with acting(Who({"type": "sign-in", "sub": "user_2"})):
        two = await env.runner.cli_run(cli="tool", args=[])
    with acting(Who({"type": "key", "key_sha256": "k" * 64})):
        key = await env.runner.cli_run(cli="tool", args=[])
    nobody = await env.runner.cli_run(cli="tool", args=[])
    assert [outcome(r)["data"]["stdout"] for r in (one, again, two, key, nobody)] == [
        "calls: 1\n",
        "calls: 2\n",
        "calls: 1\n",
        "calls: 1\n",
        "calls: 2\n",
    ]
    assert (one.subject, two.subject, key.subject, nobody.subject) == ("user_1", "user_2", None, None)
    assert env.records.load(one.id).subject == "user_1"  # kept with the run, for a continue after a restart
    assert (folder / "homes" / "user_1" / "count").read_text() == "x\nx\n"
    assert (folder / "homes" / "user_2" / "count").read_text() == "x\n"
    assert (folder / "home" / "count").read_text() == "x\nx\n"
    with acting(Who({"type": "sign-in", "sub": "../odd one"})):
        odd = await env.runner.cli_run(cli="tool", args=[])
    hashed = hashlib.sha256(b"../odd one").hexdigest()[:32]
    assert env.launcher.specs[-1].mounts.home == folder / "homes" / hashed and odd.subject == "../odd one"
    assert not (folder / "odd one").exists()
    with acting(Who({"type": "sign-in", "sub": ""})):  # a sign-in the Cloud named no subject for
        assert (await env.runner.cli_run(cli="tool", args=[])).subject is None
    assert env.programs.clis("user_1")[0]["signed_in"] is None  # no login: nothing says whether it is signed in


async def test_output_past_its_cap_is_cut_in_the_result_and_kept_in_the_log(env, monkeypatch):
    monkeypatch.setattr(clis, "OUTPUT_MAX", 10)
    await deploy(env, "#!/bin/sh\nfor i in 1 2 3 4 5; do echo line$i; done\n")
    data = outcome(await env.runner.cli_run(cli="tool", args=[]))["data"]
    assert (data["stdout"], data["stdout_bytes"], data["truncated"]) == ("line1\nline", 30, True)


async def test_a_command_past_its_seconds_is_stopped(env):
    await deploy(env, "#!/bin/sh\nsleep 30\n", limits={"seconds": 1})
    run = await env.runner.cli_run(cli="tool", args=[])
    assert (run.status, outcome(run)["data"]["code"]) == ("failed", "E_LIMIT_SECONDS")


async def test_a_command_that_cannot_start_fails_the_run(env):
    await deploy(env)
    (env.programs.get("tool").root / "cli" / "files" / "tool" / "bin" / "tool").unlink()
    run = await env.runner.cli_run(cli="tool", args=[])
    assert (run.status, outcome(run)["data"]["code"]) == ("failed", "E_CLI_EXEC")


async def test_a_missing_install_or_secret_fails_the_run(env):
    await deploy(env, cli={"env": {"TOKEN": "secret:TOKEN"}}, secrets=["TOKEN"])
    run = await env.runner.cli_run(cli="tool", args=[])
    assert (run.status, outcome(run)["data"]["code"]) == ("failed", "E_SECRET_MISSING")
    shutil.rmtree(env.programs.get("tool").root / "cli")
    run = await env.runner.cli_run(cli="tool", args=[])
    assert (run.status, outcome(run)["data"]["code"]) == ("failed", "E_CLI_INSTALL")


async def test_a_cli_with_hosts_gets_the_proxy_in_its_env(env):
    await deploy(env, '#!/bin/sh\necho "$HTTPS_PROXY $NODE_USE_ENV_PROXY"\n', hosts=["api.example.com"])
    data = outcome(await env.runner.cli_run(cli="tool", args=[]))["data"]
    assert data["stdout"] == "http://127.0.0.1:3128 1\n"


async def test_a_python_cli_runs_its_console_script(env):
    wheel = console_wheel()
    listing = (
        b'{"urls": [{"packagetype": "bdist_wheel", "url": "https://files.pythonhosted.org/h.whl", '
        b'"digests": {"sha256": "' + sha(wheel).encode() + b'"}}]}'
    )
    fetch = FakeFetch({"https://pypi.org/pypi/hello/1.0/json": listing, "https://files.pythonhosted.org/h.whl": wheel})
    block = {"install": {"pypi": "hello==1.0", "sha256": sha(wheel)}, "command": "hello"}
    await install_cli(env.programs, cli_manifest(cli=block), fetch)
    data = outcome(await env.runner.cli_run(cli="tool", args=["there"]))["data"]
    assert (data["exit_code"], data["stdout"]) == (0, "hello there\n")
    assert sys.executable  # the worker runs the script with its own interpreter


async def test_program_call_on_a_cli_and_cli_run_on_a_program_are_refused(env):
    await deploy(env)
    with pytest.raises(BadRequest) as caught:
        await env.runner.call(program="tool", function="start", inputs={})
    assert caught.value.code == "E_NOT_PROGRAM" and "cli_run" in caught.value.message
    install(env.programs, manifest("sample"), "def start(ctx, inputs):\n    pass\n", sha="d" * 40)
    with pytest.raises(BadRequest) as refused:
        await env.runner.cli_run(cli="sample", args=[])
    assert refused.value.code == "E_NOT_CLI" and "program_call" in refused.value.message
    with pytest.raises(BadRequest) as bad:
        await env.runner.cli_run(cli="tool", args="not a list")
    assert bad.value.code == "E_CLI_INPUTS"


async def test_no_cli_runs_without_the_sandbox(env):
    await deploy(env)
    env.runner.layers = Layers({"bubblewrap": {"ok": False, "detail": "no"}})
    with pytest.raises(Unavailable):
        await env.runner.cli_run(cli="tool", args=[])


async def test_an_unreadable_vault_masks_nothing_in_the_audit(env):
    await deploy(env)

    class Broken:
        def values(self, *_: Any) -> Any:
            from rrotord.errors import RrotorError

            raise RrotorError("no key", code="E_VAULT_KEY")

    env.runner.vault = Broken()
    assert env.runner._hidden_now(env.programs.get("tool")) == []
    env.runner.vault = None
    assert env.runner._hidden_now(env.programs.get("tool")) == []


async def test_a_cli_whose_sandbox_does_not_start_fails_and_cleans_up(env):
    await deploy(env)
    env.launcher.fail = OSError("no bwrap")
    run = await env.runner.cli_run(cli="tool", args=[])
    assert (run.status, outcome(run)["data"]["code"]) == ("failed", "E_SANDBOX")
    assert not (env.records.folder(run.id) / "work").exists()
