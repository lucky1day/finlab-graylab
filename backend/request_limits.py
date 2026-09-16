from __future__ import annotations

import asyncio
import time
from typing import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.db import current_http_request_context


MAX_AUTH_JSON_BODY_BYTES = 8 * 1024
AUTH_BODY_READ_TIMEOUT_SECONDS = 2.0
AUTH_JSON_WRITE_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/change-password",
        "/api/auth/update-profile",
        "/api/admin/users",
        "/api/admin/users/change-username",
        "/api/admin/users/change-role",
        "/api/admin/users/reset-password",
        "/api/admin/users/change-status",
        "/api/admin/users/update-profile",
        "/api/admin/users/edit",
    }
)


def _is_limited_request(scope: Scope) -> bool:
    """仅限制认证与管理写请求，不扩大到其他 API。"""
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return False
    return str(scope.get("path") or "") in AUTH_JSON_WRITE_PATHS


def _declared_content_length(scope: Scope) -> int | None:
    values = [
        value.strip()
        for name, value in scope.get("headers", [])
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1 or not values[0] or not values[0].isdigit():
        raise ValueError("invalid_content_length")
    return int(values[0])


async def _send_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    error_code: str,
) -> None:
    response = JSONResponse(
        status_code=status_code,
        content={"error_code": error_code},
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)


class AuthJSONBodyLimitMiddleware:
    """在 JSON 解析前有界读取认证写请求并向下游重放。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = MAX_AUTH_JSON_BODY_BYTES,
        body_read_timeout_seconds: float = AUTH_BODY_READ_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.body_read_timeout_seconds = body_read_timeout_seconds
        self.clock = clock

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if not _is_limited_request(scope):
            await self.app(scope, receive, send)
            return

        try:
            declared_length = _declared_content_length(scope)
        except ValueError:
            await _send_error(
                scope,
                receive,
                send,
                status_code=400,
                error_code="invalid_content_length",
            )
            return
        if (
            declared_length is not None
            and declared_length > self.max_body_bytes
        ):
            await _send_error(
                scope,
                receive,
                send,
                status_code=413,
                error_code="request_body_too_large",
            )
            return

        request_context = current_http_request_context()
        started_at = (
            request_context.started_at
            if request_context is not None
            else self.clock()
        )
        deadline = started_at + self.body_read_timeout_seconds
        if request_context is not None:
            deadline = min(deadline, request_context.deadline_at)

        body_buffer = bytearray()
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=408,
                    error_code="request_body_timeout",
                )
                return
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=408,
                    error_code="request_body_timeout",
                )
                return
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=408,
                    error_code="request_body_incomplete",
                )
                return
            body = message.get("body", b"")
            if body:
                body_buffer.extend(body)
            if len(body_buffer) > self.max_body_bytes:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    error_code="request_body_too_large",
                )
                return
            if not message.get("more_body", False):
                break

        actual_length = len(body_buffer)
        if declared_length is not None and actual_length != declared_length:
            await _send_error(
                scope,
                receive,
                send,
                status_code=(408 if actual_length < declared_length else 400),
                error_code=(
                    "request_body_incomplete"
                    if actual_length < declared_length
                    else "invalid_content_length"
                ),
            )
            return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": bytes(body_buffer),
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)
