"""The machine's folders a run is given, from inside the real sandbox (rrotord/folders.py): a folder given to read is
read and cannot be changed, one given to change can, a folder inside one keeps its own say, the folder beside them is
not there at all, and a call that asks for a folder the rrotor's owner does not offer never becomes a run.

Linux only, inside the image, with /data writable: `pytest -m sandbox -o addopts='' tests/sandbox`."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import rrotor
from rrotord.deploy import Deployer
from rrotord.errors import Forbidden
from rrotord.folders import Folders
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.launch import SandboxLauncher
from rrotord.runner.record import Records
from rrotord.sandbox.probe import probe
from tests.fakes import GitRepo, manifest

pytestmark = pytest.mark.sandbox

READER = """
import os

from rrotor import Result


def _try(fn):
    try:
        return fn()
    except OSError as err:
        return "blocked: " + type(err).__name__


def _read(path):
    with open(path) as f:
        return f.read()


def _write(path):
    with open(path, "w") as f:
        f.write("changed")
    return "written"


def start(ctx, inputs):
    return Result.done({
        "given": list(ctx.folders),
        **{name: _try(lambda p=path: _read(p)) for name, path in inputs.get("read", {}).items()},
        **{name: _try(lambda p=path: _write(p)) for name, path in inputs.get("write", {}).items()},
        **{name: _try(lambda p=path: sorted(os.listdir(p))) for name, path in inputs.get("list", {}).items()},
    })
"""


@dataclass
class Box:
    shared: Path  # the one folder the rrotor's owner offers
    beside: Path  # a folder of the machine beside it, never offered
    runner: Runner


@pytest.fixture
async def box() -> AsyncIterator[Box]:
    layers = await probe()
    assert layers.ready, layers.why()
    root = Path(tempfile.mkdtemp(prefix="sandbox-folders-", dir="/data"))
    home = Path(tempfile.mkdtemp(prefix="machine-", dir=Path.home()))  # the machine's own files: not rrotor's /data
    shared, beside = home / "shared", home / "private"
    for folder in (shared / "docs", shared / "out", shared / "out" / "kept", shared / "other", beside):
        folder.mkdir(parents=True)
        (folder / "note.txt").write_text(f"{folder.name} as it was")
    programs = Programs(root)
    repo = GitRepo(Path(tempfile.mkdtemp(prefix="rr-repo-")) / "reader")
    sha = repo.program(manifest("reader", folders=True), READER)
    await Deployer(programs, Path(tempfile.mkdtemp(prefix="rr-deploy-"))).from_bundle(
        repo.bundle(repo.path.parent / "reader.bundle"), sha
    )
    runner = Runner(
        programs=programs,
        records=Records(root),
        proxy=EgressProxy(Path(tempfile.mkdtemp(prefix="rr-proxy-", dir="/tmp"))),
        launcher=SandboxLauncher(),
        sdk=Path(rrotor.__file__).parent,
        layers=layers,
        folders=Folders((shared,)),
    )
    yield Box(shared, beside, runner)
    await runner.shutdown()


async def ran(box: Box, folders: list[dict[str, str]], **inputs: Any) -> dict[str, Any]:
    run = await box.runner.call(program="reader", function="start", inputs=inputs, folders=folders)
    assert run.status == "done", run.result
    data: dict[str, Any] = run.result["data"]  # type: ignore[index]
    return data


async def test_a_folder_given_to_read_is_read_and_cannot_be_changed(box):
    docs = box.shared / "docs"
    found = await ran(
        box,
        [{"path": str(docs), "access": "read"}],
        read={"read": str(docs / "note.txt")},
        write={"change": str(docs / "note.txt"), "add": str(docs / "new.txt")},
    )
    assert found["given"] == [{"path": str(docs), "access": "read"}]
    assert found["read"] == "docs as it was"
    assert found["change"].startswith("blocked") and found["add"].startswith("blocked")
    assert (docs / "note.txt").read_text() == "docs as it was" and not (docs / "new.txt").exists()


async def test_a_folder_given_to_change_is_changed_on_the_machine(box):
    out = box.shared / "out"
    found = await ran(
        box,
        [{"path": str(out), "access": "write"}],
        read={"read": str(out / "note.txt")},
        write={"add": str(out / "new.txt")},
    )
    assert (found["read"], found["add"]) == ("out as it was", "written")
    assert (out / "new.txt").read_text() == "changed"


async def test_a_folder_inside_one_given_to_change_keeps_its_own_say(box):
    out = box.shared / "out"
    given = [{"path": str(out), "access": "write"}, {"path": str(out / "kept"), "access": "read"}]
    found = await ran(box, given, write={"outer": str(out / "new.txt"), "inner": str(out / "kept" / "note.txt")})
    assert found["outer"] == "written" and found["inner"].startswith("blocked")
    assert (out / "kept" / "note.txt").read_text() == "kept as it was"


async def test_the_folders_beside_the_ones_given_are_not_there(box):
    docs = box.shared / "docs"
    found = await ran(
        box,
        [{"path": str(docs), "access": "read"}],
        read={"sibling": str(box.shared / "other" / "note.txt"), "beside": str(box.beside / "note.txt")},
        write={"sibling_write": str(box.shared / "other" / "new.txt")},
        list={"parent": str(box.shared), "home": str(box.shared.parent), "sibling_list": str(box.shared / "other")},
    )
    for name in ("sibling", "beside", "sibling_write", "sibling_list"):
        assert found[name].startswith("blocked"), (name, found[name])
    # the way down to the folder is bare: nothing of the folders beside it, not even their names
    assert found["parent"] in (["docs"], "blocked: PermissionError")
    assert found["home"] in (["shared"], "blocked: PermissionError")
    assert not (box.shared / "other" / "new.txt").exists()


async def test_a_run_given_no_folders_sees_none_of_the_machine(box):
    docs = box.shared / "docs"
    found = await ran(box, [], read={"offered": str(docs / "note.txt")}, list={"shared": str(box.shared)})
    assert found["given"] == [] and found["offered"].startswith("blocked") and found["shared"].startswith("blocked")


async def test_a_folder_the_owner_does_not_offer_is_refused_before_anything_runs(box):
    for path in (str(box.beside), str(box.shared / "docs" / ".." / ".." / "private"), "/etc"):
        with pytest.raises(Forbidden) as refused:
            await box.runner.call(
                program="reader", function="start", inputs={}, folders=[{"path": path, "access": "read"}]
            )
        assert refused.value.code == "E_FOLDER_REFUSED" and str(box.shared) in refused.value.message
    assert box.runner.records.all() == []
