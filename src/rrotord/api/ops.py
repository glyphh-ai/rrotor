"""What the API does, shared by its REST routes and its MCP tools: each takes the request's data as a dict, checks
it, and answers a JSON-ready dict. A refusal is a RrotorError with its stable code."""

from __future__ import annotations

import base64
import binascii
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rrotord.api import forward
from rrotord.errors import BadRequest, RrotorError
from rrotord.programs import Version
from rrotord.runner.record import Run

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


async def call(s: Services, data: dict[str, Any], caller: str = "claude") -> dict[str, Any]:
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
        program=program, function=function, inputs=inputs, version=version, caller=caller, clearance=float(clearance)
    )
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


def run_id_of(data: dict[str, Any]) -> str:
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise BadRequest("Send {run_id}.")
    return run_id
