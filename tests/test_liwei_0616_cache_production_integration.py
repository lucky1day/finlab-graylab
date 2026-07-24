from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


SCHEME_MODULES = (
    "schemes.liwei_0616_10y01_cons_say_k3_div_k10.inference",
    "schemes.liwei_0616_10y01_full_oos_k3_div_k10.inference",
    "schemes.liwei_0616_10y02_cons_say_k3_div_k5.inference",
    "schemes.liwei_0616_5y01_full_oos_k3_div_k10.inference",
    "schemes.liwei_0616_5y_auc_static_all_k3_div_k10.inference",
    "schemes.liwei_0616_5y_auc_yearly_all_k3_div_k10.inference",
    "schemes.liwei_0616_5y_ic_yearly_all_k3_div_k10.inference",
    "schemes.liwei_0616_7y01_cons_say_k3_div_k10.inference",
    "schemes.liwei_0616_7y03_cons_all_k3_div_k8.inference",
    "schemes.liwei_0616_cons_sda_k3_div_k10.inference",
)

ALL_K10_MODULES = frozenset(
    {
        "schemes.liwei_0616_5y_auc_static_all_k3_div_k10.inference",
        "schemes.liwei_0616_5y_auc_yearly_all_k3_div_k10.inference",
        "schemes.liwei_0616_5y_ic_yearly_all_k3_div_k10.inference",
    }
)


class Liwei0616CacheProductionIntegrationTests(unittest.TestCase):
    """生产 Native caller 只消费离线资格，日批不重复 cold/full 输出。"""

    def test_all_production_callers_disable_runtime_qualification_and_bind_consumer(
        self,
    ) -> None:
        daily = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-01"]),
                "TB5YWI0C": [1.6],
                "TB7YWI0C": [1.7],
                "TB10YWI0C": [1.8],
            }
        )
        weekly = pd.DataFrame({"week_id": [202627]})
        monthly = pd.DataFrame({"month_id": ["202607"]})

        for module_name in SCHEME_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                seen_modes: list[object] = []

                def full_output_runner(caches):
                    seen_modes.append(caches)
                    return pd.DataFrame({"direction": [1]})

                with (
                    tempfile.TemporaryDirectory() as directory,
                    patch.dict(
                        os.environ,
                        {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                        clear=False,
                    ),
                    patch.object(
                        module,
                        "prepare_phase_a_caches",
                        return_value=({"STD": {}}, {"status": "hit"}),
                    ) as prepare,
                ):
                    module._prepare_incremental_phase_a_caches(
                        daily_df=daily,
                        weekly_df=weekly,
                        monthly_df=monthly,
                        date_to_week={},
                        test_ranges=(("2026-07-01", "2026-07-01"),),
                        n_workers=1,
                        cache_root=Path(directory),
                        full_output_compare_runner=full_output_runner,
                    )

                kwargs = prepare.call_args.kwargs
                self.assertIsNone(kwargs["compare_cold"])
                self.assertIsNone(kwargs["compare_full_output"])
                self.assertEqual(
                    kwargs["cache_consumer_id"],
                    module_name.split(".")[-2],
                )
                self.assertEqual(seen_modes, [])

    def test_outer_algorithm_full_output_runs_exactly_once(self) -> None:
        module = importlib.import_module(
            "schemes.liwei_0616_10y01_cons_say_k3_div_k10.inference"
        )
        caches = {"STD": {"test_dates": ["2026-07-23"]}}
        detail = pd.DataFrame(
            {
                "anchor_date": ["2026-07-23"],
                "prediction": [1],
                "model_version": ["test-v1"],
            }
        )
        with (
            patch.object(
                module,
                "_prepare_incremental_phase_a_caches",
                return_value=(caches, {"status": "hit"}),
            ),
            patch.object(
                module,
                "run_10y01_for_window_silent",
                return_value=detail,
            ) as run_full_output,
        ):
            result = module.run_10y01_for_feature_date(
                daily_df=pd.DataFrame(),
                weekly_df=pd.DataFrame(),
                monthly_df=pd.DataFrame(),
                date_to_week={},
                feature_date="2026-07-23",
                require_labels=False,
                use_incremental_cache=True,
            )

        self.assertEqual(run_full_output.call_count, 1)
        self.assertIs(
            run_full_output.call_args.kwargs["phase_a_caches"],
            caches,
        )
        self.assertEqual(result["phase_a_cache_audit"]["status"], "hit")

    def test_all_k10_callers_pass_the_global_root_once(
        self,
    ) -> None:
        for module_name in ALL_K10_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                with (
                    tempfile.TemporaryDirectory() as directory,
                    patch.object(
                        module,
                        "prepare_phase_a_caches",
                        return_value=({}, {}),
                    ) as prepare,
                ):
                    root = Path(directory)
                    module._prepare_incremental_phase_a_caches(
                        daily_df=pd.DataFrame(),
                        weekly_df=pd.DataFrame(),
                        monthly_df=pd.DataFrame(),
                        date_to_week={},
                        test_ranges=(),
                        n_workers=1,
                        cache_root=root,
                    )

                self.assertEqual(
                    prepare.call_args.kwargs["cache_root"],
                    root,
                )


if __name__ == "__main__":
    unittest.main()
