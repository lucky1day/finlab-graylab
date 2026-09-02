from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX = (PROJECT_ROOT / "frontend" / "index.html").read_text(
    encoding="utf-8"
)
AUTH_JS = (PROJECT_ROOT / "frontend" / "auth-shell.js").read_text(
    encoding="utf-8"
)
DASHBOARD_JS = (PROJECT_ROOT / "frontend" / "aifin-shell.js").read_text(
    encoding="utf-8"
)


class _AuthMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, dict[str, str | None]] = {}
        self.account_menu_items: list[str] = []
        self._menu_button = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "input" and (
            attributes.get("id") or attributes.get("name")
        ):
            key = str(attributes.get("id") or attributes["name"])
            self.inputs[key] = attributes
        if tag == "button" and attributes.get("role") == "menuitem":
            self._menu_button = True

    def handle_data(self, data: str) -> None:
        if self._menu_button and data.strip():
            self.account_menu_items.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            self._menu_button = False


def test_login_markup_supports_password_managers_and_exact_account_menu() -> None:
    parser = _AuthMarkupParser()
    parser.feed(INDEX)
    assert parser.inputs["authUsername"]["autocomplete"] == "username"
    assert parser.inputs["authPassword"]["autocomplete"] == (
        "current-password"
    )
    assert parser.account_menu_items == ["修改密码", "退出登录"]
    assert 'id="authUsersNav"' in INDEX
    assert 'id="authUsersNav" type="button" hidden' in INDEX


def test_dashboard_cannot_start_before_authentication() -> None:
    assert "authenticated: false" in DASHBOARD_JS
    assert "if (!factorLabRuntimeState.authenticated || !window.fetch)" in (
        DASHBOARD_JS
    )
    assert "startAuthenticatedFactorLab" in DASHBOARD_JS
    assert "clearAuthenticatedFactorLab();\n})();" in DASHBOARD_JS
    assert 'window.BondFactorLabDashboard.start();' in AUTH_JS
    assert AUTH_JS.rfind('apiRequest("/api/auth/me")') > AUTH_JS.find(
        "showGate(loadingView)"
    )


def test_auth_state_never_uses_browser_persistent_storage_or_url_tokens() -> None:
    combined = AUTH_JS + DASHBOARD_JS
    assert "localStorage" not in combined
    assert "sessionStorage" not in combined
    assert "token=" not in AUTH_JS
    assert "__Host-bfl-session" not in AUTH_JS
    assert 'credentials: "same-origin"' in AUTH_JS


def test_logout_and_401_clear_dashboard_state() -> None:
    assert 'window.dispatchEvent(new CustomEvent("bfl:auth-required"))' in (
        DASHBOARD_JS
    )
    assert 'window.addEventListener("bfl:auth-required"' in AUTH_JS
    assert "factorLabRuntimeState.aggregateCache = new Map();" in DASHBOARD_JS
    assert "factorLabRuntimeState.detailCache = new Map();" in DASHBOARD_JS
    assert 'controller.abort("authentication-ended")' in DASHBOARD_JS
    assert 'detailController.abort("authentication-ended")' in DASHBOARD_JS
    assert "function clearAuthDialogs()" in AUTH_JS
    assert 'document.querySelectorAll(".auth-dialog[open]")' in AUTH_JS
    assert "dialog.close();" in AUTH_JS
    assert AUTH_JS.index("clearAuthDialogs();") < AUTH_JS.index(
        "shell.hidden = true;"
    )


def test_forced_password_and_admin_requests_use_fixed_api_paths() -> None:
    for path in (
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/api/auth/change-password",
        "/api/admin/users",
        "/api/admin/users/change-username",
        "/api/admin/users/change-role",
        "/api/admin/users/reset-password",
        "/api/admin/users/change-status",
    ):
        assert path in AUTH_JS
    assert "if (user.must_change_password)" in AUTH_JS
    assert 'user.role !== "admin"' in AUTH_JS
