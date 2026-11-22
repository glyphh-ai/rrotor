"""The Claude API's Messages endpoint as glyphh.bots.ApiClaude uses it (`client.messages.stream(...)` and
`get_final_message()`), over the run's `ctx.http`: the image carries no `anthropic` package, and every request goes
through the run's egress proxy to api.anthropic.com. One request a step (not streamed: a bot's step has no tools, so
it is one turn); an overloaded or rate-limited API is asked again, twice."""

from __future__ import annotations

import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

API = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"
AGAIN = frozenset({429, 500, 502, 503, 504, 529})  # asked again, after 2 s, then 4 s
TRIES = 3


class ClaudeApiError(Exception):
    """The API refused or failed the request (ApiClaude fails the step with it, said plainly)."""


class Messages:
    """`anthropic.Anthropic`, as far as ApiClaude needs it: `messages.stream(**kw)`. `http` is the run's ctx.http,
    `key` the org's Anthropic API key (the program's secret ANTHROPIC_API_KEY)."""

    def __init__(self, http: Any, key: str, sleep: Callable[[float], None] = time.sleep) -> None:
        self._http, self._key, self._sleep = http, key, sleep
        self.messages = self

    def __repr__(self) -> str:
        return "Messages(api.anthropic.com)"  # never the key

    def stream(self, **kw: Any) -> _Answer:
        return _Answer(self, kw)

    def create(self, **kw: Any) -> Any:
        timeout = float(kw.pop("timeout", 300.0))
        headers = {"x-api-key": self._key, "anthropic-version": VERSION, "content-type": "application/json"}
        for attempt in range(TRIES):
            res = self._http.post(API, json=kw, headers=headers, timeout=timeout)
            if res.ok:
                return _message(res.json())
            if res.status not in AGAIN or attempt == TRIES - 1:
                break
            self._sleep(2.0 * 2**attempt)
        try:
            said = res.json().get("error", {}).get("message") or res.text[:300]
        except (ValueError, AttributeError):
            said = res.text[:300]
        raise ClaudeApiError(f"HTTP {res.status}: {said}")


class _Answer:
    """What `messages.stream(...)` gives: a context whose `get_final_message()` is the API's whole answer."""

    def __init__(self, client: Messages, kw: dict[str, Any]) -> None:
        self._client, self._kw = client, kw

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._client.create(**self._kw)


def _message(body: dict[str, Any]) -> Any:
    usage = body.get("usage") or {}
    blocks = [SimpleNamespace(**b) for b in body.get("content") or [] if isinstance(b, dict)]
    return SimpleNamespace(
        content=blocks,
        stop_reason=body.get("stop_reason"),
        model=body.get("model"),
        usage=SimpleNamespace(input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0)),
    )
