"""Your usage over time, from the transcripts: per week, calls, context per call, what finding cost before the
first edit, and how often a file already read in the session was read again (the re-reads a ledger should cut).
Run it any time; the ledger went in on 2026-10-17, so the weeks before are the baseline."""

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"
FIND_CMD = re.compile(r"^\s*(grep|rg|find|cat|sed -n|head|tail|ls|git (grep|show|log|diff|status|ls-tree)|wc|tree|awk)\b")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return str(c or "")


weeks = defaultdict(lambda: {"calls": 0, "ctx": 0, "find_tokens": 0, "reads": 0, "rereads": 0, "edits": 0, "turns": 0, "ledger_lines": 0})
for project in sorted(ROOT.iterdir()):
    if not project.is_dir():
        continue
    for path in sorted(project.glob("*.jsonl")):
        if path.stat().st_size == 0:
            continue
        seen, pending, in_turn_edit, week = set(), {}, False, None
        with open(path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("isSidechain") or r.get("type") not in ("user", "assistant"):
                    continue
                ts = r.get("timestamp")
                if ts:
                    week = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%G-W%V")
                if week is None:
                    continue
                w = weeks[week]
                m = r.get("message") or {}
                c = m.get("content")
                if r["type"] == "user":
                    if isinstance(c, str):
                        w["turns"] += 1
                        in_turn_edit = False
                    elif isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                                kind = pending.pop(b["tool_use_id"])
                                if not in_turn_edit:
                                    w["find_tokens"] += len(text_of(b.get("content"))) / 4
                                if "Ledger for" in text_of(b.get("content")):
                                    w["ledger_lines"] += 1
                    continue
                u = m.get("usage") or {}
                w["calls"] += 1
                w["ctx"] += (u.get("cache_read_input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0)
                if isinstance(c, list):
                    for b in c:
                        if b.get("type") != "tool_use":
                            continue
                        name, inp = b.get("name"), b.get("input") or {}
                        if name == "Read":
                            p = str(inp.get("file_path", ""))
                            w["reads"] += 1
                            if p in seen:
                                w["rereads"] += 1
                            seen.add(p)
                            pending[b["id"]] = "Read"
                        elif name in ("Grep", "Glob") or (name == "Bash" and FIND_CMD.match(str(inp.get("command", "")))):
                            pending[b["id"]] = name
                        elif name in EDIT_TOOLS:
                            w["edits"] += 1
                            in_turn_edit = True
print(f"{'week':9s} {'calls':>7s} {'ctx/call':>9s} {'find tok/turn':>13s} {'reads':>6s} {'re-reads':>8s} {'edits':>6s}")
for week in sorted(weeks):
    w = weeks[week]
    if w["calls"] < 50:
        continue
    print(f"{week:9s} {w['calls']:7,d} {w['ctx'] / w['calls']:9,.0f} {w['find_tokens'] / max(w['turns'], 1):13,.0f} {w['reads']:6d} {w['rereads'] / max(w['reads'], 1):8.0%} {w['edits']:6d}")
