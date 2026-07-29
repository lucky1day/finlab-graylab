from __future__ import annotations

import copy
import hashlib
import json
import os
import pickle
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

import shared.liwei_0616_phase_a_cache as cache_module
from shared.liwei_0616_cache_projection import AuxiliaryDependencyProjection
from shared.liwei_0616_phase_a_cache import (
    PhaseACacheSpec,
    prepare_phase_a_caches,
)
from shared.liwei_0616_cache_contract import (
    cache_use_qualification_sha256,
)


class Liwei0616ImmutableCacheGenerationTests(unittest.TestCase):
    """Phase A cache generation、prewarmer 与容量边界。"""

    def setUp(self) -> None:
        self.spec = PhaseACacheSpec(
            cache_family="test_liwei_0616_5y_v31",
            tenor="5Y",
            baselines=("STD",),
            baseline_configs={"STD": {"close": "TB5YWI0C", "window": 200}},
            source_ic_screen_start="2024-01-01",
            horizon=5,
            purge_gap=5,
            publisher_consumer_id="consumer-a",
        )
        self.daily = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    [
                        "2026-07-01",
                        "2026-07-02",
                        "2026-07-03",
                        "2026-07-06",
                    ]
                ),
                "TB5YWI0C": [1.60, 1.61, 1.62, 1.63],
            }
        )
        self.weekly = pd.DataFrame(
            {"week_id": [202626, 202627], "weekly_x": [0.9, 1.0]}
        )
        self.monthly = pd.DataFrame(
            {"month_id": ["202606", "202607"], "monthly_x": [1.9, 2.0]}
        )

    def test_cold_build_publishes_complete_immutable_generation_and_evidence(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            _caches, audit = self._prepare(
                Path(directory),
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )

            generation_path = Path(audit["generation_path"])
            current_pointer = Path(audit["current_pointer"])
            pointer = json.loads(current_pointer.read_text(encoding="utf-8"))
            manifest_path = generation_path / "manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            evidence = audit["compare_gate_evidence"]

            self.assertEqual(audit["status"], "cold_build")
            self.assertEqual(audit["build_mode"], "full")
            self.assertEqual(pointer["generation_id"], audit["generation_id"])
            self.assertEqual(generation_path.name, audit["generation_id"])
            self.assertTrue((generation_path / "baselines" / "STD.pkl").is_file())
            self.assertEqual(
                hashlib.sha256(manifest_bytes).hexdigest(),
                audit["generation_manifest_sha256"],
            )
            self.assertEqual(
                evidence["comparison_fields"],
                [
                    "test_dates",
                    "results[].config",
                    "results[].preds",
                    "results[].probs",
                ],
            )
            self.assertEqual(
                set(
                    evidence["baselines"]["STD"][
                        "cached_integrity"
                    ]["field_sha256"]
                ),
                {
                    "test_dates",
                    "results[].config",
                    "results[].preds",
                    "results[].probs",
                },
            )
            self.assertTrue(evidence["cold_compare_required"])
            self.assertEqual(
                evidence["qualification_status"],
                "unqualified",
            )
            self.assertFalse(evidence["capacity_eligible"])
            self.assertIsNone(
                evidence["baselines"]["STD"]["cold_integrity"]
            )
            self.assertEqual(trained, [("STD", ["2026-07-01", "2026-07-02"])])

    def test_append_only_extension_creates_new_generation_without_mutating_previous(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            previous_path = Path(first["generation_path"])
            previous_bytes = self._tree_bytes(previous_path)
            trained.clear()

            cache, second = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
            )

            self.assertEqual(second["status"], "extended")
            self.assertEqual(second["build_mode"], "append")
            self.assertNotEqual(first["generation_id"], second["generation_id"])
            self.assertEqual(self._tree_bytes(previous_path), previous_bytes)
            self.assertEqual(trained, [("STD", ["2026-07-03"])])
            self.assertEqual(
                cache["STD"]["test_dates"],
                ["2026-07-01", "2026-07-02", "2026-07-03"],
            )

    def test_narrow_consumer_hit_preserves_618_date_publisher_generation(
        self,
    ) -> None:
        daily = self._long_daily()
        all_dates = daily["date"].dt.strftime("%Y-%m-%d").tolist()
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, first = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=daily,
                start=all_dates[0],
                end=all_dates[-1],
            )
            pointer = Path(first["current_pointer"])
            pointer_before = pointer.read_bytes()
            trained.clear()

            cache, second = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=daily,
                start=all_dates[-42],
                end=all_dates[-1],
                cache_consumer_id="consumer-b",
            )

            self.assertEqual(second["status"], "hit")
            self.assertEqual(second["generation_id"], first["generation_id"])
            self.assertEqual(pointer.read_bytes(), pointer_before)
            self.assertEqual(trained, [])
            self.assertEqual(cache["STD"]["test_dates"], all_dates)

    def test_consumer_accepts_same_effective_input_from_own_core_proof(
        self,
    ) -> None:
        dates = ["2026-07-01", "2026-07-02"]
        publisher_projection = self._projection(
            dates,
            [1.0, 2.0],
            proof_file_sha256="a" * 64,
            content_sha256="b" * 64,
        )
        consumer_projection = self._projection(
            dates,
            [1.0, 2.0],
            proof_file_sha256="c" * 64,
            content_sha256="d" * 64,
        )
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
                auxiliary_dependency_projection=publisher_projection,
            )
            pointer = Path(first["current_pointer"])
            pointer_before = pointer.read_bytes()
            trained.clear()

            cache, second = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
                cache_consumer_id="consumer-b",
                auxiliary_dependency_projection=consumer_projection,
            )

            self.assertEqual(second["status"], "hit")
            self.assertEqual(second["generation_id"], first["generation_id"])
            self.assertEqual(pointer.read_bytes(), pointer_before)
            self.assertEqual(trained, [])
            self.assertEqual(
                cache["STD"]["test_dates"],
                dates,
            )
            digest = (
                cache_module.consumer_input_state_equivalence_sha256
            )
            publisher_state = cache_module._input_generation_state(
                daily_df=self.daily.iloc[:2].copy(),
                weekly_df=self.weekly,
                monthly_df=self.monthly,
                auxiliary_dependency_projection=publisher_projection,
                native_generation_binding=None,
            )
            consumer_state = cache_module._input_generation_state(
                daily_df=self.daily.iloc[:2].copy(),
                weekly_df=self.weekly,
                monthly_df=self.monthly,
                auxiliary_dependency_projection=consumer_projection,
                native_generation_binding=None,
            )
            self.assertNotEqual(
                publisher_state["content_id"],
                consumer_state["content_id"],
            )
            self.assertEqual(digest(publisher_state), digest(consumer_state))
            self.assertEqual(
                first["consumer_input_equivalence_sha256"],
                digest(publisher_state),
            )
            self.assertEqual(
                second["consumer_input_equivalence_sha256"],
                digest(consumer_state),
            )

            corrupted = copy.deepcopy(consumer_state)
            corrupted["content_id"] = "0" * 64
            with self.assertRaisesRegex(
                ValueError,
                "content digest mismatch",
            ):
                digest(corrupted)

    def test_public_input_change_audit_validator_is_closed_world(
        self,
    ) -> None:
        validator = (
            cache_module.validate_phase_a_cache_input_change_audit
        )
        state = cache_module._input_generation_state(
            daily_df=self.daily.iloc[:2].copy(),
            weekly_df=self.weekly,
            monthly_df=self.monthly,
            auxiliary_dependency_projection=self._projection(
                ["2026-07-01", "2026-07-02"],
                [1.0, 2.0],
            ),
            native_generation_binding=None,
        )
        change = cache_module._public_input_change(
            cache_module._input_change_analysis(state, state)
        )
        self.assertEqual(validator(change), change)

        for label, mutate in (
            (
                "unknown-field",
                lambda value: value.update({"unexpected": True}),
            ),
            (
                "bad-frame-enum",
                lambda value: value["frames"]["daily"].update(
                    {"change_type": "forged"}
                ),
            ),
            (
                "bad-native-flag",
                lambda value: value.update(
                    {"native_generation_changed": 1}
                ),
            ),
        ):
            with self.subTest(label=label):
                invalid = copy.deepcopy(change)
                mutate(invalid)
                with self.assertRaises(ValueError):
                    validator(invalid)

    def test_consumer_rejects_any_effective_raw_mapping_or_native_drift(
        self,
    ) -> None:
        dates = ["2026-07-01", "2026-07-02"]
        publisher_projection = self._projection(
            dates,
            [1.0, 2.0],
            proof_file_sha256="a" * 64,
            content_sha256="b" * 64,
        )
        native = self._native_generation_binding(
            generation_id="native-a",
            manifest_sha256="1" * 64,
        )
        cases = {
            "effective_frame": {
                "projection": self._projection(
                    dates,
                    [1.0, 9.0],
                    proof_file_sha256="c" * 64,
                    content_sha256="d" * 64,
                ),
            },
            "mapping": {
                "projection": self._projection(
                    dates,
                    [1.0, 2.0],
                    proof_file_sha256="c" * 64,
                    week_ids=[202630, 202631],
                    content_sha256="d" * 64,
                ),
            },
            "mapping_mode": {
                "projection": self._projection(
                    dates,
                    [1.0, 2.0],
                    proof_file_sha256="c" * 64,
                    date_to_week_mode="fallback",
                    content_sha256="d" * 64,
                ),
            },
            "raw_weekly": {
                "projection": self._projection(
                    dates,
                    [1.0, 2.0],
                    proof_file_sha256="c" * 64,
                    content_sha256="d" * 64,
                ),
                "weekly": pd.DataFrame(
                    {
                        "week_id": [202626, 202627],
                        "weekly_x": [9.9, 1.0],
                    }
                ),
            },
            "raw_daily": {
                "projection": self._projection(
                    dates,
                    [1.0, 2.0],
                    proof_file_sha256="c" * 64,
                    content_sha256="d" * 64,
                ),
                "daily": self.daily.iloc[:2].assign(
                    TB5YWI0C=[9.9, 1.61]
                ),
            },
            "raw_monthly": {
                "projection": self._projection(
                    dates,
                    [1.0, 2.0],
                    proof_file_sha256="c" * 64,
                    content_sha256="d" * 64,
                ),
                "monthly": pd.DataFrame(
                    {
                        "month_id": ["202606", "202607"],
                        "monthly_x": [9.9, 2.0],
                    }
                ),
            },
            "native": {
                "projection": self._projection(
                    dates,
                    [1.0, 2.0],
                    proof_file_sha256="c" * 64,
                    content_sha256="d" * 64,
                ),
                "native": self._native_generation_binding(
                    generation_id="native-b",
                    manifest_sha256="2" * 64,
                ),
            },
        }
        for label, case in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _cache, first = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    native_generation=native,
                    auxiliary_dependency_projection=publisher_projection,
                )
                pointer = Path(first["current_pointer"])
                pointer_before = pointer.read_bytes()
                trained: list[tuple[str, list[str]]] = []

                with self.assertRaisesRegex(
                    RuntimeError,
                    "CACHE_PUBLISHER_REQUIRED",
                ):
                    self._prepare(
                        root,
                        trainer=self._trainer(trained),
                        daily=case.get(
                            "daily",
                            self.daily.iloc[:2].copy(),
                        ),
                        end="2026-07-02",
                        weekly=case.get("weekly"),
                        monthly=case.get("monthly"),
                        cache_consumer_id="consumer-b",
                        native_generation=case.get("native", native),
                        auxiliary_dependency_projection=case[
                            "projection"
                        ],
                    )

                self.assertEqual(trained, [])
                self.assertEqual(pointer.read_bytes(), pointer_before)

    def test_consumer_before_publisher_fails_without_training_or_current(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            with self.assertRaisesRegex(
                RuntimeError,
                "CACHE_PUBLISHER_REQUIRED",
            ):
                self._prepare(
                    root,
                    trainer=self._trainer(trained),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_consumer_id="consumer-b",
                )

            self.assertEqual(trained, [])
            family_root = (
                root
                / self.spec.cache_family
                / self.spec.tenor.lower()
            )
            self.assertFalse(family_root.exists())

    def test_publisher_full_revision_rebuilds_parent_union_not_narrow_request(
        self,
    ) -> None:
        daily = self._long_daily()
        all_dates = daily["date"].dt.strftime("%Y-%m-%d").tolist()
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=daily,
                start=all_dates[0],
                end=all_dates[-1],
            )
            trained.clear()
            revised = daily.copy()
            revised.loc[0, "TB5YWI0C"] += 0.01

            cache, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=revised,
                start=all_dates[-42],
                end=all_dates[-1],
            )

            self.assertEqual(audit["build_mode"], "full")
            self.assertEqual(trained, [("STD", all_dates)])
            self.assertEqual(cache["STD"]["test_dates"], all_dates)
            self.assertEqual(
                audit["generation_acceptance"]["baselines"]["STD"][
                    "affected_dates"
                ],
                all_dates,
            )
            self.assertEqual(
                audit["generation_acceptance"]["baselines"]["STD"][
                    "preserved_dates"
                ],
                [],
            )

    def test_publisher_identity_does_not_change_mathematical_cache_identity(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
            )
            alternate = PhaseACacheSpec(
                **{
                    **self.spec.__dict__,
                    "publisher_consumer_id": "publisher-b",
                }
            )
            trained.clear()

            cache, second = self._prepare(
                root,
                spec=alternate,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                start="2026-07-03",
                end="2026-07-03",
                cache_consumer_id="publisher-b",
            )

            first_manifest = json.loads(
                (
                    Path(first["generation_path"]) / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            second_manifest = json.loads(
                (
                    Path(second["generation_path"]) / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                first_manifest["spec_fingerprint"],
                second_manifest["spec_fingerprint"],
            )
            self.assertEqual(second["status"], "hit")
            self.assertEqual(second["generation_id"], first["generation_id"])
            self.assertEqual(trained, [])
            self.assertEqual(
                cache["STD"]["test_dates"],
                ["2026-07-01", "2026-07-02", "2026-07-03"],
            )

    def test_known_family_rejects_self_declared_publisher_before_io(
        self,
    ) -> None:
        production_spec = PhaseACacheSpec(
            **{
                **self.spec.__dict__,
                "cache_family": "liwei_0616_5y_v31",
                "publisher_consumer_id": "consumer-b",
            }
        )
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                RuntimeError,
                "CACHE_PUBLISHER_IDENTITY_DRIFT",
            ):
                self._prepare(
                    root,
                    spec=production_spec,
                    trainer=self._trainer(trained),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_consumer_id="consumer-b",
                )

            self.assertEqual(trained, [])
            self.assertEqual(list(root.iterdir()), [])

    def test_consumer_replays_parent_lineage_before_accepting_hit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer([]),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            _cache, current = self._prepare(
                root,
                trainer=self._trainer([]),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
            )
            parent_path = Path(first["generation_path"])
            parent_path.rename(
                parent_path.with_name(parent_path.name + ".missing")
            )
            pointer = Path(current["current_pointer"])
            pointer_before = pointer.read_bytes()
            trained: list[tuple[str, list[str]]] = []

            with self.assertRaisesRegex(
                RuntimeError,
                "CACHE_PUBLISHER_REQUIRED",
            ):
                self._prepare(
                    root,
                    trainer=self._trainer(trained),
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                    cache_consumer_id="consumer-b",
                )

            self.assertEqual(pointer.read_bytes(), pointer_before)
            self.assertEqual(trained, [])

    def test_appended_input_without_new_requested_date_still_rebinds_generation(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-02",
            )
            trained.clear()

            _cache, second = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.copy(),
                end="2026-07-02",
            )

            self.assertEqual(second["build_mode"], "append")
            self.assertNotEqual(
                second["generation_id"],
                first["generation_id"],
            )
            self.assertNotEqual(
                second["input_content_id"],
                first["input_content_id"],
            )
            self.assertEqual(trained, [])

    def test_first_auxiliary_rows_after_empty_frame_force_full_rebuild(
        self,
    ) -> None:
        empty_weekly = pd.DataFrame(
            {
                "week_id": pd.Series(dtype="int64"),
                "weekly_x": pd.Series(dtype="float64"),
            }
        )
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                weekly=empty_weekly,
                end="2026-07-02",
            )
            trained.clear()

            _cache, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                weekly=self.weekly.iloc[:1].copy(),
                end="2026-07-03",
            )

            self.assertEqual(audit["build_mode"], "full")
            self.assertEqual(
                audit["build_reason"],
                "weekly_input_append_unmappable",
            )
            self.assertEqual(
                audit["input_change"]["frames"]["weekly"][
                    "earliest_changed_key"
                ],
                202626,
            )
            self.assertEqual(
                trained,
                [
                    (
                        "STD",
                        [
                            "2026-07-01",
                            "2026-07-02",
                            "2026-07-03",
                        ],
                    )
                ],
            )

    def test_any_existing_input_revision_forces_full_rebuild(self) -> None:
        for frame_name in ("daily", "weekly", "monthly"):
            with self.subTest(frame=frame_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                trained: list[tuple[str, list[str]]] = []
                self._prepare(
                    root,
                    trainer=self._trainer(trained),
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                )
                trained.clear()
                daily = self.daily.iloc[:3].copy()
                weekly = self.weekly.copy()
                monthly = self.monthly.copy()
                if frame_name == "daily":
                    daily.loc[0, "TB5YWI0C"] = 9.9
                elif frame_name == "weekly":
                    weekly.loc[0, "weekly_x"] = 9.9
                else:
                    monthly.loc[0, "monthly_x"] = 9.9

                _cache, audit = self._prepare(
                    root,
                    trainer=self._trainer(trained),
                    daily=daily,
                    weekly=weekly,
                    monthly=monthly,
                    end="2026-07-03",
                )

                self.assertEqual(audit["status"], "cold_build")
                self.assertEqual(audit["build_mode"], "full")
                self.assertEqual(audit["build_reason"], "input_revision")
                self.assertEqual(
                    trained,
                    [("STD", ["2026-07-01", "2026-07-02", "2026-07-03"])],
                )

    def test_native_rebind_does_not_hide_input_revision(self) -> None:
        trained: list[tuple[str, list[str]]] = []
        first_native = self._native_generation_binding(
            generation_id="native-first",
            manifest_sha256="1" * 64,
        )
        second_native = self._native_generation_binding(
            generation_id="native-second",
            manifest_sha256="2" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
                native_generation=first_native,
            )
            trained.clear()
            revised = self.daily.iloc[:3].copy()
            revised.loc[0, "TB5YWI0C"] = 9.9

            _cache, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=revised,
                end="2026-07-03",
                native_generation=second_native,
            )

            self.assertEqual(audit["build_mode"], "full")
            self.assertEqual(audit["build_reason"], "input_revision")
            self.assertEqual(
                trained,
                [("STD", ["2026-07-01", "2026-07-02", "2026-07-03"])],
            )

    def test_lineage_replay_does_not_hide_revision_behind_native_rebind(
        self,
    ) -> None:
        parent = SimpleNamespace(
            manifest={"spec_fingerprint": "same"},
            caches={"STD": {}},
        )
        generation = SimpleNamespace(
            manifest={"spec_fingerprint": "same"},
            caches={"STD": {}},
        )
        input_change = {
            "native_generation_changed": True,
            "change_type": "revision",
            "frames": {
                "daily": {
                    "change_type": "revision",
                    "schema_changed": True,
                },
                "weekly": {"change_type": "unchanged"},
                "monthly": {"change_type": "unchanged"},
            },
        }

        self.assertEqual(
            cache_module._lineage_build_mode(
                parent=parent,
                generation=generation,
                input_change=input_change,
                qualification={},
            ),
            "full",
        )

    def test_proven_daily_revision_rebuilds_only_conservative_suffix(
        self,
    ) -> None:
        proven = PhaseACacheSpec(
            **{
                **self.spec.__dict__,
                "daily_dependency_lookback_rows": 1,
                "daily_dependency_proof": (
                    "phase-a inputs are causal daily rows with one-row "
                    "conservative expansion"
                ),
            }
        )
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                spec=proven,
                trainer=self._trainer(trained),
                daily=self.daily.copy(),
                end="2026-07-06",
            )
            trained.clear()
            revised = self.daily.copy()
            revised.loc[
                revised["date"] == pd.Timestamp("2026-07-03"),
                "TB5YWI0C",
            ] = 9.9

            cache, audit = self._prepare(
                root,
                spec=proven,
                trainer=self._trainer(trained),
                daily=revised,
                end="2026-07-06",
            )

            self.assertEqual(audit["status"], "extended")
            self.assertEqual(audit["build_mode"], "suffix")
            self.assertEqual(
                audit["build_reason"],
                "proven_daily_input_revision",
            )
            self.assertEqual(
                audit["input_change"]["frames"]["daily"][
                    "earliest_changed_key"
                ],
                "2026-07-03",
            )
            self.assertEqual(
                audit["input_change"]["suffix_start_date"],
                "2026-07-02",
            )
            self.assertEqual(
                trained,
                [
                    (
                        "STD",
                        ["2026-07-02", "2026-07-03", "2026-07-06"],
                    )
                ],
            )
            self.assertEqual(
                cache["STD"]["test_dates"],
                [
                    "2026-07-01",
                    "2026-07-02",
                    "2026-07-03",
                    "2026-07-06",
                ],
            )

    def test_weekly_or_monthly_revision_never_uses_daily_suffix_proof(
        self,
    ) -> None:
        proven = PhaseACacheSpec(
            **{
                **self.spec.__dict__,
                "daily_dependency_lookback_rows": 1,
                "daily_dependency_proof": "daily-only causal proof",
            }
        )
        for frame_name in ("weekly", "monthly"):
            with self.subTest(frame=frame_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._prepare(
                    root,
                    spec=proven,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                )
                trained: list[tuple[str, list[str]]] = []
                weekly = self.weekly.copy()
                monthly = self.monthly.copy()
                if frame_name == "weekly":
                    weekly.loc[0, "weekly_x"] = 7.7
                else:
                    monthly.loc[0, "monthly_x"] = 7.7

                _cache, audit = self._prepare(
                    root,
                    spec=proven,
                    trainer=self._trainer(trained),
                    daily=self.daily.iloc[:3].copy(),
                    weekly=weekly,
                    monthly=monthly,
                    end="2026-07-03",
                )

                self.assertEqual(audit["build_mode"], "full")
                self.assertEqual(
                    audit["build_reason"],
                    f"{frame_name}_input_revision_unmappable",
                )
                self.assertEqual(
                    audit["input_change"]["frames"][frame_name][
                        "earliest_changed_key"
                    ],
                    202626 if frame_name == "weekly" else "202606",
                )
                self.assertEqual(
                    trained,
                    [
                        (
                            "STD",
                            [
                                "2026-07-01",
                                "2026-07-02",
                                "2026-07-03",
                            ],
                        )
                    ],
                )

    def test_effective_auxiliary_revision_rebuilds_from_earliest_daily_date(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        initial_projection = self._projection(
            ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"],
            [1.0, 2.0, 3.0, 4.0],
        )
        revised_projection = self._projection(
            ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"],
            [1.0, 2.0, 30.0, 40.0],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.copy(),
                end="2026-07-06",
                auxiliary_dependency_projection=initial_projection,
            )
            trained.clear()

            cache, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.copy(),
                end="2026-07-06",
                auxiliary_dependency_projection=revised_projection,
            )

        self.assertEqual(audit["build_mode"], "suffix")
        self.assertEqual(
            audit["build_reason"],
            "effective_auxiliary_revision",
        )
        self.assertEqual(
            audit["input_change"]["effective_auxiliary"][
                "earliest_changed_key"
            ],
            "2026-07-03",
        )
        self.assertEqual(
            audit["input_change"]["suffix_start_date"],
            "2026-07-03",
        )
        self.assertEqual(
            trained,
            [("STD", ["2026-07-03", "2026-07-06"])],
        )
        self.assertEqual(
            cache["STD"]["test_dates"],
            [
                "2026-07-01",
                "2026-07-02",
                "2026-07-03",
                "2026-07-06",
            ],
        )

    def test_effective_auxiliary_proof_change_forces_full_rebuild(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
                auxiliary_dependency_projection=self._projection(
                    dates,
                    [1.0, 2.0, 3.0],
                    proof_file_sha256="a" * 64,
                ),
            )
            trained.clear()

            _cache, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
                auxiliary_dependency_projection=self._projection(
                    dates,
                    [1.0, 2.0, 3.0],
                    proof_file_sha256="c" * 64,
                ),
            )

        self.assertEqual(audit["build_mode"], "full")
        self.assertEqual(
            audit["build_reason"],
            "effective_auxiliary_proof_changed",
        )
        self.assertEqual(
            trained,
            [("STD", ["2026-07-01", "2026-07-02", "2026-07-03"])],
        )

    def test_projection_cannot_hide_daily_revision(self) -> None:
        trained: list[tuple[str, list[str]]] = []
        dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
        projection = self._projection(dates, [1.0, 2.0, 3.0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
                auxiliary_dependency_projection=projection,
            )
            trained.clear()
            revised = self.daily.iloc[:3].copy()
            revised.loc[0, "TB5YWI0C"] = 9.9

            _cache, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=revised,
                end="2026-07-03",
                auxiliary_dependency_projection=projection,
            )

        self.assertEqual(audit["build_mode"], "full")
        self.assertEqual(audit["build_reason"], "input_revision")
        self.assertEqual(
            trained,
            [("STD", ["2026-07-01", "2026-07-02", "2026-07-03"])],
        )

    def test_projection_lineage_replays_effective_suffix_decision(self) -> None:
        parent = SimpleNamespace(
            manifest={"spec_fingerprint": "same"},
            caches={"STD": {}},
        )
        generation = SimpleNamespace(
            manifest={"spec_fingerprint": "same"},
            caches={"STD": {}},
        )
        input_change = {
            "native_generation_changed": False,
            "change_type": "revision",
            "projection_status": "valid",
            "frames": {
                "daily": {
                    "change_type": "unchanged",
                    "schema_changed": False,
                },
                "weekly": {"change_type": "revision"},
                "monthly": {"change_type": "revision"},
            },
            "effective_auxiliary": {
                "change_type": "revision",
                "earliest_changed_key": "2026-07-03",
                "schema_changed": False,
            },
        }

        self.assertEqual(
            cache_module._lineage_build_mode(
                parent=parent,
                generation=generation,
                input_change=input_change,
                qualification={},
            ),
            "suffix",
        )
        self.assertEqual(
            input_change["suffix_start_date"],
            "2026-07-03",
        )

    def test_combined_daily_and_effective_revisions_use_earliest_cutoff(
        self,
    ) -> None:
        proven = PhaseACacheSpec(
            **{
                **self.spec.__dict__,
                "daily_dependency_lookback_rows": 0,
                "daily_dependency_proof": "causal daily rows",
            }
        )
        input_change = {
            "native_generation_changed": False,
            "change_type": "revision",
            "projection_status": "valid",
            "frames": {
                "daily": {
                    "change_type": "revision",
                    "earliest_changed_key": "2026-07-24",
                    "schema_changed": False,
                },
                "weekly": {"change_type": "revision"},
                "monthly": {"change_type": "revision"},
            },
            "effective_auxiliary": {
                "change_type": "revision",
                "earliest_changed_key": "2026-07-03",
                "schema_changed": False,
            },
            "_daily_union_keys": ["2026-07-03", "2026-07-24"],
            "suffix_start_date": None,
        }

        mode, reason, cutoff = cache_module._projection_build_decision(
            spec=proven,
            input_change=copy.deepcopy(input_change),
        )
        self.assertEqual((mode, reason, cutoff), (
            "suffix",
            "combined_daily_effective_revision",
            "2026-07-03",
        ))

        replay_change = copy.deepcopy(input_change)
        self.assertEqual(
            cache_module._lineage_build_mode(
                parent=SimpleNamespace(
                    manifest={"spec_fingerprint": "same"},
                    caches={"STD": {}},
                ),
                generation=SimpleNamespace(
                    manifest={"spec_fingerprint": "same"},
                    caches={"STD": {}},
                ),
                input_change=replay_change,
                qualification={
                    "daily_dependency_lookback_rows": 0,
                    "daily_dependency_proof": "causal daily rows",
                },
            ),
            "suffix",
        )
        self.assertEqual(
            replay_change["suffix_start_date"],
            "2026-07-03",
        )

    def test_combined_revision_remains_full_when_either_proof_is_invalid(
        self,
    ) -> None:
        input_change = {
            "native_generation_changed": False,
            "change_type": "revision",
            "projection_status": "valid",
            "frames": {
                "daily": {
                    "change_type": "revision",
                    "earliest_changed_key": "2026-07-24",
                    "schema_changed": False,
                },
                "weekly": {"change_type": "unchanged"},
                "monthly": {"change_type": "unchanged"},
            },
            "effective_auxiliary": {
                "change_type": "revision",
                "earliest_changed_key": "2026-07-03",
                "schema_changed": False,
            },
            "_daily_union_keys": ["2026-07-03", "2026-07-24"],
            "suffix_start_date": None,
        }
        mode, _reason, cutoff = cache_module._projection_build_decision(
            spec=self.spec,
            input_change=copy.deepcopy(input_change),
        )
        self.assertEqual((mode, cutoff), ("full", None))

        unproven_effective = copy.deepcopy(input_change)
        unproven_effective["effective_auxiliary"]["schema_changed"] = True
        proven = PhaseACacheSpec(
            **{
                **self.spec.__dict__,
                "daily_dependency_lookback_rows": 0,
                "daily_dependency_proof": "causal daily rows",
            }
        )
        mode, _reason, cutoff = cache_module._projection_build_decision(
            spec=proven,
            input_change=unproven_effective,
        )
        self.assertEqual((mode, cutoff), ("full", None))

    def test_projection_state_tamper_is_rejected(self) -> None:
        projection = self._projection(
            ["2026-07-01", "2026-07-02"],
            [1.0, 2.0],
        )
        state = cache_module._effective_auxiliary_generation_state(
            projection
        )
        tampered = copy.deepcopy(state)
        tampered["proof"]["date_to_week_entries"][0]["week_id"] = 202699

        with self.assertRaisesRegex(
            ValueError,
            "date_to_week digest mismatch",
        ):
            cache_module._validate_effective_auxiliary_state(tampered)

    def test_historical_date_to_week_change_forces_full_rebuild(self) -> None:
        trained: list[tuple[str, list[str]]] = []
        dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
                auxiliary_dependency_projection=self._projection(
                    dates,
                    [1.0, 2.0, 3.0],
                    week_ids=[202626, 202626, 202627],
                ),
            )
            trained.clear()

            _cache, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
                auxiliary_dependency_projection=self._projection(
                    dates,
                    [1.0, 2.0, 3.0],
                    week_ids=[202625, 202626, 202627],
                ),
            )

        self.assertEqual(audit["build_mode"], "full")
        self.assertEqual(
            audit["build_reason"],
            "date_to_week_history_changed",
        )
        self.assertEqual(
            trained,
            [("STD", ["2026-07-01", "2026-07-02", "2026-07-03"])],
        )

    def test_generation_without_projection_remains_readable_and_rebuilds_full(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
            )
            trained.clear()

            _cache, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:3].copy(),
                end="2026-07-03",
                auxiliary_dependency_projection=self._projection(
                    dates,
                    [1.0, 2.0, 3.0],
                ),
            )

        self.assertEqual(audit["build_mode"], "full")
        self.assertEqual(
            audit["build_reason"],
            "effective_auxiliary_projection_missing_from_parent",
        )
        self.assertEqual(
            trained,
            [("STD", ["2026-07-01", "2026-07-02", "2026-07-03"])],
        )

    def test_ledger_mode_rejects_unqualified_generation_before_publish(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.dict(
                    "os.environ",
                    {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                    clear=False,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "signed per-consumer cache use qualification is required",
                ),
            ):
                self._prepare(
                    root,
                    trainer=self._trainer(trained),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                )

            family = (
                root
                / self.spec.cache_family
                / self.spec.tenor.lower()
            )
            self.assertFalse((family / "current.json").exists())
            self.assertEqual(trained, [])

    def test_phase_a_cold_callback_alone_is_not_capacity_qualification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _cache, audit = self._prepare(
                Path(directory),
                trainer=self._trainer([]),
                compare_cold=self._trainer([]),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )

            evidence = audit["compare_gate_evidence"]
            self.assertEqual(
                evidence["qualification_status"],
                "phase_a_compared",
            )
            self.assertFalse(evidence["capacity_eligible"])
            self.assertIsNone(
                evidence["full_output_qualification"]
            )

    def test_signed_per_consumer_qualification_allows_ledger_without_replay(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        trainer = self._trainer(trained)
        native = self._native_generation()
        qualification = self._trusted_qualification(
            self.spec,
            base_scheme_id="consumer-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                "os.environ",
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=False,
            ):
                _cache, first = self._prepare(
                    root,
                    trainer=trainer,
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )
                _cache, second = self._prepare(
                    root,
                    trainer=trainer,
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )

            self.assertTrue(first["capacity_eligible"])
            self.assertEqual(
                first["cache_use_qualification"]["base_scheme_id"],
                "consumer-a",
            )
            self.assertEqual(
                first["generation_acceptance"]["status"],
                "ACCEPTED",
            )
            self.assertEqual(
                first["generation_acceptance"]["baselines"]["STD"][
                    "affected_dates"
                ],
                ["2026-07-01", "2026-07-02"],
            )
            self.assertEqual(
                trained,
                [("STD", ["2026-07-01", "2026-07-02"])],
            )
            self.assertEqual(second["status"], "hit")
            self.assertEqual(
                second["generation_id"],
                first["generation_id"],
            )

    def test_ledger_rejects_other_consumer_and_runtime_self_qualification(
        self,
    ) -> None:
        native = self._native_generation()
        qualification = self._trusted_qualification(
            self.spec,
            base_scheme_id="consumer-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                "os.environ",
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "base_scheme_id mismatch",
                ):
                    self._prepare(
                        root,
                        trainer=self._trainer([]),
                        daily=self.daily.iloc[:2].copy(),
                        end="2026-07-02",
                        cache_consumer_id="consumer-b",
                        cache_use_qualification=qualification,
                        native_generation=native,
                    )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "runtime compare callbacks",
                ):
                    self._prepare(
                        root,
                        trainer=self._trainer([]),
                        compare_cold=self._trainer([]),
                        daily=self.daily.iloc[:2].copy(),
                        end="2026-07-02",
                        cache_use_qualification=qualification,
                        native_generation=native,
                    )

            family = root / self.spec.cache_family / self.spec.tenor.lower()
            self.assertFalse((family / "current.json").exists())

    def test_cache_audit_file_verifier_reopens_parent_lineage(
        self,
    ) -> None:
        native = self._native_generation()
        qualification = self._trusted_qualification(
            self.spec,
            base_scheme_id="consumer-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                "os.environ",
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=False,
            ):
                _cache, first = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )
                _cache, second = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )

            cache_module.verify_phase_a_cache_audit_files(
                {"phase_a_cache": second},
                expected_qualification=qualification,
            )
            parent_manifest = Path(first["generation_path"]) / "manifest.json"
            parent_manifest.write_bytes(
                parent_manifest.read_bytes() + b"\n"
            )
            with self.assertRaisesRegex(ValueError, "parent|manifest"):
                cache_module.verify_phase_a_cache_audit_files(
                    {"phase_a_cache": second},
                    expected_qualification=qualification,
                )

    def test_cache_audit_file_verifier_recomputes_input_diff(
        self,
    ) -> None:
        native = self._native_generation()
        qualification = self._trusted_qualification(
            self.spec,
            base_scheme_id="consumer-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                "os.environ",
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=False,
            ):
                self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )
                _cache, audit = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )

            forged = copy.deepcopy(audit)
            manifest_path = Path(audit["generation_path"]) / "manifest.json"
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            acceptance = manifest["generation_acceptance_evidence"]
            acceptance["input_change"]["change_type"] = "unchanged"
            payload = {
                key: value
                for key, value in acceptance.items()
                if key != "evidence_sha256"
            }
            acceptance["evidence_sha256"] = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            forged["generation_manifest_sha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
            forged["generation_acceptance"] = copy.deepcopy(acceptance)

            with self.assertRaisesRegex(ValueError, "input change|input diff"):
                cache_module.verify_phase_a_cache_audit_files(
                    {"phase_a_cache": forged},
                    expected_qualification=qualification,
                )

    def test_ledger_cache_rejects_symlink_pointer_and_insecure_family(
        self,
    ) -> None:
        native = self._native_generation()
        qualification = self._trusted_qualification(
            self.spec,
            base_scheme_id="consumer-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                "os.environ",
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=False,
            ):
                _cache, audit = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )
                pointer = Path(audit["current_pointer"])
                saved_pointer = pointer.with_name("saved-current.json")
                pointer.rename(saved_pointer)
                pointer.symlink_to(saved_pointer.name)
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError),
                    "symlink|regular|current",
                ):
                    self._prepare(
                        root,
                        trainer=self._trainer([]),
                        daily=self.daily.iloc[:2].copy(),
                        end="2026-07-02",
                        cache_use_qualification=qualification,
                        native_generation=native,
                    )
                pointer.unlink()
                saved_pointer.rename(pointer)
                family = Path(audit["family_root"])
                family.chmod(0o777)
                try:
                    with self.assertRaisesRegex(
                        ValueError,
                        "group/world writable",
                    ):
                        self._prepare(
                            root,
                            trainer=self._trainer([]),
                            daily=self.daily.iloc[:2].copy(),
                            end="2026-07-02",
                            cache_use_qualification=qualification,
                            native_generation=native,
                        )
                finally:
                    family.chmod(0o755)

    def test_cache_audit_rejects_symlink_baseline_file(self) -> None:
        native = self._native_generation()
        qualification = self._trusted_qualification(
            self.spec,
            base_scheme_id="consumer-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                "os.environ",
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=False,
            ):
                _cache, audit = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )
            baseline = (
                Path(audit["generation_path"])
                / "baselines"
                / "STD.pkl"
            )
            saved = baseline.with_name("STD.saved")
            baseline.rename(saved)
            baseline.symlink_to(saved.name)
            with self.assertRaisesRegex(
                ValueError,
                "non-symlink|regular",
            ):
                cache_module.verify_phase_a_cache_audit_files(
                    {"phase_a_cache": audit},
                    expected_qualification=qualification,
                )

    def test_cache_audit_rejects_noncanonical_generation_path(self) -> None:
        native = self._native_generation()
        qualification = self._trusted_qualification(
            self.spec,
            base_scheme_id="consumer-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=False,
            ):
                _cache, audit = self._prepare(
                    Path(directory),
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )
            forged = copy.deepcopy(audit)
            forged["generation_path"] = str(
                Path(audit["generation_path"]).parent
                / "wrong-generation"
            )
            with self.assertRaisesRegex(
                ValueError,
                "path is not canonical",
            ):
                cache_module.verify_phase_a_cache_audit_files(
                    {"phase_a_cache": forged},
                    expected_qualification=qualification,
                )

    def test_atomic_pointer_switch_rejects_family_inode_swap(self) -> None:
        native = self._native_generation()
        qualification = self._trusted_qualification(
            self.spec,
            base_scheme_id="consumer-a",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                "os.environ",
                {"BOND_DAILY_COORDINATOR_MODE": "ledger"},
                clear=False,
            ):
                _cache, audit = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                    cache_use_qualification=qualification,
                    native_generation=native,
                )
            family = Path(audit["family_root"])
            generation = cache_module._load_generation_directory(
                Path(audit["generation_path"]),
                expected_generation_id=audit["generation_id"],
                expected_manifest_sha256=(
                    audit["generation_manifest_sha256"]
                ),
                secure=True,
            )
            moved = family.with_name(f"{family.name}-moved")
            real_read = cache_module._secure_read_regular_file
            swapped = False

            def read_and_swap(path, label, *, max_bytes):
                nonlocal swapped
                content = real_read(path, label, max_bytes=max_bytes)
                if (
                    label == "cache current pointer candidate"
                    and not swapped
                ):
                    family.rename(moved)
                    family.mkdir()
                    swapped = True
                return content

            try:
                with (
                    patch.object(
                        cache_module,
                        "_secure_read_regular_file",
                        side_effect=read_and_swap,
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        "inode changed",
                    ),
                ):
                    cache_module._switch_current_generation(
                        family,
                        generation,
                        secure=True,
                    )
            finally:
                if family.exists():
                    os.rmdir(family)
                if moved.exists():
                    moved.rename(family)

    def test_runtime_full_output_compare_qualifies_generation_for_ledger(
        self,
    ) -> None:
        outputs_seen: list[dict[str, dict[str, object]]] = []

        def compare_full_output(caches):
            outputs_seen.append(caches)
            cached_output = pd.DataFrame(
                {
                    "anchor_date": ["2026-07-02"],
                    "direction": [1],
                    "vote_score": [0.75],
                    "baseline_scores": [{"STD": 0.75}],
                    "baseline_signs": [{"STD": 1}],
                    "confidence": [0.75],
                    "internal_fields": [{"STD": {"probability": 0.75}}],
                }
            )
            return cached_output, cached_output.copy(deep=True)

        with tempfile.TemporaryDirectory() as directory:
            _cache, audit = self._prepare(
                Path(directory),
                trainer=self._trainer([]),
                compare_cold=self._trainer([]),
                compare_full_output=compare_full_output,
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )

        evidence = audit["compare_gate_evidence"]
        self.assertEqual(len(outputs_seen), 1)
        self.assertIn("STD", outputs_seen[0])
        self.assertEqual(
            evidence["qualification_status"],
            "qualified",
        )
        self.assertTrue(evidence["capacity_eligible"])
        self.assertEqual(
            evidence["full_output_qualification"][
                "verifier_version"
            ],
            "liwei-0616-runtime-full-output-exact-v1",
        )

    def test_runtime_full_output_mismatch_is_fail_closed(
        self,
    ) -> None:
        def compare_full_output(caches):
            del caches
            cached_output = pd.DataFrame(
                {
                    "direction": [1],
                    "vote_score": [0.75],
                    "baseline_scores": [{"STD": 0.75}],
                    "baseline_signs": [{"STD": 1}],
                    "confidence": [0.75],
                }
            )
            cold_output = pd.DataFrame(
                {
                    "direction": [-1],
                    "vote_score": [0.75],
                    "baseline_scores": [{"STD": 0.75}],
                    "baseline_signs": [{"STD": 1}],
                    "confidence": [0.75],
                }
            )
            return cached_output, cold_output

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                    ValueError,
                    "full output CompareGate mismatch",
            ):
                self._prepare(
                    root,
                    trainer=self._trainer([]),
                    compare_cold=self._trainer([]),
                    compare_full_output=compare_full_output,
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                )

            family = (
                root
                / self.spec.cache_family
                / self.spec.tenor.lower()
            )
            self.assertFalse((family / "current.json").exists())

    def test_runtime_full_output_missing_required_scope_is_fail_closed(
        self,
    ) -> None:
        def compare_full_output(caches):
            del caches
            incomplete = pd.DataFrame(
                {"direction": [1], "vote_score": [0.75]}
            )
            return incomplete, incomplete.copy(deep=True)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                    ValueError,
                    "full output CompareGate scope is incomplete",
            ):
                self._prepare(
                    root,
                    trainer=self._trainer([]),
                    compare_cold=self._trainer([]),
                    compare_full_output=compare_full_output,
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                )

            family = (
                root
                / self.spec.cache_family
                / self.spec.tenor.lower()
            )
            self.assertFalse((family / "current.json").exists())

    def test_v1_hot_cache_is_adopted_without_retraining_or_mutation(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path = root / "5y" / "STD.pkl"
            legacy_path.parent.mkdir(parents=True)
            cache = self._trainer([])(
                "STD",
                (
                    ("2026-07-01", "2026-07-01"),
                    ("2026-07-02", "2026-07-02"),
                ),
            )
            watermark = "2026-07-02"
            weekly_bound = 202626
            monthly_bound = "202606"
            envelope = {
                "schema_version": 1,
                "cache_family": self.spec.cache_family,
                "tenor": self.spec.tenor,
                "baseline": "STD",
                "baseline_fingerprint": (
                    cache_module._baseline_fingerprint(
                        self.spec,
                        "STD",
                    )
                ),
                "watermark": watermark,
                "input_prefix": {
                    "bounds": {
                        "daily": watermark,
                        "weekly": weekly_bound,
                        "monthly": monthly_bound,
                    },
                    "fingerprints": {
                        "daily": cache_module._frame_prefix_fingerprint(
                            self.daily.iloc[:2].copy(),
                            "date",
                            watermark,
                        ),
                        "weekly": cache_module._frame_prefix_fingerprint(
                            self.weekly,
                            "week_id",
                            weekly_bound,
                        ),
                        "monthly": cache_module._frame_prefix_fingerprint(
                            self.monthly,
                            "month_id",
                            monthly_bound,
                        ),
                    },
                },
                "phase_a_cache": cache,
                "created_at": "2026-07-23T00:00:00+00:00",
                "updated_at": "2026-07-23T00:00:00+00:00",
            }
            with legacy_path.open("wb") as handle:
                pickle.dump(
                    envelope,
                    handle,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
            legacy_bytes = legacy_path.read_bytes()

            migrated, audit = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )

            self.assertEqual(trained, [])
            self.assertEqual(
                migrated["STD"]["test_dates"],
                ["2026-07-01", "2026-07-02"],
            )
            self.assertEqual(
                audit["build_reason"],
                "legacy_v1_migration",
            )
            self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
            self.assertTrue(Path(audit["current_pointer"]).is_file())

    def test_failed_cold_comparison_preserves_previous_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer([]),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            pointer = Path(first["current_pointer"])
            pointer_before = pointer.read_bytes()

            def mismatching_cold(
                baseline: str,
                ranges: tuple[tuple[str, str], ...],
            ) -> dict[str, object]:
                result = self._trainer([])(baseline, ranges)
                result["results"][0]["probs"][0] = 0.999
                return result

            with self.assertRaisesRegex(
                ValueError,
                "cold CompareGate mismatch",
            ):
                self._prepare(
                    root,
                    trainer=self._trainer([]),
                    compare_cold=mismatching_cold,
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                )

            self.assertEqual(pointer.read_bytes(), pointer_before)

    def test_full_compare_evidence_cannot_replay_across_input_generation(
        self,
    ) -> None:
        captured: list[dict[str, object]] = []

        def capture_qualification(
            binding: dict[str, object],
        ) -> dict[str, object]:
            evidence = self._full_qualification(binding)
            captured.append(evidence)
            return evidence

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer([]),
                compare_cold=self._trainer([]),
                qualify_compare_gate=capture_qualification,
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            pointer = Path(first["current_pointer"])
            pointer_before = pointer.read_bytes()
            revised = self.daily.iloc[:2].copy()
            revised.loc[0, "TB5YWI0C"] = 9.9

            with self.assertRaisesRegex(
                ValueError,
                "not bound to this input generation",
            ):
                self._prepare(
                    root,
                    trainer=self._trainer([]),
                    compare_cold=self._trainer([]),
                    qualify_compare_gate=lambda _binding: captured[0],
                    daily=revised,
                    end="2026-07-02",
                )

            self.assertEqual(pointer.read_bytes(), pointer_before)

    def test_one_prewarmer_lock_serializes_same_family_and_tenor(self) -> None:
        active = 0
        maximum_active = 0
        counter_lock = threading.Lock()

        def slow_trainer(
            baseline: str,
            ranges: tuple[tuple[str, str], ...],
        ) -> dict[str, object]:
            nonlocal active, maximum_active
            with counter_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.08)
                return self._trainer([])(baseline, ranges)
            finally:
                with counter_lock:
                    active -= 1

        alt_spec = PhaseACacheSpec(
            **{
                **self.spec.__dict__,
                "baselines": ("ALT",),
                "baseline_configs": {
                    "ALT": {"close": "TB5YWI0C", "window": 350}
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def run(spec: PhaseACacheSpec) -> None:
                prepare_phase_a_caches(
                    spec=spec,
                    daily_df=self.daily.iloc[:2].copy(),
                    weekly_df=self.weekly,
                    monthly_df=self.monthly,
                    test_ranges=(("2026-07-01", "2026-07-02"),),
                    train_missing=slow_trainer,
                    cache_consumer_id=spec.publisher_consumer_id,
                    cache_root=root,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, spec) for spec in (self.spec, alt_spec)]
                for future in futures:
                    future.result()

        self.assertEqual(maximum_active, 1)

    def test_incomplete_or_failed_next_generation_preserves_previous_current(
        self,
    ) -> None:
        two_baselines = PhaseACacheSpec(
            **{
                **self.spec.__dict__,
                "baselines": ("STD", "ALT"),
                "baseline_configs": {
                    "STD": {"close": "TB5YWI0C", "window": 200},
                    "ALT": {"close": "TB5YWI0C", "window": 350},
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                spec=two_baselines,
                trainer=self._trainer([]),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            pointer = Path(first["current_pointer"])
            pointer_before = pointer.read_bytes()
            previous_before = self._tree_bytes(Path(first["generation_path"]))

            def fail_second(
                baseline: str,
                ranges: tuple[tuple[str, str], ...],
            ) -> dict[str, object]:
                if baseline == "ALT":
                    raise RuntimeError("ALT training failed")
                return self._trainer([])(baseline, ranges)

            with self.assertRaisesRegex(RuntimeError, "ALT training failed"):
                self._prepare(
                    root,
                    spec=two_baselines,
                    trainer=fail_second,
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                )

            self.assertEqual(pointer.read_bytes(), pointer_before)
            self.assertEqual(
                self._tree_bytes(Path(first["generation_path"])),
                previous_before,
            )
            self.assertEqual(
                list(Path(first["family_root"]).glob(".building-*")),
                [],
            )

    def test_validation_or_publish_interruption_preserves_previous_current(
        self,
    ) -> None:
        for target, error in (
            ("_validate_staged_generation", ValueError("validation failed")),
            ("_switch_current_generation", KeyboardInterrupt()),
        ):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _cache, first = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                )
                pointer = Path(first["current_pointer"])
                pointer_before = pointer.read_bytes()
                previous_before = self._tree_bytes(Path(first["generation_path"]))

                with patch.object(cache_module, target, side_effect=error):
                    with self.assertRaises(type(error)):
                        self._prepare(
                            root,
                            trainer=self._trainer([]),
                            daily=self.daily.iloc[:3].copy(),
                            end="2026-07-03",
                        )

                self.assertEqual(pointer.read_bytes(), pointer_before)
                self.assertEqual(
                    self._tree_bytes(Path(first["generation_path"])),
                    previous_before,
                )

    def test_precommit_prune_failure_preserves_previous_and_discards_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer([]),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            pointer = Path(first["current_pointer"])
            pointer_before = pointer.read_bytes()
            previous_path = Path(first["generation_path"])
            previous_before = self._tree_bytes(previous_path)
            generation_root = previous_path.parent

            with (
                patch.object(
                    cache_module,
                    "_prune_generations",
                    side_effect=OSError("prune failed"),
                ),
                self.assertRaisesRegex(OSError, "prune failed"),
            ):
                self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                )

            self.assertEqual(pointer.read_bytes(), pointer_before)
            self.assertEqual(
                self._tree_bytes(previous_path),
                previous_before,
            )
            self.assertEqual(
                {
                    path.name
                    for path in generation_root.iterdir()
                    if path.is_dir()
                },
                {first["generation_id"]},
            )

    def test_capacity_rejects_candidate_when_old_current_must_be_preserved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer([]),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            pointer = Path(first["current_pointer"])
            pointer_before = pointer.read_bytes()
            previous_path = Path(first["generation_path"])
            previous_before = self._tree_bytes(previous_path)
            previous_size = cache_module._directory_size_bytes(
                previous_path
            )
            generation_root = previous_path.parent
            revised = self.daily.iloc[:2].copy()
            revised.loc[0, "TB5YWI0C"] += 0.01

            with (
                patch.object(
                    cache_module,
                    "MAX_CACHE_FAMILY_BYTES",
                    previous_size * 2 - 1,
                ),
                self.assertRaises(cache_module.CacheCapacityError),
            ):
                self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=revised,
                    end="2026-07-02",
                )

            self.assertEqual(pointer.read_bytes(), pointer_before)
            self.assertEqual(
                self._tree_bytes(previous_path),
                previous_before,
            )
            self.assertEqual(
                {
                    path.name
                    for path in generation_root.iterdir()
                    if path.is_dir()
                },
                {first["generation_id"]},
            )

    def test_pointer_replace_is_the_commit_point_for_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer([]),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            family_root = Path(first["family_root"])
            real_fsync = cache_module._fsync_directory

            def fail_post_replace_directory_sync(path: Path) -> None:
                if Path(path) == family_root:
                    raise OSError("pointer directory fsync failed")
                real_fsync(path)

            with patch.object(
                cache_module,
                "_fsync_directory",
                side_effect=fail_post_replace_directory_sync,
            ):
                _cache, second = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:3].copy(),
                    end="2026-07-03",
                )

            pointer = json.loads(
                Path(second["current_pointer"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotEqual(
                second["generation_id"],
                first["generation_id"],
            )
            self.assertEqual(
                pointer["generation_id"],
                second["generation_id"],
            )

    def test_disk_reserve_and_family_limit_fail_closed(self) -> None:
        cases = (
            (
                "_available_disk_bytes",
                lambda _path: cache_module.GLOBAL_MIN_FREE_BYTES - 1,
            ),
            (
                "MAX_CACHE_FAMILY_BYTES",
                1,
            ),
        )
        for target, value in cases:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _cache, first = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=self.daily.iloc[:2].copy(),
                    end="2026-07-02",
                )
                pointer = Path(first["current_pointer"])
                pointer_before = pointer.read_bytes()
                patcher = (
                    patch.object(cache_module, target, side_effect=value)
                    if callable(value)
                    else patch.object(cache_module, target, value)
                )

                with patcher, self.assertRaises(cache_module.CacheCapacityError):
                    self._prepare(
                        root,
                        trainer=self._trainer([]),
                        daily=self.daily.iloc[:3].copy(),
                        end="2026-07-03",
                    )

                self.assertEqual(pointer.read_bytes(), pointer_before)

    def test_successful_publication_retains_at_most_three_generations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation_ids: list[str] = []
            for revision in range(4):
                daily = self.daily.iloc[:2].copy()
                daily.loc[0, "TB5YWI0C"] += revision
                _cache, audit = self._prepare(
                    root,
                    trainer=self._trainer([]),
                    daily=daily,
                    end="2026-07-02",
                )
                generation_ids.append(audit["generation_id"])

            generation_root = Path(audit["family_root"]) / "generations"
            retained = sorted(
                path.name
                for path in generation_root.iterdir()
                if path.is_dir()
            )

            self.assertEqual(len(set(generation_ids)), 4)
            self.assertEqual(len(retained), cache_module.CACHE_GENERATION_RETENTION)
            self.assertIn(generation_ids[-1], retained)
            self.assertNotIn(generation_ids[0], retained)

    def test_manifest_evidence_cannot_be_forged_even_if_pointer_digest_is_updated(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            manifest_path = Path(first["generation_path"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["compare_gate_evidence"]["baselines"]["STD"][
                "cached_integrity"
            ][
                "field_sha256"
            ]["results[].probs"] = "0" * 64
            encoded = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            manifest_path.write_bytes(encoded)
            pointer_path = Path(first["current_pointer"])
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
            pointer_path.write_text(
                json.dumps(pointer),
                encoding="utf-8",
            )
            trained.clear()

            _cache, second = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )

            self.assertEqual(second["status"], "cold_build")
            self.assertEqual(
                second["build_reason"],
                "current_generation_invalid",
            )
            self.assertEqual(
                trained,
                [("STD", ["2026-07-01", "2026-07-02"])],
            )
            self.assertNotEqual(
                first["generation_id"],
                second["generation_id"],
            )

    def test_compare_gate_manifest_rejects_unknown_pass_marker(
        self,
    ) -> None:
        trained: list[tuple[str, list[str]]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _cache, first = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )
            manifest_path = (
                Path(first["generation_path"]) / "manifest.json"
            )
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            manifest["compare_gate_evidence"]["status"] = "PASS"
            encoded = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            manifest_path.write_bytes(encoded)
            pointer_path = Path(first["current_pointer"])
            pointer = json.loads(
                pointer_path.read_text(encoding="utf-8")
            )
            pointer["manifest_sha256"] = hashlib.sha256(
                encoded
            ).hexdigest()
            pointer_path.write_text(
                json.dumps(pointer),
                encoding="utf-8",
            )
            trained.clear()

            _cache, second = self._prepare(
                root,
                trainer=self._trainer(trained),
                daily=self.daily.iloc[:2].copy(),
                end="2026-07-02",
            )

            self.assertEqual(
                second["build_reason"],
                "current_generation_invalid",
            )
            self.assertEqual(
                trained,
                [("STD", ["2026-07-01", "2026-07-02"])],
            )

    def _prepare(
        self,
        root: Path,
        *,
        trainer,
        daily: pd.DataFrame,
        end: str,
        start: str = "2026-07-01",
        spec: PhaseACacheSpec | None = None,
        weekly: pd.DataFrame | None = None,
        monthly: pd.DataFrame | None = None,
        compare_cold=None,
        qualify_compare_gate=None,
        compare_full_output=None,
        require_compare_gate: bool | None = None,
        cache_consumer_id: str | None = None,
        cache_use_qualification=None,
        native_generation=None,
        auxiliary_dependency_projection=None,
    ):
        return prepare_phase_a_caches(
            spec=spec or self.spec,
            daily_df=daily,
            weekly_df=self.weekly if weekly is None else weekly,
            monthly_df=self.monthly if monthly is None else monthly,
            test_ranges=((start, end),),
            train_missing=trainer,
            compare_cold=compare_cold,
            qualify_compare_gate=qualify_compare_gate,
            compare_full_output=compare_full_output,
            require_compare_gate=require_compare_gate,
            cache_consumer_id=(
                cache_consumer_id
                or (spec or self.spec).publisher_consumer_id
            ),
            cache_use_qualification=cache_use_qualification,
            native_generation=native_generation,
            auxiliary_dependency_projection=auxiliary_dependency_projection,
            cache_root=root,
        )

    @staticmethod
    def _long_daily() -> pd.DataFrame:
        dates = pd.bdate_range(end="2026-07-03", periods=618)
        return pd.DataFrame(
            {
                "date": dates,
                "TB5YWI0C": np.linspace(1.0, 2.0, len(dates)),
            }
        )

    @staticmethod
    def _trainer(log: list[tuple[str, list[str]]]):
        def train(
            baseline: str,
            ranges: tuple[tuple[str, str], ...],
        ) -> dict[str, object]:
            days = [start for start, end in ranges if start == end]
            log.append((baseline, days))
            values = np.asarray(
                [int(day[-2:]) for day in days],
                dtype=np.int32,
            )
            return {
                "test_dates": days,
                "results": [
                    {
                        "config": {"baseline": baseline, "window": 200},
                        "preds": values,
                        "probs": values.astype(np.float64) / 100.0,
                    }
                ],
            }

        return train

    @staticmethod
    def _native_generation_binding(
        *,
        generation_id: str,
        manifest_sha256: str,
    ) -> dict[str, object]:
        return {
            "generation_id": generation_id,
            "manifest_sha256": manifest_sha256,
            "dataset_content_id": "d" * 64,
            "business_date": "2026-07-07",
            "feature_date": "2026-07-06",
            "schema_version": "native-generation-v1",
            "exporter_version": "native-generation-exporter-v1",
        }

    @staticmethod
    def _projection(
        dates: list[str],
        values: list[float],
        *,
        proof_file_sha256: str = "a" * 64,
        week_ids: list[int] | None = None,
        date_to_week_mode: str = "explicit",
        content_sha256: str | None = None,
    ) -> AuxiliaryDependencyProjection:
        frame = pd.DataFrame(
            {
                "date": dates,
                "effective_aux": values,
            }
        )
        mapping_entries = (
            []
            if date_to_week_mode == "fallback"
            else [
                {"date": day, "week_id": week_id}
                for day, week_id in zip(
                    dates,
                    week_ids or [202630] * len(dates),
                    strict=True,
                )
            ]
        )
        mapping_payload = [
            [entry["date"], entry["week_id"]]
            for entry in mapping_entries
        ]
        proof = {
            "schema_version":
                "liwei-0616-auxiliary-dependency-projection-v1",
            "date_to_week_mode": date_to_week_mode,
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
            content_sha256=(
                content_sha256
                or hashlib.sha256(
                    frame.to_json(orient="split").encode("utf-8")
                ).hexdigest()
            ),
        )

    @staticmethod
    def _native_generation() -> dict[str, object]:
        return {
            "generation_id": "native-20260724",
            "manifest_sha256": "1" * 64,
            "dataset_content_id": "2" * 64,
            "business_date": "2026-07-24",
            "feature_date": "2026-07-23",
            "schema_version": "native-generation-v1",
            "exporter_version": "native-generation-exporter-v1",
        }

    @staticmethod
    def _trusted_qualification(
        spec: PhaseACacheSpec,
        *,
        base_scheme_id: str,
    ) -> dict[str, object]:
        from shared.liwei_0616_cache_contract import (
            qualification_corpus_sha256,
        )

        forced_ids = [f"forced-{index:02d}" for index in range(20)]
        revision_ids = [f"revision-{index:02d}" for index in range(20)]
        coverage = ["forced_cold", "append", "full_rebuild"]
        qualification = {
            "schema_version":
                "liwei-0616-cache-use-qualification-v1",
            "status": "PASSED",
            "source": "signed_capacity_corpus",
            "qualification_id": f"qualification-{base_scheme_id}",
            "base_scheme_id": base_scheme_id,
            "scheme_version": "scheme-version-v1",
            "code_sha256": "3" * 64,
            "config_sha256": "4" * 64,
            "cache_group": f"{spec.cache_family}:{spec.tenor}",
            "cache_family": spec.cache_family,
            "tenor": spec.tenor,
            "cache_adapter_sha256": "5" * 64,
            "cache_core_sha256": "6" * 64,
            "spec_fingerprint":
                cache_module._spec_fingerprint(spec),
            "cache_abi_version": "liwei_0616.phase_a.v1",
            "algorithm_environment_sha256": "7" * 64,
            "capacity_candidate_fingerprint": "8" * 64,
            "native_exporter_sha256": "9" * 64,
            "native_generation_schema_version":
                "native-generation-v1",
            "native_exporter_version":
                "native-generation-exporter-v1",
            "daily_dependency_lookback_rows":
                spec.daily_dependency_lookback_rows,
            "daily_dependency_proof":
                spec.daily_dependency_proof,
            "comparison_fields": [
                "direction",
                "vote_score",
                "baseline_score",
                "baseline_sign",
                "probability",
                "confidence",
                "internal_fields",
                "phase_a_cache",
            ],
            "comparison_evidence_sha256": "a" * 64,
            "corpus": {
                "forced_cold_count": 20,
                "revision_count": 20,
                "coverage_types": coverage,
                "forced_cold_trial_ids": forced_ids,
                "revision_trial_ids": revision_ids,
                "evidence_sha256": qualification_corpus_sha256(
                    forced_cold_trial_ids=forced_ids,
                    revision_trial_ids=revision_ids,
                    coverage_types=coverage,
                ),
            },
        }
        return {
            "schema_version":
                "liwei-0616-trusted-cache-use-qualification-v1",
            "qualification": qualification,
            "qualification_sha256":
                cache_use_qualification_sha256(qualification),
            "admission_decision_id": "decision-20260724",
            "admission_evidence_sha256": "c" * 64,
            "collector_signer_sha256": "d" * 64,
            "operator_signer_sha256": "e" * 64,
            "candidate_fingerprint": "8" * 64,
        }

    @staticmethod
    def _full_qualification(
        qualification_binding: dict[str, object],
    ) -> dict[str, object]:
        encoded_binding = json.dumps(
            qualification_binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "schema_version": (
                "liwei-0616-full-output-compare-qualification-v1"
            ),
            "status": "passed",
            "qualification_id": "offline-compare-20260724",
            "verifier_version": "compare-gate-test-v1",
            "evidence_uri": "reports://liwei-0616/compare.json",
            "evidence_sha256": "a" * 64,
            "qualification_binding_sha256": hashlib.sha256(
                encoded_binding
            ).hexdigest(),
            "comparison_fields": [
                "direction",
                "vote_score",
                "baseline_score",
                "baseline_sign",
                "probability",
                "confidence",
                "internal_fields",
                "phase_a_cache",
            ],
            "cache_content_sha256": qualification_binding[
                "cache_content_sha256"
            ],
        }

    @staticmethod
    def _tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
