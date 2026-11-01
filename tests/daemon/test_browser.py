"""A browser's Glyphh sign-in for this rrotor's apps: rrotor registered once as a client, the sign-in in flight in a
signed cookie, the code exchanged for a session on /data, the access token refreshed, the session dropped when it
cannot be, and signed out. The Glyphh server is faked at the HTTP layer."""

from __future__ import annotations

import hashlib
import hmac
import json
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from rrotord import browser as b
from rrotord.errors import BadRequest, Forbidden, Unavailable

SERVER = "https://glyphh.example.com"
ADDRESS = "https://rrotor.acme.com"


class Glyphh:
    """The server's OAuth endpoints, as rrotor calls them: what each answers, and what it was sent."""

    def __init__(self) -> None:
        self.register: list[tuple[int, Any] | Exception] = [(201, {"client_id": "client-1"})]
        self.token: list[tuple[int, Any] | Exception] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        self.sent.append((url, body))
        answer = self.register.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def post_form(self, url: str, headers: dict[str, str], form: dict[str, str]) -> tuple[int, Any]:
        self.sent.append((url, form))
        answer = self.token.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def cookies_of(response: web.Response) -> dict[str, str]:
    """The cookies a response sets, by value (a response not yet sent holds them in `cookies`, not its headers)."""
    jar: SimpleCookie = response.cookies
    return {k: v.value for k, v in jar.items()}


def request(path: str, cookies: dict[str, str] | None = None, method: str = "GET") -> web.Request:
    headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())} if cookies else {}
    return make_mocked_request(method, path, headers=headers)


def fresh(tmp_path: Path, clock: list[float] | None = None) -> tuple[b.Browser, Glyphh]:
    glyphh = Glyphh()
    now = clock or [1000.0]
    made = b.Browser(
        tmp_path, SERVER + "/", ADDRESS + "/", "rrotor-1", glyphh.post_json, glyphh.post_form, lambda: now[0]
    )
    return made, glyphh


async def test_rrotor_registers_itself_once_and_keeps_its_client_id(tmp_path):
    made, glyphh = fresh(tmp_path)
    assert await made.client_id() == "client-1" and await made.client_id() == "client-1"
    url, body = glyphh.sent[0]
    assert url == f"{SERVER}/oauth/register" and body["redirect_uris"] == [f"{ADDRESS}/auth/callback"]
    assert body["token_endpoint_auth_method"] == "none" and body["client_name"] == "rrotor rrotor-1"
    again, _ = fresh(tmp_path)
    assert await again.client_id() == "client-1" and len(glyphh.sent) == 1  # kept in /data/oauth-client.json
    assert json.loads((tmp_path / "oauth-client.json").read_text())["client_id"] == "client-1"


async def test_a_registration_refused_or_unreachable_is_unavailable(tmp_path):
    for answer in ((400, {"error": "invalid_client_metadata"}), (201, "junk"), OSError("down")):
        made, glyphh = fresh(tmp_path / str(id(answer)))
        glyphh.register = [answer]
        with pytest.raises(Unavailable):
            await made.client_id()


async def test_the_sign_in_goes_to_glyphh_with_pkce_and_comes_back_to_a_session(tmp_path):
    now = [1000.0]
    made, glyphh = fresh(tmp_path, now)
    with pytest.raises(web.HTTPFound) as away:
        await made.start(request("/auth/start?next=/apps/board/"))
    location = urlsplit(str(away.value.location))
    q = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{SERVER}/oauth/authorize"
    assert (q["client_id"], q["redirect_uri"], q["resource"], q["code_challenge_method"], q["scope"]) == (
        ["client-1"],
        [f"{ADDRESS}/auth/callback"],
        [f"{ADDRESS}/mcp"],
        ["S256"],
        ["account"],
    )
    signin = cookies_of(away.value)[b.SIGNIN_COOKIE]
    glyphh.token = [(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600})]
    with pytest.raises(web.HTTPFound) as back:
        await made.callback(request(f"/auth/callback?code=c-1&state={q['state'][0]}", {b.SIGNIN_COOKIE: signin}))
    assert back.value.location == "/apps/board/"
    url, form = glyphh.sent[-1]
    assert url == f"{SERVER}/oauth/token" and form["grant_type"] == "authorization_code" and form["code"] == "c-1"
    assert form["client_id"] == "client-1" and form["resource"] == f"{ADDRESS}/mcp" and len(form["code_verifier"]) > 40
    session_id = cookies_of(back.value)[b.SESSION_COOKIE]
    morsel = back.value.cookies[b.SESSION_COOKIE]
    assert (morsel["httponly"], morsel["secure"], morsel["samesite"], morsel["path"]) == (True, True, "Lax", "/")
    assert away.value.cookies[b.SIGNIN_COOKIE]["path"] == "/auth" and away.value.cookies[b.SIGNIN_COOKIE]["httponly"]
    session = await made.session(request("/apps/board/", {b.SESSION_COOKIE: session_id}))
    assert session is not None and session.access_token == "at-1" and session.expires_at == 4600.0
    assert (tmp_path / "sessions" / f"{session_id}.json").stat().st_mode & 0o777 == 0o600
    made.remember(session, "user_1")
    made.remember(session, "user_1")
    assert made._load(session_id).sub == "user_1"  # type: ignore[union-attr]
    # Past the hour: refreshed with the refresh token; the new refresh token kept
    now[0] = 4590.0
    glyphh.token = [(200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 7200})]
    refreshed = await made.session(request("/apps/board/", {b.SESSION_COOKIE: session_id}))
    assert refreshed is not None and (refreshed.access_token, refreshed.refresh_token) == ("at-2", "rt-2")
    assert glyphh.sent[-1][1]["grant_type"] == "refresh_token" and glyphh.sent[-1][1]["refresh_token"] == "rt-1"
    assert refreshed.expires_at == 4590.0 + 7200 and refreshed.sub == "user_1"
    # A refresh that only answers an access token keeps the refresh token it had, and an odd expires_in is an hour
    now[0] = 4590.0 + 7200
    glyphh.token = [(200, {"access_token": "at-3", "expires_in": "soon"})]
    kept = await made.session(request("/apps/board/", {b.SESSION_COOKIE: session_id}))
    assert kept is not None and (kept.refresh_token, kept.expires_at) == ("rt-2", now[0] + 3600)
    # Signed out: the session is gone
    gone = await made.logout(request("/auth/logout", {b.SESSION_COOKIE: session_id}, "POST"))
    assert gone.status == 200 and gone.cookies[b.SESSION_COOKIE].value == ""
    assert await made.session(request("/apps/board/", {b.SESSION_COOKIE: session_id})) is None
    assert (await made.logout(request("/auth/logout", method="POST"))).status == 200  # nothing to drop


async def test_a_session_that_cannot_be_refreshed_is_dropped(tmp_path):
    now = [1000.0]
    made, glyphh = fresh(tmp_path, now)
    one = made._make({"access_token": "at", "refresh_token": "rt", "expires_in": 60})
    two = made._make({"access_token": "at"})  # no refresh token, an hour
    assert two.refresh_token is None and two.expires_at == 4600.0
    now[0] = 1100.0
    glyphh.token = [(400, {"error": "invalid_grant"})]
    assert await made.session(request("/x", {b.SESSION_COOKIE: one.id})) is None
    assert not (tmp_path / "sessions" / f"{one.id}.json").exists()
    now[0] = 5000.0
    assert await made.session(request("/x", {b.SESSION_COOKIE: two.id})) is None
    three = made._make({"access_token": "at", "refresh_token": "rt", "expires_in": 1})
    now[0] = 5100.0
    glyphh.token = [OSError("down")]
    assert await made.session(request("/x", {b.SESSION_COOKIE: three.id})) is None
    assert (
        await made.session(request("/x")) is None
        and await made.session(request("/x", {b.SESSION_COOKIE: "../x"})) is None
    )
    assert await made.session(request("/x", {b.SESSION_COOKIE: "unknown"})) is None


async def test_a_callback_that_did_not_start_here_or_was_refused_is_refused(tmp_path):
    now = [1000.0]
    made, glyphh = fresh(tmp_path, now)
    with pytest.raises(web.HTTPFound) as away:
        await made.start(request("/auth/start?next=//evil.example.com"))
    signin = cookies_of(away.value)[b.SIGNIN_COOKIE]
    state = parse_qs(urlsplit(str(away.value.location)).query)["state"][0]
    with pytest.raises(BadRequest, match="did not start here"):
        await made.callback(request("/auth/callback?code=c&state=other", {b.SIGNIN_COOKIE: signin}))
    with pytest.raises(BadRequest, match="expired or did not start here"):
        await made.callback(request(f"/auth/callback?code=c&state={state}"))
    with pytest.raises(BadRequest, match="expired or did not start here"):
        await made.callback(request(f"/auth/callback?code=c&state={state}", {b.SIGNIN_COOKIE: signin + "x"}))
    with pytest.raises(BadRequest, match="expired or did not start here"):
        await made.callback(request(f"/auth/callback?code=c&state={state}", {b.SIGNIN_COOKIE: "no-dot"}))
    with pytest.raises(Forbidden, match="access_denied"):
        await made.callback(request(f"/auth/callback?error=access_denied&state={state}", {b.SIGNIN_COOKIE: signin}))
    with pytest.raises(BadRequest, match="no code"):
        await made.callback(request(f"/auth/callback?state={state}", {b.SIGNIN_COOKIE: signin}))
    glyphh.token = [(400, {"error": "invalid_grant"})]
    with pytest.raises(Forbidden, match="did not sign you in"):
        await made.callback(request(f"/auth/callback?code=c&state={state}", {b.SIGNIN_COOKIE: signin}))
    glyphh.token = [OSError("down")]
    with pytest.raises(Unavailable):
        await made.callback(request(f"/auth/callback?code=c&state={state}", {b.SIGNIN_COOKIE: signin}))
    glyphh.token = [(200, {"access_token": "at"})]
    with pytest.raises(web.HTTPFound) as back:  # an unsafe next goes to /
        await made.callback(request(f"/auth/callback?code=c&state={state}", {b.SIGNIN_COOKIE: signin}))
    assert back.value.location == "/"
    now[0] += b.SIGNIN_TTL + 1
    with pytest.raises(BadRequest, match="expired"):
        await made.callback(request(f"/auth/callback?code=c&state={state}", {b.SIGNIN_COOKIE: signin}))
    tampered = made._sign({"not": "json"})[:-4] + "zzzz"
    assert made._verify(tampered) is None
    assert made._verify(made._sign({"exp": 0})) is None
    raw = b._b64(b"not json at all")  # signed, yet not a note rrotor wrote
    assert made._verify(f"{raw}.{hmac.new(made._secret(), raw.encode(), hashlib.sha256).hexdigest()}") is None


def test_safe_next():
    assert b.safe_next("/apps/x/?v=1") == "/apps/x/?v=1"
    for bad in (None, "", "https://evil", "//evil", "/a\\b", "apps"):
        assert b.safe_next(bad) == "/"
