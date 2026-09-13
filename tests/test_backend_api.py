from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

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

class HealthControlPlaneTests(unittest.TestCase):
    def test_health_accepts_systemd_one_shot_and_rejects_unknown_mode(self) -> None:
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        select_one = MagicMock()
        select_one.scalar_one.return_value = 1
        connection.execute.return_value = select_one

        with patch.object(main, "get_engine", return_value=engine):
            with patch.dict(
                os.environ,
                {"BOND_FACTOR_LAB_CONTROL_PLANE": "systemd_one_shot"},
                clear=False,
            ):
                result = main.health()

            self.assertEqual(
                result["control_plane"],
                "systemd_one_shot",
            )
            with (
                patch.dict(
                    os.environ,
                    {"BOND_FACTOR_LAB_CONTROL_PLANE": "cron"},
                    clear=False,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "unsupported scheduled one-shot control plane",
                ),
            ):
                main.health()
