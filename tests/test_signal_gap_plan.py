from __future__ import annotations

from dataclasses import replace
import unittest
from typing import Any
from unittest.mock import patch

from harness import signal_gap_plan
from shared.data_bridge.authority import (
    StableDataBridgeCutoff,
    StableDataBridgeFileIdentity,
)


_CODE_HASH = "a" * 64
_CONFIG_HASH = "b" * 64


class _Mappings:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Mappings:
        return self

    def all(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]


class _Connection:
    def __init__(
        self,
        registry_rows: list[dict[str, Any]],
        version_rows: list[dict[str, Any]],
    ) -> None:
        self._registry_rows = registry_rows
        self._version_rows = version_rows

    def execute(self, statement: Any) -> _Mappings:
        sql = str(statement)
        if "FROM t_scheme_registry" in sql:
            return _Mappings(self._registry_rows)
        if "FROM t_scheme_versions" in sql:
            return _Mappings(self._version_rows)
        raise AssertionError(f"unexpected statement: {sql}")


def _registry_row(
    base_scheme_id: str,
    *,
    frequency: str,
    task_type: str,
    target_tenor: str,
    horizon: int,
) -> dict[str, Any]:
    return {
        "scheme_id": (
            f"{base_scheme_id}__h{horizon}__{target_tenor}"
        ),
        "base_scheme_id": base_scheme_id,
        "runtime_type": "native_adapter",
        "frequency": frequency,
        "task_type": task_type,
        "target_tenor": target_tenor,
        "horizon": horizon,
    }


def _active_scope_fixture() -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    tuple[signal_gap_plan.DiscoveredSchemeIdentity, ...],
]:
    registry_rows = [
        _registry_row(
            f"scheme_{index:02d}",
            frequency="daily",
            task_type="T+1",
            target_tenor="1Y",
            horizon=1,
        )
        for index in range(32)
    ]
    registry_rows.extend(
        _registry_row(
            f"scheme_{index:02d}",
            frequency="weekly",
            task_type="weekly_point",
            target_tenor="10Y",
            horizon=6,
        )
        for index in range(32, 45)
    )
    registry_rows.extend(
        _registry_row(
            f"scheme_{index:02d}",
            frequency="monthly",
            task_type="monthly",
            target_tenor="10Y",
            horizon=30,
        )
        for index in range(45, 50)
    )
    registry_rows.extend(
        _registry_row(
            f"scheme_{index:02d}",
            frequency="monthly",
            task_type="monthly",
            target_tenor="10Y",
            horizon=30,
        )
        for index in range(3)
    )
    base_scheme_ids = sorted(
        {str(row["base_scheme_id"]) for row in registry_rows}
    )
    version_rows = [
        {
            "scheme_id": base_scheme_id,
            "scheme_version": f"{base_scheme_id}-v1",
            "runtime_type": "native_adapter",
            "code_hash": _CODE_HASH,
            "config_hash": _CONFIG_HASH,
        }
        for base_scheme_id in base_scheme_ids
    ]
    execution_authority = tuple(
        signal_gap_plan.DiscoveredSchemeIdentity(
            base_scheme_id=base_scheme_id,
            scheme_version=f"{base_scheme_id}-v1",
            runtime_type="native_adapter",
            code_sha256=_CODE_HASH,
            config_sha256=_CONFIG_HASH,
            status="active",
        )
        for base_scheme_id in base_scheme_ids
    )
    return registry_rows, version_rows, execution_authority


def _valid_target() -> signal_gap_plan.RegistryTarget:
    return signal_gap_plan.RegistryTarget(
        registry_scheme_id="alpha__h1__1Y",
        base_scheme_id="alpha",
        runtime_type="native_adapter",
        frequency="daily",
        task_type="T+1",
        target_tenor="1Y",
        horizon=1,
        scheme_version="alpha-v1",
        live_target_start_date=(
            signal_gap_plan.PLATFORM_LIVE_TARGET_START_DATE
        ),
        live_boundary_source=(
            signal_gap_plan.PLATFORM_LIVE_BOUNDARY_VERSION
        ),
        input_mode="generation_v1",
        code_sha256=_CODE_HASH,
        config_sha256=_CONFIG_HASH,
    )


def _databridge_authority_payload(feature_date: str) -> dict[str, Any]:
    return {
        "authority_type": "stable_databridge_current",
        "authority_schema_version": "stable-databridge-current-authority-v2",
        "stable_identity_sha256": "c" * 64,
        "generation_id": "gray-replay-generation-1",
        "refresh_date": "2026-08-06",
        "schema_version": "data-bridge-v1",
        "business_digest": "d" * 64,
        "files": [
            {
                "filename": "daily_output.csv",
                "rows": 3,
                "columns": 2,
                "min_key": "2026-07-24",
                "max_key": "2026-08-06",
                "sha256": "1" * 64,
                "business_hash": "4" * 64,
            },
            {
                "filename": "monthly_output.csv",
                "rows": 3,
                "columns": 2,
                "min_key": "202606",
                "max_key": "202608",
                "sha256": "2" * 64,
                "business_hash": "5" * 64,
            },
            {
                "filename": "weekly_output.csv",
                "rows": 3,
                "columns": 2,
                "min_key": "202630",
                "max_key": "202632",
                "sha256": "3" * 64,
                "business_hash": "6" * 64,
            },
        ],
        "cutoff": {
            "feature_date": feature_date,
            "daily_cutoff_key": feature_date,
            "weekly_cutoff_key": "202631",
            "monthly_cutoff_key": "202607",
        },
    }


def _stable_databridge_authority(
    feature_date: str,
) -> signal_gap_plan.StableDataBridgeCurrentAuthority:
    payload = _databridge_authority_payload(feature_date)
    return signal_gap_plan.StableDataBridgeCurrentAuthority(
        authority_schema_version=str(
            payload["authority_schema_version"]
        ),
        generation_id=str(payload["generation_id"]),
        refresh_date=str(payload["refresh_date"]),
        schema_version=str(payload["schema_version"]),
        business_digest=str(payload["business_digest"]),
        files=tuple(
            StableDataBridgeFileIdentity(
                filename=str(item["filename"]),
                rows=int(item["rows"]),
                columns=int(item["columns"]),
                min_key=str(item["min_key"]),
                max_key=str(item["max_key"]),
                sha256=str(item["sha256"]),
                business_hash=str(item["business_hash"]),
            )
            for item in payload["files"]
        ),
        cutoffs=(
            StableDataBridgeCutoff(
                feature_date=feature_date,
                daily_cutoff_key=feature_date,
                weekly_cutoff_key="202631",
                monthly_cutoff_key="202607",
            ),
        ),
        publication_identity_sha256="e" * 64,
        stable_identity_sha256="c" * 64,
    )


class SignalGapPlanTests(unittest.TestCase):
    def test_authority_serializer_and_override_normalizer_round_trip(
        self,
    ) -> None:
        feature_date = "2026-08-03"
        payload = signal_gap_plan._databridge_authority_payload(
            _stable_databridge_authority(feature_date),
            feature_date=feature_date,
        )

        self.assertEqual(
            signal_gap_plan._normalize_databridge_authority_payload(
                payload,
                expected_feature_date=feature_date,
            ),
            payload,
        )

    def test_legacy_v1_authority_payload_remains_read_only_normalizable(
        self,
    ) -> None:
        """已归档 v4 plan 的 authority 能校验，不能被新写路径再生成。"""
        feature_date = "2026-08-03"
        payload = _databridge_authority_payload(feature_date)
        payload["authority_schema_version"] = (
            "stable-databridge-current-authority-v1"
        )
        payload["publication_capability"] = {
            "occurrence_id": 17,
            "business_date": "2026-08-04",
            "daily_coordinator_epoch": {
                "epoch": 3,
                "mode": "ledger",
                "record_sha256": "7" * 64,
            },
        }

        self.assertEqual(
            signal_gap_plan._normalize_databridge_authority_payload(
                payload,
                expected_feature_date=feature_date,
            ),
            payload,
        )

    def test_authority_serializer_rejects_noncanonical_text(
        self,
    ) -> None:
        authority = replace(
            _stable_databridge_authority("2026-08-03"),
            generation_id=" current-1 ",
        )

        with self.assertRaisesRegex(
            ValueError,
            "DataBridge current authority fence is invalid",
        ):
            signal_gap_plan._databridge_authority_payload(
                authority,
                feature_date="2026-08-03",
            )

    def test_frozen_authority_path_never_calls_mutable_resolver(
        self,
    ) -> None:
        feature_date = "2026-08-03"
        authority = _databridge_authority_payload(feature_date)
        with patch.object(
            signal_gap_plan,
            "resolve_stable_databridge_current_authority",
            side_effect=AssertionError("mutable resolver must not run"),
        ) as resolver:
            (
                current,
                error,
                overrides,
            ) = signal_gap_plan._resolve_snapshot_databridge_authority(
                scoped_blackbox_feature_dates=(feature_date,),
                required_databridge_feature_dates=(feature_date,),
                databridge_authority_overrides={
                    feature_date: authority
                },
                databridge_config=object(),
                connection=object(),
            )

        self.assertIsNone(current)
        self.assertIsNone(error)
        self.assertEqual(overrides, {feature_date: authority})
        resolver.assert_not_called()

    def test_frozen_authority_rejects_scope_external_override_date(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            signal_gap_plan.SignalGapPlanError,
            "DATABRIDGE_AUTHORITY_OVERRIDE_SCOPE_INVALID",
        ):
            signal_gap_plan._resolve_snapshot_databridge_authority(
                scoped_blackbox_feature_dates=("2026-08-03",),
                required_databridge_feature_dates=("2026-08-03",),
                databridge_authority_overrides={
                    "2026-08-02": _databridge_authority_payload(
                        "2026-08-02"
                    )
                },
                databridge_config=object(),
                connection=object(),
            )

    def test_frozen_authority_preserves_canonical_action_and_plan_sha(
        self,
    ) -> None:
        target = replace(
            _valid_target(),
            runtime_type="blackbox_v2",
            input_mode="databridge_v1",
        )
        case = signal_gap_plan.ExpectedSignalCase(
            registry_scheme_id=target.registry_scheme_id,
            base_scheme_id=target.base_scheme_id,
            runtime_type=target.runtime_type,
            frequency=target.frequency,
            task_type=target.task_type,
            target_tenor=target.target_tenor,
            horizon=target.horizon,
            predict_date="2026-08-04",
            feature_date="2026-08-03",
            target_date="2026-08-04",
            segment="live",
        )
        authority = _stable_databridge_authority(case.feature_date)
        payload = _databridge_authority_payload(case.feature_date)
        base_snapshot = signal_gap_plan.SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=(case,),
            canonical_signals=(),
            live_signals=(),
            input_generations=(),
            input_watermarks={},
            source_identity_sha256="1" * 64,
            discovery_identity_sha256="2" * 64,
            active_version_identity_sha256="3" * 64,
        )
        current_plan = signal_gap_plan.build_signal_gap_plan(
            replace(base_snapshot, databridge_authority=authority),
            start_date="2026-08-04",
            as_of_date="2026-08-05",
        )
        override_plan = signal_gap_plan.build_signal_gap_plan(
            replace(
                base_snapshot,
                databridge_authority_overrides={
                    case.feature_date: payload
                },
            ),
            start_date="2026-08-04",
            as_of_date="2026-08-05",
        )

        self.assertEqual(
            current_plan["actions"],
            override_plan["actions"],
        )
        self.assertEqual(
            current_plan["plan_sha256"],
            override_plan["plan_sha256"],
        )

    def test_frozen_databridge_authority_override_avoids_current_resolution(
        self,
    ) -> None:
        target = replace(
            _valid_target(),
            runtime_type="blackbox_v2",
            input_mode="databridge_v1",
        )
        case = signal_gap_plan.ExpectedSignalCase(
            registry_scheme_id=target.registry_scheme_id,
            base_scheme_id=target.base_scheme_id,
            runtime_type=target.runtime_type,
            frequency=target.frequency,
            task_type=target.task_type,
            target_tenor=target.target_tenor,
            horizon=target.horizon,
            predict_date="2026-08-04",
            feature_date="2026-08-03",
            target_date="2026-08-04",
            segment="live",
        )
        authority = _databridge_authority_payload(case.feature_date)
        snapshot = signal_gap_plan.SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=(case,),
            canonical_signals=(),
            live_signals=(),
            input_generations=(),
            input_watermarks={},
            source_identity_sha256="1" * 64,
            discovery_identity_sha256="2" * 64,
            active_version_identity_sha256="3" * 64,
        )
        seen: dict[str, object] = {}

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                return None

            def exec_driver_sql(self, statement: str) -> None:
                seen.setdefault("statements", []).append(statement)

            def rollback(self) -> None:
                seen["rolled_back"] = True

        class Engine:
            def connect(self) -> Connection:
                return Connection()

        def reader(
            _connection,
            *,
            databridge_authority_overrides,
            **_kwargs,
        ):
            seen["overrides"] = databridge_authority_overrides
            return replace(
                snapshot,
                databridge_authority_overrides=databridge_authority_overrides,
            )

        result = signal_gap_plan.plan_signal_gaps(
            Engine(),
            start_date="2026-08-04",
            as_of_date="2026-08-05",
            snapshot_reader=reader,
            execution_authority=(),
            databridge_config=object(),
            databridge_authority_overrides={case.feature_date: authority},
        )

        self.assertEqual(seen["overrides"], {case.feature_date: authority})
        self.assertTrue(seen["rolled_back"])
        self.assertEqual(result["actions"][0]["action"], "GRAY_LIVE_GAP")
        self.assertEqual(
            result["actions"][0]["input_authority"],
            authority,
        )

    def test_base_scheme_scope_is_forwarded_to_snapshot_reader(self) -> None:
        """base-only selection 也必须收窄 DataBridge authority 读取范围。"""
        target = replace(
            _valid_target(),
            runtime_type="blackbox_v2",
            input_mode="databridge_v1",
        )
        case = signal_gap_plan.ExpectedSignalCase(
            registry_scheme_id=target.registry_scheme_id,
            base_scheme_id=target.base_scheme_id,
            runtime_type=target.runtime_type,
            frequency=target.frequency,
            task_type=target.task_type,
            target_tenor=target.target_tenor,
            horizon=target.horizon,
            predict_date="2026-08-04",
            feature_date="2026-08-03",
            target_date="2026-08-04",
            segment="live",
        )
        snapshot = signal_gap_plan.SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=(case,),
            canonical_signals=(),
            live_signals=(),
            input_generations=(),
            input_watermarks={},
            source_identity_sha256="1" * 64,
            discovery_identity_sha256="2" * 64,
            active_version_identity_sha256="3" * 64,
            databridge_authority=_stable_databridge_authority(
                case.feature_date
            ),
        )
        seen: dict[str, object] = {}

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                return None

            def exec_driver_sql(self, _statement: str) -> None:
                return None

            def rollback(self) -> None:
                return None

        class Engine:
            def connect(self) -> Connection:
                return Connection()

        def reader(_connection, **kwargs):
            seen["scope"] = kwargs.get("scope")
            return snapshot

        scope = signal_gap_plan.SignalGapPlanScope(
            base_scheme_ids=("alpha",),
        )
        result = signal_gap_plan.plan_signal_gaps(
            Engine(),
            start_date="2026-08-04",
            as_of_date="2026-08-05",
            scope=scope,
            snapshot_reader=reader,
            execution_authority=(),
            databridge_config=object(),
        )

        self.assertEqual(seen["scope"], scope)
        self.assertEqual(result["actions"][0]["action"], "GRAY_LIVE_GAP")

    def test_target_date_task_scope_excludes_unrelated_future_t5(self) -> None:
        """受限 repair plan 只枚举目标日/T+1，不混入同预测日 T+5。"""
        t1_target = _valid_target()
        t5_target = replace(
            t1_target,
            registry_scheme_id="beta__h5__1Y",
            base_scheme_id="beta",
            task_type="T+5",
            horizon=5,
        )
        t1_cases = (
            signal_gap_plan.ExpectedSignalCase(
                registry_scheme_id=t1_target.registry_scheme_id,
                base_scheme_id=t1_target.base_scheme_id,
                runtime_type=t1_target.runtime_type,
                frequency=t1_target.frequency,
                task_type=t1_target.task_type,
                target_tenor=t1_target.target_tenor,
                horizon=t1_target.horizon,
                predict_date="2026-08-04",
                feature_date="2026-08-03",
                target_date="2026-08-04",
                segment="live",
            ),
            signal_gap_plan.ExpectedSignalCase(
                registry_scheme_id=t1_target.registry_scheme_id,
                base_scheme_id=t1_target.base_scheme_id,
                runtime_type=t1_target.runtime_type,
                frequency=t1_target.frequency,
                task_type=t1_target.task_type,
                target_tenor=t1_target.target_tenor,
                horizon=t1_target.horizon,
                predict_date="2026-08-05",
                feature_date="2026-08-04",
                target_date="2026-08-05",
                segment="live",
            ),
        )
        t5_case = signal_gap_plan.ExpectedSignalCase(
            registry_scheme_id=t5_target.registry_scheme_id,
            base_scheme_id=t5_target.base_scheme_id,
            runtime_type=t5_target.runtime_type,
            frequency=t5_target.frequency,
            task_type=t5_target.task_type,
            target_tenor=t5_target.target_tenor,
            horizon=t5_target.horizon,
            predict_date="2026-08-04",
            feature_date="2026-08-03",
            target_date="2026-08-10",
            segment="live",
        )
        present_t1 = tuple(
            signal_gap_plan.ObservedSignal(
                base_scheme_id=case.base_scheme_id,
                target_tenor=case.target_tenor,
                horizon=case.horizon,
                target_date=case.target_date,
                predict_date=case.predict_date,
                feature_date=case.feature_date,
                phase="gray_live",
                scheme_version=t1_target.scheme_version,
                run_status="success",
            )
            for case in t1_cases
        )
        unrelated_t5 = signal_gap_plan.ObservedSignal(
            base_scheme_id=t5_case.base_scheme_id,
            target_tenor=t5_case.target_tenor,
            horizon=t5_case.horizon,
            target_date=t5_case.target_date,
            predict_date=t5_case.predict_date,
            feature_date=t5_case.feature_date,
            phase="gray_live",
            scheme_version="wrong-version",
            run_status="failed",
        )
        snapshot = signal_gap_plan.SignalGapSnapshot(
            registry_targets=(t1_target, t5_target),
            expected_cases=(*t1_cases, t5_case),
            canonical_signals=(),
            live_signals=(*present_t1, unrelated_t5),
            input_generations=(),
            input_watermarks={},
            source_identity_sha256="1" * 64,
            discovery_identity_sha256="2" * 64,
            active_version_identity_sha256="3" * 64,
            control_plane_blockers=(
                {
                    "base_scheme_id": t5_target.base_scheme_id,
                    "code": "UNRELATED_T5_CONTROL_PLANE_BLOCKER",
                    "segment_scope": ["live"],
                },
            ),
        )

        scoped = signal_gap_plan.build_signal_gap_plan(
            snapshot,
            start_date="2026-08-04",
            as_of_date="2026-08-05",
            scope=signal_gap_plan.SignalGapPlanScope(
                target_date_start="2026-08-04",
                target_date_end="2026-08-05",
                task_types=("T+1",),
            ),
        )

        self.assertEqual(scoped["status"], "READY")
        self.assertEqual(scoped["counts"]["expected"], 2)
        self.assertEqual(scoped["counts"]["SKIP_PRESENT"], 2)
        self.assertEqual(scoped["observed_contract_anomalies"], [])
        self.assertEqual(scoped["control_plane"]["blockers"], [])
        self.assertEqual(
            scoped["selection"],
            {
                "target_date_start": "2026-08-04",
                "target_date_end": "2026-08-05",
                "task_types": ["T+1"],
                "base_scheme_ids": [],
            },
        )

    def test_base_scheme_scope_excludes_unrelated_active_blocker(
        self,
    ) -> None:
        """方案选择器必须在 action/blocker 构建前隔离其它 active 身份。"""
        target = _valid_target()
        unrelated_target = replace(
            target,
            registry_scheme_id="beta__h1__1Y",
            base_scheme_id="beta",
            scheme_version="beta-v1",
        )
        alpha_case = signal_gap_plan.ExpectedSignalCase(
            registry_scheme_id=target.registry_scheme_id,
            base_scheme_id=target.base_scheme_id,
            runtime_type=target.runtime_type,
            frequency=target.frequency,
            task_type=target.task_type,
            target_tenor=target.target_tenor,
            horizon=target.horizon,
            predict_date="2026-08-04",
            feature_date="2026-08-03",
            target_date="2026-08-04",
            segment="live",
        )
        beta_case = replace(
            alpha_case,
            registry_scheme_id=unrelated_target.registry_scheme_id,
            base_scheme_id=unrelated_target.base_scheme_id,
        )
        alpha_present = signal_gap_plan.ObservedSignal(
            base_scheme_id=alpha_case.base_scheme_id,
            target_tenor=alpha_case.target_tenor,
            horizon=alpha_case.horizon,
            target_date=alpha_case.target_date,
            predict_date=alpha_case.predict_date,
            feature_date=alpha_case.feature_date,
            phase="gray_live",
            scheme_version=target.scheme_version,
            run_status="success",
        )
        snapshot = signal_gap_plan.SignalGapSnapshot(
            registry_targets=(target, unrelated_target),
            expected_cases=(alpha_case, beta_case),
            canonical_signals=(),
            live_signals=(alpha_present,),
            input_generations=(),
            input_watermarks={},
            source_identity_sha256="1" * 64,
            discovery_identity_sha256="2" * 64,
            active_version_identity_sha256="3" * 64,
            control_plane_blockers=(
                {
                    "base_scheme_id": "beta",
                    "code": "UNRELATED_ACTIVE_BLOCKER",
                    "segment_scope": ["live"],
                },
            ),
        )

        scoped = signal_gap_plan.build_signal_gap_plan(
            snapshot,
            start_date="2026-08-04",
            as_of_date="2026-08-05",
            scope=signal_gap_plan.SignalGapPlanScope(
                base_scheme_ids=("alpha",),
            ),
        )

        self.assertEqual(scoped["status"], "READY")
        self.assertEqual(scoped["counts"]["expected"], 1)
        self.assertEqual(scoped["counts"]["SKIP_PRESENT"], 1)
        self.assertEqual(scoped["control_plane"]["blockers"], [])
        self.assertEqual(
            scoped["selection"],
            {
                "target_date_start": None,
                "target_date_end": None,
                "task_types": [],
                "base_scheme_ids": ["alpha"],
            },
        )

    def test_base_scheme_scope_rejects_unknown_active_identity(self) -> None:
        target = _valid_target()
        snapshot = signal_gap_plan.SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=(),
            canonical_signals=(),
            live_signals=(),
            input_generations=(),
            input_watermarks={},
            source_identity_sha256="1" * 64,
            discovery_identity_sha256="2" * 64,
            active_version_identity_sha256="3" * 64,
        )

        with self.assertRaises(
            signal_gap_plan.SignalGapPlanError
        ) as raised:
            signal_gap_plan.build_signal_gap_plan(
                snapshot,
                start_date="2026-08-04",
                as_of_date="2026-08-05",
                scope=signal_gap_plan.SignalGapPlanScope(
                    base_scheme_ids=("not_active",),
                ),
            )

        self.assertEqual(
            raised.exception.code,
            "UNKNOWN_ACTIVE_BASE_SCHEME_SCOPE",
        )

    def test_scope_is_canonical_and_bound_into_plan_sha(self) -> None:
        scope = signal_gap_plan.SignalGapPlanScope(
            target_date_start="2026-08-04",
            target_date_end="2026-08-05",
            task_types=("T+1", "T+1"),
        )

        self.assertEqual(
            signal_gap_plan.normalize_signal_gap_plan_scope(scope),
            signal_gap_plan.SignalGapPlanScope(
                target_date_start="2026-08-04",
                target_date_end="2026-08-05",
                task_types=("T+1",),
            ),
        )
        self.assertNotEqual(
            signal_gap_plan.canonical_plan_sha256(
                {"selection": scope.as_payload(), "actions": []}
            ),
            signal_gap_plan.canonical_plan_sha256(
                {
                    "selection": {
                        "target_date_start": "2026-08-04",
                        "target_date_end": "2026-08-05",
                        "task_types": ["T+5"],
                        "base_scheme_ids": [],
                    },
                    "actions": [],
                }
            ),
        )
        self.assertNotEqual(
            signal_gap_plan.canonical_plan_sha256(
                {
                    "selection": signal_gap_plan.SignalGapPlanScope(
                        base_scheme_ids=("alpha",),
                    ).as_payload(),
                    "actions": [],
                }
            ),
            signal_gap_plan.canonical_plan_sha256(
                {
                    "selection": signal_gap_plan.SignalGapPlanScope(
                        base_scheme_ids=("beta",),
                    ).as_payload(),
                    "actions": [],
                }
            ),
        )
        self.assertEqual(
            signal_gap_plan.normalize_signal_gap_plan_scope(
                signal_gap_plan.SignalGapPlanScope(
                    task_types=(
                        "T+1",
                        "T+5",
                        "weekly_point",
                        "weekly_average",
                        "monthly",
                    ),
                )
            ),
            signal_gap_plan.SignalGapPlanScope(),
        )
        self.assertEqual(
            signal_gap_plan.normalize_signal_gap_plan_scope(
                signal_gap_plan.SignalGapPlanScope(
                    base_scheme_ids=("beta", "alpha", "beta"),
                )
            ),
            signal_gap_plan.SignalGapPlanScope(
                base_scheme_ids=("alpha", "beta"),
            ),
        )

    def test_scope_requires_complete_target_date_bounds(self) -> None:
        with self.assertRaises(
            signal_gap_plan.SignalGapPlanError
        ) as raised:
            signal_gap_plan.normalize_signal_gap_plan_scope(
                {"target_date_start": "2026-08-04"}
            )

        self.assertEqual(raised.exception.code, "INVALID_PLAN_SCOPE")

    def test_restricted_scope_rejects_an_empty_case_selection(self) -> None:
        target = _valid_target()
        snapshot = signal_gap_plan.SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=(
                signal_gap_plan.ExpectedSignalCase(
                    registry_scheme_id=target.registry_scheme_id,
                    base_scheme_id=target.base_scheme_id,
                    runtime_type=target.runtime_type,
                    frequency=target.frequency,
                    task_type=target.task_type,
                    target_tenor=target.target_tenor,
                    horizon=target.horizon,
                    predict_date="2026-08-04",
                    feature_date="2026-08-03",
                    target_date="2026-08-04",
                    segment="live",
                ),
            ),
            canonical_signals=(),
            live_signals=(),
            input_generations=(),
            input_watermarks={},
            source_identity_sha256="1" * 64,
            discovery_identity_sha256="2" * 64,
            active_version_identity_sha256="3" * 64,
        )

        with self.assertRaises(
            signal_gap_plan.SignalGapPlanError
        ) as raised:
            signal_gap_plan.build_signal_gap_plan(
                snapshot,
                start_date="2026-08-04",
                as_of_date="2026-08-05",
                scope=signal_gap_plan.SignalGapPlanScope(
                    target_date_start="2026-08-05",
                    target_date_end="2026-08-05",
                    task_types=("T+1",),
                ),
            )

        self.assertEqual(raised.exception.code, "EMPTY_PLAN_SELECTION")

    def test_read_registry_versions_accepts_53_targets_and_50_executions(
        self,
    ) -> None:
        registry_rows, version_rows, execution_authority = (
            _active_scope_fixture()
        )

        try:
            raw_rows, targets, blockers, active_version_digest = (
                signal_gap_plan._read_registry_versions(
                    _Connection(registry_rows, version_rows),
                    execution_authority=execution_authority,
                )
            )
        except signal_gap_plan.SignalGapPlanError as exc:
            self.fail(
                "valid dynamic active scope was rejected: "
                f"{exc.code}"
            )

        self.assertEqual(len(raw_rows), 53)
        self.assertEqual(len(targets), 53)
        self.assertEqual(
            len({target.base_scheme_id for target in targets}),
            50,
        )
        self.assertEqual(
            {
                frequency: sum(
                    target.frequency == frequency for target in targets
                )
                for frequency in ("daily", "weekly", "monthly")
            },
            {"daily": 32, "weekly": 13, "monthly": 8},
        )
        self.assertEqual(blockers, ())
        self.assertTrue(active_version_digest)

    def test_read_registry_versions_rejects_empty_active_registry(
        self,
    ) -> None:
        with self.assertRaises(
            signal_gap_plan.SignalGapPlanError
        ) as raised:
            signal_gap_plan._read_registry_versions(
                _Connection([], []),
                execution_authority=(),
            )

        self.assertEqual(raised.exception.code, "NO_ACTIVE_REGISTRY_TARGETS")

    def test_registry_target_task_contract_remains_fail_closed(self) -> None:
        invalid = replace(_valid_target(), horizon=5)

        with self.assertRaises(
            signal_gap_plan.SignalGapPlanError
        ) as raised:
            signal_gap_plan._validate_registry_target(invalid)

        self.assertEqual(raised.exception.code, "INVALID_REGISTRY_TARGET")
        self.assertEqual(
            raised.exception.detail,
            "task contract drift for alpha__h1__1Y",
        )
