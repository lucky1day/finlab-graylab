from __future__ import annotations

import hashlib
import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, timedelta
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


class _RecordFrame:
    def __init__(self, rows):
        self._rows = list(rows)

    def to_dict(self, orient):
        if orient != "records":
            raise AssertionError(f"unexpected orient: {orient}")
        return list(self._rows)


class _ReaderConnection:
    def __init__(self, responses):
        self.responses = responses
        self.statements: list[str] = []
        self.parameters: list[object] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        self.parameters.append(params)
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
            _InputGeneration,
            ObservedSignal,
            RegistryTarget,
            SignalGapSnapshot,
        ) = self._types()
        from shared.data_bridge.authority import (
            StableDataBridgeCurrentAuthority,
            StableDataBridgeCutoff,
            StableDataBridgeFileIdentity,
        )

        files = tuple(
            StableDataBridgeFileIdentity(
                filename=filename,
                rows=10,
                columns=2,
                min_key=min_key,
                max_key=max_key,
                sha256=hashlib.sha256(
                    f"{filename}:bytes".encode("utf-8")
                ).hexdigest(),
                business_hash=hashlib.sha256(
                    f"{filename}:business".encode("utf-8")
                ).hexdigest(),
            )
            for filename, min_key, max_key in (
                ("daily_output.csv", "2025-01-01", "2026-05-28"),
                ("weekly_output.csv", "202501", "202622"),
                ("monthly_output.csv", "202501", "202605"),
            )
        )
        cutoffs = tuple(
            StableDataBridgeCutoff(
                feature_date=feature_date,
                daily_cutoff_key=daily_cutoff,
                weekly_cutoff_key="202622",
                monthly_cutoff_key="202605",
            )
            for feature_date, daily_cutoff in (
                ("2026-05-26", "2026-05-26"),
                ("2026-05-27", "2026-05-26"),
            )
        )
        databridge_authority = StableDataBridgeCurrentAuthority(
            authority_schema_version=(
                "stable-databridge-current-authority-v1"
            ),
            generation_id="current-20260528",
            refresh_date="2026-05-28",
            schema_version="data-bridge-v1",
            business_digest="7" * 64,
            publication_capability=None,
            files=files,
            cutoffs=cutoffs,
            publication_identity_sha256="9" * 64,
            stable_identity_sha256="8" * 64,
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
            input_generations=(),
            input_watermarks={
                "trade_calendar_max": "2026-07-31",
                "daily_source_max": "2026-07-27",
            },
            source_identity_sha256="b" * 64,
            discovery_identity_sha256="e" * 64,
            active_version_identity_sha256="f" * 64,
            databridge_authority=databridge_authority,
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
                "2026-06-03": "BLOCKED_NO_GENERATION",
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
        self.assertEqual(first["schema_version"], "active-signal-gap-plan-v2")

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
        self.assertTrue(action["business_key_present"])
        self.assertEqual(plan["counts"]["present"], 1)
        self.assertEqual(plan["counts"]["open_gap"], 4)
        self.assertEqual(
            plan["counts"]["observed_contract_anomaly"],
            1,
        )
        self.assertEqual(
            plan["counts"]["expected"],
            plan["counts"]["present"] + plan["counts"]["open_gap"],
        )

    def test_databridge_current_invalid_is_data_contract_blocked(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        plan = build_signal_gap_plan(
            replace(
                snapshot,
                databridge_authority=None,
                databridge_authority_error="INVALID",
            ),
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
            "DATABRIDGE_CURRENT_INVALID",
        )

    def test_missing_current_is_distinct_from_invalid_current(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        plan = build_signal_gap_plan(
            replace(
                snapshot,
                databridge_authority=None,
                databridge_authority_error="MISSING",
            ),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        action = next(
            row
            for row in plan["actions"]
            if row["target_date"] == "2026-06-02"
        )
        self.assertEqual(action["action"], "BLOCKED_NO_GENERATION")
        self.assertEqual(action["reason"], "NO_DATABRIDGE_CURRENT")

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

    def test_native_persisted_run_old_identity_is_present_but_blocked(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            RegistryTarget,
            SignalGapSnapshot,
            _read_persisted_canonical_observations,
            build_signal_gap_plan,
        )

        target = RegistryTarget(
            registry_scheme_id="native_demo__h5__10Y",
            base_scheme_id="native_demo",
            runtime_type="native_adapter",
            frequency="daily",
            task_type="T+5",
            target_tenor="10Y",
            horizon=5,
            scheme_version="active-version",
            live_target_start_date="2026-06-01",
            live_boundary_source="platform_live_boundary_v1",
            input_mode="generation_v1",
            code_sha256="a" * 64,
            config_sha256="b" * 64,
        )
        run = {
            "id": 7,
            "benchmark_id": "native-history",
            "scheme_id": "native_demo",
            "data_source": "framework_db_aligned",
            "start_date": "2025-01-01",
            "end_date": "2025-01-08",
            "status": "success",
            "summary": {
                "row_count": 1,
                "scheme_version": "old-version",
            },
            "code_hash": "c" * 64,
            "config_hash": "d" * 64,
            "created_at": "2026-07-01T00:00:00+08:00",
            "updated_at": "2026-07-01T00:00:00+08:00",
        }
        detail = {
            "run_id": 7,
            "target_tenor": "10Y",
            "horizon": 5,
            "predict_date": "2025-01-01",
            "feature_date": "2025-01-01",
            "target_date": "2025-01-08",
        }
        connection = _ReaderConnection(
            (
                ("FROM t_backtest_runs", [run]),
                ("FROM t_backtest_predictions", [detail]),
            )
        )

        cases, signals, watermarks, blockers, authorities = (
            _read_persisted_canonical_observations(
                connection,
                (
                    {
                        "scheme_id": target.registry_scheme_id,
                        "base_scheme_id": target.base_scheme_id,
                        "runtime_type": target.runtime_type,
                        "status": "active",
                    },
                ),
                (target,),
            )
        )
        plan = build_signal_gap_plan(
            SignalGapSnapshot(
                registry_targets=(target,),
                expected_cases=cases,
                canonical_signals=signals,
                live_signals=(),
                input_generations=(),
                input_watermarks=watermarks,
                source_identity_sha256="e" * 64,
                discovery_identity_sha256="f" * 64,
                active_version_identity_sha256="1" * 64,
                control_plane_blockers=blockers,
                canonical_authorities=authorities,
            ),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertEqual(authorities[0]["status"], "BLOCKED")
        self.assertEqual(
            authorities[0]["failure_code"],
            "CANONICAL_SCHEME_VERSION_DRIFT",
        )
        self.assertEqual(signals[0].scheme_version, "old-version")
        self.assertEqual(
            plan["actions"][0]["action"],
            "BLOCKED_DATA_CONTRACT",
        )
        self.assertTrue(plan["actions"][0]["business_key_present"])
        self.assertEqual(plan["counts"]["present"], 1)
        self.assertEqual(plan["counts"]["open_gap"], 0)
        self.assertEqual(plan["status"], "BLOCKED")

    def test_native_matching_summary_with_old_hashes_is_digest_drift(
        self,
    ) -> None:
        from harness.signal_gap_plan import _canonical_run_version_error

        target = self._snapshot().registry_targets[0]

        self.assertEqual(
            _canonical_run_version_error(
                target,
                {
                    "code_hash": "a" * 64,
                    "config_hash": "b" * 64,
                },
                {"scheme_version": target.scheme_version},
            ),
            "CANONICAL_VERSION_DIGEST_DRIFT",
        )

    def test_native_authority_digest_binds_persisted_run_identity(
        self,
    ) -> None:
        from harness.signal_gap_plan import _native_manifest_authority

        run = {
            "benchmark_id": "benchmark-1",
            "scheme_id": "native_demo",
            "data_source": "framework_db_aligned",
            "code_hash": "a" * 64,
            "config_hash": "b" * 64,
        }
        summary = {
            "scheme_version": "active-version",
            "row_count": 1,
        }
        details = (
            {
                "target_tenor": "10Y",
                "horizon": 5,
                "predict_date": "2025-01-01",
                "feature_date": "2025-01-01",
                "target_date": "2025-01-08",
            },
        )

        def digest(
            *,
            changed_run=run,
            changed_summary=summary,
        ):
            return _native_manifest_authority(
                "native_demo",
                run_id=7,
                run=changed_run,
                summary=changed_summary,
                details=details,
                failure_code=None,
            )["digest_sha256"]

        baseline = digest()
        variants = {
            "benchmark_id": digest(
                changed_run={**run, "benchmark_id": "benchmark-2"},
            ),
            "code_hash": digest(
                changed_run={**run, "code_hash": "c" * 64},
            ),
            "config_hash": digest(
                changed_run={**run, "config_hash": "d" * 64},
            ),
            "summary": digest(
                changed_summary={**summary, "row_count": 2},
            ),
        }

        for field, changed_digest in variants.items():
            with self.subTest(field=field):
                self.assertNotEqual(baseline, changed_digest)

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

        raw, targets, blockers, active_version_digest = (
            _read_registry_versions(
            connection,
            execution_authority=execution_authority,
        )
        )

        self.assertEqual(len(raw), 44)
        self.assertEqual(len(targets), 44)
        self.assertEqual(blockers, ())
        self.assertRegex(active_version_digest, r"^[0-9a-f]{64}$")
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
            raw, targets, blockers, _ = _read_registry_versions(
                connection,
                execution_authority=execution_authority,
            )
            self.assertEqual(len(raw), 44)
            self.assertEqual(len(targets), 44)
            self.assertIn(
                expected_code,
                {blocker["code"] for blocker in blockers},
            )

    def test_discovery_and_registry_active_bases_form_a_closed_set(
        self,
    ) -> None:
        from harness.signal_gap_plan import _read_registry_versions

        registry_rows, version_rows = _production_registry_rows()
        authority = _execution_authority(version_rows)
        missing_base = authority[-1].base_scheme_id
        extra = replace(
            authority[0],
            base_scheme_id="discovery_only",
            scheme_version="discovery-only-version",
            code_sha256="a" * 64,
            config_sha256="b" * 64,
        )
        connection = _ReaderConnection(
            (
                ("FROM t_scheme_registry", registry_rows),
                ("FROM t_scheme_versions", version_rows),
            )
        )

        raw, targets, blockers, _ = _read_registry_versions(
            connection,
            execution_authority=(*authority[:-1], extra),
        )

        self.assertEqual(len(raw), 44)
        self.assertEqual(len(targets), 44)
        self.assertEqual(
            {
                (blocker["code"], blocker["base_scheme_id"])
                for blocker in blockers
            },
            {
                ("DISCOVERY_IDENTITY_MISSING", missing_base),
                ("DISCOVERY_IDENTITY_NOT_REGISTERED", "discovery_only"),
            },
        )

    def test_all_active_version_bytes_are_bound_into_plan_identity(self) -> None:
        from harness.signal_gap_plan import _read_registry_versions

        registry_rows, version_rows = _production_registry_rows()
        authority = _execution_authority(version_rows)
        extra = {
            **version_rows[0],
            "scheme_version": "extra-active-version",
            "code_hash": "a" * 64,
            "config_hash": "b" * 64,
        }

        def digest_for(extra_row):
            connection = _ReaderConnection(
                (
                    ("FROM t_scheme_registry", registry_rows),
                    (
                        "FROM t_scheme_versions",
                        (*version_rows, extra_row),
                    ),
                )
            )
            _, _, _, digest = _read_registry_versions(
                connection,
                execution_authority=authority,
            )
            return digest

        first = digest_for(extra)
        second = digest_for({**extra, "config_hash": "c" * 64})

        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)

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
                    "segment_scope": ["canonical", "live"],
                }
            ],
        )
        self.assertEqual(
            [
                action["action"]
                for action in plan["actions"]
                if action["target_date"] == "2026-06-01"
            ],
            ["SKIP_PRESENT"],
        )
        self.assertTrue(
            all(
                action["action"] == "BLOCKED_DATA_CONTRACT"
                for action in plan["actions"]
                if action["target_date"] != "2026-06-01"
            )
        )
        self.assertEqual(plan["counts"]["present"], 1)
        self.assertEqual(plan["counts"]["BLOCKED_DATA_CONTRACT"], 4)

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

    def test_canonical_only_blocker_does_not_block_live_only_plan(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        live_case = next(
            case
            for case in snapshot.expected_cases
            if case.target_date == "2026-06-01"
        )
        plan = build_signal_gap_plan(
            replace(
                snapshot,
                expected_cases=(live_case,),
                canonical_signals=(),
                control_plane_blockers=(
                    {
                        "code": (
                            "NATIVE_CANONICAL_VERSION_IDENTITY_INVALID"
                        ),
                        "base_scheme_id": "demo",
                        "run_id": 123,
                        "reason": "CANONICAL_VERSION_DIGEST_DRIFT",
                        "segment_scope": ["canonical"],
                    },
                ),
            ),
            start_date="2026-05-26",
            as_of_date="2026-05-26",
        )

        self.assertEqual(plan["counts"]["canonical_expected"], 0)
        self.assertEqual(plan["counts"]["live_expected"], 1)
        self.assertEqual(plan["actions"][0]["action"], "SKIP_PRESENT")
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(
            plan["control_plane"]["blockers"][0]["segment_scope"],
            ["canonical"],
        )

    def test_invalid_expected_segment_is_rejected_before_blocker_scope(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            SignalGapPlanError,
            build_signal_gap_plan,
        )

        snapshot = self._snapshot()
        invalid_case = replace(
            snapshot.expected_cases[1],
            segment="other",
        )
        with self.assertRaisesRegex(
            SignalGapPlanError,
            "INVALID_EXPECTED_SEGMENT",
        ):
            build_signal_gap_plan(
                replace(
                    snapshot,
                    expected_cases=(invalid_case,),
                    control_plane_blockers=(
                        {
                            "code": "ACTIVE_VERSION_CARDINALITY_INVALID",
                            "base_scheme_id": "demo",
                            "segment_scope": ["live"],
                        },
                    ),
                ),
                start_date="2026-05-26",
                as_of_date="2026-05-26",
            )

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
        third = build_signal_gap_plan(
            replace(snapshot, active_version_identity_sha256="1" * 64),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        self.assertEqual(
            first["source_snapshot"]["active_version_identity_sha256"],
            "f" * 64,
        )
        self.assertNotEqual(first["plan_sha256"], third["plan_sha256"])

    def test_same_business_key_drift_is_not_pruned_by_observed_date(self) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        invalid = replace(
            snapshot.live_signals[0],
            predict_date="2024-12-31",
            phase="invalid_phase",
            run_status="failed",
        )

        plan = build_signal_gap_plan(
            replace(snapshot, live_signals=(invalid,)),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        action = next(
            item
            for item in plan["actions"]
            if item["target_date"] == "2026-06-01"
        )
        self.assertEqual(action["action"], "BLOCKED_DATA_CONTRACT")
        self.assertIn("OBSERVED_SIGNAL_CONTRACT_DRIFT", action["reason"])

    def test_cross_segment_same_key_is_preserved_before_date_pruning(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            ObservedSignal,
            build_signal_gap_plan,
        )

        snapshot = self._snapshot()
        canonical_case = snapshot.expected_cases[0]
        wrong_segment = ObservedSignal(
            base_scheme_id=canonical_case.base_scheme_id,
            target_tenor=canonical_case.target_tenor,
            horizon=canonical_case.horizon,
            target_date=canonical_case.target_date,
            predict_date="2024-12-31",
            feature_date=canonical_case.feature_date,
            phase="gray_live",
            scheme_version="version-1",
            run_status="success",
        )

        plan = build_signal_gap_plan(
            replace(snapshot, live_signals=(wrong_segment,)),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        action = next(
            item
            for item in plan["actions"]
            if item["target_date"] == canonical_case.target_date
        )
        self.assertEqual(action["action"], "BLOCKED_DATA_CONTRACT")
        self.assertEqual(
            action["reason"],
            "OBSERVED_SIGNAL_SEGMENT_OVERLAP",
        )

    def test_expected_business_key_authority_uses_requested_dates(self) -> None:
        from harness.signal_gap_plan import (
            _expected_business_keys_for_date_scope,
        )

        snapshot = self._snapshot()
        in_scope = snapshot.expected_cases[0]
        future = replace(
            snapshot.expected_cases[1],
            predict_date="2026-08-03",
            target_date="2026-08-10",
        )

        keys = _expected_business_keys_for_date_scope(
            (in_scope, future),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertEqual(keys, {in_scope.business_key})
        self.assertNotIn(future.business_key, keys)

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
            {
                **common,
                "id": 3,
                "predict_date": "2026-08-03",
                "prediction_phase": "gray_live",
                "run_id": 103,
                "run_status": "success",
                "run_scheme_id": "demo",
                "run_scheme_version": "version-1",
                "run_runtime_type": "blackbox_v2",
                "run_prediction_phase": "gray_live",
                "run_predict_date": "2026-08-03",
            },
            {
                **common,
                "id": 4,
                "target_date": "2026-08-10",
                "predict_date": "2026-08-03",
                "prediction_phase": "gray_live",
                "run_id": 104,
                "run_status": "success",
                "run_scheme_id": "demo",
                "run_scheme_version": "version-1",
                "run_runtime_type": "blackbox_v2",
                "run_prediction_phase": "gray_live",
                "run_predict_date": "2026-08-03",
            },
        ]
        connection = _ReaderConnection(
            (("FROM t_scheme_predictions", rows),)
        )

        raw, signals = _read_live_signals(
            connection,
            (target,),
            as_of_date="2026-07-27",
            expected_business_keys={
                ("demo", "10Y", 5, "2026-06-01"),
            },
        )

        self.assertEqual(len(raw), 3)
        self.assertEqual(len(signals), 3)
        self.assertIsNone(signals[0].contract_error)
        self.assertIn("LIVE_PHASE_INVALID", signals[1].contract_error or "")
        self.assertIn(
            "RUN_SCHEME_ID_DRIFT",
            signals[1].contract_error or "",
        )
        sql = connection.statements[0]
        self.assertNotIn("prediction_phase IN", sql)
        self.assertNotIn("p.predict_date <= :as_of_date", sql)
        self.assertEqual(
            connection.parameters[0],
            {
                "scheme_ids": ["demo"],
            },
        )
        for field in (
            "run_scheme_id",
            "run_scheme_version",
            "run_runtime_type",
            "run_prediction_phase",
            "run_predict_date",
        ):
            self.assertIn(field, sql)

    def test_live_old_prediction_and_run_version_is_present_but_blocked(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _read_live_signals,
            build_signal_gap_plan,
        )

        snapshot = self._snapshot()
        target = snapshot.registry_targets[0]
        row = {
            "id": 1,
            "scheme_id": target.base_scheme_id,
            "target_tenor": target.target_tenor,
            "horizon": target.horizon,
            "target_date": "2026-06-01",
            "feature_date": "2026-05-25",
            "predict_date": "2026-05-26",
            "prediction_phase": "gray_live",
            "scheme_version": "old-version",
            "run_id": 101,
            "run_status": "success",
            "run_scheme_id": target.base_scheme_id,
            "run_scheme_version": "old-version",
            "run_runtime_type": target.runtime_type,
            "run_prediction_phase": "gray_live",
            "run_predict_date": "2026-05-26",
        }
        connection = _ReaderConnection(
            (("FROM t_scheme_predictions", [row]),)
        )
        _, signals = _read_live_signals(
            connection,
            (target,),
            as_of_date="2026-07-27",
            expected_business_keys={
                (target.base_scheme_id, "10Y", 5, "2026-06-01"),
            },
        )

        plan = build_signal_gap_plan(
            replace(snapshot, live_signals=signals),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        action = next(
            item
            for item in plan["actions"]
            if item["target_date"] == "2026-06-01"
        )

        self.assertEqual(action["action"], "BLOCKED_DATA_CONTRACT")
        self.assertIn("LIVE_SCHEME_VERSION_DRIFT", action["reason"])
        self.assertTrue(action["business_key_present"])
        self.assertEqual(plan["counts"]["present"], 1)
        self.assertEqual(plan["counts"]["open_gap"], 4)
        self.assertEqual(plan["status"], "BLOCKED")

    def test_direct_old_version_observation_is_present_but_blocked(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        live_case = next(
            item
            for item in snapshot.expected_cases
            if item.target_date == "2026-06-01"
        )
        old_observation = replace(
            snapshot.live_signals[0],
            scheme_version="old-version",
        )

        plan = build_signal_gap_plan(
            replace(
                snapshot,
                expected_cases=(live_case,),
                canonical_signals=(),
                live_signals=(old_observation,),
                input_generations=(),
            ),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertEqual(
            plan["actions"][0]["action"],
            "BLOCKED_DATA_CONTRACT",
        )
        self.assertTrue(plan["actions"][0]["business_key_present"])
        self.assertEqual(plan["counts"]["present"], 1)
        self.assertEqual(plan["counts"]["open_gap"], 0)
        self.assertEqual(plan["status"], "BLOCKED")

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
            databridge_config=Mock(),
        )

        self.assertEqual(result["counts"]["active_target"], 1)
        reader.assert_called_once_with(
            connection,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
            execution_authority=execution_authority,
            discovery_identity_sha256=unittest.mock.ANY,
            databridge_config=unittest.mock.ANY,
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
                databridge_config=Mock(),
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

    def test_calendar_authority_naturally_enumerates_production_shape(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _FrozenCalendar,
            build_expected_canonical_cases,
            build_signal_gap_plan,
        )

        targets = _production_shape_targets()
        calendar = _production_shape_calendar(_FrozenCalendar)
        cases = build_expected_canonical_cases(
            targets,
            calendar=calendar,
            start_date="2025-01-01",
        )
        observed = tuple(_canonical_observation(case) for case in cases)
        snapshot = _production_shape_snapshot(
            targets=targets,
            cases=cases,
            observations=observed,
        )

        plan = build_signal_gap_plan(
            snapshot,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertEqual(len(targets), 44)
        self.assertEqual(
            {
                frequency: sum(
                    case.frequency == frequency for case in cases
                )
                for frequency in ("daily", "weekly", "monthly")
            },
            {"daily": 9677, "weekly": 504, "monthly": 128},
        )
        self.assertEqual(len(cases), 10309)
        self.assertEqual(plan["counts"]["expected"], 10309)
        self.assertEqual(plan["counts"]["SKIP_PRESENT"], 10309)
        self.assertEqual(plan["counts"]["open_gap"], 0)

        monthly = next(
            case
            for case in cases
            if case.frequency == "monthly"
            and case.predict_date == "2025-02-15"
        )
        self.assertEqual(monthly.feature_date, "2025-02-14")
        self.assertEqual(monthly.target_date, "2025-03-14")
        native_weekly = next(
            case
            for case in cases
            if case.base_scheme_id == "weekly_native_point_0"
        )
        blackbox_weekly = next(
            case
            for case in cases
            if case.base_scheme_id == "weekly_blackbox_0"
            and case.feature_date == native_weekly.feature_date
        )
        self.assertEqual(
            (
                native_weekly.predict_date,
                native_weekly.feature_date,
                native_weekly.target_date,
            ),
            (
                blackbox_weekly.predict_date,
                blackbox_weekly.feature_date,
                blackbox_weekly.target_date,
            ),
        )
        self.assertEqual(native_weekly.horizon, 6)
        self.assertEqual(blackbox_weekly.horizon, 1)

    def test_actual_facts_validate_but_never_enumerate_expected_cases(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _FrozenCalendar,
            _validate_canonical_actual_facts,
            build_expected_canonical_cases,
            build_signal_gap_plan,
        )

        targets = _production_shape_targets()
        calendar = _production_shape_calendar(_FrozenCalendar)
        cases = build_expected_canonical_cases(
            targets,
            calendar=calendar,
            start_date="2025-01-01",
        )
        daily, weekly, monthly = _actual_rows_for_cases(cases)
        missing = next(
            case
            for case in cases
            if case.frequency == "daily"
            and case.target_tenor == "3Y"
        )
        daily = [
            row
            for row in daily
            if not (
                row["tenor"] == missing.target_tenor
                and row["trade_date"] == missing.target_date
            )
        ]

        validated = _validate_canonical_actual_facts(
            cases,
            daily_actual_rows=daily,
            weekly_actual_rows=weekly,
            monthly_actual_rows=monthly,
        )

        self.assertEqual(len(validated), len(cases))
        blocked_cases = [
            case
            for case in validated
            if case.data_contract_error is not None
        ]
        self.assertGreater(len(blocked_cases), 0)
        self.assertTrue(
            all(
                case.target_tenor == "3Y"
                and case.target_date == missing.target_date
                for case in blocked_cases
            )
        )
        observed = tuple(
            _canonical_observation(case) for case in validated
        )
        plan = build_signal_gap_plan(
            _production_shape_snapshot(
                targets=targets,
                cases=validated,
                observations=observed,
            ),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )
        blocked = [
            row
            for row in plan["actions"]
            if row["action"] == "BLOCKED_DATA_CONTRACT"
        ]
        self.assertEqual(len(blocked), len(blocked_cases))
        self.assertTrue(
            all("DAILY_ACTUAL" in row["reason"] for row in blocked)
        )

    def test_blackbox_canonical_daily_excludes_trade_flagged_weekends(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _FrozenCalendar,
            build_expected_canonical_cases,
        )

        target = replace(
            next(
            target
            for target in _production_shape_targets()
                if target.base_scheme_id == "daily_blackbox_t5_0"
            ),
            registry_scheme_id="daily_blackbox_t5_0__h1__10Y",
            task_type="T+1",
            horizon=1,
        )
        rows = [
            {"rdate": "2025-01-03", "trade_flag": "1"},
            {"rdate": "2025-01-04", "trade_flag": "1"},
            {"rdate": "2025-01-05", "trade_flag": "0"},
            {"rdate": "2025-01-06", "trade_flag": "1"},
            {"rdate": "2025-01-07", "trade_flag": "1"},
            {"rdate": "2025-01-08", "trade_flag": "1"},
        ]
        calendar = _FrozenCalendar(
            trade_calendar_rows=rows,
            week_calendar_rows=[
                {**row, "week_id": 202501} for row in rows[:3]
            ]
            + [{**row, "week_id": 202502} for row in rows[3:]],
            start_date="2025-01-03",
            as_of_date="2025-01-07",
        )

        cases = build_expected_canonical_cases(
            (replace(target, live_target_start_date="2025-01-08"),),
            calendar=calendar,
            start_date="2025-01-03",
        )

        self.assertEqual(
            [
                (
                    case.predict_date,
                    case.feature_date,
                    case.target_date,
                )
                for case in cases
            ],
            [
                ("2025-01-03", "2025-01-03", "2025-01-06"),
                ("2025-01-06", "2025-01-06", "2025-01-07"),
            ],
        )

    def test_blackbox_canonical_week_uses_last_weekday_trade(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _FrozenCalendar,
            build_expected_canonical_cases,
        )

        target = next(
            target
            for target in _production_shape_targets()
            if target.base_scheme_id == "weekly_blackbox_0"
        )
        rows = [
            {"rdate": "2025-01-03", "trade_flag": "1", "week_id": 202501},
            {"rdate": "2025-01-04", "trade_flag": "1", "week_id": 202501},
            {"rdate": "2025-01-05", "trade_flag": "0", "week_id": 202501},
            {"rdate": "2025-01-06", "trade_flag": "1", "week_id": 202502},
            {"rdate": "2025-01-07", "trade_flag": "1", "week_id": 202502},
            {"rdate": "2025-01-08", "trade_flag": "1", "week_id": 202502},
        ]
        calendar = _FrozenCalendar(
            trade_calendar_rows=rows,
            week_calendar_rows=rows,
            start_date="2025-01-03",
            as_of_date="2025-01-08",
        )

        cases = build_expected_canonical_cases(
            (replace(target, live_target_start_date="2025-01-09"),),
            calendar=calendar,
            start_date="2025-01-03",
        )

        self.assertEqual(
            [
                (
                    case.predict_date,
                    case.feature_date,
                    case.target_date,
                )
                for case in cases
            ],
            [("2025-01-03", "2025-01-03", "2025-01-08")],
        )

    def test_weekly_canonical_authority_excludes_future_week_pairs(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _FrozenCalendar,
            build_expected_canonical_cases,
        )

        target = _weekly_asof_target()
        calendar = _weekly_asof_calendar(
            _FrozenCalendar,
            include_future=True,
        )

        cases = build_expected_canonical_cases(
            (target,),
            calendar=calendar,
            start_date="2024-12-20",
        )

        self.assertEqual(
            [
                (
                    case.predict_date,
                    case.feature_date,
                    case.target_date,
                )
                for case in cases
            ],
            [
                ("2024-12-27", "2024-12-27", "2025-01-03"),
                ("2025-01-03", "2025-01-03", "2025-01-08"),
            ],
        )
        self.assertNotIn(
            "2025-01-10",
            {case.predict_date for case in cases},
        )
        self.assertTrue(
            all(case.target_date <= "2025-01-08" for case in cases)
        )

    def test_future_week_rows_do_not_change_canonical_authority_or_plan(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            SignalGapSnapshot,
            _FrozenCalendar,
            _calendar_canonical_authorities,
            build_expected_canonical_cases,
            build_signal_gap_plan,
        )

        target = _weekly_asof_target()

        def plan(include_future):
            cases = build_expected_canonical_cases(
                (target,),
                calendar=_weekly_asof_calendar(
                    _FrozenCalendar,
                    include_future=include_future,
                ),
                start_date="2024-12-20",
            )
            authorities = _calendar_canonical_authorities(cases)
            snapshot = SignalGapSnapshot(
                registry_targets=(target,),
                expected_cases=cases,
                canonical_signals=tuple(
                    _canonical_observation(case) for case in cases
                ),
                live_signals=(),
                input_generations=(),
                input_watermarks={"as_of_date": "2025-01-08"},
                source_identity_sha256="a" * 64,
                discovery_identity_sha256="b" * 64,
                active_version_identity_sha256="c" * 64,
                canonical_authorities=authorities,
            )
            return authorities, build_signal_gap_plan(
                snapshot,
                start_date="2024-12-20",
                as_of_date="2025-01-08",
            )

        base_authorities, base_plan = plan(False)
        future_authorities, future_plan = plan(True)

        self.assertEqual(base_authorities, future_authorities)
        self.assertEqual(
            base_authorities[0]["expected_count"],
            future_authorities[0]["expected_count"],
        )
        self.assertEqual(
            base_authorities[0]["digest_sha256"],
            future_authorities[0]["digest_sha256"],
        )
        self.assertEqual(
            base_plan["plan_sha256"],
            future_plan["plan_sha256"],
        )

    def test_reader_scopes_calendar_watermark_and_plan_to_as_of(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            read_signal_gap_snapshot,
            build_signal_gap_plan,
        )

        target = _weekly_asof_target()
        connection = _ReaderConnection(
            (
                (
                    "SELECT DATABASE()",
                    [
                        {
                            "database_name": "signal_gap_test",
                            "server_uuid": "test-server",
                            "server_port": 3306,
                        }
                    ],
                ),
            )
        )

        def read(include_future):
            rows = _weekly_asof_rows(
                include_future=include_future,
            )
            calendar_snapshot = {
                "t_trade_calendar.csv": _RecordFrame(rows),
                "api_wind_date.csv": _RecordFrame(rows),
            }
            with (
                patch(
                    "harness.signal_gap_plan._read_registry_versions",
                    return_value=(
                        (
                            {
                                "scheme_id": target.registry_scheme_id,
                                "base_scheme_id":
                                    target.base_scheme_id,
                            },
                        ),
                        (target,),
                        (),
                        "c" * 64,
                    ),
                ),
                patch(
                    "harness.signal_gap_plan."
                    "read_calendar_snapshot_from_connection",
                    return_value=calendar_snapshot,
                ),
                patch(
                    "harness.signal_gap_plan."
                    "_read_canonical_actual_facts",
                    side_effect=lambda _, cases: (
                        tuple(cases),
                        {
                            "daily_actual_fact_count": 0,
                            "weekly_actual_fact_count": 0,
                            "monthly_actual_fact_count": 0,
                        },
                    ),
                ),
                patch(
                    "harness.signal_gap_plan."
                    "_read_persisted_canonical_observations",
                    return_value=((), (), {}, (), ()),
                ),
                patch(
                    "harness.signal_gap_plan.build_expected_live_cases",
                    return_value=(),
                ),
                patch(
                    "harness.signal_gap_plan._read_live_signals",
                    return_value=([], ()),
                ),
                patch(
                    "harness.signal_gap_plan._read_input_generations",
                    return_value=(),
                ),
            ):
                snapshot = read_signal_gap_snapshot(
                    connection,
                    start_date="2024-12-20",
                    as_of_date="2025-01-08",
                    execution_authority=(),
                    discovery_identity_sha256="b" * 64,
                    databridge_config=Mock(),
                )
            return snapshot, build_signal_gap_plan(
                snapshot,
                start_date="2024-12-20",
                as_of_date="2025-01-08",
            )

        base_snapshot, base_plan = read(False)
        future_snapshot, future_plan = read(True)

        self.assertEqual(
            base_snapshot.expected_cases,
            future_snapshot.expected_cases,
        )
        self.assertEqual(
            base_plan["actions"],
            future_plan["actions"],
        )
        self.assertEqual(
            base_snapshot.canonical_authorities,
            future_snapshot.canonical_authorities,
        )
        self.assertEqual(
            base_snapshot.input_watermarks,
            future_snapshot.input_watermarks,
        )
        self.assertEqual(
            base_snapshot.input_watermarks["trade_calendar_max"],
            "2025-01-08",
        )
        self.assertEqual(
            base_plan["plan_sha256"],
            future_plan["plan_sha256"],
        )

    def test_weekly_actual_query_range_stops_at_as_of(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _FrozenCalendar,
            _read_canonical_actual_facts,
            build_expected_canonical_cases,
        )
        from shared.prediction_context import WEEKLY_TARGET_RULE

        target = _weekly_asof_target()
        cases = build_expected_canonical_cases(
            (target,),
            calendar=_weekly_asof_calendar(
                _FrozenCalendar,
                include_future=True,
            ),
            start_date="2024-12-20",
        )
        connection = _ReaderConnection(
            (
                ("FROM t_scheme_actuals", []),
                (
                    "FROM t_scheme_weekly_actuals",
                    [
                        {
                            "tenor": "10Y",
                            "predict_date": "2024-12-28",
                            "feature_date": "2024-12-27",
                            "target_date": "2025-01-03",
                            "direction_weekly": 1,
                            "target_rule": WEEKLY_TARGET_RULE,
                        },
                        {
                            "tenor": "10Y",
                            "predict_date": "2025-01-04",
                            "feature_date": "2025-01-03",
                            "target_date": "2025-01-08",
                            "direction_weekly": -1,
                            "target_rule": WEEKLY_TARGET_RULE,
                        },
                    ],
                ),
                ("FROM t_scheme_monthly_actuals", []),
            )
        )

        validated, _ = _read_canonical_actual_facts(
            connection,
            cases,
        )

        self.assertEqual(len(validated), 2)
        self.assertTrue(
            all(
                case.data_contract_error is None
                for case in validated
            )
        )
        self.assertTrue(
            all(
                params["end_date"] <= "2025-01-08"
                for params in connection.parameters
            )
        )

    def test_weekly_actual_predict_date_does_not_enumerate_canonical_key(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _FrozenCalendar,
            _validate_canonical_actual_facts,
            build_expected_canonical_cases,
        )
        from shared.prediction_context import WEEKLY_TARGET_RULE

        target = next(
            target
            for target in _production_shape_targets()
            if target.base_scheme_id == "weekly_blackbox_0"
        )
        calendar = _production_shape_calendar(_FrozenCalendar)
        case = build_expected_canonical_cases(
            (target,),
            calendar=calendar,
            start_date="2025-01-01",
        )[0]
        actual_predict_date = (
            date.fromisoformat(case.feature_date) + timedelta(days=1)
        ).isoformat()

        validated = _validate_canonical_actual_facts(
            (case,),
            daily_actual_rows=(),
            weekly_actual_rows=(
                {
                    "tenor": case.target_tenor,
                    "predict_date": actual_predict_date,
                    "feature_date": case.feature_date,
                    "target_date": case.target_date,
                    "direction_weekly": 1,
                    "target_rule": WEEKLY_TARGET_RULE,
                },
            ),
            monthly_actual_rows=(),
        )

        self.assertIsNone(validated[0].data_contract_error)

    def test_missing_one_backtest_detail_requires_exact_full_run_group(
        self,
    ) -> None:
        from harness.signal_gap_plan import (
            _FrozenCalendar,
            build_expected_canonical_cases,
            build_signal_gap_plan,
        )

        targets = _production_shape_targets()
        calendar = _production_shape_calendar(_FrozenCalendar)
        cases = build_expected_canonical_cases(
            targets,
            calendar=calendar,
            start_date="2025-01-01",
        )
        missing = next(
            case
            for case in cases
            if case.base_scheme_id == "daily_native_t1_0"
            and case.target_tenor == "1Y"
        )
        observed = tuple(
            _canonical_observation(case)
            for case in cases
            if case != missing
        )

        plan = build_signal_gap_plan(
            _production_shape_snapshot(
                targets=targets,
                cases=cases,
                observations=observed,
            ),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        missing_actions = [
            row
            for row in plan["actions"]
            if row["action"] == "FULL_CANONICAL_RUN_REQUIRED"
        ]
        self.assertEqual(len(missing_actions), 1)
        self.assertEqual(missing_actions[0]["business_key"], [
            missing.base_scheme_id,
            missing.target_tenor,
            missing.horizon,
            missing.target_date,
        ])
        self.assertEqual(
            missing_actions[0]["rebuild_group_id"],
            missing.base_scheme_id,
        )
        self.assertEqual(
            plan["canonical_rebuild_groups"],
            [
                {
                    "base_scheme_id": missing.base_scheme_id,
                    "persistence_mode": "FULL_RUN_ONLY",
                    "registry_scheme_ids": [
                        "daily_native_t1_0__h1__1Y",
                        "daily_native_t1_0__h1__5Y",
                    ],
                    "missing_business_keys": [
                        [
                            missing.base_scheme_id,
                            missing.target_tenor,
                            missing.horizon,
                            missing.target_date,
                        ]
                    ],
                }
            ],
        )

    def test_observed_outside_calendar_authority_is_reported_not_enumerated(
        self,
    ) -> None:
        from harness.signal_gap_plan import build_signal_gap_plan

        snapshot = self._snapshot()
        extra = replace(
            snapshot.live_signals[0],
            target_date="2026-06-08",
        )

        plan = build_signal_gap_plan(
            replace(
                snapshot,
                canonical_signals=(extra,),
                live_signals=snapshot.live_signals,
            ),
            start_date="2025-01-01",
            as_of_date="2026-07-27",
        )

        self.assertEqual(plan["status"], "BLOCKED")
        self.assertEqual(plan["counts"]["expected"], 5)
        self.assertEqual(
            plan["counts"]["observed_contract_anomaly"],
            1,
        )
        self.assertEqual(
            plan["observed_contract_anomalies"],
            [
                {
                    "segment": "canonical",
                    "business_key": ["demo", "10Y", 5, "2026-06-08"],
                    "reason": "OBSERVED_SIGNAL_OUTSIDE_AUTHORITY",
                }
            ],
        )


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
        databridge_config = Mock()
        with (
            patch(
                "harness.cli.create_engine_from_env",
                return_value=engine,
            ),
            patch(
                "harness.cli.DataBridgeRefreshConfig",
            ) as config_type,
            patch(
                "harness.cli.plan_signal_gaps",
                return_value=expected,
            ) as planner,
            patch("builtins.print") as output,
        ):
            config_type.from_env.return_value = databridge_config
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

        config_type.from_env.assert_called_once_with()
        self.assertEqual(result, 0)
        planner.assert_called_once_with(
            engine,
            start_date="2025-01-01",
            as_of_date="2026-07-27",
            databridge_config=databridge_config,
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


def _production_shape_targets():
    from harness.signal_gap_plan import RegistryTarget

    targets = []

    def add(
        base_scheme_id,
        *,
        runtime_type,
        frequency,
        task_type,
        horizon,
        tenors,
    ):
        input_mode = (
            "databridge_v1"
            if runtime_type == "blackbox_v2"
            else "generation_v1"
        )
        for tenor in tenors:
            targets.append(
                RegistryTarget(
                    registry_scheme_id=(
                        f"{base_scheme_id}__h{horizon}__{tenor}"
                    ),
                    base_scheme_id=base_scheme_id,
                    runtime_type=runtime_type,
                    frequency=frequency,
                    task_type=task_type,
                    target_tenor=tenor,
                    horizon=horizon,
                    scheme_version=f"{base_scheme_id}-version",
                    live_target_start_date="2026-06-01",
                    live_boundary_source="platform_live_boundary_v1",
                    input_mode=input_mode,
                    code_sha256=hashlib.sha256(
                        f"{base_scheme_id}:code".encode()
                    ).hexdigest(),
                    config_sha256=hashlib.sha256(
                        f"{base_scheme_id}:config".encode()
                    ).hexdigest(),
                )
            )

    for index in range(4):
        add(
            f"daily_native_t1_{index}",
            runtime_type="native_adapter",
            frequency="daily",
            task_type="T+1",
            horizon=1,
            tenors=("1Y", "5Y") if index == 0 else ("1Y",),
        )
    for index in range(13):
        add(
            f"daily_native_t5_{index}",
            runtime_type="native_adapter",
            frequency="daily",
            task_type="T+5",
            horizon=5,
            tenors=(
                ("1Y", "3Y", "5Y", "7Y")
                if index == 0
                else ("1Y",)
            ),
        )
    for index in range(8):
        add(
            f"daily_blackbox_t5_{index}",
            runtime_type="blackbox_v2",
            frequency="daily",
            task_type="T+5",
            horizon=5,
            tenors=("10Y",),
        )
    for index in range(3):
        add(
            f"weekly_native_point_{index}",
            runtime_type="native_adapter",
            frequency="weekly",
            task_type="weekly_point",
            horizon=6,
            tenors=(("5Y", "7Y", "10Y")[index],),
        )
        add(
            f"weekly_native_average_{index}",
            runtime_type="native_adapter",
            frequency="weekly",
            task_type="weekly_average",
            horizon=6,
            tenors=(("1Y", "5Y", "10Y")[index],),
        )
    add(
        "weekly_blackbox_0",
        runtime_type="blackbox_v2",
        frequency="weekly",
        task_type="weekly_point",
        horizon=1,
        tenors=("10Y",),
    )
    for index, tenor in enumerate(("1Y", "5Y", "10Y")):
        add(
            f"monthly_native_{index}",
            runtime_type="native_adapter",
            frequency="monthly",
            task_type="monthly",
            horizon=30,
            tenors=(tenor,),
        )
    for index, tenor in enumerate(("1Y", "3Y", "5Y", "7Y", "10Y")):
        add(
            f"monthly_blackbox_{index}",
            runtime_type="blackbox_v2",
            frequency="monthly",
            task_type="monthly",
            horizon=1,
            tenors=(tenor,),
        )
    return tuple(targets)


def _production_shape_calendar(calendar_type):
    closed_weekdays = {
        "2025-01-01",
        "2025-01-28",
        "2025-01-29",
        "2025-01-30",
        "2025-01-31",
        "2025-02-03",
        "2025-02-04",
        "2025-04-04",
        "2025-05-01",
        "2025-05-02",
        "2025-05-05",
        "2025-06-02",
        "2025-10-01",
        "2025-10-02",
        "2025-10-03",
        "2025-10-06",
        "2025-10-07",
        "2025-10-08",
        "2026-01-01",
        "2026-01-02",
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",
        "2026-02-23",
        "2026-04-06",
        "2026-05-01",
        "2026-05-04",
        "2026-05-05",
    }
    rows = []
    current = date(2025, 1, 1)
    end = date(2026, 8, 31)
    while current <= end:
        value = current.isoformat()
        is_trade = current.weekday() < 5 and value not in closed_weekdays
        rows.append(
            {
                "rdate": value,
                "trade_flag": "1" if is_trade else "0",
            }
        )
        current += timedelta(days=1)
    week_rows = [
        {
            **row,
            "week_id": (
                date.fromisoformat(row["rdate"]).isocalendar().year * 100
                + date.fromisoformat(row["rdate"]).isocalendar().week
            ),
        }
        for row in rows
    ]
    return calendar_type(
        trade_calendar_rows=rows,
        week_calendar_rows=week_rows,
        start_date="2025-01-01",
        as_of_date="2026-07-27",
    )


def _canonical_observation(case):
    from harness.signal_gap_plan import ObservedSignal

    return ObservedSignal(
        base_scheme_id=case.base_scheme_id,
        target_tenor=case.target_tenor,
        horizon=case.horizon,
        target_date=case.target_date,
        predict_date=case.predict_date,
        feature_date=case.feature_date,
        phase="canonical",
        scheme_version=f"{case.base_scheme_id}-version",
        run_status="success",
    )


def _production_shape_snapshot(*, targets, cases, observations):
    from harness.signal_gap_plan import SignalGapSnapshot

    return SignalGapSnapshot(
        registry_targets=tuple(targets),
        expected_cases=tuple(cases),
        canonical_signals=tuple(observations),
        live_signals=(),
        input_generations=(),
        input_watermarks={"trade_calendar_max": "2026-08-31"},
        source_identity_sha256="a" * 64,
        discovery_identity_sha256="b" * 64,
        active_version_identity_sha256="c" * 64,
    )


def _actual_rows_for_cases(cases):
    from shared.prediction_context import (
        MONTHLY_TARGET_RULE,
        WEEKLY_AVERAGE_TARGET_RULE,
        WEEKLY_TARGET_RULE,
    )

    daily = {}
    weekly = {}
    monthly = {}
    for case in cases:
        if case.frequency == "daily":
            key = (case.target_tenor, case.target_date)
            daily[key] = {
                "tenor": case.target_tenor,
                "trade_date": case.target_date,
                "direction_1d": 1,
                "direction_5d": -1,
            }
        elif case.frequency == "weekly":
            target_rule = (
                WEEKLY_AVERAGE_TARGET_RULE
                if case.task_type == "weekly_average"
                else WEEKLY_TARGET_RULE
            )
            key = (case.target_tenor, case.predict_date, target_rule)
            weekly[key] = {
                "tenor": case.target_tenor,
                "predict_date": case.predict_date,
                "feature_date": case.feature_date,
                "target_date": case.target_date,
                "direction_weekly": 1,
                "target_rule": target_rule,
            }
        else:
            key = (
                case.target_tenor,
                case.predict_date,
                MONTHLY_TARGET_RULE,
            )
            monthly[key] = {
                "tenor": case.target_tenor,
                "predict_date": case.predict_date,
                "feature_date": case.feature_date,
                "target_date": case.target_date,
                "direction_monthly": 0,
                "target_rule": MONTHLY_TARGET_RULE,
            }
    return list(daily.values()), list(weekly.values()), list(monthly.values())


def _weekly_asof_target():
    from harness.signal_gap_plan import RegistryTarget

    return RegistryTarget(
        registry_scheme_id="weekly_asof__h1__10Y",
        base_scheme_id="weekly_asof",
        runtime_type="blackbox_v2",
        frequency="weekly",
        task_type="weekly_point",
        target_tenor="10Y",
        horizon=1,
        scheme_version="weekly-asof-version",
        live_target_start_date="2026-06-01",
        live_boundary_source="platform_live_boundary_v1",
        input_mode="databridge_v1",
        code_sha256="d" * 64,
        config_sha256="e" * 64,
    )


def _weekly_asof_calendar(calendar_type, *, include_future):
    rows = _weekly_asof_rows(include_future=include_future)
    return calendar_type(
        trade_calendar_rows=rows,
        week_calendar_rows=rows,
        start_date="2024-12-20",
        as_of_date="2025-01-08",
    )


def _weekly_asof_rows(*, include_future):
    rows = [
        {"rdate": "2024-12-27", "trade_flag": "1", "week_id": 202452},
        {"rdate": "2024-12-28", "trade_flag": "0", "week_id": 202452},
        {"rdate": "2025-01-03", "trade_flag": "1", "week_id": 202501},
        {"rdate": "2025-01-04", "trade_flag": "0", "week_id": 202501},
        {"rdate": "2025-01-06", "trade_flag": "1", "week_id": 202502},
        {"rdate": "2025-01-07", "trade_flag": "1", "week_id": 202502},
        {"rdate": "2025-01-08", "trade_flag": "1", "week_id": 202502},
    ]
    if include_future:
        rows.extend(
            [
                {
                    "rdate": "2025-01-09",
                    "trade_flag": "1",
                    "week_id": 202502,
                },
                {
                    "rdate": "2025-01-10",
                    "trade_flag": "1",
                    "week_id": 202502,
                },
                {
                    "rdate": "2025-01-11",
                    "trade_flag": "0",
                    "week_id": 202502,
                },
                {
                    "rdate": "2025-01-17",
                    "trade_flag": "1",
                    "week_id": 202503,
                },
                {
                    "rdate": "2025-01-18",
                    "trade_flag": "0",
                    "week_id": 202503,
                },
            ]
        )
    return rows
