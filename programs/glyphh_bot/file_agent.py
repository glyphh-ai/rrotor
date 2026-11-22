"""A bot's agent step with the files of the folders its owner gave it (docs/architecture.md, 5.10): instead of one
request with no tools, Claude works in a loop of tool use (the Messages API's: `tools`, `tool_use` blocks, a
`tool_result` for each, until `end_turn`) with five tools over exactly those folders (rrotor.files checks every path
by its real path; the sandbox mounts nothing else): list a folder, read a file (text, Word, PDF), search by name or
text, write a file, move or rename one. No delete, no shell.

A change (a write, a move) is made only on the server's word (`grant`, the server's `file_change`): at once when the
bot acts on its own, else only by taking the person's yes to that very change, once. With no yes yet the step stops
there and the run asks ("Write Notes/Summary.md?"); the conversation so far is `held` (it stays on this machine, in
the run's checkpoint), and the person's answer carries the same conversation on. Reads never ask.

What leaves the machine is names: `files.touched` (`[{name, did}]`) and the question. What is in a file goes to
Claude (it has to) and nowhere else: never into the step's data, the run's log or its state.

The client is `anthropic.Anthropic`'s Messages surface (`client.messages.stream(...).get_final_message()`), which
the program's own client (claude_api.Messages, over the run's ctx.http) has too. Tokens are counted as the single
request's are, every request of the loop added up, input read from or written to the cache counted as input."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from rrotor.files import Change, FileRefused, Files

TURNS = 40  # the requests one step may take
MAX_TOKENS = 16000
LISTED = 200  # the files a run names
YES, NO = "yes", "no"

GUIDE = """

You can use files on the person's computer, in these folders and nowhere else:
{folders}

Use the tools to list a folder, read a file, search for files and (where a folder may be changed) write, move or
rename one. Name every file and folder by its whole path. Read what the task needs before you answer; do not guess
what a file says. There is no delete and no way to run commands. A change may need the person's yes: when one is
refused, say so and carry on without it. When you are done, answer in clean markdown, naming the files you used."""

PATH = {"type": "string", "description": "Its whole path, from /"}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_folder",
        "description": "List what is in a folder: its folders and files, with sizes. With no path: the folders "
        "you were given and whether each may be changed. Call this first to see what is there.",
        "input_schema": {"type": "object", "properties": {"path": PATH}, "additionalProperties": False},
    },
    {
        "name": "read_file",
        "description": "Read one file: text of any kind, a Word document (.docx) or a PDF. Other types cannot be "
        "read and say so.",
        "input_schema": {
            "type": "object",
            "properties": {"path": PATH},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_files",
        "description": "Find files whose name has some text, and the lines of text files that have it (whatever "
        "its case). Looks in every folder you were given, or only under one.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The text to look for"},
                "under": {"type": "string", "description": "Only in this folder: its whole path"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": "Write a text file whole, making it (and its folders) when it is not there and replacing "
        "what it held when it is. Only in a folder that may be changed.",
        "input_schema": {
            "type": "object",
            "properties": {"path": PATH, "content": {"type": "string", "description": "The file's whole text"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "move_file",
        "description": "Move or rename one file, both places in folders that may be changed. Never onto a file "
        "that is there already.",
        "input_schema": {
            "type": "object",
            "properties": {"path": PATH, "to": {"type": "string", "description": "Where it goes: its whole path"}},
            "required": ["path", "to"],
            "additionalProperties": False,
        },
    },
]

Grant = Callable[[dict[str, str]], bool]


class _Ask(Exception):
    """A change the person has not said yes to yet."""

    def __init__(self, change: Change) -> None:
        super().__init__(change.name)
        self.change = change


def question(change: Change) -> str:
    """What the person is asked, in plain words, naming the file and nothing of what is in it."""
    if change.action == "move":
        return f"Move {change.name} to {change.to_name}?"
    return f"Replace what is in {change.name}?" if os.path.exists(change.path) else f"Write {change.name}?"


def blocks(content: Any) -> list[dict[str, Any]]:
    """A message's content as the API takes it back, unchanged (thinking blocks too): the official SDK's blocks
    (`model_dump`), this program's client's, or plain objects."""
    out: list[dict[str, Any]] = []
    for block in content or []:
        if isinstance(block, dict):
            out.append(block)
        elif hasattr(block, "model_dump"):
            out.append(block.model_dump(mode="json", exclude_none=True))
        else:
            out.append(dict(vars(block)))
    return out


def used(message: Any) -> tuple[int, int]:
    """A request's tokens: input (new, written to the cache and read from it, together) and output."""
    usage = message.usage
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(getattr(usage, k, 0) or 0) for k in keys), int(getattr(usage, "output_tokens", 0) or 0)


class FileClaude:
    """The bot's Claude for the SDK's runner (`answer`, as glyphh.bots.ApiClaude's), each agent step a loop of tool
    use over the run's folders. `client` is the Messages client; `grant` asks the server whether a change may be
    made now (False: the person is to be asked); `task` is what the person typed for this run; `kept` is what the
    last go of this run left (`keep()`: the conversation held at a question, the files named, the bytes read) and
    `said` the person's answer to that question."""

    def __init__(
        self,
        client: Any,
        model: str,
        folders: Any,
        grant: Grant,
        *,
        task: Any = None,
        kept: Mapping[str, Any] | None = None,
        said: Any = None,
    ) -> None:
        kept = kept or {}
        self.client, self.model, self._grant = client, model, grant
        self.task = task.strip() if isinstance(task, str) else ""
        self.files = Files(folders, before=self._before)
        self.files.touched = [dict(t) for t in kept.get("files") or []]
        self.files.read_bytes = int(kept.get("read_bytes") or 0)
        self._held: dict[str, Any] | None = kept.get("held") if said in (YES, NO) else None
        self._said = said
        self.held: dict[str, Any] | None = None  # the conversation this go stopped in, to carry on from
        self.asked: dict[str, str] | None = None  # the change it stopped to ask about
        self.spent = {
            "input_tokens": 0,
            "output_tokens": 0,
        }  # of a step that stopped to ask (the runner counts the rest)

    def keep(self) -> dict[str, Any]:
        """What the next go of this run needs of this one; it stays on the machine."""
        return {"held": self.held, "files": self.files.touched[-LISTED:], "read_bytes": self.files.read_bytes}

    def _before(self, change: Change) -> None:
        if not self._grant(change.view()):
            raise _Ask(change)

    # the tools

    def _tool(self, name: str, given: Any) -> tuple[str, list[dict[str, Any]]]:
        """One tool's answer as text, and what follows it in the message (a PDF, as a document)."""
        given = given if isinstance(given, dict) else {}
        if name == "list_folder":
            return json.dumps(self.files.list(given.get("path"))), []
        if name == "search_files":
            return json.dumps(self.files.search(given.get("query"), given.get("under"))), []
        if name == "write_file":
            return json.dumps(self.files.write(given.get("path"), given.get("content"))), []
        if name == "move_file":
            return json.dumps(self.files.move(given.get("path"), given.get("to"))), []
        if name != "read_file":
            raise FileRefused(f"There is no tool {name}.")
        read = self.files.read(given.get("path"))
        if read.kind == "pdf":
            source = {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(read.data).decode()}
            return f"{read.name} is a PDF: it follows these results.", [
                {"type": "document", "source": source, "title": read.name}
            ]
        cut = f"\n\n[{read.name} is longer: only its first {len(read.text):,} characters are here.]"
        return read.text + (cut if read.truncated else ""), []

    def _results(self, calls: list[dict[str, Any]], done: list[dict[str, Any]], said: Any) -> list[dict[str, Any]]:
        """A `tool_result` for each of the turn's calls, in order, from where `done` left off; `said` is the
        person's answer to the change the first of them asked about. _Ask leaves `done` as far as it got."""
        results = sum(1 for d in done if d["type"] == "tool_result")
        for call in calls[results:]:
            result: dict[str, Any] = {"type": "tool_result", "tool_use_id": call["id"]}
            more: list[dict[str, Any]] = []
            try:
                if said == NO:
                    raise FileRefused("The person said no to this change: it was not made.")
                result["content"], more = self._tool(str(call.get("name")), call.get("input"))
            except FileRefused as err:
                result |= {"content": err.message, "is_error": True}
            except OSError as err:
                result |= {"content": f"That could not be done: {err.strerror or err}.", "is_error": True}
            said = None
            done.append(result)
            done.extend(more)
        # every result first, then the documents they name: one user message
        return [d for d in done if d["type"] == "tool_result"] + [d for d in done if d["type"] != "tool_result"]

    # the loop

    def agent(
        self, name: str, definition: Mapping[str, Any], prompt: str, *, turns: int = 3, seconds: int = 300
    ) -> str:
        return str(self.answer(name, definition, prompt, turns=turns, seconds=seconds).text)

    def build(self, prompt: str, folder: Any, *, turns: int = 20, seconds: int = 900) -> str:
        from glyphh.bots import BotError

        raise BotError("making a bot needs Claude Code (`claude -p`): a bot on a rrotor runs agent steps only")

    def _first(self, definition: Mapping[str, Any], prompt: str) -> dict[str, Any]:
        from glyphh.bots.claude import MODELS

        named = str(definition.get("model") or "")
        given = "\n".join(
            f"- {f['path']} ({'read and change' if f['access'] == 'write' else 'read only'})"
            for f in self.files.folders()
        )
        asked = f"\n\nWhat the person asked for this run:\n{self.task}" if self.task and self.task not in prompt else ""
        return {
            "model": MODELS.get(named, named) or MODELS.get(self.model, self.model),
            "system": str(definition.get("prompt") or "") + GUIDE.format(folders=given),
            "messages": [{"role": "user", "content": prompt + asked}],
            "calls": [],
            "done": [],
        }

    def answer(
        self, name: str, definition: Mapping[str, Any], prompt: str, *, turns: int = 3, seconds: int = 300
    ) -> Any:
        """One agent step. Stops the run (the runner's Stop, `needs_input`) at a change the person is to be asked
        about; called again with their answer, it carries the same conversation on."""
        from glyphh.bots import BotError
        from glyphh.bots.claude import Answer, priced
        from glyphh.bots.runner import Stop

        state, said = self._held or self._first(definition, prompt), self._said
        self._held = self._said = None
        tokens = [0, 0]
        try:
            text = self._loop(state, said, tokens, float(seconds))
        except _Ask as ask:
            self.held, self.asked = state, ask.change.view()
            self.spent = {"input_tokens": tokens[0], "output_tokens": tokens[1]}
            asks = question(ask.change)
            raise Stop("needs_input", asks, {"question": asks, "options": [YES, NO], "approval": "files"}) from None
        except BotError:
            raise
        except Exception as err:
            raise BotError(f"Claude failed: {type(err).__name__}: {str(err)[:300]}") from err
        return Answer(text, priced(state["model"], *tokens), *tokens)

    def _loop(self, state: dict[str, Any], said: Any, tokens: list[int], seconds: float) -> str:
        from glyphh.bots import BotError

        messages: list[dict[str, Any]] = state["messages"]
        for _ in range(TURNS):
            if state["calls"]:
                results = self._results(state["calls"], state["done"], said)
                said = None
                messages.append({"role": "user", "content": results})
                state["calls"], state["done"] = [], []
            with self.client.messages.stream(
                model=state["model"],
                max_tokens=MAX_TOKENS,
                system=state["system"],
                messages=messages,
                tools=TOOLS,
                thinking={"type": "adaptive"},
                cache_control={"type": "ephemeral"},
                timeout=seconds,
            ) as stream:
                message = stream.get_final_message()
            spent = used(message)
            tokens[0], tokens[1] = tokens[0] + spent[0], tokens[1] + spent[1]
            if message.stop_reason == "refusal":
                raise BotError("Claude declined this step")
            if message.stop_reason == "max_tokens":
                raise BotError("Claude's answer was cut off before it finished: ask for less at once")
            content = blocks(message.content)
            messages.append({"role": "assistant", "content": content})
            state["calls"] = [b for b in content if b.get("type") == "tool_use"]
            if not state["calls"] and message.stop_reason != "pause_turn":
                return "".join(str(b.get("text") or "") for b in content if b.get("type") == "text").strip()
        raise BotError(f"The step took more than {TURNS} turns with its files: ask for less at once")
