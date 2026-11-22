"""glyphh_bot with its folders' files (file_agent.py): an agent step as a loop of tool use over the folders the run
was given, a change made only on the server's word, the person asked where the server says so, and nothing of a
file's contents in what goes back up. The Claude API and the server are faked (no network); the folders are real."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from rrotor.http import Response
from rrotor.testing import FakeHttp, Harness

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = _load("glyphh_bot_test_program", HERE / "test_program.py")
TOOL, API, KEY = base.TOOL, base.API, base.KEY
SECRET = "the invoice total is 4711 euros"


def package() -> dict[str, Any]:
    return {
        "bot.json": {
            "slug": "filer",
            "name": "Filer",
            "agent": "agent.md",
            "touches": "read_only",
            "loop": [{"id": "work", "agent": "Do your filing."}],
        },
        "agent.md": "---\nname: filer\ndescription: Files\n---\n\nYou are Filer.\n",
    }


class Claude:
    """The Claude API, scripted: each request kept, answered by the next of `turns`."""

    def __init__(self, *turns: Any) -> None:
        self.turns, self.asked = list(turns), []

    def __call__(self, method: str, url: str, body: bytes | None) -> Response:
        self.asked.append(json.loads(body or b"{}"))
        turn = self.turns.pop(0)
        return turn if isinstance(turn, Response) else Response(200, json.dumps(turn).encode())


def uses(*calls: tuple[str, dict[str, Any]], stop: str = "tool_use") -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    content += [
        {"type": "tool_use", "id": f"t{n}_{name}", "name": name, "input": given}
        for n, (name, given) in enumerate(calls)
    ]
    return {
        "content": content,
        "model": "claude-sonnet-5-5",
        "stop_reason": stop,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 2,
        },
    }


def says(text: str, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "model": "claude-sonnet-5-5",
        "stop_reason": stop,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


class Server:
    """The server's tool URL: `file_change` answered by the next of `allowed` (True for ever when it runs out)."""

    def __init__(self, *allowed: Any) -> None:
        self.allowed, self.calls = list(allowed), []

    def __call__(self, method: str, url: str, body: bytes | None) -> Response:
        asked = json.loads(body or b"{}")
        self.calls.append(asked)
        said = self.allowed.pop(0) if self.allowed else True
        answer = {"result": {"allowed": said}} if isinstance(said, bool) else said
        return Response(200, json.dumps(answer).encode())


@pytest.fixture
def home(tmp_path: Path) -> Path:
    root = Path(os.path.realpath(tmp_path)) / "home"
    for folder in ("Invoices", "Notes", "Private"):
        (root / folder).mkdir(parents=True)
    (root / "Invoices" / "march.txt").write_text(SECRET)
    (root / "Invoices" / "april.pdf").write_bytes(b"%PDF-1.7 april")
    (root / "Private" / "secret.txt").write_text("private")
    return root


@pytest.fixture
def bot(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    program = base.load()
    monkeypatch.setattr(program, "make_ada", lambda _base, _key: base.FakeAda())
    return program


def folders(home: Path) -> list[dict[str, str]]:
    return [{"path": str(home / "Invoices"), "access": "read"}, {"path": str(home / "Notes"), "access": "write"}]


def harness(program: ModuleType, home: Path, claude: Claude, server: Server, **kw: Any) -> Harness:
    http = FakeHttp({TOOL: server, API: claude})
    kw.setdefault("folders", folders(home))
    return Harness(program, home.parent, http=http, secrets={"ANTHROPIC_API_KEY": KEY}, **kw)


def inputs(**over: Any) -> dict[str, Any]:
    given = {"package": package(), "given": "Summarise my invoices", "file_tools": True, "effort": "low"}
    return base.inputs(**{**given, **over})


def results(request: dict[str, Any]) -> list[dict[str, Any]]:
    last = request["messages"][-1]
    assert last["role"] == "user"
    return last["content"]


def test_a_step_reads_its_folders_and_writes_on_its_own(bot, home):
    march, summary = str(home / "Invoices" / "march.txt"), str(home / "Notes" / "Summary.md")
    claude = Claude(
        uses(
            ("list_folder", {}),
            ("read_file", {"path": march}),
            ("read_file", {"path": str(home / "Invoices" / "april.pdf")}),
        ),
        uses(("write_file", {"path": summary, "content": "# Summary\n4711"})),
        says("Done: see **Summary.md**."),
    )
    server = Server()
    done = harness(bot, home, claude, server).call("run", inputs())
    assert done.status == "done", done.data.get("reason")
    assert done.data["output"] == "Done: see **Summary.md**."
    assert (home / "Notes" / "Summary.md").read_text() == "# Summary\n4711"
    assert done.data["files"] == [
        {"name": "Invoices/march.txt", "did": "read"},
        {"name": "Invoices/april.pdf", "did": "read"},
        {"name": "Notes/Summary.md", "did": "written"},
    ]
    assert "file" not in done.data
    # every request of the loop is counted, the cache's input with the rest
    assert done.data["tokens"] == {"input_tokens": 15 + 15 + 100, "output_tokens": 5 + 5 + 20}
    first, second, third = claude.asked
    assert [t["name"] for t in first["tools"]] == [
        "list_folder",
        "read_file",
        "search_files",
        "write_file",
        "move_file",
    ]
    assert first["model"] == "claude-sonnet-5-5" and first["output_config"] == {"effort": "low"}
    assert first["thinking"] == {"type": "adaptive"} and first["cache_control"] == {"type": "ephemeral"}
    assert first["system"].startswith("You are Filer.") and f"- {home / 'Notes'} (read and change)" in first["system"]
    assert f"- {home / 'Invoices'} (read only)" in first["system"]
    assert first["messages"] == [
        {"role": "user", "content": "Do your filing.\n\nWhat the person asked for this run:\nSummarise my invoices"}
    ]
    # the assistant's turn goes back as it came (its thinking block too), then a result for each call, in one message
    assert second["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    listed, read, pdf, document = results(second)
    assert [r["tool_use_id"] for r in (listed, read, pdf)] == ["t0_list_folder", "t1_read_file", "t2_read_file"]
    assert json.loads(listed["content"])["folders"][0]["name"] == "Invoices" and read["content"] == SECRET
    assert document == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(b"%PDF-1.7 april").decode(),
        },
        "title": "Invoices/april.pdf",
    }
    assert json.loads(results(third)[0]["content"]) == {"name": "Notes/Summary.md", "bytes": 14, "created": True}
    # the server was asked about the change, by its names alone
    assert server.calls == [
        {
            "tool": "file_change",
            "input": {"action": "write", "path": summary, "name": "Notes/Summary.md"},
            "run_id": None,
        }
    ]
    # what is in a file never goes back up: not in the result, its log or the runner's state
    up = json.dumps({k: v for k, v in done.data.items() if k != "state"})
    assert SECRET not in up and "4711" not in up
    assert SECRET.encode() not in base64.b64decode(done.data["state"])


def test_a_change_waits_for_the_persons_yes_and_carries_the_conversation_on(bot, home):
    summary = str(home / "Notes" / "Summary.md")
    (home / "Notes" / "old.txt").write_text("old")
    claude = Claude(
        uses(
            ("read_file", {"path": str(home / "Invoices" / "march.txt")}),
            ("write_file", {"path": summary, "content": "S"}),
        ),
        uses(("move_file", {"path": str(home / "Notes" / "old.txt"), "to": str(home / "Notes" / "done" / "old.txt")})),
        says("Written and moved."),
    )
    server = Server(False, True, False, True)
    h = harness(bot, home, claude, server)
    asked = h.call("run", inputs())
    assert asked.status == "needs_input" and asked.prompt == "Write Notes/Summary.md?"
    assert asked.data["approval"] is True and asked.data["options"] == ["yes", "no"]
    assert asked.data["file"] == {"action": "write", "path": summary, "name": "Notes/Summary.md"}
    assert asked.data["files"] == [{"name": "Invoices/march.txt", "did": "read"}]
    assert asked.data["tokens"] == {"input_tokens": 15, "output_tokens": 5}
    assert not (home / "Notes" / "Summary.md").exists() and len(claude.asked) == 1
    assert SECRET not in json.dumps(asked.data) and SECRET.encode() not in base64.b64decode(asked.data["state"])
    assert SECRET in json.dumps(h.state.checkpoints["leg-0"]["kept"])  # the conversation held: on the machine only

    again = h.resume({"approved": True})
    assert again.status == "needs_input" and again.prompt == "Move Notes/old.txt to Notes/done/old.txt?"
    assert again.data["file"]["to_name"] == "Notes/done/old.txt"
    assert (home / "Notes" / "Summary.md").read_text() == "S" and (home / "Notes" / "old.txt").exists()
    # the same conversation, carried on: the read's result as it was, then the write's
    read, written = results(claude.asked[1])
    assert read["content"] == SECRET and json.loads(written["content"])["created"] is True
    assert again.data["tokens"] == {"input_tokens": 30, "output_tokens": 10}

    done = h.resume("yes")
    assert done.status == "done" and done.data["output"] == "Written and moved."
    assert (home / "Notes" / "done" / "old.txt").read_text() == "old" and not (home / "Notes" / "old.txt").exists()
    assert done.data["files"] == [
        {"name": "Invoices/march.txt", "did": "read"},
        {"name": "Notes/Summary.md", "did": "written"},
        {"name": "Notes/done/old.txt", "did": "moved"},
    ]
    assert done.data["tokens"] == {"input_tokens": 130, "output_tokens": 30} and "file" not in done.data
    assert [c["input"]["action"] for c in server.calls] == ["write", "write", "move", "move"]
    assert len(claude.asked) == 3 and h.ended


def test_a_change_the_person_refuses_is_not_made_and_claude_is_told(bot, home):
    (home / "Notes" / "Summary.md").write_text("as it was")
    summary = str(home / "Notes" / "Summary.md")
    claude = Claude(uses(("write_file", {"path": summary, "content": "new"})), says("I left it as it was."))
    server = Server(False)
    h = harness(bot, home, claude, server)
    asked = h.call("run", inputs())
    assert asked.prompt == "Replace what is in Notes/Summary.md?"
    done = h.resume({"approved": False})
    assert done.status == "done" and done.data["output"] == "I left it as it was."
    assert (home / "Notes" / "Summary.md").read_text() == "as it was" and done.data["files"] == []
    assert results(claude.asked[1]) == [
        {
            "type": "tool_result",
            "tool_use_id": "t0_write_file",
            "content": "The person said no to this change: it was not made.",
            "is_error": True,
        }
    ]
    assert len(server.calls) == 1  # asked once: a no takes nothing


def test_a_run_whose_wait_expired_starts_the_step_again_and_takes_the_yes(bot, home):
    summary = str(home / "Notes" / "Summary.md")
    first = harness(bot, home, Claude(uses(("write_file", {"path": summary, "content": "S"}))), Server(False))
    asked = first.call("run", inputs())
    claude = Claude(uses(("write_file", {"path": summary, "content": "S"})), says("Written."))
    done = harness(bot, home, claude, Server(True)).call("run", inputs(state=asked.data["state"], answer="yes"))
    assert done.status == "done" and (home / "Notes" / "Summary.md").read_text() == "S"
    assert claude.asked[0]["messages"][0]["content"].startswith("Do your filing.")
    assert done.data["files"] == [{"name": "Notes/Summary.md", "did": "written"}]


def test_what_the_folders_do_not_allow_is_refused_and_said(bot, home):
    (home / "Invoices" / "way-out").symlink_to(home / "Private")
    (home / "Notes" / "blocked").mkdir()
    calls = (
        ("read_file", {"path": str(home / "Private" / "secret.txt")}),
        ("read_file", {"path": str(home / "Invoices" / "way-out" / "secret.txt")}),
        ("write_file", {"path": str(home / "Invoices" / "new.txt"), "content": "x"}),
        ("write_file", {"path": str(home / "Notes" / "kept.txt"), "content": "x"}),
        ("write_file", {"path": str(home / "Notes" / "blocked" / "x.txt"), "content": "x"}),
        ("search_files", {"query": "invoice"}),
        ("delete_file", {"path": str(home / "Invoices" / "march.txt")}),
        ("read_file", "not an object"),
    )
    claude = Claude(uses(*calls), says("Some of that could not be done."))
    server = Server({"error": "the bot was given no folders"})
    os.chmod(home / "Notes" / "blocked", 0o500)
    try:
        done = harness(bot, home, claude, server).call("run", inputs())
    finally:
        os.chmod(home / "Notes" / "blocked", 0o700)
    assert done.status == "done"
    outside, link, readonly, server_no, os_no, found, unknown, bad = results(claude.asked[1])
    assert outside["is_error"] and "is not in a folder this bot was given" in outside["content"]
    assert link["is_error"] and "is a link" in link["content"]
    assert readonly["is_error"] and "was given to read only" in readonly["content"]
    assert (
        server_no["is_error"]
        and server_no["content"] == "The change was not made: file_change: the bot was given no folders."
    )
    assert os_no["is_error"] and os_no["content"].startswith("That could not be done: ")
    assert "is_error" not in found and json.loads(found["content"])["matches"][0]["name"] == "Invoices/march.txt"
    assert unknown["is_error"] and unknown["content"] == "There is no tool delete_file."
    assert bad["is_error"] and "is not a path" in bad["content"]
    assert not (home / "Invoices" / "new.txt").exists() and not (home / "Notes" / "kept.txt").exists()
    assert (home / "Private" / "secret.txt").read_text() == "private" and done.data["files"] == []


def test_a_long_file_says_it_was_cut(bot, home, monkeypatch):
    from rrotor import files

    monkeypatch.setattr(files, "TEXT_MAX", 10)
    claude = Claude(uses(("read_file", {"path": str(home / "Invoices" / "march.txt")})), says("ok"))
    harness(bot, home, claude, Server()).call("run", inputs())
    said = results(claude.asked[1])[0]["content"]
    assert said.startswith(SECRET[:10] + "\n\n[Invoices/march.txt is longer: only its first 10 characters")


def test_without_file_tools_or_folders_a_step_is_one_request(bot, home):
    for kw, given in (({}, inputs(file_tools=False)), ({"folders": []}, inputs())):
        claude = Claude(says("plain"))
        done = harness(bot, home, claude, Server(), **kw).call("run", given)
        assert done.status == "done" and done.data["output"] == "plain"
        assert "tools" not in claude.asked[0] and "files" not in done.data
        assert claude.asked[0]["messages"] == [{"role": "user", "content": "Do your filing."}]


@pytest.mark.parametrize(
    ("turns", "why"),
    [
        ([says("", stop="refusal")], "Claude declined this step"),
        ([says("half", stop="max_tokens")], "Claude's answer was cut off before it finished"),
        (
            [Response(400, b'{"error": {"message": "bad request"}}')],
            "Claude failed: ClaudeApiError: HTTP 400: bad request",
        ),
    ],
)
def test_a_step_claude_cannot_finish_fails_saying_why(bot, home, turns, why):
    failed = harness(bot, home, Claude(*turns), Server()).call("run", inputs())
    assert failed.status == "failed" and why in failed.data["reason"]
    assert failed.data["files"] == [] and "file" not in failed.data


def test_a_step_is_bounded_in_turns_and_a_paused_turn_is_carried_on(bot, home, monkeypatch):
    agent = sys.modules["file_agent"]
    claude = Claude(says("so far", stop="pause_turn"), says("and done"))
    done = harness(bot, home, claude, Server()).call("run", inputs())
    assert done.data["output"] == "and done" and claude.asked[1]["messages"][-1]["role"] == "assistant"
    monkeypatch.setattr(agent, "TURNS", 2)
    spins = Claude(uses(("list_folder", {})), uses(("list_folder", {})))
    failed = harness(bot, home, spins, Server()).call("run", inputs())
    assert failed.status == "failed" and "took more than 2 turns" in failed.data["reason"]
    assert failed.data["tokens"] == {"input_tokens": 0, "output_tokens": 0}


def test_the_agent_as_the_runner_calls_it(home):
    agent = sys.modules["file_agent"]
    message = SimpleNamespace(
        content=[
            SimpleNamespace(model_dump=lambda **_: {"type": "text", "text": "hello"}),
            {"type": "text", "text": " there"},
        ],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=7, output_tokens=None),
    )
    stream = SimpleNamespace(__enter__=lambda: None, get_final_message=lambda: message)

    class Client:
        def __init__(self) -> None:
            self.messages, self.kw = self, {}

        def stream(self, **kw: Any) -> Any:
            self.kw = kw
            return self

        def __enter__(self) -> Any:
            return stream

        def __exit__(self, *_: object) -> None:
            return None

    client = Client()
    claude = agent.FileClaude(client, "haiku", folders(home), lambda _change: True, task=7)
    assert claude.agent("filer", {"prompt": "You are Filer.", "model": "opus"}, "Say hello") == "hello there"
    assert client.kw["model"] == "claude-opus-5-5" and client.kw["timeout"] == 300.0
    assert client.kw["messages"][0] == {"role": "user", "content": "Say hello"}
    got = claude.answer("filer", {}, "Say hello", seconds=5)
    assert (got.input_tokens, got.output_tokens, client.kw["model"]) == (7, 0, "claude-haiku-5-5")
    assert agent.blocks(None) == [] and claude.keep() == {"held": None, "files": [], "read_bytes": 0}
    with pytest.raises(Exception, match="making a bot needs Claude Code"):
        claude.build("make one", home)
