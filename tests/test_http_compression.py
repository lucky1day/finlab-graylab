from __future__ import annotations

import asyncio
import gzip
import importlib
from collections.abc import Awaitable, Callable, Sequence
from types import ModuleType
from typing import Any

import pytest


Message = dict[str, Any]
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _compression_module() -> ModuleType:
    """加载待实现模块，并让缺少模块表现为明确的 RED 断言。"""
    try:
        return importlib.import_module("backend.http_compression")
    except ModuleNotFoundError as exc:
        if exc.name != "backend.http_compression":
            raise
        pytest.fail("backend.http_compression has not been implemented")


def _http_scope(
    accept_encoding: str | Sequence[str] | None = None,
) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    if isinstance(accept_encoding, str):
        headers.append((b"accept-encoding", accept_encoding.encode("latin-1")))
    elif accept_encoding is not None:
        headers.extend(
            (b"accept-encoding", value.encode("latin-1"))
            for value in accept_encoding
        )
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


async def _run_asgi(app: ASGIApp, scope: Scope) -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        copied = dict(message)
        if "headers" in copied:
            copied["headers"] = list(copied["headers"])
        messages.append(copied)

    await app(scope, receive, send)
    return messages


def _run_http(
    app: ASGIApp,
    accept_encoding: str | Sequence[str] | None = None,
) -> list[Message]:
    return asyncio.run(_run_asgi(app, _http_scope(accept_encoding)))


def _response_app(
    body: bytes,
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        response_headers = list(headers)
        if not any(key.lower() == b"content-length" for key, _ in response_headers):
            response_headers.append((b"content-length", str(len(body)).encode("ascii")))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": response_headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


def _start_message(messages: Sequence[Message]) -> Message:
    return next(message for message in messages if message["type"] == "http.response.start")


def _body_bytes(messages: Sequence[Message]) -> bytes:
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )


def _header_values(messages: Sequence[Message], name: str) -> list[str]:
    wanted = name.casefold().encode("latin-1")
    return [
        value.decode("latin-1")
        for key, value in _start_message(messages).get("headers", [])
        if key.lower() == wanted
    ]


def _vary_tokens(messages: Sequence[Message]) -> list[str]:
    return [
        token.strip()
        for value in _header_values(messages, "vary")
        for token in value.split(",")
        if token.strip()
    ]


@pytest.mark.parametrize(
    "header_value",
    [
        "gzip",
        "br, gzip;q=0.8",
        "*;q=0.5",
        " GZiP ; Q = 0.800 ",
        "gzip;q=1.000",
        "gzip;q=0.001",
        "br;q=not-a-qvalue, gzip",
    ],
)
def test_accepts_gzip_for_positive_explicit_or_wildcard_weight(
    header_value: str,
) -> None:
    module = _compression_module()

    assert module.accepts_gzip(header_value) is True


@pytest.mark.parametrize(
    "header_value",
    [
        None,
        "",
        " \t ",
        "identity",
        "br",
        "gzip;q=0",
        "gzip;q=0.000",
        "gzip;q=0, *;q=1",
        "*;q=1, gzip;q=0",
        "gzip;q=invalid",
        "gzip;q=NaN",
        "gzip;q=Infinity",
        "gzip;q=-0.1",
        "gzip;q=1.1",
        "gzip;q=1.001",
        "gzip;q=0.1234",
        "gzip;q=.5",
        "gzip;q=00.5",
        "gzip;q=\"0.5\"",
        "gzip;q=",
        "gzip;q=\n0.5",
        "gzip;\N{NO-BREAK SPACE}q=0.5",
        "gzip;q=0.5;q=0.4",
        "gzip;q=0.5;Q=0.5",
        "gzip;level=1",
        "gzip;q=invalid, *;q=1",
        "*;q=invalid",
    ],
)
def test_rejects_zero_missing_or_strictly_invalid_gzip_weight(
    header_value: str | None,
) -> None:
    module = _compression_module()

    assert module.accepts_gzip(header_value) is False


@pytest.mark.parametrize(
    "header_value",
    [
        "gzip, gzip",
        "gzip;q=1, gzip;q=0",
        "gzip;q=0, gzip;q=1, *;q=1",
        "GZIP;q=0.5, gzip;q=0.5",
    ],
)
def test_duplicate_explicit_gzip_tokens_fail_closed(header_value: str) -> None:
    module = _compression_module()

    assert module.accepts_gzip(header_value) is False


@pytest.mark.parametrize(
    ("accept_encoding", "compressed"),
    [
        ("gzip", True),
        ("br, gzip;q=0.8", True),
        ("*;q=0.5", True),
        (" GZiP ; Q = 1.000 ", True),
        ("gzip;q=0", False),
        ("gzip;q=0, *;q=1", False),
        ("identity", False),
        ("gzip;q=NaN", False),
        (None, False),
        ("", False),
    ],
)
def test_middleware_selects_one_wire_encoding_and_always_varies(
    accept_encoding: str | None,
    compressed: bool,
) -> None:
    module = _compression_module()
    raw_body = (
        b'{"items":[' + b'{"value":"the quick brown fox"},' * 80 + b"]}"
    )
    middleware = module.QAwareGZipMiddleware(
        _response_app(raw_body, headers=[(b"content-type", b"application/json")])
    )

    messages = _run_http(middleware, accept_encoding)
    wire_body = _body_bytes(messages)
    content_encodings = _header_values(messages, "content-encoding")

    assert len(_header_values(messages, "vary")) == 1
    assert [token.casefold() for token in _vary_tokens(messages)].count(
        "accept-encoding"
    ) == 1
    assert int(_header_values(messages, "content-length")[0]) == len(wire_body)
    if compressed:
        assert content_encodings == ["gzip"]
        assert gzip.decompress(wire_body) == raw_body
        with pytest.raises(gzip.BadGzipFile):
            gzip.decompress(gzip.decompress(wire_body))
    else:
        assert content_encodings == []
        assert wire_body == raw_body


def test_multiple_accept_encoding_fields_are_parsed_as_one_field_value() -> None:
    module = _compression_module()
    raw_body = b"x" * 1000
    middleware = module.QAwareGZipMiddleware(_response_app(raw_body))

    compressed = _run_http(middleware, ["br", "gzip;q=0.4"])
    explicit_rejection = _run_http(middleware, ["gzip;q=0", "*;q=1"])

    assert _header_values(compressed, "content-encoding") == ["gzip"]
    assert gzip.decompress(_body_bytes(compressed)) == raw_body
    assert _header_values(explicit_rejection, "content-encoding") == []
    assert _body_bytes(explicit_rejection) == raw_body


def test_small_response_stays_identity_but_has_vary() -> None:
    module = _compression_module()
    raw_body = b'{"ok":true}'
    middleware = module.QAwareGZipMiddleware(_response_app(raw_body))

    messages = _run_http(middleware, "gzip")

    assert _body_bytes(messages) == raw_body
    assert _header_values(messages, "content-encoding") == []
    assert _header_values(messages, "content-length") == [str(len(raw_body))]
    assert [token.casefold() for token in _vary_tokens(messages)] == [
        "accept-encoding"
    ]


@pytest.mark.parametrize("content_encoding", ["br", "gzip"])
def test_existing_content_encoding_is_never_compressed_again(
    content_encoding: str,
) -> None:
    module = _compression_module()
    encoded_body = b"already encoded" * 100
    middleware = module.QAwareGZipMiddleware(
        _response_app(
            encoded_body,
            headers=[(b"content-encoding", content_encoding.encode("ascii"))],
        )
    )

    messages = _run_http(middleware, "gzip")

    assert _header_values(messages, "content-encoding") == [content_encoding]
    assert _body_bytes(messages) == encoded_body
    assert _header_values(messages, "content-length") == [str(len(encoded_body))]
    assert [token.casefold() for token in _vary_tokens(messages)].count(
        "accept-encoding"
    ) == 1


@pytest.mark.parametrize("accept_encoding", ["identity", "gzip"])
def test_multiple_vary_fields_are_merged_without_losing_or_repeating_tokens(
    accept_encoding: str,
) -> None:
    module = _compression_module()
    raw_body = b"response" * 100
    middleware = module.QAwareGZipMiddleware(
        _response_app(
            raw_body,
            headers=[
                (b"vary", b"Origin, ACCEPT-ENCODING"),
                (b"vary", b"X-Mode, accept-encoding"),
            ],
        )
    )

    messages = _run_http(middleware, accept_encoding)

    assert len(_header_values(messages, "vary")) == 1
    assert [token.casefold() for token in _vary_tokens(messages)] == [
        "origin",
        "accept-encoding",
        "x-mode",
    ]


def test_reused_response_start_is_not_mutated_or_accumulated() -> None:
    module = _compression_module()
    raw_body = b"response" * 100
    original_headers = [
        (b"content-length", str(len(raw_body)).encode("ascii")),
        (b"vary", b"Origin"),
    ]
    shared_start: Message = {
        "type": "http.response.start",
        "status": 200,
        "headers": original_headers,
    }

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send(shared_start)
        await send({"type": "http.response.body", "body": raw_body})

    middleware = module.QAwareGZipMiddleware(app)

    first = _run_http(middleware, "gzip")
    second = _run_http(middleware, "gzip")

    assert shared_start["headers"] == original_headers
    assert gzip.decompress(_body_bytes(first)) == raw_body
    assert gzip.decompress(_body_bytes(second)) == raw_body
    assert [token.casefold() for token in _vary_tokens(first)] == [
        "origin",
        "accept-encoding",
    ]
    assert [token.casefold() for token in _vary_tokens(second)] == [
        "origin",
        "accept-encoding",
    ]


def test_http_app_observes_the_original_request_scope() -> None:
    module = _compression_module()
    raw_body = b"x" * 1000
    original_scope = _http_scope(" GZIP ; Q = 1 ")
    seen_scopes: list[Scope] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        seen_scopes.append(scope)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(len(raw_body)).encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": raw_body})

    middleware = module.QAwareGZipMiddleware(app)

    messages = asyncio.run(_run_asgi(middleware, original_scope))

    assert seen_scopes == [original_scope]
    assert seen_scopes[0] is original_scope
    assert original_scope["headers"] == [(b"accept-encoding", b" GZIP ; Q = 1 ")]
    assert gzip.decompress(_body_bytes(messages)) == raw_body


def test_non_http_scope_is_passed_through_without_response_header_changes() -> None:
    module = _compression_module()
    scope: Scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "path": "/ws",
        "headers": [(b"accept-encoding", b"gzip")],
    }
    seen: list[tuple[Scope, Receive, Send]] = []

    async def app(app_scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((app_scope, receive, send))
        await send({"type": "websocket.accept", "headers": [(b"x-test", b"ok")]})

    middleware = module.QAwareGZipMiddleware(app)
    messages = asyncio.run(_run_asgi(middleware, scope))

    assert seen[0][0] is scope
    assert messages == [
        {"type": "websocket.accept", "headers": [(b"x-test", b"ok")]}
    ]


def test_started_identity_response_preserves_vary_when_app_raises() -> None:
    module = _compression_module()
    shared_start: Message = {
        "type": "http.response.start",
        "status": 500,
        "headers": [(b"vary", b"Origin")],
    }
    captured: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send(shared_start)
        raise RuntimeError("boom after response start")

    middleware = module.QAwareGZipMiddleware(app)

    async def execute() -> None:
        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            captured.append(dict(message))

        await middleware(_http_scope("identity"), receive, send)

    with pytest.raises(RuntimeError, match="boom after response start"):
        asyncio.run(execute())

    assert shared_start["headers"] == [(b"vary", b"Origin")]
    assert [
        token.strip().casefold()
        for value in _header_values(captured, "vary")
        for token in value.split(",")
        if token.strip()
    ] == ["origin", "accept-encoding"]


def test_starlette_gzip_is_configured_once_and_called_only_when_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _compression_module()
    created: list[tuple[int, int]] = []
    calls: list[Scope] = []

    class RecordingGZipMiddleware:
        def __init__(
            self,
            app: ASGIApp,
            minimum_size: int,
            compresslevel: int,
        ) -> None:
            self.app = app
            created.append((minimum_size, compresslevel))

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            calls.append(scope)
            await self.app(scope, receive, send)

    monkeypatch.setattr(module, "GZipMiddleware", RecordingGZipMiddleware)
    middleware = module.QAwareGZipMiddleware(_response_app(b"x" * 1000))

    identity_messages = _run_http(middleware, "gzip;q=0")
    gzip_messages = _run_http(middleware, "gzip;q=0.5")

    assert created == [(500, 6)]
    assert len(calls) == 1
    assert _body_bytes(identity_messages) == b"x" * 1000
    assert _body_bytes(gzip_messages) == b"x" * 1000
    assert [token.casefold() for token in _vary_tokens(identity_messages)] == [
        "accept-encoding"
    ]
    assert [token.casefold() for token in _vary_tokens(gzip_messages)] == [
        "accept-encoding"
    ]
