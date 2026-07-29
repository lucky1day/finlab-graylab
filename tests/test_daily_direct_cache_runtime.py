from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

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
            baseline_configs={"STD": {"window": 5}},
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
                "projection_proof_identity_sha256": "5" * 64,
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
                storage_root=Path(self.cache_root),
            )

        self.assertEqual(len(authority["consumers"]), 10)
        self.assertEqual(
            {
                consumer["access_mode"]
                for consumer in authority["consumers"].values()
            },
            {"publisher", "read_only"},
        )
        for scheme_id, consumer in authority["consumers"].items():
            self.assertEqual(consumer["base_scheme_id"], scheme_id)
            self.assertEqual(consumer["cache_consumer_id"], scheme_id)
            self.assertIsNone(
                consumer["daily_dependency_lookback_rows"]
            )
            self.assertIsNone(consumer["daily_dependency_proof"])
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
