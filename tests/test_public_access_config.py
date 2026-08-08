"""公网 Bond Factor Lab Nginx 策略的静态合同测试。"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

from tests.factor_lab_dashboard_conformance import (
    dashboard_v1_conformance_samples,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NGINX_PATH = PROJECT_ROOT / "deploy/nginx/bond-factor-lab.conf"
PROXY_SNIPPET_PATH = (
    PROJECT_ROOT / "deploy/nginx/snippets/bond-proxy-headers.conf"
)
CHECK_SCRIPT_PATH = PROJECT_ROOT / "scripts/check_public_access.sh"


def _without_comments(text: str) -> str:
    """删除 Nginx shell-style 注释，避免注释伪装成生效指令。"""

    return re.sub(r"(?m)^\s*#.*$", "", text)


def _extract_balanced_block(text: str, start_pattern: str) -> str:
    """返回匹配指令对应的完整花括号块。"""

    match = re.search(start_pattern, text, flags=re.MULTILINE)
    if match is None:
        raise AssertionError(f"missing block: {start_pattern}")
    opening = text.find("{", match.start(), match.end() + 1)
    if opening < 0:
        opening = text.find("{", match.end())
    if opening < 0:
        raise AssertionError(f"missing opening brace: {start_pattern}")

    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : index + 1]
    raise AssertionError(f"unclosed block: {start_pattern}")


def _exact_location(text: str, path: str) -> str:
    return _extract_balanced_block(
        text,
        rf"(?m)^\s*location\s+=\s+{re.escape(path)}\s*\{{",
    )


def _https_server(text: str) -> str:
    for match in re.finditer(r"(?m)^\s*server\s*\{", text):
        block = _extract_balanced_block(text[match.start() :], r"^\s*server\s*\{")
        if re.search(r"(?m)^\s*listen\s+(?:\[::\]:)?443\s+ssl\s*;", block):
            return block
    raise AssertionError("missing HTTPS server")


def _http_server(text: str) -> str:
    for match in re.finditer(r"(?m)^\s*server\s*\{", text):
        block = _extract_balanced_block(text[match.start() :], r"^\s*server\s*\{")
        if (
            re.search(r"(?m)^\s*listen\s+(?:\[::\]:)?80\s*;", block)
            and "443 ssl" not in block
        ):
            return block
    raise AssertionError("missing HTTP server")


def _unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_status(port: int, path: str) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", path, headers={"Host": "bond.finailab.cn"})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def test_public_paths_use_an_exact_allowlist_and_default_deny() -> None:
    config = _without_comments(NGINX_PATH.read_text(encoding="utf-8"))
    https_server = _https_server(config)

    allowed_paths = (
        "/bond-factor-lab/",
        "/bond-factor-lab/index.html",
        "/bond-factor-lab/aifin-shell.css",
        "/bond-factor-lab/aifin-shell.js",
        "/bond-factor-lab/assets/aifin-lab-icon.svg",
        "/bond-factor-lab/assets/aifin-lab-logo.svg",
        "/bond-factor-lab/api/health",
        "/bond-factor-lab/api/factor-lab/dashboard",
    )
    for path in allowed_paths:
        block = _exact_location(https_server, path)
        assert re.search(r"limit_except\s+GET\s+HEAD\s*\{\s*deny all;", block)
        assert "proxy_pass http://bond_factor_lab_backend" in block

    location_headers = re.findall(
        r"(?m)^\s*location\s+([^\{]+?)\s*\{",
        https_server,
    )
    assert "/bond-factor-lab/" not in {header.strip() for header in location_headers}

    root_deny = _extract_balanced_block(
        https_server,
        r"(?m)^\s*location\s+/\s*\{",
    )
    assert re.search(r"(?m)^\s*return\s+403\s*;", root_deny)

    dashboard_index = https_server.index(
        "location = /bond-factor-lab/api/factor-lab/dashboard"
    )
    api_deny_index = https_server.index(
        "location ~ ^/bond-factor-lab/api(?:/|$)"
    )
    assert dashboard_index < api_deny_index

    for forbidden in ("/docs", "/redoc", "/openapi.json"):
        assert not re.search(
            rf"(?m)^\s*location\s+(?:=\s+)?{re.escape(forbidden)}\s*\{{",
            https_server,
        )


def test_raw_request_uri_guard_precedes_location_matching() -> None:
    config = _without_comments(NGINX_PATH.read_text(encoding="utf-8"))
    unsafe_map = _extract_balanced_block(
        config,
        r"(?m)^\s*map\s+\$request_uri\s+\$bond_factor_unsafe_raw_path\s*\{",
    )
    assert r"%[0-9a-f][0-9a-f]" in unsafe_map.lower()
    assert "^[^?]*" in unsafe_map
    assert r"(?:\.|\.\.)" in unsafe_map
    assert "//" in unsafe_map

    https_server = _https_server(config)
    guard = _extract_balanced_block(
        https_server,
        r"(?m)^\s*if\s*\(\s*\$bond_factor_unsafe_raw_path\s*\)\s*\{",
    )

    assert https_server.index(guard) < https_server.index("location")
    assert re.search(r"(?m)^\s*return\s+403\s*;", guard)

    http_server = _http_server(config)
    http_guard = _extract_balanced_block(
        http_server,
        r"(?m)^\s*if\s*\(\s*\$bond_factor_unsafe_raw_path\s*\)\s*\{",
    )
    assert re.search(r"(?m)^\s*return\s+403\s*;", http_guard)


def test_real_nginx_rejects_normalization_bypasses_but_allows_version_query(
    tmp_path: Path,
) -> None:
    nginx = shutil.which("nginx")
    if nginx is None:
        pytest.skip("nginx is not installed; Task 12 must run the real entry config")

    class BackendHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    backend = ThreadingHTTPServer(("127.0.0.1", 0), BackendHandler)
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    public_port = _unused_tcp_port()
    redirect_port = _unused_tcp_port()
    prefix = tmp_path / "nginx"
    snippets = prefix / "snippets"
    snippets.mkdir(parents=True)

    site = NGINX_PATH.read_text(encoding="utf-8")
    snippet_match = re.search(r"include snippets/([^;]+);", site)
    assert snippet_match is not None
    (snippets / snippet_match.group(1)).write_text(
        PROXY_SNIPPET_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    site = site.replace(
        "server 127.0.0.1:18100;",
        f"server 127.0.0.1:{backend.server_port};",
    )
    site = site.replace("listen 80;", f"listen 127.0.0.1:{redirect_port};")
    site = site.replace("listen 443 ssl;", f"listen 127.0.0.1:{public_port};")
    site = re.sub(r"(?m)^\s*listen \[::\]:(?:80|443)(?: ssl)?;\n", "", site)
    site = re.sub(r"(?m)^\s*ssl_certificate(?:_key)?\s+[^;]+;\n", "", site)
    site = re.sub(
        r"(?m)^\s*access_log\s+[^;]+;",
        "    access_log off;",
        site,
    )
    config_path = prefix / "nginx.conf"
    config_path.write_text(
        f"pid {prefix / 'nginx.pid'};\n"
        f"error_log {prefix / 'error.log'} notice;\n"
        "events {}\n"
        f"http {{\n{site}\n}}\n",
        encoding="utf-8",
    )

    syntax = subprocess.run(
        [nginx, "-p", f"{prefix}/", "-c", str(config_path), "-t"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr
    process = subprocess.Popen(
        [nginx, "-p", f"{prefix}/", "-c", str(config_path), "-g", "daemon off;"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if _http_status(public_port, "/bond-factor-lab/") == 200:
                    break
            except OSError:
                time.sleep(0.02)
        else:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"nginx did not start: {stderr}")

        assert (
            _http_status(
                public_port,
                "/bond-factor-lab/aifin-shell.css?v=20260722a",
            )
            == 200
        )
        assert (
            _http_status(
                public_port,
                "/bond-factor-lab/aifin-shell.css?q=%61",
            )
            == 200
        )
        bypasses = (
            "/bond-factor-lab/api/he%61lth",
            "/bond-factor-lab/api/factor-lab/dashbo%61rd",
            "/bond-factor-lab/%61ifin-shell.js",
            "/bond-factor-lab/./",
            "/bond-factor-lab/api/./health",
            "/bond-factor-lab//api/health",
        )
        for path in bypasses:
            assert _http_status(public_port, path) == 403, path
        assert _http_status(redirect_port, "/%62ond-factor-lab/") == 403
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()


def test_redirects_are_fixed_to_the_canonical_host() -> None:
    config = _without_comments(NGINX_PATH.read_text(encoding="utf-8"))
    http_server = _http_server(config)
    https_server = _https_server(config)

    assert "return 301 https://bond.finailab.cn$request_uri;" in http_server
    assert "https://$host" not in http_server

    slash_redirect = _exact_location(https_server, "/bond-factor-lab")
    assert (
        "return 301 https://bond.finailab.cn/bond-factor-lab/;"
        in slash_redirect
    )
    assert "$host" not in slash_redirect


def test_release_stage_is_server_controlled_and_legacy_routes_are_guarded() -> None:
    config = _without_comments(NGINX_PATH.read_text(encoding="utf-8"))
    https_server = _https_server(config)
    release_map = _extract_balanced_block(
        config,
        r"(?m)^\s*map\s+\$server_addr\s+\$bond_factor_release_stage\s*\{",
    )

    assert re.search(r"(?m)^\s*default\s+rollout\s*;", release_map)
    assert "$arg_" not in release_map
    assert "$http_" not in release_map
    assert "BOND_FACTOR_PUBLIC_RELEASE_ID: 20260722b" in NGINX_PATH.read_text(
        encoding="utf-8"
    )

    legacy_paths = (
        "/bond-factor-lab/api/schemes",
        "/bond-factor-lab/api/backtests/factor-lab",
    )
    for path in legacy_paths:
        block = _exact_location(https_server, path)
        assert re.search(
            r"if\s*\(\s*\$bond_factor_release_stage\s*=\s*final\s*\)"
            r"\s*\{\s*return\s+403\s*;\s*\}",
            block,
        )
        assert "proxy_pass http://bond_factor_lab_backend" in block

    metrics = _extract_balanced_block(
        https_server,
        r"(?m)^\s*location\s+~\s+\^/bond-factor-lab/api/metrics/\[\^/\]\+\$\s*\{",
    )
    assert re.search(
        r"if\s*\(\s*\$bond_factor_release_stage\s*=\s*final\s*\)"
        r"\s*\{\s*return\s+403\s*;\s*\}",
        metrics,
    )


def test_dashboard_has_timing_limits_and_single_layer_compression() -> None:
    config = _without_comments(NGINX_PATH.read_text(encoding="utf-8"))
    https_server = _https_server(config)
    dashboard = _exact_location(
        https_server,
        "/bond-factor-lab/api/factor-lab/dashboard",
    )

    assert re.search(
        r"limit_req_zone\s+\$binary_remote_addr\s+"
        r"zone=bond_factor_dashboard:10m\s+rate=2r/s\s*;",
        config,
    )
    assert re.search(
        r"limit_conn_zone\s+\$binary_remote_addr\s+"
        r"zone=bond_factor_conn:10m\s*;",
        config,
    )
    assert re.search(r"log_format\s+bond_factor_timing\b", config)
    assert re.search(
        r"access_log\s+/var/log/nginx/bond-factor-lab-access\.log\s+"
        r"bond_factor_timing\s*;",
        https_server,
    )

    required_dashboard_directives = (
        r"limit_req\s+zone=bond_factor_dashboard\s+burst=20\s+nodelay\s*;",
        r"limit_req_status\s+429\s*;",
        r"limit_conn\s+bond_factor_conn\s+20\s*;",
        r"limit_conn_status\s+429\s*;",
        r"proxy_connect_timeout\s+1s\s*;",
        r"proxy_read_timeout\s+3s\s*;",
        r"proxy_buffering\s+on\s*;",
        r"gzip\s+off\s*;",
        r"gunzip\s+off\s*;",
    )
    for directive in required_dashboard_directives:
        assert re.search(directive, dashboard)

    # rollout 的旧 backtest 基线可超过 3 秒；短预算只能施加于新 dashboard，
    # 否则新前端上线前就会先破坏旧前端兼容窗口。
    server_defaults = https_server[: https_server.index("location")]
    assert re.search(r"proxy_connect_timeout\s+5s\s*;", server_defaults)
    assert re.search(r"proxy_read_timeout\s+30s\s*;", server_defaults)
    assert not re.search(r"proxy_read_timeout\s+3s\s*;", server_defaults)

    snippet = _without_comments(PROXY_SNIPPET_PATH.read_text(encoding="utf-8"))
    assert re.search(
        r"proxy_set_header\s+Accept-Encoding\s+\$http_accept_encoding\s*;",
        snippet,
    )
    assert re.search(
        r"proxy_set_header\s+X-Request-ID\s+\$request_id\s*;",
        snippet,
    )
    assert re.search(r"proxy_pass_header\s+Vary\s*;", snippet)


def test_timing_log_uses_bounded_traffic_classes_instead_of_raw_user_agent() -> None:
    config = _without_comments(NGINX_PATH.read_text(encoding="utf-8"))
    traffic_map = _extract_balanced_block(
        config,
        r"(?m)^\s*map\s+\$http_user_agent\s+\$bond_factor_traffic_class\s*\{",
    )
    assert re.search(r"(?m)^\s*default\s+user\s*;", traffic_map)
    assert '"bond-factor-lab-access-check/1.0" access_check;' in traffic_map
    assert '"bond-factor-lab-api-benchmark/1.0" api_benchmark;' in traffic_map
    timing_log = re.search(
        r"log_format\s+bond_factor_timing\s+(?P<body>.*?);",
        config,
        flags=re.DOTALL,
    )
    assert timing_log is not None
    assert "$bond_factor_traffic_class" in timing_log.group("body")
    assert "$http_user_agent" not in timing_log.group("body")


def test_proxy_snippet_is_deployed_as_part_of_the_versioned_policy() -> None:
    config = _without_comments(NGINX_PATH.read_text(encoding="utf-8"))

    assert "include snippets/bond-proxy-headers.conf;" not in config
    assert config.count(
        "include snippets/bond-proxy-headers-20260722b.conf;"
    ) == 11


def test_public_check_script_covers_protocol_and_bypass_matrix() -> None:
    script = CHECK_SCRIPT_PATH.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert re.search(r"--mode\s+rollout\|final", script)
    assert 'readonly USER_AGENT="bond-factor-lab-access-check/1.0"' in script
    assert '--user-agent "$USER_AGENT"' in script
    assert "--path-as-is" in script
    assert "mktemp -d" in script
    assert "trap 'rm -rf -- \"$CHECK_TMP_DIR\"' EXIT" in script
    assert not re.search(r"(?<!\S)-k(?:\s|$)", script)
    assert "%{http_code}" in script
    assert "%{size_download}" in script
    assert "%{time_total}" in script

    for label in (
        "http-redirect",
        "slash-redirect",
        "page",
        "versioned-css",
        "versioned-css-gzip",
        "versioned-css-vary",
        "versioned-css-body",
        "versioned-js",
        "versioned-js-gzip",
        "versioned-js-vary",
        "versioned-js-body",
        "asset-icon",
        "asset-logo",
        "dashboard-get-gzip",
        "dashboard-get-gzip-json",
        "dashboard-head-gzip",
        "dashboard-get-identity",
        "dashboard-get-identity-json",
        "dashboard-get-gzip-q0",
        "dashboard-get-gzip-q0-json",
        "deny-docs",
        "deny-redoc",
        "deny-openapi",
        "deny-unknown-api",
        "deny-double-slash",
        "deny-dot-segment",
        "deny-encoded-slash",
        "deny-encoded-backslash",
        "deny-encoded-dot",
        "deny-encoded-health",
        "deny-encoded-prefix",
        "deny-encoded-dashboard",
        "deny-encoded-static",
        "deny-single-dot",
        "deny-api-single-dot",
        "legacy-schemes",
        "legacy-metrics",
        "legacy-backtest",
        "legacy-backtest-gzip-encoding",
    ):
        assert label in script

    assert "dashboard_snapshot" not in script
    assert "factor-lab-dashboard-v1" in script
    assert "json.load" in script
    assert "gzip;q=0" in script
    assert "Vary" in script
    assert "--compressed" not in script
    legacy_start = script.index("run_request legacy-backtest GET")
    legacy_end = script.index("\nprintf 'Summary:", legacy_start)
    legacy_backtest = script[legacy_start:legacy_end]
    assert "--header 'Accept-Encoding: gzip'" in legacy_backtest
    assert 'if [[ "$MODE" == "rollout" ]]; then' in legacy_backtest
    assert (
        "assert_last_content_encoding legacy-backtest-gzip-encoding gzip"
        in legacy_backtest
    )


def test_public_check_script_is_bash3_safe_and_head_is_snapshot_independent() -> None:
    script = CHECK_SCRIPT_PATH.read_text(encoding="utf-8")

    # macOS 自带 Bash 3.2 不支持 ${value,,}；探针需要能从开发机直接运行。
    assert not re.search(r"\$\{[^}]+,,\}", script)
    # 动态快照可能在 GET 与 HEAD 之间更新；分别验证正 Content-Length 即可，
    # 不能错误要求两个不同快照的压缩长度相同。
    assert 'local label="$1" expected_length="$2"' not in script
    assert '"$actual_length" == "$expected_length"' not in script


def test_public_check_uses_versioned_assets_advertised_by_the_page(
    tmp_path: Path,
) -> None:
    """验收必须验证页面实际引用的资源，不能把一次性版本 token 写死。"""
    script = CHECK_SCRIPT_PATH.read_text(encoding="utf-8")
    assert "POLICY_VERSION" not in script
    match = re.search(
        r"# VERSIONED_ASSET_EXTRACTOR_BEGIN\n"
        r"(?P<extractor>.*?)\n"
        r"# VERSIONED_ASSET_EXTRACTOR_END",
        script,
        flags=re.DOTALL,
    )
    assert match is not None

    page = tmp_path / "index.html"
    page.write_text(
        """
        <link rel=\"stylesheet\" href=\"aifin-shell.css?v=css-next\">
        <script src=\"aifin-shell.js?v=js-next\"></script>
        """,
        encoding="utf-8",
    )

    def extract(asset_name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", match.group("extractor"), str(page), asset_name],
            check=False,
            capture_output=True,
            text=True,
        )

    assert extract("aifin-shell.css").stdout.strip() == "aifin-shell.css?v=css-next"
    assert extract("aifin-shell.js").stdout.strip() == "aifin-shell.js?v=js-next"

    page.write_text(
        '<script src="https://invalid.example/aifin-shell.js?v=js-next"></script>',
        encoding="utf-8",
    )
    assert extract("aifin-shell.js").returncode != 0


def test_public_check_script_extracts_a_real_composite_id_from_v1_payload(
    tmp_path: Path,
) -> None:
    script = CHECK_SCRIPT_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"# DASHBOARD_SCHEME_EXTRACTOR_BEGIN\n"
        r"(?P<extractor>.*?)\n"
        r"# DASHBOARD_SCHEME_EXTRACTOR_END",
        script,
        flags=re.DOTALL,
    )
    assert match is not None

    canonical = next(
        sample
        for sample in dashboard_v1_conformance_samples()
        if sample["name"] == "canonical"
    )
    payload = canonical["payload"]
    payload_path = tmp_path / "dashboard.json"
    payload_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-c", match.group("extractor"), str(payload_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    expected_scheme_id = payload["schemes"][0]["scheme_id"]
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == quote(expected_scheme_id, safe="")
    assert '"$APP_URL/api/metrics/$SCHEME_ID_ENCODED"' in script


def test_public_check_body_validator_rejects_corrupt_and_double_gzip(
    tmp_path: Path,
) -> None:
    script = CHECK_SCRIPT_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"# BODY_VALIDATOR_BEGIN\n"
        r"(?P<validator>.*?)\n"
        r"# BODY_VALIDATOR_END",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    validator = match.group("validator")
    canonical = next(
        sample["payload"]
        for sample in dashboard_v1_conformance_samples()
        if sample["name"] == "canonical"
    )
    raw_dashboard = json.dumps(canonical, ensure_ascii=False).encode("utf-8")
    cases = {
        "dashboard.json": raw_dashboard,
        "dashboard.json.gz": gzip.compress(raw_dashboard),
        "dashboard.double.gz": gzip.compress(gzip.compress(raw_dashboard)),
        "dashboard.corrupt.gz": b"\x1f\x8bnot-a-valid-stream",
        "dashboard.array.json": b"[]",
        "style.css.gz": gzip.compress(b"body { --surface-token: #fff; }"),
        "style.double.gz": gzip.compress(
            gzip.compress(b"body { --surface-token: #fff; }")
        ),
    }
    paths: dict[str, Path] = {}
    for name, body in cases.items():
        path = tmp_path / name
        path.write_bytes(body)
        paths[name] = path

    def validate(
        kind: str,
        encoding: str,
        name: str,
        token: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                validator,
                kind,
                encoding,
                str(paths[name]),
                token,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    assert validate("dashboard", "identity", "dashboard.json").returncode == 0
    assert validate("dashboard", "gzip", "dashboard.json.gz").returncode == 0
    assert validate("dashboard", "gzip", "dashboard.double.gz").returncode != 0
    assert validate("dashboard", "gzip", "dashboard.corrupt.gz").returncode != 0
    invalid_shape = validate("dashboard", "identity", "dashboard.array.json")
    assert invalid_shape.returncode != 0
    assert invalid_shape.stderr == ""
    assert (
        validate("text", "gzip", "style.css.gz", "--surface-token").returncode
        == 0
    )
    assert (
        validate("text", "gzip", "style.double.gz", "--surface-token").returncode
        != 0
    )


def test_public_check_curl_failure_reaches_summary_without_reusing_body(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    completed = subprocess.run(
        [
            str(CHECK_SCRIPT_PATH),
            "--mode",
            "rollout",
            "https://bond.finailab.cn",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 1
    assert "curl_exit=7" in completed.stdout
    assert "Summary:" in completed.stdout
    assert "[PASS] dashboard-scheme-id" not in completed.stdout


def test_header_parser_uses_final_block_and_exact_tokens(tmp_path: Path) -> None:
    script = CHECK_SCRIPT_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"# FINAL_HEADER_PARSER_BEGIN\n"
        r"(?P<parser>.*?)\n"
        r"# FINAL_HEADER_PARSER_END",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    parser = match.group("parser")

    final_headers = tmp_path / "final.headers"
    final_headers.write_bytes(
        b"HTTP/1.1 100 Continue\r\n"
        b"Content-Encoding: br\r\n\r\n"
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Encoding: gzip\r\n"
        b"Vary: X-Accept-Encoding-Foo, User-Agent\r\n"
        b"Vary: Accept-Encoding\r\n\r\n"
    )
    duplicate_headers = tmp_path / "duplicate.headers"
    duplicate_headers.write_bytes(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Encoding: gzip\r\n"
        b"Content-Encoding: br\r\n\r\n"
    )
    spoof_headers = tmp_path / "spoof.headers"
    spoof_headers.write_bytes(
        b"HTTP/1.1 200 OK\r\n"
        b"Vary: X-Accept-Encoding-Foo, User-Agent\r\n\r\n"
    )

    def parse(
        path: Path,
        name: str,
        mode: str,
        token: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", parser, str(path), name, mode, token],
            check=False,
            capture_output=True,
            text=True,
        )

    final_encoding = parse(final_headers, "Content-Encoding", "values")
    assert final_encoding.returncode == 0
    assert final_encoding.stdout.strip() == "gzip"
    duplicate_encoding = parse(duplicate_headers, "Content-Encoding", "values")
    assert duplicate_encoding.stdout.splitlines() == ["gzip", "br"]
    assert (
        parse(duplicate_headers, "Content-Encoding", "single-token", "gzip")
        .returncode
        != 0
    )
    assert parse(final_headers, "Vary", "token", "Accept-Encoding").returncode == 0
    assert parse(spoof_headers, "Vary", "token", "Accept-Encoding").returncode != 0
