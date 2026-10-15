from typing import Any

from rrotord.link.outbox import Outbox


def msg(seq: int) -> dict[str, Any]:
    return {"id": f"m{seq}", "seq": seq}


def test_messages_are_numbered_and_kept_until_acked(tmp_path):
    box = Outbox(tmp_path)
    assert box.add(msg) == {"id": "m1", "seq": 1}
    box.add(lambda seq: {"id": "d", "seq": seq}, numbered=False)
    assert box.seq == 1 and len(box.pending) == 2
    assert box.ack("m1") and not box.ack("m1")
    assert [m["id"] for m in Outbox(tmp_path).pending] == ["d"]


def test_a_new_token_starts_the_counter_again(tmp_path):
    box = Outbox(tmp_path)
    box.restart("t1")
    box.add(msg)
    box.restart("t1")
    assert box.seq == 1
    box.restart("t2")
    assert (box.seq, box.pending, Outbox(tmp_path).token) == (0, [], "t2")
