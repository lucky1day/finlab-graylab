from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


class MonthlySourceEvidenceTests(unittest.TestCase):
    def test_require_monthly_source_evidence_reads_manifest_entry(self) -> None:
        from shared.monthly_source_evidence import MONTHLY_SOURCE_ROLE, require_monthly_source_evidence

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            batch = root / "source_evidence" / "benchmark_batches" / "monthly_0629"
            source = batch / "source_package" / "forecast_project"
            source.mkdir(parents=True)
            (source / "README.md").write_text("fake source", encoding="utf-8")
            (batch / "manifest.json").write_text(
                textwrap.dedent(
                    f"""
                    {{
                      "source_role": "{MONTHLY_SOURCE_ROLE}",
                      "source_package": "source_package/forecast_project",
                      "runner_module": "src.monthly.run_monthly_pipeline",
                      "python_env": "forecast_env",
                      "schemes": {{
                        "monthly_10y_rf_top5_0629": {{
                          "source_role": "{MONTHLY_SOURCE_ROLE}",
                          "frequency": "M10Y",
                          "target_tenor": "10Y",
                          "final_select_id": "10Y-01",
                          "model_id": "monthly_10y_rf_top5",
                          "candidate_id": "10Y::Random Forest::top5",
                          "generator": "source_package.src.monthly.run_monthly_pipeline:M10Y"
                        }}
                      }}
                    }}
                    """
                ).strip(),
                encoding="utf-8",
            )

            evidence = require_monthly_source_evidence("monthly_10y_rf_top5_0629", project_root=root)

        self.assertEqual(evidence.source_role, MONTHLY_SOURCE_ROLE)
        self.assertEqual(evidence.frequency, "M10Y")
        self.assertEqual(evidence.target_tenor, "10Y")
        self.assertEqual(evidence.final_select_id, "10Y-01")
        self.assertEqual(len(evidence.source_package_hash), 64)

    def test_source_runner_copies_package_and_reads_monthly_predictions(self) -> None:
        from shared.monthly_source_evidence import MonthlySourceEvidence
        from shared.monthly_source_runner import run_source_monthly_live

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "forecast_project"
            module_dir = source / "monthly_project" / "src" / "monthly"
            module_dir.mkdir(parents=True)
            (source / "monthly_project" / "src" / "__init__.py").write_text("", encoding="utf-8")
            (module_dir / "__init__.py").write_text("", encoding="utf-8")
            (module_dir / "run_monthly_pipeline.py").write_text(
                textwrap.dedent(
                    """
                    from pathlib import Path

                    def run_monthly_pipeline(run_date=None, *, dry_run=False):
                        root = Path(__file__).resolve().parents[2]
                        out = root / "output" / run_date / "prediction"
                        out.mkdir(parents=True, exist_ok=True)
                        (out / "monthly_selected_predictions.csv").write_text(
                            "tenor,frequency,final_select_id,y_pred,pred_proba_up,pred_proba_down\\n"
                            "10Y,M10Y,10Y-01,0,0.31,0.69\\n",
                            encoding="utf-8",
                        )
                        return None
                    """
                ),
                encoding="utf-8",
            )
            evidence = MonthlySourceEvidence(
                scheme_id="monthly_10y_rf_top5_0629",
                source_role="source_original_monthly_algorithm",
                generator="fake",
                manifest_path=source / "manifest.json",
                source_package_path=source,
                source_package_hash="0" * 64,
                runner_module="src.monthly.run_monthly_pipeline",
                frequency="M10Y",
                target_tenor="10Y",
                final_select_id="10Y-01",
                model_id="monthly_10y_rf_top5",
                candidate_id="10Y::Random Forest::top5",
            )

            with patch("shared.monthly_source_runner._python_command", return_value=[__import__("sys").executable]):
                rows = run_source_monthly_live(evidence, predict_date="2026-04-15")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["frequency"], "M10Y")
        self.assertEqual(rows[0]["source_output_date"], "2026-04-15")
