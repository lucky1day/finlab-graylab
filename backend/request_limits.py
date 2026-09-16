from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


MAX_AUTH_JSON_BODY_BYTES = 8 * 1024
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
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

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

        messages: list[Message] = []
        actual_length = 0
        while True:
            message = await receive()
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
            actual_length += len(body)
            if actual_length > self.max_body_bytes:
                await _send_error(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    error_code="request_body_too_large",
                )
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

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

        async def replay_receive() -> Message:
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)
