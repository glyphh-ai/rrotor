import types

import pytest

from rrotor import Result
from rrotor.http import Response
from rrotor.testing import FakeHttp, Harness


def module(**fns) -> types.ModuleType:
    m = types.ModuleType("prog")
    for k, v in fns.items():
        setattr(m, k, v)
    return m


def test_fake_http_answers_json_by_the_longest_prefix():
    http = FakeHttp({"https://a.example.com/": {"short": 1}, "https://a.example.com/x": {"long": 2}})
    assert http.get("https://a.example.com/x/y").json() == {"long": 2}
    assert http.calls == [("GET", "https://a.example.com/x/y", None)]


def test_fake_http_answers_a_response_or_a_function():
    http = FakeHttp(
        {
            "https://a.example.com/r": Response(418, b"tea"),
            "https://a.example.com/f": lambda m, u, b: Response(200, (m + (b or b"").decode()).encode()),
        }
    )
    assert http.get("https://a.example.com/r").status == 418
    assert http.post("https://a.example.com/f", data=b"!").text == "POST!"


def test_fake_http_answers_404_without_a_route():
    http = FakeHttp({}, hosts=frozenset({"a.example.com"}))
    assert http.get("https://a.example.com/").status == 404


def test_harness_runs_load_function_and_end(tmp_path):
    seen = []
    prog = module(
        load=lambda ctx: seen.append("load"),
        start=lambda ctx, inputs: (ctx.log("hi", n=1), Result.done(inputs))[1],
        end=lambda ctx: seen.append("end"),
    )
    h = Harness(prog, tmp_path)
    assert h.call("start", {"a": 1}) == Result.done({"a": 1})
    assert seen == ["load", "end"] and h.ended
    assert h.logs == [{"message": "hi", "detail": {"n": 1}}]


def test_harness_waits_and_resumes(tmp_path):
    def start(ctx, inputs):
        return Result.done(ctx.ask_approval("write", "Write?"))

    h = Harness(module(start=start), tmp_path, approvals={"start": {"write"}}, today=None)
    assert h.call("start").status == "needs_approval"
    assert not h.ended
    assert h.resume({"approved": True}) == Result.done(True)


def test_harness_a_failed_load_skips_the_function(tmp_path):
    prog = module(load=lambda ctx: 1 / 0, start=lambda ctx, i: Result.done())
    assert Harness(prog, tmp_path).call("start").step == "load"


def test_harness_resume_when_not_waiting_raises(tmp_path):
    with pytest.raises(RuntimeError):
        Harness(module(), tmp_path).resume(1)


def test_harness_keeps_checkpoints_across_resumes(tmp_path):
    calls = []

    def count() -> int:
        calls.append(1)
        return len(calls)

    def start(ctx, inputs):
        value = ctx.checkpoint("c", count)
        return Result.done(ctx.needs_input("more?", key="k") + value)

    h = Harness(module(start=start), tmp_path)
    assert h.call("start").status == "needs_input"
    assert h.resume(10) == Result.done(11)
    assert calls == [1] and h.logs == []
