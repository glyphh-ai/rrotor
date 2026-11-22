from rrotor import STATUSES, Result


def test_done_carries_its_data():
    assert Result.done({"a": 1}).to_json() == {"status": "done", "data": {"a": 1}, "step": None, "prompt": None}


def test_needs_input_carries_prompt_schema_and_options():
    r = Result.needs_input("Which day?", {"type": "string"}, ["a", "b"])
    assert (r.status, r.prompt, r.data) == (
        "needs_input",
        "Which day?",
        {"schema": {"type": "string"}, "options": ["a", "b"]},
    )
    assert r.waiting


def test_needs_approval_names_its_step():
    r = Result.needs_approval("write", "Write it?")
    assert (r.status, r.step, r.prompt, r.waiting) == ("needs_approval", "write", "Write it?", True)


def test_failed_is_not_waiting():
    r = Result.failed("read", "no file")
    assert (r.status, r.step, r.data, r.waiting) == ("failed", "read", {"reason": "no file"}, False)


def test_a_question_and_a_failure_carry_their_detail_beside_what_they_always_say():
    asked = Result.needs_input("Worth it?", options=["send", "skip"], detail={"run_id": "r1", "options": "never"})
    assert asked.data == {"run_id": "r1", "schema": None, "options": ["send", "skip"]}
    failed = Result.failed("bot", "it broke", {"state": "c3Q=", "reason": "never"})
    assert failed.data == {"state": "c3Q=", "reason": "it broke"}


def test_statuses_are_the_four():
    assert {"done", "needs_input", "needs_approval", "failed"} == STATUSES
