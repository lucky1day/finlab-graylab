from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE = (
    PROJECT_ROOT / "deploy" / "nginx" / "bond-factor-lab.conf"
).read_text(encoding="utf-8")
LOCKDOWN = (
    PROJECT_ROOT
    / "deploy"
    / "nginx"
    / "bond-factor-lab-lockdown.conf"
).read_text(encoding="utf-8")


def test_auth_locations_are_exact_and_state_changes_are_body_limited() -> None:
    exact_paths = (
        "/bond-factor-lab/auth-shell.js",
        "/bond-factor-lab/api/auth/me",
        "/bond-factor-lab/api/auth/login",
        "/bond-factor-lab/api/auth/logout",
        "/bond-factor-lab/api/auth/change-password",
        "/bond-factor-lab/api/admin/users",
        "/bond-factor-lab/api/admin/users/change-username",
        "/bond-factor-lab/api/admin/users/change-role",
        "/bond-factor-lab/api/admin/users/reset-password",
        "/bond-factor-lab/api/admin/users/change-status",
    )
    for path in exact_paths:
        assert f"location = {path} {{" in SITE
    assert "zone=bond_factor_auth_login:10m rate=10r/m" in SITE
    assert "limit_req zone=bond_factor_auth_login burst=5 nodelay;" in SITE
    assert SITE.count("client_max_body_size 8k;") == 8
    assert "location ~ ^/bond-factor-lab/api(?:/|$)" in SITE


def test_lockdown_is_a_fail_closed_replacement_site() -> None:
    assert "must never be enabled alongside" in LOCKDOWN
    assert LOCKDOWN.count(
        "location = /bond-factor-lab { return 503; }"
    ) == 2
    assert LOCKDOWN.count(
        "location ^~ /bond-factor-lab/ { return 503; }"
    ) == 2
    assert LOCKDOWN.count("location / { return 403; }") == 2
    assert "proxy_pass" not in LOCKDOWN
