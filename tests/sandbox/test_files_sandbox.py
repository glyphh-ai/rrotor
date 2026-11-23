"""rrotor.files from inside the real sandbox, as a bot's file tools use it (programs/glyphh_bot/file_agent.py): a file
in a folder given to read is read; a write there is refused; a file is written in a folder given to change; a path
outside the folders and a link out of them are refused; and a change the program asks the person about waits for
their yes, and is not made on a no. The sandbox mounts only the folders given, so the refusals here are rrotor.files'
own sentences, with the sandbox behind them.

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
from rrotord.folders import Folders
from rrotord.programs import Programs
from rrotord.proxy import EgressProxy
from rrotord.runner.engine import Runner
from rrotord.runner.launch import SandboxLauncher
from rrotord.runner.record import Records
from rrotord.sandbox.probe import probe
from tests.fakes import GitRepo, manifest

pytestmark = pytest.mark.sandbox

FILER = """
from rrotor import Result
from rrotor.files import FileRefused, Files


def start(ctx, inputs):
    def before(change):
        asks = f"Write {change.name}?"
        if inputs.get("ask") and ctx.needs_input(asks, options=["yes", "no"], key=change.path) != "yes":
            raise FileRefused(f"The person said no to {change.name}.")

    files = Files(ctx.folders, before=before)
    out = {}
    for name, (tool, *args) in inputs["do"].items():
        try:
            got = getattr(files, tool)(*args)
            out[name] = got.text if tool == "read" else got
        except FileRefused as err:
            out[name] = "refused: " + err.message
    return Result.done({"did": out, "touched": files.touched})
"""


@dataclass
class Box:
    docs: Path  # given to read
    out: Path  # given to change
    beside: Path  # of the machine, never given
    runner: Runner

    @property
    def folders(self) -> list[dict[str, str]]:
        return [{"path": str(self.docs), "access": "read"}, {"path": str(self.out), "access": "write"}]


@pytest.fixture
async def box() -> AsyncIterator[Box]:
    layers = await probe()
    assert layers.ready, layers.why()
    root = Path(tempfile.mkdtemp(prefix="sandbox-files-", dir="/data"))
    home = Path(tempfile.mkdtemp(prefix="machine-", dir=Path.home()))
    shared, beside = home / "shared", home / "private"
    docs, out = shared / "docs", shared / "out"
    for folder in (docs, out, beside):
        folder.mkdir(parents=True)
        (folder / "note.txt").write_text(f"{folder.name} as it was")
    (docs / "way-out").symlink_to(beside)
    (out / "way-out").symlink_to(beside)
    programs = Programs(root)
    repo = GitRepo(Path(tempfile.mkdtemp(prefix="rr-repo-")) / "filer")
    sha = repo.program(manifest("filer", folders=True), FILER)
    await Deployer(programs, Path(tempfile.mkdtemp(prefix="rr-deploy-"))).from_bundle(
        repo.bundle(repo.path.parent / "filer.bundle"), sha
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
    yield Box(docs, out, beside, runner)
    await runner.shutdown()


async def did(box: Box, **do: list[Any]) -> dict[str, Any]:
    run = await box.runner.call(program="filer", function="start", inputs={"do": do}, folders=box.folders)
    assert run.status == "done", run.result
    data: dict[str, Any] = run.result["data"]  # type: ignore[index]
    return data


async def test_a_file_in_a_folder_given_to_read_is_read(box):
    found = await did(box, read=["read", str(box.docs / "note.txt")], listed=["list", str(box.docs)])
    assert found["did"]["read"] == "docs as it was"
    assert [e["name"] for e in found["did"]["listed"]["entries"]] == ["note.txt"]  # the link is left out
    assert found["touched"] == [{"name": "docs/note.txt", "did": "read"}]


async def test_a_write_in_a_folder_given_to_read_is_refused(box):
    found = await did(
        box,
        change=["write", str(box.docs / "note.txt"), "changed"],
        add=["write", str(box.docs / "new.txt"), "new"],
        move=["move", str(box.docs / "note.txt"), str(box.out / "taken.txt")],
    )
    for name in ("change", "add", "move"):
        assert found["did"][name].startswith("refused: ") and "read only" in found["did"][name], found
    assert (box.docs / "note.txt").read_text() == "docs as it was" and not (box.docs / "new.txt").exists()
    assert not (box.out / "taken.txt").exists() and found["touched"] == []


async def test_a_file_is_written_and_moved_in_a_folder_given_to_change(box):
    found = await did(
        box,
        add=["write", str(box.out / "2025" / "Summary.md"), "# Summary"],
        move=["move", str(box.out / "note.txt"), str(box.out / "done" / "note.txt")],
    )
    assert found["did"]["add"] == {"name": "out/2025/Summary.md", "bytes": 9, "created": True}
    assert (box.out / "2025" / "Summary.md").read_text() == "# Summary"
    assert (box.out / "done" / "note.txt").read_text() == "out as it was" and not (box.out / "note.txt").exists()
    assert found["touched"] == [
        {"name": "out/2025/Summary.md", "did": "written"},
        {"name": "out/done/note.txt", "did": "moved"},
    ]


async def test_a_path_outside_the_folders_is_refused(box):
    found = await did(
        box,
        read=["read", str(box.beside / "note.txt")],
        dots=["read", str(box.docs / ".." / ".." / "private" / "note.txt")],
        write=["write", str(box.beside / "new.txt"), "x"],
        etc=["read", "/etc/passwd"],
        listed=["list", str(box.beside)],
    )
    for name in ("read", "write", "etc", "listed"):
        assert "is not in a folder this bot was given" in found["did"][name], found
    assert "'..'" in found["did"]["dots"]
    assert not (box.beside / "new.txt").exists() and (box.beside / "note.txt").read_text() == "private as it was"


async def test_a_link_out_of_the_folders_is_no_way_out(box):
    found = await did(
        box,
        read=["read", str(box.docs / "way-out" / "note.txt")],
        write=["write", str(box.out / "way-out" / "new.txt"), "x"],
        over=["write", str(box.out / "way-out" / "note.txt"), "x"],
    )
    for name in ("read", "write", "over"):
        assert found["did"][name].startswith("refused: ") and "is a link" in found["did"][name], found
    assert not (box.beside / "new.txt").exists() and (box.beside / "note.txt").read_text() == "private as it was"


async def test_a_change_asked_about_waits_for_the_persons_yes(box):
    target = box.out / "asked.txt"
    inputs = {"ask": True, "do": {"add": ["write", str(target), "asked"]}}
    asked = await box.runner.call(program="filer", function="start", inputs=inputs, folders=box.folders)
    assert asked.status == "needs_input", asked.result
    assert asked.result["prompt"] == "Write out/asked.txt?"
    assert not target.exists()  # nothing is written while it waits
    done = await box.runner.resume(asked.id, "yes")
    assert done.status == "done", done.result
    assert target.read_text() == "asked"

    refused = await box.runner.call(
        program="filer",
        function="start",
        inputs={"ask": True, "do": {"add": ["write", str(box.out / "never.txt"), "x"]}},
        folders=box.folders,
    )
    assert refused.status == "needs_input" and not (box.out / "never.txt").exists()
    said_no = await box.runner.resume(refused.id, "no")
    assert said_no.status == "done", said_no.result
    assert said_no.result["data"]["did"]["add"] == "refused: The person said no to out/never.txt."
    assert not (box.out / "never.txt").exists()
