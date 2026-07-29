from __future__ import annotations

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

    def test_current_fifty_daily_gaps_are_exactly_gray_live_for_728_729(
        self,
    ) -> None:
        targets = tuple(
            RegistryTarget(
                registry_scheme_id=f"daily_{index}__h1__T{index}",
                base_scheme_id=f"daily_{index}",
                runtime_type="native_adapter",
                frequency="daily",
                task_type="T+1",
                target_tenor=f"T{index}",
                horizon=1,
                scheme_version=f"version-{index}",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
                input_mode="generation_v1",
                code_sha256=f"{index + 1:064x}",
                config_sha256=f"{index + 101:064x}",
            )
            for index in range(25)
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
                target_date=target_date,
                segment="live",
            )
            for predict_date, feature_date, target_date in (
                ("2026-07-28", "2026-07-27", "2026-07-29"),
                ("2026-07-29", "2026-07-28", "2026-07-30"),
            )
            for target in targets
        )
        generations = tuple(
            InputGeneration(
                generation_id=f"native-{index:024x}",
                generation_type="native_source",
                business_date=predict_date,
                feature_date=feature_date,
                readiness_basis="CLOCK_CONTRACT",
                source_commit_token="a" * 64,
                dataset_content_id=f"{index + 10:064x}",
                schema_version="native-generation-v1",
                exporter_version="native-generation-exporter-v1",
                manifest_uri=(
                    f"/frozen/native-{index:024x}/manifest.json"
                ),
                manifest_sha256=f"{index + 20:064x}",
                native_generation_id=None,
                native_manifest_sha256=None,
                state="SEALED",
                sealed_at="2026-07-30T00:00:00.000000",
            )
            for index, (predict_date, feature_date) in enumerate(
                (
                    ("2026-07-28", "2026-07-27"),
                    ("2026-07-29", "2026-07-28"),
                ),
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
                start_date="2026-07-28",
                as_of_date="2026-07-29",
            )

        self.assertEqual(plan["counts"]["GRAY_LIVE_GAP"], 50)
        self.assertEqual(plan["counts"]["blocked"], 0)
        self.assertEqual(
            {row["predict_date"] for row in plan["actions"]},
            {"2026-07-28", "2026-07-29"},
        )
        self.assertTrue(
            all(
                row["segment"] == "live"
                and row["prediction_phase"] == "gray_live"
                for row in plan["actions"]
            )
        )
