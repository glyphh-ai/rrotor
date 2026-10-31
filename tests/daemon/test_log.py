import json
import logging
import sys

from opentelemetry.sdk.trace import TracerProvider

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


def test_every_line_names_the_node_and_the_run_in_hand_and_the_span():
    log.identify(node_id="rrotor-1", cluster_id=None)
    try:
        with log.bind(run_id="r1", program="m", version=None):
            with log.bind(function="start"):
                line = json.loads(log.JsonFormatter().format(record("in a run")))
            assert "function" not in json.loads(log.JsonFormatter().format(record("after")))
        plain = json.loads(log.JsonFormatter().format(record("outside", program="own")))
        tracer = TracerProvider(shutdown_on_exit=False).get_tracer("t")
        with tracer.start_as_current_span("s") as span:
            traced = json.loads(log.JsonFormatter().format(record("traced")))
    finally:
        log.identify()
    assert (line["node_id"], line["run_id"], line["program"], line["function"]) == ("rrotor-1", "r1", "m", "start")
    assert "version" not in line and "cluster_id" not in line and "trace_id" not in line
    assert (plain["node_id"], plain["program"]) == ("rrotor-1", "own") and "run_id" not in plain
    context = span.get_span_context()
    assert traced["trace_id"] == f"{context.trace_id:032x}" and traced["span_id"] == f"{context.span_id:016x}"
    assert "node_id" not in json.loads(log.JsonFormatter().format(record("unnamed")))
