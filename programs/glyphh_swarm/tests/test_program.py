"""glyphh_swarm through rrotor.testing: real git against a local bare repo (the clone's GitHub), Claude Code and
GitHub's API faked (no network)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rrotor.http import Response
from rrotor.testing import FakeHttp, Harness

HERE = Path(__file__).resolve().parent.parent
REPO = "acme/crew"
API = "https://api.github.com/"
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("glyphh_swarm_program", HERE / "program.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


class Hub:
    """GitHub's API for one repo: pull requests opened, merged, closed, branches deleted."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, Any]] = []
        self.pulls: dict[int, dict[str, Any]] = {}
        self.mergeable = True

    def __call__(self, method: str, url: str, body: bytes | None) -> Response:
        sent = json.loads(body) if body else None
        path = url[len(API) - 1 :]
        self.asked.append((method, path, sent))
        if method == "POST" and path == f"/repos/{REPO}/pulls":
            n = len(self.pulls) + 1
            self.pulls[n] = {
                "number": n,
                "head": {"ref": sent["head"]},
                "title": sent["title"],
                "merged": False,
                "html_url": f"https://github.com/{REPO}/pull/{n}",
            }
            return Response(201, json.dumps(self.pulls[n]).encode())
        if path.startswith(f"/repos/{REPO}/pulls/"):
            n = int(path.split("/")[5])
            if n not in self.pulls:
                return Response(404, b'{"message": "Not Found"}')
            if method == "GET":
                return Response(200, json.dumps(self.pulls[n]).encode())
            if method == "PUT":
                if not self.mergeable:
                    return Response(405, b'{"message": "Pull Request is not mergeable"}')
                self.pulls[n]["merged"] = True
                return Response(200, b'{"merged": true, "sha": "' + b"m" * 40 + b'"}')
            if method == "PATCH":
                self.pulls[n]["state"] = "closed"
                return Response(200, b"{}")
        if method == "DELETE":
            return Response(204, b"")
        return Response(500, b"not here")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A bare repo acme/crew with a README on main, the program pointed at it, Claude Code faked."""
    remote = tmp_path / "github"
    bare = remote / "acme" / "crew.git"
    bare.mkdir(parents=True)
    git(bare, "init", "--quiet", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "--quiet", str(bare), str(seed))
    (seed / "README.md").write_text("# crew\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "Start")
    git(seed, "push", "--quiet", "origin", "HEAD:main")
    program = load()
    monkeypatch.setattr(program, "GITHUB", f"file://{remote}")
    turns: list[dict[str, Any]] = []

    def claude(ctx: Any, prompt: str, persona: str, model: str | None, cwd: Path) -> str:
        turns.append({"prompt": prompt, "persona": persona, "model": model, "cwd": cwd})
        act = world_state["act"]
        return act(cwd) if callable(act) else str(act)

    world_state: dict[str, Any] = {"act": None}
    monkeypatch.setattr(program, "claude_code", claude)
    hub = Hub()
    return {
        "program": program,
        "bare": bare,
        "seed": seed,
        "turns": turns,
        "hub": hub,
        "state": world_state,
        "data": tmp_path / "data",
        "files": tmp_path / "files",
    }


def harness(w: dict[str, Any]) -> Harness:
    w["files"].mkdir(exist_ok=True)
    return Harness(w["program"], w["files"], http=FakeHttp({API: w["hub"]}), data=w["data"])


def turn_inputs(**over: Any) -> dict[str, Any]:
    return {
        "repo": REPO,
        "base": "main",
        "branch": "turn-1-ada-k3x9",
        "prompt": "Write hello.",
        "persona": "You are Ada.",
        "model": "sonnet",
        "github_token": "ghs_test",
        "bot_name": "Ada",
        **over,
    }


def writes_hello(cwd: Path) -> str:
    (cwd / "hello.txt").write_text("hello\n")
    return "I wrote hello. Next someone should test it."


def test_a_turn_commits_as_the_bot_pushes_and_opens_a_pull_request(world):
    world["state"]["act"] = writes_hello
    done = harness(world).call("turn", turn_inputs())
    assert done.status == "done", done.data
    out = done.data
    assert (out["status"], out["number"], out["pr_url"]) == ("open", 1, f"https://github.com/{REPO}/pull/1")
    assert out["files"] == [{"path": "hello.txt", "added": 1, "removed": 0}] and out["done"] is False
    assert out["title"] == "Ada: I wrote hello." and out["said"].endswith("test it.")
    assert out["head_sha"] == git(world["bare"], "rev-parse", "turn-1-ada-k3x9")
    assert out["base_sha"] == git(world["bare"], "rev-parse", "main")
    assert git(world["bare"], "log", "-1", "--format=%an <%ae> %s", "turn-1-ada-k3x9") == (
        "Ada <ada@glyphhbot.local> Ada: I wrote hello."
    )
    method, path, sent = world["hub"].asked[0]
    assert (method, path) == ("POST", f"/repos/{REPO}/pulls")
    assert sent == {"title": "Ada: I wrote hello.", "head": "turn-1-ada-k3x9", "base": "main", "body": out["said"]}
    clone = world["data"] / "repos" / "acme__crew"
    told = world["turns"][0]
    assert told["cwd"] == clone and told["prompt"] == "Write hello." and told["model"] == "sonnet"
    assert told["persona"] == f"You are Ada.\nYour working folder is {clone}."
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main" and not (clone / "hello.txt").exists()
    assert "ghs_test" not in (clone / ".git" / "config").read_text()


def test_the_next_turn_starts_from_what_github_has_now(world):
    world["state"]["act"] = writes_hello
    harness(world).call("turn", turn_inputs())
    (world["seed"] / "more.md").write_text("more\n")
    git(world["seed"], "add", "-A")
    git(world["seed"], "commit", "--quiet", "-m", "More")
    git(world["seed"], "push", "--quiet", "origin", "HEAD:main")
    leftover = world["data"] / "repos" / "acme__crew" / "stray.txt"
    leftover.write_text("an earlier turn cut short")

    def edits(cwd: Path) -> str:
        assert (cwd / "more.md").exists() and not (cwd / "stray.txt").exists()
        (cwd / "README.md").write_text("# crew, by Bo\n")
        return "Done with the readme.\nDONE"

    world["state"]["act"] = edits
    out = harness(world).call("turn", turn_inputs(branch="turn-2-bo-k3x9", bot_name="Bo", bot_id="bo")).data
    assert out["base_sha"] == git(world["seed"], "rev-parse", "HEAD") and out["done"] is True
    assert out["files"] == [{"path": "README.md", "added": 1, "removed": 1}]
    assert git(world["bare"], "log", "-1", "--format=%ae", "turn-2-bo-k3x9") == "bo@glyphhbot.local"


def test_a_turn_that_changed_nothing_opens_nothing_and_leaves_no_branch(world):
    world["state"]["act"] = "Nothing to do here."
    out = harness(world).call("turn", turn_inputs()).data
    assert (out["status"], out["pr_url"], out["head_sha"], out["files"]) == ("unchanged", None, None, [])
    clone = world["data"] / "repos" / "acme__crew"
    assert git(clone, "branch", "--list", "turn-*") == "" and world["hub"].asked == []
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "turn-1-ada-k3x9"], cwd=world["bare"], capture_output=True
        ).returncode
        != 0
    )


def test_a_bot_that_committed_or_wandered_still_ends_on_its_branch(world):
    def wanders(cwd: Path) -> str:
        (cwd / "a.txt").write_text("a\n")
        git(cwd, "add", "a.txt")
        git(cwd, "commit", "--quiet", "-m", "my own commit")
        (cwd / "b.txt").write_text("b\n")
        return "Two files"

    world["state"]["act"] = wanders
    out = harness(world).call("turn", turn_inputs()).data
    assert out["status"] == "open" and [f["path"] for f in out["files"]] == ["a.txt", "b.txt"]
    assert out["title"] == "Ada: Two files"


def test_a_turn_claude_could_not_take_fails_and_leaves_the_clone_on_its_base(world):
    def fails(cwd: Path) -> str:
        (cwd / "half.txt").write_text("half")
        raise world["program"].SwarmError("Claude Code failed: overloaded")

    world["state"]["act"] = fails
    failed = harness(world).call("turn", turn_inputs())
    assert (failed.status, failed.data["code"]) == ("failed", "E_SWARM") and "overloaded" in failed.data["reason"]
    clone = world["data"] / "repos" / "acme__crew"
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main" and world["hub"].asked == []


def test_a_pull_request_github_will_not_open_fails_the_turn(world):
    world["state"]["act"] = writes_hello
    h = Harness(
        world["program"],
        world["files"],
        http=FakeHttp({API: Response(422, b'{"message": "No commits"}')}),
        data=world["data"],
    )
    world["files"].mkdir(exist_ok=True)
    failed = h.call("turn", turn_inputs())
    assert failed.status == "failed" and "did not open the pull request (422): No commits" in failed.data["reason"]


@pytest.mark.parametrize(
    ("over", "said"),
    [
        ({"repo": "not a repo"}, "repo is owner/name"),
        ({"branch": "-x"}, "branch is not a branch name"),
        ({"base": "a..b"}, "base is not a branch name"),
        ({"branch": "x.lock"}, "branch is not a branch name"),
    ],
)
def test_a_turn_refuses_names_git_or_github_would_not_take(world, over, said):
    failed = harness(world).call("turn", turn_inputs(**over))
    assert failed.status == "failed" and said in failed.data["reason"]


def test_a_turn_needs_its_data_folder(world):
    h = Harness(world["program"], world["files"], http=FakeHttp({API: world["hub"]}))
    world["files"].mkdir(exist_ok=True)
    assert '"data": true' in h.call("turn", turn_inputs()).data["reason"]


def test_merge_merges_on_github_and_drops_the_branch_everywhere(world):
    world["state"]["act"] = writes_hello
    h = harness(world)
    url = h.call("turn", turn_inputs()).data["pr_url"]
    merged = harness(world).call("merge", {"repo": REPO, "pr_url": url, "github_token": "ghs_test"})
    assert merged.data == {"status": "merged", "sha": "m" * 40, "branch": "turn-1-ada-k3x9"}
    asked = [(m, p) for m, p, _ in world["hub"].asked[1:]]
    assert asked == [
        ("GET", f"/repos/{REPO}/pulls/1"),
        ("PUT", f"/repos/{REPO}/pulls/1/merge"),
        ("DELETE", f"/repos/{REPO}/git/refs/heads/turn-1-ada-k3x9"),
    ]
    assert world["hub"].asked[2][2] == {"merge_method": "merge", "commit_title": "Merge PR #1: Ada: I wrote hello."}
    assert git(world["data"] / "repos" / "acme__crew", "branch", "--list", "turn-*") == ""
    again = harness(world).call("merge", {"repo": REPO, "pr_url": url, "github_token": "ghs_test"})
    assert again.data["status"] == "merged"  # merged already: said so, nothing asked of GitHub but the pull


def test_a_pull_request_that_conflicts_stays_open(world):
    world["state"]["act"] = writes_hello
    url = harness(world).call("turn", turn_inputs()).data["pr_url"]
    world["hub"].mergeable = False
    got = harness(world).call("merge", {"repo": REPO, "pr_url": url, "github_token": "t", "delete_branch": False})
    assert got.data == {"status": "conflict", "reason": "Pull Request is not mergeable", "branch": "turn-1-ada-k3x9"}
    assert world["hub"].pulls[1]["merged"] is False and world["hub"].asked[-1][0] == "PUT"


def test_merge_says_what_github_said_when_it_fails_otherwise(world):
    world["state"]["act"] = writes_hello
    url = harness(world).call("turn", turn_inputs()).data["pr_url"]
    broken = Harness(
        world["program"],
        world["files"],
        data=world["data"],
        http=FakeHttp({f"{API}repos/{REPO}/pulls/1/merge": Response(500, b"oops"), API: world["hub"]}),
    )
    failed = broken.call("merge", {"repo": REPO, "pr_url": url, "github_token": "t"})
    assert "did not merge the pull request (500)" in failed.data["reason"]
    missing = harness(world).call(
        "merge", {"repo": REPO, "pr_url": f"https://github.com/{REPO}/pull/9", "github_token": "t"}
    )
    assert "has no pull request acme/crew#9 (404)" in missing.data["reason"]


def test_reject_closes_the_pull_request_and_drops_its_branch(world):
    world["state"]["act"] = writes_hello
    url = harness(world).call("turn", turn_inputs()).data["pr_url"]
    got = harness(world).call("reject", {"repo": REPO, "pr_url": url, "github_token": "t"})
    assert (
        got.data == {"status": "rejected", "branch": "turn-1-ada-k3x9"} and world["hub"].pulls[1]["state"] == "closed"
    )
    assert world["hub"].asked[-1][0] == "DELETE"
    broken = Harness(
        world["program"],
        world["files"],
        data=world["data"],
        http=FakeHttp(
            {
                f"{API}repos/{REPO}/pulls/1": lambda m, u, b: Response(
                    403 if m == "PATCH" else 200, b'{"head": {"ref": "x"}}'
                )
            }
        ),
    )
    assert (
        "did not close the pull request (403)"
        in broken.call("reject", {"repo": REPO, "pr_url": url, "github_token": "t"}).data["reason"]
    )


def test_a_pull_request_of_another_repo_is_refused(world):
    got = harness(world).call(
        "merge", {"repo": REPO, "pr_url": "https://github.com/other/repo/pull/1", "github_token": "t"}
    )
    assert got.status == "failed" and "pr_url is a pull request of acme/crew" in got.data["reason"]


def test_merge_and_reject_without_a_clone_here_touch_only_github(world):
    world["hub"].pulls[3] = {"number": 3, "head": {"ref": "turn-3-x-y"}, "title": "t", "merged": False}
    url = f"https://github.com/{REPO}/pull/3"
    assert (
        harness(world)
        .call("reject", {"repo": REPO, "pr_url": url, "github_token": "t", "delete_branch": False})
        .data["status"]
        == "rejected"
    )
    assert not (world["data"] / "repos" / "acme__crew").exists()


# Claude Code, as the program runs it


class Ran:
    def __init__(self, out: Any = None, code: int = 0, err: str = "", raises: BaseException | None = None) -> None:
        self.out, self.code, self.err, self.raises = out, code, err, raises
        self.argv: list[str] = []
        self.kw: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.argv, self.kw = argv, kw
        if self.raises:
            raise self.raises
        stdout = self.out if isinstance(self.out, str) else json.dumps(self.out)
        return subprocess.CompletedProcess(argv, self.code, stdout, self.err)


def ctx_with(key: str | None) -> Any:
    from rrotor.ctx import Secrets

    return type("C", (), {"secrets": Secrets(["ANTHROPIC_API_KEY"], {"ANTHROPIC_API_KEY": key} if key else {})})()


def test_claude_code_runs_in_the_clone_on_the_orgs_key(tmp_path, monkeypatch):
    program = load()
    ran = Ran({"result": " I did it. ", "is_error": False})
    monkeypatch.setattr(program.subprocess, "run", ran)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    said = program.claude_code(ctx_with("sk-ant-x"), "Do it.", "You are Ada.", "opus", tmp_path)
    assert said == "I did it." and ran.kw["cwd"] == tmp_path
    assert ran.argv[:3] == ["claude", "-p", "Do it."] and ran.argv[-2:] == ["--model", "opus"]
    assert "--append-system-prompt" in ran.argv and "bypassPermissions" in ran.argv
    env = ran.kw["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x" and env["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    assert "sk-ant-x" not in ran.argv
    monkeypatch.setattr(program.subprocess, "run", Ran({"result": "x"}))
    assert program.claude_code(ctx_with("k"), "p", "s", None, tmp_path) == "x"


@pytest.mark.parametrize(
    ("ran", "said"),
    [
        (Ran({"result": "rate limited", "is_error": True}, 1), "Claude Code failed: rate limited"),
        (Ran("not json", 1, "boom"), "Claude Code failed: boom"),
        (Ran(raises=subprocess.TimeoutExpired("claude", 1)), "did not finish its turn"),
        (Ran(raises=FileNotFoundError()), "is not on this rrotor"),
    ],
)
def test_claude_code_that_fails_says_why(tmp_path, monkeypatch, ran, said):
    program = load()
    monkeypatch.setattr(program.subprocess, "run", ran)
    with pytest.raises(program.SwarmError, match=said):
        program.claude_code(ctx_with("k"), "p", "s", None, tmp_path)


def test_claude_code_needs_the_orgs_key(tmp_path):
    program = load()
    with pytest.raises(program.SwarmError, match="set ANTHROPIC_API_KEY"):
        program.claude_code(ctx_with(None), "p", "s", None, tmp_path)


def test_git_keeps_the_token_out_of_its_arguments_and_says_what_failed(tmp_path):
    program = load()
    g = program.Git(tmp_path / "nowhere", "ghs_secret")
    assert any(v.startswith("Authorization: Basic ") for v in g.env.values())
    with pytest.raises(program.SwarmError, match="git rev-parse failed"):
        g("rev-parse", "HEAD")
    assert g("rev-parse", "HEAD", ok=True) == ""
    g.close()
    assert not g.home.exists()
    assert sys.modules.get("swarm_turns") is not None
