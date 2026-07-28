from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
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


class DailyScheduleHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.epoch_identity = {
            "epoch": 7,
            "mode": "ledger",
            "record_sha256": "e" * 64,
        }
        self._identity_patcher = patch.object(
            main,
            "require_current_daily_coordinator_identity",
            return_value=SimpleNamespace(
                mode="ledger",
                policy_payload=lambda: dict(self.epoch_identity),
            ),
            create=True,
        )
        self._identity_patcher.start()
        self.addCleanup(self._identity_patcher.stop)
        self.now = datetime(
            2026,
            7,
            23,
            23,
            0,
            tzinfo=timezone.utc,
        )

    def _heartbeat(
        self,
        *,
        occurrence_id: int | None = None,
        state: str = "IDLE",
        age_seconds: int = 15,
        coordinator_mode: str | None = "ledger",
    ) -> SimpleNamespace:
        details = {"business_date": "2026-07-24"}
        if coordinator_mode is not None:
            details["coordinator_mode"] = coordinator_mode
        details["daily_coordinator_epoch"] = dict(
            self.epoch_identity
        )
        return SimpleNamespace(
            service_name="daily-coordinator",
            state=state,
            occurrence_id=occurrence_id,
            heartbeat_at=self.now - timedelta(seconds=age_seconds),
            details=details,
        )

    def _patch_now(self):
        mocked_datetime = MagicMock(wraps=datetime)
        mocked_datetime.now.return_value = self.now
        return patch.object(main, "datetime", mocked_datetime)

    def test_idle_without_occurrence_rejects_stale_heartbeat(self) -> None:
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "read_scheduler_heartbeat",
                return_value=self._heartbeat(age_seconds=121),
            ),
            patch(
                "shared.calendar_service.CalendarService.is_trading_day",
                return_value=False,
            ),
            self._patch_now(),
        ):
            result = main._daily_schedule_health(object())

        self.assertEqual(result["overall"], "error")
        self.assertIn("HEARTBEAT_STALE", result["reasons"])
        self.assertEqual(
            result["scheduler_heartbeat"]["status"],
            "stale",
        )

    def test_trading_day_after_0630_requires_current_occurrence(self) -> None:
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "read_scheduler_heartbeat",
                return_value=self._heartbeat(),
            ),
            patch(
                "shared.calendar_service.CalendarService.is_trading_day",
                return_value=True,
            ),
            self._patch_now(),
        ):
            result = main._daily_schedule_health(object())

        self.assertEqual(result["overall"], "error")
        self.assertIn("OCCURRENCE_MISSING", result["reasons"])

    def test_ledger_health_rejects_cross_process_mode_mismatch(
        self,
    ) -> None:
        for coordinator_mode in (None, "legacy"):
            with (
                self.subTest(coordinator_mode=coordinator_mode),
                patch.object(
                    main,
                    "_daily_coordinator_mode",
                    return_value="ledger",
                ),
                patch.object(
                    main,
                    "read_scheduler_heartbeat",
                    return_value=self._heartbeat(
                        coordinator_mode=coordinator_mode,
                    ),
                ),
                self._patch_now(),
            ):
                result = main._daily_schedule_health(object())

            self.assertEqual(result["overall"], "error")
            self.assertIn("MODE_MISMATCH", result["reasons"])

    def test_health_epoch_resolution_failure_is_safe_and_fail_closed(
        self,
    ) -> None:
        with patch.object(
            main,
            "require_current_daily_coordinator_identity",
            side_effect=RuntimeError(
                "password=must-not-reach-health"
            ),
        ):
            result = main._daily_schedule_health(object())

        self.assertEqual(result["overall"], "error")
        self.assertEqual(
            result["reasons"],
            ["COORDINATOR_EPOCH_UNAVAILABLE"],
        )
        self.assertNotIn(
            "must-not-reach-health",
            repr(result),
        )

    def test_occurrence_predict_date_must_equal_local_today(self) -> None:
        heartbeat = self._heartbeat(
            occurrence_id=91,
            state="COMPLETE",
        )
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                occurrence_id=91,
                predict_date="2026-07-23",
                policy_json={
                    "daily_coordinator_epoch": dict(
                        self.epoch_identity
                    )
                },
            ),
            items=(),
        )
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                main,
                "read_schedule_occurrence_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                main,
                "read_schedule_health_envelope",
                return_value=object(),
                create=True,
            ) as read_health,
            patch.object(
                main,
                "read_schedule_execution_envelope",
            ) as read_execution,
            patch.object(
                main,
                "project_daily_health",
                return_value={"overall": "ok", "reasons": []},
            ),
            self._patch_now(),
        ):
            result = main._daily_schedule_health(object())

        self.assertEqual(result["overall"], "error")
        self.assertIn("OCCURRENCE_DATE_MISMATCH", result["reasons"])
        read_health.assert_not_called()
        read_execution.assert_not_called()

    def test_occurrence_health_uses_observability_envelope_not_execution_gate(
        self,
    ) -> None:
        engine = object()
        heartbeat = self._heartbeat(
            occurrence_id=91,
            state="RUNNING",
        )
        item = SimpleNamespace(item_id=7)
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                occurrence_id=91,
                predict_date="2026-07-24",
                policy_json={
                    "daily_coordinator_epoch": dict(
                        self.epoch_identity
                    )
                },
            ),
            items=(SimpleNamespace(item=item),),
        )
        health_envelope = object()
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                main,
                "read_schedule_occurrence_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                main,
                "read_schedule_health_envelope",
                return_value=health_envelope,
                create=True,
            ) as read_health,
            patch.object(
                main,
                "read_schedule_execution_envelope",
                side_effect=AssertionError(
                    "health must not invoke the strict execution gate"
                ),
            ) as read_execution,
            patch.object(
                main,
                "project_daily_health",
                return_value={"overall": "error", "reasons": []},
            ) as project,
            self._patch_now(),
        ):
            result = main._daily_schedule_health(engine)

        self.assertEqual(result["overall"], "error")
        read_health.assert_called_once_with(engine, item_id=7)
        read_execution.assert_not_called()
        self.assertEqual(
            project.call_args.kwargs["execution_envelopes"],
            (health_envelope,),
        )

    def test_top_level_health_keeps_disabled_ledger_neutral_in_legacy(
        self,
    ) -> None:
        engine = MagicMock()
        connection = (
            engine.connect.return_value.__enter__.return_value
        )
        connection.execute.return_value.scalar_one.return_value = 1
        with (
            patch.object(main, "get_engine", return_value=engine),
            patch.object(
                main,
                "_daily_schedule_health",
                return_value={
                    "mode": "legacy",
                    "overall": "not_enabled",
                    "reasons": ["LEDGER_ROLLOUT_DISABLED"],
                },
            ),
            patch.object(
                main,
                "service_fingerprint_secret",
                return_value=None,
            ),
        ):
            result = main.health()

        self.assertEqual(result["status"], "ok")


class DailyScheduleVisibilityProbeTests(unittest.TestCase):
    observed_at = datetime(
        2026,
        7,
        24,
        0,
        0,
        tzinfo=timezone.utc,
    )
    epoch_identity = {
        "epoch": 7,
        "mode": "ledger",
        "record_sha256": "e" * 64,
    }

    def _identity(self) -> SimpleNamespace:
        return SimpleNamespace(
            mode="ledger",
            policy_payload=lambda: dict(self.epoch_identity),
        )

    def _heartbeat(
        self,
        *,
        heartbeat_at: datetime | None = None,
        coordinator_mode: str = "ledger",
        business_date: str = "2026-07-24",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            service_name="daily-coordinator",
            state="COMPLETE",
            occurrence_id=91,
            heartbeat_at=(
                heartbeat_at
                if heartbeat_at is not None
                else self.observed_at - timedelta(seconds=15)
            ),
            details={
                "coordinator_mode": coordinator_mode,
                "business_date": business_date,
                "daily_coordinator_epoch": dict(self.epoch_identity),
            },
        )

    def _snapshot(
        self,
        *,
        predict_date: str = "2026-07-24",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            occurrence=SimpleNamespace(
                occurrence_id=91,
                predict_date=predict_date,
                policy_json={
                    "daily_coordinator_epoch": dict(
                        self.epoch_identity
                    )
                },
            ),
            items=(),
        )

    def _probe(self) -> SimpleNamespace:
        return SimpleNamespace(
            occurrence_id=91,
            observed_at=self.observed_at,
            expected_target_count=2,
            committed_registry_ids=("a__h1__5Y",),
            linked_registry_ids=("a__h1__5Y",),
            db_visible_registry_ids=("a__h1__5Y",),
            missing_registry_ids=("b__h1__10Y",),
            receipt_missing_registry_ids=(),
            source_generation="f" * 64,
        )

    def _call(
        self,
        *,
        heartbeat: SimpleNamespace | None = None,
        snapshot: SimpleNamespace | None = None,
    ) -> dict[str, object]:
        engine = object()
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                return_value=self._identity(),
                create=True,
            ),
            patch.object(main, "get_engine", return_value=engine),
            patch.object(
                main,
                "read_scheduler_heartbeat",
                return_value=heartbeat or self._heartbeat(),
            ),
            patch.object(
                main,
                "read_schedule_occurrence_snapshot",
                return_value=snapshot or self._snapshot(),
            ),
            patch.object(
                main,
                "read_schedule_api_visibility_probe",
                return_value=self._probe(),
                create=True,
            ),
        ):
            return main.api_daily_schedule_visibility(response=Response())

    def test_probe_is_uncached_and_does_not_relabel_db_receipt_as_api_timestamp(
        self,
    ) -> None:
        response = Response()
        heartbeat = self._heartbeat()
        probe = self._probe()
        engine = object()
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                return_value=self._identity(),
                create=True,
            ),
            patch.object(main, "get_engine", return_value=engine),
            patch.object(
                main,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                main,
                "read_schedule_occurrence_snapshot",
                return_value=self._snapshot(),
            ),
            patch.object(
                main,
                "read_schedule_api_visibility_probe",
                return_value=probe,
                create=True,
            ) as read_probe,
        ):
            result = main.api_daily_schedule_visibility(
                response=response,
            )

        read_probe.assert_called_once_with(engine, occurrence_id=91)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(
            response.headers["X-Prediction-Visibility"],
            "uncached-db-probe",
        )
        self.assertEqual(result["probe_completed"], True)
        self.assertEqual(result["predict_date"], "2026-07-24")
        self.assertEqual(result["db_visible"], 1)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(
            result["api_receipt_semantics"],
            "caller_must_receive_this_uncached_response",
        )
        self.assertNotIn("api_visible_at", result)

    def test_epoch_mismatch_returns_503_before_visibility_db_probe(
        self,
    ) -> None:
        heartbeat = self._heartbeat()
        heartbeat.details["daily_coordinator_epoch"] = {
            **self.epoch_identity,
            "record_sha256": "f" * 64,
        }
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                return_value=self._identity(),
                create=True,
            ),
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                main,
                "read_schedule_occurrence_snapshot",
            ) as read_snapshot,
            patch.object(
                main,
                "read_schedule_api_visibility_probe",
            ) as read_probe,
            self.assertRaises(HTTPException) as caught,
        ):
            main.api_daily_schedule_visibility(response=Response())

        self.assertEqual(caught.exception.status_code, 503)
        read_snapshot.assert_not_called()
        read_probe.assert_not_called()

    def test_probe_rejects_stale_scheduler_heartbeat(self) -> None:
        stale = self._heartbeat(
            heartbeat_at=self.observed_at - timedelta(seconds=121),
        )

        with self.assertRaises(HTTPException) as caught:
            self._call(heartbeat=stale)

        self.assertEqual(caught.exception.status_code, 503)

    def test_probe_rejects_previous_day_occurrence(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._call(snapshot=self._snapshot(predict_date="2026-07-23"))

        self.assertEqual(caught.exception.status_code, 503)

    def test_probe_rejects_heartbeat_mode_or_business_date_mismatch(
        self,
    ) -> None:
        mismatches = (
            self._heartbeat(coordinator_mode="legacy"),
            self._heartbeat(business_date="2026-07-23"),
        )
        for heartbeat in mismatches:
            with self.subTest(details=heartbeat.details):
                with self.assertRaises(HTTPException) as caught:
                    self._call(heartbeat=heartbeat)
                self.assertEqual(caught.exception.status_code, 503)


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
    def _coordinator_identity(
        mode: str,
    ) -> SimpleNamespace:
        payload = {
            "epoch": 2,
            "mode": mode,
            "record_sha256": "2" * 64,
        }
        return SimpleNamespace(
            mode=mode,
            policy_payload=lambda: dict(payload),
        )

    @staticmethod
    def _occurrence_snapshot(
        config: SimpleNamespace,
        identity: SimpleNamespace,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            occurrence=SimpleNamespace(
                policy_json={
                    "daily_coordinator_epoch":
                        identity.policy_payload(),
                }
            ),
            items=(
                SimpleNamespace(
                    item=SimpleNamespace(
                        base_scheme_id=config.scheme_id,
                        runtime_type=config.runtime_type,
                        scheme_version=config.scheme_version,
                    )
                ),
            ),
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
            (
                self._canonical(
                    "cgb_a4_fundseason_1y",
                    "04e7af163fb0",
                ),
                "legacy",
            ),
            (
                self._canonical(
                    "ten_y_t5_maj3_k3_ic_static_v1",
                    "c54b90bcafa7",
                ),
                "legacy",
            ),
        )
        for config, mode in scenarios:
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
                patch.object(
                    main,
                    "_daily_coordinator_mode",
                    return_value=mode,
                ),
                patch(
                    "scheduler.main.discover_schemes",
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
                "scheduler.main.discover_schemes",
                return_value=[config],
            ),
            patch(
                "scheduler.main.load_blackbox_scheduler_admission",
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
                "scheduler.main.discover_schemes",
                return_value=[config],
            ),
            patch.object(
                main,
                "find_schedule_occurrence_id",
            ) as occurrence,
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
        occurrence.assert_not_called()

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
                    "scheduler.main.discover_schemes",
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

    def test_ledger_daily_force_and_missing_occurrence_identity_are_conflicts(
        self,
    ) -> None:
        config = self._canonical(
            "ten_y_t5_maj3_k3_ic_static_v1",
            "c54b90bcafa7",
        )
        registry = self._registry_row(config)
        identity = self._coordinator_identity("ledger")
        missing_snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                policy_json={
                    "daily_coordinator_epoch":
                        identity.policy_payload(),
                }
            ),
            items=(),
        )
        scenarios = (
            (
                True,
                self._occurrence_snapshot(config, identity),
                409,
            ),
            (False, missing_snapshot, 503),
        )
        for force, snapshot, status_code in scenarios:
            background = BackgroundTasks()
            with (
                self.subTest(force=force),
                patch.object(main, "get_engine", return_value=object()),
                patch.object(
                    main,
                    "list_schemes",
                    return_value=[registry],
                ),
                patch(
                    "scheduler.main.discover_schemes",
                    return_value=[config],
                ),
                patch.object(
                    main,
                    "_daily_coordinator_mode",
                    return_value="ledger",
                ),
                patch.object(
                    main,
                    "require_current_daily_coordinator_identity",
                    return_value=identity,
                ),
                patch.object(
                    main,
                    "find_schedule_occurrence_id",
                    return_value=42,
                ),
                patch.object(
                    main,
                    "read_schedule_occurrence_snapshot",
                    return_value=snapshot,
                ),
                patch.object(
                    main,
                    "assert_daily_coordinator_epoch_matches_policy",
                    return_value=identity,
                ),
            ):
                with self.assertRaises(HTTPException) as raised:
                    main.api_trigger_scheme(
                        registry["scheme_id"],
                        main.TriggerRequest(
                            predict_date="2026-07-27",
                            force=force,
                        ),
                        background,
                    )

            self.assertEqual(
                raised.exception.status_code,
                status_code,
            )
            self.assertEqual(background.tasks, [])

    def test_trigger_preflight_maps_infrastructure_failures_to_503(
        self,
    ) -> None:
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

        config = self._canonical(
            "one_y_t5_liq_excess_a_v1",
            "8d583560c9f1",
        )
        registry = self._registry_row(config)
        for failing_call in ("mode", "occurrence"):
            background = BackgroundTasks()
            identity = self._coordinator_identity("ledger")
            with (
                self.subTest(failing_call=failing_call),
                patch.object(
                    main,
                    "get_engine",
                    return_value=object(),
                ),
                patch.object(
                    main,
                    "list_schemes",
                    return_value=[registry],
                ),
                patch(
                    "scheduler.main.discover_schemes",
                    return_value=[config],
                ),
                patch.object(
                    main,
                    "_daily_coordinator_mode",
                    return_value="ledger",
                    side_effect=(
                        RuntimeError("mode unavailable")
                        if failing_call == "mode"
                        else None
                    ),
                ),
                patch.object(
                    main,
                    "require_current_daily_coordinator_identity",
                    return_value=identity,
                ),
                patch.object(
                    main,
                    "find_schedule_occurrence_id",
                    return_value=42,
                    side_effect=(
                        RuntimeError("occurrence unavailable")
                        if failing_call == "occurrence"
                        else None
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
                "scheduler.main.discover_schemes",
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

    def test_trigger_background_revalidates_exact_identity_before_execution(
        self,
    ) -> None:
        config = self._canonical(
            "weekly_10y_lgbm_point_v1",
            "0666a6989d6b",
        )
        drifted = SimpleNamespace(
            **{
                **vars(config),
                "scheme_version": "drifted-version",
            }
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
                "scheduler.main.discover_schemes",
                side_effect=[[config], [drifted]],
            ),
            patch.object(main, "run_prediction_job") as prediction,
            patch.object(
                main,
                "run_daily_operator_recovery_job",
            ) as recovery,
            self.assertLogs(main.logger, level="ERROR") as logs,
        ):
            main.api_trigger_scheme(
                registry["scheme_id"],
                main.TriggerRequest(
                    predict_date="2026-07-27",
                ),
                background,
            )
            task = background.tasks[0]
            task.func(*task.args, **task.kwargs)

        prediction.assert_not_called()
        recovery.assert_not_called()
        self.assertTrue(
            any(
                "admission revalidation failed" in message
                and registry["scheme_id"] in message
                for message in logs.output
            )
        )

    def test_trigger_unknown_scheme_is_404(self) -> None:
        with patch.object(main, "get_engine", return_value=object()), patch.object(
            main, "list_schemes", return_value=[{"scheme_id": "demo_daily__h1__10Y", "base_scheme_id": "demo_daily"}]
        ):
            with self.assertRaises(HTTPException) as ctx:
                main.api_trigger_scheme(
                    "missing", main.TriggerRequest(), BackgroundTasks()
                )
            self.assertEqual(ctx.exception.status_code, 404)

    def test_daily_trigger_rejects_epoch_drift_before_background_enqueue(
        self,
    ) -> None:
        background = BackgroundTasks()
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[
                    {
                        "scheme_id": "demo_daily__h1__10Y",
                        "base_scheme_id": "demo_daily",
                        "frequency": "daily",
                        "status": "active",
                    }
                ],
            ),
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                side_effect=RuntimeError("epoch changed"),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    "demo_daily__h1__10Y",
                    main.TriggerRequest(predict_date="2026-07-24"),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(background.tasks, [])

    def test_daily_trigger_rejects_old_occurrence_under_new_epoch(
        self,
    ) -> None:
        background = BackgroundTasks()
        current = SimpleNamespace(
            mode="ledger",
            policy_payload=lambda: {
                "epoch": 2,
                "mode": "ledger",
                "record_sha256": "2" * 64,
            },
        )
        frozen = SimpleNamespace(
            mode="ledger",
            policy_payload=lambda: {
                "epoch": 1,
                "mode": "ledger",
                "record_sha256": "1" * 64,
            },
        )
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                policy_json={
                    "daily_coordinator_epoch":
                        frozen.policy_payload(),
                }
            )
        )
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[
                    {
                        "scheme_id": "demo_daily__h1__10Y",
                        "base_scheme_id": "demo_daily",
                        "frequency": "daily",
                        "status": "active",
                    }
                ],
            ),
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                return_value=current,
            ),
            patch.object(
                main,
                "find_schedule_occurrence_id",
                return_value=42,
            ),
            patch.object(
                main,
                "read_schedule_occurrence_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                main,
                "assert_daily_coordinator_epoch_matches_policy",
                return_value=frozen,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    "demo_daily__h1__10Y",
                    main.TriggerRequest(predict_date="2026-07-24"),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(background.tasks, [])

    def test_daily_trigger_rejects_missing_occurrence_after_not_before(
        self,
    ) -> None:
        background = BackgroundTasks()
        current = SimpleNamespace(
            mode="ledger",
            policy_payload=lambda: {
                "epoch": 2,
                "mode": "ledger",
                "record_sha256": "2" * 64,
            },
        )
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[
                    {
                        "scheme_id": "demo_daily__h1__10Y",
                        "base_scheme_id": "demo_daily",
                        "frequency": "daily",
                        "status": "active",
                    }
                ],
            ),
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                return_value=current,
            ),
            patch.object(
                main,
                "find_schedule_occurrence_id",
                return_value=None,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    "demo_daily__h1__10Y",
                    main.TriggerRequest(predict_date="2020-01-01"),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(background.tasks, [])

    def test_daily_trigger_rejects_missing_occurrence_before_not_before(
        self,
    ) -> None:
        background = BackgroundTasks()
        current = SimpleNamespace(
            mode="ledger",
            policy_payload=lambda: {
                "epoch": 2,
                "mode": "ledger",
                "record_sha256": "2" * 64,
            },
        )

        class BeforeNotBefore(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 7, 24, 6, 0)
                return value.replace(tzinfo=tz) if tz is not None else value

        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[
                    {
                        "scheme_id": "demo_daily__h1__10Y",
                        "base_scheme_id": "demo_daily",
                        "frequency": "daily",
                        "status": "active",
                    }
                ],
            ),
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                return_value=current,
            ),
            patch.object(
                main,
                "find_schedule_occurrence_id",
                return_value=None,
            ),
            patch.object(main, "datetime", BeforeNotBefore),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.api_trigger_scheme(
                    "demo_daily__h1__10Y",
                    main.TriggerRequest(predict_date="2026-07-24"),
                    background,
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(background.tasks, [])

    def test_trigger_known_scheme_is_accepted(self) -> None:
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
                "scheduler.main.discover_schemes",
                return_value=[config],
            ),
        ):
            result = main.api_trigger_scheme(
                registry["scheme_id"],
                main.TriggerRequest(predict_date="2026-06-09"),
                background,
            )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["scheme_id"], registry["scheme_id"])
        self.assertEqual(
            result["base_scheme_id"],
            config.scheme_id,
        )
        # 已排入后台任务（_run_trigger）。
        self.assertEqual(len(background.tasks), 1)
        task = background.tasks[0]
        self.assertEqual(
            task.args,
            (
                registry["scheme_id"],
                "2026-06-09",
                False,
                None,
            ),
        )

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
                "scheduler.main.discover_schemes",
                return_value=[config],
            ),
            patch.object(main, "run_prediction_job") as job_mock,
        ):
            main._run_trigger(
                registry["scheme_id"],
                "2026-07-27",
                True,
                None,
            )
        job_mock.assert_called_once()
        self.assertEqual(job_mock.call_args.kwargs.get("force"), True)

    def test_run_trigger_routes_daily_recovery_through_ledger_coordinator(
        self,
    ) -> None:
        config = self._canonical(
            "ten_y_t5_maj3_k3_ic_static_v1",
            "c54b90bcafa7",
        )
        registry = self._registry_row(config)
        identity = self._coordinator_identity("ledger")
        snapshot = self._occurrence_snapshot(
            config,
            identity,
        )
        with (
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "list_schemes",
                return_value=[registry],
            ),
            patch(
                "scheduler.main.discover_schemes",
                return_value=[config],
            ),
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                return_value=identity,
            ),
            patch.object(
                main,
                "find_schedule_occurrence_id",
                return_value=42,
            ),
            patch.object(
                main,
                "read_schedule_occurrence_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                main,
                "assert_daily_coordinator_epoch_matches_policy",
                return_value=identity,
            ),
            patch.object(
                main,
                "run_daily_operator_recovery_job",
            ) as recovery,
            patch.object(main, "run_prediction_job") as legacy,
        ):
            main._run_trigger(
                registry["scheme_id"],
                "2026-07-24",
                False,
                None,
            )

        recovery.assert_called_once_with(
            config.scheme_id,
            run_date="2026-07-24",
            algo_env=main.DEFAULT_ALGO_ENV,
        )
        legacy.assert_not_called()


class AdminRegistrySyncEndpointTests(unittest.TestCase):
    def test_admin_sync_runs_sync_with_force(self) -> None:
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="legacy",
            ),
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

    def test_admin_sync_is_disabled_while_ledger_mode_is_active(
        self,
    ) -> None:
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "sync_registry_from_configs",
            ) as sync_mock,
            self.assertRaises(main.HTTPException) as raised,
        ):
            main.api_admin_registry_sync()

        self.assertEqual(raised.exception.status_code, 409)
        sync_mock.assert_not_called()


class StartupSyncTests(unittest.TestCase):
    def test_startup_runs_registry_sync_once(self) -> None:
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="legacy",
            ),
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "sync_registry_from_configs",
            ) as sync_mock,
        ):
            main._sync_registry_on_startup()
        sync_mock.assert_called_once()

    def test_ledger_startup_does_not_mutate_registry_before_admission(
        self,
    ) -> None:
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(main, "get_engine", return_value=object()),
            patch.object(
                main,
                "sync_registry_from_configs",
            ) as sync_mock,
        ):
            main._sync_registry_on_startup()

        sync_mock.assert_not_called()

    def test_startup_swallows_sync_errors(self) -> None:
        with (
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="legacy",
            ),
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
