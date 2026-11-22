"""glyphh_bot: one of the server's hosted bots, run on this rrotor (docs/architecture.md, 5.10). The server holds the
bot (its package and its runner's state) and sends `call` over the link; this program runs the Glyphh SDK's runner
(glyphh.bots) here: its agent steps on Claude (the API on the org's own key from this rrotor's vault, or Claude Code
signed in here), its gates in Ada with the bot's own key, its tools at the server (HttpProgram: the server calls the
bot's connections and fetches for it). Each question the bot asks is the run's `needs_input`; the server's
`continue` carries the answer and the bot goes on in the same run. What the run leaves goes back as the Result: its
state among it, for the server to keep. `effort` is how hard Claude works on the agent steps (the Claude API's
`output_config.effort`). The call may carry the machine's folders the bot's owner chose (the link's `folders`,
rrotord/folders.py): rrotor mounts exactly those in this run's sandbox, and `ctx.folders` names them.

    run(ctx, {package, state, given, answer, ada_base, ada_key, tool_url, tool_token, model, claude, effort})
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_api import Messages

from rrotor import CliError, Ctx, ProgramError, Result

NEEDS = (3, 12)  # the Glyphh SDK this program needs: glyphh.bots.remote, HttpProgram, RunResult.to_result
KEY = "ANTHROPIC_API_KEY"  # the org's Anthropic API key, a secret of this program (optional: claude "cli" needs none)


class SdkMissing(ProgramError):
    code = "E_SDK"


def sdk() -> Any:
    """The Glyphh SDK's bots (the image's, image/sdk.lock), refused when it is older than this program needs."""
    try:
        import glyphh
        from glyphh.bots import remote
    except ImportError as err:
        raise SdkMissing(f"The Glyphh SDK on this rrotor cannot run a remote bot: {err}") from None
    have = tuple(int(p) for p in glyphh.__version__.split(".")[:2])
    if have < NEEDS:
        raise SdkMissing(f"The Glyphh SDK on this rrotor is {glyphh.__version__}; glyphh_bot needs 3.12 or later.")
    return remote


def make_ada(base: str, key: str) -> Any:
    """The bot's Ada, by its own key (a test sets a fake)."""
    from glyphh import Ada

    return Ada(base, key)


def claude_of(ctx: Ctx, inputs: dict[str, Any]) -> Any:
    """The bot's Claude: the API on the org's key from the vault (`api`, the default), or Claude Code signed in on
    this rrotor (`cli`: `claude -p`, by ctx.cli)."""
    from glyphh.bots import ApiClaude, BotError, Claude

    model = str(inputs.get("model") or "opus")
    if inputs.get("claude") == "cli":

        def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            try:
                got = ctx.cli("claude", argv[1:])
            except CliError as err:
                raise BotError(f"claude: {err.message} ({err.code})") from None
            return subprocess.CompletedProcess(argv, got.exit_code, got.stdout, got.stderr)

        return Claude(command="claude", run=run)
    key = ctx.secrets.get(KEY)
    if not key:
        raise ProgramError(
            f"glyphh_bot runs its agent steps on the org's Anthropic API key: set {KEY} on it "
            "(PUT /v1/programs/glyphh_bot/secrets/ANTHROPIC_API_KEY or secret_set), or call it with "
            '"claude": "cli" to use Claude Code signed in here.'
        )
    return ApiClaude(model=model, client=Effort(Messages(ctx.http, key), inputs.get("effort")))


# The Claude models that take `output_config.effort`, and the levels each takes (the Claude API's own): a model not
# here is asked without it, and so is a level it does not take (a run never fails over its effort).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT_MODELS = {"claude-opus-5-5": EFFORTS, "claude-sonnet-5-5": EFFORTS, "claude-haiku-5-5": EFFORTS}


class Effort:
    """The Claude client ApiClaude calls, each request carrying the bot's effort (`output_config: {effort}`) when
    its model takes it; whatever the SDK's ApiClaude is (one older than 3.14 knows no effort)."""

    def __init__(self, client: Any, effort: Any) -> None:
        self._client, self._effort = client, effort
        self.messages = self

    def stream(self, **kw: Any) -> Any:
        if "output_config" not in kw and self._effort in EFFORT_MODELS.get(str(kw.get("model")), ()):
            kw["output_config"] = {"effort": self._effort}
        return self._client.messages.stream(**kw)


def tool_post(ctx: Ctx) -> Any:
    """How HttpProgram reaches the server's tool URL: through ctx.http (the run's egress proxy)."""
    from glyphh.bots import ToolError

    def post(req: Any, timeout: float) -> Any:
        res = ctx.http.request(
            req.get_method(), req.full_url, data=req.data, headers=dict(req.header_items()), timeout=timeout
        )
        try:
            body = res.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and (res.ok or body.get("error")):
            return body
        raise ToolError(f"the server answered {res.status}")

    return post


def answer_of(said: Any) -> Any:
    """A continue's answer as the bot takes it: an approval's {"approved": bool} is its yes or no."""
    if isinstance(said, dict) and set(said) == {"approved"}:
        return "yes" if said["approved"] is True else "no"
    return said


def leg(ctx: Ctx, inputs: dict[str, Any], state: str | None, answer: Any, spent: dict[str, int]) -> dict[str, Any]:
    """One go of the bot's runner: from its first step (no answer), or the waiting run carried on with `answer`.
    Answers its Result as JSON (RunResult.to_result), its tokens counted with what this run spent before."""
    remote = sdk()
    from glyphh.bots import BotError, HttpProgram

    try:
        program = HttpProgram(str(inputs["tool_url"]), str(inputs["tool_token"]), post=tool_post(ctx))
        ran = remote.go(
            inputs["package"],
            ada=make_ada(str(inputs["ada_base"]), str(inputs["ada_key"])),
            claude=claude_of(ctx, inputs),
            program=program,
            state=state,
            given=inputs.get("given"),
            answer=answer,
        )
    except BotError as err:
        return {
            "status": "failed",
            "step": "package",
            "prompt": None,
            "data": {"reason": err.message, "problems": err.problems, "state": state, "tokens": spent},
        }
    used = ran.tokens or {}
    ran.tokens = {k: spent.get(k, 0) + int(used.get(k, 0)) for k in ("input_tokens", "output_tokens")}
    out: dict[str, Any] = ran.to_result()
    return out


def run(ctx: Ctx, inputs: dict[str, Any]) -> Result:
    """The bot's go, and each question it asks answered in this run: `needs_input` (an approval too, answered yes or
    no, or {"approved": bool}) with the whole run in its data, its state among it; at the end `done` or `failed`, the
    same. `answer` given: the run that waited (in an earlier call, now expired) carried on from `state`."""
    zero = {"input_tokens": 0, "output_tokens": 0}
    first = inputs.get("answer")
    left: dict[str, Any] = ctx.checkpoint(
        "leg-0", lambda: leg(ctx, inputs, inputs.get("state"), None if first is None else answer_of(first), zero)
    )
    n = 0
    while left["status"] in ("needs_input", "needs_approval"):
        data = left["data"]
        said = ctx.needs_input(str(left["prompt"] or ""), options=data.get("options"), key=f"leg-{n}", detail=data)
        n += 1
        before = left

        def carry(before: dict[str, Any] = before, said: Any = said) -> dict[str, Any]:
            got = before["data"]
            return leg(ctx, inputs, got.get("state"), answer_of(said), got.get("tokens") or zero)

        left = ctx.checkpoint(f"leg-{n}", carry)
    data = left["data"]
    if left["status"] == "failed":
        return Result.failed(str(left.get("step") or "bot"), str(data.get("reason") or "the bot failed"), data)
    return Result.done(data)
