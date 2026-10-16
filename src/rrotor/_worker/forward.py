"""The proxy's door inside the sandbox: 127.0.0.1:3128 in the run's own network namespace, each connection piped to
the run's egress proxy, a unix socket the runner bound in. Models' HTTPS_PROXY points here, so any library works,
and the proxy outside decides what passes."""

from __future__ import annotations

import contextlib
import socket
import threading

PORT: int = 3128
STACK = 512 * 1024  # small thread stacks: a run's memory limit counts them


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while chunk := src.recv(65536):
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)


def _serve(client: socket.socket, target: str) -> None:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(target)
    except OSError:
        client.close()
        upstream.close()
        return
    threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
    _pipe(upstream, client)
    client.close()
    upstream.close()


class Forwarder:
    def __init__(self, target: str, port: int = PORT) -> None:
        self.target = target
        self.server = socket.create_server(("127.0.0.1", port))
        self.port: int = self.server.getsockname()[1]

    def start(self) -> Forwarder:
        threading.stack_size(STACK)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        return self

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=_serve, args=(client, self.target), daemon=True).start()

    def close(self) -> None:
        with contextlib.suppress(OSError):  # wakes a blocked accept on Linux; macOS refuses it on a listener
            self.server.shutdown(socket.SHUT_RDWR)
        self.server.close()
