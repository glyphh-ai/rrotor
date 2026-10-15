"""The Gateway interface has no behaviour of its own: its methods only describe what programs will call."""

from typing import Any, cast

from rrotor.gateway import Gateway


def test_the_interface_methods_answer_nothing():
    me = cast(Any, object())
    assert Gateway.models(me) is None
    assert Gateway.chat(me, "m", []) is None
    assert Gateway.messages(me, "m", [], 10) is None
    assert Gateway.embeddings(me, "m", ["x"]) is None
