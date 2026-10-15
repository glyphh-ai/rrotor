from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rrotord.api.errors import errors
from rrotord.errors import Conflict


async def client_for(handler) -> TestClient[web.Request, web.Application]:
    app = web.Application(middlewares=[errors])
    app.router.add_get("/x", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def answer(handler, method: str = "GET") -> tuple[int, object]:
    client = await client_for(handler)
    try:
        res = await client.request(method, "/x", allow_redirects=False)
        body = await res.json() if res.content_type == "application/json" else None
        return res.status, body
    finally:
        await client.close()


async def test_an_rrotor_error_is_its_json():
    async def h(_):
        raise Conflict("ended", code="E_RUN_STATE")

    assert await answer(h) == (409, {"error": {"code": "E_RUN_STATE", "message": "ended"}})


async def test_an_http_error_gets_a_stable_code():
    async def h(_):
        raise web.HTTPRequestEntityTooLarge(max_size=1, actual_size=2)

    status, body = await answer(h)
    assert status == 413 and body["error"]["code"] == "E_TOO_BIG"  # type: ignore[index]


async def test_an_unknown_http_error_is_a_bad_request():
    async def h(_):
        raise web.HTTPUnprocessableEntity()

    status, body = await answer(h)
    assert status == 422 and body["error"]["code"] == "E_BAD_REQUEST"  # type: ignore[index]


async def test_a_wrong_method_and_a_missing_route():
    async def h(_):
        return web.json_response({})

    assert (await answer(h, "POST"))[1]["error"]["code"] == "E_METHOD"  # type: ignore[index]
    client = await client_for(h)
    try:
        res = await client.get("/nope")
        assert (await res.json())["error"]["code"] == "E_NOT_FOUND"
    finally:
        await client.close()


async def test_a_redirect_is_not_an_error():
    async def h(_):
        raise web.HTTPFound("/elsewhere")

    assert (await answer(h))[0] == 302


async def test_anything_else_is_internal_with_no_detail(caplog):
    async def h(_):
        raise ValueError("secret detail")

    status, body = await answer(h)
    assert status == 500 and body == {"error": {"code": "E_INTERNAL", "message": "Something went wrong in rrotor."}}
    assert "local API error" in caplog.text
