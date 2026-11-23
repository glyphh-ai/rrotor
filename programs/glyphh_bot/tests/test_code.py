"""glyphh_bot's `code` (rrotor 0.10.0): a function of a program's program.py run with the step's input, in a process
of its own that is given none of this program's secrets; what it returns answered, what goes wrong said."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

from rrotor.testing import FakeHttp, Harness

HERE = Path(__file__).resolve().parent
SOURCE = '''"""A program."""
import os

FLOW = []


def count(given):
    print("counting")
    return {"n": len(given["said"]), "name": __name__}


def secret(given):
    return {"key": os.environ.get("ANTHROPIC_API_KEY"), "cwd": os.getcwd()}


def broken(given):
    return given["missing"]


def not_json(given):
    return {1, 2}


def quits(given):
    raise SystemExit(3)


def dies(given):
    os._exit(4)


if __name__ == "__main__":
    raise RuntimeError("a program.py is never run as a script")
'''


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    program = _load("glyphh_bot_program_for_code", HERE.parent / "program.py")
    key = "ANTHROPIC_API_KEY"
    return Harness(program, tmp_path, http=FakeHttp({}), declared=[key], secrets={key: "k"})


def call(h: Harness, function: object, given: object = None, source: object = SOURCE):
    return h.call("code", {"source": source, "function": function, "given": given})


def test_a_function_is_called_with_the_steps_input(harness: Harness) -> None:
    done = call(harness, "count", {"said": "lunch"})
    assert done.status == "done"
    assert done.data == {"result": {"n": 5, "name": "program"}, "printed": "counting\n"}


def test_the_code_is_given_none_of_the_programs_secrets(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-the-orgs-own")
    done = call(harness, "secret")
    assert done.status == "done" and done.data["result"]["key"] is None
    assert os.path.basename(done.data["result"]["cwd"]) == "code"


@pytest.mark.parametrize(
    ("function", "why"),
    [
        ("broken", "broken raised KeyError: 'missing' (line 17)"),
        ("not_json", "not_json raised TypeError: Object of type set is not JSON serializable"),
        ("quits", "quits raised SystemExit: 3 (line 25)"),
        ("dies", "dies stopped before it answered (exit 4)."),
        ("nope", "nope raised KeyError: 'nope'"),
        ("not a name", "A code step names a function of program.py."),
        (None, "A code step names a function of program.py."),
    ],
)
def test_what_goes_wrong_fails_the_step_saying_why(harness: Harness, function: object, why: str) -> None:
    failed = call(harness, function, {})
    assert failed.status == "failed" and failed.step == "code"
    assert failed.data["reason"].startswith(why), failed.data


def test_a_file_that_is_not_python_and_code_that_takes_too_long_fail(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = call(harness, "count", {}, "def count(:")
    assert failed.status == "failed" and failed.data["reason"].startswith("count raised SyntaxError")
    assert call(harness, "count", {}, None).data["reason"] == "A code step names a function of program.py."
    import code_step

    monkeypatch.setattr(code_step, "SECONDS", 1)
    slow = "import time\n\n\ndef nap(given):\n    time.sleep(30)\n"
    assert call(harness, "nap", {}, slow).data["reason"] == "nap did not finish within 1 seconds."
