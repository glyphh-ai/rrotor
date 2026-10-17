"""The cost of finding, measured by the size of each find's result (tokens ~ chars/4) and how long it then sits in
context: written to cache once ($12.50/M) and re-read on every later call of the session ($1/M). Compared with the
session's whole cache-read bill, so we know what share of context is finds."""

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"
FIND_CMD = re.compile(
    r"^\s*(grep|rg|find|cat|sed -n|head|tail|ls|git (grep|show|log|diff|status|ls-tree)|wc|tree|awk)\b"
)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content or "")


kinds = Counter()
persist = Counter()
write = Counter()
necessary = Counter()
cache_read_total = 0
find_in_context_total = 0
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
        # edits per turn, to tell a necessary read from the rest
        t, edits_by_turn = -1, {}
        for r in rows:
            c = (r.get("message") or {}).get("content")
            if r["type"] == "user" and isinstance(c, str):
                t += 1
                edits_by_turn[t] = set()
            elif r["type"] == "assistant" and isinstance(c, list) and t >= 0:
                for b in c:
                    if b.get("type") == "tool_use" and b.get("name") in EDIT_TOOLS:
                        edits_by_turn[t].add(str((b.get("input") or {}).get("file_path", "")))
        calls = sum(1 for r in rows if r["type"] == "assistant")
        t, call_index, pending = -1, 0, {}  # tool_use_id -> (kind, needed, call_index)
        live = []  # (tokens, kind, needed) of find results in context
        for r in rows:
            m = r.get("message") or {}
            c = m.get("content")
            if r["type"] == "user" and isinstance(c, str):
                t += 1
                continue
            if r["type"] == "user" and isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                        kind, needed, at = pending.pop(b["tool_use_id"])
                        tokens = len(text_of(b.get("content"))) / 4
                        (necessary if needed else kinds)[kind] += tokens
                        if not needed:
                            later = calls - at
                            persist[kind] += tokens * later * 1 / 1e6
                            write[kind] += tokens * 12.5 / 1e6
                            find_in_context_total += tokens * later
                continue
            if r["type"] != "assistant" or not isinstance(c, list):
                continue
            call_index += 1
            cache_read_total += (m.get("usage") or {}).get("cache_read_input_tokens", 0) or 0
            for b in c:
                if b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name"), b.get("input") or {}
                if name == "Read":
                    pending[b["id"]] = (
                        "Read",
                        str(inp.get("file_path", "")) in edits_by_turn.get(t, set()),
                        call_index,
                    )
                elif name in ("Grep", "Glob", "ToolSearch"):
                    pending[b["id"]] = (name, False, call_index)
                elif name == "Agent" and inp.get("subagent_type") == "Explore":
                    pending[b["id"]] = ("Explore", False, call_index)
                elif name == "Bash" and FIND_CMD.match(str(inp.get("command", ""))):
                    pending[b["id"]] = ("Bash find", False, call_index)
tok = {k: f"{int(v):,}" for k, v in kinds.most_common()}
print("find results not needed for an edit, tokens by kind:", tok, f"total {int(sum(kinds.values())):,}")
print("necessary reads (the file then edited), tokens:", f"{int(sum(necessary.values())):,}")
print(
    "their cost: written once $%.0f; re-read on every later call $%.0f; total $%.0f"
    % (sum(write.values()), sum(persist.values()), sum(write.values()) + sum(persist.values()))
)
print("by kind, $ re-read:", {k: f"${v:,.0f}" for k, v in persist.most_common()})
print(
    f"share of all cache-read tokens that are find results sitting in context: {find_in_context_total / max(cache_read_total, 1):.1%}  (cache reads total {cache_read_total:,} tokens = ${cache_read_total / 1e6:,.0f})"
)
