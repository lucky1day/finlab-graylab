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


class Daily0629SourceEvidenceTests(unittest.TestCase):
    def test_private_source_copy_must_match_frozen_package_hash(
        self,
    ) -> None:
        from shared.daily_0629_source_runner import _source_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            source.mkdir()
            (source / "runner.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "copied source package hash",
            ):
                with _source_runtime(source, "0" * 64):
                    self.fail(
                        "mismatched private source copy was accepted"
                    )

    def test_partial_scheduled_live_compatibility_environment_fails_closed(
        self,
    ) -> None:
        from shared import input_artifacts
        from shared.daily_0629_predict_adapter import (
            run_daily_0629_prediction,
        )

        with (
            patch.dict(
                os.environ,
                {
                    input_artifacts.LIVE_SOURCE_INPUT_MODE_ENV:
                        "live_source_0629",
                },
                clear=True,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "partial daily 0629 live source",
            ),
        ):
            run_daily_0629_prediction(
                "daily_1y_xgb_1y13_0629",
                "2026-07-24",
            )

    def test_scheduled_live_rejects_source_package_identity_drift(
        self,
    ) -> None:
        from types import SimpleNamespace

        from shared import input_artifacts
        from shared.daily_0629_predict_adapter import (
            run_daily_0629_prediction,
        )

        environment = {
            input_artifacts.LIVE_SOURCE_INPUT_MODE_ENV:
                "live_source_0629",
            input_artifacts.LIVE_SOURCE_FENCE_GENERATION_ID_ENV:
                "native-20260724",
            input_artifacts.LIVE_SOURCE_FEATURE_DATE_ENV:
                "2026-07-23",
            input_artifacts.LIVE_SOURCE_PACKAGE_SHA256_ENV:
                "b" * 64,
        }
        evidence = SimpleNamespace(source_package_hash="a" * 64)
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "shared.daily_0629_predict_adapter."
                "require_daily_0629_source_evidence",
                return_value=evidence,
            ),
            patch(
                "shared.daily_0629_predict_adapter."
                "run_source_daily_live",
            ) as source_runner,
            self.assertRaisesRegex(
                RuntimeError,
                "differs from frozen occurrence policy",
            ),
        ):
            run_daily_0629_prediction(
                "daily_1y_xgb_1y13_0629",
                "2026-07-24",
            )

        source_runner.assert_not_called()

    def test_scheduled_live_compatibility_records_input_watermark(
        self,
    ) -> None:
        from types import SimpleNamespace

        from shared import input_artifacts
        from shared.daily_0629_predict_adapter import (
            run_daily_0629_prediction,
        )

        evidence = SimpleNamespace(
            frequency="D1Y",
            target_tenor="1Y",
            source_package_hash="a" * 64,
            model_id="model",
            final_select_id="1Y13",
        )
        source = {
            "frequency": "D1Y",
            "pred_label": 1,
            "prediction_date": "2026-07-23",
            "rdate": "2026-07-27",
        }
        artifact = SimpleNamespace(
            path=Path("/tmp/daily-input.csv"),
            source="shared_data_service_daily",
            source_watermark="2026-07-23",
        )
        calendar = object()
        engine = SimpleNamespace(dispose=lambda: None)
        context = SimpleNamespace(
            feature_date="2026-07-23",
            target_date="2026-07-27",
        )
        events: list[str] = []

        def build_artifact(*args, **kwargs):
            del args, kwargs
            events.append("artifact")
            return artifact

        def run_source(*args, **kwargs):
            del args, kwargs
            events.append("source")
            return [source]

        environment = {
            input_artifacts.LIVE_SOURCE_INPUT_MODE_ENV:
                "live_source_0629",
            input_artifacts.LIVE_SOURCE_FENCE_GENERATION_ID_ENV:
                "native-20260724",
            input_artifacts.LIVE_SOURCE_FEATURE_DATE_ENV:
                "2026-07-23",
            input_artifacts.LIVE_SOURCE_PACKAGE_SHA256_ENV:
                "a" * 64,
        }
        database_config = _database_config()
        with (
            patch.dict(os.environ, environment, clear=False),
            patch(
                "shared.daily_0629_predict_adapter."
                "require_daily_0629_source_evidence",
                return_value=evidence,
            ),
            patch(
                "shared.daily_0629_predict_adapter."
                "run_source_daily_live",
                side_effect=run_source,
            ) as source_runner,
            patch(
                "shared.daily_0629_predict_adapter."
                "create_input_engine",
                return_value=engine,
            ) as create_engine,
            patch(
                "shared.daily_0629_predict_adapter.get_calendar",
                return_value=calendar,
            ),
            patch(
                "shared.daily_0629_predict_adapter."
                "build_daily_live_context",
                return_value=context,
            ),
            patch(
                "shared.daily_0629_predict_adapter."
                "build_daily_input_artifact",
                side_effect=build_artifact,
            ),
            patch(
                "shared.daily_0629_predict_adapter."
                "load_source_runtime_database_config",
                return_value=database_config,
            ),
        ):
            records = run_daily_0629_prediction(
                "daily_1y_xgb_1y13_0629",
                "2026-07-24",
            )

        extra = records[0].extra or {}
        self.assertEqual(extra["input_mode"], "live_source_0629")
        self.assertEqual(extra["data_watermark"], "2026-07-23")
        self.assertEqual(
            extra["data_watermark_basis"],
            "source_output_prediction_date",
        )
        self.assertEqual(
            extra["live_source_fence_generation_id"],
            "native-20260724",
        )
        self.assertEqual(extra["source_package_hash"], "a" * 64)
        self.assertEqual(events, ["artifact", "source"])
        self.assertIs(
            source_runner.call_args.kwargs["database_config"],
            database_config,
        )
        self.assertIs(
            create_engine.call_args.kwargs["database_config"],
            database_config,
        )

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
                      "source_package_sha256": "d3d6784a258eb3f7e26468d16153a6d3b83cff73c8085ccf52287ed9c7cad7a6",
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
                    2026-06-10,D1Y,1Y13,1y_xgb,2026-06-09,2026-06-09,True,0,平,0.50
                    CSV
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            (source / "daily_project" / "src" / "run_daily.sh").chmod(0o755)
            (source / "db_config.py").write_text(
                "DB_CONFIG = {'password': 'packaged-must-not-run'}\n",
                encoding="utf-8",
            )
            from shared.daily_0629_source_evidence import (
                source_package_tree_sha256,
            )

            evidence = Daily0629SourceEvidence(
                scheme_id="daily_1y_xgb_1y13_0629",
                source_role="source_original_daily_0629_algorithm",
                generator="fake",
                manifest_path=source / "manifest.json",
                source_package_path=source,
                source_package_hash=source_package_tree_sha256(source),
                runner_module="daily.run_backtest",
                live_runner_module="daily.run_daily",
                frequency="D1Y",
                target_tenor="1Y",
                final_select_id="1Y13",
                candidate_id="1y_xgb",
                target_col="TB1YWI0C",
                model_id="1y_xgb",
            )

            with (
                patch.dict(
                    os.environ,
                    {"DAILY_0629_SOURCE_CACHE_DISABLE": "1"},
                    clear=False,
                ),
                patch(
                    "shared.daily_0629_source_runner._python_command",
                    return_value=[__import__("sys").executable],
                ),
            ):
                rows = run_source_daily_live(
                    evidence,
                    predict_date="2026-06-10",
                    database_config=_database_config(),
                )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["frequency"], "D1Y")
        self.assertEqual(rows[0]["source_output_date"], "2026-06-10")
        self.assertEqual(rows[0]["pred_label"], "0")

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
