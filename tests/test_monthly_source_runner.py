from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


def _database_config():
    from shared.source_runtime_database import (
        SourceRuntimeDatabaseConfig,
    )

    return SourceRuntimeDatabaseConfig(
        user="source_reader",
        password="source-test-secret",
        host="127.0.0.1",
        port=43306,
        database="bfl_source_test",
        charset="utf8mb4",
        config_path=Path("/private/source-db.json"),
    )


class MonthlySourceEvidenceTests(unittest.TestCase):
    def test_mutable_source_database_does_not_enable_cache_by_default(
        self,
    ) -> None:
        from shared.monthly_source_runner import _source_cache_path

        with patch.dict(
            os.environ,
            {"MONTHLY_SOURCE_CACHE_DIR": "/tmp/source-cache"},
            clear=True,
        ):
            self.assertIsNone(
                _source_cache_path(
                    "a" * 64,
                    "source.runner",
                    "2026-07-25",
                    "b" * 64,
                )
            )

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
                      "source_package_sha256": "69366fa0aa09cfaf9fa3e95d09ab599b8abe47c329592299fb2322ef802e5bd3",
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
            (source / "db_config.py").write_text(
                "DB_CONFIG = {'password': 'packaged-must-not-run'}\n",
                encoding="utf-8",
            )
            from shared.monthly_source_evidence import (
                source_package_tree_sha256,
            )

            evidence = MonthlySourceEvidence(
                scheme_id="monthly_10y_rf_top5_0629",
                source_role="source_original_monthly_algorithm",
                generator="fake",
                manifest_path=source / "manifest.json",
                source_package_path=source,
                source_package_hash=source_package_tree_sha256(source),
                runner_module="src.monthly.run_monthly_pipeline",
                frequency="M10Y",
                target_tenor="10Y",
                final_select_id="10Y-01",
                model_id="monthly_10y_rf_top5",
                candidate_id="10Y::Random Forest::top5",
            )

            with (
                patch.dict(
                    "os.environ",
                    {"MONTHLY_SOURCE_CACHE_DISABLE": "1"},
                    clear=False,
                ),
                patch(
                    "shared.monthly_source_runner._python_command",
                    return_value=[__import__("sys").executable],
                ),
            ):
                rows = run_source_monthly_live(
                    evidence,
                    predict_date="2026-04-15",
                    database_config=_database_config(),
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["frequency"], "M10Y")
        self.assertEqual(rows[0]["source_output_date"], "2026-04-15")

    def test_source_runner_reuses_endpoint_bound_disk_cache(self) -> None:
        from shared.monthly_source_evidence import MonthlySourceEvidence
        from shared.monthly_source_runner import run_source_monthly_live

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            source = root / "forecast_project"
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
            (source / "db_config.py").write_text(
                "DB_CONFIG = {'password': 'packaged-must-not-run'}\n",
                encoding="utf-8",
            )
            from shared.monthly_source_evidence import (
                source_package_tree_sha256,
            )

            evidence = MonthlySourceEvidence(
                scheme_id="monthly_10y_rf_top5_0629",
                source_role="source_original_monthly_algorithm",
                generator="fake",
                manifest_path=source / "manifest.json",
                source_package_path=source,
                source_package_hash=source_package_tree_sha256(source),
                runner_module="src.monthly.run_monthly_pipeline",
                frequency="M10Y",
                target_tenor="10Y",
                final_select_id="10Y-01",
                model_id="monthly_10y_rf_top5",
                candidate_id="10Y::Random Forest::top5",
            )

            with (
                patch.dict(
                    os.environ,
                    {
                        "MONTHLY_SOURCE_CACHE_DIR": str(cache_dir),
                        "BFL_SOURCE_IMMUTABLE_INPUT_TOKEN":
                            "c" * 64,
                    },
                    clear=False,
                ),
                patch("shared.monthly_source_runner._python_command", return_value=[__import__("sys").executable]),
            ):
                first_rows = run_source_monthly_live(
                    evidence,
                    predict_date="2026-04-15",
                    database_config=_database_config(),
                )
                with patch(
                    "shared.monthly_source_runner._run_monthly_module",
                    side_effect=AssertionError("source runner should not be called on disk cache hit"),
                ):
                    second_rows = run_source_monthly_live(
                        evidence,
                        predict_date="2026-04-15",
                        database_config=_database_config(),
                    )

        self.assertEqual(second_rows, first_rows)

    def test_disk_cache_cannot_bypass_source_package_identity_check(
        self,
    ) -> None:
        from shared.monthly_source_evidence import (
            MonthlySourceEvidence,
            source_package_tree_sha256,
        )
        from shared.monthly_source_runner import run_source_monthly_live

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            source = root / "forecast_project"
            module_dir = (
                source
                / "monthly_project"
                / "src"
                / "monthly"
            )
            module_dir.mkdir(parents=True)
            (source / "monthly_project" / "src" / "__init__.py").write_text(
                "",
                encoding="utf-8",
            )
            (module_dir / "__init__.py").write_text(
                "",
                encoding="utf-8",
            )
            runner = module_dir / "run_monthly_pipeline.py"
            runner.write_text(
                textwrap.dedent(
                    """
                    from pathlib import Path

                    def run_monthly_pipeline(run_date=None, *, dry_run=False):
                        root = Path(__file__).resolve().parents[2]
                        out = root / "output" / run_date / "prediction"
                        out.mkdir(parents=True, exist_ok=True)
                        (out / "monthly_selected_predictions.csv").write_text(
                            "frequency,final_select_id,y_pred\\n"
                            "M10Y,10Y-01,0\\n",
                            encoding="utf-8",
                        )
                    """
                ),
                encoding="utf-8",
            )
            (source / "db_config.py").write_text(
                "DB_CONFIG = {'password': 'packaged-must-not-run'}\n",
                encoding="utf-8",
            )
            evidence = MonthlySourceEvidence(
                scheme_id="monthly_10y_rf_top5_0629",
                source_role="source_original_monthly_algorithm",
                generator="fake",
                manifest_path=source / "manifest.json",
                source_package_path=source,
                source_package_hash=source_package_tree_sha256(source),
                runner_module="src.monthly.run_monthly_pipeline",
                frequency="M10Y",
                target_tenor="10Y",
                final_select_id="10Y-01",
                model_id="monthly_10y_rf_top5",
                candidate_id="10Y::Random Forest::top5",
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "MONTHLY_SOURCE_CACHE_DIR": str(cache_dir),
                        "BFL_SOURCE_IMMUTABLE_INPUT_TOKEN":
                            "c" * 64,
                    },
                    clear=False,
                ),
                patch(
                    "shared.monthly_source_runner._python_command",
                    return_value=[__import__("sys").executable],
                ),
            ):
                run_source_monthly_live(
                    evidence,
                    predict_date="2026-04-15",
                    database_config=_database_config(),
                )
                runner.write_text(
                    runner.read_text(encoding="utf-8")
                    + "\n# identity drift\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "source package hash differs",
                ):
                    run_source_monthly_live(
                        evidence,
                        predict_date="2026-04-15",
                        database_config=_database_config(),
                    )
