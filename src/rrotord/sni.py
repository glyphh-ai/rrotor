"""The server name a TLS ClientHello asks for (RFC 8446 and RFC 6066, server_name), read from the first record."""

from __future__ import annotations


class Hello:
    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def take(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise ValueError("short")
        out = self.data[self.at : self.at + n]
        self.at += n
        return out

    def number(self, n: int) -> int:
        return int.from_bytes(self.take(n), "big")


def record_length(head: bytes) -> int | None:
    """The whole first record's size once its 5-byte header is in; None if it is not a TLS handshake."""
    if len(head) < 5:
        return 0
    if head[0] != 22 or head[1] != 3:
        return None
    return 5 + int.from_bytes(head[3:5], "big")


def server_name(record: bytes) -> str | None:
    """The host name a ClientHello names, or None (not a ClientHello, or no server_name)."""
    try:
        r = Hello(record)
        if r.number(1) != 22:
            return None
        r.take(4)  # version, length
        if r.number(1) != 1:
            return None
        r.take(3 + 2 + 32)  # length, client version, random
        r.take(r.number(1))  # session id
        r.take(r.number(2))  # cipher suites
        r.take(r.number(1))  # compression methods
        end = r.number(2) + r.at
        while r.at < end:
            kind, size = r.number(2), r.number(2)
            body = Hello(r.take(size))
            if kind != 0:
                continue
            listed = body.number(2) + body.at
            while body.at < listed:
                name_type, name = body.number(1), body.take(body.number(2))
                if name_type == 0:
                    return name.decode("ascii").lower()
    except (ValueError, UnicodeDecodeError):
        return None
    return None
