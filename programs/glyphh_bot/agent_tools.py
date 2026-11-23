"""The tools of a bot's agent turn, and the gate every call of one passes (docs/architecture.md, 5.10). The agent
has exactly these, as one in-process MCP server named `bot`; Claude Code's own tools do not exist in the session.

    ServerTools   the server-side tools (the connections', decide, fetch, ...): each call is a POST of {tool, input}
                  to the server's tool address, answered {result}, {error} or {ask: true}
    FileTools     the files of the folders the run was given (rrotor.files): list, read, search, write, move; a
                  change is made only on the server's word (its `file_change`)
    ScriptTools   run_script: one script a plugin of the bot declares, run in this run's sandbox; no other command

A tool that is not read-only is asked about first (`Gate.can_use`, the Agent SDK's permission callback): the server
is called there, and it decides. `{ask: true}` means it stopped the run with its question: the turn is interrupted
and the run waits (`Gate.asked`). A result is kept and handed to the tool's own call, so the server is asked once.

Nothing a tool answered is kept here or sent up: `steps` names the tools used and whether each worked, `files`
the files read and changed."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from file_agent import LISTED, NO, TOOLS, YES, question

from rrotor import ProgramError
from rrotor.files import Change, FileRefused, Files

SERVER = "bot"  # the MCP server's name: a tool is mcp__bot__<name> to Claude Code
TOOL_SECONDS = 120.0
SCRIPT_SECONDS = 120
SCRIPT_OUT = 64 * 1024  # the characters of a script's stdout, and of its stderr, the agent is given
ARGS_MAX, STDIN_MAX = 64, 1024 * 1024
WAIT = "The person is being asked first. Stop here: say nothing more and wait for their answer."
WAITING = "The bot is waiting for the person's answer."
READS = ("list_folder", "read_file", "search_files")
RUNNERS = {".py": [sys.executable], ".sh": ["/bin/sh"], ".js": ["node"], ".mjs": ["node"]}
PASSED = ("PATH", "LANG", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "NO_PROXY", "no_proxy", "TMPDIR")
SCRIPT = {
    "type": "object",
    "properties": {
        "plugin": {"type": "string", "description": "The plugin the script is in"},
        "script": {"type": "string", "description": "The script, by its path in the plugin, as listed"},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Its arguments"},
        "stdin": {"type": "string", "description": "What it reads on standard input"},
    },
    "required": ["plugin", "script"],
    "additionalProperties": False,
}


class Ask(Exception):
    """The person is to be asked: the run stops here and waits. `prompt` is the question as this rrotor knows it,
    `detail` what the Result says beside it."""

    def __init__(self, prompt: str, options: list[str] | None = None, detail: dict[str, Any] | None = None) -> None:
        super().__init__(prompt)
        self.prompt, self.options, self.detail = prompt, options, detail or {}


class Refused(Exception):
    """The call is not made, and the agent is told why."""


@dataclass(frozen=True, slots=True)
class Spec:
    """One tool: `name` as the agent calls it, `key` as the server and the run's steps name it."""

    name: str
    key: str
    description: str
    schema: dict[str, Any]
    read_only: bool
    kind: Any  # whose it is: its `check(spec, given)` before a call, its `run(spec, given)` -> (text, ok)


def digest(*what: Any) -> str:
    return hashlib.sha256(json.dumps(what, sort_keys=True, default=str).encode()).hexdigest()


def schema_of(given: Any) -> dict[str, Any]:
    """A tool's input schema as the Agent SDK reads one as JSON Schema: an object with `properties` (a dict without
    both it takes for a map of parameter names to types, so `{"type": "object"}` would ask for a `type`)."""
    given = given if isinstance(given, dict) else {}
    properties = given.get("properties")
    return {**given, "type": "object", "properties": properties if isinstance(properties, dict) else {}}


class Server:
    """The server's tool address for this run: `call(tool, input)` is `{"result"}`, `{"error"}` or
    `{"ask": True, "question"}`. A server out of reach or answering anything else is an error, told plainly."""

    def __init__(self, http: Any, url: str, token: str) -> None:
        self._http, self._url, self._token = http, url, token

    def call(self, tool: str, given: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            res = self._http.post(self._url, json={"tool": tool, "input": given}, headers=headers, timeout=TOOL_SECONDS)
        except ProgramError as err:
            return {"error": f"The server could not be asked: {err.message}"}
        try:
            body = res.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return {"error": f"The server answered {res.status}."}
        if body.get("ask") is True:
            return {"ask": True, "question": body.get("question")}
        if body.get("error"):
            return {"error": str(body["error"])}
        if res.ok and "result" in body:
            return {"result": body["result"]}
        return {"error": f"The server answered {res.status}."}

    def ask(self, tool: str, given: Any) -> dict[str, Any]:
        """`call`, the server's `{ask: true}` raised as Ask."""
        answer = self.call(tool, given)
        if answer.get("ask"):
            said = answer.get("question")
            raise Ask(said if isinstance(said, str) and said else WAITING)
        return answer


class ServerTools:
    """The server-side tools the call names (`[{name, description, input_schema, read_only}]`)."""

    def __init__(self, server: Server, tools: list[dict[str, Any]]) -> None:
        self._server = server
        self._kept: dict[str, list[dict[str, Any]]] = {}  # answers the gate got, for the tool's own call
        self._tools = tools

    def specs(self) -> list[Spec]:
        return [
            Spec(
                re.sub(r"[^A-Za-z0-9_]", "_", str(t["name"]))[:64],
                str(t["name"]),
                str(t.get("description") or ""),
                schema_of(t.get("input_schema")),
                t.get("read_only") is True,
                self,
            )
            for t in self._tools
        ]

    def check(self, spec: Spec, given: dict[str, Any]) -> None:
        self._kept.setdefault(digest(spec.key, given), []).append(self._server.ask(spec.key, given))

    def run(self, spec: Spec, given: dict[str, Any]) -> tuple[str, bool]:
        kept = self._kept.get(digest(spec.key, given))
        answer = kept.pop(0) if kept else self._server.ask(spec.key, given)
        if "error" in answer:
            return answer["error"], False
        result = answer["result"]
        return result if isinstance(result, str) else json.dumps(result), True


class _Seen(Exception):
    """The change a write or a move would make, caught before anything is touched."""

    def __init__(self, change: Change) -> None:
        super().__init__(change.name)
        self.change = change


class FileTools:
    """The files of the run's folders. `touched` and `read_bytes` carry on from the run's earlier goes."""

    def __init__(self, server: Server, folders: Any, touched: list[dict[str, str]], read_bytes: int) -> None:
        self._server = server
        self.files = Files(folders, before=self._before)
        self.files.touched, self.files.read_bytes = [dict(t) for t in touched], read_bytes
        self._probe = Files(folders, before=self._see)
        self._granted: set[str] = set()

    def specs(self) -> list[Spec]:
        return [
            Spec(t["name"], t["name"], t["description"], t["input_schema"], t["name"] in READS, self) for t in TOOLS
        ]

    def named(self) -> list[dict[str, str]]:
        return self.files.touched[-LISTED:]

    @staticmethod
    def _see(change: Change) -> None:
        raise _Seen(change)

    def _before(self, change: Change) -> None:
        key = digest(change.view())
        if key not in self._granted:
            raise FileRefused("This change was not allowed: it was not made.")
        self._granted.discard(key)

    def check(self, spec: Spec, given: dict[str, Any]) -> None:
        """A write or a move, before it is made: what it would change is worked out with nothing touched, and the
        server says whether it may be made now."""
        try:
            if spec.name == "write_file":
                self._probe.write(given.get("path"), given.get("content"))
            elif spec.name == "move_file":
                self._probe.move(given.get("path"), given.get("to"))
            return
        except _Seen as seen:
            change = seen.change
        except (FileRefused, OSError) as err:
            raise Refused(getattr(err, "message", None) or f"That could not be done: {err}.") from None
        answer = self._server.ask("file_change", change.view())
        if "error" in answer:
            raise Refused(f"The change was not made: {answer['error']}")
        said = answer["result"]
        if not (isinstance(said, dict) and said.get("allowed") is True):
            raise Ask(question(change), [YES, NO], {"approval": True, "file": change.view()})
        self._granted.add(digest(change.view()))

    def run(self, spec: Spec, given: dict[str, Any]) -> tuple[str, bool]:
        try:
            return self._do(spec.name, given), True
        except FileRefused as err:
            return err.message, False
        except OSError as err:
            return f"That could not be done: {err.strerror or err}.", False

    def _do(self, name: str, given: dict[str, Any]) -> str:
        if name == "list_folder":
            return json.dumps(self.files.list(given.get("path")))
        if name == "search_files":
            return json.dumps(self.files.search(given.get("query"), given.get("under")))
        if name == "write_file":
            return json.dumps(self.files.write(given.get("path"), given.get("content")))
        if name == "move_file":
            return json.dumps(self.files.move(given.get("path"), given.get("to")))
        read = self.files.read(given.get("path"))
        if read.kind == "pdf":
            raise FileRefused(f"{read.name} is a PDF: a PDF cannot be read here yet.")
        cut = f"\n\n[{read.name} is longer: only its first {len(read.text):,} characters are here.]"
        return read.text + (cut if read.truncated else "")


class ScriptTools:
    """`run_script`: one of the scripts the bot's plugins declare (`declared`, agent_plugins), run in this run's
    sandbox with `cwd` its working folder. The server is asked first (`run_script`, with the plugin, the script and
    its arguments: `{result: {allowed: true}}` lets it run), and only a call it allowed runs."""

    def __init__(self, server: Server, declared: dict[tuple[str, str], Path], cwd: Path) -> None:
        self._server, self._declared, self._cwd = server, declared, cwd
        self._granted: set[str] = set()

    def specs(self) -> list[Spec]:
        if not self._declared:
            return []
        listed = "; ".join(f"plugin {p}: {s}" for p, s in sorted(self._declared))
        about = f"Run one of this bot's own scripts and get what it printed. Only these exist: {listed}."
        return [Spec("run_script", "run_script", about, SCRIPT, False, self)]

    def _argv(self, given: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        """The command for this call, and the call as the server is told of it (never its stdin)."""
        plugin, script, args = str(given.get("plugin")), str(given.get("script")), given.get("args") or []
        path = self._declared.get((plugin, script))
        if path is None or not path.is_file():
            raise Refused(f"{plugin} has no script {script}: only the scripts listed can be run.")
        runner = RUNNERS.get(path.suffix)
        if runner is None:
            raise Refused(f"{script} is not a script this rrotor runs: it runs .py, .sh, .js and .mjs.")
        if not isinstance(args, list) or len(args) > ARGS_MAX or not all(isinstance(a, str) for a in args):
            raise Refused(f"A script's arguments are at most {ARGS_MAX} strings.")
        if len(str(given.get("stdin") or "")) > STDIN_MAX:
            raise Refused("That is too much standard input for a script.")
        return [*runner, str(path), *args], {"plugin": plugin, "script": script, "args": args}

    def check(self, spec: Spec, given: dict[str, Any]) -> None:
        _, told = self._argv(given)
        answer = self._server.ask("run_script", told)
        said = answer.get("result")
        if not (isinstance(said, dict) and said.get("allowed") is True):
            raise Refused(str(answer.get("error") or "The script may not be run."))
        self._granted.add(digest(given))

    def run(self, spec: Spec, given: dict[str, Any]) -> tuple[str, bool]:
        try:
            argv, _ = self._argv(given)
        except Refused as no:
            return str(no), False
        if digest(given) not in self._granted:
            return "The script may not be run.", False
        self._granted.discard(digest(given))
        env = {k: v for k in PASSED if (v := os.environ.get(k))} | {"HOME": str(self._cwd)}
        try:
            done = subprocess.run(
                argv,
                input=str(given.get("stdin") or ""),
                cwd=self._cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=SCRIPT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"The script did not finish within {SCRIPT_SECONDS} seconds.", False
        except OSError as err:
            return f"The script could not be run: {err.strerror or err}.", False
        said = {"exit_code": done.returncode, "stdout": done.stdout[:SCRIPT_OUT], "stderr": done.stderr[:SCRIPT_OUT]}
        return json.dumps(said), done.returncode == 0


class Gate:
    """Every tool of the turn and the two walls in front of them: `can_use` (the Agent SDK's `can_use_tool`) and
    `wall` (a PreToolUse hook). `sdk` is the claude_agent_sdk module, `kinds` whose tools they are (the first to
    name a tool keeps the name), `steps` the run's list of the tools it used, `skills` the bot's own skills
    (`<plugin>:<skill>`): Claude Code's Skill tool passes for those, by either name, and for nothing else."""

    def __init__(self, sdk: Any, kinds: list[Any], steps: list[dict[str, Any]], skills: Any = ()) -> None:
        self._sdk, self._steps = sdk, steps
        self._skills = {str(s) for s in skills} | {str(s).rpartition(":")[2] for s in skills}
        self.specs: dict[str, Spec] = {}
        for kind in kinds:
            for spec in kind.specs():
                self.specs.setdefault(f"mcp__{SERVER}__{spec.name}", spec)
        self.asked: Ask | None = None  # the question the run stopped at
        self.interrupt = False  # a tool asked from inside its own call: the turn is to be interrupted

    def allowed(self) -> list[str]:
        """The tools that never ask: the read-only ones."""
        return [name for name, spec in self.specs.items() if spec.read_only]

    def _skill(self, name: str, given: Any) -> bool:
        """Whether this is Claude Code's Skill tool, asked for one of the bot's own skills."""
        asked = given.get("skill") if isinstance(given, dict) else None
        return name == "Skill" and isinstance(asked, str) and asked.lstrip("/") in self._skills

    async def can_use(self, name: str, given: dict[str, Any], context: Any = None) -> Any:
        spec = self.specs.get(name)
        if spec is None:
            if self._skill(name, given):
                return self._sdk.PermissionResultAllow(updated_input=given)
            return self._sdk.PermissionResultDeny(message=f"{name} is not one of this bot's tools.")
        if self.asked is not None:
            return self._sdk.PermissionResultDeny(message=WAIT, interrupt=True)
        try:
            await asyncio.to_thread(spec.kind.check, spec, given)
        except Ask as ask:
            self.asked = ask
            return self._sdk.PermissionResultDeny(message=WAIT, interrupt=True)
        except Refused as no:
            return self._sdk.PermissionResultDeny(message=str(no))
        return self._sdk.PermissionResultAllow(updated_input=given)

    async def wall(self, hook_input: Any, tool_use_id: Any = None, context: Any = None) -> dict[str, Any]:
        """Denies any tool that is not one of this run's own (or Skill, for one of the bot's own skills), whatever
        else would have let it through."""
        name = str(hook_input.get("tool_name"))
        if name in self.specs or self._skill(name, hook_input.get("tool_input")):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"{name} is not one of this bot's tools.",
            }
        }

    async def _call(self, spec: Spec, given: dict[str, Any]) -> dict[str, Any]:
        waits = {"content": [{"type": "text", "text": WAIT}], "is_error": True}
        if self.asked is not None:
            return waits
        try:
            text, ok = await asyncio.to_thread(spec.kind.run, spec, given)
        except Ask as ask:
            self.asked, self.interrupt = ask, True
            return waits
        self._steps.append({"tool": spec.key, "ok": ok})
        return {"content": [{"type": "text", "text": text}], "is_error": not ok}

    def tools(self) -> list[Any]:
        """The tools as the Agent SDK's in-process MCP server takes them."""

        def made(spec: Spec) -> Any:
            async def call(given: dict[str, Any]) -> dict[str, Any]:
                return await self._call(spec, given)

            return self._sdk.tool(spec.name, spec.description, spec.schema)(call)

        return [made(spec) for spec in self.specs.values()]
