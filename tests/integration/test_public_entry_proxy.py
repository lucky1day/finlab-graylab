from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy import bindparam, text

from backend.auth import repository as auth_repository
from backend.auth.security import hash_password
from scripts.check_public_access import discover_same_origin_assets


pytestmark = pytest.mark.mysql_integration
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_HOST = "bond.finailab.cn"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _curl(port: int, path: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--insecure",
            "--noproxy",
            "*",
            "--resolve",
            f"{PUBLIC_HOST}:{port}:127.0.0.1",
            *extra,
            f"https://{PUBLIC_HOST}:{port}{path}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _wait_for_proxy(port: int, process: subprocess.Popen[str]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"nginx exited early: {stdout}\n{stderr}")
        result = _curl(
            port,
            "/bond-factor-lab/",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
        )
        if result.returncode == 0 and result.stdout == "200":
            return
        time.sleep(0.05)
    pytest.fail("temporary nginx did not become ready")


def _mysql_environment(
    engine: Engine,
    *,
    username: str,
    password: str,
) -> dict[str, str]:
    url = engine.url
    return {
        "BOND_DB_USER": username,
        "BOND_DB_PASSWORD": password,
        "BOND_DB_HOST": str(url.host or "127.0.0.1"),
        "BOND_DB_PORT": str(url.port or 3306),
        "BOND_DB_NAME": str(url.database or ""),
        "BOND_DB_CHARSET": "utf8mb4",
        "BFL_DEPLOYMENT_TARGET": "mac3-production",
        "BFL_AUTH_TRUSTED_ORIGIN": f"https://{PUBLIC_HOST}",
        "BOND_FACTOR_LAB_CONTROL_PLANE": "systemd_one_shot",
    }


@contextmanager
def _smoke_database_credentials(engine: Engine) -> Iterator[tuple[str, str]]:
    """创建非空密码、最小 DML 权限的短生命周期候选 Backend 账户。"""
    username = f"bfl_smoke_{uuid.uuid4().hex[:12]}"
    password = f"Smoke-{uuid.uuid4().hex}"
    database = str(engine.url.database)
    assert database.startswith("bfl_test_") and database.replace("_", "").isalnum()
    account = f"'{username}'@'127.0.0.1'"
    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE USER {account} IDENTIFIED BY :password"),
            {"password": password},
        )
        connection.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON `{database}`.* TO {account}"
            )
        )
    try:
        yield username, password
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP USER IF EXISTS {account}"))


@contextmanager
def _smoke_auth_user(engine: Engine) -> Iterator[tuple[str, str]]:
    """通过认证 Repository 创建只供真实代理冒烟的临时账户。"""
    suffix = uuid.uuid4().hex[:12]
    username = "admin"
    password = f"Proxy-{suffix}-A1"
    with engine.begin() as connection:
        auth_repository.initialize_protected_admin(
            connection,
            password_hash=hash_password(password),
        )
    yield username, password


@contextmanager
def _empty_dashboard_scope(engine: Engine) -> Iterator[None]:
    """让代理认证冒烟使用合法空 Summary，避免依赖方案测试数据。"""
    with engine.begin() as connection:
        active_scheme_ids = list(
            connection.execute(
                text(
                    "SELECT scheme_id FROM t_scheme_registry "
                    "WHERE status = 'active'"
                )
            ).scalars()
        )
        if active_scheme_ids:
            connection.execute(
                text(
                    "UPDATE t_scheme_registry SET status = 'paused' "
                    "WHERE status = 'active'"
                )
            )
    try:
        yield
    finally:
        if active_scheme_ids:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE t_scheme_registry SET status = 'active' "
                        "WHERE scheme_id IN :scheme_ids"
                    ).bindparams(
                        bindparam("scheme_ids", expanding=True)
                    ),
                    {"scheme_ids": active_scheme_ids},
                )


def _assert_authenticated_proxy_flow(
    port: int,
    temp_dir: Path,
    *,
    username: str,
    password: str,
) -> None:
    """经 TLS Nginx 完成真实登录、Cookie 回传和受保护读取。"""
    payload_path = temp_dir / "login-request.json"
    cookie_path = temp_dir / "cookies.txt"
    login_path = temp_dir / "login-response.json"
    me_path = temp_dir / "me-response.json"
    dashboard_path = temp_dir / "dashboard-response.json"
    payload_path.write_text(
        json.dumps({"username": username, "password": password}),
        encoding="utf-8",
    )
    payload_path.chmod(0o600)

    login = _curl(
        port,
        "/bond-factor-lab/api/auth/login",
        "--request", "POST",
        "--header", "Content-Type: application/json",
        "--header", f"Origin: https://{PUBLIC_HOST}",
        "--header", "Sec-Fetch-Site: same-origin",
        "--data-binary", f"@{payload_path}",
        "--cookie-jar", str(cookie_path),
        "--output", str(login_path),
        "--write-out", "%{http_code}",
    )
    assert login.returncode == 0, login.stderr
    assert login.stdout == "200"
    login_payload = json.loads(login_path.read_text(encoding="utf-8"))
    assert login_payload["user"]["username"] == username
    cookie_contents = cookie_path.read_text(encoding="utf-8")
    assert "__Host-bfl-session" in cookie_contents

    me = _curl(
        port,
        "/bond-factor-lab/api/auth/me",
        "--cookie", str(cookie_path),
        "--output", str(me_path),
        "--write-out", "%{http_code}",
    )
    assert me.returncode == 0, me.stderr
    assert me.stdout == "200"
    me_payload = json.loads(me_path.read_text(encoding="utf-8"))
    assert me_payload["user"]["username"] == username

    dashboard = _curl(
        port,
        "/bond-factor-lab/api/factor-lab/dashboard",
        "--cookie", str(cookie_path),
        "--output", str(dashboard_path),
        "--write-out", "%{http_code}",
    )
    assert dashboard.returncode == 0, dashboard.stderr
    assert dashboard.stdout == "200"
    dashboard_payload = json.loads(dashboard_path.read_text(encoding="utf-8"))
    assert dashboard_payload["schema_version"] == "factor-lab-dashboard-v7"


def _nginx_mime_types() -> Path:
    candidates = (
        Path("/etc/nginx/mime.types"),
        Path("/opt/homebrew/etc/nginx/mime.types"),
        Path("/usr/local/etc/nginx/mime.types"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.fail("nginx mime.types is unavailable")


def _render_site(
    temp_dir: Path,
    *,
    backend_port: int,
    http_port: int,
    https_port: int,
) -> Path:
    cert = temp_dir / "cert.pem"
    key = temp_dir / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-subj", f"/CN={PUBLIC_HOST}", "-days", "1",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    snippets = temp_dir / "snippets"
    snippets.mkdir()
    shutil.copyfile(
        PROJECT_ROOT / "deploy/nginx/snippets/bond-proxy-headers.conf",
        snippets / "bond-proxy-headers-20260722b.conf",
    )
    site = (PROJECT_ROOT / "deploy/nginx/bond-factor-lab.conf").read_text()
    site = site.replace("server 127.0.0.1:18100;", f"server 127.0.0.1:{backend_port};")
    site = site.replace("listen 80;", f"listen 127.0.0.1:{http_port};")
    site = site.replace("listen [::]:80;", "# IPv6 omitted in isolated smoke")
    site = site.replace("listen 443 ssl;", f"listen 127.0.0.1:{https_port} ssl;")
    site = site.replace("listen [::]:443 ssl;", "# IPv6 omitted in isolated smoke")
    site = site.replace(
        "/etc/letsencrypt/live/bond.finailab.cn/fullchain.pem",
        str(cert),
    )
    site = site.replace(
        "/etc/letsencrypt/live/bond.finailab.cn/privkey.pem",
        str(key),
    )
    site = site.replace(
        "/var/log/nginx/bond-factor-lab-access.log",
        str(temp_dir / "access.log"),
    )
    nginx_conf = temp_dir / "nginx.conf"
    nginx_conf.write_text(
        "\n".join(
            (
                f"pid {temp_dir / 'nginx.pid'};",
                f"error_log {temp_dir / 'error.log'} notice;",
                "events { worker_connections 64; }",
                "http {",
                f"include {_nginx_mime_types()};",
                site,
                "}",
            )
        ),
        encoding="utf-8",
    )
    return nginx_conf


@contextmanager
def _process(command: list[str], *, env: dict[str, str]) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_real_nginx_proxy_serves_every_page_asset_and_bootstraps_dashboard(
    mysql_test_engine: Engine,
    tmp_path: Path,
) -> None:
    """真实 Nginx 规则必须代理候选 FastAPI 的完整页面依赖链。"""
    nginx = shutil.which("nginx")
    node = shutil.which("node")
    curl = shutil.which("curl")
    openssl = shutil.which("openssl")
    assert nginx and node and curl and openssl
    backend_port = _free_port()
    http_port = _free_port()
    https_port = _free_port()
    nginx_conf = _render_site(
        tmp_path,
        backend_port=backend_port,
        http_port=http_port,
        https_port=https_port,
    )
    subprocess.run(
        [nginx, "-p", str(tmp_path), "-c", str(nginx_conf), "-t"],
        check=True,
        capture_output=True,
        text=True,
    )

    with (
        _empty_dashboard_scope(mysql_test_engine),
        _smoke_auth_user(mysql_test_engine) as auth_user,
        _smoke_database_credentials(mysql_test_engine) as credentials,
    ):
        environment = os.environ.copy()
        environment.update(
            _mysql_environment(
                mysql_test_engine,
                username=credentials[0],
                password=credentials[1],
            )
        )
        with _process(
            [
                sys.executable, "-B", "-m", "uvicorn", "backend.main:app",
                "--host", "127.0.0.1", "--port", str(backend_port),
                "--log-level", "warning",
            ],
            env=environment,
        ) as backend_process:
            with _process(
                [
                    nginx, "-p", str(tmp_path), "-c", str(nginx_conf),
                    "-g", "daemon off; master_process off;",
                ],
                env=environment,
            ) as nginx_process:
                _wait_for_proxy(https_port, nginx_process)
                assert backend_process.poll() is None
                page = _curl(https_port, "/bond-factor-lab/")
                assert page.returncode == 0
                assets = discover_same_origin_assets(
                    page.stdout,
                    page_url=f"https://{PUBLIC_HOST}/bond-factor-lab/",
                )
                script_paths: list[Path] = []
                for position, asset in enumerate(assets):
                    parsed = urlsplit(asset.url)
                    path_and_query = urlunsplit(
                        ("", "", parsed.path, parsed.query, "")
                    )
                    target = (
                        tmp_path / f"asset-{position}-{Path(parsed.path).name}"
                    )
                    response = _curl(
                        https_port,
                        path_and_query,
                        "--fail-with-body",
                        "--output",
                        str(target),
                    )
                    assert response.returncode == 0, response.stderr
                    assert target.stat().st_size > 0
                    if target.suffix == ".js":
                        script_paths.append(target)

                assert [
                    Path(asset.path).name
                    for asset in assets
                    if asset.path.endswith(".js")
                ] == [
                    "factor-lab-http.js",
                    "aifin-shell.js",
                    "auth-shell.js",
                ]
                anonymous = _curl(
                    https_port,
                    "/bond-factor-lab/api/factor-lab/dashboard",
                    "--output", "/dev/null", "--write-out", "%{http_code}",
                )
                assert anonymous.stdout == "401"
                health = _curl(
                    https_port,
                    "/bond-factor-lab/api/health",
                    "--output", "/dev/null", "--write-out", "%{http_code}",
                )
                assert health.stdout == "200"
                _assert_authenticated_proxy_flow(
                    https_port,
                    tmp_path,
                    username=auth_user[0],
                    password=auth_user[1],
                )
                subprocess.run(
                    [
                        node,
                        str(PROJECT_ROOT / "tests/frontend_proxy_bootstrap.js"),
                        *map(str, script_paths),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
