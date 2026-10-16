"""The egress proxy: the only network a run has. One unix socket per run, bound into its sandbox; an HTTP CONNECT
proxy that lets through port 443 of the hosts the manifest declares, checks that the TLS server name is the host
asked for, refuses addresses that resolve to loopback, private, link-local or other non-public ranges, and records
every connection, let through or refused, in the run's log (docs/architecture.md, section 4)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rrotor.http import host_allowed
from rrotord.sni import record_length, server_name

log = logging.getLogger("rrotor.proxy")

Resolve = Callable[[str, int], Awaitable[list[str]]]
Connect = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
Record = Callable[[dict[str, Any]], None]
WAIT = 10.0


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def connect(address: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(address, port)


def public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


class Gate:
    """One run's allowlist, and what happens to each connection it is asked for."""

    def __init__(
        self, hosts: frozenset[str], record: Record, resolver: Resolve = resolve, connector: Connect = connect
    ) -> None:
        self.hosts, self.record, self.resolver, self.connector = hosts, record, resolver, connector

    def _refuse(self, writer: asyncio.StreamWriter, host: str, why: str, status: str = "403 Forbidden") -> None:
        self.record({"proxy": "refused", "host": host, "why": why})
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), WAIT)
        method, _, rest = head.split(b"\r\n", 1)[0].decode("latin-1").partition(" ")
        target = rest.partition(" ")[0]
        if method != "CONNECT":
            return self._refuse(writer, target[:200], "only HTTPS (CONNECT) leaves a run")
        host, _, port = target.rpartition(":")
        host = host.strip("[]").lower()
        if port != "443":
            return self._refuse(writer, host, f"port {port or '?'}: only 443 leaves a run")
        if not host_allowed(host, self.hosts):
            return self._refuse(writer, host, "not in the model's hosts")
        try:
            addresses = [a for a in await asyncio.wait_for(self.resolver(host, 443), WAIT) if public(a)]
        except (OSError, TimeoutError):
            return self._refuse(writer, host, "the name does not resolve", "502 Bad Gateway")
        if not addresses:
            return self._refuse(writer, host, "resolves only to private or local addresses")
        try:
            up_reader, up_writer = await asyncio.wait_for(self.connector(addresses[0], 443), WAIT)
        except (OSError, TimeoutError):
            return self._refuse(writer, host, "could not connect", "502 Bad Gateway")
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        try:
            first = await asyncio.wait_for(self._client_hello(reader), WAIT)
        except BaseException:
            up_writer.close()  # the client never said hello: the upstream connection goes too
            raise
        name = server_name(first) if first else None
        if name != host:
            self.record({"proxy": "refused", "host": host, "why": f"TLS server name {name or 'missing'}"})
            up_writer.close()
            return None
        self.record({"proxy": "allowed", "host": host})
        up_writer.write(first)
        await up_writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        return None

    @staticmethod
    async def _client_hello(reader: asyncio.StreamReader) -> bytes:
        data = b""
        while True:
            size = record_length(data)
            if size is None:
                return b""
            if size and len(data) >= size:
                return data
            chunk = await reader.read(16384 + 5)
            if not chunk:
                return b""
            data += chunk


class EgressProxy:
    """The proxies of the runs going now, one socket each under `root`."""

    def __init__(self, root: Path, resolver: Resolve = resolve, connector: Connect = connect) -> None:
        self.root, self.resolver, self.connector = root, resolver, connector
        self.servers: dict[str, asyncio.Server] = {}

    async def open(self, run_id: str, hosts: frozenset[str], record: Record) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{run_id}.sock"
        path.unlink(missing_ok=True)
        gate = Gate(hosts, record, self.resolver, self.connector)
        self.servers[run_id] = await asyncio.start_unix_server(gate.handle, path=str(path))
        path.chmod(0o600)
        return path

    async def close(self, run_id: str) -> None:
        server = self.servers.pop(run_id, None)
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 1)
        (self.root / f"{run_id}.sock").unlink(missing_ok=True)
