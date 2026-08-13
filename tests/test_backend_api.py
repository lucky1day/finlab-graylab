from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, Response
from sqlalchemy import create_engine

from backend import main


def _clear_admin_token() -> None:
    os.environ.pop("BOND_ADMIN_TOKEN", None)


class RequireAdminTokenTests(unittest.TestCase):
    """直接调用 auth 依赖，覆盖缺失配置 / 缺失头 / 不匹配 / 匹配四种情形。"""

    def test_missing_server_config_fails_closed(self) -> None:
        # 未配置 BOND_ADMIN_TOKEN 时禁用写接口，避免本地直连/隧道误配暴露写库入口。
        with patch.dict("os.environ", {}, clear=False):
            _clear_admin_token()
            with self.assertRaises(HTTPException) as ctx:
                main.require_admin_token(x_admin_token=None)
            self.assertEqual(ctx.exception.status_code, 503)

    def test_repository_placeholder_server_config_fails_closed(self) -> None:
        placeholder = "__SET_REAL_TOKEN__"
        with patch.dict(
            "os.environ",
            {"BOND_ADMIN_TOKEN": placeholder},
            clear=False,
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.require_admin_token(
                    x_admin_token=placeholder,
                )
            self.assertEqual(ctx.exception.status_code, 503)

    def test_missing_header_is_unauthorized(self) -> None:
        with patch.dict("os.environ", {"BOND_ADMIN_TOKEN": "s3cret"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                main.require_admin_token(x_admin_token=None)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_token_is_forbidden(self) -> None:
        with patch.dict("os.environ", {"BOND_ADMIN_TOKEN": "s3cret"}, clear=False):
            with self.assertRaises(HTTPException) as ctx:
                main.require_admin_token(x_admin_token="wrong")
            self.assertEqual(ctx.exception.status_code, 403)

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


class DailyScheduleCompatibilityTests(unittest.TestCase):
    def test_health_keeps_retired_daily_schedule_without_ledger_query(
        self,
    ) -> None:
        """兼容字段不得再从 heartbeat/occurrence 表投影状态。"""
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        select_one = MagicMock()
        select_one.scalar_one.return_value = 1
        connection.execute.side_effect = [
            select_one,
            AssertionError("health must not query retired daily ledger"),
        ]

        with (
            patch.object(main, "get_engine", return_value=engine),
            patch.object(main, "service_fingerprint_secret", return_value=None),
        ):
            result = main.health()

        self.assertEqual("ok", result["status"])
        self.assertEqual(
            {
                "mode": "launchd_one_shot",
                "overall": "not_enabled",
                "reasons": ["LEDGER_RETIRED"],
            },
            result["daily_schedule"],
        )
        self.assertEqual(1, connection.execute.call_count)

    def test_ledger_visibility_route_and_health_projection_are_removed(
        self,
    ) -> None:
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertNotIn("/api/daily-schedule/visibility", paths)
        self.assertFalse(hasattr(main, "_daily_schedule_health"))


class MetricsCompareRemovedTests(unittest.TestCase):
    def test_metrics_compare_route_is_not_registered(self) -> None:
        """跨标的横向对比已下线，后端不再暴露 compare GET 路由。"""
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertNotIn("/api/metrics/compare", paths)


class ManualTriggerRemovedTests(unittest.TestCase):
    def test_manual_prediction_trigger_route_is_not_registered(self) -> None:
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertNotIn("/api/schemes/{scheme_id}/trigger", paths)
        self.assertNotIn(
            "/api/schemes/{scheme_id}/trigger",
            main.app.openapi()["paths"],
        )

        sent: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b"{}"}

        async def send(message: dict) -> None:
            sent.append(message)

        asyncio.run(
            main.app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/api/schemes/demo__h1__10Y/trigger",
                    "raw_path": b"/api/schemes/demo__h1__10Y/trigger",
                    "query_string": b"",
                    "headers": [(b"content-type", b"application/json")],
                    "client": ("127.0.0.1", 1),
                    "server": ("127.0.0.1", 8100),
                    "root_path": "",
                },
                receive,
                send,
            )
        )
        response_start = next(
            message
            for message in sent
            if message["type"] == "http.response.start"
        )
        self.assertEqual(response_start["status"], 404)


class BacktestMonthlyMetricsRemovedTests(unittest.TestCase):
    def test_backtest_monthly_metrics_route_is_not_registered(self) -> None:
        """旧回测月度汇总表不再有 API 读入口。"""
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertNotIn("/api/backtests/runs/{run_id}/metrics", paths)


class TargetsEndpointTests(unittest.TestCase):
    def test_targets_endpoint_fallback_includes_1y_active_treasury(self) -> None:
        """未迁移目标注册表时，API fallback 也应暴露 1Y 国债活跃。"""
        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with patch.object(main, "get_engine", return_value=engine):
            result = main.api_targets()

        self.assertEqual(
            [item["target_code"] for item in result["targets"]],
            ["1Y", "3Y", "5Y", "7Y", "10Y"],
        )
        self.assertEqual(result["target_labels"]["1Y"], "1Y国债活跃")
        self.assertEqual(result["targets"][0]["extra"], {"legacy_tenor": "1Y"})


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

    def test_metrics_endpoint_rejects_tenor_query_semantics(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            main.api_metrics("demo_daily__h1__10Y", tenor="10Y")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_metrics_endpoint_rejects_non_ascii_month_digits(self) -> None:
        """月份过滤只接受 ASCII YYYY-MM，避免 Unicode 数字进入字符串比较。"""
        for value in ("٢٠٢٦-01", "２０２６-01"):
            for field in ("start_month", "end_month"):
                with self.subTest(value=value, field=field):
                    with self.assertRaises(HTTPException) as ctx:
                        main.api_metrics(
                            "demo_daily__h1__10Y",
                            **{field: value},
                        )
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

    def test_predictions_endpoint_rejects_tenor_query_semantics(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            main.api_predictions(
                "demo_daily__h1__10Y",
                response=Response(),
                tenor="10Y",
            )
        self.assertEqual(ctx.exception.status_code, 400)

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


class SchemesLifecycleRemovedTests(unittest.TestCase):
    def test_schemes_lifecycle_route_is_not_registered(self) -> None:
        """方案生命周期健康概览已下线，后端不再暴露 lifecycle GET 路由。"""
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertNotIn("/api/schemes/lifecycle", paths)


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
    def test_default_cors_origins(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("BOND_CORS_ORIGINS", None)
            self.assertEqual(
                main._cors_origins(), ["http://localhost", "http://127.0.0.1"]
            )

    def test_cors_origins_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"BOND_CORS_ORIGINS": "https://a.example , https://b.example"},
            clear=False,
        ):
            self.assertEqual(
                main._cors_origins(), ["https://a.example", "https://b.example"]
            )

    def test_blank_env_falls_back_to_default(self) -> None:
        with patch.dict("os.environ", {"BOND_CORS_ORIGINS": "  "}, clear=False):
            self.assertEqual(
                main._cors_origins(), ["http://localhost", "http://127.0.0.1"]
            )


if __name__ == "__main__":
    unittest.main()


class DateParameterSemanticsTests(unittest.TestCase):
    """日期参数必须校验日历语义，不能只校验字符形状（issue #41）。

    `^\d{4}-\d{2}-\d{2}$` 里的 `\d{2}` 同样匹配 `99`，也匹配 2 月的 `31`。
    这类输入过了形状检查后直接进 SQL 日期比较，由 MySQL 被动承担最后的日期
    验证职责，而数据库异常又被呈现为 HTTP 500——同一类用户输入错误按字符
    外观走两套通道，污染服务错误率且客户端无法区分参数问题与平台故障。
    """

    IMPOSSIBLE = ("2026-99-99", "2026-02-31", "2026-13-01", "2026-04-31", "2026-02-29")
    MALFORMED = ("garbage", "2026-1-15", "20260115", "2026-01-15T00:00", "")

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

    def test_actuals_rejects_impossible_dates(self) -> None:
        for value in self.IMPOSSIBLE:
            for field in ("start_date", "end_date"):
                with self.subTest(value=value, field=field):
                    with self.assertRaises(HTTPException) as ctx:
                        main.api_actuals(**{field: value})
                    self.assertEqual(ctx.exception.status_code, 400)

    def test_malformed_dates_use_the_same_channel(self) -> None:
        """形状错误与语义错误必须同为 400，不再按字符外观分流。"""
        for value in self.MALFORMED:
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as ctx:
                    main.api_actuals(start_date=value)
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

    def test_none_dates_remain_optional(self) -> None:
        engine = object()
        with patch.object(main, "get_engine", return_value=engine), patch.object(
            main, "list_actuals", return_value={"items": []}
        ) as actuals_mock:
            main.api_actuals()

        self.assertIsNone(actuals_mock.call_args.kwargs["start_date"])
        self.assertIsNone(actuals_mock.call_args.kwargs["end_date"])
