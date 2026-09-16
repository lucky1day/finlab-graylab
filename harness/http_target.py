from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


_PREFIX = re.compile(r"/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?\Z")


def url_origin(url: str) -> tuple[str, str, int]:
    """返回用于认证重定向比较的规范 HTTP Origin 身份。"""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("dashboard URL must use an HTTP(S) origin without credentials")
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError as exc:
        raise ValueError("dashboard URL contains an invalid port") from exc
    return parsed.scheme, parsed.hostname.lower(), port


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class ApiTarget:
    """分离认证 Origin 与应用路径前缀的 HTTP 目标。"""

    origin: str
    prefix: str

    def url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("dashboard endpoint path must be absolute")
        return f"{self.origin}{self.prefix}{path}"


def validate_api_target(base_url: object, api_prefix: object = "") -> ApiTarget:
    """严格规范化 Gate 目标，拒绝编码和路径歧义。"""
    raw_origin = str(base_url).strip()
    parsed = urlsplit(raw_origin)
    url_origin(raw_origin)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("dashboard api_base_url must be an HTTP(S) origin")
    if parsed.scheme == "http" and not _is_loopback(str(parsed.hostname)):
        raise ValueError("non-loopback dashboard probes require HTTPS")
    origin = raw_origin.rstrip("/")

    prefix = str(api_prefix).strip()
    if not prefix:
        return ApiTarget(origin=origin, prefix="")
    if (
        not _PREFIX.fullmatch(prefix)
        or prefix == "/"
        or prefix.endswith("/")
        or "//" in prefix
        or "\\" in prefix
        or "%" in prefix
        or any(segment in {".", ".."} for segment in prefix.split("/"))
    ):
        raise ValueError("dashboard api_prefix is invalid")
    return ApiTarget(origin=origin, prefix=prefix)
