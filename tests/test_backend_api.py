from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from backend import main


class ReadOnlyApiBoundaryTests(unittest.TestCase):
    """Backend API 只能暴露读取路由。"""

    def test_api_routes_are_read_only_and_admin_sync_is_absent(self) -> None:
        actual: dict[str, set[str]] = {}
        for route in main.app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api"):
                actual.setdefault(path, set()).update(route.methods or set())
        self.assertEqual(
            actual,
            {
                "/api/health": {"GET"},
                "/api/factor-lab/dashboard": {"GET", "HEAD"},
            },
        )
        self.assertEqual(
            {getattr(route, "path", "") for route in main.app.routes},
            {
                "/api/health",
                "/api/factor-lab/dashboard",
                "",
            },
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
