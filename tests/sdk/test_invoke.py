from pathlib import Path

from rrotor.ctx import Ctx, RunState
from rrotor.errors import ModelError, Wait
from rrotor.http import Http
from rrotor.invoke import crash, failure, invoke, lifecycle
from rrotor.result import Result


def ctx(tmp_path: Path) -> Ctx:
    return Ctx(inputs={}, approvals=frozenset(), state=RunState(), files=tmp_path, http=Http(frozenset(), None))


def test_failure_carries_code_and_reason():
    assert failure("s", "E_X", "why").to_json() == {
        "status": "failed",
        "data": {"reason": "why", "code": "E_X"},
        "step": "s",
        "prompt": None,
    }


def test_crash_names_the_model_line():
    namespace: dict[str, object] = {}
    exec(compile("def fail():\n    raise ValueError('bad')\n", "/model/model.py", "exec"), namespace)  # noqa: S102
    try:
        namespace["fail"]()  # type: ignore[operator]
    except ValueError as err:
        result = crash("start", err)
    assert result.data["reason"] == "ValueError: bad (model.py line 2)"
    assert result.data["code"] == "E_CRASH"


def test_crash_outside_the_model_has_no_line():
    assert crash("start", RuntimeError("x")).data["reason"] == "RuntimeError: x"


def test_invoke_answers_the_result(tmp_path):
    outcome = invoke("start", lambda c, i: Result.done(i), ctx(tmp_path), {"a": 1})
    assert (outcome.result, outcome.pending) == (Result.done({"a": 1}), None)


def test_invoke_a_returned_wait_waits_for_a_result(tmp_path):
    outcome = invoke("start", lambda c: Result.needs_approval("s", "p"), ctx(tmp_path))
    assert outcome.pending == {"kind": "result", "key": "s"}


def test_invoke_a_returned_input_wait_has_an_empty_key(tmp_path):
    outcome = invoke("start", lambda c: Result.needs_input("p"), ctx(tmp_path))
    assert outcome.pending == {"kind": "result", "key": ""}


def test_invoke_a_raised_wait_answers_its_result(tmp_path):
    def fn(c):
        raise Wait(Result.needs_input("p"), {"kind": "input", "key": "p"})

    outcome = invoke("start", fn, ctx(tmp_path))
    assert (outcome.result.status, outcome.pending) == ("needs_input", {"kind": "input", "key": "p"})


def test_invoke_a_wait_without_a_result_fails(tmp_path):
    def fn(c):
        raise Wait("nope", {"kind": "input", "key": "p"})

    assert invoke("start", fn, ctx(tmp_path)).result.data["code"] == "E_BAD_RESULT"


def test_invoke_a_model_error_keeps_its_code(tmp_path):
    def fn(c):
        raise ModelError("no")

    assert invoke("start", fn, ctx(tmp_path)).result.data == {"reason": "no", "code": "E_MODEL"}


def test_invoke_a_crash_is_a_failed_result(tmp_path):
    assert invoke("start", lambda c: 1 / 0, ctx(tmp_path)).result.data["code"] == "E_CRASH"


def test_invoke_something_not_a_result_fails(tmp_path):
    outcome = invoke("start", lambda c: 42, ctx(tmp_path))
    assert outcome.result.data["reason"] == "start returned int, not a Result."


def test_invoke_data_json_cannot_carry_fails(tmp_path):
    outcome = invoke("start", lambda c: Result.done(object()), ctx(tmp_path))
    assert outcome.result.data["code"] == "E_NOT_JSON"


def test_lifecycle_without_the_function_answers_nothing(tmp_path):
    assert lifecycle("load", None, ctx(tmp_path)) is None


def test_lifecycle_that_works_answers_nothing(tmp_path):
    assert lifecycle("load", lambda c: None, ctx(tmp_path)) is None


def test_lifecycle_that_fails_answers_the_failure(tmp_path):
    result = lifecycle("end", lambda c: 1 / 0, ctx(tmp_path))
    assert result is not None
    assert (result.status, result.step) == ("failed", "end")


def test_a_write_on_a_read_only_run_is_e_read_only_naming_the_function(tmp_path):
    import errno
    import sqlite3

    class ReadOnlyError(Exception):  # a library's own refusal, with the code
        code = "E_READ_ONLY"

    def raising(err: BaseException):
        def fn(_ctx, _inputs):
            raise err

        return fn

    plain, ro = ctx(tmp_path), ctx(tmp_path)
    ro.readonly = True
    for err in (
        ReadOnlyError("record writes"),
        sqlite3.OperationalError("attempt to write a readonly database"),
        OSError(errno.EROFS, "Read-only file system"),
    ):
        result = invoke("sneak", raising(err), ro, {}).result
        assert result.data["code"] == "E_READ_ONLY" and result.data["reason"].startswith(
            'sneak is marked "writes": false'
        )
    assert invoke("q", raising(ReadOnlyError("x")), plain, {}).result.data["code"] == "E_READ_ONLY"
    for err in (sqlite3.OperationalError("readonly"), OSError(errno.EROFS, "ro")):
        assert invoke("q", raising(err), plain, {}).result.data["code"] == "E_CRASH"  # not a read-only run
    for other in (sqlite3.OperationalError("no such table"), OSError(errno.ENOENT, "gone"), ValueError("x")):
        assert invoke("q", raising(other), ro, {}).result.data["code"] == "E_CRASH"
