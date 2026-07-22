from __future__ import annotations

import re
from collections.abc import Sequence

from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_QVALUE_PATTERN = re.compile(
    r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)\Z",
    flags=re.ASCII,
)
_ORIGINAL_SCOPE_KEY = "_q_aware_gzip_original_scope"


def _strip_ows(value: str) -> str:
    """仅移除 RFC 定义的可选空白 SP / HTAB。"""
    return value.strip(" \t")


def _positive_quality(value: str) -> bool | None:
    """严格解析 HTTP qvalue，并返回它是否大于零。"""
    if _QVALUE_PATTERN.fullmatch(value) is None:
        return None
    return value.startswith("1") or any(digit != "0" for digit in value[2:])


def _coding_preference(member: str) -> tuple[str, bool | None] | None:
    """解析一个与 gzip 选择有关的 Accept-Encoding 成员。"""
    parts = member.split(";")
    coding = _strip_ows(parts[0]).casefold()
    if coding not in {"gzip", "*"}:
        return None
    if len(parts) == 1:
        return coding, True

    quality: bool | None = None
    quality_seen = False
    for parameter in parts[1:]:
        name, separator, value = parameter.partition("=")
        if (
            not separator
            or _strip_ows(name).casefold() != "q"
            or quality_seen
        ):
            return coding, None
        quality_seen = True
        quality = _positive_quality(_strip_ows(value))
        if quality is None:
            return coding, None

    return coding, quality if quality_seen else None


def accepts_gzip(header_value: str | None) -> bool:
    """判断 Accept-Encoding 是否明确允许 gzip 表示。"""
    if not header_value or not _strip_ows(header_value):
        return False

    gzip_preferences: list[bool | None] = []
    wildcard_preferences: list[bool | None] = []
    for member in header_value.split(","):
        preference = _coding_preference(member)
        if preference is None:
            continue
        coding, quality = preference
        if coding == "gzip":
            gzip_preferences.append(quality)
        else:
            wildcard_preferences.append(quality)

    if gzip_preferences:
        # 重复的显式 gzip 成员容易产生相互冲突的权重，统一 fail-closed。
        return len(gzip_preferences) == 1 and gzip_preferences[0] is True
    if wildcard_preferences:
        return (
            len(wildcard_preferences) == 1
            and wildcard_preferences[0] is True
        )
    return False


def _merge_vary_headers(
    headers: Sequence[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """合并所有 Vary 字段，并确保 Accept-Encoding 恰好出现一次。"""
    merged_headers: list[tuple[bytes, bytes]] = []
    vary_tokens: list[str] = []
    seen_tokens: set[str] = set()
    vary_position: int | None = None

    for key, value in headers:
        if key.lower() != b"vary":
            merged_headers.append((key, value))
            continue
        if vary_position is None:
            vary_position = len(merged_headers)
        for raw_token in value.decode("latin-1").split(","):
            token = raw_token.strip()
            normalized = token.casefold()
            if token and normalized not in seen_tokens:
                vary_tokens.append(token)
                seen_tokens.add(normalized)

    if "accept-encoding" not in seen_tokens:
        vary_tokens.append("Accept-Encoding")

    vary_header = (b"vary", ", ".join(vary_tokens).encode("latin-1"))
    if vary_position is None:
        merged_headers.append(vary_header)
    else:
        merged_headers.insert(vary_position, vary_header)
    return merged_headers


def _copy_response_start_with_vary(message: Message) -> Message:
    """复制 response.start，避免修改下游复用的 message/header 对象。"""
    if message["type"] != "http.response.start":
        return message
    copied = dict(message)
    copied["headers"] = _merge_vary_headers(message.get("headers", ()))
    return copied


class _GZipInputAdapter:
    """在 Starlette 改写响应头前复制、规范化 response.start。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        original_scope = scope.get(_ORIGINAL_SCOPE_KEY, scope)

        async def send_copy(message: Message) -> None:
            await send(_copy_response_start_with_vary(message))

        await self.app(original_scope, receive, send_copy)


class QAwareGZipMiddleware:
    """仅在 q-value 允许时调用 Starlette gzip-6 中间件。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._gzip = GZipMiddleware(
            _GZipInputAdapter(app),
            minimum_size=500,
            compresslevel=6,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header_values = [
            value.decode("latin-1")
            for key, value in scope.get("headers", ())
            if key.lower() == b"accept-encoding"
        ]

        async def send_with_vary(message: Message) -> None:
            await send(_copy_response_start_with_vary(message))

        if not accepts_gzip(",".join(header_values)):
            await self.app(scope, receive, send_with_vary)
            return

        gzip_scope = dict(scope)
        gzip_scope["headers"] = [
            (key, value)
            for key, value in scope.get("headers", ())
            if key.lower() != b"accept-encoding"
        ]
        gzip_scope["headers"].append((b"accept-encoding", b"gzip"))
        gzip_scope[_ORIGINAL_SCOPE_KEY] = scope
        await self._gzip(gzip_scope, receive, send_with_vary)
