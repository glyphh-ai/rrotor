import uuid

import pytest
from pydantic import ValidationError

from rrotord.link import protocol as p

SHA = "a" * 40


def test_down_messages_by_type():
    run_id = str(uuid.uuid4())
    assert isinstance(p.DOWN.validate_json('{"type":"welcome"}'), p.Welcome)
    welcome = p.DOWN.validate_python({"type": "welcome", "licence": {"payload": {"plan": "Pro"}, "signature": "s"}})
    assert isinstance(welcome, p.Welcome) and welcome.licence is not None and welcome.licence.signature == "s"
    assert p.DOWN.validate_python({"type": "welcome"}).licence is None  # type: ignore[union-attr]
    keyed = p.DOWN.validate_python({"type": "welcome", "license_key": "a.b"})
    assert isinstance(keyed, p.Welcome) and keyed.license_key == "a.b"
    assert p.DOWN.validate_python({"type": "welcome"}).license_key is None  # type: ignore[union-attr]
    assert isinstance(p.DOWN.validate_python({"type": "keys", "hashes": ["a" * 64]}), p.Keys)
    assert isinstance(p.DOWN.validate_python({"type": "pause"}), p.Pause)
    assert isinstance(p.DOWN.validate_python({"type": "resume"}), p.Resume)
    call = p.DOWN.validate_python(
        {
            "type": "call",
            "id": "c1",
            "run_id": run_id,
            "program": "x",
            "version": 1,
            "function": "start",
            "inputs": {},
            "caller": "routine",
        }
    )
    assert isinstance(call, p.Call) and str(call.run_id) == run_id
    assert isinstance(p.DOWN.validate_python({"type": "continue", "run_id": run_id, "answer": 1}), p.Continue)
    assert isinstance(p.DOWN.validate_python({"type": "cancel", "run_id": run_id}), p.Cancel)
    deploy = p.DOWN.validate_python({"type": "activate", "program": "x", "version": 2, "commit_sha": SHA})
    assert isinstance(deploy, p.Deploy) and deploy.repo is None
    assert isinstance(p.DOWN.validate_python({"type": "secret_set"}), p.Unbuilt)
    assert isinstance(p.DOWN.validate_python({"type": "ack", "of": "m1"}), p.Ack)
    assert isinstance(p.DOWN.validate_python({"type": "pong"}), p.Pong)


def test_a_bad_message_is_refused():
    with pytest.raises(ValidationError):
        p.DOWN.validate_python({"type": "deploy", "program": "x", "version": 1, "commit_sha": "short"})


def test_a_protocol_1_message_naming_a_model_is_refused_not_misread():
    """Protocol 1 named a program `model`: such a message fails validation (the client skips it), and `hello` says 2,
    so a server of protocol 1 closes the link with 4426 (`upgrade_needed`)."""
    assert p.PROTOCOL == 2
    for old in (
        {"type": "deploy", "model": "x", "version": 1, "commit_sha": SHA},
        {
            "type": "call",
            "run_id": str(uuid.uuid4()),
            "model": "x",
            "version": 1,
            "function": "f",
            "inputs": {},
            "caller": "routine",
        },
    ):
        with pytest.raises(ValidationError):
            p.DOWN.validate_python(old)


def test_up_messages():
    hello = p.hello("0.1.0", "local", "rid", [])
    assert (hello["type"], hello["protocol"], hello["runtime_id"]) == ("hello", 2, "rid")
    result = p.result(3, "r", {"status": "done", "data": 1, "extra": "no"}, 2.9)
    assert result["result"] == {"status": "done", "data": 1, "step": None, "prompt": None}
    assert (result["seq"], result["seconds"]) == (3, 2)
    assert p.event(4, "x", "deploy_failed", {"a": 1})["kind"] == "deploy_failed"
    assert p.deployed("x", 2, SHA)["commit_sha"] == SHA
    assert p.Ping().model_dump()["type"] == "ping" and len(p.new_id()) == 32


def test_stop_closes():
    assert p.STOPS == {4401: "refused", 4409: "replaced", 4426: "upgrade_needed", 4400: "bad_hello"}


def test_deployed_carries_the_repo_and_the_manifest_or_nulls():
    full = p.deployed("x", 2, SHA, "owner/x", {"name": "x"})
    assert full == {
        "type": "deployed",
        "id": full["id"],
        "program": "x",
        "version": 2,
        "commit_sha": SHA,
        "repo": "owner/x",
        "manifest": {"name": "x"},
    }
    bare = p.deployed("x", 2, SHA)
    assert (bare["repo"], bare["manifest"]) == (None, None) and len(bare["id"]) == 32 and bare["id"] != full["id"]


def test_usage_is_numbered_and_carries_each_keys_calls():
    keys = [{"key_sha256": "a" * 64, "calls": 3, "last_used_at": "2026-10-16T09:00:00+00:00"}]
    message = p.usage(7, keys)
    assert message == {"type": "usage", "id": message["id"], "seq": 7, "keys": keys} and len(message["id"]) == 32
