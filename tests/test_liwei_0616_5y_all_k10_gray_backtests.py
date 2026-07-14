from __future__ import annotations

import importlib
import inspect
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from backtests._base_runner import make_run_output


VARIANTS = {
    "liwei_0616_5y_auc_static_all_k3_div_k10": {
        "benchmark_id": "liwei_0616_5y_auc_static_all_k3_div_k10",
        "metric": "auc",
        "rebal": "static",
    },
    "liwei_0616_5y_auc_yearly_all_k3_div_k10": {
        "benchmark_id": "liwei_0616_5y_auc_yearly_all_k3_div_k10",
        "metric": "auc",
        "rebal": "yearly",
    },
    "liwei_0616_5y_ic_yearly_all_k3_div_k10": {
        "benchmark_id": "liwei_0616_5y_ic_yearly_all_k3_div_k10",
        "metric": "ic",
        "rebal": "yearly",
    },
}


def _runner_module(scheme_id: str):
    return importlib.import_module(f"backtests.{scheme_id}_reproduction")


def _artifact(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        path=Path(f"/tmp/{name}.csv"),
        source="framework_db_export",
        data_version="v1",
        content_hash=f"{name}-hash",
    )


class Liwei06165YAllK10GrayBacktestTest(unittest.TestCase):
    def test_canonical_backtest_mode_is_monthly_for_all_public_runners(self) -> None:
        from backtests import liwei_0616_5y_all_k10_reproduction_common as common

        self.assertEqual(common.DEFAULT_BATCH_MODE, "monthly")
        self.assertEqual(
            inspect.signature(common.run_historical_prediction).parameters["batch_mode"].default,
            "monthly",
        )
        for scheme_id in VARIANTS:
            runner = _runner_module(scheme_id)
            run_name = f"run_{scheme_id}_reproduction"
            self.assertEqual(
                inspect.signature(getattr(runner, run_name)).parameters["batch_mode"].default,
                "monthly",
            )

    def test_persist_rejects_noncanonical_daily_mode_before_writing(self) -> None:
        from backtests import liwei_0616_5y_all_k10_reproduction_common as common

        runner = _runner_module(next(iter(VARIANTS)))
        daily_artifact = _artifact("daily")
        daily_artifact.dataframe = pd.DataFrame({"date": pd.to_datetime(["2025-01-01"])})
        weekly_artifact = _artifact("weekly")
        weekly_artifact.dataframe = pd.DataFrame({"week_id": [202501]})
        monthly_artifact = _artifact("monthly")
        monthly_artifact.dataframe = pd.DataFrame({"month_id": ["202412"]})
        calendar = SimpleNamespace(
            week_id_for_date=lambda _day: 202501,
            nth_trading_day_after=lambda _day, _horizon: "2025-01-08",
        )
        engine = SimpleNamespace(dispose=lambda: None)
        deps = common.RuntimeDeps(
            create_engine=lambda: engine,
            get_calendar=lambda **_kwargs: calendar,
            build_daily=lambda **_kwargs: daily_artifact,
            build_weekly=lambda **_kwargs: weekly_artifact,
            build_monthly=lambda **_kwargs: monthly_artifact,
        )
        detail = pd.DataFrame(
            [
                {
                    "anchor_date": "2025-01-01",
                    "prediction": 1,
                    "true_label": 1,
                    "confidence": 1.0,
                    "vote_score": 0.5,
                    "baseline_signs": {name: 1 for name in ("STD", "DIV", "ACCWT", "CROSS_7Y")},
                    "baseline_scores": {name: 0.5 for name in ("STD", "DIV", "ACCWT", "CROSS_7Y")},
                }
            ]
        )

        with (
            patch.object(common, "run_historical_prediction", return_value=detail),
            patch.object(common, "persist_run_output", return_value=1) as persist_output,
            self.assertRaisesRegex(ValueError, "monthly"),
        ):
            common.run_reproduction(
                runner._variant_spec(),
                deps,
                persist=True,
                engine=engine,
                batch_mode="daily",
            )
        persist_output.assert_not_called()

    def test_runner_identity_and_historical_boundaries_are_variant_specific(self) -> None:
        modules = []
        for scheme_id, expected in VARIANTS.items():
            runner = _runner_module(scheme_id)
            modules.append(runner)
            self.assertEqual(runner.SCHEME_ID, scheme_id)
            self.assertEqual(runner.BENCHMARK_ID, expected["benchmark_id"])
            self.assertEqual(runner.TARGET_TENOR, "5Y")
            self.assertEqual(runner.HORIZON, 5)
            self.assertEqual(runner.BACKTEST_START, "2025-01-01")
            self.assertEqual(runner.BACKTEST_END, "2026-05-22")
            self.assertEqual(runner.LIVE_TARGET_CUTOFF, "2026-06-01")
            self.assertEqual(runner.SCREEN_METRIC, expected["metric"])
            self.assertEqual(runner.SCREEN_REBAL, expected["rebal"])

            source = inspect.getsource(runner)
            self.assertIn("shared.input_artifacts", source)
            self.assertIn(f"schemes.{scheme_id}", source)
            for other_scheme_id in VARIANTS:
                if other_scheme_id != scheme_id:
                    self.assertNotIn(f"schemes.{other_scheme_id}", source)
            self.assertNotIn("read_csv(", source)
            self.assertNotIn("repository.upsert", source)

        self.assertEqual(len({module.MODEL_VERSION for module in modules}), 3)
        self.assertEqual(len({module.CACHE_FAMILY for module in modules}), 3)

    def test_rows_use_true_t_plus_five_targets_cut_live_and_exclude_flat_from_accuracy(self) -> None:
        detail = pd.DataFrame(
            [
                {
                    "anchor_date": "2026-05-20",
                    "prediction": 0,
                    "true_label": 1,
                    "confidence": 0.0,
                    "vote_score": 0.01,
                    "baseline_signs": {"STD": 1, "DIV": -1, "ACCWT": 1, "CROSS_7Y": -1},
                    "baseline_scores": {"STD": 0.2, "DIV": -0.1, "ACCWT": 0.3, "CROSS_7Y": -0.2},
                },
                {
                    "anchor_date": "2026-05-21",
                    "prediction": -1,
                    "true_label": -1,
                    "confidence": 1.0,
                    "vote_score": -0.3,
                    "baseline_signs": {"STD": -1, "DIV": -1, "ACCWT": 1, "CROSS_7Y": -1},
                    "baseline_scores": {"STD": -0.2, "DIV": -0.1, "ACCWT": 0.3, "CROSS_7Y": -0.2},
                },
                {
                    "anchor_date": "2026-05-22",
                    "prediction": 1,
                    "true_label": 1,
                    "confidence": 1.0,
                    "vote_score": 0.4,
                    "baseline_signs": {"STD": 1, "DIV": 1, "ACCWT": 1, "CROSS_7Y": -1},
                    "baseline_scores": {"STD": 0.2, "DIV": 0.1, "ACCWT": 0.3, "CROSS_7Y": -0.2},
                },
            ]
        )
        target_dates = {
            "2026-05-20": "2026-05-27",
            "2026-05-21": "2026-05-28",
            "2026-05-22": "2026-06-01",
        }
        for scheme_id in VARIANTS:
            runner = _runner_module(scheme_id)
            rows = runner.build_backtest_rows(
                detail,
                target_date_for_anchor=target_dates.get,
                daily_artifact=_artifact("daily"),
                weekly_artifact=_artifact("weekly"),
                monthly_artifact=_artifact("monthly"),
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["predict_date"], "2026-05-20")
            self.assertEqual(rows[0]["feature_date"], "2026-05-20")
            self.assertEqual(rows[0]["target_date"], "2026-05-27")
            self.assertTrue(all(row["target_date"] < runner.LIVE_TARGET_CUTOFF for row in rows))

            output = make_run_output(
                scheme_id=runner.SCHEME_ID,
                data_source=runner.DATA_SOURCE,
                start_date="2026-05-20",
                end_date="2026-05-21",
                rows=rows,
                benchmark_id=runner.BENCHMARK_ID,
            )
            metrics = output.summary["by_tenor"]["5Y"]
            self.assertEqual(int(metrics["samples"]), 2)
            self.assertEqual(int(metrics["metric_samples"]), 1)
            self.assertEqual(int(metrics["correct"]), 1)

    def test_monthly_sample_preserves_continuous_source_context_from_2024(self) -> None:
        for scheme_id in VARIANTS:
            runner = _runner_module(scheme_id)
            fake_detail = pd.DataFrame(
                [
                    {
                        "anchor_date": "2026-05-18",
                        "prediction": 1,
                        "true_label": 1,
                        "confidence": 1.0,
                    },
                    {
                        "anchor_date": "2026-05-22",
                        "prediction": -1,
                        "true_label": -1,
                        "confidence": 1.0,
                    },
                ]
            )
            with patch.object(runner, "run_for_window_silent", return_value=fake_detail) as run_window:
                rows = runner._run_monthly_batch_group(
                    group_dates=["2026-05-18", "2026-05-22"],
                    daily_df=pd.DataFrame(),
                    weekly_df=pd.DataFrame(),
                    monthly_df=pd.DataFrame(),
                    date_to_week={},
                    n_workers=1,
                    model_context_end="2026-05-29",
                    source_end="2026-05-29",
                    cache_dir=None,
                    cache_key_parts={},
                )
            self.assertEqual(len(rows), 2)
            kwargs = run_window.call_args.kwargs
            self.assertEqual(kwargs["test_ranges"], (("2024-01-01", "2026-05-29"),))
            self.assertEqual(kwargs["current_start"], "2026-05-18")
            self.assertEqual(kwargs["current_end"], "2026-05-22")

    def test_targeted_sample_expands_source_and_input_to_db_target_month_end(self) -> None:
        from backtests import liwei_0616_5y_all_k10_reproduction_common as common

        self.assertIn(
            "target_month_end_for_target",
            inspect.signature(common._monthly_source_groups).parameters,
        )
        self.assertIn(
            "target_month_end_for_target",
            inspect.signature(common._effective_input_end).parameters,
        )
        target_dates = {"2026-03-25": "2026-04-01"}
        resolved_targets: list[str] = []

        def resolve_month_end(target_date: str) -> str:
            resolved_targets.append(target_date)
            return "2026-04-30"

        groups = common._monthly_source_groups(
            dates=list(target_dates),
            model_context_end="2026-04-30",
            target_date_for_anchor=target_dates.get,
            target_month_end_for_target=resolve_month_end,
            require_target_month_end=True,
        )
        calendar = SimpleNamespace(
            nth_trading_day_after=lambda day, _horizon: target_dates[day],
        )
        effective_end = common._effective_input_end(
            list(target_dates),
            calendar,
            target_month_end_for_target=resolve_month_end,
        )

        self.assertEqual(groups, [{"dates": ["2026-03-25"], "source_end": "2026-04-30"}])
        self.assertEqual(effective_end, "2026-04-30")
        self.assertEqual(set(resolved_targets), {"2026-04-01"})

    def test_targeted_sample_fails_closed_without_reliable_db_month_end(self) -> None:
        from backtests import liwei_0616_5y_all_k10_reproduction_common as common

        self.assertIn(
            "require_target_month_end",
            inspect.signature(common._monthly_source_groups).parameters,
        )
        with self.assertRaisesRegex(RuntimeError, "target month end"):
            common._monthly_source_groups(
                dates=["2026-03-25"],
                model_context_end="2026-04-30",
                target_date_for_anchor=lambda _day: "2026-04-01",
                target_month_end_for_target=None,
                require_target_month_end=True,
            )
        with self.assertRaisesRegex(RuntimeError, "target month end"):
            common._monthly_source_groups(
                dates=["2026-03-25"],
                model_context_end="2026-04-30",
                target_date_for_anchor=lambda _day: "2026-04-01",
                target_month_end_for_target=lambda _target: None,
                require_target_month_end=True,
            )

    def test_compact_rows_are_compare_gate_strict_and_keep_internal_fields(self) -> None:
        from backtests import liwei_0616_5y_all_k10_reproduction_common as common

        row = {
            "predict_date": "2026-03-25",
            "feature_date": "2026-03-25",
            "target_date": "2026-04-01",
            "target_tenor": "5Y",
            "horizon": 5,
            "predicted_direction": 0,
            "label": -1,
            "confidence": 0.0,
            "extra": {
                "vote_score": -0.1,
                "baseline_scores": {
                    "STD": 0.1,
                    "DIV": -0.2,
                    "ACCWT": 0.3,
                    "CROSS_7Y": -0.4,
                },
                "baseline_signs": {"STD": 1, "DIV": -1, "ACCWT": 1, "CROSS_7Y": -1},
            },
        }
        compact = common.compact_prediction_rows([row])[0]

        required = {"target_tenor", "horizon", "benchmark_role", "is_correct"}
        self.assertFalse(required - set(compact), f"missing strict fields: {sorted(required - set(compact))}")
        self.assertEqual(compact["target_tenor"], "5Y")
        self.assertEqual(compact["horizon"], 5)
        self.assertEqual(compact["benchmark_role"], "historical/platform-live-pit-variant")
        self.assertIs(compact["is_correct"], False)
        self.assertEqual(compact["direction"], 0)
        self.assertEqual(compact["label"], -1)
        self.assertEqual(compact["vote_score"], -0.1)
        self.assertEqual(compact["CROSS_7Y_score"], -0.4)
        self.assertEqual(compact["CROSS_7Y_dir"], -1)

    def test_cache_keys_include_variant_algorithm_code_and_input_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_root = Path(tmpdir)
            paths = []
            for scheme_id in VARIANTS:
                runner = _runner_module(scheme_id)
                base_parts = {
                    "daily_input_artifact_hash": "daily-a",
                    "weekly_input_artifact_hash": "weekly-a",
                    "monthly_input_artifact_hash": "monthly-a",
                }
                path = runner._cache_path(
                    cache_root,
                    "2026-05-22",
                    base_parts,
                    cache_mode="monthly",
                    window_end="2026-05-22",
                    model_context_end="2026-05-29",
                )
                changed_input_path = runner._cache_path(
                    cache_root,
                    "2026-05-22",
                    {**base_parts, "daily_input_artifact_hash": "daily-b"},
                    cache_mode="monthly",
                    window_end="2026-05-22",
                    model_context_end="2026-05-29",
                )
                self.assertIsNotNone(path)
                self.assertNotEqual(path, changed_input_path)
                paths.append(path)
            self.assertEqual(len(set(paths)), 3)

    def test_cache_identity_covers_alignment_week_mapping_and_runtime_environment(self) -> None:
        from backtests import liwei_0616_5y_all_k10_reproduction_common as common

        self.assertTrue(hasattr(common, "_date_to_week_fingerprint"))
        self.assertTrue(hasattr(common, "_runtime_environment"))
        runner = _runner_module(next(iter(VARIANTS)))
        spec = runner._variant_spec()
        visited: list[Path] = []
        original_read_bytes = Path.read_bytes

        def tracked_read_bytes(path: Path) -> bytes:
            visited.append(path)
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", tracked_read_bytes):
            common._code_fingerprint(spec)
        self.assertTrue(any(path.name == "data_alignment.py" for path in visited))

        first_mapping = common._date_to_week_fingerprint({"2026-03-25": 202613})
        second_mapping = common._date_to_week_fingerprint({"2026-03-25": 202614})
        self.assertNotEqual(first_mapping, second_mapping)

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_root = Path(tmpdir)
            parts = {
                "daily_input_artifact_hash": "daily-a",
                "weekly_input_artifact_hash": "weekly-a",
                "monthly_input_artifact_hash": "monthly-a",
                "date_to_week_fingerprint": first_mapping,
            }
            with patch.object(
                common,
                "_runtime_environment",
                return_value={
                    "python": "3.12-a",
                    "pandas": "a",
                    "scikit-learn": "a",
                    "lightgbm": "a",
                },
            ):
                first_path = common.cache_path(
                    spec,
                    cache_root,
                    "2026-03-25",
                    parts,
                    cache_mode="monthly",
                    window_end="2026-04-23",
                    model_context_end="2026-04-30",
                )
            with patch.object(
                common,
                "_runtime_environment",
                return_value={
                    "python": "3.12-b",
                    "pandas": "b",
                    "scikit-learn": "b",
                    "lightgbm": "b",
                },
            ):
                second_path = common.cache_path(
                    spec,
                    cache_root,
                    "2026-03-25",
                    parts,
                    cache_mode="monthly",
                    window_end="2026-04-23",
                    model_context_end="2026-04-30",
                )
        self.assertNotEqual(first_path, second_path)

        fake_run_window = MagicMock(
            return_value=pd.DataFrame(
                [{"anchor_date": "2026-03-25", "prediction": 1, "true_label": 1}]
            )
        )
        lightweight_spec = replace(spec, run_window=fake_run_window)
        with patch.object(common, "cache_path", return_value=None) as cache_path_call:
            common.run_historical_prediction(
                lightweight_spec,
                daily_df=pd.DataFrame({"date": pd.to_datetime(["2026-03-25"])}),
                weekly_df=pd.DataFrame(),
                monthly_df=pd.DataFrame(),
                date_to_week={"2026-03-25": 202613},
                feature_dates=["2026-03-25"],
                batch_mode="daily",
            )
        cache_parts = cache_path_call.call_args.args[3]
        self.assertEqual(cache_parts["date_to_week_fingerprint"], first_mapping)

    def test_cli_exposes_harness_no_persist_and_requested_execution_controls(self) -> None:
        for scheme_id in VARIANTS:
            runner = _runner_module(scheme_id)
            run_name = f"run_{scheme_id}_reproduction"
            self.assertTrue(callable(getattr(runner, run_name)))
            source = inspect.getsource(runner.main)
            for option in ("--no-persist", "--batch-mode", "--n-workers", "--phase-a-cache"):
                self.assertIn(option, source)


if __name__ == "__main__":
    unittest.main()
