"""`ctx.http`: HTTPS to the hosts the manifest declares, through the run's egress proxy. Standard library only.

Inside the sandbox the proxy is `127.0.0.1:3128` in the run's own network namespace (HTTPS_PROXY is set, so any
other library works the same way); the proxy is what enforces the hosts. This client refuses an undeclared host
before it asks, so the error says why."""

from __future__ import annotations

import json as jsonlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from rrotor.errors import ModelError

USER_AGENT = "rrotor/0.1 (+https://glyphh.ai)"


class HttpError(ModelError):
    code = "E_HTTP"


class HostUndeclared(ModelError):
    code = "E_HOST_UNDECLARED"


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.body)

    def raise_for_status(self) -> Response:
        if not self.ok:
            raise HttpError(f"HTTP {self.status}: {self.text[:200]}")
        return self


class Opener(Protocol):
    def open(self, fullurl: urllib.request.Request, data: bytes | None = None, timeout: float = 30.0) -> Any: ...


def host_allowed(host: str, hosts: frozenset[str]) -> bool:
    """An exact host, or one under a declared `*.example.com`."""
    host = host.lower().rstrip(".")
    return host in hosts or any(h.startswith("*.") and host.endswith(h[1:]) for h in hosts)


class Http:
    def __init__(self, hosts: frozenset[str], proxy: str | None, opener: Opener | None = None) -> None:
        self.hosts = frozenset(h.lower() for h in hosts)
        handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({"https": proxy} if proxy else {})]
        self._opener: Opener = opener if opener is not None else cast(Opener, urllib.request.build_opener(*handlers))

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> Response:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise HostUndeclared(f"Only https:// addresses can be reached, not {parts.scheme or 'this'}.")
        if not host_allowed(parts.hostname, self.hosts):
            raise HostUndeclared(f"{parts.hostname} is not in this model's hosts in glyphh.json.")
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = urllib.parse.urlunsplit(parts._replace(query=f"{parts.query}&{query}" if parts.query else query))
        sent = {"User-Agent": USER_AGENT, **(headers or {})}
        if json is not None:
            data = jsonlib.dumps(json).encode()
            sent.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=sent, method=method.upper())  # noqa: S310  (https only, above)
        try:
            with self._opener.open(req, timeout=timeout) as res:
                return Response(res.status, res.read(), dict(res.headers.items()))
        except urllib.error.HTTPError as err:
            return Response(err.code, err.read(), dict(err.headers.items()))
        except (urllib.error.URLError, OSError) as err:
            reason = getattr(err, "reason", err)
            raise HttpError(f"{parts.hostname} could not be reached: {reason}") from None

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Response:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any) -> Response:
        return self.request("PUT", url, **kw)

    def delete(self, url: str, **kw: Any) -> Response:
        return self.request("DELETE", url, **kw)
