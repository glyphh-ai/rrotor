"""A project's turns (grown from Glyphh Bot's own: glyphh-bot, src/renderer/src/engine.ts `swarm`, `turnPrompt`,
`persona`, `transcript`; src/main/workspace.ts: `endTurn`'s commit title): the words each bot is given, its turn's
branch, who goes next and when the crew is done. The server runs the project (who goes, waiting on each pull request)
and mirrors this module word for word (its ops/projects/turns.py); glyphh_swarm's `turn` and `pick` take one step on
this rrotor. Change the two together.

A crew has no order and a run no rounds. Wait your turn: `next_up` offers the turn to the bot that has waited longest
and has not just said it has nothing to do; a bot with nothing to do says PASS and the turn moves on. Orchestrator:
`pick_prompt` asks who goes next and why, `picked` reads the answer. Either way the run ends once
`crew_done(idle_row(...), len(crew))`: two bots in a row said DONE, or the whole crew in a row had nothing to do."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

DONE = re.compile(r"^\s*DONE\.?\s*$", re.IGNORECASE | re.MULTILINE)
PASS = re.compile(r"^\s*PASS\.?\s*$", re.IGNORECASE | re.MULTILINE)
SAID_MAX = 700  # a long message in the transcript: its last 700 characters
SAID_LAST = 14  # the transcript's last 14 messages
TITLE_MAX = 80
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
ON_BRANCH = (
    'You are on git branch "{branch}" in the crew\'s shared repo; every earlier turn is already merged into it. Read '
    "the repo as it stands, then do your part now and change real files. Do not commit, push or switch branches: when "
    "you finish, Glyphh commits your branch and opens your pull request."
)
BY_TOOLS = (
    "You work in the crew's shared repo through your tools: list_files, read_file, write_file and delete_file. You "
    "cannot run commands, tests or a browser here. Every earlier turn is already merged. Read the repo as it stands, "
    'then do your part now and change real files; when you finish, Glyphh commits your changes on branch "{branch}" '
    "and opens your pull request."
)
ENDING = (
    "Then end with your update to the crew: what you did and what should happen next. If there is nothing for you to "
    "do right now, change nothing and put the single word PASS on its own last line. If the whole project is finished "
    "and nothing is left to do, put the single word DONE on its own last line."
)


def stamp(now_ms: int) -> str:
    """A run's stamp: the time in milliseconds in base 36, its last 4 characters (`Date.now().toString(36)`)."""
    out, n = "", max(0, int(now_ms))
    while True:
        n, digit = divmod(n, 36)
        out = DIGITS[digit] + out
        if n == 0:
            return out[-4:]


def branch_name(turn: int, bot_id: str, run_stamp: str) -> str:
    """A turn's git branch: `turn-<n>-<botId>-<stamp>`."""
    return f"turn-{turn}-{bot_id}-{run_stamp}"


def is_done(said: str) -> bool:
    """The DONE rule: a turn's answer says the project is finished when a line of it is the word DONE alone (a full
    stop after it allowed, any case)."""
    return bool(DONE.search(said))


def is_pass(said: str) -> bool:
    """A turn's answer says the bot had nothing to do: a line of it is the word PASS alone."""
    return bool(PASS.search(said))


def idle_row(turns: Sequence[Mapping[str, Any]]) -> list[tuple[str, bool]]:
    """The bots at the end of the run that, one after another, had nothing more to do: each `(bot, said DONE)`, the
    newest last. A turn (`bot`, `done`, `changed`) is idle when it said DONE or changed nothing."""
    row: list[tuple[str, bool]] = []
    for t in reversed(turns):
        if not t.get("done") and t.get("changed"):
            break
        row.insert(0, (str(t["bot"]), bool(t.get("done"))))
    return row


def crew_done(row: Iterable[tuple[str, bool]], crew_size: int) -> bool:
    """The run ends once two bots in a row said DONE (the whole crew when it is one bot), or the whole crew in a row
    had nothing to do."""
    row = list(row)
    said = {bot for bot, done in row if done}
    return len(said) >= min(2, crew_size) or len({bot for bot, _ in row}) >= crew_size


def next_up(crew: Sequence[str], went: Sequence[str], idle: Iterable[str]) -> str | None:
    """Wait your turn: of the crew that did not just say it has nothing to do (`idle`), the bot that has waited
    longest (`went`: who took each turn so far, oldest first; one that never went waits longest), never the bot that
    just went while another can go. None when nobody has anything to do."""
    resting = set(idle)
    free = [b for b in crew if b not in resting]
    if len(free) > 1 and went:
        free = [b for b in free if b != went[-1]]
    last = {b: i for i, b in enumerate(went)}
    return min(free, key=lambda b: last.get(b, -1)) if free else None


def title(bot_name: str, said: str, turn: int) -> str:
    """A turn's commit and pull request title: the bot's name and the first sentence of what it said (80
    characters), or `turn <n>`."""
    first = re.split(r"(?<=[.!?])\s", " ".join(said.split()))[0]
    return f"{bot_name}: {first[:TITLE_MAX] or f'turn {turn}'}"


def transcript(messages: Iterable[Mapping[str, Any]], names: Mapping[str, str]) -> str:
    """What has been said since the run began: its last 14 messages with text (not notes, not one still being
    written), each `<who>: <text>`, a long one cut to its last 700 characters. A message: `role` (user, bot or
    note), `bot_id`, `text`, `streaming`."""
    kept = [
        m for m in messages if m.get("role") != "note" and str(m.get("text") or "").strip() and not m.get("streaming")
    ][-SAID_LAST:]
    lines = []
    for m in kept:
        who = "The person" if m.get("role") == "user" else names.get(str(m.get("bot_id")), "Bot")
        text = str(m["text"]).strip()
        lines.append(f"{who}: {'…' + text[-SAID_MAX:] if len(text) > SAID_MAX else text}")
    return "\n\n".join(lines)


def brief(project: Mapping[str, Any], crew: Iterable[Mapping[str, Any]], said: str) -> str:
    """What every bot (and the orchestrator) reads first. `project`: instructions (given on the project), path (the
    file in the repo that holds more), task (what the person asked this run, or none). Each of `crew`: name,
    pronouns, task, and for the orchestrator its id."""
    roster = "\n".join(
        f"- {b['name']}{' [' + b['id'] + ']' if b.get('id') else ''} ({b['pronouns']}): {b['task']}" for b in crew
    )
    task = f"TASK from the person: {project['task']}\n\n" if project.get("task") else ""
    return (
        f"PROJECT INSTRUCTIONS:\n{project.get('instructions') or '(none given here)'}\n\n"
        f"The repo may hold more of the project's instructions in {project['path']}: read it first if it is there, "
        f"and follow it.\n\n{task}"
        f"The crew (no order: one bot at a time, whoever has something to do):\n{roster}\n\n"
        f"What has been said so far:\n{said or '(nothing yet)'}"
    )


def turn_prompt(
    bot: Mapping[str, Any], about: str, branch: str, turn: int, why: str = "", by_tools: bool = False
) -> str:
    """What a bot is told on its turn. `about`: the `brief`. `why`: what the orchestrator said when it chose this
    bot. `by_tools`: the turn is on Glyphh cloud (no checkout: the repo through its tools)."""
    chosen = f" The orchestrator chose you: {why}" if why else ""
    work = (BY_TOOLS if by_tools else ON_BRANCH).format(branch=branch)
    return f"{about}\n\nIt is your turn, {bot['name']} (turn {turn}).{chosen} {work}\n{ENDING}"


def pick_prompt(about: str, turn: int, by_tools: bool = False) -> str:
    """What the orchestrator is asked before a turn: who goes next and why, or that the project is done."""
    how = "through your tools (list_files, read_file)" if by_tools else "in your working folder"
    return (
        f"{about}\n\nYou are this project's orchestrator. Turn {turn} is next. Read the repo as it stands {how}, and "
        "the instructions, and decide which bot of the crew goes next, or that the project is done. Change nothing.\n"
        "Answer with one JSON object and nothing else:\n"
        '{"next": "<the id in [brackets] of the bot that goes next, or null>", '
        '"why": "<one or two short sentences: what they should do now, or why the project is done>", '
        '"done": <true when the project is finished and nothing is left to do, else false>}'
    )


def picked(text: str, crew: Iterable[str]) -> tuple[str | None, str, bool]:
    """The orchestrator's answer: `(the bot it named, why, whether it said done)`. A bot not of the crew, or an
    answer that is not the JSON asked for, names nobody (the turn is then taken as wait your turn takes it)."""
    found = re.search(r"\{.*\}", text, re.S)
    try:
        got = json.loads(found.group(0)) if found else None
    except json.JSONDecodeError:
        got = None
    if not isinstance(got, dict):
        return None, "", False
    named = got["next"] if isinstance(got.get("next"), str) and got["next"] in set(crew) else None
    return named, str(got.get("why") or "").strip()[:SAID_MAX], got.get("done") is True and named is None


def persona(bot: Mapping[str, Any], look: str, cwd: str | None = None) -> str:
    """A bot's system prompt on its turn. `bot`: name, pronouns, personality, task; `look` its look in words (head,
    eyes, tail, extra, accessory, lower-cased, comma-separated). Without `cwd` the working-folder line is left for
    glyphh_swarm's turn to add (the clone is on the rrotor); a turn on Glyphh cloud has none."""
    text = (
        f"You are {bot['name']} ({bot['pronouns']}), a bot in Glyphh Bot, a voice-first bot OS. You are Spike, the "
        f"Glyphh alien cat, in black and white, with your own look: {look}.\n"
        f"Personality: {bot['personality']}\n"
        f"Your job: {bot['task']}\n"
        "Stay in character and do real work with your tools. Keep it kind.\n"
        "Your final message each turn is read aloud in your own voice when someone is watching. Make it 1 to 4 short "
        "sentences of natural speech: no markdown, no lists, no code, no file paths spelled out, no emoji. Put "
        "details in files, not in what you say."
    )
    return f"{text}\n{working_folder(cwd)}" if cwd else text


def working_folder(cwd: str) -> str:
    return f"Your working folder is {cwd}."
