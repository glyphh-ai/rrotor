import io
import urllib.error
import urllib.request
from email.message import Message
from typing import Any, cast

import pytest

from rrotor.http import USER_AGENT, HostUndeclared, Http, HttpError, Response, host_allowed


class Res:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status, self.body, self.headers = status, body, Message()
        for k, v in headers.items():
            self.headers[k] = v

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None


class Opener:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[tuple[Any, float]] = []

    def open(self, fullurl, data=None, timeout=30.0):
        self.requests.append((fullurl, timeout))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def client(answer: Any) -> tuple[Http, Opener]:
    opener = Opener(answer)
    return Http(frozenset({"API.example.com", "*.cdn.example.com"}), None, opener=opener), opener


def test_response_helpers():
    r = Response(201, b'{"a": 1}', {"X": "y"})
    assert r.ok and r.text == '{"a": 1}' and r.json() == {"a": 1}
    assert r.raise_for_status() is r


def test_response_raise_for_status_on_an_error():
    with pytest.raises(HttpError, match="HTTP 500: boom"):
        Response(500, b"boom").raise_for_status()


def test_host_allowed_exact_and_wildcard():
    hosts = frozenset({"a.example.com", "*.b.example.com"})
    assert host_allowed("A.example.com.", hosts)
    assert host_allowed("x.b.example.com", hosts)
    assert not host_allowed("c.example.com", hosts)


def test_only_https_is_reached():
    http, _ = client(None)
    with pytest.raises(HostUndeclared, match="not http"):
        http.get("http://api.example.com/")


def test_an_address_without_a_scheme_is_refused():
    http, _ = client(None)
    with pytest.raises(HostUndeclared, match="not this"):
        http.get("api.example.com")


def test_an_undeclared_host_is_refused_before_asking():
    http, opener = client(None)
    with pytest.raises(HostUndeclared, match=r"other\.example\.com"):
        http.get("https://other.example.com/")
    assert opener.requests == []


def test_get_with_params_and_headers():
    http, opener = client(Res(200, b"ok", {"Content-Type": "text/plain"}))
    r = http.get("https://api.example.com/x?a=1", params={"b": [2, 3]}, headers={"X-K": "v"}, timeout=5)
    req, timeout = opener.requests[0]
    assert req.full_url == "https://api.example.com/x?a=1&b=2&b=3"
    assert req.get_header("User-agent") == USER_AGENT and req.get_header("X-k") == "v"
    assert (timeout, r.status, r.body, r.headers) == (5, 200, b"ok", {"Content-Type": "text/plain"})


def test_params_on_an_address_without_a_query():
    http, opener = client(Res(200, b"", {}))
    http.delete("https://x.cdn.example.com/p", params={"a": 1})
    assert opener.requests[0][0].full_url == "https://x.cdn.example.com/p?a=1"
    assert opener.requests[0][0].get_method() == "DELETE"


def test_post_json_sets_the_body_and_type():
    http, opener = client(Res(200, b"", {}))
    http.post("https://api.example.com/", json={"a": 1})
    req = opener.requests[0][0]
    assert (req.data, req.get_header("Content-type"), req.get_method()) == (b'{"a": 1}', "application/json", "POST")


def test_put_raw_data():
    http, opener = client(Res(200, b"", {}))
    http.put("https://api.example.com/", data=b"raw")
    assert (opener.requests[0][0].data, opener.requests[0][0].get_method()) == (b"raw", "PUT")


def test_an_http_error_is_a_response():
    hdrs = Message()
    hdrs["X"] = "1"
    err = urllib.error.HTTPError("https://api.example.com/", 404, "nf", hdrs, io.BytesIO(b"gone"))
    http, _ = client(err)
    assert http.get("https://api.example.com/") == Response(404, b"gone", {"X": "1"})


def test_an_unreachable_host_raises_with_the_reason():
    http, _ = client(urllib.error.URLError("refused"))
    with pytest.raises(HttpError, match=r"api\.example\.com could not be reached: refused"):
        http.get("https://api.example.com/")


def test_an_os_error_raises_too():
    http, _ = client(OSError("down"))
    with pytest.raises(HttpError, match="down"):
        http.get("https://api.example.com/")


def test_the_default_opener_goes_through_the_proxy():
    http = Http(frozenset({"a.example.com"}), "http://127.0.0.1:3128")
    assert isinstance(http._opener, urllib.request.OpenerDirector)
    proxies: list[Any] = [h for h in cast(Any, http._opener).handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies[0].proxies == {"https": "http://127.0.0.1:3128"}


def test_the_default_opener_without_a_proxy_goes_direct():
    http = Http(frozenset(), None)
    handlers = http._opener.handlers  # type: ignore[attr-defined]
    assert not [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
