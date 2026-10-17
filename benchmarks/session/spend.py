"""Where the money goes across every session: the four token kinds in dollars (Fable list: $10/M input, $12.50/M
cache write, $1/M cache read, $50/M output), and what finding costs when its result stays in context for every
later call of the session. Finds that read a file the turn then edits are necessary (an edit needs the read); the
rest (other files, greps, globs, explores) are the candidates for a precedent store to replace."""

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"
PRICE = {"input": 10, "cache_write": 12.5, "cache_read": 1, "output": 50}  # $ per million
FIND_CMD = re.compile(
    r"^\s*(grep|rg|find|cat|sed -n|head|tail|ls|git (grep|show|log|diff|status|ls-tree)|wc|tree|awk)\b"
)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

totals = Counter()
find_kinds = Counter()  # tokens by kind of find
persist = Counter()  # dollars of cache reads those tokens caused later
necessary = Counter()
sessions = 0
for project in sorted(ROOT.iterdir()):
    if not project.is_dir():
        continue
    for path in sorted(project.glob("*.jsonl")):
        if path.stat().st_size == 0:
            continue
        rows = []
        with open(path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") in ("user", "assistant") and not r.get("isSidechain"):
                    rows.append(r)
        sessions += 1
        calls = [r for r in rows if r["type"] == "assistant"]
        n_calls = len(calls)
        for r in calls:
            u = (r.get("message") or {}).get("usage") or {}
            totals["input"] += u.get("input_tokens", 0) or 0
            totals["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
            totals["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            totals["output"] += u.get("output_tokens", 0) or 0
        # finds and their cost: the tokens the next assistant call wrote to cache, and the reads after it
        turn_edits, pending, call_index = set(), None, 0
        # first pass: edits per turn
        edits_by_turn, t = {}, -1
        for r in rows:
            m = r.get("message") or {}
            c = m.get("content")
            if r["type"] == "user" and isinstance(c, str):
                t += 1
                edits_by_turn[t] = set()
            elif r["type"] == "assistant" and isinstance(c, list) and t >= 0:
                for b in c:
                    if b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS:
                        edits_by_turn[t].add(str((b.get("input") or {}).get("file_path", "")))
        t = -1
        edited_so_far = set()
        for r in rows:
            m = r.get("message") or {}
            c = m.get("content")
            if r["type"] == "user" and isinstance(c, str):
                t += 1
                edited_so_far = set()
                pending = None
                continue
            if r["type"] != "assistant" or not isinstance(c, list):
                continue
            call_index += 1
            u = m.get("usage") or {}
            added = (u.get("cache_creation_input_tokens", 0) or 0) + (u.get("input_tokens", 0) or 0)
            if pending is not None:
                kind, needed = pending
                bucket = necessary if needed else find_kinds
                bucket[kind] += added
                later = n_calls - call_index
                (persist if not needed else Counter())[kind] += added * later * PRICE["cache_read"] / 1e6
                if not needed:
                    persist["$ write"] += added * PRICE["cache_write"] / 1e6
            pending = None
            for b in c:
                if b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name"), b.get("input") or {}
                if name in EDIT_TOOLS:
                    edited_so_far.add(str(inp.get("file_path", "")))
                elif name == "Read":
                    pending = ("Read", str(inp.get("file_path", "")) in edits_by_turn.get(t, set()))
                elif name in ("Grep", "Glob", "ToolSearch"):
                    pending = (name, False)
                elif name == "Agent" and inp.get("subagent_type") == "Explore":
                    pending = ("Explore", False)
                elif name == "Bash" and FIND_CMD.match(str(inp.get("command", ""))):
                    pending = ("Bash find", False)
dollars = {k: v * PRICE[k] / 1e6 for k, v in totals.items()}
print(f"sessions {sessions}")
print("tokens:", {k: f"{v:,}" for k, v in totals.items()})
print("dollars:", {k: f"${v:,.0f}" for k, v in dollars.items()}, f"total ${sum(dollars.values()):,.0f}")
print("finding not needed for the edit, tokens by kind:", {k: f"{v:,}" for k, v in find_kinds.most_common()})
print(
    "  written to cache + re-read on every later call: $",
    {k: f"{v:,.0f}" for k, v in persist.most_common()},
    f"total ${sum(persist.values()):,.0f}",
)
print("reads of the file then edited (necessary), tokens:", {k: f"{v:,}" for k, v in necessary.items()})
