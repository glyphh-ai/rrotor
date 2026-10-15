import io
import os
import socket

import pytest

from rrotor._worker.channel import Channel


def test_send_writes_one_json_line():
    out = io.BytesIO()
    Channel(io.BytesIO(), out).send("log", message="hi")
    assert out.getvalue() == b'{"op":"log","message":"hi"}\n'


def test_receive_reads_a_message():
    assert Channel(io.BytesIO(b'{"op":"start"}\n'), io.BytesIO()).receive() == {"op": "start"}


def test_receive_answers_none_once_the_runner_is_gone():
    assert Channel(io.BytesIO(b""), io.BytesIO()).receive() is None


def test_receive_refuses_a_message_that_is_not_an_object():
    with pytest.raises(ValueError, match="object"):
        Channel(io.BytesIO(b"[1]\n"), io.BytesIO()).receive()


def test_from_fd_talks_over_the_socket():
    a, b = socket.socketpair()
    channel = Channel.from_fd(os.dup(b.fileno()))
    b.close()
    channel.send("ended")
    assert a.recv(100) == b'{"op":"ended"}\n'
    a.sendall(b'{"op":"end"}\n')
    assert channel.receive() == {"op": "end"}
    a.close()
