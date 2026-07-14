from __future__ import annotations

import importlib.util
import inspect
import os
import pickle
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "build_liwei_5y_all_k10_gray_benchmarks.py"


def _load_builder():
    if not SCRIPT_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("build_liwei_5y_all_k10_gray_benchmarks", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


class _FakeCalendar:
    def __init__(self, target_dates: dict[str, str]) -> None:
        self.target_dates = target_dates

    def nth_trading_day_after(self, feature_date: str, horizon: int) -> str:
        if horizon != 5:
            raise AssertionError(f"unexpected horizon: {horizon}")
        return self.target_dates[feature_date]


def _source_contexts(dates: list[str]) -> dict[str, dict]:
    n_rows = len(dates)
    base = np.arange(1, n_rows + 1, dtype=float) / 10.0
    scores = {
        "STD": base,
        "DIV": np.full(n_rows, -0.2),
        "ACCWT": base + 0.1,
        "CROSS_7Y": base + 0.2,
    }
    contexts = {}
    for baseline, values in scores.items():
        preds = np.vstack([np.ones(n_rows), -np.ones(n_rows)]).astype(np.int32)
        probs = np.vstack([np.full(n_rows, 0.7), np.full(n_rows, 0.3)])
        contexts[baseline] = {
            "final": np.sign(values).astype(np.int32),
            "vs_full": values,
            "true_labels": np.ones(n_rows, dtype=np.int32),
            "test_dates": pd.DatetimeIndex(pd.to_datetime(dates)),
            "lgbm_cache": {
                "test_dates": np.asarray(pd.to_datetime(dates), dtype="datetime64[ns]"),
                "preds": preds,
                "probs": probs,
                "valid": np.asarray([True, True]),
                "n_configs": 2,
            },
        }
    return contexts


@unittest.skipIf(builder is None, "benchmark builder is not implemented yet")
class BuildLiwei5YAllK10GrayBenchmarksTest(unittest.TestCase):
    def test_consensus_then_div_streak_break_matches_all_k10(self) -> None:
        signs = {
            "STD": np.ones(12, dtype=np.int32),
            "DIV": -np.ones(12, dtype=np.int32),
            "ACCWT": np.ones(12, dtype=np.int32),
            "CROSS_7Y": np.ones(12, dtype=np.int32),
        }

        actual = builder.build_all_k10_prediction(signs)

        expected = np.ones(12, dtype=np.int32)
        expected[10] = -1
        np.testing.assert_array_equal(actual, expected)

    def test_source_lgbm_cache_is_strictly_converted_to_phase_a_cache(self) -> None:
        source = {
            "test_dates": np.asarray(["2026-05-06", "2026-05-07"], dtype="datetime64[D]"),
            "preds": np.asarray([[1, -1], [-1, -1], [1, 1]], dtype=np.int32),
            "probs": np.asarray([[0.7, 0.2], [0.1, 0.4], [0.8, 0.9]], dtype=float),
            "valid": np.asarray([True, False, True]),
            "n_configs": 3,
        }
        grid = [{"window": 200}, {"window": 350}, {"window": 504}]

        phase_a = builder.convert_source_lgbm_cache(source, grid)

        self.assertEqual(phase_a["test_dates"], ["2026-05-06", "2026-05-07"])
        self.assertEqual([item["config"] for item in phase_a["results"]], [grid[0], grid[2]])
        np.testing.assert_array_equal(phase_a["results"][1]["preds"], [1, 1])
        np.testing.assert_allclose(phase_a["results"][0]["probs"], [0.7, 0.2])

        with self.assertRaisesRegex(RuntimeError, "grid size"):
            builder.convert_source_lgbm_cache(source, grid[:2])

    def test_source_context_accepts_zero_actual_direction(self) -> None:
        contexts = _source_contexts(["2026-05-20", "2026-05-21"])
        contexts["STD"]["true_labels"] = np.asarray([0, 1], dtype=np.int32)

        _, labels, _ = builder._validated_source_series(contexts)

        np.testing.assert_array_equal(labels, [0, 1])

    def test_original_rows_use_true_t_plus_five_and_cut_gray_target_dates(self) -> None:
        dates = ["2026-05-20", "2026-05-21", "2026-05-22"]
        contexts = _source_contexts(dates)
        calendar = _FakeCalendar(
            {
                "2026-05-20": "2026-05-27",
                "2026-05-21": "2026-05-28",
                "2026-05-22": "2026-06-01",
            }
        )

        rows = builder.build_original_rows(
            contexts,
            calendar=calendar,
            feature_start="2026-05-20",
            feature_end="2026-05-22",
            target_cutoff="2026-06-01",
        )

        self.assertEqual(rows["feature_date"].tolist(), ["2026-05-20", "2026-05-21"])
        self.assertEqual(rows["target_date"].tolist(), ["2026-05-27", "2026-05-28"])
        self.assertTrue((rows["benchmark_role"] == "historical/source-configured-experiment").all())
        self.assertEqual(list(rows.columns), list(builder.BENCHMARK_COLUMNS))

    def test_bundle_reexecutes_platform_core_and_writes_strict_provenance(self) -> None:
        dates = ["2026-05-20", "2026-05-21"]
        contexts = _source_contexts(dates)
        calendar = _FakeCalendar(
            {"2026-05-20": "2026-05-27", "2026-05-21": "2026-05-28"}
        )
        calls: list[dict] = []

        class FakeCore:
            PROD_CONFIG = {
                "baselines": ["STD", "DIV", "ACCWT", "CROSS_7Y"],
                "k_agree": 3,
                "fallback": "DIV",
                "streak_K": 10,
            }

            @staticmethod
            def model_config(_baseline: str) -> dict:
                return {
                    "lgbm_windows": [200],
                    "lgbm_leaves": [3],
                    "lgbm_min_child": [10],
                    "lgbm_alpha": [0.0],
                    "lgbm_lambda": [0.5],
                    "lgbm_split": [0.55],
                    "lgbm_slow_path": True,
                }

            @staticmethod
            def build_lgbm_grid(*_args, **_kwargs) -> list[dict]:
                return [{"window": 200}, {"window": 350}]

            @staticmethod
            def run_5y_all_for_feature_window(**kwargs) -> pd.DataFrame:
                calls.append(kwargs)
                return pd.DataFrame(
                    {
                        "anchor_date": dates,
                        "prediction": [1, 1],
                        "confidence": [1.0, 1.0],
                        "true_label": [1, 1],
                        "vote_score": [0.1, 0.175],
                        "baseline_scores": [
                            {"STD": 0.1, "DIV": -0.2, "ACCWT": 0.2, "CROSS_7Y": 0.3},
                            {"STD": 0.2, "DIV": -0.2, "ACCWT": 0.3, "CROSS_7Y": 0.4},
                        ],
                        "baseline_signs": [
                            {"STD": 1, "DIV": -1, "ACCWT": 1, "CROSS_7Y": 1},
                            {"STD": 1, "DIV": -1, "ACCWT": 1, "CROSS_7Y": 1},
                        ],
                    }
                )

        artifact = SimpleNamespace(
            dataframe=pd.DataFrame({"date": pd.to_datetime(dates)}),
            path=Path("/tmp/framework-input.csv"),
            content_hash="artifact-hash",
            source="shared_data_service_daily",
            data_version="shared_data_service_daily.v1",
        )
        artifacts = builder.ArtifactBundle(
            daily=artifact,
            weekly=artifact,
            monthly=artifact,
            date_to_week={date: 202620 for date in dates},
        )
        spec = builder.VariantSpec(
            scheme_id="liwei_test_5y_auc_yearly_all_k3_div_k10",
            metric="auc",
            rebal="yearly",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            source_group = Path(tmpdir) / "source"
            source_group.mkdir()
            for baseline, context in contexts.items():
                with (source_group / f"{baseline}.pkl").open("wb") as handle:
                    pickle.dump(context, handle)
            output_dir = Path(tmpdir) / "benchmarks"

            result = builder.build_variant_from_source(
                spec,
                source_group=source_group,
                output_dir=output_dir,
                artifacts=artifacts,
                calendar=calendar,
                core_module=FakeCore,
                feature_start="2026-05-20",
                feature_end="2026-05-21",
                target_cutoff="2026-06-01",
            )

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["test_ranges"], (("2026-05-20", "2026-05-21"),))
            self.assertEqual(set(calls[0]["phase_a_caches"]), set(builder.BASELINES))
            self.assertEqual(result["row_count"], 2)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {
                    "original_predictions_sample.csv",
                    "current_predictions_sample.csv",
                    "original_backtest_summary.json",
                    "current_backtest_summary.json",
                    "README.md",
                },
            )
            original_summary = builder.read_json(output_dir / "original_backtest_summary.json")
            current_summary = builder.read_json(output_dir / "current_backtest_summary.json")
            self.assertEqual(original_summary["benchmark_role"], "historical/source-configured-experiment")
            self.assertEqual(original_summary["model_scope"], "experimental/platform_live_pit_variant")
            self.assertIn("wf_ic", original_summary["benchmark_provenance"]["yearly_wiring_note"])
            self.assertEqual(
                current_summary["benchmark_provenance"]["source_role"],
                "platform_core_reexecution_from_source_phase_a_cache",
            )
            self.assertEqual(set(original_summary["source_cache_fingerprints"]), set(builder.BASELINES))
            self.assertEqual(original_summary["flat_accuracy_policy"], "prediction == 0 excluded")

    def test_bundle_fails_closed_when_platform_core_diff_exceeds_tolerance(self) -> None:
        original = pd.DataFrame(
            [
                {
                    "feature_date": "2026-05-20",
                    "target_date": "2026-05-27",
                    "target_tenor": "5Y",
                    "horizon": 5,
                    "benchmark_role": "historical/source-configured-experiment",
                    "direction": 1,
                    "confidence": 1.0,
                    "label": 1,
                    "is_correct": True,
                    "vote_score": 0.1,
                    "STD_score": 0.2,
                    "STD_dir": 1,
                    "DIV_score": -0.2,
                    "DIV_dir": -1,
                    "ACCWT_score": 0.3,
                    "ACCWT_dir": 1,
                    "CROSS_7Y_score": 0.4,
                    "CROSS_7Y_dir": 1,
                }
            ]
        )
        current = original.copy()
        current.loc[0, "STD_score"] += 2e-8

        with self.assertRaisesRegex(RuntimeError, "STD_score"):
            builder.assert_exact_rows(original, current, tolerance=1e-8)

    def test_script_uses_shared_input_artifacts_for_model_inputs(self) -> None:
        source = inspect.getsource(builder)
        self.assertIn("from shared.input_artifacts import", source)
        for name in (
            "build_daily_input_artifact",
            "build_weekly_input_artifact",
            "build_monthly_input_artifact",
        ):
            self.assertIn(name, source)
        self.assertNotIn("pd.read_csv(", source)

    def test_direct_cli_bootstraps_project_import_path(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            cwd="/tmp",
            env={**os.environ, "PYTHONPATH": ""},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class BuildLiwei5YAllK10GrayBenchmarksRedTest(unittest.TestCase):
    def test_builder_script_exists(self) -> None:
        self.assertIsNotNone(builder, f"missing benchmark builder: {SCRIPT_PATH}")


if __name__ == "__main__":
    unittest.main()
