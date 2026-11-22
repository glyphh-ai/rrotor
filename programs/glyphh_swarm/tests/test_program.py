"""glyphh_swarm through rrotor.testing: real git against a local bare repo (the clone's GitHub), Claude Code and
GitHub's API faked (no network)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from rrotor.http import Response
from rrotor.testing import FakeHttp, Harness

HERE = Path(__file__).resolve().parent.parent
REPO = "acme/crew"
API = "https://api.github.com/"
USAGE = {"input_tokens": 1200, "output_tokens": 80}
ZERO = {"input_tokens": 0, "output_tokens": 0}
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

    def claude(
        ctx: Any, prompt: str, persona: str, model: str | None, cwd: Path, term: Any = None
    ) -> tuple[str, dict[str, int]]:
        turns.append({"prompt": prompt, "persona": persona, "model": model, "cwd": cwd})
        term.add("cmd", "pytest -q")  # as Claude Code's stream is logged (terminal.follow)
        term.add("out", "3 passed")
        act = world_state["act"]
        return (act(cwd) if callable(act) else str(act)), dict(world_state["usage"])

    world_state: dict[str, Any] = {"act": None, "usage": USAGE}
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
    assert out["usage"] == USAGE
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


def logged(world: dict[str, Any]) -> list[dict[str, Any]]:
    log = world["data"] / "terminal" / "acme__crew.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_a_turn_logs_what_it_ran_for_the_person_to_read_and_never_the_token(world):
    world["state"]["act"] = writes_hello
    harness(world).call("turn", turn_inputs())
    lines = logged(world)
    assert [line["n"] for line in lines] == list(range(len(lines))) and all(line["at"] for line in lines)
    said = [(line["kind"], line["text"]) for line in lines]
    assert said[0] == ("note", "Ada's turn on turn-1-ada-k3x9")
    assert said[1][0] == "cmd" and said[1][1].startswith("git clone --quiet --no-checkout ")
    at = said.index(("cmd", "pytest -q"))
    assert said[at + 1] == ("out", "3 passed") and said[at - 1] == ("cmd", "git checkout --quiet -B turn-1-ada-k3x9")
    assert ("cmd", "git push --quiet --force -u origin turn-1-ada-k3x9") in said
    assert ("note", "Ada's turn ended: pull request #1") in said
    assert said[-1] == ("cmd", "git checkout --quiet --force main")
    assert "ghs_test" not in json.dumps(lines) and "Authorization" not in json.dumps(lines)

    world["state"]["act"] = "Nothing to do here."
    harness(world).call("turn", turn_inputs(branch="turn-2-bo-k3x9", bot_name="Bo"))
    more = logged(world)
    assert more[: len(lines)] == lines and more[len(lines)]["n"] == len(lines)  # the log goes on, numbered on
    assert {"note": "Bo's turn ended: nothing changed"}.items() <= {m["kind"]: m["text"] for m in more[-3:]}.items()


def test_a_turn_that_fails_says_so_in_the_log(world):
    def fails(cwd: Path) -> str:
        raise world["program"].SwarmError("Claude Code failed: overloaded")

    world["state"]["act"] = fails
    harness(world).call("turn", turn_inputs())
    assert ("err", "Claude Code failed: overloaded") in [(line["kind"], line["text"]) for line in logged(world)]


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
    assert out["usage"] == USAGE  # a turn that changed nothing still used tokens
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


def pick_inputs(**over: Any) -> dict[str, Any]:
    return {"repo": REPO, "base": "main", "prompt": "Who goes next?", "github_token": "ghs_test", **over}


def test_a_pick_answers_what_claude_said_with_its_usage(world):
    def reads(cwd: Path) -> str:
        assert (cwd / "README.md").read_text() == "# crew\n"
        return '{"next": "bo", "why": "Tests are missing.", "done": false}'

    world["state"]["act"] = reads
    done = harness(world).call("pick", pick_inputs())
    assert done.status == "done", done.data
    assert done.data == {
        "status": "picked",
        "answer": '{"next": "bo", "why": "Tests are missing.", "done": false}',
        "usage": USAGE,
    }
    clone = world["data"] / "repos" / "acme__crew"
    told = world["turns"][0]
    assert (told["cwd"], told["prompt"], told["model"]) == (clone, "Who goes next?", None)
    assert told["persona"] == f"Your working folder is {clone}."
    assert world["hub"].asked == [] and "ghs_test" not in (clone / ".git" / "config").read_text()
    said = [(line["kind"], line["text"]) for line in logged(world)]  # a pick is logged as a turn is
    assert said[0] == ("note", "the orchestrator's pick on main")
    at = said.index(("cmd", "pytest -q"))
    assert said[at - 1] == ("cmd", "git checkout --quiet --force --detach origin/main")
    assert said[at + 1 : at + 3] == [("out", "3 passed"), ("note", "the orchestrator picked")]
    assert said[-1] == ("cmd", "git clean -fdxq") and "ghs_test" not in json.dumps(said)
    world["state"]["act"] = "not json at all"  # the server reads the answer, not the program
    again = harness(world).call("pick", pick_inputs(persona="You run this project.", model="opus")).data
    assert again["answer"] == "not json at all"
    told = world["turns"][1]
    assert (told["persona"], told["model"]) == (f"You run this project.\nYour working folder is {clone}.", "opus")


def test_a_pick_reads_what_github_has_now_and_leaves_the_clone_clean(world):
    world["state"]["act"] = writes_hello
    harness(world).call("turn", turn_inputs())  # the clone is there, on main as the turn left it
    (world["seed"] / "more.md").write_text("more\n")
    git(world["seed"], "add", "-A")
    git(world["seed"], "commit", "--quiet", "-m", "More")
    git(world["seed"], "push", "--quiet", "origin", "HEAD:main")
    before = git(world["bare"], "for-each-ref")

    def meddles(cwd: Path) -> str:
        assert (cwd / "more.md").exists() and not (cwd / "hello.txt").exists()
        (cwd / "notes.txt").write_text("an orchestrator changes nothing\n")
        (cwd / "README.md").write_text("# mine now\n")
        git(cwd, "add", "README.md")
        git(cwd, "commit", "--quiet", "-m", "my own commit")
        (cwd / "more.md").write_text("less\n")
        return "Bo goes next."

    world["state"]["act"] = meddles
    out = harness(world).call("pick", pick_inputs()).data
    assert out["status"] == "picked" and out["answer"] == "Bo goes next."
    clone = world["data"] / "repos" / "acme__crew"
    assert git(clone, "status", "--porcelain", "--ignored") == "" and not (clone / "notes.txt").exists()
    assert git(clone, "rev-parse", "HEAD") == git(world["seed"], "rev-parse", "HEAD")
    assert (clone / "README.md").read_text() == "# crew\n" and (clone / "more.md").read_text() == "more\n"
    assert git(world["bare"], "for-each-ref") == before and world["hub"].asked == [world["hub"].asked[0]]
    world["state"]["act"] = writes_hello  # and the next turn starts from GitHub's base, not the orchestrator's commit
    turn = harness(world).call("turn", turn_inputs(branch="turn-2-ada-k3x9")).data
    assert turn["base_sha"] == git(world["seed"], "rev-parse", "HEAD")
    assert turn["files"] == [{"path": "hello.txt", "added": 1, "removed": 0}]


@pytest.mark.parametrize(
    ("over", "said"),
    [
        ({"repo": "not a repo"}, "repo is owner/name"),
        ({"base": "a..b"}, "base is not a branch name"),
        ({"base": "-x"}, "base is not a branch name"),
        ({"base": "gone"}, "git checkout failed"),
    ],
)
def test_a_pick_refuses_a_repo_or_a_base_it_cannot_read(world, over, said):
    failed = harness(world).call("pick", pick_inputs(**over))
    assert (failed.status, failed.data["code"]) == ("failed", "E_SWARM") and said in failed.data["reason"]
    assert world["turns"] == []


def test_a_pick_claude_could_not_make_fails_saying_why_and_leaves_the_clone_clean(world):
    def fails(cwd: Path) -> str:
        (cwd / "half.txt").write_text("half")
        raise world["program"].SwarmError("Claude Code failed: overloaded")

    world["state"]["act"] = fails
    failed = harness(world).call("pick", pick_inputs())
    assert (failed.status, failed.data["code"]) == ("failed", "E_SWARM")
    assert failed.data["reason"] == "Claude Code failed: overloaded"
    clone = world["data"] / "repos" / "acme__crew"
    assert git(clone, "status", "--porcelain", "--ignored") == "" and not (clone / "half.txt").exists()
    assert ("err", "Claude Code failed: overloaded") in [(line["kind"], line["text"]) for line in logged(world)]


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
    """Claude Code as `subprocess.Popen` starts it: its stream (stream-json, one event a line) on stdout, `err` on
    the file it was given for stderr, its exit code; `hangs` never ends until it is killed."""

    def __init__(
        self, out: Any = None, code: int = 0, err: str = "", raises: BaseException | None = None, hangs: bool = False
    ) -> None:
        self.out, self.code, self.err, self.raises, self.hangs = out, code, err, raises, hangs
        self.argv: list[str] = []
        self.kw: dict[str, Any] = {}
        self.killed = threading.Event()

    def __call__(self, argv: list[str], **kw: Any) -> Ran:
        self.argv, self.kw = argv, kw
        if self.raises:
            raise self.raises
        kw["stderr"].write(self.err)
        return self

    def __enter__(self) -> Ran:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    @property
    def stdout(self) -> Iterator[str]:
        events = self.out if isinstance(self.out, list) else [self.out]
        for event in events:
            yield (event if isinstance(event, str) else json.dumps(event)) + "\n"
        if self.hangs:
            self.killed.wait(5)

    def wait(self) -> int:
        return self.code

    def kill(self) -> None:
        self.killed.set()


def ended(said: str, is_error: bool = False) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": is_error, "result": said}


def ctx_with(key: str | None) -> Any:
    from rrotor.ctx import Secrets

    return type("C", (), {"secrets": Secrets(["ANTHROPIC_API_KEY"], {"ANTHROPIC_API_KEY": key} if key else {})})()


def test_claude_code_runs_in_the_clone_on_the_orgs_key(tmp_path, monkeypatch):
    program = load()
    ran = Ran([{"type": "system", "subtype": "init"}, ended(" I did it. ")])
    monkeypatch.setattr(program.subprocess, "Popen", ran)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    said, usage = program.claude_code(ctx_with("sk-ant-x"), "Do it.", "You are Ada.", "opus", tmp_path)
    assert said == "I did it." and ran.kw["cwd"] == tmp_path
    assert usage == ZERO  # no usage reported: zeros
    assert ran.argv[:3] == ["claude", "-p", "Do it."] and ran.argv[-2:] == ["--model", "opus"]
    assert "--append-system-prompt" in ran.argv and "bypassPermissions" in ran.argv
    at = ran.argv.index("--output-format")
    assert ran.argv[at + 1 : at + 3] == ["stream-json", "--verbose"]
    env = ran.kw["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x" and env["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    assert "sk-ant-x" not in ran.argv and not Path(env["HOME"]).exists()  # its scratch home goes with it
    monkeypatch.setattr(program.subprocess, "Popen", Ran(ended("x")))
    assert program.claude_code(ctx_with("k"), "p", "s", None, tmp_path)[0] == "x"


@pytest.mark.parametrize(
    ("usage", "counted"),
    [
        (
            {
                "input_tokens": 12,
                "cache_creation_input_tokens": 300,
                "cache_read_input_tokens": 4000,
                "output_tokens": 77,
                "server_tool_use": {"web_search_requests": 1},
            },
            {"input_tokens": 4312, "output_tokens": 77},
        ),
        ({"input_tokens": 5, "output_tokens": 2}, {"input_tokens": 5, "output_tokens": 2}),
        ({"input_tokens": None, "cache_read_input_tokens": "many", "output_tokens": -3}, ZERO),
        ({}, ZERO),
        (None, ZERO),
        ("a lot", ZERO),
    ],
)
def test_claude_code_counts_its_tokens_the_three_kinds_of_input_together(tmp_path, monkeypatch, usage, counted):
    program = load()
    # The stream's last event, `result`, carries the run's usage; an earlier event's own usage is not the run's.
    stream = [
        {"type": "assistant", "message": {"content": [], "usage": {"input_tokens": 999, "output_tokens": 999}}},
        {**ended("ok"), "usage": usage},
    ]
    monkeypatch.setattr(program.subprocess, "Popen", Ran(stream))
    assert program.claude_code(ctx_with("k"), "p", "s", None, tmp_path) == ("ok", counted)


def test_what_claude_code_runs_is_logged_as_it_runs(tmp_path, monkeypatch):
    program = load()
    stream = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Running the tests."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}},
                    {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/data/repos/a/x.py"}},
                    {"type": "tool_use", "id": "t3", "name": "Glob", "input": {"pattern": "*.py"}},
                    "not a block",
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "3 passed"}, 7]},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "edited"},
                ]
            },
        },
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t4", "name": "Bash", "input": {}}]}},
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t4", "content": "no such", "is_error": True}]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": {"odd": 1}}]}},
        {"type": "user", "message": "not an object"},
        "a line that is not JSON",
        "[1, 2]",
        {**ended("Done."), "usage": {"input_tokens": 7, "cache_read_input_tokens": 100, "output_tokens": 9}},
    ]
    monkeypatch.setattr(program.subprocess, "Popen", Ran(stream))
    term = program.Terminal(tmp_path / "terminal" / "acme__crew.jsonl")
    # the same run gives both: the commands logged as they came, and the tokens from its last event
    assert program.claude_code(ctx_with("k"), "p", "s", None, tmp_path, term) == (
        "Done.",
        {"input_tokens": 107, "output_tokens": 9},
    )
    lines = [json.loads(line) for line in (tmp_path / "terminal" / "acme__crew.jsonl").read_text().splitlines()]
    assert [(line["kind"], line["text"]) for line in lines] == [
        ("cmd", "pytest -q"),
        ("note", "Edit /data/repos/a/x.py"),
        ("out", "3 passed"),
        ("err", "no such"),
    ]


@pytest.mark.parametrize(
    ("ran", "said"),
    [
        (Ran(ended("rate limited", is_error=True), 1), "Claude Code failed: rate limited"),
        (Ran("not json", 1, "boom"), "Claude Code failed: boom"),
        (Ran("not json", 1), "Claude Code failed: not json"),
        (Ran({"type": "system"}), "Claude Code failed: $"),
        (Ran(ended("x"), hangs=True), "did not finish its turn"),
        (Ran(raises=FileNotFoundError()), "is not on this rrotor"),
    ],
)
def test_claude_code_that_fails_says_why(tmp_path, monkeypatch, ran, said):
    program = load()
    monkeypatch.setattr(program.subprocess, "Popen", ran)
    monkeypatch.setattr(program, "CLAUDE_SECONDS", 0.05 if ran.hangs else 30)
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
