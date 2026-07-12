from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from shared.liwei_0616_phase_a_cache import PhaseACacheSpec, prepare_phase_a_caches


class Liwei0616PhaseACacheTests(unittest.TestCase):
    """liwei_0616 Phase A 增量缓存测试。"""

    def setUp(self) -> None:
        self.spec = PhaseACacheSpec(
            cache_family="liwei_0616_5y_v31",
            tenor="5Y",
            baselines=("STD",),
            baseline_configs={"STD": {"close": "TB5YWI0C", "window": 200}},
            source_ic_screen_start="2024-01-01",
            horizon=5,
            purge_gap=5,
        )
        self.daily_df = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"]),
                "TB5YWI0C": [1.6, 1.61, 1.62],
            }
        )
        self.weekly_df = pd.DataFrame({"week_id": [202627], "weekly_x": [1.0]})
        self.monthly_df = pd.DataFrame({"month_id": ["202607"], "monthly_x": [2.0]})

    def test_cold_hit_and_extension_train_only_missing_dates(self) -> None:
        trained_batches: list[list[str]] = []

        def trainer(_baseline: str, ranges: tuple[tuple[str, str], ...]) -> dict[str, object]:
            days = [start for start, end in ranges if start == end]
            trained_batches.append(days)
            values = np.asarray([int(day[-2:]) for day in days], dtype=np.int32)
            return {
                "test_dates": days,
                "results": [
                    {
                        "config": {"window": 200},
                        "preds": values,
                        "probs": values.astype(np.float64) / 100.0,
                    },
                    {
                        "config": {"window": 350},
                        "preds": -values,
                        "probs": values.astype(np.float64) / 200.0,
                    },
                ],
            }

        with tempfile.TemporaryDirectory() as tmp:
            common = {
                "spec": self.spec,
                "daily_df": self.daily_df,
                "weekly_df": self.weekly_df,
                "monthly_df": self.monthly_df,
                "train_missing": trainer,
                "cache_root": Path(tmp),
            }
            cold, cold_audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            self.assertEqual(cold_audit["status"], "cold_build")
            self.assertEqual(trained_batches, [["2026-07-01", "2026-07-02"]])
            self.assertEqual(cold["STD"]["test_dates"], ["2026-07-01", "2026-07-02"])

            trained_batches.clear()
            hit, hit_audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            self.assertEqual(hit_audit["status"], "hit")
            self.assertEqual(trained_batches, [])
            self.assertEqual(hit["STD"]["test_dates"], ["2026-07-01", "2026-07-02"])

            trained_batches.clear()
            extended, extended_audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-03"),),
            )
            self.assertEqual(extended_audit["status"], "extended")
            self.assertEqual(extended_audit["missing_dates"], ["2026-07-03"])
            self.assertEqual(trained_batches, [["2026-07-03"]])
            self.assertEqual(
                extended["STD"]["test_dates"],
                ["2026-07-01", "2026-07-02", "2026-07-03"],
            )
            np.testing.assert_array_equal(
                extended["STD"]["results"][0]["preds"],
                np.asarray([1, 2, 3], dtype=np.int32),
            )

    def test_historical_daily_revision_forces_cold_rebuild(self) -> None:
        trained_batches: list[list[str]] = []
        trainer = self._trainer(trained_batches)
        with tempfile.TemporaryDirectory() as tmp:
            common = self._common(Path(tmp), trainer)
            prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            trained_batches.clear()
            revised = self.daily_df.copy()
            revised.loc[0, "TB5YWI0C"] = 9.99
            _cache, audit = prepare_phase_a_caches(
                **{**common, "daily_df": revised},
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            self.assertEqual(audit["status"], "cold_build")
            self.assertEqual(trained_batches, [["2026-07-01", "2026-07-02"]])

    def test_appended_week_and_month_rows_do_not_invalidate_old_prefix(self) -> None:
        trained_batches: list[list[str]] = []
        trainer = self._trainer(trained_batches)
        with tempfile.TemporaryDirectory() as tmp:
            common = self._common(Path(tmp), trainer)
            prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            trained_batches.clear()
            weekly = pd.concat(
                [self.weekly_df, pd.DataFrame({"week_id": [202628], "weekly_x": [1.1]})],
                ignore_index=True,
            )
            monthly = pd.concat(
                [self.monthly_df, pd.DataFrame({"month_id": ["202608"], "monthly_x": [2.1]})],
                ignore_index=True,
            )
            _cache, audit = prepare_phase_a_caches(
                **{**common, "weekly_df": weekly, "monthly_df": monthly},
                test_ranges=(("2026-07-01", "2026-07-03"),),
            )
            self.assertEqual(audit["status"], "extended")
            self.assertEqual(trained_batches, [["2026-07-03"]])

    def test_baseline_config_change_forces_cold_rebuild(self) -> None:
        trained_batches: list[list[str]] = []
        trainer = self._trainer(trained_batches)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = self._common(root, trainer)
            prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            trained_batches.clear()
            changed = PhaseACacheSpec(
                **{
                    **self.spec.__dict__,
                    "baseline_configs": {"STD": {"close": "TB5YWI0C", "window": 350}},
                }
            )
            _cache, audit = prepare_phase_a_caches(
                **{**common, "spec": changed},
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            self.assertEqual(audit["status"], "cold_build")
            self.assertEqual(trained_batches, [["2026-07-01", "2026-07-02"]])

    def test_corrupt_pickle_is_quarantined_before_cold_rebuild(self) -> None:
        trained_batches: list[list[str]] = []
        trainer = self._trainer(trained_batches)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = self._common(root, trainer)
            prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            cache_path = root / "5y" / "STD.pkl"
            cache_path.write_bytes(b"not-a-pickle")
            trained_batches.clear()
            _cache, audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            self.assertEqual(audit["status"], "cold_build")
            self.assertEqual(trained_batches, [["2026-07-01", "2026-07-02"]])
            self.assertEqual(len(list(cache_path.parent.glob("STD.pkl.invalid-*"))), 1)

    def test_failed_extension_keeps_previous_cache_byte_for_byte(self) -> None:
        trained_batches: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = self._common(root, self._trainer(trained_batches))
            prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            cache_path = root / "5y" / "STD.pkl"
            before = cache_path.read_bytes()

            def failing_trainer(_baseline: str, _ranges: tuple[tuple[str, str], ...]) -> dict[str, object]:
                raise RuntimeError("training failed")

            with self.assertRaisesRegex(RuntimeError, "training failed"):
                prepare_phase_a_caches(
                    **{**common, "train_missing": failing_trainer},
                    test_ranges=(("2026-07-01", "2026-07-03"),),
                )
            self.assertEqual(cache_path.read_bytes(), before)

    def test_7y_scheme_pair_declares_one_shared_baseline_family(self) -> None:
        from schemes.liwei_0616_7y01_cons_say_k3_div_k10 import inference as y01_inference
        from schemes.liwei_0616_7y01_cons_say_k3_div_k10.core import v31_common as y01_core
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8 import inference as y03_inference
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core import v31_common as y03_core

        self.assertEqual(y01_inference.CACHE_FAMILY, "liwei_0616_7y_v31")
        self.assertEqual(y03_inference.CACHE_FAMILY, y01_inference.CACHE_FAMILY)
        self.assertEqual(y01_core.BASELINE_CONFIGS, y03_core.BASELINE_CONFIGS)
        self.assertEqual(y01_core.required_baselines(), ["STD", "ACCWT", "CROSS_5Y", "DIV"])

    def _common(self, root: Path, trainer):
        return {
            "spec": self.spec,
            "daily_df": self.daily_df,
            "weekly_df": self.weekly_df,
            "monthly_df": self.monthly_df,
            "train_missing": trainer,
            "cache_root": root,
        }

    @staticmethod
    def _trainer(trained_batches: list[list[str]]):
        def trainer(_baseline: str, ranges: tuple[tuple[str, str], ...]) -> dict[str, object]:
            days = [start for start, end in ranges if start == end]
            trained_batches.append(days)
            values = np.asarray([int(day[-2:]) for day in days], dtype=np.int32)
            return {
                "test_dates": days,
                "results": [
                    {
                        "config": {"window": 200},
                        "preds": values,
                        "probs": values.astype(np.float64) / 100.0,
                    }
                ],
            }

        return trainer


if __name__ == "__main__":
    unittest.main()
