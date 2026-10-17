"""Errors with stable codes: the local API answers them as {"error": {"code", "message", ...}}."""

from __future__ import annotations

from typing import Any


class RrotorError(Exception):
    code: str = "E_INTERNAL"
    status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        **detail: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.headers = headers or {}
        self.detail = detail

    def to_json(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.detail}}


class BadRequest(RrotorError):
    code, status = "E_BAD_REQUEST", 400


class Unauthorized(RrotorError):
    code, status = "E_AUTH", 401


class Forbidden(RrotorError):
    code, status = "E_FORBIDDEN", 403


class NotFound(RrotorError):
    code, status = "E_NOT_FOUND", 404


class Conflict(RrotorError):
    code, status = "E_CONFLICT", 409


class DeployError(RrotorError):
    code, status = "E_DEPLOY", 422


class Unavailable(RrotorError):
    code, status = "E_UNAVAILABLE", 503
