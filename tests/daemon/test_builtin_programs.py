"""Glyphh's own programs the image carries (programs/: glyphh_bot and glyphh_swarm, which the server calls over the
link), deployed at boot as built-ins after the CLIs they run, never over one the person deployed."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
from pathlib import Path

import pytest

import rrotor
from rrotord import manifest as m
from rrotord.config import Config
from rrotord.deploy import builtin_files
from rrotord.errors import DeployError
from tests.daemon.test_builtin_clis import builtin_folder, daemon, deployer
from tests.fakes import install, manifest

PROGRAMS = Path(__file__).resolve().parents[2] / "programs"


@pytest.mark.parametrize("name", ["glyphh_bot", "glyphh_swarm"])
def test_each_builtin_program_has_a_manifest_rrotor_takes_and_a_vendor(name):
    found = m.read(PROGRAMS / name)
    assert found.name == name and found.kind == "program"
    vendor = json.loads((PROGRAMS / name / "vendor.json").read_text())
    assert vendor["vendor"] == "Glyphh" and vendor["version"]
    assert [rel for rel, _ in builtin_files(PROGRAMS / name)] == sorted(
        str(p.relative_to(PROGRAMS / name)) for p in (PROGRAMS / name).glob("*.py")
    )


def test_glyphh_bot_reads_the_orgs_key_only_when_set_and_runs_claude():
    found = m.read(PROGRAMS / "glyphh_bot")
    assert found.optional() == {"ANTHROPIC_API_KEY"} and found.clis == ["claude"]
    assert set(found.functions) == {"run"} and found.functions["run"].writes
    swarm = m.read(PROGRAMS / "glyphh_swarm")
    assert set(swarm.functions) == {"turn", "pick", "merge", "reject"} and swarm.data and swarm.optional() == set()
    assert all(f.writes for f in swarm.functions.values())


def test_a_builtin_programs_files_leave_out_its_tests_caches_and_dotfiles(tmp_path):
    folder = tmp_path / "p"
    for rel in (
        "glyphh.json",
        "vendor.json",
        "program.py",
        "lib/x.py",
        "files/seed.txt",
        "tests/test_p.py",
        "__pycache__/program.pyc",
        "lib/__pycache__/x.pyc",
        ".hidden",
    ):
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(rel)
    assert [rel for rel, _ in builtin_files(folder)] == ["files/seed.txt", "lib/x.py", "program.py"]


async def test_a_builtin_program_is_deployed_once_and_again_when_its_code_changes(tmp_path):
    d = deployer(tmp_path)
    image = tmp_path / "image"
    shutil.copytree(PROGRAMS / "glyphh_swarm", image / "glyphh_swarm")
    first = await d.builtin(image / "glyphh_swarm")
    assert first is not None and first.builtin == {"vendor": "Glyphh", "title": "Glyphh project", "version": "2"}
    assert (first.src / "program.py").is_file() and (first.src / "swarm_turns.py").is_file()
    assert (first.src / "terminal.py").is_file()  # the clone's command log, imported by program.py
    assert not (first.src / "tests").exists() and (first.root / "venv").is_dir()
    assert d.programs.deployed() == [] and d.programs.active("glyphh_swarm") == first
    assert await d.builtin(image / "glyphh_swarm") is None  # the same files: nothing to do
    turns = image / "glyphh_swarm" / "swarm_turns.py"
    turns.write_text(turns.read_text() + "\n# changed\n")
    newer = await d.builtin(image / "glyphh_swarm")
    assert newer is not None and newer.version == 2 and newer.commit_sha != first.commit_sha


async def test_glyphh_bot_waits_for_the_claude_cli_and_one_the_person_deployed_is_theirs(tmp_path):
    d = deployer(tmp_path)
    with pytest.raises(DeployError, match="names the CLI claude, not on this rrotor"):
        await d.builtin(PROGRAMS / "glyphh_bot")
    assert d.programs.names() == []
    await d.builtin(builtin_folder(tmp_path / "clis", "claude"))
    made = await d.builtin(PROGRAMS / "glyphh_bot")
    assert made is not None and (made.src / "claude_api.py").is_file()
    install(d.programs, manifest("glyphh_swarm"), "def start(ctx, inputs):\n    pass\n", sha="e" * 40)
    assert await d.builtin(PROGRAMS / "glyphh_swarm") is None
    assert d.programs.active("glyphh_swarm").builtin is None  # type: ignore[union-attr]


async def test_the_daemon_deploys_the_image_programs_at_boot_after_the_clis(tmp_path, short_dir, caplog):
    clis = tmp_path / "clis"
    builtin_folder(clis, "claude")
    c = Config(
        tmp_path / "data",
        "local",
        "http://127.0.0.1:1",
        0,
        0,
        None,
        Path(rrotor.__file__).parent,
        short_dir,
        builtin_clis=clis,
        builtin_programs=PROGRAMS,
    )
    alone = daemon(dataclasses.replace(c, data=tmp_path / "alone", builtin_clis=tmp_path / "nowhere"))
    with caplog.at_level("WARNING", logger="rrotor"):
        assert await alone.install_builtin_programs() == ["glyphh_swarm v1"]
    assert "a built-in program was not deployed" in caplog.text  # glyphh_bot: no claude CLI there
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level("INFO", logger="rrotor"):
        await daemon(c).serve(stop)
    assert "built-in program deployed" in caplog.text
    assert await daemon(dataclasses.replace(c, builtin_programs=tmp_path / "none")).install_builtin_programs() == []
