"""What the API does, shared by its REST routes and its MCP tools: each takes the request's data as a dict, checks
it, and answers a JSON-ready dict. A refusal is a RrotorError with its stable code."""

from __future__ import annotations

import base64
import binascii
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rrotord import apps, clis
from rrotord.api import forward
from rrotord.audit import current
from rrotord.cluster.transport import ClusterError
from rrotord.errors import BadRequest, Forbidden, RrotorError, Unavailable
from rrotord.programs import Version
from rrotord.runner.record import Run
from rrotord.scheduler import Scheduler
from rrotord.vault import Vault

if TYPE_CHECKING:
    from rrotord.api.routes import Services

BUNDLE_MAX = 64 * 1024 * 1024


def answer(run: Run) -> dict[str, Any]:
    result = run.result or {}
    return {
        "run_id": run.id,
        "program": run.program,
        "version": run.version,
        "function": run.function,
        "status": run.status,
        "data": result.get("data"),
        "step": result.get("step"),
        "prompt": result.get("prompt"),
        "seconds": round(run.seconds, 3),
    }


async def deploy_bundle(s: Services, encoded: str, commit: str) -> Version:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise BadRequest("bundle is a git bundle in base64.", code="E_BUNDLE") from None
    if len(raw) > BUNDLE_MAX:
        raise BadRequest("The bundle is over 64 MB.", code="E_TOO_BIG")
    folder = s.config.run_dir / "bundles"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, suffix=".bundle", delete=False) as f:
        f.write(raw)
        path = Path(f.name)
    try:
        return await s.deployer.from_bundle(path, commit)
    finally:
        path.unlink(missing_ok=True)


async def deploy(s: Services, data: dict[str, Any]) -> dict[str, Any]:
    """A version from a git bundle of the commit (base64) or from a GitHub repo at the commit; `deployed` goes up
    the link either way (the repo only for a repo deploy). A follower sends it to its head."""
    if forward.follower(s):
        return await forward.relayed(s, "deploy", {"data": data})
    term = forward.leading(s)
    commit, bundle, repo, token = data.get("commit"), data.get("bundle"), data.get("repo"), data.get("token")
    if not isinstance(commit, str):
        raise BadRequest("Send {bundle, commit} (a git bundle, base64) or {repo, commit, token}.")
    try:
        if isinstance(bundle, str):
            version, source = await deploy_bundle(s, bundle, commit), None
        elif isinstance(repo, str) and (token is None or isinstance(token, str)):
            version, source = await s.deployer.from_repo(repo, commit, token), repo
        else:
            raise BadRequest("Send {bundle, commit} (a git bundle, base64) or {repo, commit, token}.")
    except RrotorError as err:
        if s.audit is not None:
            outcome = {"status": "failed", "code": err.code}
            s.audit.emit("program.deploy_failed", repo=repo, commit_sha=commit, outcome=outcome)
        raise
    if s.audit is not None:
        s.audit.emit(
            "program.deploy",
            program=version.program,
            version=version.version,
            commit_sha=version.commit_sha,
            repo=source,
            outcome={"status": "done", "code": None},
        )
    await forward.committed(s, term)
    if s.report is not None:
        await s.report(version, source)
    return version.to_json()


async def app_publish(s: Services, data: dict[str, Any]) -> dict[str, Any]:
    """An app published from Claude (docs/architecture.md, 5.8): its files and manifest made a version of
    `"kind": "app"`, not activated while an approved version is active (the catalogue activates the one it approves);
    `deployed` goes up the link with `by`, the Glyphh subject publishing. A person's act: on a public rrotor a
    runtime key is refused. A follower sends it to its head."""
    if forward.follower(s):
        return await forward.relayed(s, "app_publish", {"data": data})
    actor = current().actor
    if s.config.public and actor.get("type") != "sign-in":
        raise Forbidden("Publishing an app is a person's act: sign in to Glyphh for this runtime.", code="E_FORBIDDEN")
    name = apps.check_name(data.get("name"))
    files = apps.read_files(data.get("files"))
    manifest = apps.manifest_of(data, name)
    files["glyphh.json"] = json.dumps(manifest, indent=2).encode()
    program = data.get("program")
    if program is not None:
        if not isinstance(program, str):
            raise BadRequest("program is the text of program.py.", code="E_INPUTS")
        files["program.py"] = program.encode()
    if s.artifacts is None:
        raise Unavailable("This rrotor keeps no apps.", code="E_APPS")
    active = s.programs.active(name) if name in s.programs.names() else None
    keep = active is not None and active.manifest.app is not None and s.artifacts.approved(name, active.version)
    term = forward.leading(s)
    try:
        version, made = await s.deployer.from_files(files, activate=not keep)
    except RrotorError as err:
        if s.audit is not None:
            s.audit.emit("program.deploy_failed", program=name, outcome={"status": "failed", "code": err.code})
        raise
    if s.audit is not None:
        s.audit.emit(
            "program.deploy",
            program=version.program,
            version=version.version,
            commit_sha=version.commit_sha,
            repo=None,
            outcome={"status": "done", "code": None},
        )
    await forward.committed(s, term)
    if s.report is not None and made:
        await s.report(version, None, clis.subject_of(actor))
    now_active = s.programs.active(name)
    return {
        **version.to_json(),
        "active": now_active is not None and now_active.commit_sha == version.commit_sha,
        "state": s.artifacts.state(name, version.version),
        "url": f"{s.config.address}/apps/{name}/",
    }


async def apps_list(s: Services) -> dict[str, Any]:
    """The apps here: each one's title, version, roles, the catalogue's state and where a browser opens it."""
    if s.artifacts is None:
        raise Unavailable("This rrotor keeps no apps.", code="E_APPS")
    return {"apps": apps.Apps(s.programs, s.artifacts, s.config.address).listed()}


async def call(s: Services, data: dict[str, Any], caller: str = "claude", origin: str = "local") -> dict[str, Any]:
    """Open a run of a program's function; the first Result, with its data."""
    program, function = data.get("program"), data.get("function", "start")
    inputs, version, clearance = data.get("inputs") or {}, data.get("version"), data.get("clearance", 1.0)
    if (
        not isinstance(program, str)
        or not isinstance(function, str)
        or not isinstance(inputs, dict)
        or not (version is None or isinstance(version, int))
        or isinstance(clearance, bool)
        or not isinstance(clearance, int | float)
        or not 0 <= clearance <= 1
    ):
        raise BadRequest("Send {program, function, inputs, version?, clearance? (0 to 1)}.")
    c = s.cluster
    if (
        c is not None
        and c.role == "follower"
        and (forward.writes(s, program, function, version) or not await c.caught_up(forward.MIN_SEQ.get()))
    ):
        return await forward.relayed(s, "call", {"data": data, "caller": caller})
    term = forward.leading(s) if forward.writes(s, program, function, version) else None
    run = await s.runner.call(
        program=program,
        function=function,
        inputs=inputs,
        version=version,
        caller=caller,
        origin=origin,
        clearance=float(clearance),
    )
    await forward.committed(s, term)
    return answer(run)


async def clis_list(s: Services) -> dict[str, Any]:
    """The CLIs here: each one's version, command, install, allowed patterns and approval patterns, and whether it is
    signed in for this caller (their own HOME; unknown on a follower, whose homes are the head's)."""
    return {"clis": s.programs.clis(clis.subject_of(current().actor), homes=not forward.follower(s))}


async def cli_run(s: Services, data: dict[str, Any], caller: str = "claude") -> dict[str, Any]:
    """One command of a CLI, run in the sandbox: its Result (exit code, stdout, stderr, files) or `needs_approval`.
    A CLI runs on the cluster's head (its install and home are there): a follower sends the call to it."""
    cli = data.get("cli")
    if not isinstance(cli, str) or not cli:
        raise BadRequest("Send {cli, args, stdin?, files?}.")
    if forward.follower(s):
        return await forward.relayed(s, "cli", {"data": data, "caller": caller})
    term = forward.leading(s)
    run = await s.runner.cli_run(
        cli=cli, args=data.get("args", []), stdin=data.get("stdin"), files=data.get("files"), caller=caller
    )
    await forward.committed(s, term)
    return answer(run)


async def cli_login(s: Services, data: dict[str, Any], caller: str = "claude") -> dict[str, Any]:
    """A CLI's sign-in: `needs_input` with the address (and device code) to open on the person's phone, answered
    by program_continue with the code the page shows; on the cluster's head, as every CLI run is."""
    cli = data.get("cli")
    if not isinstance(cli, str) or not cli:
        raise BadRequest("Send {cli}.")
    if forward.follower(s):
        return await forward.relayed(s, "cli_login", {"data": data, "caller": caller})
    term = forward.leading(s)
    run = await s.runner.cli_login(cli=cli, caller=caller)
    await forward.committed(s, term)
    return answer(run)


async def resume(s: Services, run_id: str, value: Any) -> dict[str, Any]:
    far, node = forward.elsewhere(s, run_id)
    if far:
        return await forward.relayed(s, "resume", {"run_id": run_id, "answer": value}, node)
    term = forward.leading(s)
    run = await s.runner.resume(run_id, value)
    await forward.committed(s, term)
    return answer(run)


async def cancel(s: Services, run_id: str) -> dict[str, Any]:
    far, node = forward.elsewhere(s, run_id)
    if far:
        return await forward.relayed(s, "cancel", {"run_id": run_id}, node)
    return answer(await s.runner.cancel(run_id))


async def run_status(s: Services, run_id: str) -> dict[str, Any]:
    far, node = forward.elsewhere(s, run_id)
    if far:
        return await forward.relayed(s, "status", {"run_id": run_id}, node)
    return s.runner.get(run_id).view()


async def run_log(s: Services, run_id: str) -> dict[str, Any]:
    far, node = forward.elsewhere(s, run_id)
    if far:
        return await forward.relayed(s, "log", {"run_id": run_id}, node)
    run = s.runner.get(run_id)
    return {"run_id": run.id, "lines": s.runner.records.lines(run.id)}


async def approvals(s: Services) -> list[dict[str, Any]]:
    """The runs waiting for an approval: in a cluster, every node's (a node that does not answer is left out)."""
    here = s.runner.approvals()
    c = s.cluster
    if c is None or not c.member:
        return here
    if c.role == "follower":
        try:
            return list((await c.forward("approvals", {})).get("result", {}).get("approvals") or [])
        except RrotorError:
            return here
    found = list(here)
    for node in list(c.links):
        try:
            got = await c.forward("approvals_here", {}, node)
        except RrotorError:
            continue
        found += list((got.get("result") or {}).get("approvals") or [])
    return found


def on_head(s: Services, what: str) -> None:
    """What only the head keeps (the vault, the schedules' state) is changed there: a follower refuses, naming it."""
    if forward.follower(s):
        assert s.cluster is not None  # noqa: S101  (a follower is a member)
        head = s.cluster.state.get("head")
        raise ClusterError(
            f"{what} on the cluster's head ({head}), which keeps them; this node is a follower.",
            code="E_NOT_HEAD",
            head=head,
        )


def vault(s: Services) -> Vault:
    if s.runner.vault is None:
        raise Unavailable("This rrotor has no vault.", code="E_VAULT")
    return s.runner.vault


async def secrets_list(s: Services, program: str) -> dict[str, Any]:
    """A program's secrets by name: whether its active version declares each, and whether it is set here. Never a
    value. A follower asks its head, which keeps them."""
    if forward.follower(s):
        return await forward.relayed(s, "secrets", {"program": program})
    declared = set(s.programs.get(program).manifest.secrets)
    held = set(vault(s).names(program))
    listed = [{"name": n, "declared": n in declared, "set": n in held} for n in sorted(declared | held)]
    return {"program": program, "secrets": listed}


async def secret_set(s: Services, program: str, name: str, value: Any) -> dict[str, Any]:
    """A secret's value kept, encrypted, for a program here; an audit event naming it (never the value)."""
    if not isinstance(value, str):
        raise BadRequest("Send {value}: the secret's value, a string.")
    on_head(s, "Secrets are set")
    s.programs.get(program)
    vault(s).set(program, name, value)
    if s.audit is not None:
        s.audit.emit("secret.set", program=program, secret=name, outcome={"status": "done", "code": None})
    return {"program": program, "name": name, "set": True}


async def secret_delete(s: Services, program: str, name: str) -> dict[str, Any]:
    on_head(s, "Secrets are deleted")
    s.programs.get(program)
    vault(s).delete(program, name)
    if s.audit is not None:
        s.audit.emit("secret.delete", program=program, secret=name, outcome={"status": "done", "code": None})
    return {"program": program, "name": name, "set": False}


def scheduler(s: Services) -> Scheduler:
    if s.scheduler is None:
        raise Unavailable("This rrotor has no scheduler.", code="E_SCHEDULER")
    return s.scheduler


async def schedules(s: Services) -> dict[str, Any]:
    """Every program's schedules: the next fire, the last run, the last skip, paused or not. A follower asks its
    head, which fires them."""
    if forward.follower(s):
        return await forward.relayed(s, "schedules", {})
    return {"schedules": scheduler(s).view()}


async def schedule_pause(s: Services, program: str, name: str, paused: bool) -> dict[str, Any]:
    on_head(s, "Schedules are paused and resumed")
    return scheduler(s).pause(program, name, paused)


def text_of(data: dict[str, Any], key: str) -> str:
    found = data.get(key)
    if not isinstance(found, str) or not found:
        raise BadRequest(f"Send {{{key}}}.")
    return found


def run_id_of(data: dict[str, Any]) -> str:
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise BadRequest("Send {run_id}.")
    return run_id
