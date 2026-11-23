"""glyphh_bot's `agent` (agent_loop.py): a bot's run as one Claude Agent SDK turn. The Agent SDK is real up to its
client: `ClaudeSDKClient` is faked (`Cli`, no Claude Code and no network), and plays Claude Code's part the way
Claude Code does it: every tool call goes through the options' PreToolUse hook, then `allowed_tools` or
`can_use_tool`, then the options' own in-process MCP server (the SDK's bridge, as the CLI's control channel uses it).
The server's tool address is faked; the folders and the run's folder are real. Needs `claude-agent-sdk` importable
(CI installs it beside rrotor)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import claude_agent_sdk as sdk
import pytest
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

from rrotor.http import Response
from rrotor.testing import FakeHttp, Harness

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = _load("glyphh_bot_test_program_for_agent", HERE / "test_program.py")
TOOL, KEY = base.TOOL, base.KEY
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
SECRET = "the invoice total is 4711 euros"
USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 2, "output_tokens": 5}
TOOLS = [
    {"name": "gmail-find-email", "description": "Find email", "input_schema": {"type": "object"}, "read_only": True},
    {
        "name": "slack-send-message",
        "description": "Send a Slack message",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "read_only": False,
    },
]
FIND, SEND = "mcp__bot__gmail_find_email", "mcp__bot__slack_send_message"


class Server:
    """The server's tool address: each call kept, answered by tool name (a list: one answer a call, in order)."""

    def __init__(self, **answers: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.auth: list[str] = []
        self.answers: dict[str, Any] = {"gmail-find-email": {"result": [{"id": "m1", "subject": SECRET}]}, **answers}

    def __call__(self, method: str, url: str, body: bytes | None) -> Response:
        asked = json.loads(body or b"{}")
        self.calls.append(asked)
        answer = self.answers[asked["tool"]]
        if isinstance(answer, list):
            answer = answer.pop(0)
        return answer if isinstance(answer, Response) else Response(200, json.dumps(answer).encode())

    def asked(self, tool: str) -> list[Any]:
        return [c["input"] for c in self.calls if c["tool"] == tool]


def result(text: str | None = "Done.", **over: Any) -> sdk.ResultMessage:
    given: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": SID,
        "usage": dict(USAGE),
        "result": text,
    }
    return sdk.ResultMessage(**{**given, **over})


def says(text: str) -> sdk.AssistantMessage:
    return sdk.AssistantMessage(content=[sdk.TextBlock(text)], model="claude-sonnet-5-5")


INIT = sdk.SystemMessage(subtype="init", data={"session_id": SID})
Script = Callable[["Cli"], AsyncIterator[Any]]


class Cli:
    """`ClaudeSDKClient` for one turn: `script(cli)` yields the turn's messages and uses tools with `cli.use`."""

    def __init__(self, options: Any, script: Script) -> None:
        self.options, self.script = options, script
        self.bridge = SdkMcpBridge("bot", options.mcp_servers["bot"]["instance"])
        self.sent: list[str] = []
        self.interrupts = 0
        self.n = 0

    async def __aenter__(self) -> Cli:
        hello = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "cli", "version": "1"}}
        await self._rpc("initialize", hello)
        await self.bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.bridge.aclose()

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.n += 1
        ask = {"jsonrpc": "2.0", "id": self.n, "method": method, **({"params": params} if params else {})}
        return (await self.bridge.handle(ask))["result"]

    async def query(self, message: str) -> None:
        self.sent.append(message)
        # Claude Code keeps the session's transcript in its config folder, under the working folder's name
        kept = Path(self.options.env["CLAUDE_CONFIG_DIR"]) / "projects" / "-work"
        kept.mkdir(parents=True, exist_ok=True)
        (kept / f"{SID}.jsonl").write_text("{}\n")

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def receive_response(self) -> AsyncIterator[Any]:
        async for message in self.script(self):
            yield message

    async def listed(self) -> list[str]:
        return [t["name"] for t in (await self._rpc("tools/list"))["tools"]]

    async def use(self, name: str, given: dict[str, Any]) -> dict[str, Any]:
        """One tool call as Claude Code makes it: `{denied, interrupt}` or `{text, error}`."""
        hook = self.options.hooks["PreToolUse"][0].hooks[0]
        said = await hook({"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": given}, "tu", {})
        if said:
            return {"denied": said["hookSpecificOutput"]["permissionDecisionReason"], "interrupt": False}
        if name not in self.options.allowed_tools:
            may = await self.options.can_use_tool(name, given, sdk.ToolPermissionContext(tool_use_id="tu"))
            if isinstance(may, sdk.PermissionResultDeny):
                return {"denied": may.message, "interrupt": may.interrupt}
            assert may.updated_input == given
        got = await self._rpc("tools/call", {"name": name.removeprefix("mcp__bot__"), "arguments": given})
        return {"text": got["content"][0]["text"], "error": got.get("isError", False)}


class Claude:
    """The turns of a test, one script a go; the clients made (their options, what was sent) are kept."""

    def __init__(self, *scripts: Script) -> None:
        self.scripts, self.clis = list(scripts), []

    def __call__(self, _sdk: Any, options: Any) -> Cli:
        self.clis.append(Cli(options, self.scripts.pop(0)))
        return self.clis[-1]

    @property
    def options(self) -> Any:
        return self.clis[-1].options


async def hello(cli: Cli) -> AsyncIterator[Any]:
    yield INIT
    yield sdk.UserMessage(content="Hi there")  # a message that says nothing the run keeps
    yield says("Hi! I triage your inbox.")
    yield result("Hi! I triage your inbox.")


def inputs(**over: Any) -> dict[str, Any]:
    given = {
        "system": "You are Inbox Triage.",
        "message": "Hi there",
        "tools": TOOLS,
        "model": "sonnet",
        "effort": "low",
        "limits": {"turns": 25, "seconds": 300, "tokens": 400000},
        "tool_url": TOOL,
        "tool_token": "tok_run",
        "claude": "api",
    }
    return {**given, **over}


@pytest.fixture
def loop(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    base.load()
    module = sys.modules["agent_loop"]
    monkeypatch.setattr(module, "claude_path", lambda: "/usr/local/bin/claude")
    return module


def harness(tmp_path: Path, server: Server, **kw: Any) -> Harness:
    kw.setdefault("secrets", {"ANTHROPIC_API_KEY": KEY})
    http = FakeHttp({TOOL: server})
    return Harness(base.load(), tmp_path / "files", http=http, declared=["ANTHROPIC_API_KEY"], **kw)


def start(loop: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *scripts: Script, **kw: Any) -> Any:
    """A harness, its server and its Claude, the scripts one a go."""
    server, claude = kw.pop("server", None) or Server(), Claude(*scripts)
    monkeypatch.setattr(loop, "make_client", claude)
    (tmp_path / "files").mkdir(exist_ok=True)
    return harness(tmp_path, server, **kw), server, claude


def run_folder(tmp_path: Path) -> Path:
    return sys.modules["agent_run"].Kept(tmp_path / "files", TOOL).folder


# the turn


def test_hello_is_answered_and_nothing_runs(loop, monkeypatch, tmp_path):
    h, server, claude = start(loop, monkeypatch, tmp_path, hello)
    done = h.call("agent", inputs())
    assert done.status == "done" and done.data["output"] == "Hi! I triage your inbox."
    assert done.data["steps"] == [] and done.data["files"] == []
    assert done.data["tokens"] == {"input_tokens": 15, "output_tokens": 5}  # cache writes and reads are input
    assert server.calls == [] and claude.clis[0].sent == ["Hi there"]
    assert not run_folder(tmp_path).exists() and h.ended  # nothing of the run is left on the machine


def test_the_options_carry_no_tool_but_the_runs_own(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def script(cli: Cli) -> AsyncIterator[Any]:
        seen["listed"] = await cli.listed()
        seen["bash"] = await cli.use("Bash", {"command": "ls /"})
        seen["other"] = await cli.use("mcp__other__thing", {})
        seen["skill"] = await cli.use("Skill", {"skill": "x"})
        seen["asked"] = await cli.options.can_use_tool("Read", {"file_path": "/etc/passwd"}, None)
        seen["skill_asked"] = await cli.options.can_use_tool("Skill", {}, None)
        yield result()

    monkeypatch.setenv("GLYPHH_API_KEY", "gk_secret")
    monkeypatch.setenv("RROTOR_DATA", "/data")
    h, _, claude = start(loop, monkeypatch, tmp_path, script)
    assert h.call("agent", inputs()).status == "done"
    o = claude.options
    assert o.tools == [] and o.skills is None and o.plugins == []  # no tool of Claude Code's exists
    assert {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "Agent"} <= set(
        o.disallowed_tools
    )
    assert o.strict_mcp_config is True and o.setting_sources == [] and o.permission_mode == "default"
    assert o.can_use_tool is not None and list(o.mcp_servers) == ["bot"]
    assert o.allowed_tools == [FIND]  # only the read-only tool never asks
    assert all(t.startswith("mcp__bot__") for t in o.allowed_tools)
    assert seen["listed"] == ["gmail_find_email", "slack_send_message"]
    for name in ("bash", "other", "skill"):
        assert "is not one of this bot's tools" in seen[name]["denied"]
    assert isinstance(seen["asked"], sdk.PermissionResultDeny) and not seen["asked"].interrupt
    assert isinstance(seen["skill_asked"], sdk.PermissionResultDeny)
    assert o.system_prompt == "You are Inbox Triage." and o.model == "sonnet" and o.effort == "low"
    assert o.max_turns == 25 and o.resume is None and o.cli_path == "/usr/local/bin/claude"
    folder = run_folder(tmp_path)
    assert o.cwd == str(folder / "work") and o.env["CLAUDE_CONFIG_DIR"] == str(folder / "config")
    assert o.env["ANTHROPIC_API_KEY"] == KEY and o.env["GLYPHH_API_KEY"] == "" and o.env["RROTOR_DATA"] == ""
    assert o.env["DISABLE_AUTOUPDATER"] == "1" and o.env["DISABLE_TELEMETRY"] == "1"
    o.stderr("noise")  # what Claude Code writes to stderr goes nowhere


def test_a_bot_with_skills_gets_the_skill_tool_and_its_plugins(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def script(cli: Cli) -> AsyncIterator[Any]:
        hook = cli.options.hooks["PreToolUse"][0].hooks[0]
        seen["skill"] = await hook({"tool_name": "Skill", "tool_input": {"skill": "notes:slack-note"}}, "tu", {})
        seen["short"] = await hook({"tool_name": "Skill", "tool_input": {"skill": "/slack-note"}}, "tu", {})
        seen["asked"] = await cli.options.can_use_tool("Skill", {"skill": "notes:slack-note"}, None)
        # a skill that is not the bot's own (Claude Code brings some) does not pass, nor a call that names none
        seen["theirs"] = await cli.use("Skill", {"skill": "deep-research"})
        seen["none"] = await hook({"tool_name": "Skill"}, "tu", {})
        seen["theirs_asked"] = await cli.options.can_use_tool("Skill", {"skill": "deep-research"}, None)
        seen["files"] = sorted(
            str(p.relative_to(cli.options.plugins[0]["path"]))
            for p in Path(cli.options.plugins[0]["path"]).rglob("*")
            if p.is_file()
        )
        seen["listed"] = await cli.listed()
        yield result()

    plugin = {"name": "notes", "files": {"skills/slack-note/SKILL.md": "---\nname: slack-note\n---\nPINEAPPLE"}}
    h, _, claude = start(loop, monkeypatch, tmp_path, script)
    given = inputs(plugins=[plugin], skills=["notes:slack-note"], model=None, effort="none", limits={})
    assert h.call("agent", given).status == "done"
    o = claude.options
    assert o.tools == ["Skill"] and o.skills == ["notes:slack-note"] and "Skill" not in o.disallowed_tools
    assert o.plugins == [{"type": "local", "path": str(run_folder(tmp_path) / "plugins" / "notes")}]
    assert seen["files"] == [".claude-plugin/plugin.json", "skills/slack-note/SKILL.md"]
    assert seen["skill"] == {} and seen["short"] == {} and isinstance(seen["asked"], sdk.PermissionResultAllow)
    assert "is not one of this bot's tools" in seen["theirs"]["denied"] and seen["none"] != {}
    assert isinstance(seen["theirs_asked"], sdk.PermissionResultDeny)
    assert "run_script" not in seen["listed"]  # no plugin declares a script
    assert o.model is None and o.effort is None and o.max_turns is None


# asking, and carrying on


def test_an_action_asks_the_server_first_and_the_run_waits_then_carries_on(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def first(cli: Cli) -> AsyncIterator[Any]:
        yield INIT
        seen["found"] = await cli.use(FIND, {})
        seen["send"] = await cli.use(SEND, {"text": "PINEAPPLE"})
        seen["again"] = await cli.use(SEND, {"text": "other"})  # nothing more is asked while the run waits
        seen["read"] = await cli.use(FIND, {})
        yield result(None, subtype="error_during_execution", is_error=True, terminal_reason="aborted_tools")

    async def second(cli: Cli) -> AsyncIterator[Any]:
        yield INIT
        seen["sent"] = await cli.use(SEND, {"text": "PINEAPPLE"})
        yield says("Sent it.")
        yield result("")

    server = Server(
        **{"slack-send-message": [{"ask": True, "question": "Send a message in Slack?"}, {"result": {"ok": True}}]}
    )
    h, _, claude = start(loop, monkeypatch, tmp_path, first, second, server=server)
    asked = h.call("agent", inputs(message="Tell Slack what is urgent."))
    assert asked.status == "needs_input" and asked.prompt == "Send a message in Slack?"
    assert seen["found"] == {"text": json.dumps([{"id": "m1", "subject": SECRET}]), "error": False}
    assert seen["send"]["interrupt"] is True and seen["again"]["interrupt"] is True
    assert seen["read"]["error"] is True and "wait" in seen["read"]["text"]
    assert server.calls == [
        {"tool": "gmail-find-email", "input": {}},
        {"tool": "slack-send-message", "input": {"text": "PINEAPPLE"}},
    ]
    assert asked.data["steps"] == [{"tool": "gmail-find-email", "ok": True}] and asked.data["output"] == ""
    assert asked.data["tokens"] == {"input_tokens": 15, "output_tokens": 5}
    assert SECRET not in json.dumps(asked.data) and SECRET not in json.dumps(h.logs)
    folder = run_folder(tmp_path)
    assert json.loads((folder / "state.json").read_text())["session"] == SID and not h.ended

    done = h.resume("The person said yes to Send a message in Slack. Carry on.")
    assert done.status == "done" and done.data["output"] == "Sent it."  # the last words, when the result has none
    assert claude.clis[1].sent == ["The person said yes to Send a message in Slack. Carry on."]
    assert claude.options.resume == SID
    assert seen["sent"] == {"text": '{"ok": true}', "error": False}
    assert len(server.asked("slack-send-message")) == 2  # asked once in the gate, not again by the tool
    assert done.data["steps"] == [
        {"tool": "gmail-find-email", "ok": True},
        {"tool": "slack-send-message", "ok": True},
    ]
    assert done.data["tokens"] == {"input_tokens": 30, "output_tokens": 10}  # the whole run's
    assert not folder.exists() and h.ended


def test_a_fresh_call_with_resume_carries_on_from_the_transcript_on_disk(loop, monkeypatch, tmp_path):
    async def first(cli: Cli) -> AsyncIterator[Any]:
        yield INIT
        await cli.use(SEND, {"text": "a"})
        yield result(None, terminal_reason="aborted_tools")

    server = Server(**{"slack-send-message": [{"ask": True}, {"ask": True}]})
    h, _, claude = start(loop, monkeypatch, tmp_path, first, hello, hello, server=server)
    asked = h.call("agent", inputs())
    assert asked.status == "needs_input" and asked.prompt == "The bot is waiting for the person's answer."

    again = harness(tmp_path, server)  # the rrotor's run expired: the server calls afresh
    done = again.call("agent", inputs(message="The person said yes. Carry on.", resume=True))
    assert done.status == "done" and claude.options.resume == SID
    assert claude.clis[1].sent == ["The person said yes. Carry on."]
    assert done.data["tokens"] == {"input_tokens": 30, "output_tokens": 10}

    lost = harness(tmp_path, server)  # nothing of the run is here any more: the turn starts again
    done = lost.call("agent", inputs(message="The person said yes. Carry on.", resume=True))
    assert done.status == "done" and claude.options.resume is None
    assert done.data["tokens"] == {"input_tokens": 15, "output_tokens": 5}


def test_a_call_without_resume_starts_again_whatever_is_on_disk(loop, monkeypatch, tmp_path):
    async def first(cli: Cli) -> AsyncIterator[Any]:
        await cli.use(SEND, {"text": "a"})
        yield result(None)

    server = Server(**{"slack-send-message": [{"ask": True}]})
    h, _, claude = start(loop, monkeypatch, tmp_path, first, hello, server=server)
    assert h.call("agent", inputs()).status == "needs_input"
    done = harness(tmp_path, server).call("agent", inputs())
    assert done.status == "done" and claude.options.resume is None and done.data["tokens"]["input_tokens"] == 15


def test_a_run_whose_transcript_is_gone_starts_again_from_its_message(loop, monkeypatch, tmp_path):
    async def first(cli: Cli) -> AsyncIterator[Any]:
        await cli.use(SEND, {"text": "a"})
        yield result(None, session_id="")  # no session was named: nothing to carry on

    server = Server(**{"slack-send-message": [{"ask": True}]})
    h, _, claude = start(loop, monkeypatch, tmp_path, first, hello, server=server)
    assert h.call("agent", inputs()).status == "needs_input"
    done = h.resume({"approved": True})
    assert done.status == "done" and claude.clis[1].sent == ["Hi there"] and claude.options.resume is None


def test_an_answer_becomes_the_message_the_session_carries_on_with(loop):
    assert loop.words({"approved": True}) == "The person said yes."
    assert loop.words({"approved": False}) == "The person said no."
    assert loop.words("go on") == "go on" and loop.words(3) == "3"


def test_what_the_server_answers_a_tool(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def script(cli: Cli) -> AsyncIterator[Any]:
        for n in range(6):
            seen[n] = await cli.use(SEND, {"text": str(n)})
        seen["bad"] = await cli.use(SEND, {})  # the tool's own schema is checked
        yield result()

    answers = [
        {"result": "plain words"},
        {"error": "Slack said no"},
        Response(502, b"<html>"),
        Response(200, b"[1]"),
        Response(500, json.dumps({"detail": "x"}).encode()),
        {"neither": 1},
        {"result": "never given: the call's input was wrong"},
    ]
    h, server, _ = start(loop, monkeypatch, tmp_path, script, server=Server(**{"slack-send-message": answers}))
    done = h.call("agent", inputs())
    assert seen[0] == {"text": "plain words", "error": False}
    assert seen[1] == {"text": "Slack said no", "error": True}
    assert [seen[n]["text"] for n in (2, 3, 4, 5)] == [
        "The server answered 502.",
        "The server answered 200.",
        "The server answered 500.",
        "The server answered 200.",
    ]
    assert "required property" in seen["bad"]["text"] and seen["bad"]["error"] is True
    assert [s["ok"] for s in done.data["steps"]] == [True, False, False, False, False, False]
    assert all(c == {"tool": "slack-send-message", "input": c["input"]} for c in server.calls)


def test_a_server_out_of_reach_is_the_tools_error(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def script(cli: Cli) -> AsyncIterator[Any]:
        seen["found"] = await cli.use(FIND, {})
        yield result()

    h, _, _ = start(loop, monkeypatch, tmp_path, script)
    done = h.call("agent", inputs(tool_url="https://elsewhere.example.com/tool"))
    assert done.status == "done" and seen["found"]["error"] is True
    assert "The server could not be asked" in seen["found"]["text"]


def test_a_read_only_tool_the_server_asks_about_interrupts_the_turn(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def script(cli: Cli) -> AsyncIterator[Any]:
        seen["found"] = await cli.use(FIND, {})
        yield says("...")
        seen["interrupts"] = cli.interrupts
        yield result(None)

    server = Server(**{"gmail-find-email": {"ask": True, "question": "Read your Gmail?"}})
    h, _, _ = start(loop, monkeypatch, tmp_path, script, server=server)
    asked = h.call("agent", inputs())
    assert asked.status == "needs_input" and asked.prompt == "Read your Gmail?"
    assert seen["found"]["error"] is True and seen["interrupts"] == 1 and asked.data["steps"] == []


# the folders' files


def folders(tmp_path: Path) -> list[dict[str, str]]:
    docs, notes = tmp_path / "Docs", tmp_path / "Notes"
    docs.mkdir()
    notes.mkdir()
    (docs / "March.txt").write_text(SECRET)
    (docs / "Scan.pdf").write_bytes(b"%PDF-1.4 fake")
    return [{"path": str(docs), "access": "read"}, {"path": str(notes), "access": "write"}]


def test_the_folders_files_are_tools_and_a_change_waits_for_the_servers_word(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    given = folders(tmp_path)
    docs, notes = given[0]["path"], given[1]["path"]

    async def first(cli: Cli) -> AsyncIterator[Any]:
        yield INIT
        seen["listed"] = await cli.listed()
        seen["list"] = await cli.use("mcp__bot__list_folder", {})
        seen["read"] = await cli.use("mcp__bot__read_file", {"path": f"{docs}/March.txt"})
        seen["search"] = await cli.use("mcp__bot__search_files", {"query": "march"})
        seen["pdf"] = await cli.use("mcp__bot__read_file", {"path": f"{docs}/Scan.pdf"})
        seen["missing"] = await cli.use("mcp__bot__read_file", {"path": f"{docs}/Nope.txt"})
        seen["now"] = await cli.use("mcp__bot__write_file", {"path": f"{notes}/Now.md", "content": "at once"})
        seen["outside"] = await cli.use("mcp__bot__write_file", {"path": "/etc/passwd", "content": "x"})
        seen["readonly"] = await cli.use("mcp__bot__move_file", {"path": f"{docs}/March.txt", "to": f"{notes}/M.txt"})
        seen["refused"] = await cli.use("mcp__bot__write_file", {"path": f"{notes}/No.md", "content": "x"})
        seen["ask"] = await cli.use("mcp__bot__write_file", {"path": f"{notes}/Summary.md", "content": SECRET})
        yield result(None)

    async def second(cli: Cli) -> AsyncIterator[Any]:
        seen["written"] = await cli.use("mcp__bot__write_file", {"path": f"{notes}/Summary.md", "content": SECRET})
        seen["moved"] = await cli.use("mcp__bot__move_file", {"path": f"{notes}/Now.md", "to": f"{notes}/Then.md"})
        yield result("Filed.")

    changes = [
        {"result": {"allowed": True}},
        {"error": "the bot was paused"},
        {"result": {"allowed": False}},
        {"result": {"allowed": True}},
        {"result": {"allowed": True}},
    ]
    server = Server(file_change=changes)
    h, _, claude = start(loop, monkeypatch, tmp_path, first, second, server=server, folders=given)
    asked = h.call("agent", inputs(file_tools=True))
    assert seen["listed"][:5] == ["list_folder", "read_file", "search_files", "write_file", "move_file"]
    assert set(claude.clis[0].options.allowed_tools) == {
        "mcp__bot__list_folder",
        "mcp__bot__read_file",
        "mcp__bot__search_files",
        FIND,
    }
    assert "in these folders and nowhere else" in claude.clis[0].options.system_prompt
    assert f"- {notes} (read and change)" in claude.clis[0].options.system_prompt
    assert seen["read"] == {"text": SECRET, "error": False} and "March.txt" in seen["search"]["text"]
    assert seen["pdf"]["error"] is True and "PDF" in seen["pdf"]["text"]
    assert seen["missing"]["error"] is True
    assert seen["now"]["error"] is False and (Path(notes) / "Now.md").read_text() == "at once"
    assert "denied" in seen["outside"] and not seen["outside"]["interrupt"]
    assert "denied" in seen["readonly"] and not seen["readonly"]["interrupt"]
    assert seen["refused"] == {"denied": "The change was not made: the bot was paused", "interrupt": False}
    assert seen["ask"]["interrupt"] is True and not (Path(notes) / "Summary.md").exists()
    assert asked.status == "needs_input" and asked.prompt == "Write Notes/Summary.md?"
    assert asked.data["options"] == ["yes", "no"] and asked.data["approval"] is True
    assert asked.data["file"] == {"action": "write", "path": f"{notes}/Summary.md", "name": "Notes/Summary.md"}
    assert {"name": "Docs/March.txt", "did": "read"} in asked.data["files"]
    assert {"name": "Notes/Now.md", "did": "written"} in asked.data["files"]
    assert SECRET not in json.dumps(asked.data) and SECRET not in json.dumps(h.logs)
    assert server.asked("file_change")[0] == {"action": "write", "path": f"{notes}/Now.md", "name": "Notes/Now.md"}

    done = h.resume("The person said yes to Write Notes/Summary.md. Carry on.")
    assert done.status == "done" and (Path(notes) / "Summary.md").read_text() == SECRET
    assert seen["moved"]["error"] is False and (Path(notes) / "Then.md").exists()
    assert {"name": "Notes/Summary.md", "did": "written"} in done.data["files"]
    assert {"name": "Docs/March.txt", "did": "read"} in done.data["files"]  # the earlier go's, kept
    assert server.asked("file_change")[-1]["action"] == "move"
    assert SECRET not in json.dumps(done.data)


def test_a_change_the_server_stops_the_run_for_and_one_never_allowed(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    given = folders(tmp_path)
    notes = given[1]["path"]
    tools = sys.modules["agent_tools"]

    async def script(cli: Cli) -> AsyncIterator[Any]:
        gate = cli.options.can_use_tool.__self__
        kind = gate.specs["mcp__bot__write_file"].kind
        # a write the gate never allowed is not made, whoever calls the tool
        seen["bare"] = kind.run(gate.specs["mcp__bot__write_file"], {"path": f"{notes}/Bare.md", "content": "x"})
        seen["reads"] = kind.check(gate.specs["mcp__bot__read_file"], {"path": "x"})  # a read is never asked about
        monkeypatch.setattr(tools.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space")))
        seen["full"] = await cli.use("mcp__bot__write_file", {"path": f"{notes}/Full.md", "content": "x"})
        monkeypatch.undo()
        seen["ask"] = await cli.use("mcp__bot__write_file", {"path": f"{notes}/A.md", "content": "x"})
        yield result(None)

    server = Server(file_change=[{"result": {"allowed": True}}, {"ask": True, "question": "Write A.md?"}])
    claude = Claude(script)
    monkeypatch.setattr(loop, "make_client", claude)
    (tmp_path / "files").mkdir()
    h = harness(tmp_path, server, folders=given)
    asked = h.call("agent", inputs(file_tools=True, tools=[]))
    assert seen["bare"] == ("This change was not allowed: it was not made.", False) and seen["reads"] is None
    assert seen["full"]["error"] is True and "No space" in seen["full"]["text"]
    assert asked.status == "needs_input" and asked.prompt == "Write A.md?" and "approval" not in asked.data
    assert not (Path(notes) / "Bare.md").exists() and not (Path(notes) / "A.md").exists()


def test_without_folders_or_file_tools_there_are_no_file_tools(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def script(cli: Cli) -> AsyncIterator[Any]:
        seen.setdefault("listed", []).append(await cli.listed())
        yield result()

    given = folders(tmp_path)
    h, _, _ = start(loop, monkeypatch, tmp_path, script, script, folders=given)
    assert h.call("agent", inputs()).status == "done"  # an older server: no file_tools
    assert harness(tmp_path, Server()).call("agent", inputs(file_tools=True)).status == "done"  # no folders
    assert seen["listed"] == [["gmail_find_email", "slack_send_message"]] * 2


def test_the_runs_own_tools_keep_their_names(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    async def script(cli: Cli) -> AsyncIterator[Any]:
        seen["listed"] = await cli.listed()
        seen["read"] = await cli.use("mcp__bot__read_file", {"path": "/nowhere"})
        yield result()

    mine = [{"name": "read_file", "description": "The server's own", "read_only": True}, {"name": "x y/z"}]
    h, server, _ = start(loop, monkeypatch, tmp_path, script, folders=folders(tmp_path))
    assert h.call("agent", inputs(file_tools=True, tools=mine)).status == "done"
    assert seen["listed"].count("read_file") == 1 and "x_y_z" in seen["listed"] and server.calls == []


# a plugin's scripts

ECHO = "import sys\nprint('args', sys.argv[1:], 'stdin', sys.stdin.read())\n"
SCRIPTS = {
    "name": "tools",
    "files": {
        "skills/pdf/scripts/echo.py": ECHO,
        "skills/pdf/scripts/fail.sh": "echo oops >&2\nexit 3\n",
        "skills/pdf/scripts/slow.py": "import time\ntime.sleep(30)\n",
        "skills/pdf/scripts/tool.rb": "puts 1",
        "skills/pdf/scripts/app.js": "1",
        "skills/pdf/scripts/hidden.py": "print('never declared')",
    },
    "code": [
        {
            "skill": "pdf",
            "scripts": [
                "skills/pdf/scripts/echo.py",
                "skills/pdf/scripts/fail.sh",
                "skills/pdf/scripts/slow.py",
                "skills/pdf/scripts/tool.rb",
                "skills/pdf/scripts/app.js",
                "skills/pdf/scripts/gone.py",
            ],
        }
    ],
}
RUN = "mcp__bot__run_script"


def script_call(script: str, **more: Any) -> dict[str, Any]:
    return {"plugin": "tools", "script": f"skills/pdf/scripts/{script}", **more}


def test_run_script_runs_a_declared_script_and_nothing_else(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    tools = sys.modules["agent_tools"]

    async def script(cli: Cli) -> AsyncIterator[Any]:
        seen["listed"] = await cli.listed()
        seen["echo"] = await cli.use(RUN, script_call("echo.py", args=["a b", "c"], stdin="in"))
        seen["fail"] = await cli.use(RUN, script_call("fail.sh"))
        seen["hidden"] = await cli.use(RUN, script_call("hidden.py"))
        seen["gone"] = await cli.use(RUN, script_call("gone.py"))
        seen["shell"] = await cli.use(RUN, {"plugin": "tools", "script": "/bin/sh", "args": ["-c", "id"]})
        seen["ruby"] = await cli.use(RUN, script_call("tool.rb"))
        seen["many"] = await cli.use(RUN, script_call("echo.py", args=["x"] * 65))
        seen["big"] = await cli.use(RUN, script_call("echo.py", stdin="x" * (1024 * 1024 + 1)))
        seen["no"] = await cli.use(RUN, script_call("echo.py", args=["no"]))
        seen["refused"] = await cli.use(RUN, script_call("echo.py", args=["refused"]))
        gate = cli.options.can_use_tool.__self__
        spec = gate.specs[RUN]
        seen["bare"] = spec.kind.run(spec, script_call("echo.py"))  # never allowed: never run
        seen["bare_bad"] = spec.kind.run(spec, script_call("hidden.py"))
        monkeypatch.setitem(tools.RUNNERS, ".js", ["/nonexistent/node"])
        seen["node"] = await cli.use(RUN, script_call("app.js"))
        monkeypatch.setattr(tools, "SCRIPT_SECONDS", 1)
        seen["slow"] = await cli.use(RUN, script_call("slow.py"))
        seen["ask"] = await cli.use(RUN, script_call("echo.py", args=["ask"]))
        yield result(None)

    yes = {"result": {"allowed": True}}
    answers = [yes, yes, {"result": {"allowed": False}}, {"error": "Scripts are off for this bot."}, yes, yes]
    server = Server(run_script=[*answers, {"ask": True, "question": "Run echo.py?"}])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-a-script")
    h, _, claude = start(loop, monkeypatch, tmp_path, script, server=server)
    asked = h.call("agent", inputs(plugins=[SCRIPTS], tools=[]))
    assert seen["listed"] == ["run_script"] and RUN not in claude.options.allowed_tools
    said = json.loads(seen["echo"]["text"])
    assert said == {"exit_code": 0, "stdout": "args ['a b', 'c'] stdin in\n", "stderr": ""}
    assert json.loads(seen["fail"]["text"])["exit_code"] == 3 and seen["fail"]["error"] is True
    for name in ("hidden", "gone", "shell"):
        assert "only the scripts listed can be run" in seen[name]["denied"], name
    assert "runs .py, .sh, .js and .mjs" in seen["ruby"]["denied"]
    assert "at most 64 strings" in seen["many"]["denied"] and "too much" in seen["big"]["denied"]
    assert seen["no"]["denied"] == "The script may not be run."
    assert seen["refused"]["denied"] == "Scripts are off for this bot."
    assert seen["bare"] == ("The script may not be run.", False) and seen["bare_bad"][1] is False
    assert "could not be run" in seen["node"]["text"] and "did not finish within 1 seconds" in seen["slow"]["text"]
    assert asked.status == "needs_input" and asked.prompt == "Run echo.py?"
    # the server is told the script and its arguments, never its standard input
    assert server.asked("run_script")[0] == {
        "plugin": "tools",
        "script": "skills/pdf/scripts/echo.py",
        "args": ["a b", "c"],
    }
    assert [s for s in asked.data["steps"] if s["ok"]] == [{"tool": "run_script", "ok": True}]
    assert "stdin in" not in json.dumps(asked.data)


def test_a_script_gets_none_of_the_runs_secrets(loop, monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    files = {"env.py": "import os\nprint(sorted(os.environ))\n"}
    env = {"name": "tools", "files": files, "code": [{"scripts": ["env.py"]}]}

    async def script(cli: Cli) -> AsyncIterator[Any]:
        seen["env"] = await cli.use(RUN, {"plugin": "tools", "script": "env.py"})
        yield result()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-a-script")
    monkeypatch.setenv("GLYPHH_API_KEY", "gk")
    h, _, _ = start(loop, monkeypatch, tmp_path, script, server=Server(run_script={"result": {"allowed": True}}))
    assert h.call("agent", inputs(plugins=[env])).status == "done"
    said = json.loads(seen["env"]["text"])["stdout"]
    assert "HOME" in said and "PATH" in said and "ANTHROPIC_API_KEY" not in said and "GLYPHH_API_KEY" not in said


@pytest.mark.parametrize(
    "plugin",
    [
        {"name": "../up", "files": {}},
        {"name": "ok", "files": {"../../etc/passwd": "x"}},
        {"name": "ok", "files": {"/abs": "x"}},
        {"name": "ok", "files": {"": "x"}},
    ],
)
def test_a_plugin_that_would_write_outside_its_folder_is_refused(loop, monkeypatch, tmp_path, plugin):
    h, _, _ = start(loop, monkeypatch, tmp_path, hello)
    failed = h.call("agent", inputs(plugins=[plugin]))
    assert failed.status == "failed" and failed.data["code"] == "E_PLUGIN"
    assert not (tmp_path / "etc").exists() and not (tmp_path / "up").exists()


def test_a_plugins_file_given_as_an_object_is_written_as_json(tmp_path):
    plugins = sys.modules["agent_plugins"]
    files = {".claude-plugin/plugin.json": {"name": "a", "v": 1}}
    made = plugins.write(tmp_path / "p", [{"name": "a", "files": files}])
    assert json.loads((made[0] / ".claude-plugin" / "plugin.json").read_text()) == {"name": "a", "v": 1}


# limits and failures


def test_the_token_limit_fails_the_run_when_a_go_ends_and_before_it_carries_on(loop, monkeypatch, tmp_path):
    async def first(cli: Cli) -> AsyncIterator[Any]:
        await cli.use(SEND, {"text": "a"})
        yield result(None)

    server = Server(**{"slack-send-message": [{"ask": True}, {"ask": True}]})
    h, _, _ = start(loop, monkeypatch, tmp_path, first, first, server=server)
    over = h.call("agent", inputs(limits={"tokens": 19}))
    assert over.status == "failed" and "used 20 tokens: more than the 19" in over.data["reason"]
    assert over.step == "agent" and over.data["tokens"] == {"input_tokens": 15, "output_tokens": 5}
    assert not run_folder(tmp_path).exists()

    again = harness(tmp_path, server)
    assert again.call("agent", inputs(limits={"tokens": 20})).status == "needs_input"
    late = harness(tmp_path, server).call("agent", inputs(limits={"tokens": 19}, resume=True))
    assert late.status == "failed" and "used 20 tokens" in late.data["reason"]  # no turn was run for it


def test_a_turn_out_of_time_is_interrupted_and_fails(loop, monkeypatch, tmp_path):
    async def slow(cli: Cli) -> AsyncIterator[Any]:
        yield INIT
        if not cli.interrupts:
            await asyncio.sleep(30)
        yield result(None, terminal_reason="aborted_streaming")

    async def stuck(cli: Cli) -> AsyncIterator[Any]:
        await asyncio.sleep(30)
        yield result()

    monkeypatch.setattr(loop, "GRACE", 0.2)
    h, _, claude = start(loop, monkeypatch, tmp_path, slow, stuck)
    began = time.monotonic()
    late = h.call("agent", inputs(limits={"seconds": 1}))
    assert late.status == "failed" and late.data["reason"] == "The bot ran out of time: a run may take 1 seconds."
    assert claude.clis[0].interrupts == 1 and late.data["tokens"] == {"input_tokens": 15, "output_tokens": 5}
    never = harness(tmp_path, Server()).call("agent", inputs(limits={"seconds": 1}))
    assert never.status == "failed" and never.data["tokens"] == {"input_tokens": 0, "output_tokens": 0}
    assert time.monotonic() - began < 10


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (result(None, subtype="error_max_turns", is_error=True), "The bot took more than the 25 turns a run may take."),
        (result("Credit balance is too low", is_error=True), "Claude failed: Credit balance is too low"),
        (result(None, subtype="error_during_execution", errors=["boom", "bang"]), "Claude failed: boom; bang"),
        (result(None, subtype="error_during_execution", usage=None), "Claude failed: error_during_execution"),
        (None, "Claude Code ended before it answered."),
    ],
)
def test_a_turn_that_fails_says_why(loop, monkeypatch, tmp_path, message, reason):
    async def script(cli: Cli) -> AsyncIterator[Any]:
        yield INIT
        if message is not None:
            yield message

    h, _, _ = start(loop, monkeypatch, tmp_path, script)
    failed = h.call("agent", inputs())
    assert failed.status == "failed" and failed.step == "agent" and failed.data["reason"] == reason
    assert not run_folder(tmp_path).exists()


def test_claude_code_failing_to_run_fails_the_run(loop, monkeypatch, tmp_path):
    def script(cli: Cli) -> AsyncIterator[Any]:
        raise sdk.ProcessError("exit 1", exit_code=1)

    h, _, _ = start(loop, monkeypatch, tmp_path, script)
    failed = h.call("agent", inputs())
    assert failed.status == "failed" and failed.data["reason"].startswith("Claude Code failed: ProcessError")


def test_what_the_rrotor_lacks_fails_the_run_plainly(loop, monkeypatch, tmp_path):
    h, _, _ = start(loop, monkeypatch, tmp_path, hello, secrets={})
    nokey = h.call("agent", inputs())
    assert nokey.status == "failed" and "set ANTHROPIC_API_KEY" in nokey.data["reason"]

    monkeypatch.undo()
    monkeypatch.setattr(loop.shutil, "which", lambda name: None)
    with pytest.raises(loop.ProgramError, match="Claude Code \\(claude\\) is not on this rrotor"):
        loop.claude_path()
    monkeypatch.setattr(loop.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert loop.claude_path() == "/usr/local/bin/claude"

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    nosdk = harness(tmp_path, Server()).call("agent", inputs())
    assert nosdk.status == "failed" and nosdk.data["code"] == "E_SDK"
    monkeypatch.undo()
    assert loop.make_client(sdk, sdk.ClaudeAgentOptions()).__class__ is sdk.ClaudeSDKClient


# the run's folder


def test_a_state_that_cannot_be_read_is_no_state(tmp_path):
    kept_of = sys.modules["agent_run"].Kept
    kept = kept_of(tmp_path, TOOL)
    assert kept.load() is False
    kept.open()
    for state in ("not json", json.dumps({"session": "../../x"}), json.dumps({"session": SID})):
        (kept.folder / "state.json").write_text(state)
        assert kept.load() is False and kept.session is None
    transcript = kept.config / "projects" / "-w"
    transcript.mkdir(parents=True)
    (transcript / f"{SID}.jsonl").write_text("{}")
    assert kept.load() is False  # the transcript is there, the rest of the state is not
    assert kept_of(tmp_path, TOOL).folder == kept.folder != kept_of(tmp_path, TOOL + "2").folder


def test_runs_left_waiting_too_long_are_swept(tmp_path):
    runs = sys.modules["agent_run"]
    runs.sweep(tmp_path)  # nothing kept yet
    old, new = runs.Kept(tmp_path, "https://a"), runs.Kept(tmp_path, "https://b")
    for kept in (old, new):
        kept.open()
        kept.save()
    (tmp_path / runs.ROOT / "stray.txt").write_text("x")
    long_ago = time.time() - runs.STALE - 60
    os.utime(old.folder, (long_ago, long_ago))
    runs.sweep(tmp_path)
    assert not old.folder.exists() and new.folder.exists()
    runs.sweep(tmp_path, now=time.time() + runs.STALE + 60)
    assert not new.folder.exists()


def test_the_manifest_takes_what_the_server_sends():
    import jsonschema

    manifest = json.loads((HERE.parent / "glyphh.json").read_text())
    schema = manifest["functions"]["agent"]["inputs"]
    full = inputs(plugins=[SCRIPTS], skills=["tools:pdf"], resume=True, file_tools=True)
    jsonschema.validate(full, schema)
    jsonschema.validate(inputs(model=None, effort=None), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(inputs(claude="cli"), schema)
    assert set(manifest["functions"]) == {"run", "agent"} and "api.anthropic.com" in manifest["hosts"]
