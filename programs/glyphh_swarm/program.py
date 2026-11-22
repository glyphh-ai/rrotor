"""glyphh_swarm: a swarm's git turns, taken on this rrotor (docs/architecture.md, 5.10). The server runs the swarm (the
crew, the rounds, the words: swarm_turns.py, which it mirrors) and calls one function at a time over the link:

    turn    {repo, base, branch, prompt, persona, model, github_token, bot_id?, bot_name?}
            the repo cloned (or fetched) into this program's data folder, a fresh branch off origin/<base>, Claude Code
            (`claude -p`, the persona its system prompt) doing the bot's part in it, the changes committed as the bot,
            pushed, and a pull request opened: {status: open|unchanged, pr_url, number, head_sha, base_sha, files, said,
            title, done}
    merge   {repo, pr_url, github_token}   the pull request merged: {status: merged, sha} or {status: conflict}
    reject  {repo, pr_url, github_token}   the pull request closed: {status: rejected}

Glyphh Bot's own (glyphh-bot, src/main/workspace.ts: beginTurn, endTurn, merge, reject), with GitHub's API in place of
`gh` (the image carries none): `GitHub` is that seam. Claude Code runs on the org's Anthropic key, this program's
secret ANTHROPIC_API_KEY. The GitHub token is the call's: in git's environment for a fetch or a push, never an
argument or a file.

What a turn runs is logged as it runs (terminal.py: git's commands, the shell commands Claude Code ran and what they
answered) in the data folder's `terminal/<owner>__<repo>.jsonl`, which the person reads from this rrotor itself (the
work panels' Terminal, `GET /v1/work/{ws}/terminal`); it never leaves the machine."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import swarm_turns
from terminal import Terminal, follow

from rrotor import Ctx, ProgramError, Result

GITHUB = "https://github.com"  # where a repo is cloned from (a test points it at a folder of bare repos)
API = "https://api.github.com"
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<![./])$")
PR_URL = re.compile(r"^https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<n>[0-9]+)$")
GIT_SECONDS = 300
CLAUDE_SECONDS = 1500  # within the manifest's 1800
KEY = "ANTHROPIC_API_KEY"
PASSED = ("PATH", "LANG", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy", "TMPDIR")


class SwarmError(ProgramError):
    code = "E_SWARM"


# git


class Git:
    """git in one clone, with the call's GitHub token in its environment (http.extraHeader), never in its config."""

    def __init__(self, folder: Path, token: str | None, term: Terminal | None = None) -> None:
        self.folder = folder
        self.term = term or Terminal(None)  # the clone's command log: each command as it is run, never its environment
        self.home = Path(tempfile.mkdtemp(prefix="glyphh-swarm-"))
        self.env = {k: v for k in PASSED if (v := os.environ.get(k))} | {
            "HOME": str(self.home),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        pairs = [("safe.directory", "*")]
        if token:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            pairs.append(("http.extraHeader", f"Authorization: Basic {basic}"))
        self.env["GIT_CONFIG_COUNT"] = str(len(pairs))
        for i, (key, value) in enumerate(pairs):
            self.env[f"GIT_CONFIG_KEY_{i}"], self.env[f"GIT_CONFIG_VALUE_{i}"] = key, value

    def __call__(self, *args: str, ok: bool = False) -> str:
        """git with these arguments in the clone: its stdout; a failure raises SwarmError (with `ok`, answers None)."""
        self.term.add("cmd", " ".join(["git", *args]))
        done = subprocess.run(
            ["git", *args],
            cwd=self.folder if self.folder.exists() else self.home,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
        )
        if done.returncode != 0:
            if ok:
                return ""
            said = (done.stderr or done.stdout).strip().splitlines()
            self.term.add("err", said[-1] if said else f"exit {done.returncode}")
            raise SwarmError(f"git {args[0]} failed: {said[-1] if said else f'exit {done.returncode}'}")
        return done.stdout.strip()

    def succeeds(self, *args: str) -> bool:
        self.term.add("cmd", " ".join(["git", *args]))
        return (
            subprocess.run(
                ["git", *args], cwd=self.folder, env=self.env, capture_output=True, check=False, timeout=GIT_SECONDS
            ).returncode
            == 0
        )

    def close(self) -> None:
        shutil.rmtree(self.home, ignore_errors=True)


def clone_of(ctx: Ctx, repo: str) -> Path:
    if ctx.data is None:
        raise SwarmError('glyphh_swarm keeps its clones in its data folder ("data": true)')
    return ctx.data / "repos" / repo.replace("/", "__")


def terminal_of(folder: Path) -> Terminal:
    """A clone's command log, beside the clones: `<data>/terminal/<owner>__<repo>.jsonl` (terminal.py)."""
    return Terminal(folder.parent.parent / "terminal" / f"{folder.name}.jsonl")


@contextlib.contextmanager
def held(folder: Path) -> Iterator[None]:
    """One turn at a time in a clone (another call for the same repo waits here)."""
    folder.parent.mkdir(parents=True, exist_ok=True)
    with open(folder.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def fetched(git: Git, repo: str) -> None:
    """The clone there and up to date with GitHub, clean (what an earlier turn cut short left is dropped)."""
    if not (git.folder / ".git").is_dir():
        git.folder.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", "--no-checkout", f"{GITHUB}/{repo}.git", str(git.folder))
    else:
        git("fetch", "--quiet", "--prune", "origin")
    git("reset", "--quiet", "--hard")
    git("clean", "-fdxq")


# GitHub, in place of gh


class GitHub:
    """GitHub's REST API as Glyphh Bot used gh: open, merge and close a pull request, delete its branch."""

    def __init__(self, http: Any, token: str) -> None:
        self.http = http
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _ask(self, method: str, path: str, body: Any = None) -> Any:
        res = self.http.request(method, f"{API}{path}", json=body, headers=self.headers, timeout=60.0)
        try:
            said = res.json()
        except ValueError:
            said = {}
        return res.status, said if isinstance(said, dict) else {}

    def open(self, repo: str, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        status, said = self._ask(
            "POST", f"/repos/{repo}/pulls", {"title": title, "head": head, "base": base, "body": body}
        )
        if status != 201:
            raise SwarmError(f"GitHub did not open the pull request ({status}): {said.get('message', '')}".strip())
        return {"url": said["html_url"], "number": said["number"]}

    def merge(self, repo: str, number: int, title: str) -> tuple[bool, str]:
        """(True, the merge commit) or (False, why it did not merge: a conflict, or a head that moved)."""
        status, said = self._ask(
            "PUT", f"/repos/{repo}/pulls/{number}/merge", {"merge_method": "merge", "commit_title": title}
        )
        if status == 200 and said.get("merged"):
            return True, str(said.get("sha") or "")
        if status in (405, 409, 422):
            return False, str(said.get("message") or "not mergeable")
        raise SwarmError(f"GitHub did not merge the pull request ({status}): {said.get('message', '')}".strip())

    def pull(self, repo: str, number: int) -> dict[str, Any]:
        status, said = self._ask("GET", f"/repos/{repo}/pulls/{number}")
        if status != 200:
            raise SwarmError(f"GitHub has no pull request {repo}#{number} ({status})")
        return said

    def close(self, repo: str, number: int) -> None:
        status, said = self._ask("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})
        if status != 200:
            raise SwarmError(f"GitHub did not close the pull request ({status}): {said.get('message', '')}".strip())

    def delete_branch(self, repo: str, branch: str) -> None:
        self._ask("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")  # gone already is fine


# Claude Code


def streamed(argv: list[str], cwd: Path, env: dict[str, str], term: Terminal) -> tuple[int, dict[str, Any] | None, str]:
    """Claude Code run to its end, its stream (one JSON event a line) read as it comes so the clone's command log
    shows each command while the turn is still at work: (its exit code, its `result` event if it sent one, what
    else it wrote: its stderr, else the lines that were not events). Killed after CLAUDE_SECONDS."""
    result: dict[str, Any] | None = None
    other: list[str] = []
    shells: set[str] = set()
    late = threading.Event()
    with (
        tempfile.TemporaryFile("w+") as errors,
        subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=errors, text=True) as proc,
    ):
        timer = threading.Timer(CLAUDE_SECONDS, lambda: (late.set(), proc.kill()))
        timer.start()
        try:
            for line in proc.stdout or ():
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if not isinstance(event, dict):
                    other.append(line)
                    continue
                if event.get("type") == "result":
                    result = event
                follow(term, event, shells)
            code = proc.wait()
        finally:
            timer.cancel()
        if late.is_set():
            raise SwarmError(f"Claude Code did not finish its turn within {CLAUDE_SECONDS} s")
        errors.seek(0)
        return code, result, errors.read() or "".join(other)


def claude_code(ctx: Ctx, prompt: str, persona: str, model: str | None, cwd: Path, term: Terminal | None = None) -> str:
    """`claude -p` in the clone, the persona its system prompt, edits and commands allowed (rrotor's sandbox is its
    sandbox), on the org's key: what it said at the end; what it ran goes to the clone's command log (`term`) as it
    runs. A test sets a fake."""
    key = ctx.secrets.get(KEY)
    if not key:
        raise SwarmError(
            f"glyphh_swarm runs Claude Code on the org's Anthropic API key: set {KEY} on it "
            "(PUT /v1/programs/glyphh_swarm/secrets/ANTHROPIC_API_KEY or secret_set)."
        )
    home = Path(tempfile.mkdtemp(prefix="glyphh-claude-"))
    env = {k: v for k in PASSED if (v := os.environ.get(k))} | {
        "HOME": str(home),
        KEY: key,
        "DISABLE_AUTOUPDATER": "1",
        "NODE_USE_ENV_PROXY": "1",
        "CI": "1",
    }
    argv = [
        "claude",
        "-p",
        prompt,
        "--append-system-prompt",
        persona,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--strict-mcp-config",
    ]
    if model:
        argv += ["--model", model]
    try:
        code, got, rest = streamed(argv, cwd, env, term or Terminal(None))
    except FileNotFoundError:
        raise SwarmError("Claude Code (claude) is not on this rrotor") from None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if code != 0 or got is None or got.get("is_error"):
        raise SwarmError(f"Claude Code failed: {str((got or {}).get('result') or rest).strip()[:300]}")
    return str(got.get("result") or "").strip()


# the functions


def _repo(inputs: dict[str, Any]) -> str:
    repo = str(inputs.get("repo") or "")
    if not REPO.match(repo):
        raise SwarmError(f"repo is owner/name on GitHub, not {repo!r}")
    return repo


def _branch(name: Any, what: str) -> str:
    text = str(name or "")
    if not BRANCH.match(text) or text.endswith(".lock"):
        raise SwarmError(f"{what} is not a branch name git takes: {text!r}")
    return text


def _pull(inputs: dict[str, Any], repo: str) -> int:
    found = PR_URL.match(str(inputs.get("pr_url") or ""))
    if found is None or found["repo"].lower() != repo.lower():
        raise SwarmError(f"pr_url is a pull request of {repo}: https://github.com/{repo}/pull/<n>")
    return int(found["n"])


def turn(ctx: Ctx, inputs: dict[str, Any]) -> Result:
    """One bot's git turn (Glyphh Bot's beginTurn, the agent's run, endTurn): a fresh branch off origin/<base>, Claude
    Code doing the bot's part, its changes committed as the bot, pushed and opened as a pull request. A turn that
    changed nothing opens none (status unchanged) and leaves no branch."""
    repo, base, branch = _repo(inputs), _branch(inputs.get("base"), "base"), _branch(inputs.get("branch"), "branch")
    bot_id = str(inputs.get("bot_id") or _named(branch)[1])
    bot_name = str(inputs.get("bot_name") or bot_id)
    token = str(inputs["github_token"])
    folder = clone_of(ctx, repo)
    with held(folder):
        term = terminal_of(folder)
        git = Git(folder, token, term)
        term.add("note", f"{bot_name}'s turn on {branch}")
        try:
            fetched(git, repo)
            git("checkout", "--quiet", "-B", base, f"origin/{base}")
            base_sha = git("rev-parse", "HEAD")
            git("checkout", "--quiet", "-B", branch)
            persona = str(inputs.get("persona") or "")
            if "Your working folder is" not in persona:
                persona = f"{persona}\n{swarm_turns.working_folder(str(folder))}".strip()
            said = claude_code(ctx, str(inputs["prompt"]), persona, inputs.get("model"), folder, term)
            ctx.log("turn taken", branch=branch, said=said[:200])
            bot = {"id": bot_id, "name": bot_name}
            ended = end_turn(ctx, git, token, (repo, base, base_sha, branch), bot, said)
            number = f": pull request #{ended['number']}" if ended["number"] else ": nothing changed"
            term.add("note", f"{bot_name}'s turn ended{number}")
            return Result.done(ended)
        except SwarmError as err:
            term.add("err", str(err))
            raise
        finally:
            git("checkout", "--quiet", "--force", base, ok=True)
            git.close()


def _named(branch: str) -> tuple[int, str]:
    """A turn's number and bot id from its branch, `turn-<n>-<botId>-<stamp>` (0 and "bot" for another name)."""
    parts = branch.split("-")
    if len(parts) >= 4 and parts[0] == "turn" and parts[1].isdigit():
        return int(parts[1]), "-".join(parts[2:-1])
    return 0, "bot"


def end_turn(
    ctx: Ctx, git: Git, token: str, where: tuple[str, str, str, str], bot: dict[str, str], said: str
) -> dict[str, Any]:
    """Glyphh Bot's endTurn: on the turn's branch (the bot may have wandered), everything added and committed as the
    bot; nothing ahead of the base, the branch dropped; else pushed and opened as a pull request."""
    repo, base, base_sha, branch = where
    if git("rev-parse", "--abbrev-ref", "HEAD") != branch:
        git("checkout", "--quiet", branch, ok=True)
    git("add", "-A")
    name = swarm_turns.title(bot["name"], said, _named(branch)[0])
    out: dict[str, Any] = {
        "branch": branch,
        "base": base,
        "base_sha": base_sha,
        "said": said,
        "title": name,
        "done": swarm_turns.is_done(said),
    }
    if not git.succeeds("diff", "--cached", "--quiet"):
        git(
            "-c",
            f"user.name={bot['name']}",
            "-c",
            f"user.email={bot['id']}@glyphhbot.local",
            "commit",
            "--quiet",
            "-m",
            name,
        )
    if int(git("rev-list", "--count", f"{base_sha}..HEAD")) == 0:
        git("checkout", "--quiet", base)
        git("branch", "-D", branch, ok=True)
        return {**out, "status": "unchanged", "pr_url": None, "number": None, "head_sha": None, "files": []}
    head_sha = git("rev-parse", "HEAD")
    files = []
    for line in git("diff", "--numstat", base_sha, head_sha).splitlines():
        added, removed, path = line.split("\t", 2)
        files.append(
            {
                "path": path,
                "added": int(added) if added.isdigit() else 0,
                "removed": int(removed) if removed.isdigit() else 0,
            }
        )
    git("push", "--quiet", "--force", "-u", "origin", branch)
    pr = GitHub(ctx.http, token).open(repo, branch, base, name, said)
    git("checkout", "--quiet", base)
    return {**out, "status": "open", "pr_url": pr["url"], "number": pr["number"], "head_sha": head_sha, "files": files}


def merge(ctx: Ctx, inputs: dict[str, Any]) -> Result:
    """Glyphh Bot's merge: the pull request merged on GitHub (a merge commit), its branch deleted there and in the
    clone; a pull request that does not merge (a conflict) stays open: {status: conflict, reason}."""
    repo = _repo(inputs)
    number = _pull(inputs, repo)
    hub = GitHub(ctx.http, str(inputs["github_token"]))
    pr = hub.pull(repo, number)
    branch, title = str(pr["head"]["ref"]), f"Merge PR #{number}: {pr.get('title') or ''}".strip()
    if pr.get("merged"):
        return Result.done({"status": "merged", "sha": pr.get("merge_commit_sha"), "branch": branch})
    merged, said = hub.merge(repo, number, title)
    if not merged:
        return Result.done({"status": "conflict", "reason": said, "branch": branch})
    if inputs.get("delete_branch", True):
        hub.delete_branch(repo, branch)
    _drop_local(ctx, repo, branch)
    return Result.done({"status": "merged", "sha": said, "branch": branch})


def reject(ctx: Ctx, inputs: dict[str, Any]) -> Result:
    """Glyphh Bot's reject: the pull request closed on GitHub, its branch deleted there and in the clone."""
    repo = _repo(inputs)
    number = _pull(inputs, repo)
    hub = GitHub(ctx.http, str(inputs["github_token"]))
    branch = str(hub.pull(repo, number)["head"]["ref"])
    hub.close(repo, number)
    if inputs.get("delete_branch", True):
        hub.delete_branch(repo, branch)
    _drop_local(ctx, repo, branch)
    return Result.done({"status": "rejected", "branch": branch})


def _drop_local(ctx: Ctx, repo: str, branch: str) -> None:
    folder = clone_of(ctx, repo)
    if (folder / ".git").is_dir():
        with held(folder):
            git = Git(folder, None)
            try:
                git("branch", "-D", branch, ok=True)
            finally:
                git.close()
