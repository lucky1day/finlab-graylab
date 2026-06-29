from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class WeeklyAverageSourceEvidenceTests(unittest.TestCase):
    def test_rebuild_specs_do_not_reference_weekly_point_runners(self) -> None:
        from scripts import rebuild_weekly_average_0529_benchmarks as rebuild

        for scheme_id, spec in rebuild.SPECS.items():
            with self.subTest(scheme_id=scheme_id):
                self.assertFalse(hasattr(spec, "point_scheme_id"))
                self.assertFalse(hasattr(spec, "point_runner_module"))
                self.assertFalse(hasattr(spec, "point_runner_function"))
                self.assertNotIn("weekly_10y_d_overlay_0529_reproduction", repr(spec))

    def test_missing_weekly_average_source_manifest_fails_closed(self) -> None:
        from shared.weekly_average_source_evidence import require_weekly_average_source_evidence

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaisesRegex(RuntimeError, "weekly average source evidence missing"):
                require_weekly_average_source_evidence(
                    "weekly_avg_10y_d_overlay_0529",
                    project_root=root,
                )

    def test_manifest_cannot_point_to_weekly_point_algorithm(self) -> None:
        from shared.weekly_average_source_evidence import require_weekly_average_source_evidence

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            batch_dir = root / "source_evidence" / "benchmark_batches" / "model_muti_0529" / "weekly_average_0529"
            source_package = batch_dir / "weekly_10y_d_overlay_0529" / "source_package"
            source_package.mkdir(parents=True)
            (batch_dir / "manifest.json").write_text(
                """
{
  "source_role": "source_original_weekly_average_algorithm",
  "source_package": "weekly_10y_d_overlay_0529/source_package",
  "schemes": {
    "weekly_avg_10y_d_overlay_0529": {
      "frequency": "W10Y",
      "target_tenor": "10Y",
      "target_column": "TB0YWI1C",
      "model_id": "WEEKLY-10Y-LGBM-01",
      "generator": "point runner"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
                )

            with self.assertRaisesRegex(RuntimeError, "must not reference weekly point"):
                require_weekly_average_source_evidence(
                    "weekly_avg_10y_d_overlay_0529",
                    project_root=root,
                )

    def test_manifest_requires_source_package_not_prebuilt_predictions(self) -> None:
        from shared.weekly_average_source_evidence import require_weekly_average_source_evidence

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            batch_dir = root / "source_evidence" / "benchmark_batches" / "model_muti_0529" / "weekly_average_0529"
            source_package = batch_dir / "source_package" / "forecast_project"
            source_package.mkdir(parents=True)
            (source_package / "README.md").write_text("source package\n", encoding="utf-8")
            (batch_dir / "manifest.json").write_text(
                """
{
  "source_role": "source_original_weekly_average_algorithm",
  "source_package": "source_package/forecast_project",
  "runner_module": "weekly.run_backtest",
  "live_runner_module": "weekly.run_weekly",
  "schemes": {
    "weekly_avg_1y_lgbm_0529": {
      "frequency": "W1Y",
      "target_tenor": "1Y",
      "target_column": "TB1YWI1C",
      "model_id": "WEEKLY-1Y-LGBM-01",
      "generator": "source_package.weekly.run_backtest:W1Y"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            evidence = require_weekly_average_source_evidence(
                "weekly_avg_1y_lgbm_0529",
                project_root=root,
            )

            self.assertEqual(evidence.frequency, "W1Y")
            self.assertEqual(evidence.target_tenor, "1Y")
            self.assertEqual(evidence.source_package_path, source_package.resolve())
            self.assertFalse(hasattr(evidence, "original_predictions_path"))

    def test_manifest_rejects_weekly_average_7y_entry(self) -> None:
        from shared.weekly_average_source_evidence import require_weekly_average_source_evidence

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            batch_dir = root / "source_evidence" / "benchmark_batches" / "model_muti_0529" / "weekly_average_0529"
            source_package = batch_dir / "source_package" / "forecast_project"
            source_package.mkdir(parents=True)
            (batch_dir / "manifest.json").write_text(
                """
{
  "source_role": "source_original_weekly_average_algorithm",
  "source_package": "source_package/forecast_project",
  "schemes": {
    "weekly_avg_7y_lgbm_0529": {
      "frequency": "W7Y",
      "target_tenor": "7Y",
      "target_column": "TB7YWI1C",
      "model_id": "WEEKLY-7Y-LGBM-01",
      "generator": "source_package.weekly.run_backtest:W7Y"
    }
  }
}
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "does not support 7Y"):
                require_weekly_average_source_evidence(
                    "weekly_avg_7y_lgbm_0529",
                    project_root=root,
                )


if __name__ == "__main__":
    unittest.main()
