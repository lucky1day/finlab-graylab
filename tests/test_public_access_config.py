"""公网 Bond Factor Lab Nginx 策略的静态合同测试。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from tests.factor_lab_dashboard_conformance import (
    dashboard_v1_conformance_samples,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NGINX_PATH = PROJECT_ROOT / "deploy/nginx/bond-factor-lab.conf"
PROXY_SNIPPET_PATH = (
    PROJECT_ROOT / "deploy/nginx/snippets/bond-proxy-headers.conf"
)
CHECK_SCRIPT_PATH = PROJECT_ROOT / "scripts/check_public_access.sh"
DEPLOY_README_PATH = PROJECT_ROOT / "deploy/README.md"


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
    https_server = _https_server(config)
    guard = _extract_balanced_block(
        https_server,
        r'(?m)^\s*if\s*\(\s*\$request_uri\s+~\*\s+"[^\n]+"\s*\)\s*\{',
    )

    assert https_server.index(guard) < https_server.index("location")
    assert re.search(r"(?m)^\s*return\s+403\s*;", guard)
    for unsafe_token in ("//", "%2f", "%5c", "%2e", r"\.\."):
        assert unsafe_token in guard.lower()

    http_server = _http_server(config)
    http_guard = _extract_balanced_block(
        http_server,
        r'(?m)^\s*if\s*\(\s*\$request_uri\s+~\*\s+"[^\n]+"\s*\)\s*\{',
    )
    assert re.search(r"(?m)^\s*return\s+403\s*;", http_guard)


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
    assert "BOND_FACTOR_PUBLIC_POLICY_VERSION: 20260722a" in NGINX_PATH.read_text(
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


def test_proxy_snippet_is_deployed_as_part_of_the_versioned_policy() -> None:
    config = _without_comments(NGINX_PATH.read_text(encoding="utf-8"))
    readme = DEPLOY_README_PATH.read_text(encoding="utf-8")

    assert "include snippets/bond-proxy-headers.conf;" not in config
    assert config.count(
        "include snippets/bond-proxy-headers-20260722a.conf;"
    ) == 11
    assert (
        "/etc/nginx/snippets/bond-proxy-headers-20260722a.conf"
        in readme
    )


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
        "versioned-js",
        "versioned-js-gzip",
        "versioned-js-vary",
        "asset-icon",
        "asset-logo",
        "dashboard-get-gzip",
        "dashboard-head-gzip",
        "dashboard-get-identity",
        "dashboard-get-gzip-q0",
        "health-ready",
        "deny-docs",
        "deny-redoc",
        "deny-openapi",
        "deny-unknown-api",
        "deny-double-slash",
        "deny-dot-segment",
        "deny-encoded-slash",
        "deny-encoded-backslash",
        "deny-encoded-dot",
        "legacy-schemes",
        "legacy-metrics",
        "legacy-backtest",
    ):
        assert label in script

    assert "dashboard_snapshot" in script
    assert "factor-lab-dashboard-v1" in script
    assert "json.load" in script
    assert "gzip;q=0" in script
    assert "Vary" in script


def test_public_check_script_is_bash3_safe_and_head_is_snapshot_independent() -> None:
    script = CHECK_SCRIPT_PATH.read_text(encoding="utf-8")

    # macOS 自带 Bash 3.2 不支持 ${value,,}；探针需要能从开发机直接运行。
    assert not re.search(r"\$\{[^}]+,,\}", script)
    # 动态快照可能在 GET 与 HEAD 之间更新；分别验证正 Content-Length 即可，
    # 不能错误要求两个不同快照的压缩长度相同。
    assert 'local label="$1" expected_length="$2"' not in script
    assert '"$actual_length" == "$expected_length"' not in script


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


def test_deploy_readme_points_to_current_performance_work() -> None:
    readme = DEPLOY_README_PATH.read_text(encoding="utf-8")

    assert "docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md" in readme
    assert (
        "[公网性能运行手册]"
        "(../docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)"
        in readme
    )
    assert "Task 10 手册落地前禁止执行本次发布" in readme
    assert "该文件落地前不创建失效链接" not in readme
    assert "不改任何业务代码" not in readme
    assert "后端不参与" not in readme
    assert 'grep -Fxq "    default ${release_stage};"' in readme
    assert 'if [[ -n "$previous_target" ]]; then' in readme
    assert 'sudo rm -f -- "$active"' in readme


def test_regular_rollback_keeps_the_public_service_online() -> None:
    readme = DEPLOY_README_PATH.read_text(encoding="utf-8")
    assert "## 回滚" in readme
    assert "## 全站紧急下线" in readme
    rollback = readme.split("## 回滚", maxsplit=1)[1].split(
        "## 全站紧急下线",
        maxsplit=1,
    )[0]
    emergency = readme.split("## 全站紧急下线", maxsplit=1)[1]

    assert "launchctl bootout" not in rollback
    assert "SSH 反向隧道和服务必须保持在线" in rollback
    assert "launchctl bootout" in emergency
    assert "专项授权" in emergency


def test_nginx_reload_failure_restores_the_previous_live_policy() -> None:
    readme = DEPLOY_README_PATH.read_text(encoding="utf-8")
    deployment = readme.split("### 1) 公网入口机：Nginx", maxsplit=1)[1].split(
        "### 2) 本地 Mac",
        maxsplit=1,
    )[0]

    assert "restore_previous_link() {" in deployment
    assert "if ! sudo nginx -t; then" in deployment
    assert "if ! sudo systemctl reload nginx; then" in deployment
    assert deployment.count("restore_previous_link") >= 3
    assert "sudo nginx -t && sudo systemctl reload nginx" in deployment
    # 一次是候选 reload，一次是 previous policy 恢复；无 previous 的分支不得 reload。
    assert deployment.count("sudo systemctl reload nginx") == 2
    assert 'sudo rm -f -- "$active"' in deployment
    assert "reload 失败也必须以非零状态结束" in deployment
