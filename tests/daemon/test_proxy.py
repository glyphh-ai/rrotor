"""The egress proxy on a real unix socket: a fake resolver answers public addresses, a fake connector reaches a local
echo server in place of the host's port 443, and the client sends a real TLS ClientHello."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rrotord import proxy
from rrotord.proxy import EgressProxy, Gate, public
from tests.daemon.test_sni import client_hello

HOSTS = frozenset({"api.example.com", "*.cdn.example.com"})


class Upstream:
    """The host's port 443: echoes what it gets after the ClientHello it was handed."""

    def __init__(self) -> None:
        self.got: list[bytes] = []
        self.server: asyncio.Server | None = None

    async def start(self) -> Upstream:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            self.got.append(data)
            writer.write(b"echo:" + data)
            await writer.drain()
        writer.close()

    async def connect(self, address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert self.server is not None
        self.asked = (address, port)
        return await asyncio.open_connection("127.0.0.1", self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()


def resolver(addresses: list[str] | BaseException):
    async def resolve(host: str, port: int) -> list[str]:
        if isinstance(addresses, BaseException):
            raise addresses
        return addresses

    return resolve


async def open_proxy(
    short_dir: Path,
    connector: Any,
    addresses: list[str] | BaseException = ["93.184.216.34"],  # noqa: B006
) -> tuple[EgressProxy, Path, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    egress = EgressProxy(short_dir / "proxy", resolver(addresses), connector)
    path = await egress.open("run1", HOSTS, records.append)
    return egress, path, records


async def ask(path: Path, request: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(request)
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    return reader, writer, head


@pytest.fixture
async def upstream():
    up = await Upstream().start()
    yield up
    await up.close()


def test_public_addresses():
    assert public("93.184.216.34") and public("2606:2800:220:1:248:1893:25c8:1946")
    assert not public("127.0.0.1") and not public("10.0.0.1") and not public("169.254.1.1")
    assert not public("::1") and not public("not-an-address")


async def test_a_declared_host_on_443_goes_through_with_its_server_name(short_dir, upstream):
    egress, path, records = await open_proxy(short_dir, upstream.connect)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    reader, writer, head = await ask(path, b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    hello = client_hello("api.example.com")
    writer.write(hello)
    await writer.drain()
    assert await reader.readexactly(len(hello) + 5) == b"echo:" + hello
    writer.write(b"more")
    await writer.drain()
    assert await reader.readexactly(9) == b"echo:more"
    assert upstream.asked == ("93.184.216.34", 443)
    assert records == [{"proxy": "allowed", "host": "api.example.com"}]
    writer.close()
    await egress.close("run1")
    assert not path.exists()


async def test_a_client_hello_in_pieces_is_read_whole(short_dir, upstream):
    egress, path, records = await open_proxy(short_dir, upstream.connect)
    reader, writer, _ = await ask(path, b"CONNECT x.cdn.example.com:443 HTTP/1.1\r\n\r\n")
    hello = client_hello("x.cdn.example.com")
    writer.write(hello[:3])
    await writer.drain()
    await asyncio.sleep(0.05)
    writer.write(hello[3:])
    await writer.drain()
    assert (await reader.readexactly(5)) == b"echo:"
    assert records == [{"proxy": "allowed", "host": "x.cdn.example.com"}]
    writer.close()
    await egress.close("run1")


async def refused(short_dir: Path, request: bytes, connector: Any = None, addresses: Any = ["93.184.216.34"]):  # noqa: B006
    egress, path, records = await open_proxy(short_dir, connector, addresses)
    reader, writer, head = await ask(path, request)
    assert await reader.read() == b""
    writer.close()
    await egress.close("run1")
    return head, records


async def test_plain_http_is_refused(short_dir):
    head, records = await refused(short_dir, b"GET http://api.example.com/ HTTP/1.1\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 403")
    assert records == [
        {"proxy": "refused", "host": "http://api.example.com/", "why": "only HTTPS (CONNECT) leaves a run"}
    ]


async def test_a_port_other_than_443_is_refused(short_dir):
    _, records = await refused(short_dir, b"CONNECT api.example.com:80 HTTP/1.1\r\n\r\n")
    assert records[0]["why"] == "port 80: only 443 leaves a run"


async def test_a_target_without_a_port_is_refused(short_dir):
    _, records = await refused(short_dir, b"CONNECT api.example.com: HTTP/1.1\r\n\r\n")
    assert records[0]["why"] == "port ?: only 443 leaves a run"


async def test_an_undeclared_host_is_refused(short_dir):
    _, records = await refused(short_dir, b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n")
    assert records == [{"proxy": "refused", "host": "::1", "why": "not in the program's hosts"}]


async def test_a_name_that_does_not_resolve_is_a_bad_gateway(short_dir):
    head, records = await refused(short_dir, b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n", addresses=OSError("nx"))
    assert head.startswith(b"HTTP/1.1 502") and records[0]["why"] == "the name does not resolve"


async def test_a_name_that_resolves_only_to_private_addresses_is_refused(short_dir):
    _, records = await refused(
        short_dir, b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n", addresses=["127.0.0.1", "10.1.2.3"]
    )
    assert records[0]["why"] == "resolves only to private or local addresses"


async def test_a_host_that_cannot_be_reached_is_a_bad_gateway(short_dir):
    async def down(address: str, port: int):
        raise ConnectionRefusedError

    head, records = await refused(short_dir, b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n", connector=down)
    assert head.startswith(b"HTTP/1.1 502") and records[0]["why"] == "could not connect"


async def tunnel_then(short_dir: Path, upstream: Upstream, first: bytes, close: bool = False) -> list[dict[str, Any]]:
    egress, path, records = await open_proxy(short_dir, upstream.connect)
    reader, writer, _ = await ask(path, b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n")
    writer.write(first)
    await writer.drain()
    if close:
        writer.write_eof()
    assert await reader.read() == b""
    writer.close()
    await egress.close("run1")
    return records


async def test_a_different_server_name_is_refused(short_dir, upstream):
    records = await tunnel_then(short_dir, upstream, client_hello("evil.example.com"))
    assert records == [{"proxy": "refused", "host": "api.example.com", "why": "TLS server name evil.example.com"}]
    assert upstream.got == []


async def test_bytes_that_are_not_tls_are_refused(short_dir, upstream):
    records = await tunnel_then(short_dir, upstream, b"GET / HTTP/1.1\r\n\r\n")
    assert records[0]["why"] == "TLS server name missing"


async def test_a_client_that_goes_before_its_hello_is_refused(short_dir, upstream):
    records = await tunnel_then(short_dir, upstream, b"\x16\x03", close=True)
    assert records[0]["why"] == "TLS server name missing"


async def test_a_request_head_never_finished_is_dropped(short_dir, monkeypatch):
    monkeypatch.setattr(proxy, "WAIT", 0.1)
    egress, path, records = await open_proxy(short_dir, None)
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(b"CONNECT api")
    await writer.drain()
    assert await reader.read() == b""
    assert records == []
    writer.close()
    await egress.close("run1")


async def test_closing_a_run_that_has_no_proxy(short_dir):
    await EgressProxy(short_dir / "proxy").close("none")


async def test_the_default_resolver_and_connector(upstream):
    assert "127.0.0.1" in await proxy.resolve("localhost", 443)
    port = upstream.server.sockets[0].getsockname()[1]
    _, writer = await proxy.connect("127.0.0.1", port)
    writer.close()


async def test_a_pipe_whose_writer_fails_closes_it():
    class Writer:
        closed = False

        def write(self, data: bytes) -> None:
            raise ConnectionResetError

        def close(self) -> None:
            self.closed = True

    reader = asyncio.StreamReader()
    reader.feed_data(b"x")
    w = Writer()
    await proxy._pipe(reader, w)  # type: ignore[arg-type]
    assert w.closed


async def test_gate_handles_a_writer_that_fails_to_close():
    class Writer:
        def write(self, data: bytes) -> None:
            pass

        def close(self) -> None:
            raise RuntimeError("closed already")

    reader = asyncio.StreamReader()
    reader.feed_data(b"GET / HTTP/1.1\r\n\r\n")
    records: list[dict[str, Any]] = []
    await Gate(HOSTS, records.append).handle(reader, Writer())  # type: ignore[arg-type]
    assert records[0]["proxy"] == "refused"


async def test_a_client_that_never_says_hello_closes_the_upstream_too(short_dir, upstream, monkeypatch):
    monkeypatch.setattr(proxy, "WAIT", 0.2)
    closed = asyncio.Event()

    async def connector(address: str, port: int):
        r, w = await upstream.connect(address, port)
        real = w.close

        def close() -> None:
            closed.set()
            real()

        w.close = close
        return r, w

    egress, path, records = await open_proxy(short_dir, connector)
    reader, writer, head = await ask(path, b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    assert await reader.read() == b""
    assert closed.is_set() and records == []
    writer.close()
    await egress.close("run1")
