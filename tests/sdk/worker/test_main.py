import os
import socket

from rrotor._worker import __main__ as entry


def test_main_reads_its_channel_fd_and_stops_without_a_start(monkeypatch):
    a, b = socket.socketpair()
    monkeypatch.setenv("RROTOR_CHANNEL_FD", str(os.dup(b.fileno())))
    b.close()
    a.close()
    assert entry.main() == 2
