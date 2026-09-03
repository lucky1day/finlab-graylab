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
    assert parser.account_menu_items == ["个人资料", "修改密码", "退出登录"]
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


def test_profile_password_help_and_admin_requests_use_fixed_api_paths() -> None:
    for path in (
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/api/auth/change-password",
        "/api/auth/update-profile",
        "/api/admin/users",
        "/api/admin/users/edit",
    ):
        assert path in AUTH_JS
    assert "authForcedPassword" not in INDEX
    assert "if (user.must_change_password)" not in AUTH_JS
    assert "密码不得少于6位，其中至少包含大写字母、小写字母和数字" in INDEX
    assert 'name="fullName"' in INDEX
    assert 'name="organizationName"' in INDEX
    assert "user.full_name" in AUTH_JS
    assert "user.organization_name" in AUTH_JS
    assert 'user.role !== "admin"' in AUTH_JS


def test_user_management_uses_one_edit_dialog_without_initial_avatars() -> None:
    assert "安全会话 · 仅限授权账户" not in INDEX
    assert "auth-user-avatar" not in AUTH_JS
    assert "window.prompt" not in AUTH_JS
    assert "window.confirm" not in AUTH_JS
    assert 'id="authUserEditDialog"' in INDEX
    assert 'aria-labelledby="authUserEditTitle"' in INDEX
    assert 'id="authUserEditForm"' in INDEX
    for field in (
        'name="username"',
        'name="fullName"',
        'name="organizationName"',
        'name="role"',
        'name="status"',
        'name="newPassword"',
    ):
        assert field in INDEX
    assert 'actionButton("编辑", "edit", user.id, false)' in AUTH_JS
    assert 'actionButton("编辑资料"' not in AUTH_JS
    assert 'actionButton("改用户名"' not in AUTH_JS
    assert 'actionButton("重置密码"' not in AUTH_JS
    assert 'dialog.dataset.saving = "true"' in AUTH_JS
    assert 'dialog.dataset.saving !== "true"' in AUTH_JS
    assert 'dialog.addEventListener("cancel"' in AUTH_JS
    assert 'input.type = "password"' in AUTH_JS
    assert 'button.textContent = "显示"' in AUTH_JS
    show_dialog = AUTH_JS[
        AUTH_JS.index("function showDialog(dialog)") :
        AUTH_JS.index("function closeDialog(dialog)")
    ]
    assert "dialog.showModal();" in show_dialog
    assert 'firstField = dialog.querySelector(' in show_dialog
    assert "firstField.focus();" in show_dialog
