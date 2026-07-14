from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


class Daily0629SourceEvidenceTests(unittest.TestCase):
    def test_require_daily_source_evidence_reads_manifest_entry(self) -> None:
        from shared.daily_0629_source_evidence import DAILY_0629_SOURCE_ROLE, require_daily_0629_source_evidence

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            batch = root / "source_evidence" / "benchmark_batches" / "daily_0629"
            source = batch / "source_package" / "forecast_project"
            source.mkdir(parents=True)
            (source / "README.md").write_text("fake daily source", encoding="utf-8")
            (batch / "manifest.json").write_text(
                textwrap.dedent(
                    f"""
                    {{
                      "source_role": "{DAILY_0629_SOURCE_ROLE}",
                      "source_package": "source_package/forecast_project",
                      "runner_module": "daily.run_backtest",
                      "live_runner_module": "daily.run_daily",
                      "python_env": "forecast_env",
                      "schemes": {{
                        "daily_10y_lgbm_10y04_0629": {{
                          "source_role": "{DAILY_0629_SOURCE_ROLE}",
                          "frequency": "D10Y",
                          "target_tenor": "10Y",
                          "final_select_id": "10Y04",
                          "candidate_id": "10y_all_daily_ic_top200_lgbm_7sig",
                          "target_col": "TB0YWI0C",
                          "model_id": "10y_all_daily_ic_top200_lgbm_7sig"
                        }}
                      }}
                    }}
                    """
                ).strip(),
                encoding="utf-8",
            )

            evidence = require_daily_0629_source_evidence("daily_10y_lgbm_10y04_0629", project_root=root)

        self.assertEqual(evidence.source_role, DAILY_0629_SOURCE_ROLE)
        self.assertEqual(evidence.frequency, "D10Y")
        self.assertEqual(evidence.target_tenor, "10Y")
        self.assertEqual(evidence.final_select_id, "10Y04")
        self.assertEqual(len(evidence.source_package_hash), 64)

    def test_source_live_runner_copies_package_and_reads_selected_rows(self) -> None:
        from shared.daily_0629_source_evidence import Daily0629SourceEvidence
        from shared.daily_0629_source_runner import run_source_daily_live

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "forecast_project"
            module_dir = source / "daily_project" / "src" / "daily"
            module_dir.mkdir(parents=True)
            (module_dir / "__init__.py").write_text("", encoding="utf-8")
            (source / "daily_project" / "src" / "__init__.py").write_text("", encoding="utf-8")
            (source / "daily_project" / "src" / "run_daily.sh").write_text(
                textwrap.dedent(
                    """
                    #!/bin/sh
                    set -eu
                    mkdir -p daily_project/output/$2/prediction
                    cat > daily_project/output/$2/prediction/daily_selected_prediction_rows.csv <<'CSV'
                    rdate,frequency,final_select_id,candidate_id,prediction_date,expected_prediction_date,is_prediction_date_aligned,pred_label,result,prob_up
                    2026-06-10,D10Y,10Y04,10y_all_daily_ic_top200_lgbm_7sig,2026-06-09,2026-06-09,True,-1,多,0.51
                    CSV
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (source / "daily_project" / "src" / "run_daily.sh").chmod(0o755)
            evidence = Daily0629SourceEvidence(
                scheme_id="daily_10y_lgbm_10y04_0629",
                source_role="source_original_daily_0629_algorithm",
                generator="fake",
                manifest_path=source / "manifest.json",
                source_package_path=source,
                source_package_hash="0" * 64,
                runner_module="daily.run_backtest",
                live_runner_module="daily.run_daily",
                frequency="D10Y",
                target_tenor="10Y",
                final_select_id="10Y04",
                candidate_id="10y_all_daily_ic_top200_lgbm_7sig",
                target_col="TB0YWI0C",
                model_id="10y_all_daily_ic_top200_lgbm_7sig",
            )

            with patch("shared.daily_0629_source_runner._python_command", return_value=[__import__("sys").executable]):
                rows = run_source_daily_live(evidence, predict_date="2026-06-10")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["frequency"], "D10Y")
        self.assertEqual(rows[0]["source_output_date"], "2026-06-10")

    def test_live_model_version_uses_short_select_id_for_db_column(self) -> None:
        from shared.daily_0629_predict_adapter import _model_version_from_evidence
        from shared.daily_0629_source_evidence import Daily0629SourceEvidence

        evidence = Daily0629SourceEvidence(
            scheme_id="daily_5y_lgbm_5y10_0629",
            source_role="source_original_daily_0629_algorithm",
            generator="fake",
            manifest_path=Path("manifest.json"),
            source_package_path=Path("forecast_project"),
            source_package_hash="0" * 64,
            runner_module="daily.run_backtest",
            live_runner_module="daily.run_daily",
            frequency="D5Y",
            target_tenor="5Y",
            final_select_id="5Y10",
            candidate_id="5Y_weekmap_top120_seed_quota_7sig_combo_rolling40_target_0.50_mtd_floor_0.50_causal",
            target_col="TB5YWI0C",
            model_id="5Y_weekmap_top120_seed_quota_7sig_combo_rolling40_target_0.50_mtd_floor_0.50_causal",
        )

        self.assertGreater(len(evidence.model_id), 64)
        model_version = _model_version_from_evidence(evidence)
        self.assertEqual(model_version, "5Y10")
        self.assertLessEqual(len(model_version), 64)


if __name__ == "__main__":
    unittest.main()
