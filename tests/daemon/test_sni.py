import contextlib
import ssl

import pytest

from rrotord.sni import Hello, record_length, server_name


def client_hello(host: str | None) -> bytes:
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    ctx = ssl.create_default_context()
    if host is None:
        ctx.check_hostname = False
    tls = ctx.wrap_bio(incoming, outgoing, server_hostname=host)
    with contextlib.suppress(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def test_server_name_of_a_real_client_hello():
    assert server_name(client_hello("API.Example.com")) == "api.example.com"


def test_no_server_name_when_none_is_sent():
    assert server_name(client_hello(None)) is None


def test_record_length():
    hello = client_hello("a.example.com")
    assert record_length(hello[:3]) == 0
    assert record_length(hello) == len(hello)
    assert record_length(b"GET / HTTP/1.1") is None
    assert record_length(b"\x16\x02\x00\x00\x00") is None


def test_not_a_handshake_or_not_a_client_hello():
    hello = client_hello("a.example.com")
    assert server_name(b"\x17" + hello[1:]) is None
    assert server_name(hello[:5] + b"\x02" + hello[6:]) is None


def test_a_cut_record_has_no_name():
    assert server_name(client_hello("a.example.com")[:60]) is None


def test_a_name_that_is_not_ascii_is_refused():
    hello = bytearray(client_hello("aaaa.example.com"))
    at = hello.find(b"aaaa.example.com")
    hello[at] = 0xFF
    assert server_name(bytes(hello)) is None


def test_other_name_types_are_skipped():
    hello = bytearray(client_hello("aaaa.example.com"))
    at = hello.find(b"aaaa.example.com")
    hello[at - 3] = 1  # the name's type: not host_name
    assert server_name(bytes(hello)) is None


def test_hello_reader_refuses_reading_past_the_end():
    h = Hello(b"\x00\x01")
    assert h.number(2) == 1
    with pytest.raises(ValueError, match="short"):
        h.take(1)
