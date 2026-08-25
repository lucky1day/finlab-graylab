from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, Response
from backend import main


class RequireAdminTokenTests(unittest.TestCase):
    """直接调用 auth 依赖，覆盖缺失配置 / 缺失头 / 不匹配 / 匹配四种情形。"""

    def test_invalid_admin_token_states_fail_closed(self) -> None:
        for environ, header, status_code in (
            ({}, None, 503),
            ({"BOND_ADMIN_TOKEN": "s3cret"}, None, 401),
            ({"BOND_ADMIN_TOKEN": "s3cret"}, "wrong", 403),
        ):
            with self.subTest(status_code=status_code), patch.dict(
                os.environ,
                environ,
                clear=True,
            ):
                with self.assertRaises(HTTPException) as ctx:
                    main.require_admin_token(x_admin_token=header)
                self.assertEqual(ctx.exception.status_code, status_code)

    def test_correct_token_passes(self) -> None:
        with patch.dict("os.environ", {"BOND_ADMIN_TOKEN": "s3cret"}, clear=False):
            # 不抛异常即视为通过。
            self.assertIsNone(main.require_admin_token(x_admin_token="s3cret"))


class GetSchemesReadOnlyTests(unittest.TestCase):
    def test_api_schemes_does_not_call_registry_sync(self) -> None:
        """GET /api/schemes 读路径不得触发 registry 写库同步。"""
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "sync_registry_from_configs"
        ) as sync_mock, patch.object(
            main, "list_schemes", return_value=[{"scheme_id": "demo_daily"}]
        ) as list_mock:
            result = main.api_schemes()

        sync_mock.assert_not_called()
        list_mock.assert_called_once()
        self.assertEqual(result, [{"scheme_id": "demo_daily"}])


class HealthControlPlaneTests(unittest.TestCase):

    def test_health_reports_systemd_one_shot_when_installed(self) -> None:
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        select_one = MagicMock()
        select_one.scalar_one.return_value = 1
        connection.execute.return_value = select_one

        with (
            patch.dict(
                os.environ,
                {"BOND_FACTOR_LAB_CONTROL_PLANE": "systemd_one_shot"},
                clear=False,
            ),
            patch.object(main, "get_engine", return_value=engine),
        ):
            result = main.health()

        self.assertEqual(
            result["daily_schedule"]["mode"],
            "systemd_one_shot",
        )

    def test_health_rejects_unknown_control_plane(self) -> None:
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        select_one = MagicMock()
        select_one.scalar_one.return_value = 1
        connection.execute.return_value = select_one

        with (
            patch.dict(
                os.environ,
                {"BOND_FACTOR_LAB_CONTROL_PLANE": "cron"},
                clear=False,
            ),
            patch.object(main, "get_engine", return_value=engine),
            self.assertRaisesRegex(
                ValueError,
                "unsupported scheduled one-shot control plane",
            ),
        ):
            main.health()
class MetricsEndpointTests(unittest.TestCase):
    def test_metrics_endpoint_uses_registry_scheme_id_only(self) -> None:
        engine = object()
        with patch.object(main, "get_engine", return_value=engine), patch.object(
            main, "scheme_metrics", return_value={"scheme_id": "demo_daily__h1__10Y"}
        ) as metrics_mock:
            result = main.api_metrics("demo_daily__h1__10Y")

        metrics_mock.assert_called_once()
        self.assertEqual(metrics_mock.call_args.args[:2], (engine, "demo_daily__h1__10Y"))
        self.assertEqual(result["scheme_id"], "demo_daily__h1__10Y")

    def test_endpoints_reject_tenor_query_semantics(self) -> None:
        calls = {
            "metrics": lambda: main.api_metrics(
                "demo_daily__h1__10Y",
                tenor="10Y",
            ),
            "predictions": lambda: main.api_predictions(
                "demo_daily__h1__10Y",
                response=Response(),
                tenor="10Y",
            ),
        }
        for endpoint, call in calls.items():
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(HTTPException) as ctx:
                    call()
                self.assertEqual(ctx.exception.status_code, 400)


    def test_metrics_endpoint_rejects_malformed_month(self) -> None:
        for value in ("2026-9", "garbage", "2026-13", "2026-00"):
            for field in ("start_month", "end_month"):
                with self.subTest(value=value, field=field):
                    with self.assertRaises(HTTPException) as ctx:
                        main.api_metrics(
                            "demo_daily__h1__10Y",
                            **{field: value},
                        )
                    self.assertEqual(ctx.exception.status_code, 400)

    def test_metrics_endpoint_accepts_canonical_month(self) -> None:
        engine = object()
        with patch.object(main, "get_engine", return_value=engine), patch.object(
            main,
            "scheme_metrics",
            return_value={"scheme_id": "demo_daily__h1__10Y"},
        ) as metrics_mock:
            main.api_metrics(
                "demo_daily__h1__10Y",
                start_month="2026-01",
                end_month="2026-12",
            )

        self.assertEqual(metrics_mock.call_args.kwargs["start_month"], "2026-01")
        self.assertEqual(metrics_mock.call_args.kwargs["end_month"], "2026-12")


class PredictionsEndpointTests(unittest.TestCase):
    def test_predictions_endpoint_uses_registry_scheme_id_only(self) -> None:
        engine = object()
        response = Response()
        with patch.object(main, "get_engine", return_value=engine), patch.object(
            main, "list_predictions", return_value={"items": []}
        ) as predictions_mock:
            result = main.api_predictions(
                "demo_daily__h1__10Y",
                response=response,
            )

        predictions_mock.assert_called_once()
        self.assertEqual(predictions_mock.call_args.args[:1], (engine,))
        self.assertEqual(predictions_mock.call_args.kwargs["scheme_id"], "demo_daily__h1__10Y")
        self.assertEqual(result, {"items": []})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.headers["X-Prediction-Visibility"],
            "uncached-db",
        )

    def test_predictions_endpoint_maps_unknown_registry_scheme_to_404(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "list_predictions", side_effect=LookupError("scheme not found: demo_daily")
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.api_predictions(
                    "demo_daily",
                    response=Response(),
                )
        self.assertEqual(ctx.exception.status_code, 404)


class AdminRegistrySyncEndpointTests(unittest.TestCase):
    def test_admin_sync_runs_explicit_sync(self) -> None:
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "sync_registry_from_configs",
                return_value=True,
            ) as sync_mock,
        ):
            result = main.api_admin_registry_sync()
        sync_mock.assert_called_once()
        self.assertTrue(result["synced"])


class AuthDependencyWiredTests(unittest.TestCase):
    """断言受保护端点确实声明了 require_admin_token 依赖。"""

    def _deps_for(self, path: str, method: str) -> list:
        for route in main.app.routes:
            if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
                return [d.call for d in route.dependant.dependencies]
        raise AssertionError(f"route not found: {method} {path}")

    def test_admin_sync_endpoint_requires_admin_token(self) -> None:
        deps = self._deps_for("/api/admin/registry/sync", "POST")
        self.assertIn(main.require_admin_token, deps)


class CorsConfigTests(unittest.TestCase):
    def test_cors_origins_follow_environment(self) -> None:
        for environ, expected in (
            ({}, ["http://localhost", "http://127.0.0.1"]),
            (
                {"BOND_CORS_ORIGINS": "https://a.example , https://b.example"},
                ["https://a.example", "https://b.example"],
            ),
        ):
            with self.subTest(environ=environ), patch.dict(
                os.environ,
                environ,
                clear=True,
            ):
                self.assertEqual(main._cors_origins(), expected)

class DateParameterSemanticsTests(unittest.TestCase):
    """日期参数必须校验真实日历日期。"""

    IMPOSSIBLE = ("2026-99-99", "2026-02-31", "2026-13-01", "2026-04-31", "2026-02-29")

    def test_predictions_rejects_impossible_dates(self) -> None:
        for value in self.IMPOSSIBLE:
            for field in ("start_date", "end_date"):
                with self.subTest(value=value, field=field):
                    with self.assertRaises(HTTPException) as ctx:
                        main.api_predictions(
                            "demo_daily__h1__10Y",
                            response=Response(),
                            **{field: value},
                        )
                    self.assertEqual(ctx.exception.status_code, 400)

    def test_valid_dates_pass_through_unchanged(self) -> None:
        engine = object()
        with patch.object(main, "get_engine", return_value=engine), patch.object(
            main, "list_actuals", return_value={"items": []}
        ) as actuals_mock:
            # 2026 不是闰年：2026-02-29 会被正确拒绝，这里用真实存在的日期。
            main.api_actuals(start_date="2026-01-15", end_date="2026-02-28")

        self.assertEqual(actuals_mock.call_args.kwargs["start_date"], "2026-01-15")
        self.assertEqual(actuals_mock.call_args.kwargs["end_date"], "2026-02-28")
