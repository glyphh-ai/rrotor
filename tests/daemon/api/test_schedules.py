"""The schedules over REST and MCP: listed with their next fire and last run, paused and resumed; a follower asks its
head for the list and refuses to pause; a rrotor with no scheduler says so."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from rrotord.api import forward
from tests.daemon.api.conftest import Api
from tests.daemon.api.test_mcp import rpc
from tests.daemon.api.test_routes import send
from tests.fakes import DONE_PROGRAM, install, manifest

SCHEDULES = [{"function": "start", "cron": "15 6 * * *", "input": {"day": "x"}}]


def deploy(api: Api) -> None:
    install(api.services.programs, manifest("sample", schedules=SCHEDULES), DONE_PROGRAM)


async def call(api: Api, tool_name: str, **arguments: Any) -> dict[str, Any]:
    status, body = await rpc(api, "tools/call", {"name": tool_name, "arguments": arguments})
    assert status == 200, body
    result: dict[str, Any] = body["result"]
    return result


async def test_list_pause_and_resume_over_rest(api):
    deploy(api)
    status, body = await send(api, "GET", "/v1/schedules")
    [listed] = body["schedules"]
    assert status == 200 and (listed["id"], listed["function"], listed["cron"], listed["tz"]) == (
        "sample/start",
        "start",
        "15 6 * * *",
        "UTC",
    )
    assert listed["input"] == {"day": "x"} and listed["next_at"].endswith("06:15:00Z") and listed["last"] is None
    status, body = await send(api, "POST", "/v1/schedules/sample/start/pause")
    assert (status, body["paused"], body["next_at"]) == (200, True, None)
    status, body = await send(api, "POST", "/v1/schedules/sample/start/resume")
    assert (status, body["paused"]) == (200, False) and body["next_at"] is not None
    status, body = await send(api, "POST", "/v1/schedules/sample/nothing/pause")
    assert (status, body["error"]["code"]) == (404, "E_SCHEDULE_MISSING")


async def test_the_mcp_tools(api):
    deploy(api)
    listed = await call(api, "schedules_list")
    assert listed["structuredContent"]["schedules"][0]["id"] == "sample/start"
    paused = await call(api, "schedule_pause", program="sample", name="start")
    assert paused["structuredContent"]["paused"] is True
    resumed = await call(api, "schedule_resume", program="sample", name="start")
    assert resumed["structuredContent"]["paused"] is False
    bad = await call(api, "schedule_pause", program="sample")
    assert bad["isError"] is True and bad["structuredContent"]["error"]["message"] == "Send {name}."


async def test_a_rrotor_with_no_scheduler_says_so(api):
    api.services.scheduler = None
    status, body = await send(api, "GET", "/v1/schedules")
    assert (status, body["error"]["code"]) == (503, "E_SCHEDULER")


async def test_a_follower_asks_its_head_and_refuses_to_pause(api):
    deploy(api)
    asked: list[str] = []

    async def relay(op: str, args: dict[str, Any], target: str | None = None) -> dict[str, Any]:
        asked.append(op)
        return {"result": {"schedules": [{"id": "sample/start", "paused": True}]}}

    api.services.cluster = cast(Any, SimpleNamespace(role="follower", member=True, state={"head": "n0"}, forward=relay))
    status, body = await send(api, "GET", "/v1/schedules")
    assert (status, body["schedules"][0]["paused"], asked) == (200, True, ["schedules"])
    status, body = await send(api, "POST", "/v1/schedules/sample/start/pause")
    assert (status, body["error"]["code"]) == (503, "E_NOT_HEAD")
    assert "Schedules are paused and resumed on the cluster's head (n0)" in body["error"]["message"]


async def test_the_head_answers_a_forwarded_list(api):
    deploy(api)
    got = await forward._serve(api.services, "schedules", {})
    assert got["schedules"][0]["id"] == "sample/start"
