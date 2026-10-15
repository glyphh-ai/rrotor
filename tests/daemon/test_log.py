import json
import logging
import sys

from rrotord import log
from tests.fakes import token


def test_mask_hides_tokens():
    secret = token()
    text = f"Bearer {secret} token={secret} x-access-token:{secret}"
    assert secret not in log.mask(text)
    assert log.mask("Bearer short") == "Bearer short"


def record(message: str, **extra) -> logging.LogRecord:
    r = logging.makeLogRecord({"name": "rrotor.t", "levelname": "INFO", "msg": message, "created": 0.0})
    r.__dict__.update(extra)
    return r


def test_format_is_one_json_object_with_extras():
    line = json.loads(log.JsonFormatter().format(record("hi %s", run_id="r")))
    assert line["message"] == "hi %s" and line["run_id"] == "r" and line["level"] == "info"
    assert line["at"].startswith("1970-01-01")


def test_format_keeps_a_masked_error():
    secret = token()
    try:
        raise ValueError(f"Bearer {secret}")
    except ValueError:
        r = record("failed", exc_info=sys.exc_info())
    out = log.JsonFormatter().format(r)
    assert "ValueError" in json.loads(out)["error"] and secret not in out


def test_setup_sends_json_to_stdout(capsys):
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        log.setup("DEBUG")
        logging.getLogger("rrotor.t").debug("hello")
        assert json.loads(capsys.readouterr().out)["message"] == "hello"
    finally:
        root.handlers[:] = saved
        root.setLevel(level)
