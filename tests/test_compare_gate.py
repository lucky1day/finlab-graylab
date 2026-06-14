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
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_benchmark_required_config(root: Path, scheme_id: str = "demo") -> None:
    config = root / "schemes" / scheme_id / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                "horizon: 5",
                "tenors: [\"5Y\"]",
                "frequency: daily",
                "status: active",
                "backtest:",
                "  benchmark_required: true",
            ]
        )
        + "\n",
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
