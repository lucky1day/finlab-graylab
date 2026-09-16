#!/usr/bin/env python3
"""验证公网精确入口，并用公共 Dashboard Gate 完成认证正向验收。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PUBLIC_ORIGIN = "https://bond.finailab.cn"
APP_PREFIX = "/bond-factor-lab"
USER_AGENT = "bond-factor-lab-access-check/1.0"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
REVALIDATE_CACHE = "no-cache, must-revalidate"
MIME_BY_SUFFIX = {
    ".css": "text/css",
    ".js": ("application/javascript", "text/javascript"),
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": ("image/x-icon", "image/vnd.microsoft.icon"),
}


class PublicAccessError(RuntimeError):
    """公网入口合同不满足。"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True)
class AssetReference:
    """首页声明的一项同源静态资源。"""

    url: str
    path: str
    expected_mime: tuple[str, ...]
    version_digest: str | None


@dataclass(frozen=True)
class HttpResult:
    """一次不跟随重定向的 HTTP 结果。"""

    status: int
    headers: object
    body: bytes


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attribute = {
            "link": "href",
            "script": "src",
            "img": "src",
        }.get(tag.casefold())
        if attribute is None:
            return
        value = dict(attrs).get(attribute)
        if value:
            self.references.append(value)


def _expected_mime(path: str) -> tuple[str, ...] | None:
    value = MIME_BY_SUFFIX.get(Path(path).suffix.casefold())
    if value is None:
        return None
    return (value,) if isinstance(value, str) else value


def _version_digest(query: str) -> str | None:
    if not query:
        return None
    if "%" in query:
        raise PublicAccessError("asset version query must not be encoded")
    items = parse_qsl(query, keep_blank_values=True)
    if len(items) != 1 or items[0][0] not in {"v", "version"}:
        raise PublicAccessError("asset query must contain one version token")
    digest = items[0][1].casefold()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise PublicAccessError("asset version token must be a SHA-256 digest")
    return digest


def discover_same_origin_assets(
    html: str,
    *,
    page_url: str,
) -> tuple[AssetReference, ...]:
    """按页面顺序提取全部同源、已识别的静态引用。"""
    page = urlsplit(page_url)
    parser = _AssetParser()
    parser.feed(html)
    assets: list[AssetReference] = []
    seen: set[str] = set()
    for raw_reference in parser.references:
        absolute = urljoin(page_url, raw_reference)
        parsed = urlsplit(absolute)
        if (parsed.scheme, parsed.netloc) != (page.scheme, page.netloc):
            continue
        if parsed.username or parsed.password or parsed.fragment:
            raise PublicAccessError("ambiguous same-origin asset reference")
        expected_mime = _expected_mime(parsed.path)
        if expected_mime is None:
            raise PublicAccessError(
                f"unsupported same-origin static asset: {parsed.path}"
            )
        if absolute in seen:
            continue
        seen.add(absolute)
        assets.append(
            AssetReference(
                url=absolute,
                path=parsed.path,
                expected_mime=expected_mime,
                version_digest=_version_digest(parsed.query),
            )
        )
    if not assets:
        raise PublicAccessError("homepage contains no same-origin static assets")
    return tuple(assets)


def _request(url: str, *, method: str = "GET") -> HttpResult:
    request = Request(
        url,
        method=method,
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            return HttpResult(response.status, response.headers, response.read())
    except HTTPError as error:
        return HttpResult(error.code, error.headers, error.read())
    except URLError as error:
        raise PublicAccessError(f"request failed: {url}") from error


def _expect(url: str, status: int, *, method: str = "GET") -> HttpResult:
    result = _request(url, method=method)
    if result.status != status:
        raise PublicAccessError(
            f"{method} {url} returned {result.status}, expected {status}"
        )
    print(f"[PASS] {method} {url} status={status}")
    return result


def _header(result: HttpResult, name: str) -> str:
    return str(result.headers.get(name, "")).strip()


def _validate_asset(asset: AssetReference) -> None:
    get_result = _expect(asset.url, 200)
    if not get_result.body:
        raise PublicAccessError(f"empty static asset: {asset.url}")
    content_type = _header(get_result, "Content-Type").split(";", 1)[0].casefold()
    if content_type not in asset.expected_mime:
        raise PublicAccessError(
            f"unexpected MIME for {asset.url}: {content_type or 'missing'}"
        )
    expected_cache = IMMUTABLE_CACHE if asset.version_digest else REVALIDATE_CACHE
    if _header(get_result, "Cache-Control").casefold() != expected_cache.casefold():
        raise PublicAccessError(f"unexpected cache contract for {asset.url}")
    if _header(get_result, "X-Content-Type-Options").casefold() != "nosniff":
        raise PublicAccessError(f"missing nosniff for {asset.url}")
    if asset.version_digest is not None:
        actual_digest = hashlib.sha256(get_result.body).hexdigest()
        if actual_digest != asset.version_digest:
            raise PublicAccessError(f"version digest mismatch for {asset.url}")

    head_result = _expect(asset.url, 200, method="HEAD")
    if head_result.body:
        raise PublicAccessError(f"HEAD returned a body for {asset.url}")
    head_type = _header(head_result, "Content-Type").split(";", 1)[0].casefold()
    if head_type != content_type:
        raise PublicAccessError(f"GET/HEAD MIME mismatch for {asset.url}")
    content_length = _header(head_result, "Content-Length")
    if not content_length.isdigit() or int(content_length) != len(get_result.body):
        raise PublicAccessError(f"GET/HEAD length mismatch for {asset.url}")


def _dashboard_gate_command(
    args: argparse.Namespace,
) -> tuple[list[str], tuple[int, ...]]:
    command = [
        sys.executable,
        "-B",
        "-m",
        "harness",
        "gate",
        "dashboard",
        "--api-base-url",
        PUBLIC_ORIGIN,
        "--api-prefix",
        APP_PREFIX,
        "--project-root",
        str(args.project_root),
    ]
    for scheme_id in args.scheme_id:
        command.extend(("--scheme-id", scheme_id))
    if args.session_file is not None:
        command.extend(("--session-file", str(args.session_file)))
        return command, ()
    command.extend(("--session-fd", str(args.session_fd)))
    return command, (args.session_fd,)


def _validate_public_entry(args: argparse.Namespace) -> None:
    redirect = _expect("http://bond.finailab.cn/bond-factor-lab/", 301)
    if _header(redirect, "Location") != f"{PUBLIC_ORIGIN}{APP_PREFIX}/":
        raise PublicAccessError("HTTP canonical redirect is invalid")
    slash_redirect = _expect(f"{PUBLIC_ORIGIN}{APP_PREFIX}", 301)
    if _header(slash_redirect, "Location") != f"{PUBLIC_ORIGIN}{APP_PREFIX}/":
        raise PublicAccessError("application slash redirect is invalid")

    page = _expect(f"{PUBLIC_ORIGIN}{APP_PREFIX}/", 200)
    if not page.body:
        raise PublicAccessError("homepage is empty")
    try:
        html = page.body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PublicAccessError("homepage is not UTF-8") from error
    for asset in discover_same_origin_assets(
        html,
        page_url=f"{PUBLIC_ORIGIN}{APP_PREFIX}/",
    ):
        _validate_asset(asset)

    health = _expect(f"{PUBLIC_ORIGIN}{APP_PREFIX}/api/health", 200)
    try:
        health_payload = json.loads(health.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicAccessError("health response is not valid JSON") from error
    if health_payload.get("status") != "ok":
        raise PublicAccessError("health response is not ok")

    anonymous = _expect(
        f"{PUBLIC_ORIGIN}{APP_PREFIX}/api/factor-lab/dashboard",
        401,
    )
    if _header(anonymous, "Cache-Control").casefold() != "no-store":
        raise PublicAccessError("anonymous Dashboard response is cacheable")

    denied_paths: Iterable[str] = (
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/not-allowed",
        "/not-allowed",
        "/api/factor-lab/dashboard/",
        "//api/health",
        "/not-allowed/../api/health",
        "/api%2fhealth",
        "/api%5chealth",
        "/%2e%2e/api/health",
        "/api/he%61lth",
        "/%61ifin-shell.js",
        "/./",
        "/api/./health",
        "/api%252fhealth",
    )
    for path in denied_paths:
        _expect(f"{PUBLIC_ORIGIN}{APP_PREFIX}{path}", 403)
    _expect(f"{PUBLIC_ORIGIN}/Bond-Factor-Lab/", 403)

    command, pass_fds = _dashboard_gate_command(args)
    completed = subprocess.run(
        command,
        cwd=args.project_root,
        check=False,
        pass_fds=pass_fds,
    )
    if completed.returncode != 0:
        raise PublicAccessError("authenticated Dashboard Gate failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("origin")
    parser.add_argument("--scheme-id", action="append", required=True)
    session = parser.add_mutually_exclusive_group(required=True)
    session.add_argument("--session-file", type=Path)
    session.add_argument("--session-fd", type=int)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.origin.rstrip("/") != PUBLIC_ORIGIN:
        raise SystemExit(f"origin must be exactly {PUBLIC_ORIGIN}")
    args.project_root = args.project_root.resolve()
    if args.session_file is not None:
        args.session_file = args.session_file.resolve()
    if args.session_fd is not None and args.session_fd < 0:
        raise SystemExit("session fd must be non-negative")
    if len(set(args.scheme_id)) != len(args.scheme_id):
        raise SystemExit("scheme ids must not be repeated")
    try:
        _validate_public_entry(args)
    except PublicAccessError as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        return 1
    print("[PASS] public entry and authenticated Dashboard Gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
