import socket
import threading

from rrotor._worker import forward
from rrotor._worker.forward import Forwarder


def echo_server(path: str) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                while data := conn.recv(1024):
                    conn.sendall(data.upper())

    threading.Thread(target=serve, daemon=True).start()
    return server


def test_a_connection_is_piped_to_the_proxy_socket(short_dir):
    target = str(short_dir / "p.sock")
    server = echo_server(target)
    fwd = Forwarder(target, port=0).start()
    try:
        with socket.create_connection(("127.0.0.1", fwd.port), timeout=5) as client:
            client.sendall(b"hello")
            assert client.recv(100) == b"HELLO"
    finally:
        fwd.close()
        server.close()


def test_a_missing_proxy_socket_closes_the_connection(short_dir):
    fwd = Forwarder(str(short_dir / "missing.sock"), port=0).start()
    try:
        with socket.create_connection(("127.0.0.1", fwd.port), timeout=5) as client:
            assert client.recv(100) == b""
    finally:
        fwd.close()


def test_the_default_port_is_the_proxys():
    assert forward.PORT == 3128


class Broken:
    def __init__(self) -> None:
        self.shut = False

    def recv(self, n: int) -> bytes:
        raise OSError("reset")

    def shutdown(self, how: int) -> None:
        self.shut = True
        raise OSError("not connected")


def test_a_pipe_that_breaks_shuts_both_ends():
    src, dst = Broken(), Broken()
    forward._pipe(src, dst)  # type: ignore[arg-type]
    assert src.shut and dst.shut


def test_close_ends_the_accept_loop(short_dir):
    fwd = forward.Forwarder(str(short_dir / "none.sock"), port=0).start()
    fwd.close()
    fwd.thread.join(5)
    assert not fwd.thread.is_alive()
