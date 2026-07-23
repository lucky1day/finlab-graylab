#!/usr/bin/env python3
"""通过 Chrome DevTools Protocol 测量导航到 Factor Lab 完整绘制。"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit


USER_AGENT = "bond-factor-lab-browser-benchmark/1.0"
READY_FIELDS = {
    "seq",
    "snapshotId",
    "committedAt",
    "stale",
    "source",
    "schemeCount",
    "liveRowCount",
    "backtestRowCount",
}
_DASHBOARD_PATH = re.compile(r"(?:^|/)api/factor-lab/dashboard$")
_LEGACY_PATHS = (
    re.compile(r"(?:^|/)api/schemes$"),
    re.compile(r"(?:^|/)api/backtests/factor-lab$"),
    re.compile(r"(?:^|/)api/metrics/[^/%]+$"),
)


@dataclass(frozen=True)
class BrowserAttempt:
    """一次浏览器导航；错误、stale 和 fallback 永不剔除。"""

    index: int
    started_at: str
    wait_before_seconds: float
    total_ms: float
    success: bool
    error: str | None
    dashboard_request_urls: list[str]
    dashboard_response_evidence: list[dict[str, Any]]
    legacy_request_urls: list[str]
    console_errors: list[str]
    page_errors: list[str]
    ready: dict[str, Any] | None


class CDPCommandError(RuntimeError):
    """保留稳定 CDP error code，同时不把可能敏感的原消息写入报告。"""

    def __init__(self, method: str, code: int | str, message: str = "") -> None:
        self.method = method
        self.code = code
        self.cdp_message = message
        super().__init__(f"CDP {method} failed with code {code}")

    def is_navigation_context_loss(self) -> bool:
        """只识别导航切换期间已知的 evaluate context 消失错误。"""

        folded = self.cdp_message.casefold()
        return (
            self.method == "Runtime.evaluate"
            and self.code == -32000
            and (
                "cannot find context with specified id" in folded
                or "execution context was destroyed" in folded
                or "cannot find context" in folded
            )
        )


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("nearest rank requires samples")
    return sorted(values)[math.ceil(percentile * len(values)) - 1]


def _safe_url_without_query(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def classify_factor_lab_request(url: str) -> tuple[str | None, str | None]:
    """按严格 API path 分类，并删除 query/fragment 防止报告泄密。"""

    redacted = _safe_url_without_query(url)
    if redacted is None:
        return None, None
    path = urlsplit(redacted).path
    if _DASHBOARD_PATH.search(path):
        return "dashboard", redacted
    if any(pattern.search(path) for pattern in _LEGACY_PATHS):
        return "legacy", redacted
    return None, None


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_ready_value(value: object) -> str | None:
    """返回稳定错误码；``None`` 表示 fresh dashboard ready 合同通过。"""

    if not isinstance(value, dict) or not READY_FIELDS.issubset(value):
        return "ready_schema"
    if not _nonnegative_integer(value["seq"]) or value["seq"] < 1:
        return "ready_schema"
    if not isinstance(value["snapshotId"], str) or not value["snapshotId"]:
        return "ready_schema"
    committed_at = value["committedAt"]
    if (
        not isinstance(committed_at, (int, float))
        or isinstance(committed_at, bool)
        or not math.isfinite(float(committed_at))
    ):
        return "ready_schema"
    if not isinstance(value["stale"], bool):
        return "ready_schema"
    if not isinstance(value["source"], str):
        return "ready_schema"
    for field in ("schemeCount", "liveRowCount", "backtestRowCount"):
        if not _nonnegative_integer(value[field]):
            return "ready_schema"
    if value["source"] != "dashboard":
        return "ready_source"
    if value["stale"]:
        return "ready_stale"
    return None


def select_ready_context(
    contexts: dict[Any, str],
    frame_urls: dict[str, str],
    main_frame_id: str,
    ready_frame_url_substring: str | None,
) -> Any | None:
    """选择 ready 所在 execution context；iframe 模式永不回退到父 frame。"""

    target_frames: set[str]
    if ready_frame_url_substring:
        target_frames = _matching_ready_frame_ids(
            frame_urls,
            main_frame_id,
            ready_frame_url_substring,
        )
        if len(target_frames) > 1:
            raise ValueError(f"ready_frame_match_count={len(target_frames)}")
    else:
        target_frames = {main_frame_id}
    return next(
        (context_id for context_id, frame_id in contexts.items() if frame_id in target_frames),
        None,
    )


def _matching_ready_frame_ids(
    frame_urls: dict[str, str],
    main_frame_id: str,
    ready_frame_url_substring: str,
) -> set[str]:
    return {
        frame_id
        for frame_id, url in frame_urls.items()
        if frame_id != main_frame_id and ready_frame_url_substring in url
    }


class CDPClient:
    """同步 CDP 消息 ID/event demultiplexer。"""

    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self._next_id = 1
        self._events: deque[dict[str, Any]] = deque()
        self._responses: dict[int, dict[str, Any]] = {}

    def _receive(self, timeout: float | None) -> dict[str, Any]:
        raw = self.websocket.recv(timeout=timeout)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise RuntimeError("CDP message is not an object")
        return message

    def _route(self, message: dict[str, Any]) -> None:
        if "id" in message:
            self._responses[int(message["id"])] = message
        elif "method" in message:
            self._events.append(message)

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        message_id = self._next_id
        self._next_id += 1
        command: dict[str, Any] = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        if session_id is not None:
            command["sessionId"] = session_id
        self.websocket.send(
            json.dumps(command, separators=(",", ":"))
        )
        deadline = time.monotonic() + timeout
        while message_id not in self._responses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP call timed out: {method}")
            self._route(self._receive(remaining))
        response = self._responses.pop(message_id)
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else "unknown"
            message = str(error.get("message", "")) if isinstance(error, dict) else ""
            raise CDPCommandError(method, code, message)
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError(f"CDP {method} returned a non-object result")
        return result

    def pending_response(self, message_id: int) -> dict[str, Any] | None:
        response = self._responses.pop(message_id, None)
        if response is None:
            return None
        if "error" in response:
            return {"error": response["error"]}
        result = response.get("result", {})
        return result if isinstance(result, dict) else None

    def next_event(self, timeout: float | None = None) -> dict[str, Any]:
        if self._events:
            return self._events.popleft()
        while True:
            message = self._receive(timeout)
            if "method" in message:
                return message
            self._route(message)

    def close(self) -> None:
        self.websocket.close()


@dataclass
class OwnedBrowser:
    process: subprocess.Popen[bytes]
    profile_directory: Path
    debug_port: int
    browser_websocket_url: str
    product: str
    user_agent: str
    protocol_version: str


def _connect_websocket(url: str) -> Any:
    try:
        from websockets.sync.client import connect
    except ImportError as error:
        raise RuntimeError("the environment package 'websockets' is required") from error
    return connect(
        url,
        open_timeout=5,
        close_timeout=2,
        additional_headers={"User-Agent": USER_AGENT},
    )


def _read_devtools_active_port(
    profile_directory: Path,
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> tuple[int, str]:
    active_port_path = profile_directory / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"browser exited before CDP became ready: {process.returncode}")
        try:
            if active_port_path.is_symlink():
                raise RuntimeError("DevToolsActivePort must not be a symlink")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(active_port_path, flags)
            try:
                data = os.read(descriptor, 4096).decode("utf-8")
            finally:
                os.close(descriptor)
            lines = data.splitlines()
            if len(lines) >= 2:
                port = int(lines[0])
                websocket_path = lines[1]
                if not 1 <= port <= 65535:
                    raise RuntimeError("DevToolsActivePort contains invalid port")
                if not websocket_path.startswith("/devtools/browser/"):
                    raise RuntimeError("DevToolsActivePort contains invalid websocket path")
                return port, websocket_path
        except FileNotFoundError:
            pass
        except (ValueError, UnicodeDecodeError):
            pass
        time.sleep(0.02)
    raise TimeoutError("timed out waiting for DevToolsActivePort")


def start_owned_browser(browser_binary: Path, timeout_seconds: float = 10.0) -> OwnedBrowser:
    """以 port=0 和独立 profile 启动唯一归本探针所有的 Chromium。"""

    if not browser_binary.is_file():
        raise FileNotFoundError(f"browser binary not found: {browser_binary}")
    profile = Path(tempfile.mkdtemp(prefix="factor-lab-browser-profile-"))
    command = [
        str(browser_binary),
        "--headless=new",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-gpu",
        "--disable-sync",
        "--metrics-recording-only",
        "--no-default-browser-check",
        "--no-first-run",
        "--remote-allow-origins=*",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "about:blank",
    ]
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        port, websocket_path = _read_devtools_active_port(
            profile, process, timeout_seconds
        )
        websocket_url = f"ws://127.0.0.1:{port}{websocket_path}"
        client = CDPClient(_connect_websocket(websocket_url))
        try:
            version = client.call("Browser.getVersion", timeout=5)
        finally:
            client.close()
        return OwnedBrowser(
            process=process,
            profile_directory=profile,
            debug_port=port,
            browser_websocket_url=websocket_url,
            product=str(version.get("product", "unknown")),
            user_agent=str(version.get("userAgent", "unknown")),
            protocol_version=str(version.get("protocolVersion", "unknown")),
        )
    except BaseException:
        if process is not None:
            terminate_owned_process(process)
        shutil.rmtree(profile, ignore_errors=True)
        raise


def terminate_owned_process(
    process: subprocess.Popen[bytes], timeout_seconds: float = 3.0
) -> None:
    """只终止传入的自启进程；SIGTERM 超时后有界 SIGKILL。"""

    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return


def stop_owned_browser(browser: OwnedBrowser) -> None:
    terminate_owned_process(browser.process)
    shutil.rmtree(browser.profile_directory, ignore_errors=True)


def _browser_call(browser: OwnedBrowser, method: str, params: dict[str, Any]) -> dict:
    client = CDPClient(_connect_websocket(browser.browser_websocket_url))
    try:
        return client.call(method, params, timeout=5)
    finally:
        client.close()


def _close_page(browser: OwnedBrowser, target_id: str) -> bool:
    try:
        result = _browser_call(browser, "Target.closeTarget", {"targetId": target_id})
    except Exception:
        return False
    return result.get("success") is True


def _close_target_with_fallback(
    client: CDPClient,
    browser: OwnedBrowser,
    target_id: str,
) -> bool:
    """仅接受显式 success=true；否则通过独立 browser websocket 二次关闭。"""

    try:
        result = client.call(
            "Target.closeTarget",
            {"targetId": target_id},
            timeout=2,
        )
        if result.get("success") is True:
            return True
    except Exception:
        pass
    return _close_page(browser, target_id)


def _drain_events(client: CDPClient) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(client.next_event(timeout=0))
        except (TimeoutError, IndexError):
            return events


def _record_event(
    event: dict[str, Any],
    *,
    contexts: dict[tuple[str, int], str],
    frame_urls: dict[str, str],
    frame_sessions: dict[str, str],
    dashboard_requests: list[str],
    legacy_requests: list[str],
    dashboard_response_evidence: list[dict[str, Any]],
    dashboard_request_indices: dict[tuple[str, str], int],
    console_errors: list[str],
    page_errors: list[str],
) -> bool:
    method = event.get("method")
    params = event.get("params", {})
    event_session = str(event.get("sessionId", ""))
    if method == "Runtime.executionContextCreated":
        context = params.get("context", {})
        context_id = context.get("id")
        aux = context.get("auxData", {})
        frame_id = aux.get("frameId") if isinstance(aux, dict) else None
        if isinstance(context_id, int) and isinstance(frame_id, str) and aux.get("isDefault"):
            contexts[(event_session, context_id)] = frame_id
            frame_sessions[frame_id] = event_session
    elif method == "Runtime.executionContextDestroyed":
        context_id = params.get("executionContextId")
        if isinstance(context_id, int):
            contexts.pop((event_session, context_id), None)
    elif method == "Runtime.executionContextsCleared":
        for key in [key for key in contexts if key[0] == event_session]:
            contexts.pop(key, None)
    elif method == "Page.frameNavigated":
        frame = params.get("frame", {})
        if isinstance(frame.get("id"), str) and isinstance(frame.get("url"), str):
            frame_urls[frame["id"]] = frame["url"]
            frame_sessions[frame["id"]] = event_session
    elif method == "Page.frameDetached":
        frame_id = params.get("frameId")
        if isinstance(frame_id, str):
            frame_urls.pop(frame_id, None)
            frame_sessions.pop(frame_id, None)
            for key, context_frame in list(contexts.items()):
                if context_frame == frame_id:
                    contexts.pop(key, None)
    elif method == "Network.requestWillBeSent":
        request = params.get("request", {})
        request_key = (event_session, str(params.get("requestId", "unknown")))
        previous_index = dashboard_request_indices.get(request_key)
        redirect_response = params.get("redirectResponse")
        if previous_index is not None and isinstance(redirect_response, dict):
            previous = dashboard_response_evidence[previous_index]
            previous["response_received"] = True
            previous["redirected"] = True
            previous["status"] = redirect_response.get("status")
            previous["from_disk_cache"] = (
                previous.get("from_disk_cache") is True
                or redirect_response.get("fromDiskCache") is True
            )
            previous["from_service_worker"] = (
                previous.get("from_service_worker") is True
                or redirect_response.get("fromServiceWorker") is True
            )
            previous["from_prefetch_cache"] = (
                previous.get("from_prefetch_cache") is True
                or redirect_response.get("fromPrefetchCache") is True
            )
        request_url = str(request.get("url", ""))
        frame_id = params.get("frameId")
        if params.get("type") == "Document" and isinstance(frame_id, str):
            redacted_document_url = _safe_url_without_query(request_url)
            if redacted_document_url is not None:
                frame_urls[frame_id] = redacted_document_url
                frame_sessions[frame_id] = event_session
        kind, redacted = classify_factor_lab_request(request_url)
        if kind == "dashboard" and redacted:
            dashboard_requests.append(redacted)
            dashboard_response_evidence.append(
                {
                    "url": redacted,
                    "response_received": False,
                    "redirected": False,
                    "status": None,
                    "from_disk_cache": False,
                    "from_service_worker": False,
                    "from_prefetch_cache": False,
                }
            )
            dashboard_request_indices[request_key] = (
                len(dashboard_response_evidence) - 1
            )
        elif kind == "legacy" and redacted:
            legacy_requests.append(redacted)
            dashboard_request_indices.pop(request_key, None)
        else:
            dashboard_request_indices.pop(request_key, None)
    elif method == "Network.requestServedFromCache":
        request_key = (event_session, str(params.get("requestId", "unknown")))
        evidence_index = dashboard_request_indices.get(request_key)
        if evidence_index is not None:
            dashboard_response_evidence[evidence_index]["from_disk_cache"] = True
    elif method == "Network.responseReceived":
        response = params.get("response", {})
        if isinstance(response, dict):
            request_key = (
                event_session,
                str(params.get("requestId", "unknown")),
            )
            evidence_index = dashboard_request_indices.get(request_key)
            if evidence_index is not None:
                evidence = dashboard_response_evidence[evidence_index]
                evidence["response_received"] = True
                evidence["status"] = response.get("status")
                evidence["from_disk_cache"] = (
                    evidence.get("from_disk_cache") is True
                    or response.get("fromDiskCache") is True
                )
                evidence["from_service_worker"] = (
                    evidence.get("from_service_worker") is True
                    or response.get("fromServiceWorker") is True
                )
                evidence["from_prefetch_cache"] = (
                    evidence.get("from_prefetch_cache") is True
                    or response.get("fromPrefetchCache") is True
                )
    elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
        console_errors.append("Runtime.consoleAPICalled:error")
    elif method == "Log.entryAdded":
        entry = params.get("entry", {})
        if entry.get("level") == "error":
            console_errors.append("Log.entryAdded:error")
    elif method == "Runtime.exceptionThrown":
        page_errors.append("Runtime.exceptionThrown")
    elif method in {"Inspector.targetCrashed", "Target.targetCrashed"}:
        page_errors.append(str(method))
        return True
    return False


_TARGET_SESSION_DOMAINS: dict[str, tuple[str, ...]] = {
    "page": ("Page", "Runtime", "Network", "Log", "Inspector"),
    "iframe": ("Page", "Runtime", "Network", "Log", "Inspector"),
    "worker": ("Runtime", "Network", "Log"),
    "shared_worker": ("Runtime", "Network", "Log"),
    "service_worker": ("Runtime", "Network", "Log"),
}
_PAGE_TARGET_TYPES = frozenset({"page", "iframe"})


def _enable_cdp_session(
    client: CDPClient,
    session_id: str,
    target_type: str,
) -> bool:
    """按 target type 启用 flattened session；任何配置失败前先恢复执行。"""

    domains = _TARGET_SESSION_DOMAINS.get(target_type)
    if domains is None:
        client.call(
            "Runtime.runIfWaitingForDebugger",
            timeout=3,
            session_id=session_id,
        )
        return False
    try:
        client.call(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
            },
            timeout=3,
            session_id=session_id,
        )
        for domain in domains:
            client.call(f"{domain}.enable", timeout=3, session_id=session_id)
        client.call(
            "Network.setCacheDisabled",
            {"cacheDisabled": True},
            timeout=3,
            session_id=session_id,
        )
        client.call(
            "Network.setBypassServiceWorker",
            {"bypass": True},
            timeout=3,
            session_id=session_id,
        )
        client.call(
            "Network.setUserAgentOverride",
            {"userAgent": USER_AGENT},
            timeout=3,
            session_id=session_id,
        )
        if target_type in _PAGE_TARGET_TYPES:
            client.call(
                "Network.clearBrowserCache",
                timeout=3,
                session_id=session_id,
            )
    except BaseException:
        try:
            client.call(
                "Runtime.runIfWaitingForDebugger",
                timeout=3,
                session_id=session_id,
            )
        except Exception:
            pass
        raise
    client.call(
        "Runtime.runIfWaitingForDebugger",
        timeout=3,
        session_id=session_id,
    )
    return True


def _record_frame_tree(
    frame_tree: object,
    frame_urls: dict[str, str],
) -> None:
    if not isinstance(frame_tree, dict):
        return
    frame = frame_tree.get("frame", {})
    if isinstance(frame, dict):
        frame_id = frame.get("id")
        frame_url = frame.get("url")
        if isinstance(frame_id, str) and isinstance(frame_url, str) and frame_url:
            frame_urls[frame_id] = frame_url
    children = frame_tree.get("childFrames", [])
    if isinstance(children, list):
        for child in children:
            _record_frame_tree(child, frame_urls)


def _drop_cdp_session_state(
    session_id: str,
    *,
    configured_sessions: set[str],
    session_targets: dict[str, str],
    session_target_types: dict[str, str],
    contexts: dict[tuple[str, int], str],
    frame_urls: dict[str, str],
    frame_sessions: dict[str, str],
) -> None:
    """幂等移除 detached/ignored session 的全部归属状态。"""

    configured_sessions.discard(session_id)
    session_target_types.pop(session_id, None)
    target_id = session_targets.pop(session_id, None)
    if target_id is not None:
        frame_urls.pop(target_id, None)
        frame_sessions.pop(target_id, None)
    for context_key in [key for key in contexts if key[0] == session_id]:
        contexts.pop(context_key, None)
    for frame_id in [
        frame_id
        for frame_id, owner_session in frame_sessions.items()
        if owner_session == session_id
    ]:
        frame_sessions.pop(frame_id, None)
        frame_urls.pop(frame_id, None)


def _detach_cdp_session(client: CDPClient, session_id: str) -> None:
    client.call(
        "Target.detachFromTarget",
        {"sessionId": session_id},
        timeout=3,
    )


def _consume_cdp_events(
    client: CDPClient,
    *,
    configured_sessions: set[str],
    session_targets: dict[str, str],
    session_target_types: dict[str, str],
    contexts: dict[tuple[str, int], str],
    frame_urls: dict[str, str],
    frame_sessions: dict[str, str],
    dashboard_requests: list[str],
    legacy_requests: list[str],
    dashboard_response_evidence: list[dict[str, Any]],
    dashboard_request_indices: dict[tuple[str, str], int],
    console_errors: list[str],
    page_errors: list[str],
) -> bool:
    """消费 browser websocket 上所有 root/OOPIF flattened session 事件。"""

    crashed = False
    pending = deque(_drain_events(client))
    while pending:
        event = pending.popleft()
        method = event.get("method")
        params = event.get("params", {})
        if method == "Target.attachedToTarget":
            child_session = params.get("sessionId")
            target_info = params.get("targetInfo", {})
            target_id = (
                target_info.get("targetId") if isinstance(target_info, dict) else None
            )
            target_url = (
                target_info.get("url") if isinstance(target_info, dict) else None
            )
            target_type = (
                target_info.get("type") if isinstance(target_info, dict) else None
            )
            if isinstance(child_session, str):
                normalized_target_type = (
                    target_type if isinstance(target_type, str) else ""
                )
                if isinstance(target_id, str):
                    session_targets[child_session] = target_id
                    frame_sessions[target_id] = child_session
                    if (
                        normalized_target_type in _PAGE_TARGET_TYPES
                        and isinstance(target_url, str)
                        and target_url
                    ):
                        frame_urls[target_id] = target_url
                session_target_types[child_session] = normalized_target_type
                known_target_type = normalized_target_type in _TARGET_SESSION_DOMAINS
                if child_session not in configured_sessions:
                    try:
                        supported = _enable_cdp_session(
                            client,
                            child_session,
                            session_target_types[child_session],
                        )
                    except BaseException as error:
                        detached = False
                        try:
                            _detach_cdp_session(client, child_session)
                            detached = True
                        except Exception:
                            pass
                        _drop_cdp_session_state(
                            child_session,
                            configured_sessions=configured_sessions,
                            session_targets=session_targets,
                            session_target_types=session_target_types,
                            contexts=contexts,
                            frame_urls=frame_urls,
                            frame_sessions=frame_sessions,
                        )
                        if (
                            isinstance(error, Exception)
                            and not known_target_type
                            and detached
                        ):
                            pending.extend(_drain_events(client))
                            continue
                        raise
                    if supported:
                        configured_sessions.add(child_session)
                    else:
                        try:
                            _detach_cdp_session(client, child_session)
                        finally:
                            _drop_cdp_session_state(
                                child_session,
                                configured_sessions=configured_sessions,
                                session_targets=session_targets,
                                session_target_types=session_target_types,
                                contexts=contexts,
                                frame_urls=frame_urls,
                                frame_sessions=frame_sessions,
                            )
                    pending.extend(_drain_events(client))
            continue
        if method == "Target.detachedFromTarget":
            detached_session = params.get("sessionId")
            if isinstance(detached_session, str):
                _drop_cdp_session_state(
                    detached_session,
                    configured_sessions=configured_sessions,
                    session_targets=session_targets,
                    session_target_types=session_target_types,
                    contexts=contexts,
                    frame_urls=frame_urls,
                    frame_sessions=frame_sessions,
                )
            continue
        crashed = _record_event(
            event,
            contexts=contexts,
            frame_urls=frame_urls,
            frame_sessions=frame_sessions,
            dashboard_requests=dashboard_requests,
            legacy_requests=legacy_requests,
            dashboard_response_evidence=dashboard_response_evidence,
            dashboard_request_indices=dashboard_request_indices,
            console_errors=console_errors,
            page_errors=page_errors,
        ) or crashed
    return crashed


def validate_dashboard_response_evidence(
    dashboard_request_urls: list[str],
    evidence: list[object],
) -> str | None:
    """只接受与请求逐项对应、HTTP 200、无跳转且未命中缓存的响应。"""

    if len(evidence) != len(dashboard_request_urls):
        return "dashboard_response_count_invalid"
    required_fields = {
        "url",
        "response_received",
        "redirected",
        "status",
        "from_disk_cache",
        "from_service_worker",
        "from_prefetch_cache",
    }
    cache_fields = (
        "from_disk_cache",
        "from_service_worker",
        "from_prefetch_cache",
    )
    typed_evidence: list[dict[str, Any]] = []
    for request_url, item in zip(dashboard_request_urls, evidence, strict=True):
        if not isinstance(item, dict):
            return "dashboard_response_evidence_invalid"
        if set(item) - required_fields:
            return "dashboard_response_evidence_invalid"
        if not {
            "url",
            "response_received",
            "redirected",
        }.issubset(item):
            return "dashboard_response_evidence_invalid"
        item_url = item["url"]
        if (
            not isinstance(item_url, str)
            or not isinstance(request_url, str)
            or _safe_url_without_query(item_url) != item_url
            or _safe_url_without_query(request_url) != request_url
            or item_url != request_url
        ):
            return "dashboard_response_evidence_invalid"
        if (
            item["response_received"] is not False
            and item["response_received"] is not True
        ):
            return "dashboard_response_evidence_invalid"
        if item["redirected"] is not False and item["redirected"] is not True:
            return "dashboard_response_evidence_invalid"
        if any(
            item.get(field) is not False and item.get(field) is not True
            for field in cache_fields
        ):
            return "dashboard_cache_evidence_invalid"
        typed_evidence.append(item)

    if any(item["response_received"] is False for item in typed_evidence):
        return "dashboard_response_count_invalid"
    if any(
        type(item.get("status")) is not int
        for item in typed_evidence
    ):
        return "dashboard_response_status_invalid"
    if any(item["redirected"] is True for item in typed_evidence):
        return "dashboard_redirect_detected"
    if any(item["status"] != 200 for item in typed_evidence):
        return "dashboard_response_status_invalid"
    if any(item[field] is True for item in typed_evidence for field in cache_fields):
        return "dashboard_cache_or_service_worker"
    return None


def run_browser_attempt(
    *,
    browser: OwnedBrowser,
    url: str,
    index: int,
    timeout_seconds: float,
    wait_before_seconds: float,
    ready_frame_url_substring: str | None,
    browser_context_id: str | None = None,
    expected_counts: dict[str, int] | None = None,
) -> BrowserAttempt:
    """新建 page、禁缓存，从 Page.navigate 前的外部 monotonic 等到 ready。"""

    target_id = ""
    client: CDPClient | None = None
    started_at = datetime.now(timezone.utc).isoformat()
    navigation_started = time.monotonic()
    dashboard_requests: list[str] = []
    legacy_requests: list[str] = []
    dashboard_response_evidence: list[dict[str, Any]] = []
    dashboard_request_indices: dict[tuple[str, str], int] = {}
    console_errors: list[str] = []
    page_errors: list[str] = []
    ready: dict[str, Any] | None = None
    error_message: str | None = None
    try:
        client = CDPClient(_connect_websocket(browser.browser_websocket_url))
        client.call("Target.setDiscoverTargets", {"discover": True}, timeout=3)
        target_params: dict[str, Any] = {"url": "about:blank"}
        if browser_context_id is not None:
            target_params["browserContextId"] = browser_context_id
        target_id = str(client.call("Target.createTarget", target_params, timeout=3)["targetId"])
        root_session = str(
            client.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
                timeout=3,
            )["sessionId"]
        )
        configured_sessions = {root_session}
        session_targets = {root_session: target_id}
        session_target_types = {root_session: "page"}
        _enable_cdp_session(client, root_session, "page")
        contexts: dict[tuple[str, int], str] = {}
        frame_urls: dict[str, str] = {}
        frame_sessions: dict[str, str] = {target_id: root_session}
        _consume_cdp_events(
            client,
            configured_sessions=configured_sessions,
            session_targets=session_targets,
            session_target_types=session_target_types,
            contexts=contexts,
            frame_urls=frame_urls,
            frame_sessions=frame_sessions,
            dashboard_requests=dashboard_requests,
            legacy_requests=legacy_requests,
            dashboard_response_evidence=dashboard_response_evidence,
            dashboard_request_indices=dashboard_request_indices,
            console_errors=console_errors,
            page_errors=page_errors,
        )

        navigation_started = time.monotonic()
        navigation = client.call(
            "Page.navigate",
            {"url": url},
            timeout=timeout_seconds,
            session_id=root_session,
        )
        main_frame_id = str(navigation.get("frameId", ""))
        if not main_frame_id:
            raise RuntimeError("Page.navigate did not return a frameId")
        deadline = navigation_started + timeout_seconds
        next_frame_tree_refresh = navigation_started
        checked_context_urls: set[tuple[str, int]] = set()
        crashed = False
        while time.monotonic() < deadline and not crashed:
            crashed = _consume_cdp_events(
                client,
                configured_sessions=configured_sessions,
                session_targets=session_targets,
                session_target_types=session_target_types,
                contexts=contexts,
                frame_urls=frame_urls,
                frame_sessions=frame_sessions,
                dashboard_requests=dashboard_requests,
                legacy_requests=legacy_requests,
                dashboard_response_evidence=dashboard_response_evidence,
                dashboard_request_indices=dashboard_request_indices,
                console_errors=console_errors,
                page_errors=page_errors,
            ) or crashed
            if (
                ready_frame_url_substring
                and time.monotonic() >= next_frame_tree_refresh
                and not _matching_ready_frame_ids(
                    frame_urls,
                    main_frame_id,
                    ready_frame_url_substring,
                )
            ):
                tree_result = client.call(
                    "Page.getFrameTree",
                    timeout=max(0.05, min(1.0, deadline - time.monotonic())),
                    session_id=root_session,
                )
                _record_frame_tree(tree_result.get("frameTree"), frame_urls)
                next_frame_tree_refresh = time.monotonic() + 0.05
            if ready_frame_url_substring:
                for context_key, context_frame_id in list(contexts.items()):
                    if context_key in checked_context_urls:
                        continue
                    owner_session, context_id = context_key
                    try:
                        location_result = client.call(
                            "Runtime.evaluate",
                            {
                                "expression": "window.location.href",
                                "contextId": context_id,
                                "returnByValue": True,
                                "awaitPromise": False,
                            },
                            timeout=max(
                                0.05,
                                min(1.0, deadline - time.monotonic()),
                            ),
                            session_id=owner_session,
                        )
                    except CDPCommandError as error:
                        if not error.is_navigation_context_loss():
                            raise
                        contexts.pop(context_key, None)
                        continue
                    remote_location = location_result.get("result", {})
                    context_url = (
                        remote_location.get("value")
                        if isinstance(remote_location, dict)
                        else None
                    )
                    if isinstance(context_url, str):
                        safe_context_url = _safe_url_without_query(context_url)
                        if safe_context_url is not None:
                            frame_urls[context_frame_id] = safe_context_url
                    checked_context_urls.add(context_key)
            context_key = select_ready_context(
                contexts,
                frame_urls,
                main_frame_id,
                ready_frame_url_substring,
            )
            if context_key is not None:
                owner_session, context_id = context_key
                try:
                    result = client.call(
                        "Runtime.evaluate",
                        {
                            "expression": "window.__factorLabReady || null",
                            "contextId": context_id,
                            "returnByValue": True,
                            "awaitPromise": False,
                        },
                        timeout=max(0.05, min(1.0, deadline - time.monotonic())),
                        session_id=owner_session,
                    )
                except CDPCommandError as error:
                    if not error.is_navigation_context_loss():
                        raise
                    contexts.pop(context_key, None)
                    continue
                remote = result.get("result", {})
                value = remote.get("value") if isinstance(remote, dict) else None
                if isinstance(value, dict):
                    ready = value
                    break
            time.sleep(0.01)
        crashed = _consume_cdp_events(
            client,
            configured_sessions=configured_sessions,
            session_targets=session_targets,
            session_target_types=session_target_types,
            contexts=contexts,
            frame_urls=frame_urls,
            frame_sessions=frame_sessions,
            dashboard_requests=dashboard_requests,
            legacy_requests=legacy_requests,
            dashboard_response_evidence=dashboard_response_evidence,
            dashboard_request_indices=dashboard_request_indices,
            console_errors=console_errors,
            page_errors=page_errors,
        ) or crashed
        if crashed:
            raise RuntimeError("browser target crashed")
        if ready is None:
            matching_frames = len(
                _matching_ready_frame_ids(
                    frame_urls,
                    main_frame_id,
                    ready_frame_url_substring,
                )
                if ready_frame_url_substring is not None
                else set()
            )
            raise TimeoutError(
                "factor lab readiness timed out "
                f"sessions={len(configured_sessions)} frames={len(frame_urls)} "
                f"contexts={len(contexts)} matching_frames={matching_frames}"
            )
        ready_error = validate_ready_value(ready)
        if ready_error:
            raise ValueError(ready_error)
        if expected_counts is not None:
            for ready_field, expected in expected_counts.items():
                if ready.get(ready_field) != expected:
                    raise ValueError(
                        f"{ready_field}_mismatch expected={expected} actual={ready.get(ready_field)}"
                    )
        if len(dashboard_requests) != 1:
            raise ValueError(f"dashboard_request_count={len(dashboard_requests)}")
        dashboard_response_error = validate_dashboard_response_evidence(
            dashboard_requests,
            dashboard_response_evidence,
        )
        if dashboard_response_error:
            raise ValueError(dashboard_response_error)
        if legacy_requests:
            raise ValueError("legacy_request_detected")
        if console_errors:
            raise ValueError("console_error_detected")
        if page_errors:
            raise ValueError("page_error_detected")
    except Exception as error:
        error_message = f"{type(error).__name__}: {error}"
    finally:
        total_ms = (time.monotonic() - navigation_started) * 1000
        target_closed = not target_id
        if client is not None and target_id:
            target_closed = _close_target_with_fallback(client, browser, target_id)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if target_id and not target_closed:
            cleanup_error = "target_cleanup_failed"
            error_message = (
                f"{error_message}; {cleanup_error}"
                if error_message
                else f"RuntimeError: {cleanup_error}"
            )
    return BrowserAttempt(
        index=index,
        started_at=started_at,
        wait_before_seconds=wait_before_seconds,
        total_ms=total_ms,
        success=error_message is None,
        error=error_message,
        dashboard_request_urls=list(dashboard_requests),
        dashboard_response_evidence=list(dashboard_response_evidence),
        legacy_request_urls=list(legacy_requests),
        console_errors=console_errors,
        page_errors=page_errors,
        ready=ready,
    )


def _is_chrome(product: str) -> bool:
    folded = product.casefold()
    return "chrome/" in folded and "edg/" not in folded


def _is_edge(product: str) -> bool:
    folded = product.casefold()
    return "edg/" in folded or "edge/" in folded


def _browser_family(identity: str) -> str:
    if _is_edge(identity):
        return "edge"
    if _is_chrome(identity):
        return "chrome"
    return "unknown"


def _approval_reference_complete(reference: str | None) -> bool:
    return (
        isinstance(reference, str)
        and bool(reference.strip())
        and reference.strip().casefold() != "unknown"
    )


def summarize_browser_attempts(
    attempts: list[BrowserAttempt],
    *,
    browser_product: str,
    browser_user_agent: str,
    allow_edge_acceptance: bool,
    edge_approval_reference: str | None,
    metadata_complete: bool,
    expected_counts_configured: bool = True,
) -> dict[str, Any]:
    """硬编码真实浏览器正式门槛，不把失败样本移出 latency。"""

    values = [attempt.total_ms for attempt in attempts]
    latency = {
        "sample_count": len(values),
        "p50": _nearest_rank(values, 0.50) if values else None,
        "p95": _nearest_rank(values, 0.95) if values else None,
        "p99": _nearest_rank(values, 0.99) if values else None,
    }
    failures = [
        {"index": attempt.index, "error": attempt.error or "browser_probe_failed"}
        for attempt in attempts
        if not attempt.success
    ]
    formal = len(attempts) >= 200
    product_family = _browser_family(browser_product)
    user_agent_family = _browser_family(browser_user_agent)
    identity_matches = (
        product_family == user_agent_family
        and product_family in {"chrome", "edge"}
    )
    approved_browser = identity_matches and (
        product_family == "chrome"
        or (
            product_family == "edge"
            and allow_edge_acceptance
            and _approval_reference_complete(edge_approval_reference)
        )
    )
    gate_failures: list[str] = []
    if not formal:
        gate_failures.append("insufficient_attempts")
    if latency["p95"] is None or float(latency["p95"]) >= 1000:
        gate_failures.append("p95_not_below_1000ms")
    if failures:
        gate_failures.append("attempt_failures")
    if any(attempt.legacy_request_urls for attempt in attempts):
        gate_failures.append("legacy_request_detected")
    if any(attempt.ready and attempt.ready.get("stale") is True for attempt in attempts):
        gate_failures.append("stale_ready")
    if any(
        attempt.ready is None or validate_ready_value(attempt.ready)
        for attempt in attempts
    ):
        gate_failures.append("ready_invalid")
    if any(len(attempt.dashboard_request_urls) != 1 for attempt in attempts):
        gate_failures.append("dashboard_request_count_invalid")
    if any(
        validate_dashboard_response_evidence(
            attempt.dashboard_request_urls,
            attempt.dashboard_response_evidence,
        )
        == "dashboard_response_count_invalid"
        for attempt in attempts
    ):
        gate_failures.append("dashboard_response_count_invalid")
    if any(
        validate_dashboard_response_evidence(
            attempt.dashboard_request_urls,
            attempt.dashboard_response_evidence,
        )
        == "dashboard_redirect_detected"
        for attempt in attempts
    ):
        gate_failures.append("dashboard_redirect_detected")
    if any(
        validate_dashboard_response_evidence(
            attempt.dashboard_request_urls,
            attempt.dashboard_response_evidence,
        )
        == "dashboard_response_status_invalid"
        for attempt in attempts
    ):
        gate_failures.append("dashboard_response_status_invalid")
    if any(
        validate_dashboard_response_evidence(
            attempt.dashboard_request_urls,
            attempt.dashboard_response_evidence,
        )
        == "dashboard_response_evidence_invalid"
        for attempt in attempts
    ):
        gate_failures.append("dashboard_response_evidence_invalid")
    if any(
        validate_dashboard_response_evidence(
            attempt.dashboard_request_urls,
            attempt.dashboard_response_evidence,
        )
        == "dashboard_cache_evidence_invalid"
        for attempt in attempts
    ):
        gate_failures.append("dashboard_cache_evidence_invalid")
    if any(
        validate_dashboard_response_evidence(
            attempt.dashboard_request_urls,
            attempt.dashboard_response_evidence,
        )
        == "dashboard_cache_or_service_worker"
        for attempt in attempts
    ):
        gate_failures.append("dashboard_cache_or_service_worker")
    if any(attempt.console_errors for attempt in attempts):
        gate_failures.append("console_errors")
    if any(attempt.page_errors for attempt in attempts):
        gate_failures.append("page_errors")
    if not identity_matches:
        gate_failures.append("browser_identity_mismatch")
    if not approved_browser:
        gate_failures.append("browser_not_approved")
    if not metadata_complete:
        gate_failures.append("formal_metadata_incomplete")
    if not expected_counts_configured:
        gate_failures.append("expected_counts_not_configured")
    return {
        "attempt_count": len(attempts),
        "formal_acceptance": formal,
        "acceptance": formal and not gate_failures,
        "latency_ms": latency,
        "success_count": sum(attempt.success for attempt in attempts),
        "failure_count": len(failures),
        "failures": failures,
        "legacy_request_count": sum(len(attempt.legacy_request_urls) for attempt in attempts),
        "dashboard_cache_violation_count": sum(
            validate_dashboard_response_evidence(
                attempt.dashboard_request_urls,
                attempt.dashboard_response_evidence,
            )
            == "dashboard_cache_or_service_worker"
            for attempt in attempts
        ),
        "dashboard_cache_evidence_invalid_count": sum(
            validate_dashboard_response_evidence(
                attempt.dashboard_request_urls,
                attempt.dashboard_response_evidence,
            )
            == "dashboard_cache_evidence_invalid"
            for attempt in attempts
        ),
        "dashboard_redirect_violation_count": sum(
            validate_dashboard_response_evidence(
                attempt.dashboard_request_urls,
                attempt.dashboard_response_evidence,
            )
            == "dashboard_redirect_detected"
            for attempt in attempts
        ),
        "stale_count": sum(
            bool(attempt.ready and attempt.ready.get("stale")) for attempt in attempts
        ),
        "console_error_count": sum(len(attempt.console_errors) for attempt in attempts),
        "page_error_count": sum(len(attempt.page_errors) for attempt in attempts),
        "browser_approved": approved_browser,
        "browser_product_family": product_family,
        "browser_user_agent_family": user_agent_family,
        "metadata_complete": metadata_complete,
        "expected_counts_configured": expected_counts_configured,
        "gate_failures": gate_failures,
    }


def browser_exit_code(
    summary: dict[str, Any],
    *,
    mode: str,
    enforce_slo: bool,
) -> int:
    """区分显式正式验收、自动 200 样本验收和短 smoke。"""

    if mode == "soak":
        return 0 if summary.get("failure_count") == 0 else 1
    if enforce_slo and not summary.get("formal_acceptance"):
        return 2
    if summary.get("formal_acceptance"):
        return 0 if summary.get("acceptance") else 1
    return 0 if summary.get("failure_count") == 0 else 1


def _metadata_complete(arguments: argparse.Namespace) -> bool:
    return all(
        isinstance(value, str) and value.strip() and value.casefold() != "unknown"
        for value in (
            arguments.probe_location,
            arguments.device,
            arguments.network,
            arguments.connection_condition,
        )
    )


def _run_sequential(
    browser: OwnedBrowser, arguments: argparse.Namespace
) -> list[BrowserAttempt]:
    attempts: list[BrowserAttempt] = []
    previous_started: float | None = None
    for index in range(1, arguments.attempts + 1):
        wait_seconds = 0.0
        if previous_started is not None:
            wait_seconds = max(
                0.0,
                arguments.minimum_attempt_period_seconds
                - (time.monotonic() - previous_started),
            )
            if wait_seconds:
                time.sleep(wait_seconds)
        previous_started = time.monotonic()
        attempts.append(
            run_browser_attempt(
                browser=browser,
                url=arguments.url,
                index=index,
                timeout_seconds=arguments.timeout_seconds,
                wait_before_seconds=wait_seconds,
                ready_frame_url_substring=arguments.ready_frame_url_substring,
                expected_counts=_expected_counts(arguments),
            )
        )
    return attempts


def _run_soak(browser: OwnedBrowser, arguments: argparse.Namespace) -> list[BrowserAttempt]:
    attempts: list[BrowserAttempt] = []
    lock = threading.Lock()
    deadline = time.monotonic() + arguments.duration_seconds
    next_index = 1

    def worker() -> None:
        nonlocal next_index
        worker_started = time.monotonic()
        context_id: str | None = None
        try:
            context_id = str(
                _browser_call(browser, "Target.createBrowserContext", {})[
                    "browserContextId"
                ]
            )
            while time.monotonic() < deadline:
                started = time.monotonic()
                with lock:
                    index = next_index
                    next_index += 1
                attempt = run_browser_attempt(
                    browser=browser,
                    browser_context_id=context_id,
                    url=arguments.url,
                    index=index,
                    timeout_seconds=arguments.timeout_seconds,
                    wait_before_seconds=0.0,
                    ready_frame_url_substring=arguments.ready_frame_url_substring,
                    expected_counts=_expected_counts(arguments),
                )
                with lock:
                    attempts.append(attempt)
                wait_seconds = min(
                    max(0.0, arguments.refresh_interval_seconds - (time.monotonic() - started)),
                    max(0.0, deadline - time.monotonic()),
                )
                if wait_seconds:
                    time.sleep(wait_seconds)
        except Exception as error:
            with lock:
                index = next_index
                next_index += 1
                attempts.append(
                    BrowserAttempt(
                        index=index,
                        started_at=datetime.now(timezone.utc).isoformat(),
                        wait_before_seconds=0.0,
                        total_ms=(time.monotonic() - worker_started) * 1000,
                        success=False,
                        error=f"{type(error).__name__}: soak_worker_failed",
                        dashboard_request_urls=[],
                        dashboard_response_evidence=[],
                        legacy_request_urls=[],
                        console_errors=[],
                        page_errors=[],
                        ready=None,
                    )
                )
        finally:
            if context_id is not None:
                try:
                    _browser_call(
                        browser,
                        "Target.disposeBrowserContext",
                        {"browserContextId": context_id},
                    )
                except Exception:
                    pass

    threads = [threading.Thread(target=worker) for _ in range(arguments.concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=arguments.duration_seconds + arguments.timeout_seconds + 10)
    if any(thread.is_alive() for thread in threads):
        raise TimeoutError("soak workers did not stop within the bounded deadline")
    return sorted(attempts, key=lambda attempt: attempt.index)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    destination = path.expanduser().resolve()
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"output parent directory does not exist: {parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--attempts", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--browser-binary", type=Path, required=True)
    parser.add_argument("--ready-frame-url-substring")
    parser.add_argument("--minimum-attempt-period-seconds", type=float, default=0.0)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--refresh-interval-seconds", type=float)
    parser.add_argument("--allow-edge-acceptance", action="store_true")
    parser.add_argument("--enforce-slo", action="store_true")
    parser.add_argument("--edge-approval-reference")
    parser.add_argument("--probe-location", default="unknown")
    parser.add_argument("--device", default="unknown")
    parser.add_argument("--network", default="unknown")
    parser.add_argument("--connection-condition", default="unknown")
    parser.add_argument("--expected-scheme-count", type=int)
    parser.add_argument("--expected-live-row-count", type=int)
    parser.add_argument("--expected-backtest-row-count", type=int)
    arguments = parser.parse_args(argv)
    soak_values = (
        arguments.concurrency,
        arguments.duration_seconds,
        arguments.refresh_interval_seconds,
    )
    if any(value is not None for value in soak_values):
        if arguments.attempts is not None:
            parser.error("--attempts and soak mode are mutually exclusive")
        if arguments.enforce_slo:
            parser.error("--enforce-slo is only valid with sequential attempts")
        if any(value is None for value in soak_values):
            parser.error("soak mode requires concurrency, duration and refresh interval")
        if arguments.concurrency < 1:
            parser.error("--concurrency must be positive")
        if arguments.duration_seconds <= 0 or arguments.refresh_interval_seconds <= 0:
            parser.error("soak duration and refresh interval must be positive")
        arguments.mode = "soak"
    else:
        arguments.mode = "attempts"
        if arguments.attempts is None:
            arguments.attempts = 200
        if arguments.attempts < 1:
            parser.error("--attempts must be positive")
    if arguments.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if arguments.minimum_attempt_period_seconds < 0:
        parser.error("--minimum-attempt-period-seconds must be non-negative")
    if arguments.allow_edge_acceptance and not _approval_reference_complete(
        arguments.edge_approval_reference
    ):
        parser.error("--allow-edge-acceptance requires --edge-approval-reference")
    for field in (
        "expected_scheme_count",
        "expected_live_row_count",
        "expected_backtest_row_count",
    ):
        value = getattr(arguments, field)
        if value is not None and value < 0:
            parser.error(f"--{field.replace('_', '-')} must be non-negative")
    expected_values = (
        arguments.expected_scheme_count,
        arguments.expected_live_row_count,
        arguments.expected_backtest_row_count,
    )
    if any(value is not None for value in expected_values) and any(
        value is None for value in expected_values
    ):
        parser.error("all three --expected-*-count arguments must be supplied together")
    return arguments


def _expected_counts(arguments: argparse.Namespace) -> dict[str, int] | None:
    values = (
        arguments.expected_scheme_count,
        arguments.expected_live_row_count,
        arguments.expected_backtest_row_count,
    )
    if any(value is None for value in values):
        return None
    return {
        "schemeCount": values[0],
        "liveRowCount": values[1],
        "backtestRowCount": values[2],
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    parsed_url = urlsplit(arguments.url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise SystemExit("--url must be an absolute http(s) URL")
    started_at = datetime.now(timezone.utc)
    browser = start_owned_browser(arguments.browser_binary)
    try:
        attempts = (
            _run_sequential(browser, arguments)
            if arguments.mode == "attempts"
            else _run_soak(browser, arguments)
        )
        summary = summarize_browser_attempts(
            attempts,
            browser_product=browser.product,
            browser_user_agent=browser.user_agent,
            allow_edge_acceptance=arguments.allow_edge_acceptance,
            edge_approval_reference=arguments.edge_approval_reference,
            metadata_complete=_metadata_complete(arguments),
            expected_counts_configured=_expected_counts(arguments) is not None,
        )
        if arguments.mode == "soak":
            summary["formal_acceptance"] = False
            summary["acceptance"] = False
            summary["gate_failures"] = sorted(
                set(summary["gate_failures"] + ["soak_is_not_formal_acceptance"])
            )
        report = {
            "report_schema": "factor-lab-browser-benchmark-v1",
            "mode": arguments.mode,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "timezone": time.tzname[0] if time.tzname else "unknown",
            "metadata": {
                "url_origin_and_path": _safe_url_without_query(arguments.url),
                "user_agent": USER_AGENT,
                "browser_product": browser.product,
                "browser_user_agent": browser.user_agent,
                "cdp_protocol_version": browser.protocol_version,
                "browser_binary": str(arguments.browser_binary),
                "probe_location": arguments.probe_location,
                "device": arguments.device,
                "network": arguments.network,
                "connection_condition": arguments.connection_condition,
                "cache": "disabled_and_cleared_each_attempt",
                "service_worker": "bypassed",
                "page_isolation": "new_target_each_attempt",
                "timing_boundary": "external_monotonic_Page.navigate_to_ready",
                "minimum_attempt_period_seconds": arguments.minimum_attempt_period_seconds,
                "ready_frame_url_substring": arguments.ready_frame_url_substring,
                "edge_acceptance_approved": arguments.allow_edge_acceptance,
                "enforce_slo": arguments.enforce_slo,
                "edge_approval_reference": arguments.edge_approval_reference,
                "output_parent_policy": "parent_must_exist; atomic_replace",
                "soak_concurrency": arguments.concurrency,
                "soak_duration_seconds": arguments.duration_seconds,
                "soak_refresh_interval_seconds": arguments.refresh_interval_seconds,
                "expected_scheme_count": arguments.expected_scheme_count,
                "expected_live_row_count": arguments.expected_live_row_count,
                "expected_backtest_row_count": arguments.expected_backtest_row_count,
            },
            "summary": summary,
            "attempts": [asdict(attempt) for attempt in attempts],
        }
        write_json_atomic(arguments.output_json, report)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return browser_exit_code(
            summary,
            mode=arguments.mode,
            enforce_slo=arguments.enforce_slo,
        )
    finally:
        stop_owned_browser(browser)


if __name__ == "__main__":
    raise SystemExit(main())
