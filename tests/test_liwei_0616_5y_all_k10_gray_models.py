"""Liwei 5Y ALL_K10 AUC/IC 灰度方案的聚焦契约测试。"""

from __future__ import annotations

import importlib
import inspect
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINES = ["STD", "DIV", "ACCWT", "CROSS_7Y"]
CASES = (
    (
        "liwei_0616_5y_auc_static_all_k3_div_k10",
        "auc",
        "static",
    ),
    (
        "liwei_0616_5y_auc_yearly_all_k3_div_k10",
        "auc",
        "yearly",
    ),
    (
        "liwei_0616_5y_ic_yearly_all_k3_div_k10",
        "ic",
        "yearly",
    ),
)


def _load_config(scheme_id: str) -> dict:
    from harness.config_loader import load_config_raw

    return load_config_raw(PROJECT_ROOT / "schemes" / scheme_id / "config.yaml")


class FiveYearAllK10ConfigTests(unittest.TestCase):
    def test_each_gray_scheme_is_an_independent_active_5y_t_plus_5_model(self) -> None:
        benchmark_ids: list[str] = []
        runner_names: list[str] = []

        for scheme_id, metric, rebal in CASES:
            with self.subTest(scheme_id=scheme_id):
                config = _load_config(scheme_id)

                self.assertEqual(config["scheme_id"], scheme_id)
                self.assertEqual(config["tenors"], ["5Y"])
                self.assertEqual(config["horizon"], 5)
                self.assertEqual(config["task_type"], "T+5")
                self.assertEqual(config["frequency"], "daily")
                self.assertEqual(config["status"], "active")
                self.assertEqual(config["schedule"]["cron"], "3 7 * * 1-5")
                self.assertEqual(config["schedule"]["timezone"], "Asia/Shanghai")
                self.assertEqual(config["schedule"]["timeout_sec"], 3600)
                self.assertIn("TB5YWI0C", config["input_spec"]["required_columns"])

                description = config["description"].lower()
                self.assertIn(metric, description)
                self.assertIn(rebal, description)
                self.assertIn("all_k10", description)

                runner_name = config["backtest"]["runner"]
                benchmark_id = config["backtest"]["benchmark_id"]
                self.assertEqual(
                    runner_name,
                    f"backtests.{scheme_id}_reproduction",
                )
                self.assertTrue(config["backtest"]["benchmark_required"])
                self.assertEqual(
                    config["backtest"]["required_internal_fields"],
                    [
                        "vote_score",
                        "STD_score",
                        "STD_dir",
                        "DIV_score",
                        "DIV_dir",
                        "ACCWT_score",
                        "ACCWT_dir",
                        "CROSS_7Y_score",
                        "CROSS_7Y_dir",
                    ],
                )
                self.assertNotIn(runner_name, runner_names)
                self.assertNotIn(benchmark_id, benchmark_ids)
                runner_names.append(runner_name)
                benchmark_ids.append(benchmark_id)

class FiveYearAllK10CoreAuditTests(unittest.TestCase):
    def test_yearly_screen_anchor_is_stable_for_singleton_cache_extension(self) -> None:
        daily_dates = pd.Series(pd.bdate_range("2024-01-01", "2026-07-10"))
        full_year_rows = np.flatnonzero(
            (daily_dates >= pd.Timestamp("2026-01-01")).to_numpy()
        )
        singleton_rows = np.flatnonzero(
            (daily_dates == pd.Timestamp("2026-07-10")).to_numpy()
        )
        period = pd.Period("2026", freq="Y")

        for scheme_id, _metric, rebal in CASES[1:]:
            with self.subTest(scheme_id=scheme_id):
                core = importlib.import_module(
                    f"schemes.{scheme_id}.core.v31_common"
                )
                cold_anchor = core._wf_screen_first_row(
                    wf_rebal=rebal,
                    period=period,
                    daily_dates=daily_dates,
                    period_test_rows=full_year_rows,
                    yearly_eligible_rows=full_year_rows,
                )
                extend_anchor = core._wf_screen_first_row(
                    wf_rebal=rebal,
                    period=period,
                    daily_dates=daily_dates,
                    period_test_rows=singleton_rows,
                    yearly_eligible_rows=full_year_rows,
                )

                self.assertEqual(cold_anchor, extend_anchor)
                self.assertEqual(
                    daily_dates.iloc[cold_anchor].strftime("%Y-%m-%d"),
                    "2026-01-01",
                )
                self.assertEqual(
                    daily_dates.iloc[cold_anchor - core.HORIZON].strftime(
                        "%Y-%m-%d"
                    ),
                    daily_dates.iloc[extend_anchor - core.HORIZON].strftime(
                        "%Y-%m-%d"
                    ),
                )

    def test_yearly_screen_anchor_uses_first_eligible_row_in_period(self) -> None:
        daily_dates = pd.Series(
            pd.to_datetime(
                [
                    "2025-12-25",
                    "2025-12-26",
                    "2026-01-01",
                    "2026-01-02",
                    "2026-07-10",
                ]
            )
        )
        full_year_eligible_rows = np.asarray([3, 4], dtype=np.int64)
        cold_period_rows = np.asarray([3, 4], dtype=np.int64)
        singleton_period_rows = np.asarray([4], dtype=np.int64)
        period = pd.Period("2026", freq="Y")

        for scheme_id, _metric, rebal in CASES[1:]:
            with self.subTest(scheme_id=scheme_id):
                core = importlib.import_module(
                    f"schemes.{scheme_id}.core.v31_common"
                )
                cold_anchor = core._wf_screen_first_row(
                    wf_rebal=rebal,
                    period=period,
                    daily_dates=daily_dates,
                    period_test_rows=cold_period_rows,
                    yearly_eligible_rows=full_year_eligible_rows,
                )
                extend_anchor = core._wf_screen_first_row(
                    wf_rebal=rebal,
                    period=period,
                    daily_dates=daily_dates,
                    period_test_rows=singleton_period_rows,
                    yearly_eligible_rows=full_year_eligible_rows,
                )

                self.assertEqual(cold_anchor, 3)
                self.assertEqual(extend_anchor, cold_anchor)
                self.assertEqual(
                    daily_dates.iloc[cold_anchor].strftime("%Y-%m-%d"),
                    "2026-01-02",
                )

    def test_run_prediction_passes_full_year_eligibility_to_anchor_helper(self) -> None:
        for scheme_id, _metric, _rebal in CASES[1:]:
            with self.subTest(scheme_id=scheme_id):
                core = importlib.import_module(
                    f"schemes.{scheme_id}.core.v31_common"
                )
                source = inspect.getsource(core.run_prediction)

                self.assertIn("yearly_eligible_rows = np.flatnonzero(", source)
                self.assertIn("yearly_eligible_rows=yearly_eligible_rows", source)

    def test_core_constants_and_model_config_expose_exact_screening_variant(self) -> None:
        for scheme_id, metric, rebal in CASES:
            with self.subTest(scheme_id=scheme_id):
                core = importlib.import_module(
                    f"schemes.{scheme_id}.core.v31_common"
                )

                self.assertEqual(core.TARGET_TENOR, "5Y")
                self.assertEqual(core.SCREEN_METRIC, metric)
                self.assertEqual(core.SCREEN_REBAL, rebal)
                self.assertEqual(core.PROD_CONFIG["baselines"], BASELINES)
                self.assertEqual(core.PROD_CONFIG["k_agree"], 3)
                self.assertEqual(core.PROD_CONFIG["fallback"], "DIV")
                self.assertEqual(core.PROD_CONFIG["streak_K"], 10)

                for baseline in BASELINES:
                    with self.subTest(scheme_id=scheme_id, baseline=baseline):
                        model_config = core.model_config(baseline)
                        expected_tenor = "7Y" if baseline == "CROSS_7Y" else "5Y"
                        expected_close = (
                            "TB7YWI0C" if baseline == "CROSS_7Y" else "TB5YWI0C"
                        )
                        self.assertEqual(model_config["tenor"], expected_tenor)
                        self.assertEqual(model_config["close"], expected_close)
                        self.assertEqual(model_config["screen_metric"], metric)
                        self.assertEqual(model_config["screen_rebal"], rebal)
                        if rebal == "yearly":
                            self.assertEqual(
                                model_config["wf_ic"],
                                {"rebal": "yearly", "cap": 2000},
                            )
                        else:
                            self.assertNotIn("wf_ic", model_config)

                cross_config = core.model_config("CROSS_7Y")
                self.assertEqual(cross_config["name"], "5y_7y_cross")
                self.assertEqual(cross_config["self_name"], "7Y")
                self.assertEqual(
                    cross_config["aux_pairs"],
                    [("5Y", "TB5YWI0C"), ("10Y", "TB0YWI0C")],
                )
                self.assertEqual(cross_config["seeds"], [42, 314])
                self.assertEqual(cross_config["K"], 15)
                self.assertEqual(
                    cross_config["combo_template"],
                    {"butterfly": 3, "term_prem": 2},
                )
                self.assertEqual(cross_config["lgbm_w"], 1.5)
                self.assertEqual(cross_config["rebal"], "quarterly")
                self.assertEqual(cross_config["ew"], 252)
                self.assertEqual(cross_config["min_acc"], 0.45)
                self.assertEqual(cross_config["ml_mode"], "prob")
                self.assertEqual(cross_config["ens_mode"], "standard")

                core_source = (
                    PROJECT_ROOT / "schemes" / scheme_id / "core" / "v31_common.py"
                ).read_text(encoding="utf-8")
                self.assertIn("from .screen_metrics import feat_screen", core_source)
                self.assertEqual(core_source.count("feat_screen("), 2)
                self.assertEqual(core_source.count("metric=SCREEN_METRIC"), 2)

    def test_model_versions_and_cache_families_do_not_alias_other_models(self) -> None:
        model_versions: list[str] = []
        cache_families: list[str] = []
        cache_roots: list[Path] = []

        for scheme_id, metric, rebal in CASES:
            with self.subTest(scheme_id=scheme_id):
                inference = importlib.import_module(f"schemes.{scheme_id}.inference")
                model_version = str(inference.MODEL_VERSION)
                cache_family = str(inference.CACHE_FAMILY)
                cache_root = inference.family_cache_root(Path("/tmp/liwei-phase-a"))

                self.assertIn(metric, model_version.lower())
                self.assertIn(rebal, model_version.lower())
                self.assertIn(metric, cache_family.lower())
                self.assertIn(rebal, cache_family.lower())
                self.assertEqual(
                    cache_root,
                    Path("/tmp/liwei-phase-a") / cache_family,
                )
                self.assertNotIn(model_version, model_versions)
                self.assertLessEqual(len(model_version), 64)
                self.assertNotIn(cache_family, cache_families)
                self.assertNotIn(cache_root, cache_roots)
                model_versions.append(model_version)
                cache_families.append(cache_family)
                cache_roots.append(cache_root)

                with patch.object(
                    inference,
                    "prepare_phase_a_caches",
                    return_value=({}, {"status": "hit"}),
                ) as prepare:
                    inference._prepare_incremental_phase_a_caches(
                        daily_df=pd.DataFrame(),
                        weekly_df=pd.DataFrame(),
                        monthly_df=pd.DataFrame(),
                        date_to_week=None,
                        test_ranges=(("2026-07-10", "2026-07-10"),),
                        n_workers=1,
                        cache_root=Path("/tmp/liwei-phase-a"),
                    )
                self.assertEqual(
                    prepare.call_args.kwargs["cache_root"],
                    Path("/tmp/liwei-phase-a"),
                )

        self.assertNotIn("liwei_0616_5y01_full_oos_v1", model_versions)
        self.assertNotIn("liwei_0616_5y_v31", cache_families)


if __name__ == "__main__":
    unittest.main()
