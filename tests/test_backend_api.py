from __future__ import annotations

import unittest

from backend import main


class ApiBoundaryTests(unittest.TestCase):
    """Backend API 只暴露已审查的 Dashboard 与认证路由。"""

    def test_api_routes_match_reviewed_contract(self) -> None:
        actual: dict[str, set[str]] = {}
        for route in main.app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api"):
                actual.setdefault(path, set()).update(route.methods or set())
        self.assertEqual(
            actual,
            {
                "/api/auth/login": {"POST"},
                "/api/auth/logout": {"POST"},
                "/api/auth/me": {"GET"},
                "/api/auth/change-password": {"POST"},
                "/api/auth/update-profile": {"POST"},
                "/api/admin/users": {"GET", "POST"},
                "/api/admin/users/change-username": {"POST"},
                "/api/admin/users/change-role": {"POST"},
                "/api/admin/users/reset-password": {"POST"},
                "/api/admin/users/change-status": {"POST"},
                "/api/admin/users/update-profile": {"POST"},
                "/api/admin/users/edit": {"POST"},
                "/api/health": {"GET"},
                "/api/factor-lab/dashboard": {"GET", "HEAD"},
            },
        )
        self.assertEqual(
            {
                getattr(route, "path", "")
                for route in main.app.routes
                if not getattr(route, "path", "").startswith("/api")
            },
            {""},
        )
