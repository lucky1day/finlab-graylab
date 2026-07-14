from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from backtests.weekly_base_runner import (
    WeeklyBacktestSpec,
    build_weekly_backtest_rows,
    compact_weekly_benchmark_rows,
    index_weekly_core_output_rows,
)


class WeeklyBaseRunnerTests(unittest.TestCase):
    """周频回测公共行构造器的无信号政策测试。"""

    def test_spec_defaults_to_skip_and_rejects_invalid_flat_config(self) -> None:
        spec = _spec()

        self.assertEqual(spec.no_signal_policy, "skip")
        self.assertIsNone(spec.no_signal_source_component)
        with self.assertRaisesRegex(ValueError, "no_signal_policy"):
            _spec(no_signal_policy="unknown")
        with self.assertRaisesRegex(ValueError, "no_signal_source_component"):
            _spec(no_signal_policy="flat")

    def test_none_prediction_is_skipped_by_default(self) -> None:
        rows = _build_rows(_weekly_frame(), spec=_spec())

        self.assertEqual(rows, [])

    def test_flat_policy_builds_complete_auditable_row(self) -> None:
        rows = _build_rows(
            _weekly_frame(),
            spec=_spec(
                no_signal_policy="flat",
                no_signal_source_component="rule_vote",
            ),
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["predicted_direction"], 0)
        self.assertEqual(row["model_pred"], 0)
        self.assertEqual(row["confidence"], 0.0)
        self.assertEqual(row["predict_date"], "2026-01-02")
        self.assertEqual(row["feature_date"], "2026-01-02")
        self.assertEqual(row["target_date"], "2026-01-09")
        self.assertEqual(row["label"], 1)
        self.assertEqual(row["source_row"]["week_id"], 202601)
        self.assertEqual(row["source_row"]["TB5YWI3C"], 2.0)
        self.assertAlmostEqual(row["extra"]["future_return"], 0.05)
        self.assertEqual(
            {key: value for key, value in row["extra"].items() if key != "future_return"},
            {
                "frequency": "weekly",
                "model_version": "unit.v1",
                "target_rule": "unit_next_week",
                "feature_week_id": 202601,
                "target_week_id": 202602,
                "feature_date": "2026-01-02",
                "target_date": "2026-01-09",
                "input_artifact_path": "/tmp/weekly-input.csv",
                "input_artifact_source": "unit_test",
                "signal_state": "no_signal",
                "signal_policy": "no_signal_to_flat_v1",
                "signal_policy_applied": True,
                "no_signal_reason": "core_output_missing_current_feature",
                "source_component": "rule_vote",
            },
        )

    def test_flat_policy_skips_when_target_label_is_missing(self) -> None:
        weekly_df = _weekly_frame()
        weekly_df.loc[1, "TB5YWI3C"] = None

        rows = _build_rows(
            weekly_df,
            spec=_spec(
                no_signal_policy="flat",
                no_signal_source_component="rule_vote",
            ),
        )

        self.assertEqual(rows, [])

    def test_flat_policy_does_not_catch_prediction_errors(self) -> None:
        def fail_prediction(history: pd.DataFrame, feature_week_id: int):
            del history, feature_week_id
            raise RuntimeError("core failed")

        with self.assertRaisesRegex(RuntimeError, "core failed"):
            _build_rows(
                _weekly_frame(),
                spec=_spec(
                    no_signal_policy="flat",
                    no_signal_source_component="rule_vote",
                ),
                predict_for_feature=fail_prediction,
            )

    def test_core_output_index_rejects_empty_and_invalid_week_ids(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unit core 未产生任何输出"):
            index_weekly_core_output_rows(pd.DataFrame(), source_component="unit")
        with self.assertRaisesRegex(ValueError, "week_id 必须为严格整数"):
            index_weekly_core_output_rows(
                pd.DataFrame({"week_id": ["invalid"]}),
                source_component="unit",
            )

    def test_compact_benchmark_excludes_policy_generated_rows(self) -> None:
        source_row = {
            "feature_date": "2026-01-09",
            "target_date": "2026-01-16",
            "target_tenor": "5Y",
            "horizon": 6,
            "predicted_direction": 0,
            "confidence": 0.5,
            "label": 1,
            "extra": {"feature_week_id": 202602, "target_week_id": 202603},
        }
        policy_row = {
            **source_row,
            "feature_date": "2026-01-02",
            "extra": {
                "feature_week_id": 202601,
                "target_week_id": 202602,
                "signal_policy_applied": True,
            },
        }

        compact = compact_weekly_benchmark_rows([policy_row, source_row])

        self.assertEqual(len(compact), 1)
        self.assertEqual(compact[0]["feature_week_id"], 202602)
        self.assertEqual(compact[0]["direction"], 0)


def _spec(**overrides: object) -> WeeklyBacktestSpec:
    values = {
        "benchmark_id": "unit_benchmark",
        "scheme_id": "unit_weekly",
        "target_tenor": "5Y",
        "horizon_days": 6,
        "target_column": "TB5YWI3C",
        "model_version": "unit.v1",
        "predict_start_date": "2025-01-01",
        "live_target_start_date": "2026-06-01",
        "target_rule": "unit_next_week",
    }
    values.update(overrides)
    return WeeklyBacktestSpec(**values)


def _weekly_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "week_id": [202601, 202602],
            "TB5YWI3C": [2.0, 2.1],
        }
    )


def _calendar() -> SimpleNamespace:
    week_dates = {
        202601: "2026-01-02",
        202602: "2026-01-09",
    }
    return SimpleNamespace(
        week_id_to_last_trading_day=lambda week_id: week_dates[int(week_id)],
        next_trading_days=lambda day, count: ["2026-01-09"][:count] if day == "2026-01-02" else [],
        week_id_for_date=lambda day: {value: key for key, value in week_dates.items()}.get(day),
    )


def _build_rows(
    weekly_df: pd.DataFrame,
    *,
    spec: WeeklyBacktestSpec,
    predict_for_feature=None,
) -> list[dict[str, object]]:
    return build_weekly_backtest_rows(
        weekly_df,
        calendar=_calendar(),
        spec=spec,
        artifact_path=Path("/tmp/weekly-input.csv"),
        artifact_source="unit_test",
        normalize_frame=lambda frame: frame.copy(),
        predict_for_feature=predict_for_feature or (lambda history, feature_week_id: None),
    )


if __name__ == "__main__":
    unittest.main()
