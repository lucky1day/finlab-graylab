from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

from backend import main


def _clear_admin_token() -> None:
    os.environ.pop("BOND_ADMIN_TOKEN", None)


class RequireAdminTokenTests(unittest.TestCase):
    """直接调用 auth 依赖，覆盖缺失配置 / 缺失头 / 不匹配 / 匹配四种情形。"""

    def test_missing_server_config_is_open(self) -> None:
        # 软默认：未配置 BOND_ADMIN_TOKEN 时放行（单用户本机场景）。
        with patch.dict("os.environ", {}, clear=False):
            _clear_admin_token()
            self.assertIsNone(main.require_admin_token(x_admin_token=None))
            self.assertIsNone(main.require_admin_token(x_admin_token="anything"))

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


class MetricsCompareRemovedTests(unittest.TestCase):
    def test_metrics_compare_route_is_not_registered(self) -> None:
        """跨标的横向对比已下线，后端不再暴露 compare GET 路由。"""
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertNotIn("/api/metrics/compare", paths)


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


class PredictionsEndpointTests(unittest.TestCase):
    def test_predictions_endpoint_uses_registry_scheme_id_only(self) -> None:
        engine = object()
        with patch.object(main, "get_engine", return_value=engine), patch.object(
            main, "list_predictions", return_value={"items": []}
        ) as predictions_mock:
            result = main.api_predictions("demo_daily__h1__10Y")

        predictions_mock.assert_called_once()
        self.assertEqual(predictions_mock.call_args.args[:1], (engine,))
        self.assertEqual(predictions_mock.call_args.kwargs["scheme_id"], "demo_daily__h1__10Y")
        self.assertEqual(result, {"items": []})

    def test_predictions_endpoint_rejects_tenor_query_semantics(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            main.api_predictions("demo_daily__h1__10Y", tenor="10Y")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_predictions_endpoint_maps_unknown_registry_scheme_to_404(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "list_predictions", side_effect=LookupError("scheme not found: demo_daily")
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.api_predictions("demo_daily")
        self.assertEqual(ctx.exception.status_code, 404)


class SchemesLifecycleRemovedTests(unittest.TestCase):
    def test_schemes_lifecycle_route_is_not_registered(self) -> None:
        """方案生命周期健康概览已下线，后端不再暴露 lifecycle GET 路由。"""
        paths = {getattr(route, "path", None) for route in main.app.routes}
        self.assertNotIn("/api/schemes/lifecycle", paths)


class TriggerEndpointTests(unittest.TestCase):
    """trigger 端点本体（auth 由 Depends 单独覆盖）：未知方案 404 / 已知方案 202。"""

    def test_trigger_unknown_scheme_is_404(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "list_schemes", return_value=[{"scheme_id": "demo_daily__h1__10Y", "base_scheme_id": "demo_daily"}]
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.api_trigger_scheme(
                    "missing", main.TriggerRequest(), BackgroundTasks()
                )
            self.assertEqual(ctx.exception.status_code, 404)

    def test_trigger_known_scheme_is_accepted(self) -> None:
        background = BackgroundTasks()
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main,
            "list_schemes",
            return_value=[
                {"scheme_id": "demo_daily__h1__10Y", "base_scheme_id": "demo_daily", "status": "active"}
            ],
        ):
            result = main.api_trigger_scheme(
                "demo_daily__h1__10Y",
                main.TriggerRequest(predict_date="2026-06-09"),
                background,
            )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["scheme_id"], "demo_daily__h1__10Y")
        self.assertEqual(result["base_scheme_id"], "demo_daily")
        # 已排入后台任务（_run_trigger）。
        self.assertEqual(len(background.tasks), 1)
        task = background.tasks[0]
        self.assertEqual(task.args[0], "demo_daily")

    def test_trigger_non_active_scheme_is_404(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main,
            "list_schemes",
            return_value=[
                {"scheme_id": "demo_paused__h1__10Y", "base_scheme_id": "demo_paused", "status": "paused"},
                {"scheme_id": "demo_archived__h1__10Y", "base_scheme_id": "demo_archived", "status": "archived"},
            ],
        ):
            with self.assertRaises(HTTPException) as paused_ctx:
                main.api_trigger_scheme(
                    "demo_paused__h1__10Y",
                    main.TriggerRequest(predict_date="2026-06-09"),
                    BackgroundTasks(),
                )
            with self.assertRaises(HTTPException) as archived_ctx:
                main.api_trigger_scheme(
                    "demo_archived__h1__10Y",
                    main.TriggerRequest(predict_date="2026-06-09"),
                    BackgroundTasks(),
                )
        self.assertEqual(paused_ctx.exception.status_code, 404)
        self.assertEqual(archived_ctx.exception.status_code, 404)

    def test_run_trigger_invokes_prediction_job(self) -> None:
        with patch.object(main, "run_prediction_job") as job_mock:
            main._run_trigger("demo_daily", main.TriggerRequest(force=True))
        job_mock.assert_called_once()
        self.assertEqual(job_mock.call_args.kwargs.get("force"), True)


class AdminRegistrySyncEndpointTests(unittest.TestCase):
    def test_admin_sync_runs_sync_with_force(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "sync_registry_from_configs", return_value=True
        ) as sync_mock:
            result = main.api_admin_registry_sync()
        sync_mock.assert_called_once()
        self.assertTrue(sync_mock.call_args.kwargs.get("force"))
        self.assertTrue(result["synced"])


class StartupSyncTests(unittest.TestCase):
    def test_startup_runs_registry_sync_once(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "sync_registry_from_configs"
        ) as sync_mock:
            main._sync_registry_on_startup()
        sync_mock.assert_called_once()

    def test_startup_swallows_sync_errors(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "sync_registry_from_configs", side_effect=RuntimeError("db down")
        ):
            # 启动同步失败不应抛出（仅记录日志）。
            with self.assertLogs("backend.main", level="ERROR") as logs:
                main._sync_registry_on_startup()
        self.assertIn("Registry sync on startup failed", "\n".join(logs.output))


class AuthDependencyWiredTests(unittest.TestCase):
    """断言受保护端点确实声明了 require_admin_token 依赖。"""

    def _deps_for(self, path: str, method: str) -> list:
        for route in main.app.routes:
            if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
                return [d.call for d in route.dependant.dependencies]
        raise AssertionError(f"route not found: {method} {path}")

    def test_trigger_endpoint_requires_admin_token(self) -> None:
        deps = self._deps_for("/api/schemes/{scheme_id}/trigger", "POST")
        self.assertIn(main.require_admin_token, deps)

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
