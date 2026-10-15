from rrotord.errors import (
    BadRequest,
    Conflict,
    DeployError,
    Forbidden,
    NotFound,
    RrotorError,
    Unauthorized,
    Unavailable,
)


def test_an_error_answers_its_code_message_and_detail():
    err = NotFound("gone", code="E_RUN_MISSING", run_id="x")
    assert (err.status, err.to_json()) == (404, {"error": {"code": "E_RUN_MISSING", "message": "gone", "run_id": "x"}})


def test_status_can_be_set():
    assert RrotorError("x", status=418).status == 418


def test_defaults_per_class():
    pairs = [
        (c("m").code, c("m").status)
        for c in (RrotorError, BadRequest, Unauthorized, Forbidden, NotFound, Conflict, DeployError, Unavailable)
    ]
    assert pairs == [
        ("E_INTERNAL", 500),
        ("E_BAD_REQUEST", 400),
        ("E_AUTH", 401),
        ("E_FORBIDDEN", 403),
        ("E_NOT_FOUND", 404),
        ("E_CONFLICT", 409),
        ("E_DEPLOY", 422),
        ("E_UNAVAILABLE", 503),
    ]
