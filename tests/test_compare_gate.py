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
        "benchmark_role",
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
            writer.writerow({"benchmark_role": "platform_current", **row})


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


def _benchmark_role_key(row: dict[str, str]) -> tuple[tuple[str, str], ...]:
    candidates = [
        "feature_month_id",
        "feature_week_id",
        "feature_date",
        "target_month_id",
        "target_week_id",
        "target_date",
        "target_tenor",
        "tenor",
        "horizon",
        "predict_date",
        "date",
    ]
    return tuple((name, str(row.get(name) or "").strip()) for name in candidates if name in row)


class CompareGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        config = self.root / "schemes" / "demo" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("scheme_id: demo\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_no_benchmark_skips(self) -> None:
        ctx = _make_ctx(self.root)
        result = CompareGate().run(ctx)
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
        result = CompareGate().run(ctx)
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
        result = CompareGate().run(ctx)
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

        result = CompareGate().run(ctx)

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

        result = CompareGate().run(ctx)

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("missing dates/tenors" in e for e in result.errors), result.errors)
        self.assertTrue(any("extra dates/tenors" in e for e in result.errors), result.errors)

    def test_benchmark_required_new_format_uses_benchmark_role_in_key(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root)
        bench = self.root / "schemes" / "demo" / "benchmarks"
        original = [
            {
                "feature_date": "2025-01-02",
                "target_date": "2025-01-09",
                "target_tenor": "5Y",
                "horizon": "5",
                "benchmark_role": "source-original",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        current = [{**original[0], "benchmark_role": "source-compatible-extension"}]
        _write_strict_predictions(bench / "original_predictions_sample.csv", original)
        _write_strict_predictions(bench / "current_predictions_sample.csv", current)

        result = CompareGate().run(ctx)

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

        result = CompareGate().run(ctx)

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("missing dates/tenors" in e for e in result.errors), result.errors)
        self.assertTrue(any("extra dates/tenors" in e for e in result.errors), result.errors)



    def test_monthly_required_format_uses_feature_month_id_in_key(self) -> None:
        ctx = _make_ctx(self.root)
        _write_benchmark_required_config(self.root, frequency="monthly", task_type="monthly")
        config = self.root / "schemes" / "demo" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + 'target_rule: "next_month_observation_yield_vs_feature_month_observation_yield"\n',
            encoding="utf-8",
        )
        bench = self.root / "schemes" / "demo" / "benchmarks"
        original = [
            {
                "feature_month_id": "2026-04",
                "feature_date": "2026-04-15",
                "target_month_id": "2026-05",
                "target_date": "2026-05-15",
                "target_tenor": "10Y",
                "horizon": "30",
                "target_rule": "next_month_observation_yield_vs_feature_month_observation_yield",
                "direction": "1",
                "confidence": "0.6",
                "label": "1",
                "is_correct": "true",
            },
        ]
        current = [{**original[0], "feature_month_id": "2026-03"}]
        _write_strict_predictions(bench / "original_predictions_sample.csv", original)
        _write_strict_predictions(bench / "current_predictions_sample.csv", current)

        result = CompareGate().run(ctx)

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("missing dates/tenors" in e for e in result.errors), result.errors)
        self.assertTrue(any("extra dates/tenors" in e for e in result.errors), result.errors)






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

        result = CompareGate().run(ctx)

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

        result = CompareGate().run(ctx)

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("internal benchmark" in e for e in result.errors), result.errors)
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["internal_mismatch_count"], 1)
        self.assertEqual(evidence["internal_mismatches"][0]["field"], "custom_score")


    def test_tracked_benchmark_samples_have_nonempty_aligned_roles(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        missing_or_blank: list[str] = []
        inconsistent: list[str] = []

        for bench_dir in sorted((project_root / "schemes").glob("*/benchmarks")):
            original_path = bench_dir / "original_predictions_sample.csv"
            current_path = bench_dir / "current_predictions_sample.csv"
            if not original_path.exists() or not current_path.exists():
                continue
            with original_path.open(newline="", encoding="utf-8") as handle:
                original_reader = csv.DictReader(handle)
                original_rows = list(original_reader)
                original_fields = original_reader.fieldnames or []
            with current_path.open(newline="", encoding="utf-8") as handle:
                current_reader = csv.DictReader(handle)
                current_rows = list(current_reader)
                current_fields = current_reader.fieldnames or []

            for path, fields, rows in (
                (original_path, original_fields, original_rows),
                (current_path, current_fields, current_rows),
            ):
                if "benchmark_role" not in fields:
                    missing_or_blank.append(str(path.relative_to(project_root)))
                    continue
                blank_count = sum(1 for row in rows if not str(row.get("benchmark_role") or "").strip())
                if blank_count:
                    missing_or_blank.append(f"{path.relative_to(project_root)}:{blank_count} blank roles")

            original_role_by_key = {
                _benchmark_role_key(row): str(row.get("benchmark_role") or "").strip()
                for row in original_rows
            }
            for row in current_rows:
                key = _benchmark_role_key(row)
                expected_role = original_role_by_key.get(key)
                current_role = str(row.get("benchmark_role") or "").strip()
                if expected_role and current_role != expected_role:
                    inconsistent.append(str(current_path.relative_to(project_root)))
                    break

        self.assertEqual(missing_or_blank, [])
        self.assertEqual(inconsistent, [])

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
        result = CompareGate().run(ctx)
        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertTrue(any("accuracy" in e for e in result.errors), result.errors)
