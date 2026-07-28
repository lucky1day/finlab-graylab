from __future__ import annotations

import hashlib
import json
import pickle
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from shared.liwei_0616_cache_projection import AuxiliaryDependencyProjection
from shared.liwei_0616_phase_a_cache import PhaseACacheSpec, prepare_phase_a_caches


class Liwei0616PhaseACacheTests(unittest.TestCase):
    """liwei_0616 Phase A 增量缓存测试。"""

    def setUp(self) -> None:
        self.spec = PhaseACacheSpec(
            cache_family="test_liwei_0616_5y_v31",
            tenor="5Y",
            publisher_consumer_id="consumer-a",
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
        self.weekly_df = pd.DataFrame(
            {"week_id": [202626, 202627], "weekly_x": [0.9, 1.0]}
        )
        self.monthly_df = pd.DataFrame(
            {"month_id": ["202606", "202607"], "monthly_x": [1.9, 2.0]}
        )

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
                "cache_consumer_id": "consumer-a",
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

    def test_appended_week_and_month_rows_force_full_without_mapping_proof(
        self,
    ) -> None:
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
            self.assertEqual(audit["status"], "cold_build")
            self.assertEqual(audit["build_mode"], "full")
            self.assertEqual(
                audit["build_reason"],
                "weekly_input_append_unmappable",
            )
            self.assertEqual(
                trained_batches,
                [["2026-07-01", "2026-07-02", "2026-07-03"]],
            )

    def test_effective_projection_append_ignores_unrelated_raw_auxiliary_revision(
        self,
    ) -> None:
        trained_batches: list[list[str]] = []
        trainer = self._trainer(trained_batches)
        initial_daily = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-07-23"]),
                "TB5YWI0C": [1.60],
            }
        )
        current_daily = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-07-23", "2026-07-24", "2026-07-27"]
                ),
                "TB5YWI0C": [1.60, 1.61, 1.62],
            }
        )
        initial_projection = self._projection(
            ["2026-07-23"],
            [0.10],
        )
        current_projection = self._projection(
            ["2026-07-23", "2026-07-24", "2026-07-27"],
            [0.10, 0.20, 0.30],
        )
        revised_weekly = self.weekly_df.copy()
        revised_weekly.loc[0, "weekly_x"] = 9.90
        revised_monthly = self.monthly_df.copy()
        revised_monthly.loc[0, "monthly_x"] = 8.80

        with tempfile.TemporaryDirectory() as tmp:
            prepare_phase_a_caches(
                spec=self.spec,
                daily_df=initial_daily,
                weekly_df=self.weekly_df,
                monthly_df=self.monthly_df,
                auxiliary_dependency_projection=initial_projection,
                test_ranges=(("2026-07-23", "2026-07-23"),),
                train_missing=trainer,
                cache_consumer_id="consumer-a",
                cache_root=Path(tmp),
            )
            trained_batches.clear()

            _cache, audit = prepare_phase_a_caches(
                spec=self.spec,
                daily_df=current_daily,
                weekly_df=revised_weekly,
                monthly_df=revised_monthly,
                auxiliary_dependency_projection=current_projection,
                test_ranges=(("2026-07-23", "2026-07-27"),),
                train_missing=trainer,
                cache_consumer_id="consumer-a",
                cache_root=Path(tmp),
            )

        self.assertEqual(audit["build_mode"], "append")
        self.assertEqual(
            audit["build_reason"],
            "effective_auxiliary_append",
        )
        self.assertEqual(
            audit["input_change"]["frames"]["weekly"]["change_type"],
            "revision",
        )
        self.assertEqual(
            audit["input_change"]["frames"]["monthly"]["change_type"],
            "revision",
        )
        self.assertEqual(
            audit["input_change"]["effective_auxiliary"]["change_type"],
            "append",
        )
        self.assertEqual(
            trained_batches,
            [["2026-07-24", "2026-07-27"]],
        )

    def test_current_week_and_month_revisions_force_full_rebuild(self) -> None:
        trained_batches: list[list[str]] = []
        trainer = self._trainer(trained_batches)
        with tempfile.TemporaryDirectory() as tmp:
            common = self._common(Path(tmp), trainer)
            prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            trained_batches.clear()
            weekly = self.weekly_df.copy()
            weekly.loc[weekly["week_id"] == 202627, "weekly_x"] = 9.9
            monthly = self.monthly_df.copy()
            monthly.loc[monthly["month_id"] == "202607", "monthly_x"] = 8.8

            _cache, audit = prepare_phase_a_caches(
                **{**common, "weekly_df": weekly, "monthly_df": monthly},
                test_ranges=(("2026-07-01", "2026-07-03"),),
            )

            self.assertEqual(audit["status"], "cold_build")
            self.assertEqual(audit["build_mode"], "full")
            self.assertEqual(audit["build_reason"], "input_revision")
            self.assertEqual(
                trained_batches,
                [["2026-07-01", "2026-07-02", "2026-07-03"]],
            )

    def test_completed_week_revision_forces_cold_rebuild(self) -> None:
        trained_batches: list[list[str]] = []
        trainer = self._trainer(trained_batches)
        with tempfile.TemporaryDirectory() as tmp:
            common = self._common(Path(tmp), trainer)
            prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            trained_batches.clear()
            weekly = self.weekly_df.copy()
            weekly.loc[weekly["week_id"] == 202626, "weekly_x"] = 7.7

            _cache, audit = prepare_phase_a_caches(
                **{**common, "weekly_df": weekly},
                test_ranges=(("2026-07-01", "2026-07-03"),),
            )

            self.assertEqual(audit["status"], "cold_build")
            self.assertEqual(
                trained_batches,
                [["2026-07-01", "2026-07-02", "2026-07-03"]],
            )

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
            _cache, first_audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            cache_path = (
                Path(first_audit["generation_path"])
                / "baselines"
                / "STD.pkl"
            )
            cache_path.write_bytes(b"not-a-pickle")
            trained_batches.clear()
            _cache, audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            self.assertEqual(audit["status"], "cold_build")
            self.assertEqual(
                audit["build_reason"],
                "current_generation_invalid",
            )
            self.assertEqual(trained_batches, [["2026-07-01", "2026-07-02"]])
            self.assertNotEqual(
                audit["generation_id"],
                first_audit["generation_id"],
            )
            self.assertEqual(cache_path.read_bytes(), b"not-a-pickle")

    def test_failed_extension_keeps_previous_cache_byte_for_byte(self) -> None:
        trained_batches: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = self._common(root, self._trainer(trained_batches))
            _cache, first_audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )
            pointer = Path(first_audit["current_pointer"])
            before = pointer.read_bytes()
            cache_path = (
                Path(first_audit["generation_path"])
                / "baselines"
                / "STD.pkl"
            )
            cache_before = cache_path.read_bytes()

            def failing_trainer(_baseline: str, _ranges: tuple[tuple[str, str], ...]) -> dict[str, object]:
                raise RuntimeError("training failed")

            with self.assertRaisesRegex(RuntimeError, "training failed"):
                prepare_phase_a_caches(
                    **{**common, "train_missing": failing_trainer},
                    test_ranges=(("2026-07-01", "2026-07-03"),),
                )
            self.assertEqual(pointer.read_bytes(), before)
            self.assertEqual(cache_path.read_bytes(), cache_before)

    def test_older_truncated_request_does_not_lower_persisted_watermark(self) -> None:
        trained_batches: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = self._common(root, self._trainer(trained_batches))
            _cache, first_audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-03"),),
            )
            pointer = Path(first_audit["current_pointer"])
            before = pointer.read_bytes()
            trained_batches.clear()

            cache, audit = prepare_phase_a_caches(
                **{
                    **common,
                    "daily_df": self.daily_df.iloc[:2].copy(),
                },
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )

            self.assertEqual(trained_batches, [])
            self.assertEqual(
                cache["STD"]["test_dates"],
                ["2026-07-01", "2026-07-02", "2026-07-03"],
            )
            self.assertEqual(audit["status"], "hit")
            self.assertTrue(audit["baselines"]["STD"]["preserved_newer_watermark"])
            self.assertEqual(pointer.read_bytes(), before)

    def test_structurally_corrupt_newer_cache_is_not_preserved(self) -> None:
        trained_batches: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = self._common(root, self._trainer(trained_batches))
            _cache, first_audit = prepare_phase_a_caches(
                **common,
                test_ranges=(("2026-07-01", "2026-07-03"),),
            )
            cache_path = (
                Path(first_audit["generation_path"])
                / "baselines"
                / "STD.pkl"
            )
            with cache_path.open("rb") as handle:
                envelope = pickle.load(handle)
            envelope["phase_a_cache"]["results"] = []
            with cache_path.open("wb") as handle:
                pickle.dump(envelope, handle)
            trained_batches.clear()

            _cache, audit = prepare_phase_a_caches(
                **{
                    **common,
                    "daily_df": self.daily_df.iloc[:2].copy(),
                },
                test_ranges=(("2026-07-01", "2026-07-02"),),
            )

            self.assertFalse(audit["baselines"]["STD"]["preserved_newer_watermark"])
            self.assertEqual(
                audit["build_reason"],
                "current_generation_invalid",
            )
            self.assertNotEqual(
                audit["generation_id"],
                first_audit["generation_id"],
            )
            self.assertEqual(trained_batches, [["2026-07-01", "2026-07-02"]])

    def test_concurrent_cold_requests_train_each_date_once(self) -> None:
        trained_batches: list[list[str]] = []

        def slow_trainer(baseline: str, ranges: tuple[tuple[str, str], ...]):
            time.sleep(0.05)
            return self._trainer(trained_batches)(baseline, ranges)

        with tempfile.TemporaryDirectory() as tmp:
            common = self._common(Path(tmp), slow_trainer)

            def prepare():
                return prepare_phase_a_caches(
                    **common,
                    test_ranges=(("2026-07-01", "2026-07-02"),),
                )[1]

            with ThreadPoolExecutor(max_workers=2) as pool:
                audits = [future.result() for future in [pool.submit(prepare), pool.submit(prepare)]]

            self.assertEqual(trained_batches, [["2026-07-01", "2026-07-02"]])
            self.assertEqual({audit["status"] for audit in audits}, {"cold_build", "hit"})

    def test_7y_incompatible_specs_use_distinct_cache_families(self) -> None:
        from schemes.liwei_0616_7y01_cons_say_k3_div_k10 import inference as y01_inference
        from schemes.liwei_0616_7y01_cons_say_k3_div_k10.core import v31_common as y01_core
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8 import inference as y03_inference
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8.core import v31_common as y03_core

        self.assertEqual(y01_inference.CACHE_FAMILY, "liwei_0616_7y01_v31")
        self.assertEqual(y03_inference.CACHE_FAMILY, "liwei_0616_7y03_v31")
        self.assertNotEqual(
            y01_inference.CACHE_FAMILY,
            y03_inference.CACHE_FAMILY,
        )
        self.assertEqual(y01_core.BASELINE_CONFIGS, y03_core.BASELINE_CONFIGS)
        self.assertNotEqual(y01_core.__file__, y03_core.__file__)
        self.assertEqual(y01_core.required_baselines(), ["STD", "ACCWT", "CROSS_5Y", "DIV"])

    def test_10y_scheme_pair_declares_one_shared_baseline_family(self) -> None:
        from schemes.liwei_0616_10y01_cons_say_k3_div_k10 import inference as y01_inference
        from schemes.liwei_0616_10y01_cons_say_k3_div_k10.core import v31_common as y01_core
        from schemes.liwei_0616_10y02_cons_say_k3_div_k5 import inference as y02_inference
        from schemes.liwei_0616_10y02_cons_say_k3_div_k5.core import v31_common as y02_core

        self.assertEqual(y01_inference.CACHE_FAMILY, "liwei_0616_10y_v61")
        self.assertEqual(y02_inference.CACHE_FAMILY, y01_inference.CACHE_FAMILY)
        self.assertEqual(y01_core.BASELINE_CONFIGS, y02_core.BASELINE_CONFIGS)
        self.assertEqual(y01_core.required_baselines(), ["STD", "ACCWT", "V55_7Y", "DIV"])

    def test_7y_and_10y_phase_a_trainers_request_and_unpack_return_context(self) -> None:
        from schemes.liwei_0616_10y01_cons_say_k3_div_k10 import inference as y10_01
        from schemes.liwei_0616_10y02_cons_say_k3_div_k5 import inference as y10_02
        from schemes.liwei_0616_7y01_cons_say_k3_div_k10 import inference as y7_01
        from schemes.liwei_0616_7y03_cons_all_k3_div_k8 import inference as y7_03

        phase_a_cache = {
            "test_dates": ["2026-07-03"],
            "results": [
                {
                    "config": {"window": 200},
                    "preds": np.asarray([1], dtype=np.int32),
                    "probs": np.asarray([0.6], dtype=np.float64),
                }
            ],
        }

        for inference in (y7_01, y7_03, y10_01, y10_02):
            with self.subTest(module=inference.__name__):
                def fake_prepare(**kwargs):
                    trained = kwargs["train_missing"](
                        "STD",
                        (("2026-07-03", "2026-07-03"),),
                    )
                    return {"STD": trained}, {"status": "cold_build"}

                with (
                    patch.object(inference, "model_config", return_value={}) as config,
                    patch.object(
                        inference,
                        "build_auxiliary_dependency_projection",
                        return_value=object(),
                    ),
                    patch.object(
                        inference,
                        "run_prediction",
                        return_value=(np.zeros(1, dtype=np.int32), {"phase_a_cache": phase_a_cache}),
                    ),
                    patch.object(inference, "prepare_phase_a_caches", side_effect=fake_prepare),
                ):
                    caches, _audit = inference._prepare_incremental_phase_a_caches(
                        daily_df=self.daily_df,
                        weekly_df=self.weekly_df,
                        monthly_df=self.monthly_df,
                        date_to_week={"2026-07-03": 202627},
                        test_ranges=(("2026-07-03", "2026-07-03"),),
                        n_workers=1,
                        cache_root=None,
                    )

                self.assertIs(caches["STD"], phase_a_cache)
                self.assertTrue(config.call_args.kwargs["phase_a_only"])
                self.assertTrue(config.call_args.kwargs["return_ctx"])

    def _common(self, root: Path, trainer):
        return {
            "spec": self.spec,
            "daily_df": self.daily_df,
            "weekly_df": self.weekly_df,
            "monthly_df": self.monthly_df,
            "train_missing": trainer,
            "cache_consumer_id": "consumer-a",
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

    @staticmethod
    def _projection(
        dates: list[str],
        values: list[float],
        *,
        proof_file_sha256: str = "a" * 64,
    ) -> AuxiliaryDependencyProjection:
        frame = pd.DataFrame(
            {
                "date": dates,
                "effective_aux": values,
            }
        )
        mapping_entries = [
            {"date": day, "week_id": 202630}
            for day in dates
        ]
        mapping_payload = [
            [entry["date"], entry["week_id"]]
            for entry in mapping_entries
        ]
        proof = {
            "schema_version":
                "liwei-0616-auxiliary-dependency-projection-v1",
            "date_to_week_mode": "explicit",
            "date_to_week_sha256": hashlib.sha256(
                json.dumps(
                    mapping_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
            "date_to_week_entries": mapping_entries,
            "proof_files": [
                {"name": "data_alignment.py", "sha256": proof_file_sha256}
            ],
            "columns": list(frame.columns),
            "dtypes": [
                str(frame[column].dtype) for column in frame.columns
            ],
            "daily_grid_sha256": hashlib.sha256(
                "|".join(dates).encode("utf-8")
            ).hexdigest(),
            "feature_cutoff": dates[-1],
        }
        return AuxiliaryDependencyProjection(
            frame=frame,
            proof=proof,
            content_sha256=hashlib.sha256(
                frame.to_json(orient="split").encode("utf-8")
            ).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
