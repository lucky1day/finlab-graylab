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


def test_public_timing_log_starts_with_timestamp_and_request_id() -> None:
    assert (
        "log_format bond_factor_timing '$time_iso8601 $request_id "
        "$remote_addr $bond_factor_traffic_class '"
    ) in SITE
    assert '"$request_method $uri $server_protocol" $status $upstream_status ' in SITE


def test_dashboard_upstream_has_five_second_read_budget() -> None:
    dashboard_location = SITE.split(
        "location = /bond-factor-lab/api/factor-lab/dashboard {", 1
    )[1].split("\n    }", 1)[0]
    assert "proxy_connect_timeout 1s;" in dashboard_location
    assert "proxy_read_timeout 5s;" in dashboard_location


def test_auth_locations_are_exact_and_state_changes_are_body_limited() -> None:
    exact_paths = (
        "/bond-factor-lab/auth-shell.js",
        "/bond-factor-lab/api/auth/me",
        "/bond-factor-lab/api/auth/login",
        "/bond-factor-lab/api/auth/logout",
        "/bond-factor-lab/api/auth/change-password",
        "/bond-factor-lab/api/auth/update-profile",
        "/bond-factor-lab/api/admin/users",
        "/bond-factor-lab/api/admin/users/change-username",
        "/bond-factor-lab/api/admin/users/change-role",
        "/bond-factor-lab/api/admin/users/reset-password",
        "/bond-factor-lab/api/admin/users/change-status",
        "/bond-factor-lab/api/admin/users/update-profile",
        "/bond-factor-lab/api/admin/users/edit",
    )
    for path in exact_paths:
        assert f"location = {path} {{" in SITE
    assert "zone=bond_factor_auth_login:10m rate=10r/m" in SITE
    assert "limit_req zone=bond_factor_auth_login burst=5 nodelay;" in SITE
    assert SITE.count("client_max_body_size 8k;") == 11
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
