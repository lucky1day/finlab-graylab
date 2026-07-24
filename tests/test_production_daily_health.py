"""生产日频健康检查测试。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, text

from scripts.check_production_daily_health import (
    ActualWatermark,
    DataBridgeHealthSnapshot,
    DailyHealthSnapshot,
    PredictionDateCheck,
    RunPredictionCount,
    V2SchedulerGateSnapshot,
    evaluate_daily_health,
    evaluate_data_bridge_health,
    evaluate_ledger_daily_health,
    evaluate_v2_scheduler_gate,
    load_snapshot,
    status_from_findings,
)


class ProductionDailyHealthTests(unittest.TestCase):
    def test_main_ledger_mode_uses_occurrence_projection_not_legacy_v2_gate(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        engine = SimpleNamespace(dispose=lambda: None)
        projection = {
            "overall": "ok",
            "reasons": [],
            "checked_at": "2026-07-24T00:00:00Z",
            "occurrence": {
                "predict_date": "2026-07-24",
                "sla_deadline_at": "2026-07-24T00:00:00Z",
                "sla_outcome": "MET",
            },
            "items": {
                "expected": 21,
                "actual": 21,
                "completed": 21,
                "late": 0,
                "failed": 0,
            },
            "targets": {
                "expected": 25,
                "actual": 25,
                "accepted": 25,
                "late": 0,
                "missing": 0,
                "missing_registry_ids": [],
            },
        }
        stdout = io.StringIO()
        with (
            patch.object(
                health_script,
                "_resolve_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                health_script,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                health_script,
                "load_ledger_daily_health",
                return_value=projection,
            ),
            patch.object(
                health_script,
                "load_snapshot",
                side_effect=AssertionError("legacy snapshot must not load"),
            ),
            patch.object(
                health_script,
                "load_v2_scheduler_gate",
                side_effect=AssertionError("legacy V2 gate must not load"),
            ),
            patch(
                "sys.argv",
                [
                    "check_production_daily_health.py",
                    "--predict-date",
                    "2026-07-24",
                ],
            ),
            redirect_stdout(stdout),
        ):
            exit_code = health_script.main()

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "ledger")
        self.assertEqual(payload["daily_schedule"]["targets"]["accepted"], 25)

    def test_ledger_health_treats_24_of_25_targets_as_hard_error_at_sla(
        self,
    ) -> None:
        projection = {
            "overall": "error",
            "reasons": ["SLA_BREACHED", "SLA_DEADLINE_INCOMPLETE"],
            "checked_at": "2026-07-24T00:00:00Z",
            "occurrence": {
                "predict_date": "2026-07-24",
                "sla_deadline_at": "2026-07-24T00:00:00Z",
                "sla_outcome": "BREACHED",
            },
            "items": {
                "expected": 21,
                "actual": 21,
                "completed": 20,
                "late": 0,
                "failed": 0,
            },
            "targets": {
                "expected": 25,
                "actual": 25,
                "accepted": 24,
                "late": 0,
                "missing": 1,
                "missing_registry_ids": [
                    "blackbox_demo__h1__10Y",
                ],
            },
        }

        findings = evaluate_ledger_daily_health(projection)

        self.assertEqual(status_from_findings(findings), "error")
        incomplete = [
            finding
            for finding in findings
            if finding.code == "daily_targets_incomplete"
        ]
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(
            incomplete[0].detail["missing_registry_ids"],
            ["blackbox_demo__h1__10Y"],
        )

    def test_ledger_health_reports_committed_target_with_missing_receipt_as_missing(
        self,
    ) -> None:
        projection = {
            "overall": "error",
            "reasons": ["VISIBILITY_RECEIPT_MISSING"],
            "checked_at": "2026-07-24T00:00:00Z",
            "occurrence": {
                "predict_date": "2026-07-24",
                "sla_deadline_at": "2026-07-24T00:00:00Z",
                "sla_outcome": "BREACHED",
            },
            "items": {
                "expected": 21,
                "actual": 21,
                "completed": 21,
                "late": 0,
                "failed": 0,
            },
            "targets": {
                "expected": 25,
                "actual": 25,
                "accepted": 24,
                "committed": 25,
                "linked": 25,
                "late": 0,
                "missing": 1,
                "missing_registry_ids": ["v2__h5__1Y"],
                "receipt_missing_registry_ids": ["v2__h5__1Y"],
                "availability_basis": "db_commit_visibility_receipt",
            },
            "api_visibility": {
                "asserted": False,
                "proof_required": "uncached_external_api_probe",
                "probe_endpoint": "/api/daily-schedule/visibility",
                "canonical_endpoint": "/api/predictions",
                "cache_policy": "no-store",
            },
        }

        findings = evaluate_ledger_daily_health(projection)

        incomplete = next(
            item
            for item in findings
            if item.code == "daily_targets_incomplete"
        )
        self.assertEqual(incomplete.detail["accepted"], 24)
        self.assertEqual(incomplete.detail["committed"], 25)
        self.assertEqual(
            incomplete.detail["receipt_missing_registry_ids"],
            ["v2__h5__1Y"],
        )

    def test_ledger_health_accepts_exact_complete_dynamic_cardinality(
        self,
    ) -> None:
        projection = {
            "overall": "ok",
            "reasons": [],
            "checked_at": "2026-07-24T00:00:00Z",
            "occurrence": {
                "predict_date": "2026-07-24",
                "sla_deadline_at": "2026-07-24T00:00:00Z",
                "sla_outcome": "MET",
            },
            "items": {
                "expected": 21,
                "actual": 21,
                "completed": 21,
                "late": 0,
                "failed": 0,
            },
            "targets": {
                "expected": 25,
                "actual": 25,
                "accepted": 25,
                "late": 0,
                "missing": 0,
                "missing_registry_ids": [],
            },
        }

        self.assertEqual(evaluate_ledger_daily_health(projection), [])

    def test_ledger_health_rejects_pending_sla_after_deadline_at_25_of_25(
        self,
    ) -> None:
        projection = {
            "overall": "ok",
            "reasons": [],
            "checked_at": "2026-07-24T08:04:00+08:00",
            "occurrence": {
                "predict_date": "2026-07-24",
                "sla_deadline_at": "2026-07-24T00:00:00Z",
                "sla_outcome": "PENDING",
            },
            "items": {
                "expected": 21,
                "actual": 21,
                "completed": 21,
                "late": 21,
                "failed": 0,
            },
            "targets": {
                "expected": 25,
                "actual": 25,
                "accepted": 25,
                "late": 25,
                "missing": 0,
                "missing_registry_ids": [],
            },
        }

        findings = evaluate_ledger_daily_health(projection)

        self.assertEqual(status_from_findings(findings), "error")
        self.assertEqual(
            [finding.code for finding in findings],
            ["sla_outcome_pending_after_deadline"],
        )

    def test_main_ledger_mode_exits_error_for_pending_sla_after_deadline(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        engine = SimpleNamespace(dispose=lambda: None)
        projection = {
            "overall": "ok",
            "reasons": [],
            "checked_at": "2026-07-24T08:04:00+08:00",
            "occurrence": {
                "predict_date": "2026-07-24",
                "sla_deadline_at": "2026-07-24T00:00:00Z",
                "sla_outcome": "PENDING",
            },
            "items": {
                "expected": 21,
                "actual": 21,
                "completed": 21,
                "late": 21,
                "failed": 0,
            },
            "targets": {
                "expected": 25,
                "actual": 25,
                "accepted": 25,
                "late": 25,
                "missing": 0,
                "missing_registry_ids": [],
            },
        }
        stdout = io.StringIO()
        with (
            patch.object(
                health_script,
                "_resolve_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                health_script,
                "create_engine_from_env",
                return_value=engine,
            ),
            patch.object(
                health_script,
                "load_ledger_daily_health",
                return_value=projection,
            ),
            patch(
                "sys.argv",
                [
                    "check_production_daily_health.py",
                    "--predict-date",
                    "2026-07-24",
                ],
            ),
            redirect_stdout(stdout),
        ):
            exit_code = health_script.main()

        self.assertEqual(exit_code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(
            [finding["code"] for finding in payload["findings"]],
            ["sla_outcome_pending_after_deadline"],
        )

    def test_auto_mode_uses_ledger_heartbeat_not_shell_default(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        engine = object()
        inspector = SimpleNamespace(has_table=lambda name: True)
        heartbeat = SimpleNamespace(
            details={"coordinator_mode": "ledger"},
        )
        with (
            patch.dict(
                health_script.os.environ,
                {},
                clear=True,
            ),
            patch.object(
                health_script,
                "inspect",
                return_value=inspector,
                create=True,
            ),
            patch.object(
                health_script,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                health_script,
                "read_deployment_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                health_script,
                "require_current_daily_coordinator_identity",
                return_value=SimpleNamespace(mode="ledger"),
            ),
        ):
            mode = health_script._resolve_coordinator_mode(
                engine,
                requested_mode="auto",
            )

        self.assertEqual(mode, "ledger")

    def test_explicit_mode_cannot_override_current_epoch_identity(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        identity = SimpleNamespace(mode="ledger")
        with (
            patch.object(
                health_script,
                "require_current_daily_coordinator_identity",
                return_value=identity,
                create=True,
            ),
            patch.object(
                health_script,
                "read_deployment_daily_coordinator_mode",
                side_effect=AssertionError(
                    "health must not read rollout after epoch resolution"
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "assertion"),
        ):
            health_script._resolve_coordinator_mode(
                object(),
                requested_mode="legacy",
            )

    def test_auto_mode_fails_closed_when_migrated_state_is_ambiguous(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        engine = object()
        inspector = SimpleNamespace(has_table=lambda name: True)
        with (
            patch.dict(
                health_script.os.environ,
                {},
                clear=True,
            ),
            patch.object(
                health_script,
                "inspect",
                return_value=inspector,
                create=True,
            ),
            patch.object(
                health_script,
                "require_current_daily_coordinator_identity",
                side_effect=RuntimeError(
                    "current coordinator epoch is ambiguous"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "ambiguous",
            ):
                health_script._resolve_coordinator_mode(
                    engine,
                    requested_mode="auto",
                )

    def test_auto_mode_keeps_legacy_when_schema_exists_without_heartbeat(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        engine = object()
        inspector = SimpleNamespace(has_table=lambda name: True)
        with (
            patch.dict(
                health_script.os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
                clear=True,
            ),
            patch.object(
                health_script,
                "inspect",
                return_value=inspector,
                create=True,
            ),
            patch.object(
                health_script,
                "read_scheduler_heartbeat",
                return_value=None,
            ),
            patch.object(
                health_script,
                "require_current_daily_coordinator_identity",
                return_value=SimpleNamespace(mode="legacy"),
            ),
        ):
            mode = health_script._resolve_coordinator_mode(
                engine,
                requested_mode="auto",
            )

        self.assertEqual(mode, "legacy")

    def test_explicit_mode_rejects_assertion_that_conflicts_with_epoch(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        engine = object()
        inspector = SimpleNamespace(has_table=lambda name: True)
        heartbeat = SimpleNamespace(
            details={"coordinator_mode": "ledger"},
        )
        with (
            patch.dict(
                health_script.os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "legacy"},
                clear=True,
            ),
            patch.object(
                health_script,
                "inspect",
                return_value=inspector,
                create=True,
            ),
            patch.object(
                health_script,
                "require_current_daily_coordinator_identity",
                return_value=SimpleNamespace(mode="ledger"),
            ),
            self.assertRaisesRegex(RuntimeError, "assertion"),
        ):
            health_script._resolve_coordinator_mode(
                engine,
                requested_mode="legacy",
            )

    def test_auto_mode_rejects_ledger_env_before_schema_exists(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        engine = object()
        inspector = SimpleNamespace(has_table=lambda name: False)
        with (
            patch.dict(
                health_script.os.environ,
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=True,
            ),
            patch.object(
                health_script,
                "inspect",
                return_value=inspector,
                create=True,
            ),
            self.assertRaisesRegex(RuntimeError, "chain"),
        ):
            health_script._resolve_coordinator_mode(
                engine,
                requested_mode="auto",
            )

    def test_ledger_projection_error_cannot_be_downgraded_to_pending_warning(
        self,
    ) -> None:
        projection = {
            "overall": "error",
            "reasons": ["DATABRIDGE_READY_V2_INCOMPLETE"],
            "checked_at": "2026-07-23T23:00:00Z",
            "occurrence": {
                "predict_date": "2026-07-24",
                "sla_deadline_at": "2026-07-24T00:00:00Z",
                "sla_outcome": "PENDING",
            },
            "items": {
                "expected": 21,
                "actual": 21,
                "completed": 20,
                "late": 0,
                "failed": 0,
            },
            "targets": {
                "expected": 25,
                "actual": 25,
                "accepted": 24,
                "late": 0,
                "missing": 1,
                "missing_registry_ids": ["v2__h5__1Y"],
            },
        }

        findings = evaluate_ledger_daily_health(projection)

        self.assertEqual(status_from_findings(findings), "error")
        self.assertIn(
            "daily_ledger_unhealthy",
            {finding.code for finding in findings},
        )

    def test_ledger_idle_heartbeat_is_healthy_on_non_trading_day(
        self,
    ) -> None:
        from scripts import check_production_daily_health as health_script

        epoch = {
            "epoch": 7,
            "mode": "ledger",
            "record_sha256": "e" * 64,
        }
        heartbeat = SimpleNamespace(
            occurrence_id=None,
            details={"daily_coordinator_epoch": epoch},
        )
        with (
            patch.object(
                health_script,
                "require_current_daily_coordinator_identity",
                return_value=SimpleNamespace(
                    mode="ledger",
                    policy_payload=lambda: dict(epoch),
                ),
            ),
            patch.object(
                health_script,
                "assert_daily_coordinator_epoch_payload_matches_current",
                return_value=SimpleNamespace(mode="ledger"),
            ),
            patch.object(
                health_script,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                health_script,
                "is_trading_day",
                return_value=False,
            ),
            patch.object(
                health_script,
                "read_schedule_occurrence_snapshot",
            ) as read_snapshot,
        ):
            projection = health_script.load_ledger_daily_health(
                object(),
                predict_date="2026-07-25",
                now=datetime(
                    2026,
                    7,
                    25,
                    1,
                    0,
                    tzinfo=timezone.utc,
                ),
            )

        self.assertEqual(projection["overall"], "ok")
        self.assertEqual(projection["reasons"], ["NON_TRADING_DAY"])
        self.assertEqual(evaluate_ledger_daily_health(projection), [])
        read_snapshot.assert_not_called()

    def test_ledger_idle_heartbeat_is_healthy_before_0630(self) -> None:
        from scripts import check_production_daily_health as health_script

        epoch = {
            "epoch": 7,
            "mode": "ledger",
            "record_sha256": "e" * 64,
        }
        heartbeat = SimpleNamespace(
            occurrence_id=None,
            details={"daily_coordinator_epoch": epoch},
        )
        with (
            patch.object(
                health_script,
                "require_current_daily_coordinator_identity",
                return_value=SimpleNamespace(
                    mode="ledger",
                    policy_payload=lambda: dict(epoch),
                ),
            ),
            patch.object(
                health_script,
                "assert_daily_coordinator_epoch_payload_matches_current",
                return_value=SimpleNamespace(mode="ledger"),
            ),
            patch.object(
                health_script,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                health_script,
                "is_trading_day",
                return_value=True,
            ),
        ):
            projection = health_script.load_ledger_daily_health(
                object(),
                predict_date="2026-07-24",
                now=datetime(
                    2026,
                    7,
                    23,
                    22,
                    29,
                    59,
                    tzinfo=timezone.utc,
                ),
            )

        self.assertEqual(projection["overall"], "ok")
        self.assertEqual(projection["reasons"], ["BEFORE_NOT_BEFORE"])
        self.assertEqual(evaluate_ledger_daily_health(projection), [])

    def test_ledger_missing_occurrence_is_error_at_0630(self) -> None:
        from scripts import check_production_daily_health as health_script

        epoch = {
            "epoch": 7,
            "mode": "ledger",
            "record_sha256": "e" * 64,
        }
        heartbeat = SimpleNamespace(
            occurrence_id=None,
            details={"daily_coordinator_epoch": epoch},
        )
        with (
            patch.object(
                health_script,
                "require_current_daily_coordinator_identity",
                return_value=SimpleNamespace(
                    mode="ledger",
                    policy_payload=lambda: dict(epoch),
                ),
            ),
            patch.object(
                health_script,
                "assert_daily_coordinator_epoch_payload_matches_current",
                return_value=SimpleNamespace(mode="ledger"),
            ),
            patch.object(
                health_script,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                health_script,
                "is_trading_day",
                return_value=True,
            ),
        ):
            projection = health_script.load_ledger_daily_health(
                object(),
                predict_date="2026-07-24",
                now=datetime(
                    2026,
                    7,
                    23,
                    22,
                    30,
                    tzinfo=timezone.utc,
                ),
            )

        self.assertEqual(projection["overall"], "error")
        self.assertEqual(projection["reasons"], ["OCCURRENCE_MISSING"])

    def test_stale_data_bridge_refresh_is_error_after_deadline(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        snapshot = DataBridgeHealthSnapshot(
            required_refresh_date="2026-07-19",
            current_refresh_date="2026-07-18",
            generation_id="full-old",
            refreshed_at="2026-07-18T05:45:00+08:00",
            business_digest="abc",
            files={},
            last_attempt={"status": "failed", "error": "source not ready"},
            validation_error=None,
        )

        findings = evaluate_data_bridge_health(
            snapshot,
            now=datetime(2026, 7, 19, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            deadline="06:45",
        )

        self.assertEqual(status_from_findings(findings), "error")
        self.assertEqual(findings[0].code, "data_bridge_refresh_stale")
        self.assertEqual(findings[0].detail["last_attempt"]["error"], "source not ready")

    def test_blocked_v2_gate_is_reported_without_changing_v1_health(self) -> None:
        snapshot = V2SchedulerGateSnapshot(
            run_date="2026-07-22",
            status="blocked",
            generation_id="full-old",
            current_generation_id="full-current",
            restart_verified=False,
            error="current dataset is stale",
        )

        findings = evaluate_v2_scheduler_gate(snapshot)

        self.assertEqual([item.code for item in findings], ["v2_daily_gate_blocked"])
        self.assertEqual(status_from_findings(findings), "error")
        self.assertEqual(
            status_from_findings(evaluate_daily_health(self._snapshot())),
            "ok",
        )

    def test_ready_v2_gate_requires_matching_generation(self) -> None:
        snapshot = V2SchedulerGateSnapshot(
            run_date="2026-07-22",
            status="ready",
            generation_id="full-old",
            current_generation_id="full-current",
            restart_verified=True,
            error=None,
        )

        findings = evaluate_v2_scheduler_gate(snapshot)

        self.assertEqual(
            [item.code for item in findings],
            ["v2_daily_gate_generation_mismatch"],
        )

    def test_ready_v2_gate_requires_verified_restart(self) -> None:
        snapshot = V2SchedulerGateSnapshot(
            run_date="2026-07-22",
            status="ready",
            generation_id="full-current",
            current_generation_id="full-current",
            restart_verified=False,
            error=None,
        )

        findings = evaluate_v2_scheduler_gate(snapshot)

        self.assertEqual(
            [item.code for item in findings],
            ["v2_scheduler_restart_unverified"],
        )

    def test_verified_v2_gate_is_healthy(self) -> None:
        snapshot = V2SchedulerGateSnapshot(
            run_date="2026-07-22",
            status="ready",
            generation_id="full-current",
            current_generation_id="full-current",
            restart_verified=True,
            error=None,
        )

        self.assertEqual(evaluate_v2_scheduler_gate(snapshot), [])

    def _snapshot(self, **overrides: object) -> DailyHealthSnapshot:
        data = {
            "predict_date": "2026-07-06",
            "expected_feature_date": "2026-07-03",
            "is_trading_day": True,
            "active_daily_base_schemes": ("a", "b"),
            "successful_daily_run_schemes": ("a", "b"),
            "predictions_count": 2,
            "run_prediction_counts": (
                RunPredictionCount(1, "a", 1, 1),
                RunPredictionCount(2, "b", 1, 1),
            ),
            "prediction_date_checks": (
                PredictionDateCheck(
                    prediction_id=1,
                    scheme_id="a",
                    target_tenor="5Y",
                    horizon=1,
                    predict_date="2026-07-06",
                    feature_date="2026-07-03",
                    target_date="2026-07-06",
                    expected_feature_date="2026-07-03",
                    expected_target_date="2026-07-06",
                ),
            ),
            "actual_watermarks": (
                ActualWatermark("5Y", "2026-07-03", "2026-07-03"),
                ActualWatermark("10Y", "2026-07-03", "2026-07-03"),
            ),
        }
        data.update(overrides)
        return DailyHealthSnapshot(**data)

    def test_trading_day_with_no_predictions_is_error(self) -> None:
        findings = evaluate_daily_health(
            self._snapshot(successful_daily_run_schemes=(), predictions_count=0)
        )

        self.assertEqual(status_from_findings(findings), "error")
        self.assertTrue(any(item.code == "daily_predictions_missing" for item in findings))

    def test_no_predictions_is_warning_when_all_active_schemes_are_source_blocked(self) -> None:
        findings = evaluate_daily_health(
            self._snapshot(
                predict_date="2026-07-07",
                expected_feature_date="2026-07-06",
                active_daily_base_schemes=("t1_daily", "t5_daily"),
                successful_daily_run_schemes=(),
                predictions_count=0,
                run_prediction_counts=(),
                prediction_date_checks=(),
                actual_watermarks=(
                    ActualWatermark("3Y", "2026-07-01", "2026-07-01"),
                    ActualWatermark("5Y", "2026-07-01", "2026-07-01"),
                    ActualWatermark("7Y", "2026-07-01", "2026-07-01"),
                    ActualWatermark("10Y", "2026-07-03", "2026-07-03"),
                ),
            )
        )

        self.assertEqual(status_from_findings(findings), "warning")
        missing = [item for item in findings if item.code == "daily_predictions_missing"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].level, "warning")

    def test_missing_active_daily_runs_are_warning_by_default(self) -> None:
        findings = evaluate_daily_health(self._snapshot(successful_daily_run_schemes=("a",)))

        self.assertEqual(status_from_findings(findings), "warning")
        missing = [item for item in findings if item.code == "active_daily_runs_missing"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].detail["missing_base_schemes"], ["b"])

    def test_missing_active_daily_runs_can_be_strict_error(self) -> None:
        findings = evaluate_daily_health(
            self._snapshot(successful_daily_run_schemes=("a",)),
            strict_runs=True,
        )

        self.assertEqual(status_from_findings(findings), "error")
        self.assertTrue(any(item.code == "active_daily_runs_missing" for item in findings))

    def test_actual_tail_later_than_source_is_error(self) -> None:
        findings = evaluate_daily_health(
            self._snapshot(
                actual_watermarks=(ActualWatermark("5Y", "2026-07-03", "2026-07-01"),)
            )
        )

        self.assertEqual(status_from_findings(findings), "error")
        self.assertTrue(any(item.code == "actual_tail_after_source" for item in findings))

    def test_non_trading_day_does_not_require_daily_predictions(self) -> None:
        findings = evaluate_daily_health(
            self._snapshot(
                is_trading_day=False,
                successful_daily_run_schemes=(),
                predictions_count=0,
            )
        )

        self.assertEqual(status_from_findings(findings), "ok")

    def test_missing_run_reports_input_watermark_blockers_for_known_scheme_family(self) -> None:
        findings = evaluate_daily_health(
            self._snapshot(
                active_daily_base_schemes=("daily_1y_xgb_1y13_0629",),
                successful_daily_run_schemes=(),
                predictions_count=1,
                actual_watermarks=(
                    ActualWatermark("1Y", "2026-07-01", "2026-07-01"),
                    ActualWatermark("5Y", "2026-07-01", "2026-07-01"),
                    ActualWatermark("10Y", "2026-07-03", "2026-07-03"),
                ),
            )
        )

        blockers = [item for item in findings if item.code == "daily_run_input_watermark_blocked"]
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].level, "warning")
        self.assertEqual(blockers[0].detail["expected_feature_date"], "2026-07-03")
        self.assertEqual(
            blockers[0].detail["blocked_schemes"],
            [
                {
                    "scheme_id": "daily_1y_xgb_1y13_0629",
                    "required_tenors": ["1Y", "5Y", "10Y"],
                    "blocked_tenors": [
                        {"tenor": "1Y", "source_max": "2026-07-01"},
                        {"tenor": "5Y", "source_max": "2026-07-01"},
                    ],
                }
            ],
        )

    def test_successful_run_with_missing_prediction_rows_is_error(self) -> None:
        findings = evaluate_daily_health(
            self._snapshot(
                run_prediction_counts=(
                    RunPredictionCount(568, "t5_daily", 4, 0),
                )
            )
        )

        self.assertEqual(status_from_findings(findings), "error")
        mismatches = [item for item in findings if item.code == "successful_run_prediction_rows_mismatch"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(
            mismatches[0].detail["mismatched_runs"],
            [
                {
                    "run_id": 568,
                    "scheme_id": "t5_daily",
                    "records_written": 4,
                    "prediction_rows": 0,
                }
            ],
        )

    def test_daily_prediction_date_semantics_mismatch_is_error(self) -> None:
        findings = evaluate_daily_health(
            self._snapshot(
                predict_date="2026-07-07",
                expected_feature_date="2026-07-06",
                prediction_date_checks=(
                    PredictionDateCheck(
                        prediction_id=686,
                        scheme_id="t5_daily",
                        target_tenor="10Y",
                        horizon=5,
                        predict_date="2026-07-07",
                        feature_date="2026-07-03",
                        target_date="2026-07-10",
                        expected_feature_date="2026-07-06",
                        expected_target_date="2026-07-13",
                    ),
                ),
            )
        )

        self.assertEqual(status_from_findings(findings), "error")
        mismatches = [item for item in findings if item.code == "daily_prediction_date_semantics_mismatch"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(
            mismatches[0].detail["invalid_predictions"],
            [
                {
                    "prediction_id": 686,
                    "scheme_id": "t5_daily",
                    "target_tenor": "10Y",
                    "horizon": 5,
                    "predict_date": "2026-07-07",
                    "feature_date": "2026-07-03",
                    "target_date": "2026-07-10",
                    "expected_feature_date": "2026-07-06",
                    "expected_target_date": "2026-07-13",
                }
            ],
        )

    def test_legacy_snapshot_does_not_exclude_active_blackbox_v2(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        with engine.begin() as conn:
            for stmt in (
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT,
                    base_scheme_id TEXT,
                    target_tenor TEXT,
                    horizon INTEGER,
                    status TEXT,
                    frequency TEXT,
                    task_type TEXT,
                    runtime_type TEXT
                )
                """,
                """
                CREATE TABLE t_scheme_runs (
                    run_id INTEGER,
                    scheme_id TEXT,
                    predict_date TEXT,
                    status TEXT,
                    records_written INTEGER
                )
                """,
                """
                CREATE TABLE t_scheme_predictions (
                    id INTEGER,
                    run_id INTEGER,
                    scheme_id TEXT,
                    target_tenor TEXT,
                    horizon INTEGER,
                    predict_date TEXT,
                    feature_date TEXT,
                    target_date TEXT
                )
                """,
                """
                CREATE TABLE t_scheme_actuals (
                    tenor TEXT,
                    trade_date TEXT
                )
                """,
                """
                CREATE TABLE api_wind_daily (
                    indicators_code TEXT,
                    rdate TEXT,
                    indicators_value REAL
                )
                """,
                """
                CREATE TABLE t_trade_calendar (
                    rdate TEXT,
                    trade_flag TEXT
                )
                """,
            ):
                conn.execute(text(stmt))
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_registry
                        (scheme_id, base_scheme_id, target_tenor, horizon, status, frequency, task_type, runtime_type)
                    VALUES
                        ('t1_daily__h1__5Y', 't1_daily', '5Y', 1, 'active', 'daily', 'T+1', 'native_adapter'),
                        ('blackbox_demo__h5__1Y', 'blackbox_demo', '1Y', 5, 'active', 'daily', 'T+5', 'blackbox_v2')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (id, run_id, scheme_id, target_tenor, horizon, predict_date, feature_date, target_date)
                    VALUES
                        (1, 900, 'monthly_demo', '10Y', 30, '2026-07-07', '2026-07-07', '2026-08-07'),
                        (2, 901, 'blackbox_demo', '1Y', 5, '2026-07-07', '2026-07-06', '2026-07-13')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_trade_calendar (rdate, trade_flag)
                    VALUES ('2026-07-06', '1'), ('2026-07-07', '1')
                    """
                )
            )

        snapshot = load_snapshot(engine, predict_date="2026-07-07", tenors=("10Y",))

        self.assertEqual(snapshot.predictions_count, 1)
        self.assertEqual(len(snapshot.prediction_date_checks), 1)
        self.assertEqual(
            snapshot.prediction_date_checks[0].scheme_id,
            "blackbox_demo",
        )
        self.assertEqual(
            snapshot.active_daily_base_schemes,
            ("blackbox_demo", "t1_daily"),
        )


if __name__ == "__main__":
    unittest.main()
