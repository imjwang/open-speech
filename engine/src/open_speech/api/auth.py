"""Loopback API protection (03 § Security, R10).

- Bearer token on every REST call and on the WS upgrade (header, or `?token=` for clients that can't set
  headers).
- `Host` must be 127.0.0.1:<port> or localhost:<port> (defeats DNS rebinding).
- Browser `Origin`s other than the shell's (`app://open-speech`) are rejected; there is no permissive CORS.
  Dev/test origins can be added explicitly with OPEN_SPEECH_ALLOWED_ORIGINS.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost"}


def _problem(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": f"https://open-speech.dev/problems/{code}",
            "title": code.replace("_", " ").capitalize(),
            "status": status,
            "detail": detail,
            "code": code,
        },
        status_code=status,
        media_type="application/problem+json",
    )


def host_ok(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header
    return host in ALLOWED_HOSTNAMES


def token_ok(expected: str, authorization: str | None, query_token: str | None) -> bool:
    supplied = None
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    elif query_token:
        supplied = query_token
    return supplied is not None and hmac.compare_digest(supplied, expected)


class LoopbackGuard(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, token: str, allowed_origins: set[str], open_paths: set[str]) -> None:
        super().__init__(app)
        self.token = token
        self.allowed_origins = allowed_origins
        self.open_paths = open_paths

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not host_ok(request.headers.get("host")):
            return _problem(421, "invalid_request", "Requests must target 127.0.0.1 or localhost.")
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            return _problem(403, "invalid_request", "Origin not allowed.")
        if request.method == "OPTIONS":
            # CORS preflight from an allowed origin: answer without a token (browsers never send one here).
            return self._cors(Response(status_code=204), origin)
        if request.url.path not in self.open_paths and not token_ok(
            self.token, request.headers.get("authorization"), request.query_params.get("token")
        ):
            return self._cors(_problem(401, "invalid_request", "Missing or invalid token."), origin)
        response = await call_next(request)
        return self._cors(response, origin)

    def _cors(self, response: Response, origin: str | None) -> Response:
        if origin is not None and origin in self.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        return response


def ws_allowed(token: str, allowed_origins: set[str], headers: dict[str, str], query_token: str | None) -> bool:
    if not host_ok(headers.get("host")):
        return False
    origin = headers.get("origin")
    if origin is not None and origin not in allowed_origins:
        return False
    return token_ok(token, headers.get("authorization"), query_token)
