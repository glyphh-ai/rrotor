"""A CLI and a SQLite database in the real sandbox: the image's `sh` deployed as a CLI runs in its working folder with
its private HOME, reaches nothing else of /data, cannot execute what it writes; a sign-in that listens on loopback
takes the pasted redirect inside the sandbox, one that does not may not bind; ctx.db opens in the program's data
folder.

Linux only, inside the image: `pytest -m sandbox -o addopts='' tests/sandbox`."""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator
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
from tests.fakes import GitRepo, cli_manifest, manifest, token

pytestmark = pytest.mark.sandbox

COUNTS = """
from rrotor import Result

def start(ctx, inputs):
    with ctx.db:
        ctx.db.execute("create table if not exists seen (n integer)")
        ctx.db.execute("insert into seen values (1)")
    return Result.done(ctx.db.execute("select count(*) from seen").fetchone()[0])
"""

PROBE = r"""
try_() { if "$@" >/dev/null 2>&1; then echo open; else echo blocked; fi; }
echo "home=$HOME"
echo "read_runtime_json=$(try_ cat "$1")"
echo "write_cli=$(try_ touch /cli/owned)"
echo "write_program=$(try_ touch /program/owned)"
printf '#!/bin/sh\necho ran\n' > /work/x.sh && chmod +x /work/x.sh
echo "exec_work=$(try_ /work/x.sh)"
echo "exec_tmp=$(cp /work/x.sh /tmp/x.sh && try_ /tmp/x.sh)"
echo kept >> "$HOME/count"
echo "calls=$(wc -l < "$HOME/count")"
echo made > /work/out.txt
"""


async def deploy(deployer: Deployer, name: str, files: dict[str, str]) -> None:
    repo = GitRepo(Path(tempfile.mkdtemp(prefix="rr-repo-")) / name)
    commit = repo.commit(files)
    await deployer.from_bundle(repo.bundle(repo.path.parent / f"{name}.bundle"), commit)


@pytest.fixture
async def runner() -> AsyncIterator[tuple[Path, Runner]]:
    layers = await probe()
    assert layers.ready, layers.why()
    root = Path(tempfile.mkdtemp(prefix="sandbox-cli-", dir="/data"))
    (root / "runtime.json").write_text(f'{{"token": "{token()}"}}')
    programs = Programs(root)
    deployer = Deployer(programs, Path(tempfile.mkdtemp(prefix="rr-deploy-")))
    sh = cli_manifest("shell", cli={"install": {"image": "sh"}, "command": "sh", "home": True})
    await deploy(deployer, "shell", {"glyphh.json": json.dumps(sh)})
    db = manifest("counts", databases=["sqlite"])
    await deploy(deployer, "counts", {"glyphh.json": json.dumps(db), "program.py": COUNTS})
    made = Runner(
        programs=programs,
        records=Records(root),
        proxy=EgressProxy(Path(tempfile.mkdtemp(prefix="rr-proxy-", dir="/tmp"))),
        launcher=SandboxLauncher(),
        sdk=Path(rrotor.__file__).parent,
        layers=layers,
    )
    yield root, made
    await made.shutdown()


async def shell(runner: Runner, root: Path) -> dict[str, Any]:
    run = await runner.cli_run(cli="shell", args=["-c", PROBE, "probe", str(root / "runtime.json")])
    assert run.status == "done", run.result
    data: dict[str, Any] = run.result["data"]  # type: ignore[index]
    return data


async def test_a_cli_runs_in_its_folders_and_reaches_nothing_else(runner):
    root, made = runner
    data = await shell(made, root)
    found = dict(line.split("=", 1) for line in data["stdout"].splitlines())
    assert data["exit_code"] == 0, data["stderr"]
    assert found["home"] == "/home/cli" and found["calls"].strip() == "1"
    assert found["read_runtime_json"] == "blocked"
    assert (found["write_cli"], found["write_program"]) == ("blocked", "blocked")
    assert (found["exec_work"], found["exec_tmp"]) == ("blocked", "blocked")
    assert data["files"]["out.txt"] == "made\n" and "x.sh" in data["files"]
    again = dict(line.split("=", 1) for line in (await shell(made, root))["stdout"].splitlines())
    assert again["calls"].strip() == "2"  # its HOME kept across calls


LISTENER = r"""
import http.server, os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        open(os.environ["HOME"] + "/creds", "w").write(self.path)
        self.send_response(200)
        self.end_headers()
    def log_message(self, *a):
        pass
s = http.server.HTTPServer(("127.0.0.1", 0), H)
print(f"visit https://auth.example.com/authorize?redirect_uri=http://127.0.0.1:{s.server_port}/cb", flush=True)
s.handle_request()
print("signed in", flush=True)
"""


async def deploy_listener(deployer: Deployer, name: str, listen: bool) -> None:
    login = {"args": ["-c", LISTENER], "listen": listen, "signed_in": "creds"}
    cli = {"install": {"image": "python3"}, "command": "python3", "home": True, "login": login}
    await deploy(deployer, name, {"glyphh.json": json.dumps(cli_manifest(name, cli=cli))})


async def test_a_sign_in_that_listens_takes_the_pasted_redirect_inside_the_sandbox(runner):
    root, made = runner
    deployer = Deployer(made.programs, Path(tempfile.mkdtemp(prefix="rr-deploy-")))
    await deploy_listener(deployer, "listens", True)
    asked = await made.cli_login(cli="listens")
    assert asked.status == "needs_input", asked.result
    port = asked.result["data"]["url"].rsplit(":", 1)[1].split("/")[0]
    done = await made.resume(asked.id, f"http://localhost:{port}/cb?code=abc&state=s")
    assert (done.status, done.result["data"]["signed_in"]) == ("done", True), done.result
    assert (Programs(root).folder("listens") / "home" / "creds").read_text() == "/cb?code=abc&state=s"
    await deploy_listener(deployer, "held", False)  # not declared to listen: Landlock refuses the bind
    refused = await made.cli_login(cli="held")
    data = refused.result["data"]
    assert refused.status == "done" and data["exit_code"] != 0 and "PermissionError" in data["output"], data


USES_A_CLI = """
from rrotor import Result

def start(ctx, inputs):
    import glyphh
    got = ctx.cli("shell", ["-c", "echo from $HOME; touch /work/x && echo wrote"])
    return Result.done({"out": got.stdout, "exit": got.exit_code, "glyphh": glyphh.__version__})
"""


async def test_a_program_runs_a_cli_in_the_clis_own_sandbox(runner):
    _, made = runner
    deployer = Deployer(made.programs, Path(tempfile.mkdtemp(prefix="rr-deploy-")))
    data = manifest("uses", clis=["shell"])
    await deploy(deployer, "uses", {"glyphh.json": json.dumps(data), "program.py": USES_A_CLI})
    run = await made.call(program="uses", function="start", inputs={})
    assert run.status == "done", run.result
    result = run.result["data"]
    assert (result["out"], result["exit"]) == ("from /home/cli\nwrote\n", 0)  # the CLI's HOME and folder
    pinned = json.loads(Path("/usr/local/lib/rrotor-clis/glyphh/vendor.json").read_text())["version"]
    assert result["glyphh"] == pinned  # the image's Glyphh SDK, with no requirements.lock


async def test_ctx_db_opens_in_the_programs_data_folder(runner):
    root, made = runner
    first = await made.call(program="counts", function="start", inputs={})
    second = await made.call(program="counts", function="start", inputs={})
    assert (first.status, first.result["data"], second.result["data"]) == ("done", 1, 2), first.result
    assert (Programs(root).data("counts") / "db.sqlite3").is_file()
