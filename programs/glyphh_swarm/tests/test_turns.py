"""swarm_turns: the words and rules of a project's turns (grown from Glyphh Bot's own: glyphh-bot,
src/renderer/src/engine.ts), which the server mirrors (its ops/projects/turns.py, the same code): its tests, the same
too, below the loader."""

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
ADA = {"id": "ada", "name": "Ada", "pronouns": "she/her", "task": "Builds the page", "personality": "Bright and quick."}
BO = {"id": "bo", "name": "Bo", "pronouns": "he/him", "task": "Tests it", "personality": "Calm."}
NAMELESS = {k: v for k, v in BO.items() if k != "id"}
PROJECT = {"instructions": "Ship a landing page.", "path": "PROJECT.md", "task": "Make the hero"}
ABOUT = (
    "PROJECT INSTRUCTIONS:\nShip a landing page.\n\n"
    "The repo may hold more of the project's instructions in PROJECT.md: read it first if it is there, and follow "
    "it.\n\n"
    "TASK from the person: Make the hero\n\n"
    "The crew (no order: one bot at a time, whoever has something to do):\n"
    "- Ada [ada] (she/her): Builds the page\n- Bo (he/him): Tests it\n\n"
    "What has been said so far:\nThe person: Make a page"
)
ENDING = (
    "Then end with your update to the crew: what you did and what should happen next. If there is nothing for you to "
    "do right now, change nothing and put the single word PASS on its own last line. If the whole project is finished "
    "and nothing is left to do, put the single word DONE on its own last line."
)


def test_the_brief_is_the_instructions_the_task_the_crew_and_what_was_said() -> None:
    said = t.transcript([{"role": "user", "text": "Make a page"}], {})
    assert t.brief(PROJECT, [ADA, NAMELESS], said) == ABOUT
    bare = t.brief({"path": "PROJECT.md"}, [NAMELESS], "")
    assert bare == (
        "PROJECT INSTRUCTIONS:\n(none given here)\n\n"
        "The repo may hold more of the project's instructions in PROJECT.md: read it first if it is there, and "
        "follow it.\n\n"
        "The crew (no order: one bot at a time, whoever has something to do):\n- Bo (he/him): Tests it\n\n"
        "What has been said so far:\n(nothing yet)"
    )


def test_a_turn_in_a_checkout_is_told_its_branch_and_the_pass_and_done_rules() -> None:
    assert t.turn_prompt(ADA, ABOUT, "turn-1-ada-k3x9", 1) == (
        f"{ABOUT}\n\n"
        'It is your turn, Ada (turn 1). You are on git branch "turn-1-ada-k3x9" in the crew\'s shared repo; every '
        "earlier turn is already merged into it. Read the repo as it stands, then do your part now and change real "
        "files. Do not commit, push or switch branches: when you finish, Glyphh commits your branch and opens your "
        f"pull request.\n{ENDING}"
    )
    chosen = t.turn_prompt(BO, "about", "turn-2-bo-k3x9", 2, why="The page needs tests.")
    assert chosen.startswith(
        "about\n\nIt is your turn, Bo (turn 2). The orchestrator chose you: The page needs tests. You are on git "
        'branch "turn-2-bo-k3x9"'
    )


def test_a_turn_by_tools_has_no_checkout_and_is_told_its_tools() -> None:
    assert t.turn_prompt(ADA, "about", "turn-3-ada-k3x9", 3, by_tools=True) == (
        "about\n\n"
        "It is your turn, Ada (turn 3). You work in the crew's shared repo through your tools: list_files, read_file, "
        "write_file and delete_file. You cannot run commands, tests or a browser here. Every earlier turn is already "
        "merged. Read the repo as it stands, then do your part now and change real files; when you finish, Glyphh "
        f'commits your changes on branch "turn-3-ada-k3x9" and opens your pull request.\n{ENDING}'
    )
    assert "The orchestrator chose you: Go." in t.turn_prompt(ADA, "about", "b", 3, "Go.", True)


def test_the_orchestrator_is_asked_who_goes_next_as_json() -> None:
    assert t.pick_prompt("about", 4) == (
        "about\n\nYou are this project's orchestrator. Turn 4 is next. Read the repo as it stands in your working "
        "folder, and the instructions, and decide which bot of the crew goes next, or that the project is done. "
        "Change nothing.\n"
        "Answer with one JSON object and nothing else:\n"
        '{"next": "<the id in [brackets] of the bot that goes next, or null>", '
        '"why": "<one or two short sentences: what they should do now, or why the project is done>", '
        '"done": <true when the project is finished and nothing is left to do, else false>}'
    )
    by_tools = t.pick_prompt("about", 4, by_tools=True)
    assert "Read the repo as it stands through your tools (list_files, read_file), and the instructions" in by_tools


def test_the_orchestrators_answer_names_a_bot_of_the_crew_or_says_done() -> None:
    crew = ["ada", "bo"]
    assert t.picked('{"next": "bo", "why": " Tests are missing. ", "done": false}', crew) == (
        "bo",
        "Tests are missing.",
        False,
    )
    fenced = 'Here you go:\n```json\n{"next": "ada",\n "why": "The hero is empty."}\n```'
    assert t.picked(fenced, crew) == ("ada", "The hero is empty.", False)
    assert t.picked('{"next": null, "why": "All shipped.", "done": true}', crew) == (None, "All shipped.", True)
    assert t.picked('{"next": "ada", "why": "One more.", "done": true}', crew) == ("ada", "One more.", False)
    assert t.picked('{"next": null, "done": "yes"}', crew) == (None, "", False)
    assert t.picked('{"next": "x", "why": "' + "y" * 800 + '"}', crew) == (None, "y" * 700, False)


def test_an_answer_that_is_not_the_json_asked_for_or_names_a_stranger_names_nobody() -> None:
    crew = ["ada", "bo"]
    assert t.picked("Bo should go next.", crew) == (None, "", False)
    assert t.picked('{"next": "bo", "why": }', crew) == (None, "", False)
    assert t.picked("", crew) == (None, "", False)
    assert t.picked('{"next": "cy", "why": "Cy knows.", "done": false}', crew) == (None, "Cy knows.", False)
    assert t.picked('{"next": 7, "why": 3}', ["bo"]) == (None, "3", False)
    assert t.picked('{"next": ["bo"], "done": true}', ["bo"]) == (None, "", True)
    assert t.picked('["bo"]', ["bo"]) == (None, "", False) and t.picked("{bo} [1]", ["bo"]) == (None, "", False)


def test_the_done_and_pass_rules() -> None:
    assert t.is_done("All set.\nDONE") and t.is_done("done.") and t.is_done("x\n  Done  \ny")
    assert not t.is_done("I am done with it") and not t.is_done("DONE!") and not t.is_done("PASS")
    assert t.is_pass("Nothing for me.\nPASS") and t.is_pass("pass.") and t.is_pass("x\n  Pass  \ny")
    assert not t.is_pass("I pass on this") and not t.is_pass("PASS!") and not t.is_pass("DONE")


def test_the_idle_row_is_the_runs_last_turns_with_nothing_more_to_do() -> None:
    worked = {"bot": "ada", "done": False, "changed": True}
    passed = {"bot": "bo", "done": False, "changed": False}
    done = {"bot": "ada", "done": True, "changed": True}
    assert t.idle_row([]) == [] and t.idle_row([passed, worked]) == []
    assert t.idle_row([passed, worked, passed, done]) == [("bo", False), ("ada", True)]
    assert t.idle_row([done, {"bot": "cy"}]) == [("ada", True), ("cy", False)]


def test_the_crew_is_done_after_two_dones_in_a_row_or_a_whole_crew_with_nothing_to_do() -> None:
    assert t.crew_done([("ada", True), ("bo", True)], 3) and not t.crew_done([("ada", True)], 3)
    assert not t.crew_done([("ada", True), ("ada", True)], 3) and t.crew_done([("ada", True)], 1)
    assert not t.crew_done([("ada", False), ("bo", False)], 3)
    assert t.crew_done([("ada", False), ("bo", False), ("cy", False)], 3)
    assert t.crew_done(iter([("ada", False), ("bo", True)]), 2) and t.crew_done([("ada", False)], 1)
    assert not t.crew_done([], 2)


def test_wait_your_turn_offers_it_to_the_bot_that_waited_longest() -> None:
    crew = ["ada", "bo", "cy"]
    assert t.next_up(crew, [], []) == "ada" and t.next_up(crew, ["ada"], []) == "bo"
    assert t.next_up(crew, ["ada", "bo", "cy"], []) == "ada" and t.next_up(crew, ["ada", "bo", "ada"], []) == "cy"
    assert t.next_up(crew, ["ada", "bo", "cy", "ada"], ["bo"]) == "cy"
    assert t.next_up(crew, ["ada", "bo", "cy"], iter(["ada", "bo"])) == "cy"  # the one that just went: nobody else can
    assert t.next_up(crew, ["ada"], ["ada", "bo", "cy"]) is None and t.next_up([], [], []) is None
    assert t.next_up(["ada"], ["ada", "ada"], []) == "ada"


def test_the_transcript_is_the_last_fourteen_said_each_cut_to_its_end() -> None:
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


def test_a_turns_branch_stamp_and_title() -> None:
    assert t.stamp(1_763_700_000_000) == format_36(1_763_700_000_000)[-4:] and t.stamp(5) == "5"
    assert t.branch_name(3, "ada", "k3x9") == "turn-3-ada-k3x9"
    assert t.title("Ada", "I wrote  the page!  Next, tests.", 1) == "Ada: I wrote the page!"
    assert t.title("Ada", "", 4) == "Ada: turn 4" and len(t.title("Ada", "y" * 200, 1)) == len("Ada: ") + 80


def test_the_persona_is_the_apps_with_the_working_folder_last() -> None:
    got = t.persona(ADA, "round head, wide eyes", "/data/repos/acme__crew")
    assert got.startswith(
        "You are Ada (she/her), a bot in Glyphh Bot, a voice-first bot OS. You are Spike, the Glyphh "
        "alien cat, in black and white, with your own look: round head, wide eyes.\n"
        "Personality: Bright and quick.\nYour job: Builds the page\n"
    )
    assert "Your final message each turn is read aloud in your own voice when someone is watching." in got
    assert got.endswith("Put details in files, not in what you say.\nYour working folder is /data/repos/acme__crew.")
    assert "working folder" not in t.persona(ADA, "x")


def format_36(n: int) -> str:
    digits = ""
    while n:
        n, d = divmod(n, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[d] + digits
    return digits
