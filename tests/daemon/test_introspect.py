"""A sign-in checked with the Cloud: asked once a minute per token, good or bad; nothing asked without the runtime's
own token; the Cloud unreachable is a no."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from rrotord.introspect import KEEP, Introspector
from rrotord.link.credentials import Credentials


class Cloud:
    def __init__(self, answers: list[tuple[int, Any] | OSError]) -> None:
        self.answers: list[tuple[int, Any] | OSError] = list(answers)
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        self.calls.append((url, headers, body))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def credentials(tmp_path: Path, token: str | None = "tok_" + "a" * 20) -> Credentials:
    c = Credentials(tmp_path)
    if token:
        c.set(str(uuid.uuid4()), token)
    return c


async def test_an_answer_is_kept_a_minute_good_or_bad(tmp_path):
    now = [100.0]
    cloud = Cloud([(200, {"active": True}), (200, {"active": False}), (200, {"active": True})])
    i = Introspector("https://glyphh.example.com/", credentials(tmp_path), cloud.post, clock=lambda: now[0])
    assert await i.active("good") is True and await i.active("good") is True
    assert cloud.calls == [
        (
            "https://glyphh.example.com/v1/runtime/introspect",
            {"Authorization": "Bearer tok_" + "a" * 20},
            {"token": "good"},
        )
    ]
    assert await i.active("bad") is False and await i.active("bad") is False and len(cloud.calls) == 2
    now[0] += 61
    assert await i.active("good") is True and len(cloud.calls) == 3  # asked again once the minute is up
    i.forget()
    assert i.answers == {}


async def test_no_runtime_token_the_cloud_down_or_an_odd_answer_is_a_no(tmp_path):
    cloud = Cloud([])
    assert await Introspector("https://c", credentials(tmp_path, None), cloud.post).active("t") is False
    assert cloud.calls == []
    for answer in (OSError("down"), (401, {"error": "x"}), (200, "junk"), (200, {"active": "yes"})):
        i = Introspector("https://c", credentials(tmp_path), Cloud([answer]).post)
        assert await i.active("t") is False


async def test_the_oldest_answers_go_when_too_many_are_kept(tmp_path):
    cloud = Cloud([(200, {"active": True})] * (KEEP + 1))
    now = [0.0]
    i = Introspector("https://c", credentials(tmp_path), cloud.post, clock=lambda: now[0])
    for n in range(KEEP + 1):
        now[0] += 0.001
        await i.active(f"t{n}")
    assert len(i.answers) == KEEP - KEEP // 10 + 1


async def test_the_subject_signed_in_is_kept_with_the_answer(tmp_path):
    cloud = Cloud([(200, {"active": True, "sub": "user_1"}), (200, {"active": True, "sub": 7})])
    i = Introspector("https://c", credentials(tmp_path), cloud.post)
    assert await i.check("a") == (True, "user_1") and await i.check("a") == (True, "user_1")
    assert await i.check("b") == (True, None) and len(cloud.calls) == 2
