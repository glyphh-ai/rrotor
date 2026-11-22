"""The clone's command log (terminal.py): numbered lines that only grow, cut when long, nothing kept without a
file."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent.parent


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("glyphh_swarm_terminal", HERE / "terminal.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_lines_are_numbered_on_across_turns(tmp_path):
    t = load()
    path = tmp_path / "terminal" / "acme__crew.jsonl"
    first = t.Terminal(path)
    first.add("cmd", "git fetch --quiet --prune origin")
    first.add("out", "ok\n")
    first.add("out", "\n")  # nothing said: nothing kept
    second = t.Terminal(path)
    second.add("note", "Bo's turn on turn-2-bo-k3x9")
    got = lines(path)
    assert [(line["n"], line["kind"], line["text"]) for line in got] == [
        (0, "cmd", "git fetch --quiet --prune origin"),
        (1, "out", "ok"),
        (2, "note", "Bo's turn on turn-2-bo-k3x9"),
    ]
    assert all(line["at"].endswith("+00:00") for line in got)


def test_a_long_entry_is_clipped_and_a_long_log_is_cut_to_its_newest_lines(tmp_path, monkeypatch):
    t = load()
    monkeypatch.setattr(t, "CLIP", 10)
    monkeypatch.setattr(t, "MAX", 5)
    monkeypatch.setattr(t, "KEEP", 3)
    path = tmp_path / "log.jsonl"
    term = t.Terminal(path)
    term.add("out", "x" * 25)
    assert lines(path)[0]["text"] == "x" * 10 + "\n… (15 more characters)"
    for n in range(6):
        term.add("cmd", f"step {n}")
    assert len(lines(path)) == 7  # cut when a turn begins, not as it goes
    again = t.Terminal(path)
    assert [line["n"] for line in lines(path)] == [4, 5, 6] and again.n == 7


def test_a_damaged_line_is_stepped_over_and_no_path_keeps_nothing(tmp_path):
    t = load()
    path = tmp_path / "log.jsonl"
    path.write_text('{"n": 4, "kind": "cmd", "text": "a"}\nnot json\n{"kind": "out"}\n[1]\n')
    assert t.Terminal(path).n == 5
    path.write_text("not json\n")
    assert t.Terminal(path).n == 0
    nowhere = t.Terminal(None)
    nowhere.add("cmd", "git status")
    assert nowhere.n == 0
