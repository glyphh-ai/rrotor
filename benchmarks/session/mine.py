"""Every turn of every Claude Code session on this machine: the request, what was read or searched before the first
edit (and what that cost, by the API's own count of tokens added to context), the files edited, the commands run.
Writes turns.jsonl (one turn per line, time-ordered) and prints the prize."""

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"
FIND_TOOLS = {"Read", "Grep", "Glob", "ToolSearch"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
FIND_CMD = re.compile(
    r"^\s*(grep|rg|find|cat|sed -n|head|tail|ls|git (grep|show|log|diff|status|ls-tree)|wc|tree|awk)\b"
)


def turns_of(path: Path):
    by_uuid = {}
    rows = []
    with open(path) as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") in ("user", "assistant") and not r.get("isSidechain"):
                rows.append(r)
    turn = None
    for r in rows:
        m = r.get("message") or {}
        c = m.get("content")
        if r["type"] == "user" and isinstance(c, str):
            if turn:
                yield turn
            turn = {
                "session": r.get("sessionId"),
                "at": r.get("timestamp"),
                "cwd": r.get("cwd"),
                "branch": r.get("gitBranch"),
                "request": c[:2000],
                "finds": [],
                "edits": [],
                "commands": [],
                "find_tokens": 0,
                "all_tokens": 0,
                "pending": None,
            }
            continue
        if turn is None:
            continue
        if r["type"] == "assistant" and isinstance(c, list):
            usage = m.get("usage") or {}
            added = int(usage.get("cache_creation_input_tokens") or 0) + int(usage.get("input_tokens") or 0)
            turn["all_tokens"] += added
            if turn["pending"] == "find" and not turn["edits"]:
                turn["find_tokens"] += added  # what the last find's result cost to bring into context
            turn["pending"] = None
            for b in c:
                if b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name"), b.get("input") or {}
                if name in FIND_TOOLS or (name == "Agent" and inp.get("subagent_type") == "Explore"):
                    target = (
                        inp.get("file_path") or inp.get("pattern") or inp.get("query") or inp.get("description") or ""
                    )
                    if not turn["edits"]:
                        turn["finds"].append([name, str(target)[:200]])
                        turn["pending"] = "find"
                elif name == "Bash":
                    cmd = str(inp.get("command", ""))
                    turn["commands"].append(cmd[:200])
                    if FIND_CMD.match(cmd) and not turn["edits"]:
                        turn["finds"].append(["Bash", cmd[:200]])
                        turn["pending"] = "find"
                elif name in EDIT_TOOLS:
                    turn["edits"].append(str(inp.get("file_path", ""))[:300])
    if turn:
        yield turn


out = open(Path(__file__).parent / "turns.jsonl", "w")
n = Counter()
find_tokens = all_tokens = 0
per_turn = []
for project in sorted(ROOT.iterdir()):
    if not project.is_dir():
        continue
    for path in sorted(project.glob("*.jsonl")):
        if path.stat().st_size == 0:
            continue
        for t in turns_of(path):
            t.pop("pending", None)
            t["project"] = project.name
            out.write(json.dumps(t) + "\n")
            n["turns"] += 1
            n["with_edits"] += bool(t["edits"])
            n["with_finds"] += bool(t["finds"])
            find_tokens += t["find_tokens"]
            all_tokens += t["all_tokens"]
            if t["edits"] and t["finds"]:
                per_turn.append(t["find_tokens"])
out.close()
per_turn.sort()
print(n)
print(
    f"tokens added to context across all turns: {all_tokens:,}; of which finding before the first edit: {find_tokens:,} ({find_tokens / max(all_tokens, 1):.0%})"
)
if per_turn:
    print(
        f"turns that searched then edited: {len(per_turn)}; find tokens per such turn: median {per_turn[len(per_turn) // 2]:,}, p90 {per_turn[int(len(per_turn) * 0.9)]:,}"
    )
print(
    f"at Fable's $10/M input: finding cost ${find_tokens * 10 / 1e6:,.2f}; at Opus's $4/M: ${find_tokens * 4 / 1e6:,.2f}"
)
