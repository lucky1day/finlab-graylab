from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from harness.context import GateContext
from harness.gates.compare_gate import CompareGate
from harness.result import GateStatus


def _make_ctx(root: Path, scheme_id: str = "demo") -> GateContext:
    return GateContext(
        scheme_id=scheme_id,
        predict_date="static",
        project_root=root,
        report_dir=root / "reports" / "out",
    )


def _write_predictions(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["predict_date", "tenor", "direction", "confidence"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_strict_predictions(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "feature_date",
        "target_date",
        "target_tenor",
        "horizon",
        "direction",
        "confidence",
        "label",
        "is_correct",
    ]
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_benchmark_required_config(
    root: Path,
    scheme_id: str = "demo",
    *,
    frequency: str = "daily",
    task_type: str | None = None,
    required_internal_fields: list[str] | None = None,
) -> None:
    config = root / "schemes" / scheme_id / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"scheme_id: {scheme_id}",
        "horizon: 5",
        "tenors: [\"5Y\"]",
        f"frequency: {frequency}",
        "status: active",
    ]
    if task_type is not None:
        lines.append(f"task_type: {task_type}")
    lines.extend(
        [
            "backtest:",
            "  benchmark_required: true",
        ]
    )
    if required_internal_fields:
        rendered = ", ".join(f'"{field}"' for field in required_internal_fields)
        lines.append(f"  required_internal_fields: [{rendered}]")
    config.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


class CompareGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_benchmark_skips(self) -> None:
        ctx = _make_ctx(self.root)
        (self.root / "schemes" / "demo").mkdir(parents=True)
        result = CompareGate(ctx).run()
        self.assertEqual(result.status, GateStatus.SKIPPED)
        self.assertTrue(result.passed)

    def test_identical_outputs_passed(self) -> None:
        ctx = _make_ctx(self.root)
        bench = self.root / "schemes" / "demo" / "benchmarks"
        rows = [
            {"predict_date": "2025-01-02", "tenor": "5Y", "direction": "up", "confidence": "0.6"},
            {"predict_date": "2025-01-02", "tenor": "7Y", "direction": "down", "confidence": "0.4"},
        ]
        _write_predictions(bench / "original_predictions_sample.csv", rows)
        _write_predictions(bench / "current_predictions_sample.csv", rows)
        result = CompareGate(ctx).run()
        self.assertEqual(result.status, GateStatus.PASSED, result.errors)
        self.assertTrue(result.passed)

    def test_direction_mismatch_failed(self) -> None:
        ctx = _make_ctx(self.root)
        bench = self.root / "schemes" / "demo" / "benchmarks"
        original = [
            {"predict_date": "2025-01-02", "tenor": "5Y", "direction": "up", "confidence": "0.6"},
        ]
        current = [
            {"predict_date": "2025-01-02", "tenor": "5Y", "direction": "down", "confidence": "0.6"},
        ]
        _write_predictions(bench / "original_predictions_sample.csv", original)
        _write_predictions(bench / "current_predictions_sample.csv", current)
        result = CompareGate(ctx).run()
        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("direction_match_rate" in e for e in result.errors), result.errors)

    def test_benchmark_required_new_format_missing_target_date_failed(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root)
        bench = self.root / "schemes" / "demo" / "benchmarks"
        rows = [
            {
                "feature_date": "2025-01-02",
                "target_date": "",
                "target_tenor": "5Y",
                "horizon": "5",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        _write_strict_predictions(bench / "original_predictions_sample.csv", rows)
        _write_strict_predictions(bench / "current_predictions_sample.csv", rows)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("missing required benchmark columns/values" in e for e in result.errors), result.errors)

    def test_benchmark_required_new_format_uses_target_date_in_key(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root)
        bench = self.root / "schemes" / "demo" / "benchmarks"
        original = [
            {
                "feature_date": "2025-01-02",
                "target_date": "2025-01-09",
                "target_tenor": "5Y",
                "horizon": "5",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        current = [
            {
                "feature_date": "2025-01-02",
                "target_date": "2025-01-10",
                "target_tenor": "5Y",
                "horizon": "5",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        _write_strict_predictions(bench / "original_predictions_sample.csv", original)
        _write_strict_predictions(bench / "current_predictions_sample.csv", current)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("missing dates/tenors" in e for e in result.errors), result.errors)
        self.assertTrue(any("extra dates/tenors" in e for e in result.errors), result.errors)

    def test_weekly_required_format_uses_feature_week_id_in_key(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root, frequency="weekly")
        bench = self.root / "schemes" / "demo" / "benchmarks"
        original = [
            {
                "feature_week_id": "202501",
                "feature_date": "2025-01-03",
                "target_week_id": "202502",
                "target_date": "2025-01-10",
                "target_tenor": "5Y",
                "horizon": "6",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        current = [{**original[0], "feature_week_id": "202500"}]
        _write_strict_predictions(bench / "original_predictions_sample.csv", original)
        _write_strict_predictions(bench / "current_predictions_sample.csv", current)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("missing dates/tenors" in e for e in result.errors), result.errors)
        self.assertTrue(any("extra dates/tenors" in e for e in result.errors), result.errors)

    def test_weekly_average_requires_target_rule(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root, frequency="weekly", task_type="weekly_average")
        bench = self.root / "schemes" / "demo" / "benchmarks"
        rows = [
            {
                "feature_week_id": "202501",
                "feature_date": "2025-01-03",
                "target_week_id": "202502",
                "target_date": "2025-01-10",
                "target_tenor": "5Y",
                "horizon": "6",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        _write_strict_predictions(bench / "original_predictions_sample.csv", rows)
        _write_strict_predictions(bench / "current_predictions_sample.csv", rows)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("missing required benchmark columns/values" in e for e in result.errors), result.errors)

    def test_weekly_average_requires_target_rule_match(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root, frequency="weekly", task_type="weekly_average")
        bench = self.root / "schemes" / "demo" / "benchmarks"
        original = [
            {
                "feature_week_id": "202501",
                "feature_date": "2025-01-03",
                "target_week_id": "202502",
                "target_date": "2025-01-10",
                "target_tenor": "5Y",
                "horizon": "6",
                "target_rule": "next_week_average_yield_vs_current_week_average_yield",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        current = [{**original[0], "target_rule": "next_week_point_yield_vs_current_week_point_yield"}]
        _write_strict_predictions(bench / "original_predictions_sample.csv", original)
        _write_strict_predictions(bench / "current_predictions_sample.csv", current)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("strict benchmark value mismatches" in e for e in result.errors), result.errors)
        summary = json.loads((ctx.report_dir / "comparison_summary.json").read_text(encoding="utf-8"))
        pred = summary["comparison"]["predictions"]
        self.assertEqual(pred["strict_value_mismatches"][0]["field"], "target_rule")

    def test_weekly_average_requires_target_rule_match_config(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root, frequency="weekly", task_type="weekly_average")
        config = self.root / "schemes" / "demo" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + 'target_rule: "next_week_average_yield_vs_current_week_average_yield"\n',
            encoding="utf-8",
        )
        bench = self.root / "schemes" / "demo" / "benchmarks"
        rows = [
            {
                "feature_week_id": "202501",
                "feature_date": "2025-01-03",
                "target_week_id": "202502",
                "target_date": "2025-01-10",
                "target_tenor": "5Y",
                "horizon": "6",
                "target_rule": "wrong_rule",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        _write_strict_predictions(bench / "original_predictions_sample.csv", rows)
        _write_strict_predictions(bench / "current_predictions_sample.csv", rows)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("strict benchmark value mismatches" in e for e in result.errors), result.errors)
        summary = json.loads((ctx.report_dir / "comparison_summary.json").read_text(encoding="utf-8"))
        pred = summary["comparison"]["predictions"]
        self.assertEqual(len(pred["strict_value_mismatches"]), 2)
        self.assertEqual({item["source"] for item in pred["strict_value_mismatches"]}, {"original", "current"})

    def test_weekly_average_rejects_point_backed_provenance(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root, frequency="weekly", task_type="weekly_average")
        bench = self.root / "schemes" / "demo" / "benchmarks"
        rows = [
            {
                "feature_week_id": "202501",
                "feature_date": "2025-01-03",
                "target_week_id": "202502",
                "target_date": "2025-01-10",
                "target_tenor": "5Y",
                "horizon": "6",
                "target_rule": "next_week_average_yield_vs_current_week_average_yield",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        _write_strict_predictions(bench / "original_predictions_sample.csv", rows)
        _write_strict_predictions(bench / "current_predictions_sample.csv", rows)
        for summary_name in ("original_backtest_summary.json", "current_backtest_summary.json"):
            (bench / summary_name).write_text(
                json.dumps(
                    {
                        "benchmark_provenance": {
                            "source_role": "source_original_predictions_with_weekly_average_actual_oracle",
                            "bootstrap_source": "source_backed_point_runner_plus_weekly_average_oracle",
                        }
                    }
                ),
                encoding="utf-8",
            )

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("weekly average benchmark provenance" in e for e in result.errors), result.errors)

    def test_benchmark_required_duplicate_strict_key_failed(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root)
        bench = self.root / "schemes" / "demo" / "benchmarks"
        rows = [
            {
                "feature_date": "2026-05-18",
                "target_date": "2026-05-25",
                "target_tenor": "7Y",
                "horizon": "5",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
            {
                "feature_date": "2026-05-18",
                "target_date": "2026-05-25",
                "target_tenor": "7Y",
                "horizon": "5",
                "direction": "-1",
                "confidence": "0.4",
                "label": "1",
                "is_correct": "false",
            },
        ]
        _write_strict_predictions(bench / "original_predictions_sample.csv", rows)
        _write_strict_predictions(bench / "current_predictions_sample.csv", rows[:1])

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("duplicate strict benchmark keys" in e for e in result.errors), result.errors)
        summary = json.loads((ctx.report_dir / "comparison_summary.json").read_text(encoding="utf-8"))
        pred = summary["comparison"]["predictions"]
        self.assertEqual(pred["duplicate_key_errors"][0]["source"], "original")

    def test_required_internal_fields_missing_column_failed(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(
            self.root,
            required_internal_fields=["vote_score", "model_score", "model_dir"],
        )
        bench = self.root / "schemes" / "demo" / "benchmarks"
        rows = [
            {
                "feature_date": "2026-05-18",
                "target_date": "2026-05-25",
                "target_tenor": "7Y",
                "horizon": "5",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
                "vote_score": "0.2",
                "model_score": "0.2",
            },
        ]
        _write_strict_predictions(bench / "original_predictions_sample.csv", rows)
        _write_strict_predictions(bench / "current_predictions_sample.csv", rows)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("missing required internal benchmark" in e for e in result.errors), result.errors)

    def test_required_internal_numeric_mismatch_failed(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(
            self.root,
            required_internal_fields=["custom_score", "custom_signal"],
        )
        bench = self.root / "schemes" / "demo" / "benchmarks"
        original = [
            {
                "feature_date": "2026-05-18",
                "target_date": "2026-05-25",
                "target_tenor": "7Y",
                "horizon": "5",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
                "custom_score": "0.2",
                "custom_signal": "source",
            },
        ]
        current = [{**original[0], "custom_score": "0.20000002"}]
        _write_strict_predictions(bench / "original_predictions_sample.csv", original)
        _write_strict_predictions(bench / "current_predictions_sample.csv", current)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("internal benchmark" in e for e in result.errors), result.errors)
        summary = json.loads((ctx.report_dir / "comparison_summary.json").read_text(encoding="utf-8"))
        pred = summary["comparison"]["predictions"]
        self.assertEqual(pred["internal_mismatch_count"], 1)
        self.assertEqual(pred["internal_mismatches"][0]["field"], "custom_score")

    def test_internal_score_mismatch_failed_when_benchmark_declares_internal_columns(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root)
        bench = self.root / "schemes" / "demo" / "benchmarks"
        original = [
            {
                "feature_date": "2026-05-18",
                "target_date": "2026-05-25",
                "target_tenor": "7Y",
                "horizon": "5",
                "direction": "0",
                "confidence": "0.0",
                "label": "-1",
                "is_correct": "false",
                "STD_score": "-1.0488493212117835",
                "STD_dir": "-1",
                "CROSS_5Y_score": "0.008396337253219598",
                "CROSS_5Y_dir": "1",
            },
        ]
        current = [
            {
                **original[0],
                "CROSS_5Y_score": "-0.6420104710111437",
                "CROSS_5Y_dir": "-1",
            },
        ]
        _write_strict_predictions(bench / "original_predictions_sample.csv", original)
        _write_strict_predictions(bench / "current_predictions_sample.csv", current)

        result = CompareGate(ctx).run()

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("internal benchmark" in e for e in result.errors), result.errors)
        summary = json.loads((ctx.report_dir / "comparison_summary.json").read_text(encoding="utf-8"))
        pred = summary["comparison"]["predictions"]
        self.assertEqual(pred["internal_mismatch_count"], 2)
        self.assertAlmostEqual(pred["max_internal_abs_diff"], 0.6504068082643633)

    def test_metric_diff_failed(self) -> None:
        ctx = _make_ctx(self.root)
        bench = self.root / "schemes" / "demo" / "benchmarks"
        rows = [{"predict_date": "2025-01-02", "tenor": "5Y", "direction": "up", "confidence": "0.6"}]
        _write_predictions(bench / "original_predictions_sample.csv", rows)
        _write_predictions(bench / "current_predictions_sample.csv", rows)
        (bench / "original_backtest_summary.json").write_text(
            json.dumps({"accuracy": 0.80}), encoding="utf-8"
        )
        (bench / "current_backtest_summary.json").write_text(
            json.dumps({"accuracy": 0.90}), encoding="utf-8"
        )
        result = CompareGate(ctx).run()
        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("accuracy" in e for e in result.errors), result.errors)


if __name__ == "__main__":
    unittest.main()
