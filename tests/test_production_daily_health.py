"""生产日频健康检查测试。"""

from __future__ import annotations

import unittest

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
    evaluate_v2_scheduler_gate,
    load_snapshot,
    status_from_findings,
)


class ProductionDailyHealthTests(unittest.TestCase):
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

    def test_load_snapshot_counts_only_active_daily_predictions(self) -> None:
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
                    task_type TEXT
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
                        (scheme_id, base_scheme_id, target_tenor, horizon, status, frequency, task_type)
                    VALUES
                        ('t1_daily__h1__5Y', 't1_daily', '5Y', 1, 'active', 'daily', 'T+1')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_predictions
                        (id, run_id, scheme_id, target_tenor, horizon, predict_date, feature_date, target_date)
                    VALUES
                        (1, 900, 'monthly_demo', '10Y', 30, '2026-07-07', '2026-07-07', '2026-08-07')
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

        self.assertEqual(snapshot.predictions_count, 0)
        self.assertEqual(snapshot.prediction_date_checks, ())


if __name__ == "__main__":
    unittest.main()
