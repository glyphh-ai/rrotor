"""The work on the machine (rrotord/work.py): the crew's clones as workspaces, their folders, files, diffs and command
log, read where they are; one file saved, never while a turn holds the clone, never outside it or in `.git`."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from rrotord import work as work_module
from rrotord.errors import BadRequest, NotFound
from rrotord.work import Busy, TooBig, Work, as_text, inside
from tests.fakes import git

WS = "acme__crew"
BRANCH = "turn-1-ada-k3x9"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


@dataclass
class Crew:
    work: Work
    clone: Path
    data: Path


@pytest.fixture
def crew(tmp_path: Path) -> Crew:
    """acme/crew cloned as glyphh_swarm keeps it: main as GitHub has it, and a turn's branch that changed a file, added
    two (one an image), and deleted one; the clone back on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    data = tmp_path / "data"
    clone = data / "repos" / WS
    clone.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "src").mkdir()
    (clone / "README.md").write_text("# crew\n")
    (clone / "src" / "app.py").write_text("print('one')\n")
    (clone / "old.txt").write_text("going\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "Start")
    git(clone, "push", "-q", "origin", "HEAD:main")
    git(clone, "remote", "set-head", "origin", "main")
    git(clone, "checkout", "-q", "-b", BRANCH)
    (clone / "src" / "app.py").write_text("print('one')\nprint('two')\n")
    (clone / "notes.md").write_text("new\n")
    (clone / "logo.png").write_bytes(PNG)
    (clone / "old.txt").unlink()
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "Ada: wrote two")
    git(clone, "checkout", "-q", "main")
    return Crew(Work(data), clone, data)


def test_the_workspaces_are_the_clones_with_their_branches(crew, tmp_path):
    assert Work(tmp_path / "nothing").workspaces() == {"workspaces": []}
    (crew.data / "repos" / "acme__crew.lock").write_text("")
    (crew.data / "repos" / "not-a-clone").mkdir()
    (crew.data / "repos" / "acme__empty").mkdir()  # no .git: not a clone yet
    (got,) = crew.work.workspaces()["workspaces"]
    assert sorted(got.pop("branches")) == ["main", BRANCH]  # newest first; the two were made in the same second
    assert got == {"id": WS, "name": "acme/crew", "kind": "repo", "branch": "main"}
    git(crew.clone, "checkout", "-q", "--detach")
    assert crew.work.workspaces()["workspaces"][0]["branch"] is None


def test_a_workspace_that_is_not_there_is_not_found(crew):
    for ws in ("acme__other", "..", "acme", "acme__crew/..", ""):
        with pytest.raises(NotFound):
            crew.work.tree(ws, "")


def test_a_folders_entries_come_folders_first_and_never_git(crew, monkeypatch):
    os.symlink("/etc", crew.clone / "link")
    got = crew.work.tree(WS, "")
    assert got["under"] == "" and got["truncated"] is False
    assert [(e["name"], e["kind"]) for e in got["entries"]] == [
        ("src", "dir"),
        ("link", "file"),
        ("old.txt", "file"),
        ("README.md", "file"),
    ]
    sizes = {e["name"]: e["size"] for e in got["entries"]}
    assert sizes == {"src": None, "link": None, "old.txt": 6, "README.md": 7}
    inner = crew.work.tree(WS, "src/")
    assert inner["under"] == "src" and inner["entries"] == [
        {"name": "app.py", "path": "src/app.py", "kind": "file", "size": 13}
    ]
    monkeypatch.setattr(work_module, "ENTRIES_MAX", 2)
    assert len((cut := crew.work.tree(WS, ""))["entries"]) == 2 and cut["truncated"] is True
    with pytest.raises(NotFound):
        crew.work.tree(WS, "README.md")
    with pytest.raises(NotFound):
        crew.work.tree(WS, "nowhere")


@pytest.mark.parametrize(
    "rel", ["/etc/passwd", "../x", "src/../../x", ".git/config", "src/.GIT/x", "a\x00b", "link/hosts"]
)
def test_a_path_that_leaves_the_workspace_or_enters_git_is_refused(crew, rel):
    os.symlink("/etc", crew.clone / "link")
    with pytest.raises(BadRequest) as refused:
        crew.work.file(WS, rel)
    assert refused.value.code == "E_PATH"
    with pytest.raises(BadRequest):
        crew.work.write(WS, rel, "x")


def test_a_symlink_into_git_and_an_empty_path_are_refused(crew):
    os.symlink(".git", crew.clone / "inner")
    with pytest.raises(BadRequest):
        inside(crew.clone, "inner/config")
    with pytest.raises(BadRequest):
        inside(crew.clone, "")
    assert inside(crew.clone, "", root_ok=True) == crew.clone.resolve()


def test_a_file_is_read_from_the_working_tree_or_as_a_branch_has_it(crew):
    assert crew.work.file(WS, "src/app.py") == {
        "path": "src/app.py",
        "size": 13,
        "binary": False,
        "text": "print('one')\n",
        "truncated": False,
    }
    assert crew.work.file(WS, "src/app.py", BRANCH)["text"] == "print('one')\nprint('two')\n"
    image = crew.work.file(WS, "logo.png", BRANCH)
    assert image["binary"] is True and image["text"] is None and image["size"] == len(PNG)
    assert crew.work.raw(WS, "logo.png", BRANCH) == (PNG, "image/png")
    assert crew.work.raw(WS, "old.txt") == (b"going\n", "text/plain")
    (crew.clone / "blob").write_bytes(b"\xff\xfe")
    assert crew.work.raw(WS, "blob") == (b"\xff\xfe", "application/octet-stream")
    assert crew.work.file(WS, "blob")["binary"] is True
    for rel, ref in (("nope.txt", None), ("src", None), ("notes.md", None), ("nope.txt", BRANCH), ("x", "no-branch")):
        with pytest.raises(NotFound):
            crew.work.file(WS, rel, ref)
    with pytest.raises(BadRequest) as refused:
        crew.work.file(WS, "README.md", "-x")
    assert refused.value.code == "E_BRANCH"
    with pytest.raises(BadRequest):
        crew.work.file(WS, "README.md", "x.lock")


def test_a_long_file_is_cut_and_a_huge_one_is_not_read(crew, monkeypatch):
    monkeypatch.setattr(work_module, "TEXT_MAX", 8)
    cut = crew.work.file(WS, "src/app.py")
    assert cut["text"] == "print('o" and cut["truncated"] is True and cut["size"] == 13
    monkeypatch.setattr(work_module, "RAW_MAX", 8)
    for ref in (None, BRANCH):
        with pytest.raises(TooBig):
            crew.work.raw(WS, "src/app.py", ref)
    assert crew.work.read(WS, "src/app.py", None, 8) == (None, 13)
    assert as_text(None, 8) == (None, False, True)
    assert as_text("é".encode() * 3, 3) == ("é", False, True)  # a character cut in two is dropped, not refused


def test_a_file_is_saved_in_the_working_tree_only(crew):
    assert crew.work.write(WS, "src/new/deep.txt", "héllo\n") == {"path": "src/new/deep.txt", "size": 7}
    assert (crew.clone / "src/new/deep.txt").read_text() == "héllo\n"
    assert crew.work.write(WS, "README.md", "# changed\n")["size"] == 10
    assert git(crew.clone, "status", "--porcelain").split() == ["M", "README.md", "??", "src/new/"]
    with pytest.raises(BadRequest):
        crew.work.write(WS, "src", "a folder")
    with pytest.raises(TooBig):
        crew.work.write(WS, "big.txt", "x" * (work_module.TEXT_MAX + 1))
    assert not (crew.clone / "big.txt").exists()


def test_nothing_is_saved_while_a_turn_holds_the_clone(crew):
    assert crew.work.live(crew.clone) is False
    with open(crew.clone.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # as glyphh_swarm's `held` does for a turn
        assert crew.work.live(crew.clone) is True
        with pytest.raises(Busy) as refused:
            crew.work.write(WS, "README.md", "mine\n")
        assert (refused.value.status, refused.value.code) == (409, "E_BUSY")
    assert (crew.clone / "README.md").read_text() == "# crew\n"
    assert crew.work.live(crew.clone) is False


def test_a_branchs_diff_is_each_file_it_changed_since_its_base_with_both_sides(crew):
    got = crew.work.diff(WS, BRANCH)
    assert (got["base"], got["branch"], got["truncated"]) == ("main", BRANCH, False)
    files = {f["path"]: f for f in got["files"]}
    assert sorted(files) == ["logo.png", "notes.md", "old.txt", "src/app.py"]
    assert files["src/app.py"] == {
        "path": "src/app.py",
        "status": "modified",
        "added": 1,
        "removed": 0,
        "binary": False,
        "before": "print('one')\n",
        "after": "print('one')\nprint('two')\n",
    }
    assert (files["notes.md"]["status"], files["notes.md"]["before"], files["notes.md"]["after"]) == (
        "added",
        None,
        "new\n",
    )
    assert (files["old.txt"]["status"], files["old.txt"]["before"], files["old.txt"]["after"]) == (
        "deleted",
        "going\n",
        None,
    )
    assert files["old.txt"]["removed"] == 1
    assert (
        files["logo.png"]["binary"] is True and files["logo.png"]["after"] is None and files["logo.png"]["added"] == 0
    )
    assert crew.work.diff(WS, BRANCH, "main") == got
    for branch, base in (("no-such", None), (BRANCH, "no-such")):
        with pytest.raises(NotFound):
            crew.work.diff(WS, branch, base)
    with pytest.raises(BadRequest):
        crew.work.diff(WS, "-x")
    with pytest.raises(BadRequest):
        crew.work.diff(WS, BRANCH, "a..b")


def test_with_no_branch_the_diff_is_the_working_trees_new_files_among_it(crew):
    assert crew.work.diff(WS) == {"base": "main", "branch": None, "files": [], "truncated": False}
    (crew.clone / "README.md").write_text("# crew\nmore\n")
    (crew.clone / "fresh.txt").write_text("a\nb\n")
    (crew.clone / "shot.png").write_bytes(PNG)
    (crew.clone / "old.txt").unlink()
    os.symlink("/etc/hosts", crew.clone / "hosts")
    files = {f["path"]: f for f in crew.work.diff(WS)["files"]}
    assert sorted(files) == ["README.md", "fresh.txt", "hosts", "old.txt", "shot.png"]
    assert (files["README.md"]["added"], files["README.md"]["after"]) == (1, "# crew\nmore\n")
    assert files["fresh.txt"] == {
        "path": "fresh.txt",
        "status": "added",
        "added": 2,
        "removed": 0,
        "binary": False,
        "before": None,
        "after": "a\nb\n",
    }
    assert files["shot.png"]["binary"] is True and files["shot.png"]["after"] is None
    assert files["old.txt"]["status"] == "deleted" and files["old.txt"]["after"] is None
    assert files["hosts"]["after"] is None  # a link out of the workspace is never followed


def test_a_diff_is_cut_at_its_limits(crew, monkeypatch):
    monkeypatch.setattr(work_module, "FILES_MAX", 2)
    cut = crew.work.diff(WS, BRANCH)
    assert len(cut["files"]) == 2 and cut["truncated"] is True
    monkeypatch.setattr(work_module, "FILES_MAX", 200)
    monkeypatch.setattr(work_module, "SIDE_MAX", 20)
    got = crew.work.diff(WS, BRANCH)
    app = next(f for f in got["files"] if f["path"] == "src/app.py")
    assert got["truncated"] is True and (app["before"], app["after"]) == ("print('one')\n", None)
    (crew.clone / "long.txt").write_text("x" * 40)
    long = next(f for f in crew.work.diff(WS)["files"] if f["path"] == "long.txt")
    assert long["after"] is None and "truncated" not in long


def test_the_base_is_the_one_named_else_githubs_head_else_main(crew):
    assert crew.work.base_of(crew.clone, "release/1.x") == "release/1.x"
    assert crew.work.base_of(crew.clone, None) == "main"
    git(crew.clone, "remote", "set-head", "origin", "--delete")
    assert crew.work.base_of(crew.clone, None) == "main"


def test_git_that_fails_or_does_not_answer_is_no_answer(crew, monkeypatch):
    assert crew.work.git(crew.clone, "rev-parse", "no-such-ref") is None

    def slow(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(subprocess, "run", slow)
    assert crew.work.git(crew.clone, "status") is None and crew.work.said(crew.clone, "status") is None
    assert crew.work.blob(crew.clone, "main", "README.md", 10) is None


def test_git_runs_with_no_config_of_the_machine_no_hooks_and_no_shell(crew, monkeypatch):
    ran: list[tuple[list[str], dict[str, object]]] = []
    real = subprocess.run

    def spy(argv: list[str], **kw: object) -> object:
        ran.append((argv, kw))
        return real(argv, **kw)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_not_for_git")
    crew.work.diff(WS, BRANCH)
    assert ran
    for argv, kw in ran:
        assert argv[:7] == [
            "git",
            "-c",
            "safe.directory=*",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
        ]
        assert "shell" not in kw and kw["timeout"] == work_module.GIT_SECONDS
        env = kw["env"]
        assert isinstance(env, dict) and env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "GITHUB_TOKEN" not in env
        if "diff" in argv:
            assert "--no-ext-diff" in argv and "--no-textconv" in argv


def test_the_command_log_is_read_from_a_line_number_on(crew):
    assert crew.work.terminal(WS) == {"lines": [], "next": 0, "live": False}
    log = crew.data / "terminal" / f"{WS}.jsonl"
    log.parent.mkdir()
    lines = [{"n": n, "at": "2026-11-22T10:00:00+00:00", "kind": "cmd", "text": f"git step {n}"} for n in range(3, 8)]
    log.write_text("\n".join([json.dumps(line) for line in lines] + ["not json", '"a string"', '{"n": "x"}']) + "\n")
    got = crew.work.terminal(WS)
    assert got["lines"] == lines and got["next"] == 8 and got["live"] is False
    assert [line["n"] for line in crew.work.terminal(WS, 6)["lines"]] == [6, 7]
    assert crew.work.terminal(WS, 8) == {"lines": [], "next": 8, "live": False}
    with open(crew.clone.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert crew.work.terminal(WS, 8)["live"] is True


def test_only_the_newest_lines_are_answered_at_once(crew, monkeypatch):
    monkeypatch.setattr(work_module, "LINES_MAX", 2)
    log = crew.data / "terminal" / f"{WS}.jsonl"
    log.parent.mkdir()
    log.write_text("".join(json.dumps({"n": n, "kind": "out", "text": str(n)}) + "\n" for n in range(5)))
    assert [line["n"] for line in crew.work.terminal(WS)["lines"]] == [3, 4]
