from __future__ import annotations

import copy
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from shared.liwei_0616_cache_projection import (
    build_auxiliary_dependency_projection,
)
from shared.liwei_0616_phase_a_cache import PhaseACacheSpec


def _frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    daily = pd.DataFrame(
        {
            "date": ["2026-07-29"],
            "value": [1.0],
        }
    )
    weekly = pd.DataFrame(
        {
            "week_id": [202631],
            "value": [1.0],
        }
    )
    monthly = pd.DataFrame(
        {
            "month_id": [202607],
            "value": [1.0],
        }
    )
    return daily, weekly, monthly


def _direct_registry_config(
    *,
    runtime_type: str,
) -> SimpleNamespace:
    scheme_id = (
        "native_current"
        if runtime_type == "native_adapter"
        else "blackbox_current"
    )
    return SimpleNamespace(
        scheme_id=scheme_id,
        runtime_type=runtime_type,
        scheme_version="current-v2",
        code_hash="1" * 64,
        config_hash="2" * 64,
        manifest_hash=(
            None if runtime_type == "native_adapter" else "3" * 64
        ),
    )


def _direct_registry_row(
    config: SimpleNamespace,
    **overrides: object,
) -> dict[str, object]:
    row = {
        "registry_scheme_id": f"{config.scheme_id}__h1__10Y",
        "base_scheme_id": config.scheme_id,
        "scheme_version": config.scheme_version,
        "version_runtime_type": config.runtime_type,
        "code_hash": config.code_hash,
        "config_hash": config.config_hash,
        "manifest_hash": config.manifest_hash,
    }
    row.update(overrides)
    return row


def _real_projection_inputs(
    inference: object,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, int],
]:
    core = inference.v31_common
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-02", "2026-01-05"]
            )
        }
    )
    weekly = pd.DataFrame(
        {
            "week_id": [202601, 202602],
            **{
                column: [float(index), float(index + 1)]
                for index, column in enumerate(
                    core.WEEKLY_COLS,
                    start=1,
                )
            },
        }
    )
    monthly = pd.DataFrame(
        {
            "month_id": ["202512"],
            **{
                column: [float(index)]
                for index, column in enumerate(
                    core.MONTHLY_COLS,
                    start=1,
                )
            },
        }
    )
    return (
        daily,
        weekly,
        monthly,
        {
            "2026-01-02": 202601,
            "2026-01-05": 202602,
        },
    )


def _build_real_projection(
    inference: object,
    *,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    monthly: pd.DataFrame,
    date_to_week: dict[str, int],
    build_wkmo_features: object | None = None,
):
    core = inference.v31_common
    return build_auxiliary_dependency_projection(
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        date_to_week=date_to_week,
        prepare_model_frames=core.prepare_model_frames,
        build_wkmo_features=(
            build_wkmo_features
            if build_wkmo_features is not None
            else core.build_wkmo_features
        ),
        proof_files=(
            Path(core.__file__),
            Path(inference.data_alignment.__file__),
        ),
    )


class DailyDirectCacheRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cache_root = str(
            (Path(self._tmpdir.name) / "cache-root").resolve()
        )
        self.spec = PhaseACacheSpec(
            cache_family="liwei_direct_test",
            tenor="5Y",
            publisher_consumer_id="publisher",
            baselines=("STD",),
            baseline_configs={
                "STD": {"close": "value", "window": 5}
            },
            source_ic_screen_start="2018-01-01",
            horizon=5,
            purge_gap=5,
        )

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _context(self) -> dict[str, object]:
        from shared.liwei_0616_phase_a_cache import _spec_fingerprint

        return {
            "schema_version":
                "liwei-0616-direct-cache-runtime-context-v1",
            "storage_root": self.cache_root,
            "contract": {
                "manifest_schema_version": 3,
                "input_state_schema_version": 3,
                "cache_abi_version": "liwei_0616.phase_a.v1",
                "projection_schema_version":
                    "liwei-0616-auxiliary-dependency-projection-v1",
                "native_generation_type": "native_source",
                "native_generation_schema_version":
                    "native-generation-v1",
                "native_exporter_version":
                    "native-generation-exporter-v1",
            },
            "consumer": {
                "base_scheme_id": "publisher",
                "cache_consumer_id": "publisher",
                "scheme_version": "publisher-v1",
                "code_sha256": "1" * 64,
                "config_sha256": "2" * 64,
                "cache_group": "liwei_direct_test:5Y",
                "spec_fingerprint": _spec_fingerprint(self.spec),
                "publisher_consumer_id": "publisher",
                "cache_family": "liwei_direct_test",
                "tenor": "5Y",
                "access_mode": "publisher",
                "cache_adapter_sha256": "3" * 64,
                "cache_core_sha256": "4" * 64,
                "publisher_projection_proof_identity_sha256":
                    "5" * 64,
                "daily_dependency_lookback_rows": None,
                "daily_dependency_proof": None,
            },
        }

    def _native(self) -> dict[str, object]:
        return {
            "generation_id": "native-direct",
            "manifest_sha256": "6" * 64,
            "dataset_content_id": "7" * 64,
            "business_date": "2026-07-30",
            "feature_date": "2026-07-29",
            "schema_version": "native-generation-v1",
            "exporter_version": "native-generation-exporter-v1",
        }

    def _current(self) -> SimpleNamespace:
        from shared.liwei_0616_phase_a_cache import (
            _baseline_fingerprint,
            _spec_fingerprint,
        )

        return SimpleNamespace(
            manifest={
                "schema_version": 3,
                "generation_id": "generation-direct",
                "generation_content_id": "8" * 64,
                "abi_version": "liwei_0616.phase_a.v1",
                "cache_family": self.spec.cache_family,
                "tenor": self.spec.tenor,
                "spec_fingerprint": _spec_fingerprint(self.spec),
                "input_state": {
                    "schema_version": 3,
                    "effective_auxiliary": {
                        "schema_version":
                            "liwei-0616-auxiliary-dependency-projection-v1",
                        "proof_identity_sha256": "5" * 64,
                    }
                },
                "parent_generation_id": None,
                "build_mode": "append",
                "input_change": {},
                "created_at": "2026-07-30T00:00:00+00:00",
                "baselines": {
                    "STD": {
                        "relative_path": "baselines/STD.pkl",
                        "file_sha256": "1" * 64,
                        "baseline_fingerprint":
                            _baseline_fingerprint(self.spec, "STD"),
                        "cache_content_sha256": "2" * 64,
                        "watermark": "2026-07-29",
                        "evidence": {},
                    }
                },
                "compare_gate_evidence": {},
                "generation_acceptance_evidence": {},
            },
            caches={"STD": {"test_dates": ["2026-07-29"]}},
        )

    def test_direct_runtime_context_validates_exact_shape(self) -> None:
        from shared.liwei_0616_cache_contract import (
            validate_direct_cache_runtime_context,
        )

        self.assertEqual(
            validate_direct_cache_runtime_context(self._context()),
            self._context(),
        )

        drifted = self._context()
        drifted["consumer"]["spec_fingerprint"] = "z" * 64
        with self.assertRaisesRegex(ValueError, "spec_fingerprint"):
            validate_direct_cache_runtime_context(drifted)

    def test_ledger_accepts_direct_context_without_legacy_qualification(
        self,
    ) -> None:
        from shared import liwei_0616_phase_a_cache as module

        daily, weekly, monthly = _frames()
        expected = ({"STD": {"test_dates": ["2026-07-29"]}}, {"ok": True})
        with (
            patch.dict(
                os.environ,
                {
                    module.DAILY_COORDINATOR_MODE_ENV: "ledger",
                    module.DIRECT_CACHE_RUNTIME_CONTEXT_ENV:
                        module._canonical_json(self._context()),
                },
                clear=False,
            ),
            patch.object(
                module,
                "_prepare_under_family_lock",
                return_value=expected,
            ) as prepare,
            patch.object(
                module,
                "_effective_auxiliary_generation_state",
                return_value={
                    "schema_version":
                        "liwei-0616-auxiliary-dependency-projection-v1",
                    "proof_identity_sha256": "5" * 64,
                },
            ),
        ):
            result = module.prepare_phase_a_caches(
                spec=self.spec,
                daily_df=daily,
                weekly_df=weekly,
                monthly_df=monthly,
                test_ranges=(("2026-07-29", "2026-07-29"),),
                train_missing=lambda _baseline, _ranges: {},
                cache_consumer_id="publisher",
                native_generation=self._native(),
                auxiliary_dependency_projection=object(),
                cache_root=self.cache_root,
            )

        self.assertEqual(result, expected)
        self.assertTrue(prepare.call_args.kwargs["qualification_required"])
        self.assertIsNone(
            prepare.call_args.kwargs["trusted_qualification"]
        )

    def test_ledger_rejects_direct_context_identity_drift(self) -> None:
        from shared import liwei_0616_phase_a_cache as module

        daily, weekly, monthly = _frames()
        context = self._context()
        context["consumer"]["publisher_consumer_id"] = "other"
        with (
            patch.dict(
                os.environ,
                {
                    module.DAILY_COORDINATOR_MODE_ENV: "ledger",
                    module.DIRECT_CACHE_RUNTIME_CONTEXT_ENV:
                        module._canonical_json(context),
                },
                clear=False,
            ),
            patch.object(
                module,
                "_effective_auxiliary_generation_state",
                return_value={
                    "schema_version":
                        "liwei-0616-auxiliary-dependency-projection-v1",
                    "proof_identity_sha256": "5" * 64,
                },
            ),
            self.assertRaisesRegex(
                (RuntimeError, ValueError),
                "publisher|identity",
            ),
        ):
            module.prepare_phase_a_caches(
                spec=self.spec,
                daily_df=daily,
                weekly_df=weekly,
                monthly_df=monthly,
                test_ranges=(("2026-07-29", "2026-07-29"),),
                train_missing=lambda _baseline, _ranges: {},
                cache_consumer_id="publisher",
                native_generation=self._native(),
                auxiliary_dependency_projection=object(),
                cache_root=self.cache_root,
            )

    def test_read_only_direct_context_allows_own_projection_identity(
        self,
    ) -> None:
        from shared import liwei_0616_phase_a_cache as module

        daily, weekly, monthly = _frames()
        context = self._context()
        context["consumer"].update(
            {
                "base_scheme_id": "reader",
                "cache_consumer_id": "reader",
                "scheme_version": "reader-v1",
                "access_mode": "read_only",
            }
        )
        expected = ({"STD": {"test_dates": ["2026-07-29"]}}, {"ok": True})
        with (
            patch.dict(
                os.environ,
                {
                    module.DAILY_COORDINATOR_MODE_ENV: "ledger",
                    module.DIRECT_CACHE_RUNTIME_CONTEXT_ENV:
                        module._canonical_json(context),
                },
                clear=False,
            ),
            patch.object(
                module,
                "_prepare_under_family_lock",
                return_value=expected,
            ) as prepare,
            patch.object(
                module,
                "_effective_auxiliary_generation_state",
                return_value={
                    "schema_version":
                        "liwei-0616-auxiliary-dependency-projection-v1",
                    "proof_identity_sha256": "6" * 64,
                },
            ),
        ):
            result = module.prepare_phase_a_caches(
                spec=self.spec,
                daily_df=daily,
                weekly_df=weekly,
                monthly_df=monthly,
                test_ranges=(("2026-07-29", "2026-07-29"),),
                train_missing=lambda _baseline, _ranges: {},
                cache_consumer_id="reader",
                native_generation=self._native(),
                auxiliary_dependency_projection=object(),
                cache_root=self.cache_root,
            )

        self.assertEqual(result, expected)
        self.assertEqual(
            prepare.call_args.kwargs["cache_consumer_id"],
            "reader",
        )

    def test_publisher_direct_context_requires_own_projection_identity(
        self,
    ) -> None:
        from shared import liwei_0616_phase_a_cache as module

        daily, weekly, monthly = _frames()
        with (
            patch.dict(
                os.environ,
                {
                    module.DAILY_COORDINATOR_MODE_ENV: "ledger",
                    module.DIRECT_CACHE_RUNTIME_CONTEXT_ENV:
                        module._canonical_json(self._context()),
                },
                clear=False,
            ),
            patch.object(
                module,
                "_effective_auxiliary_generation_state",
                return_value={
                    "schema_version":
                        "liwei-0616-auxiliary-dependency-projection-v1",
                    "proof_identity_sha256": "6" * 64,
                },
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "effective_projection",
            ),
        ):
            module.prepare_phase_a_caches(
                spec=self.spec,
                daily_df=daily,
                weekly_df=weekly,
                monthly_df=monthly,
                test_ranges=(("2026-07-29", "2026-07-29"),),
                train_missing=lambda _baseline, _ranges: {},
                cache_consumer_id="publisher",
                native_generation=self._native(),
                auxiliary_dependency_projection=object(),
                cache_root=self.cache_root,
            )

    def test_current_manifest_must_match_frozen_publisher_projection(
        self,
    ) -> None:
        from shared.liwei_0616_cache_contract import (
            validate_direct_cache_runtime_context,
        )
        from shared.liwei_0616_phase_a_cache import (
            _validate_direct_current_generation_authority,
        )

        context = validate_direct_cache_runtime_context(
            self._context()
        )
        current = self._current()
        _validate_direct_current_generation_authority(
            current,
            context,
            self.spec,
        )

        current.manifest["input_state"]["effective_auxiliary"][
            "proof_identity_sha256"
        ] = "6" * 64
        with self.assertRaisesRegex(
            RuntimeError,
            "DIRECT_CACHE_CURRENT_AUTHORITY_DRIFT",
        ):
            _validate_direct_current_generation_authority(
                current,
                context,
                self.spec,
            )

    def test_direct_current_manifest_identity_is_closed_world(
        self,
    ) -> None:
        from shared.liwei_0616_cache_contract import (
            validate_direct_cache_runtime_context,
        )
        from shared.liwei_0616_phase_a_cache import (
            _validate_direct_current_generation_authority,
        )

        context = validate_direct_cache_runtime_context(
            self._context()
        )

        def mutate_baselines(current: SimpleNamespace) -> None:
            current.manifest["baselines"] = {
                "OTHER": current.manifest["baselines"]["STD"]
            }

        def mutate_proof(current: SimpleNamespace) -> None:
            current.manifest["input_state"]["effective_auxiliary"][
                "proof_identity_sha256"
            ] = "9" * 64

        mutations = {
            "schema": lambda current: current.manifest.update(
                {"schema_version": 2}
            ),
            "input_schema": lambda current: current.manifest[
                "input_state"
            ].update({"schema_version": 2}),
            "abi": lambda current: current.manifest.update(
                {"abi_version": "drift"}
            ),
            "family": lambda current: current.manifest.update(
                {"cache_family": "other"}
            ),
            "tenor": lambda current: current.manifest.update(
                {"tenor": "10Y"}
            ),
            "spec": lambda current: current.manifest.update(
                {"spec_fingerprint": "9" * 64}
            ),
            "baseline": mutate_baselines,
            "proof": mutate_proof,
            "unknown": lambda current: current.manifest.update(
                {"unexpected": True}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                current = copy.deepcopy(self._current())
                mutate(current)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "DIRECT_CACHE_CURRENT_AUTHORITY_DRIFT",
                ):
                    _validate_direct_current_generation_authority(
                        current,
                        context,
                        self.spec,
                    )

    def test_direct_runtime_refuses_full_or_unproven_rebind(
        self,
    ) -> None:
        from shared.liwei_0616_phase_a_cache import (
            _require_direct_runtime_incremental_build_mode,
        )

        for build_mode, build_reason in (
            ("full", "date_to_week_history_changed"),
            ("full", "effective_auxiliary_projection_unknown"),
            ("rebind", "native_generation_rebound"),
        ):
            with (
                self.subTest(
                    build_mode=build_mode,
                    build_reason=build_reason,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "DIRECT_CACHE_OPERATOR_BOOTSTRAP_REQUIRED",
                ),
            ):
                _require_direct_runtime_incremental_build_mode(
                    self._context(),
                    build_mode=build_mode,
                    build_reason=build_reason,
                )

    def test_native_generation_change_with_proven_append_is_append(
        self,
    ) -> None:
        from shared.liwei_0616_phase_a_cache import (
            _projection_build_decision,
        )

        input_change = {
            "projection_status": "valid",
            "change_type": "append",
            "native_generation_changed": True,
            "frames": {
                "daily": {
                    "change_type": "append",
                    "earliest_changed_key": "2026-07-30",
                    "schema_changed": False,
                }
            },
            "effective_auxiliary": {
                "change_type": "append",
                "earliest_changed_key": "2026-07-30",
                "schema_changed": False,
            },
            "date_to_week": {
                "change_type": "append",
                "earliest_changed_key": "2026-07-30",
            },
        }

        self.assertEqual(
            _projection_build_decision(
                spec=self.spec,
                input_change=input_change,
            ),
            ("append", "effective_auxiliary_append", None),
        )

    def test_direct_native_generation_append_publishes_append_mode(
        self,
    ) -> None:
        from shared import liwei_0616_phase_a_cache as module

        publisher = importlib.import_module(
            "schemes.liwei_0616_5y01_full_oos_k3_div_k10."
            "inference"
        )
        daily, weekly, monthly, mapping = _real_projection_inputs(
            publisher
        )
        daily["value"] = [1.0, 2.0]
        projection = _build_real_projection(
            publisher,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            date_to_week=mapping,
        )

        def trainer(
            baseline: str,
            ranges: tuple[tuple[str, str], ...],
        ) -> dict[str, object]:
            dates = [start for start, end in ranges if start == end]
            values = np.arange(len(dates), dtype=np.int32)
            return {
                "test_dates": dates,
                "results": [
                    {
                        "config": {"baseline": baseline},
                        "preds": values,
                        "probs": values.astype(np.float64),
                    }
                ],
            }

        _cache, first = module.prepare_phase_a_caches(
            spec=self.spec,
            daily_df=daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=(("2026-01-02", "2026-01-05"),),
            train_missing=trainer,
            require_compare_gate=False,
            cache_consumer_id="publisher",
            native_generation=self._native(),
            auxiliary_dependency_projection=projection,
            cache_root=self.cache_root,
        )
        Path(self.cache_root).chmod(0o700)

        next_daily = pd.concat(
            [
                daily,
                pd.DataFrame(
                    {
                        "date": [pd.Timestamp("2026-01-06")],
                        "value": [3.0],
                    }
                ),
            ],
            ignore_index=True,
        )
        next_weekly = pd.concat(
            [
                weekly,
                pd.DataFrame(
                    {
                        "week_id": [202603],
                        **{
                            column: [float(index)]
                            for index, column in enumerate(
                                publisher.v31_common.WEEKLY_COLS,
                                start=10,
                            )
                        },
                    }
                ),
            ],
            ignore_index=True,
        )
        next_mapping = {**mapping, "2026-01-06": 202603}
        next_projection = _build_real_projection(
            publisher,
            daily=next_daily,
            weekly=next_weekly,
            monthly=monthly,
            date_to_week=next_mapping,
        )
        first_proof = module._effective_auxiliary_generation_state(
            projection
        )["proof_identity_sha256"]
        self.assertEqual(
            first_proof,
            module._effective_auxiliary_generation_state(
                next_projection
            )["proof_identity_sha256"],
        )
        context = self._context()
        context["consumer"][
            "publisher_projection_proof_identity_sha256"
        ] = first_proof
        next_native = {
            **self._native(),
            "generation_id": "native-direct-next",
            "manifest_sha256": "a" * 64,
            "dataset_content_id": "b" * 64,
            "business_date": "2026-07-31",
            "feature_date": "2026-07-30",
        }
        with patch.dict(
            os.environ,
            {
                module.DAILY_COORDINATOR_MODE_ENV: "ledger",
                module.DIRECT_CACHE_RUNTIME_CONTEXT_ENV:
                    module._canonical_json(context),
            },
            clear=False,
        ):
            _cache, second = module.prepare_phase_a_caches(
                spec=self.spec,
                daily_df=next_daily,
                weekly_df=next_weekly,
                monthly_df=monthly,
                test_ranges=(
                    ("2026-01-02", "2026-01-06"),
                ),
                train_missing=trainer,
                cache_consumer_id="publisher",
                native_generation=next_native,
                auxiliary_dependency_projection=next_projection,
                cache_root=self.cache_root,
            )

        self.assertNotEqual(
            first["generation_id"],
            second["generation_id"],
        )
        self.assertEqual(second["build_mode"], "append")
        self.assertNotEqual(second["build_mode"], "rebind")
        self.assertTrue(
            second["input_change"]["native_generation_changed"]
        )

    def test_direct_mapping_unknown_and_native_drift_never_train(
        self,
    ) -> None:
        from shared import liwei_0616_phase_a_cache as module

        daily, weekly, monthly = _frames()
        unchanged = {
            "change_type": "unchanged",
            "earliest_changed_key": None,
            "schema_changed": False,
        }
        cases = {
            "mapping": {
                "change_type": "unknown",
                "raw_change_type": "unchanged",
                "frames": {
                    "daily": unchanged,
                    "weekly": unchanged,
                    "monthly": unchanged,
                },
                "effective_auxiliary": unchanged,
                "date_to_week": {
                    "change_type": "revision",
                    "earliest_changed_key": "2026-07-29",
                },
                "projection_status": "mapping_changed",
                "suffix_start_date": None,
                "native_generation_changed": False,
            },
            "unknown": {
                "change_type": "unknown",
                "raw_change_type": "unchanged",
                "frames": {
                    "daily": unchanged,
                    "weekly": unchanged,
                    "monthly": unchanged,
                },
                "effective_auxiliary": {
                    "change_type": "unavailable",
                    "earliest_changed_key": None,
                    "schema_changed": False,
                },
                "date_to_week": {
                    "change_type": "unavailable",
                    "earliest_changed_key": None,
                },
                "projection_status": "schema_changed",
                "suffix_start_date": None,
                "native_generation_changed": False,
            },
            "native_unproven": {
                "change_type": "unchanged",
                "raw_change_type": "unchanged",
                "frames": {
                    "daily": unchanged,
                    "weekly": unchanged,
                    "monthly": unchanged,
                },
                "effective_auxiliary": unchanged,
                "date_to_week": {
                    "change_type": "unchanged",
                    "earliest_changed_key": None,
                },
                "projection_status": "valid",
                "suffix_start_date": None,
                "native_generation_changed": True,
            },
        }
        for label, input_change in cases.items():
            trained: list[object] = []
            with (
                self.subTest(label=label),
                patch.object(
                    module,
                    "_input_generation_state",
                    return_value={"content_id": "9" * 64},
                ),
                patch.object(
                    module,
                    "_load_current_generation",
                    return_value=(self._current(), None),
                ),
                patch.object(
                    module,
                    "_validate_direct_current_generation_authority",
                ),
                patch.object(
                    module,
                    "_input_change_analysis",
                    return_value=copy.deepcopy(input_change),
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "DIRECT_CACHE_OPERATOR_BOOTSTRAP_REQUIRED",
                ),
            ):
                module._prepare_under_family_lock(
                    spec=self.spec,
                    cache_consumer_id="publisher",
                    is_publisher=True,
                    root=Path(self.cache_root),
                    family_root=(
                        Path(self.cache_root)
                        / self.spec.cache_family
                        / self.spec.tenor.lower()
                    ),
                    daily_df=daily,
                    weekly_df=weekly,
                    monthly_df=monthly,
                    auxiliary_dependency_projection=object(),
                    test_ranges=(
                        ("2026-07-29", "2026-07-29"),
                    ),
                    train_missing=lambda *_args: trained.append(
                        object()
                    ),
                    compare_cold=None,
                    qualify_compare_gate=None,
                    compare_full_output=None,
                    qualification_required=True,
                    trusted_qualification=None,
                    direct_runtime_context=self._context(),
                    native_generation_binding=self._native(),
                )
            self.assertEqual(trained, [])

    def test_real_shared_consumers_ignore_only_own_proof_files(
        self,
    ) -> None:
        from shared.liwei_0616_phase_a_cache import (
            _input_generation_state,
            consumer_input_state_equivalence_sha256,
        )

        module_pairs = (
            (
                "schemes.liwei_0616_10y01_full_oos_k3_div_k10."
                "inference",
                "schemes.liwei_0616_10y02_cons_say_k3_div_k5."
                "inference",
            ),
            (
                "schemes.liwei_0616_5y01_full_oos_k3_div_k10."
                "inference",
                "schemes.liwei_0616_cons_sda_k3_div_k10.inference",
            ),
        )
        for publisher_name, consumer_name in module_pairs:
            with self.subTest(consumer=consumer_name):
                publisher = importlib.import_module(publisher_name)
                consumer = importlib.import_module(consumer_name)
                daily, weekly, monthly, mapping = (
                    _real_projection_inputs(publisher)
                )
                publisher_projection = _build_real_projection(
                    publisher,
                    daily=daily,
                    weekly=weekly,
                    monthly=monthly,
                    date_to_week=mapping,
                )
                consumer_projection = _build_real_projection(
                    consumer,
                    daily=daily,
                    weekly=weekly,
                    monthly=monthly,
                    date_to_week=mapping,
                )
                publisher_state = _input_generation_state(
                    daily_df=daily,
                    weekly_df=weekly,
                    monthly_df=monthly,
                    auxiliary_dependency_projection=(
                        publisher_projection
                    ),
                    native_generation_binding=self._native(),
                )
                consumer_state = _input_generation_state(
                    daily_df=daily,
                    weekly_df=weekly,
                    monthly_df=monthly,
                    auxiliary_dependency_projection=(
                        consumer_projection
                    ),
                    native_generation_binding=self._native(),
                )

                self.assertNotEqual(
                    publisher_state["effective_auxiliary"][
                        "proof_identity_sha256"
                    ],
                    consumer_state["effective_auxiliary"][
                        "proof_identity_sha256"
                    ],
                )
                self.assertEqual(
                    consumer_input_state_equivalence_sha256(
                        publisher_state
                    ),
                    consumer_input_state_equivalence_sha256(
                        consumer_state
                    ),
                )

    def test_consumer_equivalence_rejects_real_input_projection_drift(
        self,
    ) -> None:
        from shared.liwei_0616_phase_a_cache import (
            _input_generation_state,
            consumer_input_state_equivalence_sha256,
        )

        publisher = importlib.import_module(
            "schemes.liwei_0616_10y01_full_oos_k3_div_k10."
            "inference"
        )
        consumer = importlib.import_module(
            "schemes.liwei_0616_10y02_cons_say_k3_div_k5."
            "inference"
        )
        daily, weekly, monthly, mapping = _real_projection_inputs(
            publisher
        )

        def input_state(
            *,
            projection: object,
            weekly_frame: pd.DataFrame = weekly,
        ) -> dict[str, object]:
            return _input_generation_state(
                daily_df=daily,
                weekly_df=weekly_frame,
                monthly_df=monthly,
                auxiliary_dependency_projection=projection,
                native_generation_binding=self._native(),
            )

        publisher_projection = _build_real_projection(
            publisher,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            date_to_week=mapping,
        )
        publisher_digest = (
            consumer_input_state_equivalence_sha256(
                input_state(projection=publisher_projection)
            )
        )

        drifted_weekly = weekly.copy()
        drifted_weekly.loc[
            0,
            publisher.v31_common.WEEKLY_COLS[0],
        ] += 10.0
        data_projection = _build_real_projection(
            consumer,
            daily=daily,
            weekly=drifted_weekly,
            monthly=monthly,
            date_to_week=mapping,
        )

        def renamed_features(
            weekly_frame: pd.DataFrame,
            monthly_frame: pd.DataFrame,
        ) -> pd.DataFrame:
            features = consumer.v31_common.build_wkmo_features(
                weekly_frame,
                monthly_frame,
            )
            return features.rename(
                columns={features.columns[0]: "renamed_feature"}
            )

        columns_projection = _build_real_projection(
            consumer,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            date_to_week=mapping,
            build_wkmo_features=renamed_features,
        )
        mapping_projection = _build_real_projection(
            consumer,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
            date_to_week={
                **mapping,
                "2026-01-05": 202601,
            },
        )
        cases = (
            (
                "data",
                input_state(
                    projection=data_projection,
                    weekly_frame=drifted_weekly,
                ),
            ),
            ("columns", input_state(projection=columns_projection)),
            ("date_mapping", input_state(projection=mapping_projection)),
        )
        for label, consumer_state in cases:
            with self.subTest(label=label):
                self.assertNotEqual(
                    publisher_digest,
                    consumer_input_state_equivalence_sha256(
                        consumer_state
                    ),
                )

    def test_scheduled_executor_selects_direct_consumer_context(
        self,
    ) -> None:
        from scheduler.scheduled_executor import (
            _frozen_direct_cache_runtime_context,
        )

        context = self._context()
        consumer = context["consumer"]
        policy = {
            "schemes": [
                {
                    "scheme_id": "publisher",
                    "cache_spec_fingerprint":
                        consumer["spec_fingerprint"],
                    "cache_adapter_sha256":
                        consumer["cache_adapter_sha256"],
                    "cache_core_sha256":
                        consumer["cache_core_sha256"],
                }
            ],
            "direct_cache_authorities": {
                "schema_version":
                    "daily-direct-cache-authorities-v1",
                "storage_root": context["storage_root"],
                "contract": context["contract"],
                "consumers": {"publisher": consumer},
            }
        }
        envelope = SimpleNamespace(
            occurrence=SimpleNamespace(policy_json=policy),
            item=SimpleNamespace(
                base_scheme_id="publisher",
                scheme_version="publisher-v1",
                code_sha256="1" * 64,
                config_sha256="2" * 64,
                cache_group="liwei_direct_test:5Y",
            ),
        )

        self.assertEqual(
            _frozen_direct_cache_runtime_context(envelope),
            context,
        )

        envelope.item.code_sha256 = "9" * 64
        with self.assertRaisesRegex(
            RuntimeError,
            "direct cache.*drift",
        ):
            _frozen_direct_cache_runtime_context(envelope)
        envelope.item.code_sha256 = "1" * 64
        policy["schemes"][0]["cache_core_sha256"] = "9" * 64
        with self.assertRaisesRegex(
            RuntimeError,
            "policy digest drift",
        ):
            _frozen_direct_cache_runtime_context(envelope)
        policy["schemes"][0]["cache_core_sha256"] = "4" * 64
        del policy["direct_cache_authorities"]["consumers"]["publisher"]
        with self.assertRaisesRegex(
            RuntimeError,
            "missing direct authority",
        ):
            _frozen_direct_cache_runtime_context(envelope)

    def test_direct_authority_freezes_exact_current_policy_shape(
        self,
    ) -> None:
        from scheduler.daily_direct_authority import (
            _build_direct_cache_authorities,
        )
        from scheduler.daily_policy import POLICY_V2_PATH, load_daily_policy
        from scheduler.discovery import discover_schemes

        discovered = tuple(
            config
            for config in discover_schemes(strict=True)
            if config.status == "active" and config.frequency == "daily"
        )
        policy = load_daily_policy(
            POLICY_V2_PATH,
            discovered=discovered,
        )
        configs = {
            config.scheme_id: config
            for config in discovered
            if config.scheme_id in policy.schemes
        }
        storage_root = Path(self.cache_root)
        storage_root.mkdir()
        storage_root.chmod(0o700)
        for item in policy.schemes.values():
            if item.cache_spec_fingerprint is None:
                continue
            cache_family, tenor = item.cache_group.rsplit(":", 1)
            namespace = storage_root / cache_family
            family_root = namespace / tenor.lower()
            family_root.mkdir(parents=True, exist_ok=True)
            namespace.chmod(0o700)
            family_root.chmod(0o700)
        target_manifest = [
            {
                "base_scheme_id": scheme_id,
                "cache_adapter_sha256": "a" * 64,
                "cache_core_sha256": "b" * 64,
            }
            for scheme_id, item in policy.schemes.items()
            if item.cache_spec_fingerprint is not None
        ]
        loaded = SimpleNamespace(
            manifest={
                "schema_version": 3,
                "abi_version": "liwei_0616.phase_a.v1",
                "cache_family": "unused",
                "tenor": "unused",
                "input_state": {
                    "schema_version": 3,
                    "native_generation": self._native(),
                    "effective_auxiliary": {
                        "schema_version":
                            "liwei-0616-auxiliary-dependency-projection-v1",
                        "proof_identity_sha256": "c" * 64,
                    },
                },
            },
            caches={"STD": {"test_dates": ["2026-07-29"]}},
            path=Path(self.cache_root) / "unused",
        )
        with (
            patch(
                "scheduler.daily_direct_authority."
                "_load_current_generation",
                side_effect=lambda family_root, secure: (
                    SimpleNamespace(
                        manifest={
                            **loaded.manifest,
                            "cache_family": family_root.parent.name,
                            "tenor": family_root.name.upper(),
                            "spec_fingerprint": next(
                                item.cache_spec_fingerprint
                                for item in policy.schemes.values()
                                if (
                                    item.cache_group.rsplit(":", 1)[0]
                                    == family_root.parent.name
                                )
                            ),
                        },
                        caches=loaded.caches,
                        path=loaded.path,
                    ),
                    None,
                ),
            ),
            patch(
                "scheduler.daily_direct_authority."
                "_verify_generation_acceptance_lineage",
            ),
            patch(
                "scheduler.daily_direct_authority."
                "_validate_generation_acceptance_for_use",
            ),
        ):
            authority = _build_direct_cache_authorities(
                policy=policy,
                configs=configs,
                target_manifest=target_manifest,
                storage_root=storage_root,
            )

        self.assertEqual(len(authority["consumers"]), 10)
        self.assertEqual(
            {
                consumer["access_mode"]
                for consumer in authority["consumers"].values()
            },
            {"publisher", "read_only"},
        )
        publisher_proofs_by_group: dict[str, set[str]] = {}
        for scheme_id, consumer in authority["consumers"].items():
            self.assertEqual(consumer["base_scheme_id"], scheme_id)
            self.assertEqual(consumer["cache_consumer_id"], scheme_id)
            self.assertIsNone(
                consumer["daily_dependency_lookback_rows"]
            )
            self.assertIsNone(consumer["daily_dependency_proof"])
            self.assertIn(
                "publisher_projection_proof_identity_sha256",
                consumer,
            )
            self.assertNotIn(
                "projection_proof_identity_sha256",
                consumer,
            )
            publisher_proofs_by_group.setdefault(
                str(consumer["cache_group"]),
                set(),
            ).add(
                str(
                    consumer[
                        "publisher_projection_proof_identity_sha256"
                    ]
                )
            )
        self.assertTrue(
            all(
                len(proofs) == 1
                for proofs in publisher_proofs_by_group.values()
            )
        )
        from scheduler.daily_runtime import _policy_payload

        frozen = _policy_payload(
            policy,
            direct_cache_authorities=authority,
        )
        rows = {
            row["scheme_id"]: row
            for row in frozen["schemes"]
            if row["cache_spec_fingerprint"] is not None
        }
        self.assertEqual(set(rows), set(authority["consumers"]))
        for scheme_id, consumer in authority["consumers"].items():
            self.assertEqual(
                rows[scheme_id]["cache_adapter_sha256"],
                consumer["cache_adapter_sha256"],
            )
            self.assertEqual(
                rows[scheme_id]["cache_core_sha256"],
                consumer["cache_core_sha256"],
            )

    def test_direct_authority_rejects_28_target_projection(self) -> None:
        from scheduler.daily_direct_authority import (
            DailyDirectAuthorityError,
            _validate_direct_daily_shape,
        )

        policy = SimpleNamespace(
            expected_item_count=25,
            expected_target_count=29,
            native_max_concurrency=2,
            v2_max_concurrency=2,
        )
        configs = [
            SimpleNamespace(runtime_type="native_adapter")
            for _ in range(17)
        ] + [
            SimpleNamespace(runtime_type="blackbox_v2")
            for _ in range(8)
        ]
        with self.assertRaisesRegex(
            DailyDirectAuthorityError,
            "29 targets",
        ):
            _validate_direct_daily_shape(
                policy=policy,
                configs=configs,
                target_manifest=[{}] * 28,
            )

    def test_direct_cache_ancestry_requires_exact_mode_0700(
        self,
    ) -> None:
        from scheduler.daily_direct_authority import (
            DailyDirectAuthorityError,
            _require_direct_cache_ancestry,
        )

        storage_root = Path(self.cache_root)
        family_root = storage_root / "namespace" / "5y"
        family_root.mkdir(parents=True)
        for path in (
            storage_root,
            family_root.parent,
            family_root,
        ):
            path.chmod(
                0o700 if path == storage_root else 0o755
            )
        _require_direct_cache_ancestry(storage_root, family_root)

        storage_root.chmod(0o755)
        with self.assertRaisesRegex(
            DailyDirectAuthorityError,
            "mode 0700",
        ):
            _require_direct_cache_ancestry(
                storage_root,
                family_root,
            )
        storage_root.chmod(0o700)

        for insecure_path in (
            family_root.parent,
            family_root,
        ):
            insecure_path.chmod(0o775)
            with self.assertRaisesRegex(
                DailyDirectAuthorityError,
                "group/world writable",
            ):
                _require_direct_cache_ancestry(
                    storage_root,
                    family_root,
                )
            insecure_path.chmod(0o755)

    def test_direct_cache_ancestry_rejects_symlink_and_owner_drift(
        self,
    ) -> None:
        from scheduler.daily_direct_authority import (
            DailyDirectAuthorityError,
            _require_direct_cache_ancestry,
        )

        target_root = Path(self.cache_root)
        family_root = target_root / "namespace" / "5y"
        family_root.mkdir(parents=True)
        for path in (
            target_root,
            family_root.parent,
            family_root,
        ):
            path.chmod(0o700)

        symlink_root = Path(self._tmpdir.name) / "cache-link"
        symlink_root.symlink_to(target_root, target_is_directory=True)
        with self.assertRaisesRegex(
            DailyDirectAuthorityError,
            "real directory",
        ):
            _require_direct_cache_ancestry(
                symlink_root,
                symlink_root / "namespace" / "5y",
            )

        with (
            patch(
                "scheduler.daily_direct_authority.os.geteuid",
                return_value=os.geteuid() + 1,
            ),
            self.assertRaisesRegex(
                DailyDirectAuthorityError,
                "owner mismatch",
            ),
        ):
            _require_direct_cache_ancestry(
                target_root,
                family_root,
            )

    def test_direct_cache_directory_rejects_mode_change_while_opening(
        self,
    ) -> None:
        from scheduler.daily_direct_authority import (
            DailyDirectAuthorityError,
            _require_direct_cache_directory,
        )

        family_root = Path(self.cache_root) / "namespace" / "5y"
        family_root.mkdir(parents=True)
        family_root.chmod(0o755)
        actual_fstat = os.fstat

        def changed_mode(descriptor: int) -> SimpleNamespace:
            opened = actual_fstat(descriptor)
            return SimpleNamespace(
                st_dev=opened.st_dev,
                st_ino=opened.st_ino,
                st_mode=(opened.st_mode & ~0o777) | 0o700,
                st_uid=opened.st_uid,
            )

        with (
            patch(
                "scheduler.daily_direct_authority.os.fstat",
                side_effect=changed_mode,
            ),
            self.assertRaisesRegex(
                DailyDirectAuthorityError,
                "changed while opening",
            ),
        ):
            _require_direct_cache_directory(
                family_root,
                "direct cache family",
                exact_mode_0700=False,
            )

    def test_direct_authority_rejects_migration_017_shape(self) -> None:
        from migrations.runner import MigrationPreflightError
        from scheduler.daily_direct_authority import (
            DailyDirectAuthorityError,
            build_daily_direct_cache_authorities,
        )

        with (
            patch(
                "scheduler.daily_direct_authority."
                "preflight_schedule_run_started_at_nullable",
                side_effect=MigrationPreflightError(
                    "requires migration 018"
                ),
            ),
            self.assertRaisesRegex(
                DailyDirectAuthorityError,
                "migration 018",
            ),
        ):
            build_daily_direct_cache_authorities(object())

    def test_native_historical_active_versions_allow_one_exact_current(
        self,
    ) -> None:
        from scheduler.daily_direct_authority import (
            _select_direct_current_registry_versions,
        )

        config = _direct_registry_config(runtime_type="native_adapter")
        current = _direct_registry_row(config)
        historical = _direct_registry_row(
            config,
            scheme_version="historical-v1",
            code_hash="9" * 64,
        )

        self.assertEqual(
            _select_direct_current_registry_versions(
                (config,),
                [historical, current],
            ),
            [current],
        )

    def test_native_requires_exactly_one_current_active_version(
        self,
    ) -> None:
        from scheduler.daily_direct_authority import (
            DailyDirectAuthorityError,
            _select_direct_current_registry_versions,
        )

        config = _direct_registry_config(runtime_type="native_adapter")
        for field, drifted in {
            "scheme_version": "historical-v1",
            "version_runtime_type": "blackbox_v2",
            "code_hash": "9" * 64,
            "config_hash": "8" * 64,
            "manifest_hash": "7" * 64,
        }.items():
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    DailyDirectAuthorityError,
                    "exact current active version.*0",
                ),
            ):
                _select_direct_current_registry_versions(
                    (config,),
                    [
                        _direct_registry_row(
                            config,
                            **{field: drifted},
                        )
                    ],
                )

        current = _direct_registry_row(config)
        with self.assertRaisesRegex(
            DailyDirectAuthorityError,
            "exact current active version.*2",
        ):
            _select_direct_current_registry_versions(
                (config,),
                [current, dict(current)],
            )

    def test_blackbox_still_rejects_multiple_active_versions(
        self,
    ) -> None:
        from scheduler.daily_direct_authority import (
            DailyDirectAuthorityError,
            _select_direct_current_registry_versions,
        )

        config = _direct_registry_config(runtime_type="blackbox_v2")
        current = _direct_registry_row(config)
        historical = _direct_registry_row(
            config,
            scheme_version="historical-v1",
            code_hash="9" * 64,
            manifest_hash="8" * 64,
        )
        with self.assertRaisesRegex(
            DailyDirectAuthorityError,
            "Blackbox.*exactly one active version",
        ):
            _select_direct_current_registry_versions(
                (config,),
                [historical, current],
            )

    def test_direct_authority_rejects_shrinking_parent_coverage(
        self,
    ) -> None:
        from scheduler.daily_direct_authority import (
            _validate_monotonic_parent_coverage,
        )

        generation = SimpleNamespace(
            path=Path(self.cache_root) / "generations" / "current",
            manifest={
                "parent_generation_id": "parent",
                "generation_acceptance_evidence": {
                    "parent": {
                        "generation_id": "parent",
                        "manifest_sha256": "d" * 64,
                    }
                },
            },
            caches={"STD": {"test_dates": ["2026-07-29"]}},
        )
        parent = SimpleNamespace(
            caches={
                "STD": {
                    "test_dates": [
                        "2026-07-28",
                        "2026-07-29",
                    ]
                }
            }
        )
        with (
            patch(
                "scheduler.daily_direct_authority."
                "_load_generation_directory",
                return_value=parent,
            ),
            self.assertRaisesRegex(ValueError, "not monotonic"),
        ):
            _validate_monotonic_parent_coverage(generation)


if __name__ == "__main__":
    unittest.main()
