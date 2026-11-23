"""A bot's run as one Claude Agent SDK conversation turn, in this run's sandbox (docs/architecture.md, 5.10;
`function: "agent"`). The server sends the bot's prompt, the person's message, the server-side tools, the bot's
plugins and its limits; here Claude Code (the image's `claude`, driven by `claude_agent_sdk.ClaudeSDKClient`) works
the turn on the org's Anthropic key with exactly the tools of agent_tools.py and nothing else.

The lock: `tools=[]` (`["Skill"]` for a bot with skills) so no tool of Claude Code's exists, those tools named in
`disallowed_tools` too, `strict_mcp_config` and `setting_sources=[]` so nothing of the machine's is read, a
PreToolUse hook that denies whatever is not this run's own, and `permission_mode="default"` with `can_use_tool`.

A run that must ask the person stops: the turn is interrupted, the Result is `needs_input`, and the session's
transcript stays in the run's folder on this machine (agent_run.py). The answer (a `continue`, or a fresh `call`
with `resume`) carries the same session on; with no transcript left the turn starts again from `message`.

What goes back up: the status, the agent's final words (`output`), the tools it used (`steps`: names and whether
each worked), the files it named (`files`) and the tokens of the whole run so far. Never what a tool answered or
what is in a file."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import warnings
from typing import Any

import agent_plugins
from agent_run import STEPS, Kept, sweep
from agent_tools import FileTools, Gate, ScriptTools, Server, ServerTools
from file_agent import GUIDE

from rrotor import Ctx, ProgramError, Result

KEY = "ANTHROPIC_API_KEY"
SECONDS, SECONDS_MAX = 300, 1500  # a go's time when the call names none; the most it may name (the manifest's 1800)
GRACE = 10.0  # how long an interrupted turn is given to report what it used
EFFORTS = ("low", "medium", "high", "xhigh", "max")
INPUT_KINDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
OFF = ("GLYPHH_", "RROTOR_")  # nothing of the runtime's own reaches Claude Code's environment
# Claude Code's own tools: none exists in the session (`tools`), and each is refused by name as well.
BUILT_IN = (
    "Agent",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "Edit",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillBash",
    "KillShell",
    "LS",
    "ListMcpResourcesTool",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "Read",
    "ReadMcpResourceTool",
    "SlashCommand",
    "Task",
    "TaskOutput",
    "TaskStop",
    "TodoRead",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Write",
)


class AgentSdkMissing(ProgramError):
    code = "E_SDK"


def agent_sdk() -> Any:
    """The Claude Agent SDK (the image's, image/sdk.lock)."""
    try:
        import claude_agent_sdk
    except ImportError as err:
        raise AgentSdkMissing(f"This rrotor cannot run an agent bot: it has no Claude Agent SDK ({err}).") from None
    return claude_agent_sdk


def claude_path() -> str:
    found = shutil.which("claude")
    if found is None:
        raise ProgramError("Claude Code (claude) is not on this rrotor.")
    return found


def make_client(sdk: Any, options: Any) -> Any:
    """The SDK's client for one turn (a test sets a fake)."""
    return sdk.ClaudeSDKClient(options)


def env_of(kept: Kept, key: str) -> dict[str, str]:
    """Claude Code's environment beside the sandbox's own: its config in the run's folder, the key the run is billed
    on, no telemetry, no update, and none of the runtime's variables."""
    return {k: "" for k in os.environ if k.startswith(OFF)} | {
        "HOME": str(kept.folder),
        "CLAUDE_CONFIG_DIR": str(kept.config),
        KEY: key,
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "NODE_USE_ENV_PROXY": "1",
    }


def limits_of(inputs: dict[str, Any]) -> dict[str, int]:
    """The call's limits, as whole numbers above zero: `turns`, `seconds`, `tokens` (one not named is not held)."""
    given = inputs.get("limits") or {}
    return {k: int(v) for k in ("turns", "seconds", "tokens") if isinstance(v := given.get(k), int | float) and v > 0}


def kinds_of(ctx: Ctx, inputs: dict[str, Any], server: Server, kept: Kept) -> tuple[list[Any], FileTools | None]:
    """Whose tools the turn has: this rrotor's own first (the files of the folders given, a plugin's scripts), then
    the server's."""
    plugins = inputs.get("plugins") or []
    files = None
    if inputs.get("file_tools") is True and ctx.folders:
        files = FileTools(server, ctx.folders, kept.files, kept.read_bytes)
    scripts = ScriptTools(server, agent_plugins.declared(kept.plugins, plugins), kept.work)
    own = [k for k in (files, scripts) if k is not None]
    return [*own, ServerTools(server, inputs.get("tools") or [])], files


def system_of(inputs: dict[str, Any], files: FileTools | None) -> str:
    """The bot's own prompt, and what its folders are when it has any."""
    if files is None:
        return str(inputs["system"])
    given = "\n".join(
        f"- {f['path']} ({'read and change' if f['access'] == 'write' else 'read only'})" for f in files.files.folders()
    )
    return str(inputs["system"]) + GUIDE.format(folders=given)


def options_of(sdk: Any, inputs: dict[str, Any], kept: Kept, gate: Gate, key: str, files: FileTools | None) -> Any:
    """The turn's options: the bot's prompt, model and limits, its tools as the one MCP server `bot`, its plugins
    and skills, and the lock (this module's docstring)."""
    skills = [str(s) for s in inputs.get("skills") or []]
    plugins = agent_plugins.write(kept.plugins, inputs.get("plugins") or [])
    effort = inputs.get("effort")
    return sdk.ClaudeAgentOptions(
        system_prompt=system_of(inputs, files),
        tools=["Skill"] if skills else [],
        allowed_tools=gate.allowed(),
        disallowed_tools=list(BUILT_IN),
        mcp_servers={"bot": sdk.create_sdk_mcp_server("bot", tools=gate.tools())},
        strict_mcp_config=True,
        setting_sources=[],
        permission_mode="default",
        can_use_tool=gate.can_use,
        hooks={"PreToolUse": [sdk.HookMatcher(hooks=[gate.wall])]},
        skills=skills or None,
        plugins=[{"type": "local", "path": str(p)} for p in plugins],
        model=str(inputs["model"]) if inputs.get("model") else None,
        effort=effort if effort in EFFORTS else None,
        max_turns=limits_of(inputs).get("turns"),
        cwd=str(kept.work),
        cli_path=claude_path(),
        env=env_of(kept, key),
        resume=kept.session,
        stderr=lambda line: None,
    )


class Heard:
    """What a turn's messages said: the session's id, the result at its end, the agent's last words."""

    def __init__(self, sdk: Any, session: str | None) -> None:
        self._sdk, self.session = sdk, session
        self.result: Any = None
        self.text = ""

    def note(self, got: Any) -> None:
        if isinstance(got, self._sdk.SystemMessage) and got.subtype == "init":
            self.session = got.data.get("session_id") or self.session
        elif isinstance(got, self._sdk.AssistantMessage):
            said = "".join(b.text for b in got.content if isinstance(b, self._sdk.TextBlock)).strip()
            self.text = said or self.text
        elif isinstance(got, self._sdk.ResultMessage):
            self.result, self.session = got, got.session_id or self.session


async def turn(sdk: Any, options: Any, message: str, seconds: float, gate: Gate, heard: Heard) -> bool:
    """One turn: the message sent, every message read until the turn's result. True when it ran out of time (the
    turn interrupted, and given GRACE seconds to say what it used)."""

    async def read(client: Any) -> None:
        async for got in client.receive_response():
            heard.note(got)
            if gate.interrupt:
                gate.interrupt = False
                await client.interrupt()

    async with make_client(sdk, options) as client:
        await client.query(message)
        try:
            async with asyncio.timeout(seconds):
                await read(client)
        except TimeoutError:
            with contextlib.suppress(TimeoutError, sdk.ClaudeSDKError):
                async with asyncio.timeout(GRACE):
                    await client.interrupt()
                    await read(client)
            return True
    return False


def spent(kept: Kept, result: Any) -> None:
    """The go's tokens added to the run's: input (new, written to the cache and read from it, together), output."""
    usage = getattr(result, "usage", None) or {}

    def count(kind: str) -> int:
        n = usage.get(kind)
        return int(n) if isinstance(n, int | float) and n > 0 else 0

    kept.tokens = {
        "input_tokens": kept.tokens["input_tokens"] + sum(count(kind) for kind in INPUT_KINDS),
        "output_tokens": kept.tokens["output_tokens"] + count("output_tokens"),
    }


def over(kept: Kept, limits: dict[str, int]) -> str | None:
    used = kept.tokens["input_tokens"] + kept.tokens["output_tokens"]
    if "tokens" in limits and used > limits["tokens"]:
        return f"The run used {used:,} tokens: more than the {limits['tokens']:,} a run of this bot may use."
    return None


def left(kept: Kept, status: str, prompt: str | None = None, **more: Any) -> dict[str, Any]:
    """A go's Result as JSON (it is a checkpoint): the folder kept while the run waits, dropped when it ended."""
    data = {"output": "", "steps": kept.steps[-STEPS:], "files": kept.files, "tokens": kept.tokens, **more}
    if status == "needs_input":
        kept.save()
    else:
        kept.drop()
    return {"status": status, "step": None if status == "done" else "agent", "prompt": prompt, "data": data}


def why(result: Any, late: bool, limits: dict[str, int], seconds: int) -> str | None:
    """Why the go failed, in a plain sentence; None when it did not."""
    if late:
        return f"The bot ran out of time: a run may take {seconds} seconds."
    if result is None:
        return "Claude Code ended before it answered."
    if result.subtype == "error_max_turns":
        return f"The bot took more than the {limits.get('turns', result.num_turns)} turns a run may take."
    if result.is_error or result.subtype != "success":
        said = result.result or "; ".join(result.errors or []) or result.subtype
        return f"Claude failed: {str(said)[:300]}"
    return None


def go(ctx: Ctx, inputs: dict[str, Any], message: str, resume: bool) -> dict[str, Any]:
    """One go of the run: the turn from `message`, or (`resume`, and the transcript still here) the session that
    waited carried on with it."""
    sdk = agent_sdk()
    key = ctx.secrets.get(KEY)
    if not key:
        raise ProgramError(
            f"glyphh_bot runs a bot's agent on the org's Anthropic API key: set {KEY} on it "
            "(PUT /v1/programs/glyphh_bot/secrets/ANTHROPIC_API_KEY or secret_set)."
        )
    kept = Kept(ctx.files, str(inputs["tool_url"]))
    if not (resume and kept.load()):
        kept.start()
        message = str(inputs["message"])
    kept.open()
    limits = limits_of(inputs)
    if said := over(kept, limits):
        return left(kept, "failed", reason=said)
    server = Server(ctx.http, str(inputs["tool_url"]), str(inputs["tool_token"]))
    kinds, files = kinds_of(ctx, inputs, server, kept)
    gate = Gate(sdk, kinds, kept.steps, skills=inputs.get("skills") or [])
    heard = Heard(sdk, kept.session)
    seconds = min(limits.get("seconds", SECONDS), SECONDS_MAX)
    try:
        with warnings.catch_warnings():
            # the read-only tools are in allowed_tools on purpose: they never ask (the SDK warns that they skip
            # can_use_tool); the hook still stands in front of them
            warnings.simplefilter("ignore", sdk.CanUseToolShadowedWarning)
            options = options_of(sdk, inputs, kept, gate, key, files)
            late = asyncio.run(turn(sdk, options, message, seconds, gate, heard))
    except sdk.ClaudeSDKError as err:
        return left(kept, "failed", reason=f"Claude Code failed: {type(err).__name__}: {str(err)[:300]}")
    kept.session = heard.session
    spent(kept, heard.result)
    if files is not None:
        kept.files, kept.read_bytes = files.named(), files.files.read_bytes
    if said := over(kept, limits):
        return left(kept, "failed", reason=said)
    if gate.asked is not None and not late:
        ask = gate.asked
        return left(kept, "needs_input", ask.prompt, options=ask.options, **ask.detail)
    if said := why(heard.result, late, limits, seconds):
        return left(kept, "failed", reason=said)
    return left(kept, "done", output=str(heard.result.result or heard.text).strip())


def words(said: Any) -> str:
    """A continue's answer as the message the session carries on with: the server's own words, or a yes or a no."""
    if isinstance(said, dict) and set(said) == {"approved"}:
        return "The person said yes." if said["approved"] is True else "The person said no."
    return said if isinstance(said, str) else str(said)


def run(ctx: Ctx, inputs: dict[str, Any]) -> Result:
    """The bot's turn, and each question it stops at answered in this run: `needs_input` until the turn ends `done`
    or `failed`. Each go is a checkpoint, so a resumed run never runs one twice."""
    sweep(ctx.files)
    now: dict[str, Any] = ctx.checkpoint(
        "go-0", lambda: go(ctx, inputs, str(inputs["message"]), inputs.get("resume") is True)
    )
    n = 0
    while now["status"] == "needs_input":
        data = now["data"]
        said = ctx.needs_input(str(now["prompt"]), options=data.get("options"), key=f"go-{n}", detail=data)
        n += 1
        now = ctx.checkpoint(f"go-{n}", lambda said=said: go(ctx, inputs, words(said), True))
    data = now["data"]
    if now["status"] == "failed":
        return Result.failed("agent", str(data["reason"]), data)
    return Result.done(data)
