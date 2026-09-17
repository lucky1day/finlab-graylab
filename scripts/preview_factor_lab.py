"""用仓库外的只读产品快照启动本机前端验收，不连接生产服务。"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine  # noqa: E402

from backend.factor_lab_dashboard import (  # noqa: E402
    DashboardQueryError,
    build_factor_lab_dashboard,
    build_factor_lab_dashboard_detail,
)


def main() -> None:
    """只允许回环访问、GET 请求和 SQLite 只读连接。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18120)
    args = parser.parse_args()
    snapshot = args.snapshot.resolve(strict=True)
    if snapshot.is_relative_to(PROJECT_ROOT):
        parser.error("快照必须保存在工作区之外")
    metadata = json.loads(snapshot.with_suffix(".json").read_text("utf-8"))
    os.environ["BFL_RUNTIME_ROOT"] = str(snapshot.parent)
    engine = create_engine(
        "sqlite+pysqlite://",
        creator=lambda: sqlite3.connect(
            snapshot.as_uri() + "?mode=ro&immutable=1", uri=True,
            check_same_thread=False,
        ),
    )
    frontend = PROJECT_ROOT / "frontend"
    notice = (
        '<aside style="position:fixed;bottom:10px;left:16px;z-index:10000;'
        'padding:7px 12px;border:1px solid #d7b44a;border-radius:6px;'
        'background:#fff9e6;color:#665019;font:12px sans-serif;pointer-events:none">'
        "本地验收 · 离线数据快照 "
        + html.escape(metadata["captured_at"])
        + " · 不影响线上系统</aside>"
    )

    class PreviewHandler(BaseHTTPRequestHandler):
        """本机验收仅提供静态资源与现有 Dashboard 读模型。"""

        def respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def json_response(self, status: int, payload: object) -> None:
            self.respond(
                status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            if self.headers.get("Host") not in {
                f"127.0.0.1:{args.port}", f"localhost:{args.port}",
            }:
                self.json_response(403, {"error": "local_preview_only"})
                return
            url = urlsplit(self.path)
            if url.path == "/api/auth/me":
                self.json_response(200, {"user": {
                    "id": 1, "username": "本地验收", "role": "user",
                    "status": "active", "is_protected_admin": False,
                    "must_change_password": False,
                    "created_at": metadata["captured_at"],
                    "full_name": None, "organization_name": None,
                }})
                return
            if url.path == "/api/factor-lab/dashboard":
                try:
                    pairs = parse_qsl(
                        url.query, keep_blank_values=True, strict_parsing=True,
                    )
                    query = dict(pairs)
                    if len(query) != len(pairs):
                        raise DashboardQueryError("duplicate query parameter")
                    if not query:
                        payload = build_factor_lab_dashboard(engine)
                    elif set(query) == {"start-date", "end-date"}:
                        payload = build_factor_lab_dashboard(
                            engine, start_date=query["start-date"],
                            end_date=query["end-date"],
                        )
                    elif set(query) == {"scheme-id", "month", "source"}:
                        payload = build_factor_lab_dashboard_detail(
                            engine, scheme_id=query["scheme-id"],
                            month=query["month"], source=query["source"],
                        )
                    else:
                        raise DashboardQueryError("invalid query parameters")
                    self.json_response(200 if payload is not None else 404, payload)
                except (DashboardQueryError, ValueError):
                    self.json_response(400, {"error": "invalid_dashboard_query"})
                except Exception:
                    self.log_error("local snapshot dashboard build failed")
                    traceback.print_exc()
                    self.json_response(500, {"error": "local_snapshot_build_failed"})
                return
            requested = "index.html" if url.path == "/" else unquote(url.path).lstrip("/")
            resource = (frontend / requested).resolve()
            if not resource.is_relative_to(frontend) or not resource.is_file():
                self.json_response(404, {"error": "not_found"})
                return
            body = resource.read_bytes()
            if resource.name == "index.html":
                body = body.replace(b"</body>", (notice + "</body>").encode("utf-8"))
            self.respond(
                200, body,
                (mimetypes.guess_type(resource.name)[0] or "application/octet-stream")
                + "; charset=utf-8",
            )

    server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Local preview: http://localhost:{args.port}/", flush=True)
    print(f"Snapshot captured: {metadata['captured_at']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.dispose()


if __name__ == "__main__":
    main()
