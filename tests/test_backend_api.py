from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException, Response
from sqlalchemy import create_engine

from backend import main
from scheduler.blackbox_scheduler_admission import (
    EXPECTED_EXACT_ADMISSIONS,
    BlackboxSchedulerAdmissionError,
)


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


class TriggerEndpointTests(unittest.TestCase):
    """trigger 端点本体（auth 由 Depends 单独覆盖）：未知方案 404 / 已知方案 202。"""

    @staticmethod
    def _canonical(
        scheme_id: str,
        scheme_version: str,
        *,
        runtime_type: str = "blackbox_v2",
    ) -> SimpleNamespace:
        admission = EXPECTED_EXACT_ADMISSIONS[
            (scheme_id, scheme_version)
        ]
        return SimpleNamespace(
            scheme_id=scheme_id,
            scheme_version=scheme_version,
            status="active",
            version_status="active",
            runtime_type=runtime_type,
            frequency=admission.frequency,
            task_type=admission.task_type,
            horizon=admission.horizon,
            tenors=[admission.target_tenor],
        )

    @staticmethod
    def _registry_row(
        config: SimpleNamespace,
        *,
        registry_scheme_id: str | None = None,
        **overrides,
    ) -> dict:
        row = {
            "scheme_id": (
                registry_scheme_id
                or (
                    f"{config.scheme_id}__h{config.horizon}"
                    f"__{config.tenors[0]}"
                )
            ),
            "base_scheme_id": config.scheme_id,
            "runtime_type": config.runtime_type,
            "frequency": config.frequency,
            "task_type": config.task_type,
            "horizon": config.horizon,
            "target_tenor": config.tenors[0],
            "status": "active",
        }
        row.update(overrides)
        return row

    def test_trigger_preflight_denies_ungranted_control_plane_before_enqueue(
        self,
    ) -> None:
        scenarios = (
            self._canonical(
                "cgb_a4_fundseason_1y",
                "04e7af163fb0",
            ),
            self._canonical(
                "ten_y_t5_maj3_k3_ic_static_v1",
                "c54b90bcafa7",
            ),
        )
        for config in scenarios:
            background = BackgroundTasks()
            registry = self._registry_row(config)
            with (
                self.subTest(scheme_id=config.scheme_id),
                patch.object(main, "get_engine", return_value=object()),
                patch.object(
                    main,
                    "list_schemes",
                    return_value=[registry],
                ),
                patch(
                    "scheduler.direct_prediction.discover_schemes",
                    return_value=[config],
                ),
            ):
                with self.assertRaises(HTTPException) as raised:
                    main.api_trigger_scheme(
                        registry["scheme_id"],
                        main.TriggerRequest(
                            predict_date="2026-07-27"
                        ),
                        background,
                    )

            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(background.tasks, [])

    def test_trigger_preflight_fails_closed_when_admission_unavailable(
        self,
    ) -> None:
        config = self._canonical(
            "weekly_10y_lgbm_point_v1",
            "0666a6989d6b",
        )
        registry = self._registry_row(config)
        background = BackgroundTasks()
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[registry],
            ),
            patch(
                "scheduler.direct_prediction.discover_schemes",
                return_value=[config],
            ),
            patch(
                "scheduler.blackbox_scheduler_admission."
                "load_blackbox_scheduler_admission",
                side_effect=BlackboxSchedulerAdmissionError(
                    "policy unavailable"
                ),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    registry["scheme_id"],
                    main.TriggerRequest(
                        predict_date="2026-07-27"
                    ),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(background.tasks, [])

    def test_trigger_preflight_rejects_registry_config_route_drift(
        self,
    ) -> None:
        config = self._canonical(
            "weekly_10y_lgbm_point_v1",
            "0666a6989d6b",
        )
        registry = self._registry_row(
            config,
            frequency="daily",
        )
        background = BackgroundTasks()
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[registry],
            ),
            patch(
                "scheduler.direct_prediction.discover_schemes",
                return_value=[config],
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    registry["scheme_id"],
                    main.TriggerRequest(
                        predict_date="2026-07-27"
                    ),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(background.tasks, [])

    def test_trigger_preflight_rejects_composite_and_runtime_drift(
        self,
    ) -> None:
        config = self._canonical(
            "weekly_10y_lgbm_point_v1",
            "0666a6989d6b",
        )
        scenarios = (
            self._registry_row(
                config,
                registry_scheme_id=(
                    f"{config.scheme_id}__h5__10Y"
                ),
            ),
            self._registry_row(
                config,
                runtime_type="native_adapter",
            ),
        )
        for registry in scenarios:
            background = BackgroundTasks()
            with (
                self.subTest(registry=registry),
                patch.object(main, "get_engine", return_value=object()),
                patch.object(
                    main,
                    "list_schemes",
                    return_value=[registry],
                ),
                patch(
                    "scheduler.direct_prediction.discover_schemes",
                    return_value=[config],
                ),
            ):
                with self.assertRaises(HTTPException) as raised:
                    main.api_trigger_scheme(
                        registry["scheme_id"],
                        main.TriggerRequest(
                            predict_date="2026-07-27"
                        ),
                        background,
                    )

            self.assertEqual(raised.exception.status_code, 503)
            self.assertEqual(background.tasks, [])

    def test_trigger_preflight_maps_infrastructure_failures_to_503(
        self,
    ) -> None:
        """手动 trigger 的数据库连接失败仍须 fail-closed。"""
        background = BackgroundTasks()
        with patch.object(
            main,
            "get_engine",
            side_effect=RuntimeError("engine unavailable"),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    "any__h1__1Y",
                    main.TriggerRequest(),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(background.tasks, [])

    def test_trigger_preflight_maps_identity_drift_to_503(
        self,
    ) -> None:
        canonical = self._canonical(
            "weekly_10y_lgbm_point_v1",
            "0666a6989d6b",
        )
        drifted = SimpleNamespace(
            **{
                **vars(canonical),
                "scheme_version": "drifted-version",
            }
        )
        registry = self._registry_row(canonical)
        background = BackgroundTasks()
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[registry],
            ),
            patch(
                "scheduler.direct_prediction.discover_schemes",
                return_value=[drifted],
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    registry["scheme_id"],
                    main.TriggerRequest(
                        predict_date="2026-07-27"
                    ),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(background.tasks, [])

    def test_trigger_unknown_scheme_is_404(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "list_schemes", return_value=[{"scheme_id": "demo_daily__h1__10Y", "base_scheme_id": "demo_daily"}]
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.api_trigger_scheme(
                    "missing", main.TriggerRequest(), BackgroundTasks()
                )
            self.assertEqual(ctx.exception.status_code, 404)

    def test_trigger_preflight_rejects_direct_scheduled_without_manual_write_phase(
        self,
    ) -> None:
        """手工入口不能把非自然写入伪装成 launchd scheduled_live。"""
        config = self._canonical(
            "weekly_10y_lgbm_point_v1",
            "0666a6989d6b",
        )
        registry = self._registry_row(config)
        background = BackgroundTasks()
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[registry],
            ),
            patch(
                "scheduler.direct_prediction.discover_schemes",
                return_value=[config],
            ),
            patch(
                "scheduler.executor.discover_schemes",
                return_value=[config],
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    registry["scheme_id"],
                    main.TriggerRequest(predict_date="2026-06-09"),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn(
            "requires launchd_one_shot",
            str(raised.exception.detail),
        )
        self.assertEqual(background.tasks, [])

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

    def test_run_trigger_does_not_invoke_prediction_job_when_direct_write_is_unavailable(
        self,
    ) -> None:
        config = self._canonical(
            "weekly_10y_lgbm_point_v1",
            "0666a6989d6b",
        )
        registry = self._registry_row(config)
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[registry],
            ),
            patch(
                "scheduler.direct_prediction.discover_schemes",
                return_value=[config],
            ),
            patch(
                "scheduler.executor.discover_schemes",
                return_value=[config],
            ),
            patch.object(main, "run_prediction_job") as job_mock,
            self.assertLogs(main.logger, level="ERROR") as logs,
        ):
            main._run_trigger(
                registry["scheme_id"],
                "2026-07-27",
                True,
                None,
            )
        job_mock.assert_not_called()
        self.assertTrue(
            any(
                "admission revalidation failed" in message
                and "requires launchd_one_shot" in message
                for message in logs.output
            )
        )

class AdminRegistrySyncEndpointTests(unittest.TestCase):
    def test_admin_sync_runs_sync_with_force(self) -> None:
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
        self.assertTrue(sync_mock.call_args.kwargs.get("force"))
        self.assertTrue(result["synced"])

class StartupSyncTests(unittest.TestCase):
    def test_startup_runs_registry_sync_once(self) -> None:
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "sync_registry_from_configs",
            ) as sync_mock,
        ):
            main._sync_registry_on_startup()
        sync_mock.assert_called_once()

    def test_startup_swallows_sync_errors(self) -> None:
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "sync_registry_from_configs",
                side_effect=RuntimeError("db down"),
            ),
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
