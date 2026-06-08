from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class CompareRefactorOutputsTests(unittest.TestCase):
    def test_identical_json_directories_have_zero_diffs(self) -> None:
        from scripts.compare_refactor_outputs import compare_paths

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "baseline"
            current = root / "current"
            baseline.mkdir()
            current.mkdir()
            payload = [{"scheme_id": "t5_daily", "confidence": 0.42}]
            (baseline / "dry_run.json").write_text(json.dumps(payload), encoding="utf-8")
            (current / "dry_run.json").write_text(json.dumps(payload), encoding="utf-8")

            report = compare_paths(baseline, current)

        self.assertEqual(report.diff_count, 0)
        self.assertEqual(report.file_count, 1)
        self.assertEqual(report.diffs, [])

    def test_float_values_compare_with_tolerance(self) -> None:
        from scripts.compare_refactor_outputs import compare_paths

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "baseline.json"
            current = root / "current.json"
            baseline.write_text(json.dumps({"confidence": 0.28}), encoding="utf-8")
            current.write_text(json.dumps({"confidence": 0.2800000001}), encoding="utf-8")

            report = compare_paths(baseline, current, float_tolerance=1e-9)

        self.assertEqual(report.diff_count, 0)

    def test_missing_keys_and_changed_values_are_counted(self) -> None:
        from scripts.compare_refactor_outputs import compare_paths

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "baseline.json"
            current = root / "current.json"
            baseline.write_text(
                json.dumps({"target_date": "2026-06-12", "extra": {"feature_week_id": 202621}}),
                encoding="utf-8",
            )
            current.write_text(json.dumps({"target_date": "2026-06-19", "extra": {}}), encoding="utf-8")

            report = compare_paths(baseline, current)

        self.assertEqual(report.diff_count, 2)
        paths = {diff.path for diff in report.diffs}
        self.assertEqual(paths, {"$.target_date", "$.extra.feature_week_id"})

    def test_directory_compare_reports_extra_current_file(self) -> None:
        from scripts.compare_refactor_outputs import compare_paths

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "baseline"
            current = root / "current"
            baseline.mkdir()
            current.mkdir()
            (baseline / "dry_run.json").write_text("[]", encoding="utf-8")
            (current / "dry_run.json").write_text("[]", encoding="utf-8")
            (current / "new_backtest.json").write_text("{}", encoding="utf-8")

            report = compare_paths(baseline, current)

        self.assertEqual(report.diff_count, 1)
        self.assertEqual(report.diffs[0].kind, "extra_file")
        self.assertEqual(report.diffs[0].path, "new_backtest.json")

    def test_explicit_ignored_paths_are_not_counted(self) -> None:
        from scripts.compare_refactor_outputs import compare_paths

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = root / "baseline.json"
            current = root / "current.json"
            baseline.write_text(json.dumps({"summary": {"rows": 45}, "elapsed_sec": 1.23}), encoding="utf-8")
            current.write_text(json.dumps({"summary": {"rows": 45}, "elapsed_sec": 9.87}), encoding="utf-8")

            report = compare_paths(baseline, current, ignored_paths={"$.elapsed_sec"})

        self.assertEqual(report.diff_count, 0)


if __name__ == "__main__":
    unittest.main()
