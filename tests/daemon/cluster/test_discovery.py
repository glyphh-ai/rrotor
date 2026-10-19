"""Finding the members: a static list as given, or a name's addresses, resolved at most every 10 seconds."""

from __future__ import annotations

import logging
import socket
from typing import Any

from rrotord.cluster import discovery
from rrotord.cluster.discovery import Discovery


async def test_a_static_list_is_the_addresses():
    assert await Discovery(("rrotor-0:7778", "rrotor-1:7778"), None, 7778).addresses() == [
        "rrotor-0:7778",
        "rrotor-1:7778",
    ]


async def test_a_name_is_resolved_every_10_seconds_and_its_last_answer_kept_through_a_failure(monkeypatch, caplog):
    monkeypatch.setattr(discovery, "RESOLVE_EVERY", 10.0)
    now, answers = (
        [0.0],
        [
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 7778)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 7778, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 7778)),
            ],
            OSError("no such name"),
        ],
    )
    asked: list[tuple[str, int]] = []

    async def resolve(name: str, port: int) -> list[Any]:
        asked.append((name, port))
        answer = answers.pop(0)
        if isinstance(answer, OSError):
            raise answer
        assert isinstance(answer, list)
        return answer

    found = Discovery((), "rrotor-headless", 7778, resolve, clock=lambda: now[0])
    assert await found.addresses() == ["10.0.0.2:7778", "[fd00::1]:7778"]
    now[0] = 5.0
    assert await found.addresses() == ["10.0.0.2:7778", "[fd00::1]:7778"] and len(asked) == 1
    now[0] = 11.0
    with caplog.at_level(logging.WARNING):
        assert await found.addresses() == ["10.0.0.2:7778", "[fd00::1]:7778"]
    assert asked == [("rrotor-headless", 7778)] * 2 and "the peers' name did not resolve" in caplog.text


async def test_the_resolver_is_the_event_loops():
    infos = await discovery.getaddrinfo("127.0.0.1", 7778)
    assert infos[0][4][:2] == ("127.0.0.1", 7778)
