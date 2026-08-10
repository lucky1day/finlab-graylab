from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, event, text

from harness.context import GateContext
from harness.result import GateStatus


_SCHEME_ID = "native_daily"
_CURRENT_VERSION = "candidate-v2"
_REGISTRY_SCHEME_ID = "native_daily__h1__10Y"
_NATIVE_BUSINESS_IDENTITY = {
    "scheme_id": _SCHEME_ID,
    "runtime_type": "native_adapter",
    "horizon": 1,
    "task_type": "T+1",
    "frequency": "daily",
    "tenors": ["10Y"],
    "registry_scheme_ids": [_REGISTRY_SCHEME_ID],
}


class NativeMaintenanceAdmissionTests(unittest.TestCase):
    def test_admits_explicitly_missing_static_identity_only_with_canonical_legacy_receipt(self) -> None:
        """legacy Static 只缺身份字段时，可由单一受控 receipt 补足来源。"""
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        fixed_scheme_id = "weekly_10y_d_overlay_0529"
        fixed_prior_version = "prior-v1"
        fixed_prior_run_id = "hr-prior"
        fixed_registry_scheme_id = f"{fixed_scheme_id}__h6__10Y"
        fixed_identity = {
            "scheme_id": fixed_scheme_id,
            "runtime_type": "native_adapter",
            "horizon": 6,
            "task_type": "weekly_point",
            "frequency": "weekly",
            "tenors": ["10Y"],
            "registry_scheme_ids": [fixed_registry_scheme_id],
        }
        engine = _sqlite_engine()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_policy(root, (fixed_scheme_id,))
                with engine.begin() as conn:
                    _insert_registry_row_conn(
                        conn,
                        _registry_row(
                            scheme_id=fixed_registry_scheme_id,
                            base_scheme_id=fixed_scheme_id,
                            horizon=6,
                            task_type="weekly_point",
                            frequency="weekly",
                            target_tenor="10Y",
                        ),
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO t_scheme_versions
                                (scheme_id, scheme_version, runtime_type, status)
                            VALUES
                                (:scheme_id, :prior_version, 'native_adapter', 'active'),
                                (:scheme_id, 'candidate-v2', 'native_adapter', 'active')
                            """
                        ),
                        {
                            "scheme_id": fixed_scheme_id,
                            "prior_version": fixed_prior_version,
                        },
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO t_harness_runs
                                (harness_run_id, scheme_id, scheme_version, stage,
                                 status, finished_at)
                            VALUES
                                (:prior_run_id, :scheme_id, :prior_version, 'all',
                                 'passed', '2026-08-01 10:00:00')
                            """
                        ),
                        {
                            "prior_run_id": fixed_prior_run_id,
                            "scheme_id": fixed_scheme_id,
                            "prior_version": fixed_prior_version,
                        },
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO t_harness_gate_results
                                (harness_run_id, gate_name, status, summary_json)
                            VALUES
                                (:prior_run_id, 'compare', 'passed', NULL),
                                (:prior_run_id, 'static', 'passed', :static_summary)
                            """
                        ),
                        {
                            "prior_run_id": fixed_prior_run_id,
                            "static_summary": json.dumps(
                                {"passed": True, "evidence": [], "errors": []}
                            ),
                        },
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO t_harness_runs
                                (harness_run_id, scheme_id, scheme_version, stage,
                                 status, finished_at, triggered_by)
                            VALUES
                                ('lna_hr-prior', :scheme_id, NULL,
                                 'legacy-native-admission-attestation', 'passed',
                                 '2026-08-04 12:00:00',
                                 'native-legacy-admission-attestation-operator')
                            """
                        ),
                        {"scheme_id": fixed_scheme_id},
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO t_harness_gate_results
                                (harness_run_id, gate_name, status, summary_json)
                            VALUES
                                ('lna_hr-prior',
                                 'legacy-native-admission-attestation', 'passed',
                                 :summary_json)
                            """
                        ),
                        {
                            "summary_json": json.dumps(
                                {
                                    "passed": True,
                                    "evidence": [
                                        {
                                            "key": "legacy_native_admission_attestation",
                                            "value": {
                                                "schema_version": "legacy_native_admission_attestation_v1",
                                                "scope_scheme_id": fixed_scheme_id,
                                                "assertion": "operator_attests_legacy_native_admission_identity",
                                                "prior_admitted_scheme_version": fixed_prior_version,
                                                "prior_harness_run_id": fixed_prior_run_id,
                                                "business_identity": fixed_identity,
                                                "issued_by": "native-release-owner",
                                                "issued_at": "2026-08-04T12:00:00+00:00",
                                                "authorization_token_sha256": "a" * 64,
                                            },
                                            "detail": None,
                                        }
                                    ],
                                    "errors": [],
                                }
                            )
                        },
                    )

                result = NativeMaintenanceAdmissionGate().run(
                    GateContext(
                        scheme_id=fixed_scheme_id,
                        predict_date="2026-08-04",
                        project_root=root,
                        report_dir=root / "reports",
                        config=SimpleNamespace(
                            scheme_id=fixed_scheme_id,
                            scheme_version="candidate-v2",
                            runtime_type="native_adapter",
                            status="active",
                            tenors=["10Y"],
                            horizon=6,
                            task_type="weekly_point",
                            frequency="weekly",
                        ),
                        engine_factory=lambda: engine,
                    )
                )

            self.assertEqual(result.status, GateStatus.PASSED)
            self.assertTrue(result.passed)
            evidence = {item.key: item.value for item in result.evidence}
            self.assertEqual(
                evidence["admission_identity_source"],
                "legacy_operator_attestation_v1",
            )
            self.assertEqual(
                evidence["legacy_admission_attestation_harness_run_id"],
                "lna_hr-prior",
            )
        finally:
            engine.dispose()

    def test_admits_latest_prior_native_revision_with_matching_active_registry(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
            verify_native_maintenance_admission,
        )

        engine = _sqlite_engine()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_policy(root, (_SCHEME_ID,))
                _insert_registry_row(engine)
                _seed_prior_admission(
                    engine,
                    scheme_version="prior-v1",
                    harness_run_id="hr-a",
                    finished_at="2026-08-01 10:00:00",
                )
                _seed_prior_admission(
                    engine,
                    scheme_version="prior-v2",
                    harness_run_id="hr-z",
                    finished_at="2026-08-01 10:00:00",
                )
                _seed_current_candidate(engine, status="active")
                ctx = _context(root, engine)

                admission, errors = verify_native_maintenance_admission(ctx)
                result = NativeMaintenanceAdmissionGate().run(ctx)

                self.assertEqual(errors, [])
                self.assertIsNotNone(admission)
                assert admission is not None
                self.assertEqual(admission.prior_admitted_scheme_version, "prior-v2")
                self.assertEqual(admission.prior_harness_run_id, "hr-z")
                self.assertEqual(admission.registry_scheme_ids, (_REGISTRY_SCHEME_ID,))
                self.assertEqual(result.status, GateStatus.PASSED)
                self.assertTrue(result.passed)
                evidence = {item.key: item.value for item in result.evidence}
                self.assertEqual(
                    evidence["validation_profile"],
                    "native_post_admission_revision_v1",
                )
                self.assertEqual(
                    evidence["prior_admitted_scheme_version"],
                    "prior-v2",
                )
                self.assertEqual(evidence["prior_harness_run_id"], "hr-z")
                self.assertEqual(
                    evidence["registry_scheme_ids"],
                    [_REGISTRY_SCHEME_ID],
                )

                # The caller-owned test Engine remains usable and all read tables
                # retain their seed rows after a successful admission check.
                with engine.connect() as conn:
                    self.assertEqual(
                        conn.execute(
                            text("SELECT COUNT(*) FROM t_scheme_versions")
                        ).scalar_one(),
                        3,
                    )
                    self.assertEqual(
                        conn.execute(
                            text("SELECT COUNT(*) FROM t_scheme_registry")
                        ).scalar_one(),
                        1,
                    )
        finally:
            engine.dispose()

    def test_blocks_when_no_prior_active_native_revision_is_admitted(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        engine = _sqlite_engine()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_policy(root, (_SCHEME_ID,))
                _insert_registry_row(engine)

                result = NativeMaintenanceAdmissionGate().run(_context(root, engine))

            self.assertEqual(result.status, GateStatus.BLOCKED)
            self.assertFalse(result.passed)
            self.assertTrue(result.errors)
        finally:
            engine.dispose()

    def test_admits_pre_activation_paused_registry_with_matching_identity(self) -> None:
        """候选 Native version 激活前允许 Registry 保持统一 paused。"""
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        engine = _sqlite_engine()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_policy(root, (_SCHEME_ID,))
                _insert_registry_row(engine, status="paused", deployed_at=None)
                _seed_prior_admission(engine)
                _seed_current_candidate(engine, status="draft")

                result = NativeMaintenanceAdmissionGate().run(
                    _context(root, engine)
                )

            self.assertEqual(result.status, GateStatus.PASSED)
            self.assertTrue(result.passed)
            evidence = {item.key: item.value for item in result.evidence}
            self.assertEqual(
                evidence["current_candidate_runtime_type"],
                "native_adapter",
            )
            self.assertEqual(evidence["current_candidate_status"], "draft")
            self.assertEqual(evidence["registry_lifecycle"], "paused")
        finally:
            engine.dispose()

    def test_blocks_missing_or_invalid_current_exact_native_version(self) -> None:
        """维护准入必须绑定当前精确候选的 Native lifecycle。"""
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        cases = (
            ("missing", None, None),
            ("wrong_runtime", "blackbox_v2", "draft"),
            ("paused", "native_adapter", "paused"),
            ("archived", "native_adapter", "archived"),
        )
        for case, runtime_type, status in cases:
            with self.subTest(case=case):
                engine = _sqlite_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root, (_SCHEME_ID,))
                        _insert_registry_row(engine, status="paused", deployed_at=None)
                        _seed_prior_admission(engine)
                        if runtime_type is not None and status is not None:
                            _seed_current_candidate(
                                engine,
                                runtime_type=runtime_type,
                                status=status,
                            )

                        result = NativeMaintenanceAdmissionGate().run(
                            _context(root, engine)
                        )

                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertFalse(result.passed)
                    self.assertTrue(
                        any("current exact Native version" in error for error in result.errors),
                        result.errors,
                    )
                finally:
                    engine.dispose()

    def test_blocks_draft_current_version_with_active_registry(self) -> None:
        """draft 候选不得绑定已经 active 的业务 Registry。"""
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        engine = _sqlite_engine()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_policy(root, (_SCHEME_ID,))
                _insert_registry_row(engine)
                _seed_prior_admission(engine)
                _seed_current_candidate(engine, status="draft")

                result = NativeMaintenanceAdmissionGate().run(
                    _context(root, engine)
                )

            self.assertEqual(result.status, GateStatus.BLOCKED)
            self.assertFalse(result.passed)
            self.assertTrue(
                any(
                    "draft" in error and "registry" in error.lower()
                    for error in result.errors
                ),
                result.errors,
            )
        finally:
            engine.dispose()

    def test_blocks_when_prior_revision_lacks_passed_all_or_compare(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        for case, stage, compare_status in (
            ("not_all", "dry-run", "passed"),
            ("compare_not_passed", "all", "failed"),
        ):
            with self.subTest(case=case):
                engine = _sqlite_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root, (_SCHEME_ID,))
                        _insert_registry_row(engine)
                        _seed_prior_admission(
                            engine,
                            scheme_version="prior-v1",
                            harness_run_id="hr-prior",
                            stage=stage,
                            compare_status=compare_status,
                        )

                        result = NativeMaintenanceAdmissionGate().run(
                            _context(root, engine)
                        )

                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertFalse(result.passed)
                    self.assertTrue(result.errors)
                finally:
                    engine.dispose()

    def test_blocks_ambiguous_prior_compare_evidence(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        for duplicate_status in ("passed", "failed"):
            with self.subTest(duplicate_status=duplicate_status):
                engine = _sqlite_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root, (_SCHEME_ID,))
                        _insert_registry_row(engine)
                        _seed_prior_admission(engine)
                        with engine.begin() as conn:
                            conn.execute(
                                text(
                                    "INSERT INTO t_harness_gate_results "
                                    "(harness_run_id, gate_name, status) VALUES "
                                    "('hr-prior', 'compare', :status)"
                                ),
                                {"status": duplicate_status},
                            )

                        result = NativeMaintenanceAdmissionGate().run(
                            _context(root, engine)
                        )

                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertFalse(result.passed)
                    self.assertTrue(
                        any("compare" in error.lower() for error in result.errors),
                        result.errors,
                    )
                finally:
                    engine.dispose()

    def test_blocks_when_selected_prior_static_identity_snapshot_is_missing_malformed_or_mismatched(
        self,
    ) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        cases = (
            ("missing", None, False),
            ("malformed", "not-json", True),
            (
                "mismatched",
                _summary_json(
                    {
                        **_NATIVE_BUSINESS_IDENTITY,
                        "frequency": "weekly",
                    }
                ),
                True,
            ),
        )
        for case, static_summary_json, include_static_result in cases:
            with self.subTest(case=case):
                engine = _sqlite_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root, (_SCHEME_ID,))
                        _insert_registry_row(engine)
                        _seed_prior_admission(
                            engine,
                            scheme_version="prior-matching",
                            harness_run_id="hr-older",
                            finished_at="2026-08-01 10:00:00",
                        )
                        _seed_prior_admission(
                            engine,
                            scheme_version="prior-selected",
                            harness_run_id="hr-newer",
                            finished_at="2026-08-02 10:00:00",
                            static_summary_json=static_summary_json,
                            include_static_result=include_static_result,
                        )

                        result = NativeMaintenanceAdmissionGate().run(
                            _context(root, engine)
                        )

                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertFalse(result.passed)
                    self.assertTrue(
                        any(
                            "identity snapshot" in error.lower()
                            for error in result.errors
                        ),
                        result.errors,
                    )
                finally:
                    engine.dispose()

    def test_blocks_ambiguous_selected_prior_static_gate_identity_evidence(self) -> None:
        cases = (
            ("duplicate_static_gate_rows", _summary_json(_NATIVE_BUSINESS_IDENTITY), True),
            (
                "duplicate_identity_evidence",
                _summary_json(
                    _NATIVE_BUSINESS_IDENTITY,
                    duplicate_identity_evidence=True,
                ),
                False,
            ),
        )
        for case, static_summary_json, duplicate_static_row in cases:
            with self.subTest(case=case):
                self._assert_selected_snapshot_blocked(
                    static_summary_json,
                    duplicate_static_row=duplicate_static_row,
                )

    def test_blocks_selected_prior_noncanonical_static_identity_snapshot(self) -> None:
        cases = (
            (
                "extra_key",
                _summary_json(
                    {**_NATIVE_BUSINESS_IDENTITY, "unexpected": "value"}
                ),
                False,
            ),
            (
                "unsorted_tenors_and_registry_ids",
                _summary_json(
                    {
                        **_NATIVE_BUSINESS_IDENTITY,
                        "tenors": ["5Y", "10Y"],
                        "registry_scheme_ids": [
                            "native_daily__h1__5Y",
                            "native_daily__h1__10Y",
                        ],
                    }
                ),
                True,
            ),
        )
        for case, static_summary_json, multi_tenor in cases:
            with self.subTest(case=case):
                self._assert_selected_snapshot_blocked(
                    static_summary_json,
                    multi_tenor=multi_tenor,
                )

    def _assert_selected_snapshot_blocked(
        self,
        static_summary_json: str,
        *,
        duplicate_static_row: bool = False,
        multi_tenor: bool = False,
    ) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        engine = _sqlite_engine()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_policy(root, (_SCHEME_ID,))
                if multi_tenor:
                    _insert_multi_tenor_registry_rows(engine)
                else:
                    _insert_registry_row(engine)
                _seed_prior_admission(
                    engine,
                    scheme_version="prior-selected",
                    harness_run_id="hr-selected",
                    static_summary_json=static_summary_json,
                )
                if duplicate_static_row:
                    _insert_static_gate_result(
                        engine,
                        harness_run_id="hr-selected",
                        summary_json=static_summary_json,
                    )

                result = NativeMaintenanceAdmissionGate().run(
                    _multi_tenor_context(root, engine)
                    if multi_tenor
                    else _context(root, engine)
                )

            self.assertEqual(result.status, GateStatus.BLOCKED)
            self.assertFalse(result.passed)
            self.assertTrue(
                any(
                    "identity snapshot" in error.lower() for error in result.errors
                ),
                result.errors,
            )
        finally:
            engine.dispose()

    def test_blocks_non_active_non_native_or_policy_denied_candidate(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        cases = (
            (
                "paused",
                {"status": "paused"},
                (_SCHEME_ID,),
                "status",
            ),
            (
                "blackbox",
                {"runtime_type": "blackbox_v2"},
                (_SCHEME_ID,),
                "runtime_type",
            ),
            (
                "policy_denied",
                {},
                ("other_native",),
                "maintenance-only",
            ),
        )
        for case, config_overrides, policy_ids, expected_error in cases:
            with self.subTest(case=case):
                engine = _sqlite_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root, policy_ids)
                        result = NativeMaintenanceAdmissionGate().run(
                            _context(root, engine, **config_overrides)
                        )

                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertFalse(result.passed)
                    self.assertTrue(
                        any(expected_error in error for error in result.errors),
                        result.errors,
                    )
                finally:
                    engine.dispose()

    def test_blocks_config_scheme_id_mismatch_before_any_database_read(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        engine = _sqlite_engine()
        statements: list[str] = []
        event.listen(
            engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _parameters, _context, _many: (
                statements.append(statement)
            ),
        )
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_policy(root, ("other_native",))

                result = NativeMaintenanceAdmissionGate().run(
                    _context(root, engine, scheme_id="other_native")
                )

            self.assertEqual(result.status, GateStatus.BLOCKED)
            self.assertFalse(result.passed)
            self.assertTrue(
                any("scheme_id mismatch" in error for error in result.errors),
                result.errors,
            )
            self.assertEqual(statements, [])
        finally:
            engine.dispose()

    def test_blocks_registry_identity_drift_without_repairing_it(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        cases = (
            ("base_scheme_id", "UPDATE t_scheme_registry SET base_scheme_id = 'other_native'", None),
            ("task_type", "UPDATE t_scheme_registry SET task_type = 'T+5'", None),
            ("frequency", "UPDATE t_scheme_registry SET frequency = 'weekly'", None),
            ("horizon", "UPDATE t_scheme_registry SET horizon = 5", None),
            ("target_tenor", "UPDATE t_scheme_registry SET target_tenor = '5Y'", None),
            ("runtime_type", "UPDATE t_scheme_registry SET runtime_type = 'blackbox_v2'", None),
            ("status", "UPDATE t_scheme_registry SET status = 'archived'", None),
            ("missing", "DELETE FROM t_scheme_registry", None),
            ("extra_active", None, _extra_active_registry_row()),
        )
        for case, mutation, extra_row in cases:
            with self.subTest(case=case):
                engine = _sqlite_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root, (_SCHEME_ID,))
                        _insert_registry_row(engine)
                        _seed_prior_admission(engine)
                        with engine.begin() as conn:
                            if mutation is not None:
                                conn.execute(text(mutation))
                            if extra_row is not None:
                                _insert_registry_row_conn(conn, extra_row)

                        result = NativeMaintenanceAdmissionGate().run(
                            _context(root, engine)
                        )

                        with engine.connect() as conn:
                            count = conn.execute(
                                text("SELECT COUNT(*) FROM t_scheme_registry")
                            ).scalar_one()

                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertFalse(result.passed)
                    self.assertTrue(
                        any("registry" in error.lower() for error in result.errors),
                        result.errors,
                    )
                    self.assertEqual(count, 2 if case == "extra_active" else 0 if case == "missing" else 1)
                finally:
                    engine.dispose()


class NativeMaintenanceStageRegistryTests(unittest.TestCase):
    def test_stage_all_and_auto_sequence_exclude_api_readiness(self) -> None:
        from harness.registry import AUTO_SEQUENCE, sequence_for_stage

        expected = [
            "static",
            "input",
            "unit",
            "dry-run",
            "compare",
            "backtest",
        ]
        self.assertEqual(AUTO_SEQUENCE, expected)
        self.assertEqual(sequence_for_stage("all"), expected)

    def test_native_maintenance_stage_expands_only_to_its_five_gates(self) -> None:
        from harness.registry import gates_for_stage, sequence_for_stage

        expected = [
            "static",
            "native-maintenance-admission",
            "input",
            "unit",
            "dry-run",
        ]
        ctx = GateContext(
            scheme_id=_SCHEME_ID,
            predict_date="2026-08-04",
            project_root=Path("/tmp/native-maintenance"),
            report_dir=Path("/tmp/native-maintenance/reports"),
            config=_config(),
        )

        self.assertEqual(sequence_for_stage("native-maintenance"), expected)
        self.assertEqual(
            [gate.name for gate in gates_for_stage("native-maintenance", ctx=ctx)],
            expected,
        )
        with self.assertRaisesRegex(ValueError, "unsupported onboard stage"):
            sequence_for_stage("native-maintenance-admission")

    def test_blackbox_dispatch_rejects_native_maintenance_stage(self) -> None:
        from harness.registry import gates_for_stage

        ctx = GateContext(
            scheme_id="blackbox_daily",
            predict_date="2026-08-04",
            project_root=Path("/tmp/native-maintenance"),
            report_dir=Path("/tmp/native-maintenance/reports"),
            config=SimpleNamespace(runtime_type="blackbox_v2"),
        )

        with self.assertRaisesRegex(ValueError, "Blackbox"):
            gates_for_stage("native-maintenance", ctx=ctx)

    def test_check_only_cli_rejects_native_maintenance_stage(self) -> None:
        from harness.cli import main

        with self.assertRaisesRegex(SystemExit, "check-only requires --stage all"):
            main(
                [
                    "onboard",
                    _SCHEME_ID,
                    "--predict-date",
                    "2026-08-04",
                    "--stage",
                    "native-maintenance",
                    "--check-only",
                ]
            )


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "scheme_id": _SCHEME_ID,
        "scheme_version": _CURRENT_VERSION,
        "runtime_type": "native_adapter",
        "status": "active",
        "tenors": ["10Y"],
        "horizon": 1,
        "task_type": "T+1",
        "frequency": "daily",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(root: Path, engine, **config_overrides: object) -> GateContext:
    return GateContext(
        scheme_id=_SCHEME_ID,
        predict_date="2026-08-04",
        project_root=root,
        report_dir=root / "reports",
        config=_config(**config_overrides),
        engine_factory=lambda: engine,
    )


def _multi_tenor_context(root: Path, engine) -> GateContext:
    return _context(root, engine, tenors=["5Y", "10Y"])


def _write_policy(root: Path, scheme_ids: tuple[str, ...]) -> None:
    policy_path = root / "deploy" / "onboarding_policy_v1.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(
            {
                "policy_version": "1.0",
                "new_scheme_runtime_type": "blackbox_v2",
                "native_v1_mode": "maintenance_only",
                "legacy_native_scheme_ids": sorted(scheme_ids),
            }
        ),
        encoding="utf-8",
    )


def _sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_versions (
                    scheme_id TEXT,
                    scheme_version TEXT,
                    runtime_type TEXT,
                    status TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_harness_runs (
                    harness_run_id TEXT,
                    scheme_id TEXT,
                    scheme_version TEXT,
                    stage TEXT,
                    status TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    triggered_by TEXT,
                    project_root TEXT,
                    git_commit TEXT,
                    code_hash TEXT,
                    config_hash TEXT,
                    report_uri TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_harness_gate_results (
                    harness_run_id TEXT,
                    gate_name TEXT,
                    status TEXT,
                    summary_json TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_scheme_registry (
                    scheme_id TEXT,
                    base_scheme_id TEXT,
                    name TEXT,
                    description TEXT,
                    horizon INTEGER,
                    task_type TEXT,
                    runtime_type TEXT,
                    tenors TEXT,
                    frequency TEXT,
                    target_tenor TEXT,
                    schedule_cron TEXT,
                    schedule_timezone TEXT,
                    status TEXT,
                    deployed_at TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_runs (
                    id INTEGER PRIMARY KEY,
                    scheme_id TEXT,
                    benchmark_id TEXT,
                    data_source TEXT,
                    updated_at TEXT,
                    status TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE t_backtest_predictions (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER,
                    scheme_id TEXT
                )
                """
            )
        )
    return engine


def _seed_prior_admission(
    engine,
    *,
    scheme_version: str = "prior-v1",
    harness_run_id: str = "hr-prior",
    stage: str = "all",
    run_status: str = "passed",
    compare_status: str = "passed",
    finished_at: str = "2026-08-01 10:00:00",
    static_summary_json: str | None = None,
    include_static_result: bool = True,
    business_identity: dict[str, object] | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_versions
                    (scheme_id, scheme_version, runtime_type, status)
                VALUES
                    (:scheme_id, :scheme_version, 'native_adapter', 'active')
                """
            ),
            {"scheme_id": _SCHEME_ID, "scheme_version": scheme_version},
        )
        conn.execute(
            text(
                """
                INSERT INTO t_harness_runs
                    (harness_run_id, scheme_id, scheme_version, stage, status, finished_at)
                VALUES
                    (:harness_run_id, :scheme_id, :scheme_version, :stage, :status, :finished_at)
                """
            ),
            {
                "harness_run_id": harness_run_id,
                "scheme_id": _SCHEME_ID,
                "scheme_version": scheme_version,
                "stage": stage,
                "status": run_status,
                "finished_at": finished_at,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO t_harness_gate_results
                    (harness_run_id, gate_name, status)
                VALUES
                    (:harness_run_id, 'compare', :status)
                """
            ),
            {"harness_run_id": harness_run_id, "status": compare_status},
        )
        if include_static_result:
            _insert_static_gate_result_conn(
                conn,
                harness_run_id=harness_run_id,
                summary_json=(
                    _summary_json(
                        _NATIVE_BUSINESS_IDENTITY
                        if business_identity is None
                        else business_identity
                    )
                    if static_summary_json is None
                    else static_summary_json
                ),
            )


def _seed_current_candidate(
    engine,
    *,
    scheme_version: str = _CURRENT_VERSION,
    runtime_type: str = "native_adapter",
    status: str = "draft",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_versions
                    (scheme_id, scheme_version, runtime_type, status)
                VALUES
                    (:scheme_id, :scheme_version, :runtime_type, :status)
                """
            ),
            {
                "scheme_id": _SCHEME_ID,
                "scheme_version": scheme_version,
                "runtime_type": runtime_type,
                "status": status,
            },
        )


def _insert_static_gate_result(
    engine,
    *,
    harness_run_id: str,
    summary_json: str,
) -> None:
    with engine.begin() as conn:
        _insert_static_gate_result_conn(
            conn,
            harness_run_id=harness_run_id,
            summary_json=summary_json,
        )


def _insert_static_gate_result_conn(
    conn,
    *,
    harness_run_id: str,
    summary_json: str,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO t_harness_gate_results
                (harness_run_id, gate_name, status, summary_json)
            VALUES
                (:harness_run_id, 'static', 'passed', :summary_json)
            """
        ),
        {
            "harness_run_id": harness_run_id,
            "summary_json": summary_json,
        },
    )


def _summary_json(
    snapshot: dict[str, object],
    *,
    duplicate_identity_evidence: bool = False,
) -> str:
    evidence = [
        {
            "key": "native_business_identity",
            "value": snapshot,
            "detail": None,
        }
    ]
    if duplicate_identity_evidence:
        evidence.append(
            {
                "key": "native_business_identity",
                "value": snapshot,
                "detail": None,
            }
        )
    return json.dumps(
        {
            "passed": True,
            "evidence": evidence,
            "errors": [],
        }
    )


def _registry_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "scheme_id": _REGISTRY_SCHEME_ID,
        "base_scheme_id": _SCHEME_ID,
        "name": "Native Daily",
        "description": "Native Daily",
        "horizon": 1,
        "task_type": "T+1",
        "runtime_type": "native_adapter",
        "tenors": '["10Y"]',
        "frequency": "daily",
        "target_tenor": "10Y",
        "schedule_cron": "25 9 * * 1-5",
        "schedule_timezone": "Asia/Shanghai",
        "status": "active",
        "deployed_at": "2026-08-01 10:00:00",
    }
    row.update(overrides)
    return row


def _extra_active_registry_row() -> dict[str, object]:
    return _registry_row(
        scheme_id="native_daily__h5__5Y",
        horizon=5,
        target_tenor="5Y",
    )


def _insert_registry_row(engine, **overrides: object) -> None:
    with engine.begin() as conn:
        _insert_registry_row_conn(conn, _registry_row(**overrides))


def _insert_multi_tenor_registry_rows(engine) -> None:
    _insert_registry_row(
        engine,
        scheme_id="native_daily__h1__5Y",
        target_tenor="5Y",
        tenors='["5Y"]',
    )
    _insert_registry_row(engine)


def _insert_registry_row_conn(conn, row: dict[str, object]) -> None:
    conn.execute(
        text(
            """
            INSERT INTO t_scheme_registry (
                scheme_id, base_scheme_id, name, description, horizon, task_type,
                runtime_type, tenors, frequency, target_tenor, schedule_cron,
                schedule_timezone, status, deployed_at
            ) VALUES (
                :scheme_id, :base_scheme_id, :name, :description, :horizon,
                :task_type, :runtime_type, :tenors, :frequency, :target_tenor,
                :schedule_cron, :schedule_timezone, :status, :deployed_at
            )
            """
        ),
        row,
    )


if __name__ == "__main__":
    unittest.main()
