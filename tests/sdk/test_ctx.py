import datetime as dt
from pathlib import Path

import pytest

from rrotor.ctx import Ctx, RunState
from rrotor.errors import NotJson, PartMissing, ProgramError, StepUndeclared, Wait
from rrotor.http import Http


def make(tmp_path: Path, state: RunState | None = None, sink=None, today=None, approvals=frozenset({"write"})) -> Ctx:
    return Ctx(
        inputs={"a": 1},
        approvals=approvals,
        state=state or RunState(),
        files=tmp_path,
        http=Http(frozenset(), None),
        sink=sink,
        today=today,
    )


def test_run_state_round_trips():
    state = RunState({"write": True}, {"k": 1}, {"c": [1]})
    assert RunState.from_json(state.to_json()) == state


def test_run_state_from_nothing_is_empty():
    assert RunState.from_json(None) == RunState()


def test_record_keeps_an_approval_only_when_approved_is_true():
    state = RunState()
    state.record({"kind": "approval", "key": "write"}, {"approved": True})
    state.record({"kind": "approval", "key": "send"}, {"approved": "yes"})
    state.record({"kind": "approval", "key": "drop"}, "approved")
    assert state.approvals == {"write": True, "send": False, "drop": False}


def test_record_keeps_an_input_by_its_key():
    state = RunState()
    state.record({"kind": "input", "key": "day"}, "2026-10-14")
    assert state.answers == {"day": "2026-10-14"}


def test_record_ignores_a_result_the_function_returned():
    state = RunState()
    state.record({"kind": "result", "key": ""}, "x")
    assert state == RunState()


def test_ask_approval_of_an_undeclared_step_raises(tmp_path):
    with pytest.raises(StepUndeclared):
        make(tmp_path).ask_approval("delete", "Delete?")


def test_ask_approval_waits_until_answered(tmp_path):
    with pytest.raises(Wait) as caught:
        make(tmp_path).ask_approval("write", "Write?")
    assert caught.value.pending == {"kind": "approval", "key": "write"}
    assert caught.value.result.status == "needs_approval"  # type: ignore[attr-defined]


def test_ask_approval_answers_the_decision(tmp_path):
    assert make(tmp_path, RunState(approvals={"write": False})).ask_approval("write", "Write?") is False


def test_needs_input_waits_by_its_key(tmp_path):
    with pytest.raises(Wait) as caught:
        make(tmp_path).needs_input("Which day?", key="day")
    assert caught.value.pending == {"kind": "input", "key": "day"}


def test_needs_input_answers_by_the_prompt_by_default(tmp_path):
    assert make(tmp_path, RunState(answers={"Which day?": "x"})).needs_input("Which day?") == "x"


def test_checkpoint_runs_once_and_tells_the_sink(tmp_path):
    seen = []
    ctx = make(tmp_path, sink=lambda op, data: seen.append((op, data)))
    assert ctx.checkpoint("k", lambda: 5) == 5
    assert ctx.checkpoint("k", lambda: 6) == 5
    assert seen == [("checkpoint", {"key": "k", "value": 5})]


def test_checkpoint_refuses_a_value_json_cannot_carry(tmp_path):
    with pytest.raises(NotJson):
        make(tmp_path).checkpoint("k", lambda: object())


def test_log_sends_message_and_detail(tmp_path):
    seen = []
    make(tmp_path, sink=lambda op, data: seen.append((op, data))).log("hi", n=1)
    assert seen == [("log", {"message": "hi", "detail": {"n": 1}})]


def test_log_without_a_sink_goes_nowhere(tmp_path):
    make(tmp_path).log("hi")


def test_today_and_yesterday_use_the_clock(tmp_path):
    ctx = make(tmp_path, today=lambda: dt.date(2026, 3, 1))
    assert (ctx.today(), ctx.yesterday()) == ("2026-03-01", "2026-02-28")


def test_today_defaults_to_utc(tmp_path):
    assert make(tmp_path).today() == dt.datetime.now(dt.UTC).date().isoformat()


@pytest.mark.parametrize("name", ["db", "gateway"])
def test_parts_not_installed_raise_part_missing(tmp_path, name):
    with pytest.raises(PartMissing):
        getattr(make(tmp_path), name)


def test_secrets_are_not_built(tmp_path):
    with pytest.raises(ProgramError) as caught:
        _ = make(tmp_path).secrets
    assert caught.value.code == "E_NOT_BUILT"
