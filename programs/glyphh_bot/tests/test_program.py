"""glyphh_bot through rrotor.testing: a hosted bot run here, its questions waiting as needs_input and carried on by
continue, its tools at the server's tool URL and its agent steps on the Claude API, all faked (no network). Needs the
Glyphh SDK 3.12 or later importable (CI installs it beside rrotor)."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from glyphh.client import Decision, ModelInfo
from glyphh.errors import ModelMissingError

from rrotor.ctx import CliResult
from rrotor.http import Response
from rrotor.testing import FakeCli, FakeHttp, Harness

HERE = Path(__file__).resolve().parent.parent
TOOL = "https://api.glyphh.ai/v1/bots/runs/r1/tool"
API = "https://api.anthropic.com/v1/messages"
KEY = "sk-ant-test-0123456789"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("glyphh_bot_program", HERE / "program.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAda:
    """Ada in memory: a model with no precedents asks; one with precedents decides as most of them said, unsure."""

    def __init__(self) -> None:
        self.specs: dict[str, Any] = {}
        self.cases: dict[str, list[tuple[Any, Any]]] = defaultdict(list)

    def models(self) -> list[ModelInfo]:
        return [ModelInfo({"name": n, "precedents": len(self.cases[n])}) for n in self.specs]

    def create_model(self, name: str, spec: dict[str, Any]) -> None:
        self.specs[name] = spec

    def model(self, name: str) -> Any:
        ada = self

        def there() -> None:
            if name not in ada.specs:
                raise ModelMissingError("E_MODEL_MISSING", name, 404)

        def info() -> ModelInfo:
            there()
            return ModelInfo({"name": name, "precedents": len(ada.cases[name])})

        def record(data: Any, outcome: Any = None) -> Any:
            there()
            ada.cases[name].append((data, outcome))
            return SimpleNamespace(data={})

        def decide(data: Any) -> Decision:
            said = [o for _, o in ada.cases[name]]
            best = max(set(said), key=said.count) if said else None
            return Decision.of({"outcome": best, "probabilities": {best: 0.5} if best else {}, "ask": True}, name)

        def facts(data: Any, top: int | None = None) -> Any:
            return SimpleNamespace(data=[])

        return SimpleNamespace(info=info, record=record, decide=decide, facts=facts)


SPEC = {
    "name": "worth",
    "layers": [
        {
            "name": "d",
            "segments": [
                {
                    "name": "s",
                    "roles": [
                        {"name": "n", "numeric_config": {"min_value": 0, "max_value": 50, "bin_width": 1}},
                        {"name": "run", "key_part": True, "similarity_weight": 0},
                    ],
                }
            ],
        }
    ],
    "output": {"ask_below": 0.7},
}


def package() -> dict[str, Any]:
    return {
        "bot.json": {
            "slug": "lister",
            "name": "Lister",
            "agent": "agent.md",
            "touches": "sends",
            "gates": {
                "worth": {
                    "case": {"d": {"s": {"n": "$fetch.count", "run": "x"}}},
                    "outcomes": ["send", "skip"],
                    "question": "Worth it?",
                    "spec": SPEC,
                }
            },
            "loop": [
                {"id": "fetch", "run": "inbox", "input": {"box": "$input.box"}},
                {"id": "worth", "gate": "worth", "on": {"send": "sum", "skip": "end"}},
                {"id": "sum", "agent": "Sum up: $fetch"},
                {"id": "post", "run": "slack_post", "input": {"text": "$sum"}, "approval": "post it"},
            ],
        },
        "agent.md": "---\nname: lister\ndescription: Sums up\n---\n\nYou are Lister.\n",
    }


class Server:
    """The server's tool URL: each tool's call kept, answered by tool name."""

    def __init__(self, **answers: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers = {"inbox": {"result": {"count": 3}}, "slack_post": {"result": {"ok": True}}, **answers}

    def __call__(self, method: str, url: str, body: bytes | None) -> Response:
        asked = json.loads(body or b"{}")
        self.calls.append(asked)
        answer = self.answers[asked["tool"]]
        return answer if isinstance(answer, Response) else Response(200, json.dumps(answer).encode())


def claude_says(text: str = "the sum") -> dict[str, Any]:
    return {
        "content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": text}],
        "model": "claude-opus-5-5",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def inputs(**over: Any) -> dict[str, Any]:
    given = {
        "package": package(),
        "state": None,
        "given": {"box": "in"},
        "ada_base": "https://ada.glyphh.ai",
        "ada_key": "ak_test",
        "tool_url": TOOL,
        "tool_token": "tok_run",
        "model": "sonnet",
    }
    return {**given, **over}


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, FakeAda]:
    program, ada = load(), FakeAda()
    monkeypatch.setattr(program, "make_ada", lambda base, key: ada)
    return program, ada


def harness(program: ModuleType, tmp_path: Path, server: Server, api: Any = None, **kw: Any) -> Harness:
    http = FakeHttp({TOOL: server, API: api if api is not None else claude_says()})
    return Harness(program, tmp_path, http=http, declared=["ANTHROPIC_API_KEY"], **kw)


def test_a_bot_asks_waits_and_carries_on_in_one_run(bot, tmp_path):
    program, ada = bot
    server = Server()
    h = harness(program, tmp_path, server, secrets={"ANTHROPIC_API_KEY": KEY})
    asked = h.call("run", inputs())
    assert asked.status == "needs_input" and asked.prompt.startswith("Worth it?")
    assert asked.data["options"] == ["send", "skip"] and asked.data["step"] == "worth" and asked.data["state"]
    assert asked.data["bot"] == "lister" and asked.data["status"] == "needs_input"
    assert server.calls == [{"tool": "inbox", "input": {"box": "in"}, "run_id": asked.data["run_id"]}]

    approval = h.resume("send")
    assert approval.status == "needs_input" and approval.data["approval"] is True and approval.data["step"] == "post"
    assert approval.data["options"] == ["yes", "no"] and ada.cases["bot-lister-gate-worth"][0][1] == "send"
    sent = json.loads(h.http.calls[-1][2])  # type: ignore[attr-defined]
    assert sent["model"] == "claude-sonnet-5-5" and sent["system"].startswith("You are Lister.")
    assert approval.data["tokens"] == {"input_tokens": 100, "output_tokens": 20}

    done = h.resume({"approved": True})
    assert done.status == "done" and done.data["status"] == "done" and done.data["output"] == '{"ok": true}'
    assert server.calls[-1]["input"] == {"text": "the sum"} and done.data["tokens"]["input_tokens"] == 100
    assert sum(1 for c in h.http.calls if c[1] == API) == 1  # type: ignore[attr-defined]  (the legs before, read back)
    assert h.ended


def test_a_run_that_waited_and_expired_is_carried_on_from_its_state(bot, tmp_path):
    program, _ = bot
    first = harness(program, tmp_path, Server(), secrets={"ANTHROPIC_API_KEY": KEY}).call("run", inputs())
    h = harness(program, tmp_path, Server(), secrets={"ANTHROPIC_API_KEY": KEY})
    done = h.call("run", inputs(state=first.data["state"], answer="skip"))
    assert done.status == "done" and done.data["run_id"] == first.data["run_id"]
    assert "gate worth said skip: the run ends here" in done.data["reason"]


def test_claude_code_signed_in_here_needs_no_key(bot, tmp_path):
    program, _ = bot
    said = json.dumps({"result": "the sum", "usage": {"input_tokens": 7, "output_tokens": 3}})
    claude = FakeCli(said, allow=["-p *"])
    h = harness(program, tmp_path, Server(), clis={"claude": claude})
    h.call("run", inputs(claude="cli"))
    h.resume("send")
    assert h.cli_calls[0][0] == "claude" and h.cli_calls[0][1][0] == "-p"
    assert h.resume("no").data["reason"] == "the person said no to post it"


def test_a_claude_code_that_cannot_run_fails_the_step_keeping_the_state(bot, tmp_path):
    program, _ = bot
    h = harness(program, tmp_path, Server(), clis={"claude": FakeCli(CliResult(0, "", ""), allow=["--version"])})
    h.call("run", inputs(claude="cli"))
    failed = h.resume("send")
    assert failed.status == "failed" and "E_CLI_DENIED" in failed.data["reason"] and failed.data["state"]


def test_the_api_needs_the_orgs_key(bot, tmp_path):
    program, _ = bot
    failed = harness(program, tmp_path, Server()).call("run", inputs())
    assert (failed.status, failed.step, failed.data["code"]) == ("failed", "run", "E_PROGRAM")
    assert "set ANTHROPIC_API_KEY" in failed.data["reason"]


def test_a_tool_the_server_refuses_fails_the_run_saying_why(bot, tmp_path):
    program, _ = bot
    server = Server(inbox=Response(403, json.dumps({"error": "inbox is not in scope"}).encode()))
    failed = harness(program, tmp_path, server, secrets={"ANTHROPIC_API_KEY": KEY}).call("run", inputs())
    assert failed.status == "failed" and failed.step == "bot" and failed.data["state"]
    assert "inbox: the server" not in failed.data["reason"] and "inbox is not in scope" in failed.data["reason"]
    down = harness(program, tmp_path, Server(inbox=Response(502, b"<html>")), secrets={"ANTHROPIC_API_KEY": KEY})
    assert "the server answered 502" in down.call("run", inputs()).data["reason"]


def test_a_package_that_is_not_a_bot_says_every_problem(bot, tmp_path):
    program, _ = bot
    wrong = package()
    wrong["bot.json"]["loop"][1]["gate"] = "nope"
    failed = harness(program, tmp_path, Server(), secrets={"ANTHROPIC_API_KEY": KEY}).call("run", inputs(package=wrong))
    assert (failed.status, failed.step) == ("failed", "package") and failed.data["problems"]
    coded = harness(program, tmp_path, Server(), secrets={"ANTHROPIC_API_KEY": KEY})
    with_code = {**package(), "program.py": "import os"}
    assert "carries no code" in coded.call("run", inputs(package=with_code)).data["reason"]


def test_an_old_sdk_is_refused(bot, tmp_path, monkeypatch):
    program, _ = bot
    import glyphh

    monkeypatch.setattr(glyphh, "__version__", "3.6.0")
    failed = harness(program, tmp_path, Server(), secrets={"ANTHROPIC_API_KEY": KEY}).call("run", inputs())
    assert (failed.status, failed.data["code"]) == ("failed", "E_SDK") and "needs 3.12" in failed.data["reason"]
    monkeypatch.setitem(sys.modules, "glyphh.bots.remote", None)
    assert harness(program, tmp_path, Server()).call("run", inputs()).data["code"] == "E_SDK"


def test_the_real_ada_is_the_bots_own():
    ada = load().make_ada("https://ada.example", "ak_x")
    assert (ada.url, ada.key) == ("https://ada.example", "ak_x")


def test_the_bots_effort_goes_with_each_agent_step_when_its_model_takes_it(bot, tmp_path):
    program, _ = bot
    h = harness(program, tmp_path, Server(), secrets={"ANTHROPIC_API_KEY": KEY})
    h.call("run", inputs(effort="high"))
    h.resume("send")
    sent = json.loads(h.http.calls[-1][2])  # type: ignore[attr-defined]
    assert sent["model"] == "claude-sonnet-5-5" and sent["output_config"] == {"effort": "high"}

    class Inner:
        def __init__(self) -> None:
            self.messages, self.asked = self, []

        def stream(self, **kw: Any) -> str:
            self.asked.append(kw)
            return "streamed"

    # no effort, a level or a model that takes none, or a caller's own output_config: asked as it was, never refused
    for effort, model, config in (
        (None, "claude-opus-5-5", None),
        ("extreme", "claude-opus-5-5", None),
        ("high", "claude-haiku-4-5", None),
        ("high", "claude-opus-5-5", {"effort": "low"}),
    ):
        inner = Inner()
        kw = {"model": model} | ({"output_config": config} if config else {})
        assert program.Effort(inner, effort).messages.stream(**kw) == "streamed" and inner.asked == [kw]
    inner = Inner()
    program.Effort(inner, "max").stream(model="claude-haiku-5-5")
    assert inner.asked == [{"model": "claude-haiku-5-5", "output_config": {"effort": "max"}}]


# The Claude API over ctx.http


def test_the_api_is_asked_again_when_busy_and_says_why_it_failed():
    claude_api = load().Messages.__module__
    from claude_api import ClaudeApiError, Messages  # type: ignore[import-not-found]

    assert claude_api == "claude_api"
    answers = [
        Response(529, b'{"error": {"message": "overloaded"}}'),
        Response(200, json.dumps(claude_says()).encode()),
    ]
    slept: list[float] = []
    http = FakeHttp({API: lambda m, u, b: answers.pop(0)})
    client = Messages(http, KEY, sleep=slept.append)
    with client.messages.stream(model="m", max_tokens=5, messages=[], timeout=9.0) as s:
        got = s.get_final_message()
    assert got.content[1].text == "the sum" and got.usage.output_tokens == 20 and slept == [2.0]
    assert "timeout" not in json.loads(http.calls[0][2]) and KEY not in repr(client)
    refused = Messages(FakeHttp({API: Response(401, b'{"error": {"message": "bad key"}}')}), KEY)
    with pytest.raises(ClaudeApiError, match="HTTP 401: bad key"):
        refused.create(model="m")
    busy = Messages(FakeHttp({API: Response(503, b"<html>down</html>")}), KEY, sleep=slept.append)
    with pytest.raises(ClaudeApiError, match="HTTP 503: <html>down"):
        busy.create(model="m")
    odd = Messages(FakeHttp({API: Response(400, b"[1]")}), KEY)
    with pytest.raises(ClaudeApiError, match=r"HTTP 400: \[1\]"):
        odd.create(model="m")
