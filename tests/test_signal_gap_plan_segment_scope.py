from __future__ import annotations

from collections import Counter
from dataclasses import replace
import unittest
from unittest.mock import patch

from harness.signal_gap_plan import (
    DiscoveredSchemeIdentity,
    ExpectedSignalCase,
    InputGeneration,
    RegistryTarget,
    SignalGapSnapshot,
    _read_registry_versions,
    build_signal_gap_plan,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Connection:
    def __init__(self, registry_rows, version_rows):
        self._registry_rows = registry_rows
        self._version_rows = version_rows

    def execute(self, statement):
        sql = str(statement)
        if "FROM t_scheme_registry" in sql:
            return _Rows(self._registry_rows)
        if "FROM t_scheme_versions" in sql:
            return _Rows(self._version_rows)
        raise AssertionError(sql)


def _authority(runtime_type: str) -> DiscoveredSchemeIdentity:
    return DiscoveredSchemeIdentity(
        base_scheme_id="demo",
        scheme_version="current",
        runtime_type=runtime_type,
        code_sha256="a" * 64,
        config_sha256="b" * 64,
        status="active",
    )


def _registry(runtime_type: str) -> dict[str, object]:
    return {
        "scheme_id": "demo__h1__10Y",
        "base_scheme_id": "demo",
        "runtime_type": runtime_type,
        "frequency": "daily",
        "task_type": "T+1",
        "target_tenor": "10Y",
        "horizon": 1,
    }


def _version(version: str, runtime_type: str) -> dict[str, object]:
    return {
        "scheme_id": "demo",
        "scheme_version": version,
        "runtime_type": runtime_type,
        "code_hash": "a" * 64,
        "config_hash": "b" * 64,
    }


class SignalGapPlanSegmentScopeTests(unittest.TestCase):
    def test_native_allows_historical_active_versions_when_exact_is_unique(
        self,
    ) -> None:
        connection = _Connection(
            [_registry("native_adapter")],
            [
                _version("historical", "native_adapter"),
                _version("current", "native_adapter"),
            ],
        )

        with patch(
            "harness.signal_gap_plan._validate_active_scope",
            return_value=None,
        ):
            _, targets, blockers, _ = _read_registry_versions(
                connection,
                execution_authority=(_authority("native_adapter"),),
            )

        self.assertEqual(targets[0].scheme_version, "current")
        self.assertFalse(
            any(
                blocker["code"] == "ACTIVE_VERSION_CARDINALITY_INVALID"
                for blocker in blockers
            )
        )

    def test_blackbox_still_requires_one_active_version(self) -> None:
        connection = _Connection(
            [_registry("blackbox_v2")],
            [
                _version("historical", "blackbox_v2"),
                _version("current", "blackbox_v2"),
            ],
        )

        with patch(
            "harness.signal_gap_plan._validate_active_scope",
            return_value=None,
        ):
            _, _, blockers, _ = _read_registry_versions(
                connection,
                execution_authority=(_authority("blackbox_v2"),),
            )

        self.assertTrue(
            any(
                blocker["code"] == "ACTIVE_VERSION_CARDINALITY_INVALID"
                for blocker in blockers
            )
        )

    def test_canonical_only_blocker_does_not_reach_live_action(
        self,
    ) -> None:
        target = RegistryTarget(
            registry_scheme_id="demo__h1__10Y",
            base_scheme_id="demo",
            runtime_type="native_adapter",
            frequency="daily",
            task_type="T+1",
            target_tenor="10Y",
            horizon=1,
            scheme_version="current",
            live_target_start_date="2026-06-01",
            live_boundary_source="platform_live_boundary_v1",
            input_mode="generation_v1",
            code_sha256="a" * 64,
            config_sha256="b" * 64,
        )
        canonical = ExpectedSignalCase(
            registry_scheme_id=target.registry_scheme_id,
            base_scheme_id="demo",
            runtime_type="native_adapter",
            frequency="daily",
            task_type="T+1",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-05-28",
            feature_date="2026-05-28",
            target_date="2026-05-29",
            segment="canonical",
        )
        live = replace(
            canonical,
            predict_date="2026-07-28",
            feature_date="2026-07-27",
            target_date="2026-07-29",
            segment="live",
        )
        snapshot = SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=(canonical, live),
            canonical_signals=(),
            live_signals=(),
            input_generations=(),
            input_watermarks={},
            source_identity_sha256="c" * 64,
            discovery_identity_sha256="d" * 64,
            active_version_identity_sha256="e" * 64,
            control_plane_blockers=(
                {
                    "code": "NATIVE_CANONICAL_RUN_MISSING",
                    "base_scheme_id": "demo",
                    "segment_scope": ["canonical"],
                },
            ),
        )
        seen: list[tuple[str, str | None]] = []

        def resolve(item, **kwargs):
            seen.append((item.segment, kwargs["control_plane_error"]))
            return "SKIP_PRESENT", None, "test"

        with patch(
            "harness.signal_gap_plan._resolve_action",
            side_effect=resolve,
        ):
            plan = build_signal_gap_plan(
                snapshot,
                start_date="2026-05-28",
                as_of_date="2026-07-28",
            )

        self.assertEqual(
            seen,
            [
                ("canonical", "NATIVE_CANONICAL_RUN_MISSING"),
                ("live", None),
            ],
        )
        self.assertEqual(
            plan["control_plane"]["blockers"][0]["segment_scope"],
            ["canonical"],
        )

    def test_current_fifty_daily_gaps_match_frozen_real_matrix(
        self,
    ) -> None:
        t5_singletons = tuple(
            RegistryTarget(
                registry_scheme_id=f"daily_t5_{index}__h5__10Y",
                base_scheme_id=f"daily_t5_{index}",
                runtime_type="native_adapter",
                frequency="daily",
                task_type="T+5",
                target_tenor="10Y",
                horizon=5,
                scheme_version=f"t5-version-{index}",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
                input_mode="generation_v1",
                code_sha256=f"{index + 1:064x}",
                config_sha256=f"{index + 101:064x}",
            )
            for index in range(20)
        )
        multi_t5_targets = tuple(
            RegistryTarget(
                registry_scheme_id=f"t5_daily__h5__{tenor}",
                base_scheme_id="t5_daily",
                runtime_type="native_adapter",
                frequency="daily",
                task_type="T+5",
                target_tenor=tenor,
                horizon=5,
                scheme_version="multi-t5-version",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
                input_mode="generation_v1",
                code_sha256="3" * 64,
                config_sha256="4" * 64,
            )
            for tenor in ("1Y", "3Y", "5Y", "10Y")
        )
        t1_singletons = tuple(
            RegistryTarget(
                registry_scheme_id=f"daily_t1_{index}__h1__{tenor}",
                base_scheme_id=f"daily_t1_{index}",
                runtime_type="native_adapter",
                frequency="daily",
                task_type="T+1",
                target_tenor=tenor,
                horizon=1,
                scheme_version=f"t1-version-{index}",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
                input_mode="generation_v1",
                code_sha256=f"{index + 201:064x}",
                config_sha256=f"{index + 301:064x}",
            )
            for index, tenor in enumerate(
                ("1Y", "3Y", "5Y")
            )
        )
        multi_t1_targets = tuple(
            RegistryTarget(
                registry_scheme_id=f"t1_daily__h1__{tenor}",
                base_scheme_id="t1_daily",
                runtime_type="native_adapter",
                frequency="daily",
                task_type="T+1",
                target_tenor=tenor,
                horizon=1,
                scheme_version="multi-t1-version",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
                input_mode="generation_v1",
                code_sha256="5" * 64,
                config_sha256="6" * 64,
            )
            for tenor in ("7Y", "10Y")
        )
        targets = (
            *t5_singletons,
            *multi_t5_targets,
            *t1_singletons,
            *multi_t1_targets,
        )
        date_matrix = (
            (
                "2026-07-23",
                "2026-07-22",
                "2026-07-29",
                t5_singletons[:1],
            ),
            (
                "2026-07-24",
                "2026-07-23",
                "2026-07-30",
                t5_singletons[:1],
            ),
            (
                "2026-07-27",
                "2026-07-24",
                "2026-07-31",
                t5_singletons[:2],
            ),
            (
                "2026-07-28",
                "2026-07-27",
                "2026-08-03",
                t5_singletons[:17],
            ),
            (
                "2026-07-29",
                "2026-07-28",
                None,
                targets,
            ),
        )
        cases = tuple(
            ExpectedSignalCase(
                registry_scheme_id=target.registry_scheme_id,
                base_scheme_id=target.base_scheme_id,
                runtime_type=target.runtime_type,
                frequency=target.frequency,
                task_type=target.task_type,
                target_tenor=target.target_tenor,
                horizon=target.horizon,
                predict_date=predict_date,
                feature_date=feature_date,
                target_date=(
                    "2026-07-29"
                    if target.task_type == "T+1"
                    else target_date
                ),
                segment="live",
            )
            for (
                predict_date,
                feature_date,
                common_target_date,
                date_targets,
            ) in date_matrix
            for target in date_targets
            for target_date in [
                (
                    "2026-08-04"
                    if common_target_date is None
                    else common_target_date
                )
            ]
        )
        generations = tuple(
            InputGeneration(
                generation_id=f"native-{index:024x}",
                generation_type="native_source",
                business_date="2026-07-30",
                feature_date=feature_date,
                readiness_basis="CLOCK_CONTRACT",
                source_commit_token="a" * 64,
                dataset_content_id=f"{index + 10:064x}",
                schema_version="native-generation-v1",
                exporter_version=
                    "native-signal-gap-current-snapshot-v1",
                manifest_uri=(
                    f"/frozen/native-{index:024x}/manifest.json"
                ),
                manifest_sha256=f"{index + 20:064x}",
                native_generation_id=None,
                native_manifest_sha256=None,
                state="SEALED",
                sealed_at="2026-07-30T00:00:00.000000",
            )
            for index, (
                predict_date,
                feature_date,
                _target_date,
                _targets,
            ) in enumerate(
                date_matrix,
                start=1,
            )
        )
        snapshot = SignalGapSnapshot(
            registry_targets=targets,
            expected_cases=cases,
            canonical_signals=(),
            live_signals=(),
            input_generations=generations,
            input_watermarks={},
            source_identity_sha256="a" * 64,
            discovery_identity_sha256="b" * 64,
            active_version_identity_sha256="c" * 64,
        )

        with patch(
            "harness.signal_gap_plan._NativeArtifactVerifier.verify",
            return_value=({"artifact": "verified"}, None),
        ):
            plan = build_signal_gap_plan(
                snapshot,
                start_date="2026-07-23",
                as_of_date="2026-07-29",
            )

        self.assertEqual(plan["counts"]["GRAY_LIVE_GAP"], 50)
        self.assertEqual(plan["counts"]["blocked"], 0)

        self.assertEqual(
            Counter(row["predict_date"] for row in plan["actions"]),
            {
                "2026-07-23": 1,
                "2026-07-24": 1,
                "2026-07-27": 2,
                "2026-07-28": 17,
                "2026-07-29": 29,
            },
        )
        self.assertEqual(
            Counter(row["task_type"] for row in plan["actions"]),
            {"T+1": 5, "T+5": 45},
        )
        self.assertEqual(
            Counter(row["horizon"] for row in plan["actions"]),
            {1: 5, 5: 45},
        )
        self.assertEqual(
            Counter(row["target_date"] for row in plan["actions"]),
            {
                "2026-07-29": 6,
                "2026-07-30": 1,
                "2026-07-31": 2,
                "2026-08-03": 17,
                "2026-08-04": 24,
            },
        )
        self.assertEqual(
            len(
                {
                    (row["base_scheme_id"], row["predict_date"])
                    for row in plan["actions"]
                }
            ),
            46,
        )
        groups_729 = Counter(
            row["base_scheme_id"]
            for row in plan["actions"]
            if row["predict_date"] == "2026-07-29"
        )
        self.assertEqual(len(groups_729), 25)
        self.assertEqual(
            Counter(groups_729.values()),
            {1: 23, 2: 1, 4: 1},
        )
        self.assertTrue(
            all(
                row["segment"] == "live"
                and row["prediction_phase"] == "gray_live"
                for row in plan["actions"]
            )
        )
