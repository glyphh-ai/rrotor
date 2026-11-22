"""swarm_turns: Glyphh Bot's words and rules for a swarm's turns (glyphh-bot, src/renderer/src/engine.ts), which the
server mirrors: the texts here are the app's, character for character."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent.parent


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("swarm_turns_under_test", HERE / "swarm_turns.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


t = load()
ADA = {"name": "Ada", "pronouns": "she/her", "task": "Builds the page", "personality": "Bright and quick."}
BO = {"name": "Bo", "pronouns": "he/him", "task": "Tests it", "personality": "Calm."}


def test_a_turn_in_the_repo_is_told_its_branch_and_the_done_rule():
    said = t.transcript([{"role": "user", "text": "Make a page"}], {})
    got = t.turn_prompt(ADA, [ADA, BO], "Make a page", said, "turn-1-ada-k3x9", 1, False)
    assert got == (
        "CREW TASK from the person: Make a page\n\n"
        "The crew, in speaking order:\n- Ada (she/her): Builds the page\n- Bo (he/him): Tests it\n\n"
        "What has been said so far:\nThe person: Make a page\n\n"
        'It is your turn, Ada (turn 1). You are on git branch "turn-1-ada-k3x9" in the crew\'s shared repo; every '
        "earlier turn is already merged into it. Do your part of the task now and change real files. Do not commit, "
        "push or switch branches: when you finish, Glyphh Bot commits your branch and opens your pull request.\n"
        "Then end with your spoken update to the crew: what you did and what should happen next. If the whole task is "
        "finished and nothing is left to do, put the single word DONE on its own last line."
    )
    last = t.turn_prompt(BO, [ADA, BO], "Make a page", "", "turn-2-bo-k3x9", 2, True)
    assert "(nothing yet, you go first)" in last
    assert "what should happen next. This is the last turn, so leave things working. If the whole task" in last


def test_without_git_the_crew_only_talks():
    got = t.turn_prompt(ADA, [ADA], "Ideas?", "", None, 1, True)
    assert got.endswith(
        "It is your turn, Ada (turn 1). This is a round-table: just talk. Answer the person in your own "
        "voice, add something the others have not said, and keep it short."
    )


def test_the_transcript_is_the_last_fourteen_said_each_cut_to_its_end():
    messages = [
        {"role": "note", "text": "a note"},
        {"role": "bot", "bot_id": "x", "text": "  "},
        {"role": "bot", "bot_id": "ada", "text": "still writing", "streaming": True},
    ]
    messages += [{"role": "bot", "bot_id": "ada" if i % 2 else "gone", "text": f"line {i}"} for i in range(20)]
    messages.append({"role": "bot", "bot_id": "ada", "text": "x" * 701})
    got = t.transcript(messages, {"ada": "Ada"}).split("\n\n")
    assert len(got) == 14 and got[0] == "Ada: line 7" and got[1] == "Bot: line 8"
    assert got[-1] == "Ada: …" + "x" * 700


def test_the_done_rule_and_when_the_crew_is_done():
    assert t.is_done("All set.\nDONE") and t.is_done("done.") and t.is_done("x\n  Done  \ny")
    assert not t.is_done("I am done with it") and not t.is_done("DONE!")
    assert t.crew_done({"ada", "bo"}, 3) and not t.crew_done({"ada"}, 3) and t.crew_done(["ada"], 1)


def test_a_turns_branch_stamp_and_title():
    assert t.stamp(1_763_700_000_000) == format_36(1_763_700_000_000)[-4:] and t.stamp(5) == "5"
    assert t.branch_name(3, "ada", "k3x9") == "turn-3-ada-k3x9"
    assert t.title("Ada", "I wrote  the page!  Next, tests.", 1) == "Ada: I wrote the page!"
    assert t.title("Ada", "", 4) == "Ada: turn 4" and len(t.title("Ada", "y" * 200, 1)) == len("Ada: ") + 80


def test_the_persona_is_the_apps_with_the_working_folder_last():
    got = t.persona(ADA, "round head, wide eyes", "/data/repos/acme__crew")
    assert got.startswith(
        "You are Ada (she/her), a bot in Glyphh Bot, a voice-first bot OS. You are Spike, the Glyphh "
        "alien cat, in black and white, with your own look: round head, wide eyes.\n"
        "Personality: Bright and quick.\nYour job: Builds the page\n"
    )
    assert got.endswith("Put details in files, not in what you say.\nYour working folder is /data/repos/acme__crew.")
    assert "working folder" not in t.persona(ADA, "x")


def format_36(n: int) -> str:
    digits = ""
    while n:
        n, d = divmod(n, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[d] + digits
    return digits
