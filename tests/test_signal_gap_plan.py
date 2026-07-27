from __future__ import annotations

import hashlib
import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import Mock, patch


class _Connection:
    def __init__(self) -> None:
        self.driver_sql: list[str] = []
        self.rollback_count = 0
        self.commit_count = 0

    def exec_driver_sql(self, statement: str):
        self.driver_sql.append(statement)

    def rollback(self) -> None:
        self.rollback_count += 1

    def commit(self) -> None:
        self.commit_count += 1


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one(self):
        if len(self._rows) != 1:
            raise AssertionError(f"expected one row, got {len(self._rows)}")
        return self._rows[0]


class _ReaderConnection:
    def __init__(self, responses):
        self.responses = responses
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        for marker, rows in self.responses:
            if marker in sql:
                return _Rows(rows)
        raise AssertionError(f"unexpected SQL: {sql}")


class _Calendar:
    daily_predict_dates = ("2026-06-01",)
    weekly_predict_dates = ("2026-05-30",)
    monthly_predict_dates = ("2026-05-15",)


class SignalGapPlanTests(unittest.TestCase):
    def _types(self):
        from harness.signal_gap_plan import (
            DiscoveredSchemeIdentity,
            ExpectedSignalCase,
            InputGeneration,
            ObservedSignal,
            RegistryTarget,
            SignalGapSnapshot,
        )

        return (
            DiscoveredSchemeIdentity,
            ExpectedSignalCase,
            InputGeneration,
            ObservedSignal,
            RegistryTarget,
            SignalGapSnapshot,
        )

    def _snapshot(self):
        (
            DiscoveredSchemeIdentity,
            ExpectedSignalCase,
            InputGeneration,
            ObservedSignal,
            RegistryTarget,
            SignalGapSnapshot,
        ) = self._types()
        target = RegistryTarget(
            registry_scheme_id="demo__h5__10Y",
            base_scheme_id="demo",
            runtime_type="blackbox_v2",
            frequency="daily",
            task_type="T+5",
            target_tenor="10Y",
            horizon=5,
            scheme_version="version-1",
            live_target_start_date="2026-06-01",
            live_boundary_source="platform_live_boundary_v1",
            input_mode="databridge_v1",
            code_sha256="c" * 64,
            config_sha256="d" * 64,
        )
        cases = (
            ExpectedSignalCase(
                registry_scheme_id=target.registry_scheme_id,
                base_scheme_id=target.base_scheme_id,
                runtime_type=target.runtime_type,
                frequency=target.frequency,
                task_type=target.task_type,
                target_tenor=target.target_tenor,
                horizon=target.horizon,
                predict_date="2026-05-22",
                feature_date="2026-05-22",
                target_date="2026-05-29",
                segment="canonical",
            ),
            ExpectedSignalCase(
                registry_scheme_id=target.registry_scheme_id,
                base_scheme_id=target.base_scheme_id,
                runtime_type=target.runtime_type,
                frequency=target.frequency,
                task_type=target.task_type,
                target_tenor=target.target_tenor,
                horizon=target.horizon,
                predict_date="2026-05-26",
                feature_date="2026-05-25",
                target_date="2026-06-01",
                segment="live",
            ),
            ExpectedSignalCase(
                registry_scheme_id=target.registry_scheme_id,
                base_scheme_id=target.base_scheme_id,
                runtime_type=target.runtime_type,
                frequency=target.frequency,
                task_type=target.task_type,
                target_tenor=target.target_tenor,
                horizon=target.horizon,
                predict_date="2026-05-27",
                feature_date="2026-05-26",
                target_date="2026-06-02",
                segment="live",
            ),
            ExpectedSignalCase(
                registry_scheme_id=target.registry_scheme_id,
                base_scheme_id=target.base_scheme_id,
                runtime_type=target.runtime_type,
                frequency=target.frequency,
                task_type=target.task_type,
                target_tenor=target.target_tenor,
                horizon=target.horizon,
                predict_date="2026-05-28",
                feature_date="2026-05-27",
                target_date="2026-06-03",
                segment="live",
            ),
            ExpectedSignalCase(
                registry_scheme_id=target.registry_scheme_id,
                base_scheme_id=target.base_scheme_id,
                runtime_type=target.runtime_type,
                frequency=target.frequency,
                task_type=target.task_type,
                target_tenor=target.target_tenor,
                horizon=target.horizon,
                predict_date="2026-05-29",
                feature_date="2026-05-28",
                target_date="2026-06-04",
                segment="live",
                data_contract_error="WEEK_CALENDAR_CONTRACT",
            ),
        )
        return SignalGapSnapshot(
            registry_targets=(target,),
            expected_cases=cases,
            canonical_signals=(),
            live_signals=(
                ObservedSignal(
                    base_scheme_id="demo",
                    target_tenor="10Y",
                    horizon=5,
                    target_date="2026-06-01",
                    predict_date="2026-05-26",
                    feature_date="2026-05-25",
                    phase="gray_live",
                    scheme_version="version-1",
                    run_status="success",
                ),
            ),
            input_generations=(
                InputGeneration(
                    generation_id="db-20260526",
                    generation_type="databridge_v1",
                    state="SEALED",
                    feature_date="2026-05-26",
                    business_date="2026-05-27",
                    manifest_sha256="a" * 64,
                    parent_generation_id="native-20260526",
                    parent_manifest_sha256="c" * 64,
                    dataset_content_id="dataset-1",
                    source_commit_token="commit-1",
                    cutoff_feature_dates=("2026-05-26",),
                ),
            ),
            input_watermarks={
                "trade_calendar_max": "2026-07-31",
                "daily_source_max": "2026-07-27",
            },
            source_identity_sha256="b" * 64,
            discovery_identity_sha256="e" * 64,
        )

    def test_actions_use_business_key_and_fail_closed_readiness(self) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        plan = build_signal_gap_plan(
            self._snapshot(),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        by_target = {
            item["target_date"]: item["action"] for item in plan["actions"]
        }
        self.assertEqual(
            by_target,
            {
                "2026-05-29": "FULL_CANONICAL_RUN_REQUIRED",
                "2026-06-01": "SKIP_PRESENT",
                "2026-06-02": "GRAY_LIVE_GAP",
                "2026-06-03": "BLOCKED_DATA_CONTRACT",
                "2026-06-04": "BLOCKED_DATA_CONTRACT",
            },
        )
        self.assertEqual(plan["counts"]["open_gap"], 4)
        self.assertEqual(plan["counts"]["expected_total"], 5)
        self.assertEqual(plan["counts"]["present"], 1)
        self.assertEqual(plan["counts"]["actionable"], 2)
        self.assertEqual(plan["counts"]["blocked"], 2)
        self.assertEqual(
            plan["counts"]["expected_total"],
            plan["counts"]["present"] + plan["counts"]["open_gap"],
        )
        self.assertEqual(plan["counts"]["SKIP_PRESENT"], 1)
        self.assertEqual(plan["counts"]["active_target"], 1)
        self.assertEqual(plan["scope"]["execution_count"], 1)
        self.assertTrue(plan["control_plane"]["read_only"])
        self.assertEqual(
            plan["control_plane"]["as_of_semantics"],
            "predict_date_lte_as_of_date",
        )
        self.assertEqual(plan["source_snapshot"]["canonical_row_count"], 0)
        self.assertEqual(plan["source_snapshot"]["live_row_count"], 1)

    def test_plan_is_deterministic_and_sha_excludes_its_own_field(self) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        first = build_signal_gap_plan(
            snapshot,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        second = build_signal_gap_plan(
            replace(
                snapshot,
                expected_cases=tuple(reversed(snapshot.expected_cases)),
                live_signals=tuple(reversed(snapshot.live_signals)),
                input_generations=tuple(
                    reversed(snapshot.input_generations)
                ),
                input_watermarks=dict(
                    reversed(tuple(snapshot.input_watermarks.items()))
                ),
            ),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertEqual(first, second)
        unsigned = dict(first)
        plan_sha256 = unsigned.pop("plan_sha256")
        encoded = json.dumps(
            unsigned,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(plan_sha256, hashlib.sha256(encoded).hexdigest())
        self.assertEqual(first["schema_version"], "active-signal-gap-plan-v1")

    def test_duplicate_active_business_target_is_rejected(self) -> None:
        from harness.signal_gap_plan import SignalGapPlanError, build_signal_gap_plan

        snapshot = self._snapshot()
        duplicate = replace(
            snapshot.registry_targets[0],
        )
        with self.assertRaisesRegex(
            SignalGapPlanError,
            "duplicate active business target",
        ):
            build_signal_gap_plan(
                replace(
                    snapshot,
                    registry_targets=(
                        *snapshot.registry_targets,
                        duplicate,
                    ),
                ),
                start_date="2025-01-01",
                as_of_date="2026-07-27",
            )

    def test_present_business_key_with_date_or_version_drift_is_blocked(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        drifted = replace(
            snapshot.live_signals[0],
            feature_date="2026-05-24",
            scheme_version="old-version",
        )
        plan = build_signal_gap_plan(
            replace(snapshot, live_signals=(drifted,)),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        action = next(
            row
            for row in plan["actions"]
            if row["target_date"] == "2026-06-01"
        )
        self.assertEqual(action["action"], "BLOCKED_DATA_CONTRACT")
        self.assertIn("OBSERVED_SIGNAL_CONTRACT_DRIFT", action["reason"])

    def test_generation_present_but_invalid_is_data_contract_blocked(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        invalid = replace(
            snapshot.input_generations[0],
            state="INVALIDATED",
        )
        plan = build_signal_gap_plan(
            replace(snapshot, input_generations=(invalid,)),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        action = next(
            row
            for row in plan["actions"]
            if row["target_date"] == "2026-06-02"
        )
        self.assertEqual(action["action"], "BLOCKED_DATA_CONTRACT")
        self.assertEqual(
            action["reason"],
            "GENERATION_CONTRACT_INVALID",
        )

    def test_missing_generation_is_distinct_from_invalid_generation(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        plan = build_signal_gap_plan(
            replace(snapshot, input_generations=()),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        action = next(
            row
            for row in plan["actions"]
            if row["target_date"] == "2026-06-02"
        )
        self.assertEqual(action["action"], "BLOCKED_NO_GENERATION")
        self.assertEqual(action["reason"], "NO_DATABRIDGE_GENERATION")

    def test_segment_date_semantics_and_platform_boundary_are_enforced(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            SignalGapPlanError,
            build_signal_gap_plan,
        )

        snapshot = self._snapshot()
        invalid_case = replace(
            snapshot.expected_cases[0],
            predict_date="2026-05-21",
        )
        with self.assertRaisesRegex(
            SignalGapPlanError,
            "canonical predict_date must equal feature_date",
        ):
            build_signal_gap_plan(
                replace(
                    snapshot,
                    expected_cases=(
                        invalid_case,
                        *snapshot.expected_cases[1:],
                    ),
                ),
                start_date="2025-01-01",
                as_of_date="2026-07-27",
            )

    def test_persisted_run_manifest_must_cover_exact_target_multiplicity(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            SignalGapPlanError,
            _validate_canonical_run_manifest,
        )

        details = [
            {
                "target_tenor": "5Y",
                "horizon": 5,
                "target_date": "2025-01-08",
            },
            {
                "target_tenor": "10Y",
                "horizon": 5,
                "target_date": "2025-01-08",
            },
        ]
        _validate_canonical_run_manifest(
            run_id=7,
            summary={"row_count": 2},
            details=details,
            expected_target_pairs={("5Y", 5), ("10Y", 5)},
        )
        with self.assertRaisesRegex(
            SignalGapPlanError,
            "manifest/detail count mismatch",
        ):
            _validate_canonical_run_manifest(
                run_id=7,
                summary={"row_count": 3},
                details=details,
                expected_target_pairs={("5Y", 5), ("10Y", 5)},
            )
        with self.assertRaisesRegex(
            SignalGapPlanError,
            "target multiplicity mismatch",
        ):
            _validate_canonical_run_manifest(
                run_id=7,
                summary={"row_count": 2},
                details=details,
                expected_target_pairs={("5Y", 5)},
            )

    def test_run_149_manifest_accepts_all_approved_count_shapes(self) -> None:
        from harness.signal_gap_plan import (
            SignalGapPlanError,
            _validate_canonical_run_manifest,
        )

        details = [
            {
                "target_tenor": "1Y",
                "horizon": 30,
                "target_date": f"2025-{month:02d}-14",
            }
            for month in range(1, 10)
        ]
        details.extend(
            {
                "target_tenor": "1Y",
                "horizon": 30,
                "target_date": f"2025-{month:02d}-14",
            }
            for month in range(10, 13)
        )
        details.extend(
            {
                "target_tenor": "1Y",
                "horizon": 30,
                "target_date": f"2026-{month:02d}-14",
            }
            for month in range(1, 7)
        )
        self.assertEqual(len(details), 18)

        for field in (
            "persisted_prediction_count",
            "row_count",
            "written_predictions",
            "rows",
        ):
            _validate_canonical_run_manifest(
                run_id=149,
                summary={field: 18},
                details=details,
                expected_target_pairs={("1Y", 30)},
            )
        _validate_canonical_run_manifest(
            run_id=149,
            summary={
                "persisted_prediction_count": 18,
                "row_count": 18,
                "written_predictions": 18,
                "rows": 18,
            },
            details=details,
            expected_target_pairs={("1Y", 30)},
        )
        with self.assertRaisesRegex(
            SignalGapPlanError,
            "manifest count fields disagree",
        ):
            _validate_canonical_run_manifest(
                run_id=149,
                summary={"written_predictions": 18, "rows": 17},
                details=details,
                expected_target_pairs={("1Y", 30)},
            )

    def test_runtime_specific_native_task_contracts_are_accepted(self) -> None:
        from harness.signal_gap_plan import (
            RegistryTarget,
            _validate_registry_target,
        )

        for task_type, frequency, horizon in (
            ("weekly_point", "weekly", 6),
            ("weekly_average", "weekly", 6),
            ("monthly", "monthly", 30),
        ):
            target = RegistryTarget(
                registry_scheme_id=f"native_{frequency}__h{horizon}__10Y",
                base_scheme_id=f"native_{frequency}",
                runtime_type="native_adapter",
                frequency=frequency,
                task_type=task_type,
                target_tenor="10Y",
                horizon=horizon,
                scheme_version="native-version",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
                input_mode="generation_v1",
                code_sha256="1" * 64,
                config_sha256="2" * 64,
            )
            _validate_registry_target(target)

    def test_registry_and_version_authorities_freeze_44_targets(self) -> None:
        from harness.signal_gap_plan import _read_registry_versions

        registry_rows, version_rows = _production_registry_rows()
        execution_authority = _execution_authority(version_rows)
        connection = _ReaderConnection(
            (
                ("FROM t_scheme_registry", registry_rows),
                ("FROM t_scheme_versions", version_rows),
            )
        )

        raw, targets, blockers = _read_registry_versions(
            connection,
            execution_authority=execution_authority,
        )

        self.assertEqual(len(raw), 44)
        self.assertEqual(len(targets), 44)
        self.assertEqual(blockers, ())
        self.assertEqual(
            len({target.base_scheme_id for target in targets}),
            40,
        )
        self.assertEqual(
            {
                frequency: sum(
                    target.frequency == frequency for target in targets
                )
                for frequency in ("daily", "weekly", "monthly")
            },
            {"daily": 29, "weekly": 7, "monthly": 8},
        )
        self.assertEqual(len(connection.statements), 2)
        self.assertNotIn("JOIN", connection.statements[0].upper())
        self.assertNotIn("JOIN", connection.statements[1].upper())
        self.assertTrue(
            all(target.input_mode == "generation_v1" for target in targets)
        )

    def test_active_registry_blockers_preserve_the_complete_plan(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _read_registry_versions,
        )

        registry_rows, version_rows = _production_registry_rows()
        execution_authority = _execution_authority(version_rows)
        cases = (
            (
                version_rows[:-1],
                "ACTIVE_VERSION_EXACT_IDENTITY_MISSING",
            ),
            ((
                *version_rows,
                {
                    **version_rows[0],
                    "scheme_version": "second-active-version",
                },
            ), "ACTIVE_VERSION_CARDINALITY_INVALID"),
            ((
                {
                    **version_rows[0],
                    "runtime_type": "blackbox_v2",
                },
                *version_rows[1:],
            ), "REGISTRY_VERSION_RUNTIME_DRIFT"),
        )
        for rows, expected_code in cases:
            connection = _ReaderConnection(
                (
                    ("FROM t_scheme_registry", registry_rows),
                    ("FROM t_scheme_versions", rows),
                )
            )
            raw, targets, blockers = _read_registry_versions(
                connection,
                execution_authority=execution_authority,
            )
            self.assertEqual(len(raw), 44)
            self.assertEqual(len(targets), 44)
            self.assertIn(
                expected_code,
                {blocker["code"] for blocker in blockers},
            )

    def test_control_plane_blocker_blocks_every_affected_base_action(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = replace(
            self._snapshot(),
            control_plane_blockers=(
                {
                    "code": "ACTIVE_VERSION_CARDINALITY_INVALID",
                    "base_scheme_id": "demo",
                    "active_version_count": 2,
                },
            ),
        )

        plan = build_signal_gap_plan(
            snapshot,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertEqual(plan["status"], "BLOCKED")
        self.assertEqual(
            plan["control_plane"]["blockers"],
            [
                {
                    "code": "ACTIVE_VERSION_CARDINALITY_INVALID",
                    "base_scheme_id": "demo",
                    "active_version_count": 2,
                }
            ],
        )
        self.assertTrue(
            all(
                action["action"] == "BLOCKED_DATA_CONTRACT"
                for action in plan["actions"]
            )
        )
        self.assertEqual(plan["counts"]["present"], 0)

        empty_plan = build_signal_gap_plan(
            replace(
                snapshot,
                expected_cases=(),
                canonical_signals=(),
                live_signals=(),
            ),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        self.assertEqual(empty_plan["status"], "BLOCKED")

    def test_discovery_digest_is_bound_into_plan_sha(self) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        first = build_signal_gap_plan(
            snapshot,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        second = build_signal_gap_plan(
            replace(snapshot, discovery_identity_sha256="f" * 64),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertEqual(
            first["source_snapshot"]["discovery_identity_sha256"],
            "e" * 64,
        )
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])

    def test_registry_digest_binds_exact_execution_identity(self) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        first = build_signal_gap_plan(
            snapshot,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        changed_target = replace(
            snapshot.registry_targets[0],
            code_sha256="e" * 64,
        )
        second = build_signal_gap_plan(
            replace(snapshot, registry_targets=(changed_target,)),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertRegex(
            first["source_snapshot"]["registry_digest_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertNotEqual(
            first["source_snapshot"]["registry_digest_sha256"],
            second["source_snapshot"]["registry_digest_sha256"],
        )

    def test_live_reader_preserves_invalid_rows_and_validates_run_identity(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            RegistryTarget,
            _read_live_signals,
        )

        target = RegistryTarget(
            registry_scheme_id="demo__h5__10Y",
            base_scheme_id="demo",
            runtime_type="blackbox_v2",
            frequency="daily",
            task_type="T+5",
            target_tenor="10Y",
            horizon=5,
            scheme_version="version-1",
            live_target_start_date="2026-06-01",
            live_boundary_source="platform_live_boundary_v1",
            input_mode="databridge_v1",
            code_sha256="a" * 64,
            config_sha256="b" * 64,
        )
        common = {
            "scheme_id": "demo",
            "target_tenor": "10Y",
            "horizon": 5,
            "target_date": "2026-06-01",
            "feature_date": "2026-05-25",
            "scheme_version": "version-1",
        }
        rows = [
            {
                **common,
                "id": 1,
                "predict_date": "2026-05-26",
                "prediction_phase": "gray_live",
                "run_id": 101,
                "run_status": "success",
                "run_scheme_id": "demo",
                "run_scheme_version": "version-1",
                "run_runtime_type": "blackbox_v2",
                "run_prediction_phase": "gray_live",
                "run_predict_date": "2026-05-26",
            },
            {
                **common,
                "id": 2,
                "predict_date": "2026-05-24",
                "prediction_phase": "shadow",
                "run_id": 102,
                "run_status": "failed",
                "run_scheme_id": "other",
                "run_scheme_version": "old-version",
                "run_runtime_type": "native_adapter",
                "run_prediction_phase": "scheduled_live",
                "run_predict_date": "2026-05-23",
            },
        ]
        connection = _ReaderConnection(
            (("FROM t_scheme_predictions", rows),)
        )

        raw, signals = _read_live_signals(
            connection,
            (target,),
            as_of_date="2026-07-27",
        )

        self.assertEqual(len(raw), 2)
        self.assertEqual(len(signals), 2)
        self.assertIsNone(signals[0].contract_error)
        self.assertIn("LIVE_PHASE_INVALID", signals[1].contract_error or "")
        self.assertIn(
            "RUN_SCHEME_ID_DRIFT",
            signals[1].contract_error or "",
        )
        sql = connection.statements[0]
        self.assertNotIn("prediction_phase IN", sql)
        for field in (
            "run_scheme_id",
            "run_scheme_version",
            "run_runtime_type",
            "run_prediction_phase",
            "run_predict_date",
        ):
            self.assertIn(field, sql)

    def test_read_occurs_in_one_repeatable_read_only_snapshot_and_rolls_back(
        self,
    ) -> None:
        from harness.signal_gap_plan import plan_signal_gaps

        connection = _Connection()
        engine = _Engine(connection)
        reader = Mock(return_value=self._snapshot())

        execution_authority = _demo_execution_authority()
        result = plan_signal_gaps(
            engine,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
            snapshot_reader=reader,
            execution_authority=execution_authority,
        )

        self.assertEqual(result["counts"]["active_target"], 1)
        reader.assert_called_once_with(
            connection,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
            execution_authority=execution_authority,
            discovery_identity_sha256=unittest.mock.ANY,
        )
        self.assertEqual(
            connection.driver_sql,
            [
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
            ],
        )
        self.assertEqual(connection.rollback_count, 1)
        self.assertEqual(connection.commit_count, 0)

    def test_snapshot_reader_failure_rolls_back_and_fails_closed(self) -> None:
        from harness.signal_gap_plan import SignalGapPlanError, plan_signal_gaps

        connection = _Connection()
        engine = _Engine(connection)

        with self.assertRaisesRegex(
            SignalGapPlanError,
            "CASE_BUILDER_FAILED",
        ):
            plan_signal_gaps(
                engine,
                start_date="2025-01-01",
                as_of_date="2026-07-27",
                snapshot_reader=Mock(side_effect=ValueError("bad calendar")),
                execution_authority=_demo_execution_authority(),
            )

        self.assertEqual(connection.rollback_count, 1)
        self.assertEqual(connection.commit_count, 0)

    def test_live_cases_reuse_daily_weekly_and_monthly_context_builders(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            RegistryTarget,
            build_expected_live_cases,
        )

        targets = (
            RegistryTarget(
                registry_scheme_id="daily__h5__10Y",
                base_scheme_id="daily",
                runtime_type="blackbox_v2",
                frequency="daily",
                task_type="T+5",
                target_tenor="10Y",
                horizon=5,
                scheme_version="daily-version",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
            ),
            RegistryTarget(
                registry_scheme_id="weekly__h1__10Y",
                base_scheme_id="weekly",
                runtime_type="blackbox_v2",
                frequency="weekly",
                task_type="weekly_point",
                target_tenor="10Y",
                horizon=1,
                scheme_version="weekly-version",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
            ),
            RegistryTarget(
                registry_scheme_id="monthly__h1__10Y",
                base_scheme_id="monthly",
                runtime_type="blackbox_v2",
                frequency="monthly",
                task_type="monthly",
                target_tenor="10Y",
                horizon=1,
                scheme_version="monthly-version",
                live_target_start_date="2026-06-01",
                live_boundary_source="platform_live_boundary_v1",
            ),
        )
        daily = Mock(
            return_value=Mock(
                feature_date="2026-05-25",
                target_date="2026-06-01",
            )
        )
        weekly = Mock(
            return_value=Mock(
                feature_date="2026-05-29",
                target_date="2026-06-05",
            )
        )
        monthly = Mock(
            return_value=Mock(
                feature_date="2026-05-15",
                target_date="2026-06-15",
            )
        )

        with (
            patch(
                "harness.signal_gap_plan.build_daily_live_context",
                daily,
            ),
            patch(
                "harness.signal_gap_plan.build_weekly_live_context",
                weekly,
            ),
            patch(
                "harness.signal_gap_plan.build_monthly_live_context",
                monthly,
            ),
        ):
            cases = build_expected_live_cases(
                targets,
                calendar=_Calendar(),
                as_of_date="2026-06-01",
                live_target_start_date="2026-06-01",
            )

        self.assertEqual(len(cases), 3)
        daily.assert_called_once_with(
            unittest.mock.ANY,
            "2026-06-01",
            horizon=5,
        )
        weekly.assert_called_once_with(unittest.mock.ANY, "2026-05-30")
        monthly.assert_called_once_with(unittest.mock.ANY, "2026-05-15")


class SignalGapPlanCliTests(unittest.TestCase):
    def test_production_reader_source_contains_no_write_or_lock_primitive(
        self,
    ) -> None:
        import inspect

        from harness import signal_gap_plan

        source = inspect.getsource(signal_gap_plan.read_signal_gap_snapshot)
        module_source = inspect.getsource(signal_gap_plan)
        self.assertIn(
            "read_calendar_snapshot_from_connection(connection)",
            source,
        )
        self.assertNotIn("engine.begin(", module_source)
        self.assertNotIn("FOR UPDATE", module_source.upper())
        self.assertNotIn("INSERT INTO", module_source.upper())
        self.assertNotIn("UPDATE t_", module_source)
        self.assertNotIn("DELETE FROM", module_source.upper())

    def test_cli_exposes_json_only_read_command(self) -> None:
        from harness.cli import _build_parser

        args = _build_parser().parse_args(
            [
                "signal-gap-plan",
                "--start",
                "2025-01-01",
                "--as-of",
                "2026-07-27",
                "--format",
                "json",
            ]
        )
        self.assertEqual(args.command, "signal-gap-plan")
        self.assertEqual(args.start, "2025-01-01")
        self.assertEqual(args.as_of, "2026-07-27")
        self.assertEqual(args.format, "json")
        self.assertFalse(hasattr(args, "persist"))
        self.assertFalse(hasattr(args, "authorize"))

    def test_cli_prints_exact_plan_without_writing(self) -> None:
        from harness.cli import main

        expected = {
            "schema_version": "active-signal-gap-plan-v1",
            "plan_sha256": "a" * 64,
        }
        engine = Mock()
        with (
            patch(
                "harness.cli.create_engine_from_env",
                return_value=engine,
            ),
            patch(
                "harness.cli.plan_signal_gaps",
                return_value=expected,
            ) as planner,
            patch("builtins.print") as output,
        ):
            result = main(
                [
                    "signal-gap-plan",
                    "--start",
                    "2025-01-01",
                    "--as-of",
                    "2026-07-27",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 0)
        planner.assert_called_once_with(
            engine,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        engine.dispose.assert_called_once_with()
        self.assertEqual(json.loads(output.call_args.args[0]), expected)

    def test_cli_returns_one_for_a_valid_plan_with_blocked_actions(self) -> None:
        from harness.cli import main

        expected = {
            "schema_version": "active-signal-gap-plan-v1",
            "plan_sha256": "a" * 64,
            "counts": {"blocked": 1},
        }
        engine = Mock()
        with (
            patch(
                "harness.cli.create_engine_from_env",
                return_value=engine,
            ),
            patch(
                "harness.cli.plan_signal_gaps",
                return_value=expected,
            ),
            patch("builtins.print"),
        ):
            result = main(
                [
                    "signal-gap-plan",
                    "--start",
                    "2025-01-01",
                    "--as-of",
                    "2026-07-27",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 1)

    def test_cli_returns_one_for_control_plane_blocked_empty_plan(self) -> None:
        from harness.cli import main

        expected = {
            "schema_version": "active-signal-gap-plan-v1",
            "status": "BLOCKED",
            "plan_sha256": "a" * 64,
            "counts": {"blocked": 0},
        }
        engine = Mock()
        with (
            patch(
                "harness.cli.create_engine_from_env",
                return_value=engine,
            ),
            patch(
                "harness.cli.plan_signal_gaps",
                return_value=expected,
            ),
            patch("builtins.print"),
        ):
            result = main(
                [
                    "signal-gap-plan",
                    "--start",
                    "2025-01-01",
                    "--as-of",
                    "2026-07-27",
                ]
            )

        self.assertEqual(result, 1)

    def test_cli_structures_engine_and_transaction_setup_failures(self) -> None:
        from harness.cli import main

        for failure in (
            RuntimeError("secret-dsn-password"),
            None,
        ):
            engine = Mock()
            if failure is None:
                engine.connect.side_effect = RuntimeError(
                    "secret-transaction-password"
                )
            with (
                patch(
                    "harness.cli.create_engine_from_env",
                    side_effect=failure,
                    return_value=engine,
                ),
                patch("builtins.print") as output,
            ):
                result = main(
                    [
                        "signal-gap-plan",
                        "--start",
                        "2025-01-01",
                        "--as-of",
                        "2026-07-27",
                        "--format",
                        "json",
                    ]
                )

            self.assertEqual(result, 2)
            payload = json.loads(output.call_args.args[0])
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(
                payload["failure_code"],
                "SIGNAL_GAP_PLAN_INTERNAL_ERROR",
            )
            self.assertNotIn("secret", json.dumps(payload))


def _production_registry_rows():
    registry_rows = []
    version_rows = []
    tenors = ("1Y", "3Y", "5Y", "7Y", "10Y")
    for index in range(25):
        base_id = f"daily_{index}"
        target_tenors = tenors if index == 0 else ("1Y",)
        for tenor in target_tenors:
            registry_rows.append(
                {
                    "scheme_id": f"{base_id}__h5__{tenor}",
                    "base_scheme_id": base_id,
                    "runtime_type": "native_adapter",
                    "frequency": "daily",
                    "task_type": "T+5",
                    "target_tenor": tenor,
                    "horizon": 5,
                }
            )
        version_rows.append(
            {
                "scheme_id": base_id,
                "scheme_version": f"version-{index}",
                "runtime_type": "native_adapter",
                "code_hash": f"{index + 1:064x}",
                "config_hash": f"{index + 101:064x}",
            }
        )
    for frequency, count, task_type, horizon, offset in (
        ("weekly", 7, "weekly_point", 6, 100),
        ("monthly", 8, "monthly", 30, 200),
    ):
        for index in range(count):
            base_id = f"{frequency}_{index}"
            registry_rows.append(
                {
                    "scheme_id": f"{base_id}__h{horizon}__10Y",
                    "base_scheme_id": base_id,
                    "runtime_type": "native_adapter",
                    "frequency": frequency,
                    "task_type": task_type,
                    "target_tenor": "10Y",
                    "horizon": horizon,
                }
            )
            version_rows.append(
                {
                    "scheme_id": base_id,
                    "scheme_version": f"version-{index}",
                    "runtime_type": "native_adapter",
                    "code_hash": f"{index + offset:064x}",
                    "config_hash": f"{index + offset + 50:064x}",
                }
            )
    return registry_rows, version_rows


def _execution_authority(version_rows):
    from harness.signal_gap_plan import DiscoveredSchemeIdentity

    return tuple(
        DiscoveredSchemeIdentity(
            base_scheme_id=str(row["scheme_id"]),
            scheme_version=str(row["scheme_version"]),
            runtime_type=str(row["runtime_type"]),
            code_sha256=str(row["code_hash"]),
            config_sha256=str(row["config_hash"]),
            status="active",
        )
        for row in version_rows
    )


def _demo_execution_authority():
    from harness.signal_gap_plan import DiscoveredSchemeIdentity

    return (
        DiscoveredSchemeIdentity(
            base_scheme_id="demo",
            scheme_version="version-1",
            runtime_type="blackbox_v2",
            code_sha256="c" * 64,
            config_sha256="d" * 64,
            status="active",
        ),
    )
