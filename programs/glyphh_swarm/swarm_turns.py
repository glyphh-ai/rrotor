"""The swarm's turns, as Glyphh Bot takes them (glyphh-bot, src/renderer/src/engine.ts: `swarm`, `turnPrompt`,
`persona`, `transcript`; src/main/workspace.ts: `endTurn`'s commit title): the words each bot is given, its turn's
branch, and when the crew is done. The server runs the swarm (who goes, the rounds, waiting on each pull request) and
mirrors this module word for word; glyphh_swarm's `turn` takes one turn on this rrotor.

The swarm, as the server runs it: for each round (1 to `rounds`) and each bot in the crew in order, a turn: its branch
`branch_name(turn, bot_id, stamp)` (turn counts up across rounds, `stamp` once a swarm), `turn_prompt(...)` with
`last` on the last bot of the last round, `persona(...)` as its system prompt; an open pull request waits for the
person's merge or no before the next bot goes; a conflict stays open. A bot whose answer `is_done` is kept in `done`,
any other answer empties it; the swarm ends early once `crew_done(done, len(crew))`."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

DONE = re.compile(r"^\s*DONE\.?\s*$", re.IGNORECASE | re.MULTILINE)
SAID_MAX = 700  # a long message in the transcript: its last 700 characters
SAID_LAST = 14  # the transcript's last 14 messages
TITLE_MAX = 80
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def stamp(now_ms: int) -> str:
    """A swarm's stamp: the time in milliseconds in base 36, its last 4 characters (`Date.now().toString(36)`)."""
    out, n = "", max(0, int(now_ms))
    while True:
        n, digit = divmod(n, 36)
        out = DIGITS[digit] + out
        if n == 0:
            return out[-4:]


def branch_name(turn: int, bot_id: str, swarm_stamp: str) -> str:
    """A turn's git branch: `turn-<n>-<botId>-<stamp>`."""
    return f"turn-{turn}-{bot_id}-{swarm_stamp}"


def is_done(said: str) -> bool:
    """The DONE rule: a turn's answer says the task is finished when a line of it is the word DONE alone (a full stop
    after it allowed, any case)."""
    return bool(DONE.search(said))


def crew_done(done: Iterable[str], crew_size: int) -> bool:
    """The swarm ends once this many bots in a row said DONE: two, or the whole crew when it is one bot."""
    return len(set(done)) >= min(2, crew_size)


def title(bot_name: str, said: str, turn: int) -> str:
    """A turn's commit and pull request title: the bot's name and the first sentence of what it said (80
    characters), or `turn <n>`."""
    first = re.split(r"(?<=[.!?])\s", " ".join(said.split()))[0]
    return f"{bot_name}: {first[:TITLE_MAX] or f'turn {turn}'}"


def transcript(messages: Iterable[Mapping[str, Any]], names: Mapping[str, str]) -> str:
    """What has been said since the swarm began: its last 14 messages with text (not notes, not one still being
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


def turn_prompt(
    bot: Mapping[str, Any],
    crew: Iterable[Mapping[str, Any]],
    task: str,
    said: str,
    branch: str | None,
    turn: int,
    last: bool,
) -> str:
    """What a bot is told on its turn. `bot` and each of `crew`: name, pronouns, task. `said`: the transcript. With a
    `branch` it works in the repo; without one (git off) it only talks."""
    roster = "\n".join(f"- {b['name']} ({b['pronouns']}): {b['task']}" for b in crew)
    if branch:
        work = (
            f'You are on git branch "{branch}" in the crew\'s shared repo; every earlier turn is already merged into '
            "it. Do your part of the task now and change real files. Do not commit, push or switch branches: when you "
            "finish, Glyphh Bot commits your branch and opens your pull request.\n"
            "Then end with your spoken update to the crew: what you did and what should happen next."
            f"{' This is the last turn, so leave things working.' if last else ''} If the whole task is finished and "
            "nothing is left to do, put the single word DONE on its own last line."
        )
    else:
        work = (
            "This is a round-table: just talk. Answer the person in your own voice, add something the others have not "
            "said, and keep it short."
        )
    return (
        f"CREW TASK from the person: {task}\n\n"
        f"The crew, in speaking order:\n{roster}\n\n"
        f"What has been said so far:\n{said or '(nothing yet, you go first)'}\n\n"
        f"It is your turn, {bot['name']} (turn {turn}). {work}"
    )


def persona(bot: Mapping[str, Any], look: str, cwd: str | None = None) -> str:
    """A bot's system prompt on its turn. `bot`: name, pronouns, personality, task; `look` its look in words (head,
    eyes, tail, extra, accessory, lower-cased, comma-separated). Without `cwd` the working-folder line is left for
    glyphh_swarm's turn to add (the clone is on the rrotor)."""
    text = (
        f"You are {bot['name']} ({bot['pronouns']}), a bot in Glyphh Bot, a voice-first bot OS. You are Spike, the "
        f"Glyphh alien cat, in black and white, with your own look: {look}.\n"
        f"Personality: {bot['personality']}\n"
        f"Your job: {bot['task']}\n"
        "Stay in character and do real work with your tools. Keep it kind.\n"
        "Your final message each turn is read aloud in your own voice. Make it 1 to 4 short sentences of natural "
        "speech: no markdown, no lists, no code, no file paths spelled out, no emoji. Put details in files, not in "
        "what you say."
    )
    return f"{text}\n{working_folder(cwd)}" if cwd else text


def working_folder(cwd: str) -> str:
    return f"Your working folder is {cwd}."
