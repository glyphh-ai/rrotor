from rrotor.errors import NotJson, PartMissing, ProgramError, StepUndeclared, Wait


def test_program_error_keeps_its_message_and_code():
    err = ProgramError("broke")
    assert (err.code, err.message, str(err)) == ("E_PROGRAM", "broke", "broke")


def test_part_missing_names_what_and_the_mod():
    err = PartMissing("The gateway (ctx.gateway)", "Gateway")
    assert (err.code, err.what, err.mod) == ("E_PART_MISSING", "The gateway (ctx.gateway)", "Gateway")
    assert "Gateway mod's part" in err.message


def test_step_undeclared_names_the_step():
    err = StepUndeclared("write")
    assert (err.code, err.step) == ("E_STEP_UNDECLARED", "write")


def test_not_json_has_its_code():
    assert NotJson("x").code == "E_NOT_JSON"


def test_wait_is_not_an_exception_a_program_catches():
    wait = Wait("result", {"kind": "input", "key": "k"})
    assert not isinstance(wait, Exception)
    assert (wait.result, wait.pending) == ("result", {"kind": "input", "key": "k"})
