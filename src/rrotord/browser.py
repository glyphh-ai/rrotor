"""A browser signed in to Glyphh for this rrotor, to open its apps (docs/architecture.md, 5.8). rrotor is an OAuth
client of the Glyphh server: registered once (`POST /oauth/register`, a public client with PKCE, its id kept in
/data/oauth-client.json), it sends the browser to the server's `/oauth/authorize` with `resource` this runtime's
own `/mcp` (the resource its introspection is bound to), exchanges the code at `/oauth/token`, and keeps the tokens
in a **session** on /data (`/data/sessions/<id>.json`, private: the access token, the refresh token, the subject,
when the access token expires), named by the `rrotor_session` cookie (HttpOnly, SameSite Lax, Secure, 30 days).
The sign-in in flight travels in the `rrotor_signin` cookie, signed with a secret kept on /data (ten minutes). An
access token past its hour is refreshed with the refresh token; a session that cannot be refreshed is dropped. The
gate (api/gate.py) reads the session on the apps' paths and introspects its access token like a bearer."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

from rrotord.errors import BadRequest, Forbidden, Unavailable
from rrotord.store import read_json, write_json

SIGNIN_COOKIE = "rrotor_signin"
SESSION_COOKIE = "rrotor_session"
SIGNIN_TTL = 600  # seconds a sign-in in flight lives
SESSION_DAYS = 30
EARLY = 30.0  # seconds before the access token's expiry at which it is refreshed
SCOPE = "account"

PostJson = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[tuple[int, Any]]]
PostForm = Callable[[str, dict[str, str], dict[str, str]], Awaitable[tuple[int, Any]]]


@dataclass(slots=True)
class Session:
    id: str
    access_token: str
    refresh_token: str | None
    sub: str | None
    expires_at: float  # unix time the access token expires
    created_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "sub": self.sub,
            "expires_at": self.expires_at,
            "created_at": self.created_at,
        }


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def safe_next(value: str | None) -> str:
    """Where to go after the sign-in: a path on this host, nothing else (`/` otherwise)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


class Browser:
    def __init__(
        self,
        data: Path,
        server: str,
        address: str,
        name: str,
        post_json: PostJson,
        post_form: PostForm,
        clock: Callable[[], float] = time.time,
        secure: bool = True,
    ) -> None:
        self.data, self.server, self.address = data, server.rstrip("/"), address.rstrip("/")
        self.name, self.post_json, self.post_form, self.clock, self.secure = name, post_json, post_form, clock, secure
        self.sessions = data / "sessions"
        self.client_file = data / "oauth-client.json"
        self.secret_file = data / "browser-secret"
        self._client_id: str | None = None

    # The client

    def _secret(self) -> bytes:
        try:
            return _unb64(self.secret_file.read_text().strip())
        except FileNotFoundError:
            made = secrets.token_bytes(32)
            self.secret_file.parent.mkdir(parents=True, exist_ok=True)
            self.secret_file.touch(mode=0o600)
            self.secret_file.write_text(_b64(made))
            return made

    @property
    def redirect_uri(self) -> str:
        return f"{self.address}/auth/callback"

    @property
    def resource(self) -> str:
        return f"{self.address}/mcp"

    async def client_id(self) -> str:
        """This rrotor as a client of the Glyphh server: registered once, its id kept."""
        if self._client_id is None:
            kept = read_json(self.client_file)
            if isinstance(kept, dict) and isinstance(kept.get("client_id"), str):
                self._client_id = str(kept["client_id"])
            else:
                self._client_id = await self._register()
        return self._client_id

    async def _register(self) -> str:
        body = {
            "client_name": f"rrotor {self.name}",
            "redirect_uris": [self.redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
        try:
            status, answer = await self.post_json(f"{self.server}/oauth/register", {}, body)
        except Exception as err:  # noqa: BLE001  (the server or the network: no sign-in for now)
            raise Unavailable(f"Glyphh could not be reached to register this runtime ({type(err).__name__}).") from None
        if status != 201 or not isinstance(answer, dict) or not isinstance(answer.get("client_id"), str):
            raise Unavailable("Glyphh did not register this runtime as a client: sign in later.", status=status)
        write_json(self.client_file, {"client_id": answer["client_id"], "registered_at": self.clock()}, private=True)
        return str(answer["client_id"])

    # The sign-in in flight

    def _sign(self, payload: dict[str, Any]) -> str:
        raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
        return f"{raw}.{hmac.new(self._secret(), raw.encode(), hashlib.sha256).hexdigest()}"

    def _verify(self, value: str | None) -> dict[str, Any] | None:
        if not value:
            return None
        raw, _, mac = value.rpartition(".")
        if not raw or not hmac.compare_digest(mac, hmac.new(self._secret(), raw.encode(), hashlib.sha256).hexdigest()):
            return None
        try:
            found = json.loads(_unb64(raw))
        except ValueError:
            return None
        if not isinstance(found, dict) or found.get("exp", 0) < self.clock():
            return None
        return found

    async def start(self, request: web.Request) -> web.Response:
        """Off to Glyphh: a PKCE verifier and a state kept in the sign-in cookie, the browser sent to authorize."""
        verifier = _b64(secrets.token_bytes(48))
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        state = _b64(secrets.token_bytes(24))
        pending = {
            "v": verifier,
            "s": state,
            "n": safe_next(request.query.get("next")),
            "exp": self.clock() + SIGNIN_TTL,
        }
        query = {
            "response_type": "code",
            "client_id": await self.client_id(),
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": SCOPE,
            "resource": self.resource,
        }
        away = web.HTTPFound(f"{self.server}/oauth/authorize?{urlencode(query)}")
        away.set_cookie(
            SIGNIN_COOKIE,
            self._sign(pending),
            max_age=SIGNIN_TTL,
            httponly=True,
            secure=self.secure,
            samesite="Lax",
            path="/auth",
        )
        raise away

    async def callback(self, request: web.Request) -> web.Response:
        """Back from Glyphh with a code: exchanged for the tokens, a session made, the browser sent on to `next`."""
        pending = self._verify(request.cookies.get(SIGNIN_COOKIE))
        if pending is None:
            raise BadRequest("This sign-in expired or did not start here. Open the app again.", code="E_SIGNIN")
        if request.query.get("state") != pending["s"]:
            raise BadRequest("This sign-in did not start here. Open the app again.", code="E_SIGNIN")
        if "error" in request.query:
            raise Forbidden("Glyphh did not sign you in: " + str(request.query.get("error")), code="E_SIGNIN")
        code = request.query.get("code")
        if not code:
            raise BadRequest("Glyphh sent no code. Open the app again.", code="E_SIGNIN")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": await self.client_id(),
            "code_verifier": str(pending["v"]),
            "resource": self.resource,
        }
        tokens = await self._tokens(form)
        session = self._make(tokens)
        on = web.HTTPFound(str(pending["n"]))
        on.set_cookie(
            SESSION_COOKIE,
            session.id,
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            secure=self.secure,
            samesite="Lax",
            path="/",
        )
        on.del_cookie(SIGNIN_COOKIE, path="/auth")
        raise on

    async def _tokens(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            status, answer = await self.post_form(f"{self.server}/oauth/token", {"Accept": "application/json"}, form)
        except Exception as err:  # noqa: BLE001
            raise Unavailable(f"Glyphh could not be reached to sign you in ({type(err).__name__}).") from None
        if status != 200 or not isinstance(answer, dict) or not isinstance(answer.get("access_token"), str):
            raise Forbidden("Glyphh did not sign you in. Open the app again.", code="E_SIGNIN", status=403)
        return answer

    def _make(self, tokens: dict[str, Any]) -> Session:
        now = self.clock()
        expires = tokens.get("expires_in")
        session = Session(
            id=_b64(secrets.token_bytes(32)),
            access_token=str(tokens["access_token"]),
            refresh_token=tokens.get("refresh_token") if isinstance(tokens.get("refresh_token"), str) else None,
            sub=None,
            expires_at=now
            + (float(expires) if isinstance(expires, int | float) and not isinstance(expires, bool) else 3600.0),
            created_at=now,
        )
        self._save(session)
        return session

    # Sessions

    def _file(self, session_id: str) -> Path:
        return self.sessions / f"{session_id}.json"

    def _save(self, session: Session) -> None:
        write_json(self._file(session.id), session.to_json(), private=True)

    def _load(self, session_id: str) -> Session | None:
        if not session_id or not all(c.isalnum() or c in "-_" for c in session_id):
            return None
        kept = read_json(self._file(session_id))
        if not isinstance(kept, dict):
            return None
        return Session(
            session_id,
            str(kept["access_token"]),
            kept.get("refresh_token"),
            kept.get("sub"),
            float(kept["expires_at"]),
            float(kept["created_at"]),
        )

    def drop(self, session_id: str) -> None:
        self._file(session_id).unlink(missing_ok=True)

    async def session(self, request: web.Request) -> Session | None:
        """The request's session, its access token refreshed when it is about to expire; None when there is no
        session, or it cannot be refreshed (then it is dropped)."""
        session_id = request.cookies.get(SESSION_COOKIE, "")
        session = self._load(session_id)
        if session is None:
            return None
        if session.expires_at - EARLY > self.clock():
            return session
        if session.refresh_token is None:
            self.drop(session.id)
            return None
        form = {
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "client_id": await self.client_id(),
            "resource": self.resource,
        }
        try:
            tokens = await self._tokens(form)
        except (Forbidden, Unavailable):
            self.drop(session.id)
            return None
        fresh = tokens.get("refresh_token")
        session.access_token = str(tokens["access_token"])
        session.refresh_token = fresh if isinstance(fresh, str) else session.refresh_token
        expires = tokens.get("expires_in")
        session.expires_at = self.clock() + (
            float(expires) if isinstance(expires, int | float) and not isinstance(expires, bool) else 3600.0
        )
        self._save(session)
        return session

    def remember(self, session: Session, sub: str | None) -> None:
        """The subject the Cloud named for this session, kept with it (for the logs; introspection stays the gate)."""
        if sub is not None and session.sub != sub:
            session.sub = sub
            self._save(session)

    async def logout(self, request: web.Request) -> web.Response:
        session_id = request.cookies.get(SESSION_COOKIE, "")
        if session_id and all(c.isalnum() or c in "-_" for c in session_id):
            self.drop(session_id)
        gone = web.Response(
            text="<!doctype html><title>Signed out</title><p>You are signed out of this runtime.</p>",
            content_type="text/html",
        )
        gone.del_cookie(SESSION_COOKIE, path="/")
        return gone
