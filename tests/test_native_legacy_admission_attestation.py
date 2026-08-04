from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, event, text

from harness.authorization import issue_token, verify_authorization
from harness.context import GateContext
from harness.result import Evidence, GateResult, GateStatus


_SCHEME_ID = "weekly_10y_d_overlay_0529"
_CURRENT_VERSION = "candidate-v2"
_PRIOR_VERSION = "prior-v1"
_PRIOR_RUN_ID = "hr-prior"


class NativeLegacyAdmissionAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_secret = os.environ.get("HARNESS_AUTH_SECRET")
        os.environ["HARNESS_AUTH_SECRET"] = "legacy-attestation-test-secret"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write_policy(self.root)
        self.engine = _sqlite_engine()
        _seed_prior_admission(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        if self._previous_secret is None:
            os.environ.pop("HARNESS_AUTH_SECRET", None)
        else:
            os.environ["HARNESS_AUTH_SECRET"] = self._previous_secret
        self._tmp.cleanup()

    def test_records_a_single_canonical_receipt_for_the_selected_prior_admission(self) -> None:
        from harness.legacy_native_admission_attestation import (
            LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
            LEGACY_NATIVE_ADMISSION_ATTEST_GATE_NAME,
            LEGACY_NATIVE_ADMISSION_ATTEST_STAGE,
            run_legacy_native_admission_attestation,
        )

        token = issue_token(
            _SCHEME_ID,
            LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
            scheme_version=_PRIOR_VERSION,
            harness_run_id=_PRIOR_RUN_ID,
            issued_by="native-release-owner",
            ttl_seconds=300,
        )

        result = run_legacy_native_admission_attestation(
            _context(self.root, self.engine, token)
        )

        self.assertEqual(result.status, GateStatus.PASSED)
        self.assertTrue(result.passed)
        with self.engine.connect() as conn:
            run = conn.execute(
                text(
                    "SELECT scheme_version, code_hash, config_hash, stage, status "
                    "FROM t_harness_runs "
                    "WHERE stage = 'legacy-native-admission-attestation'"
                )
            ).mappings().one()
            gate = conn.execute(
                text(
                    "SELECT gate_name, status, summary_json "
                    "FROM t_harness_gate_results "
                    "WHERE harness_run_id = 'lna_hr-prior'"
                )
            ).mappings().one()

        self.assertIsNone(run["scheme_version"])
        self.assertIsNone(run["code_hash"])
        self.assertIsNone(run["config_hash"])
        self.assertEqual(run["stage"], LEGACY_NATIVE_ADMISSION_ATTEST_STAGE)
        self.assertEqual(run["status"], "passed")
        self.assertEqual(gate["gate_name"], LEGACY_NATIVE_ADMISSION_ATTEST_GATE_NAME)
        self.assertEqual(gate["status"], "passed")
        summary = json.loads(gate["summary_json"])
        self.assertEqual(len(summary["evidence"]), 1)
        payload = summary["evidence"][0]["value"]
        self.assertEqual(payload["scope_scheme_id"], _SCHEME_ID)
        self.assertEqual(payload["prior_admitted_scheme_version"], _PRIOR_VERSION)
        self.assertEqual(payload["prior_harness_run_id"], _PRIOR_RUN_ID)
        self.assertEqual(
            payload["business_identity"]["registry_scheme_ids"],
            [f"{_SCHEME_ID}__h6__10Y"],
        )

    def test_rejects_static_summary_with_errors_when_identity_is_absent(self) -> None:
        from harness.legacy_native_admission_attestation import (
            LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
            run_legacy_native_admission_attestation,
        )

        _replace_static_summary(
            self.engine,
            {"passed": True, "evidence": [], "errors": ["legacy failure"]},
        )
        token = issue_token(
            _SCHEME_ID,
            LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
            scheme_version=_PRIOR_VERSION,
            harness_run_id=_PRIOR_RUN_ID,
            issued_by="native-release-owner",
            ttl_seconds=300,
        )

        result = run_legacy_native_admission_attestation(
            _context(self.root, self.engine, token)
        )

        self.assertEqual(result.status, GateStatus.BLOCKED)
        self.assertFalse(result.passed)
        self.assertEqual(_attestation_run_count(self.engine), 0)

    def test_issue_rejects_wrong_scope_and_missing_required_binding_fields(self) -> None:
        from harness.legacy_native_admission_attestation import (
            LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
        )

        cases = (
            ("wrong_scheme", "other_scheme", _PRIOR_VERSION, _PRIOR_RUN_ID, "operator", 300),
            ("missing_version", _SCHEME_ID, None, _PRIOR_RUN_ID, "operator", 300),
            ("missing_run", _SCHEME_ID, _PRIOR_VERSION, None, "operator", 300),
            ("missing_issuer", _SCHEME_ID, _PRIOR_VERSION, _PRIOR_RUN_ID, "", 300),
            ("missing_expiry", _SCHEME_ID, _PRIOR_VERSION, _PRIOR_RUN_ID, "operator", None),
            ("long_expiry", _SCHEME_ID, _PRIOR_VERSION, _PRIOR_RUN_ID, "operator", 901),
        )
        for label, scheme_id, version, run_id, issued_by, ttl in cases:
            with self.subTest(label=label), self.assertRaises(ValueError):
                issue_token(
                    scheme_id,
                    LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
                    scheme_version=version,
                    harness_run_id=run_id,
                    issued_by=issued_by,
                    ttl_seconds=ttl,
                )

    def test_blocks_missing_wrong_action_or_wrong_prior_binding_without_writing(self) -> None:
        from harness.legacy_native_admission_attestation import (
            LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
            run_legacy_native_admission_attestation,
        )

        tokens = {
            "missing": None,
            "wrong_action": issue_token(_SCHEME_ID, "activate"),
            "wrong_version": issue_token(
                _SCHEME_ID,
                LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
                scheme_version="different-version",
                harness_run_id=_PRIOR_RUN_ID,
                issued_by="operator",
                ttl_seconds=300,
            ),
            "wrong_run": issue_token(
                _SCHEME_ID,
                LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
                scheme_version=_PRIOR_VERSION,
                harness_run_id="different-run",
                issued_by="operator",
                ttl_seconds=300,
            ),
        }
        for label, token in tokens.items():
            with self.subTest(label=label):
                result = run_legacy_native_admission_attestation(
                    _context(self.root, self.engine, token)
                )
                self.assertEqual(result.status, GateStatus.BLOCKED)
                self.assertFalse(result.passed)
                self.assertEqual(_attestation_run_count(self.engine), 0)

    def test_blocks_expired_and_replayed_tokens(self) -> None:
        from harness.legacy_native_admission_attestation import (
            run_legacy_native_admission_attestation,
        )

        expired = _expired_token(_issue_valid_token())
        expired_result = run_legacy_native_admission_attestation(
            _context(self.root, self.engine, expired)
        )
        self.assertEqual(expired_result.status, GateStatus.BLOCKED)
        self.assertEqual(_attestation_run_count(self.engine), 0)

        token = _issue_valid_token()
        first = run_legacy_native_admission_attestation(
            _context(self.root, self.engine, token)
        )
        second = run_legacy_native_admission_attestation(
            _context(self.root, self.engine, token)
        )
        self.assertEqual(first.status, GateStatus.PASSED)
        self.assertEqual(second.status, GateStatus.BLOCKED)
        self.assertTrue(
            any("already used" in error for error in second.errors), second.errors
        )

    def test_blocks_nonpassed_or_duplicate_prior_compare(self) -> None:
        from harness.legacy_native_admission_attestation import (
            run_legacy_native_admission_attestation,
        )

        for label, mutate in (
            ("failed", _set_prior_compare_failed),
            ("duplicate", _insert_duplicate_prior_compare),
        ):
            with self.subTest(label=label):
                engine = _sqlite_engine()
                try:
                    _seed_prior_admission(engine)
                    mutate(engine)
                    result = run_legacy_native_admission_attestation(
                        _context(self.root, engine, _issue_valid_token())
                    )
                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertEqual(_attestation_run_count(engine), 0)
                finally:
                    engine.dispose()

    def test_blocks_missing_or_invalid_current_exact_native_candidate(self) -> None:
        from harness.legacy_native_admission_attestation import (
            run_legacy_native_admission_attestation,
        )

        cases = (
            ("missing", _remove_current_candidate),
            ("wrong_runtime", lambda engine: _set_current_candidate(engine, runtime_type="blackbox_v2", status="draft")),
            ("paused", lambda engine: _set_current_candidate(engine, runtime_type="native_adapter", status="paused")),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                engine = _sqlite_engine()
                try:
                    _seed_prior_admission(engine)
                    mutate(engine)
                    result = run_legacy_native_admission_attestation(
                        _context(self.root, engine, _issue_valid_token())
                    )
                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertTrue(
                        any("current exact Native version" in error for error in result.errors),
                        result.errors,
                    )
                    self.assertEqual(_attestation_run_count(engine), 0)
                finally:
                    engine.dispose()

    def test_blocks_current_config_that_drifts_from_fixed_10y_business_identity(self) -> None:
        from harness.legacy_native_admission_attestation import (
            run_legacy_native_admission_attestation,
        )

        for label, overrides in (
            ("task_type", {"task_type": "weekly_average"}),
            ("horizon", {"horizon": 5}),
            ("tenor", {"tenors": ["5Y"]}),
        ):
            with self.subTest(label=label):
                engine = _sqlite_engine()
                try:
                    _seed_prior_admission(engine)
                    ctx = _context(self.root, engine, _issue_valid_token())
                    config = SimpleNamespace(
                        **{**ctx.config.__dict__, **overrides}
                    )
                    result = run_legacy_native_admission_attestation(
                        GateContext(**{**ctx.__dict__, "config": config})
                    )

                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertTrue(
                        any(
                            "fixed 10Y business identity" in error
                            for error in result.errors
                        ),
                        result.errors,
                    )
                    self.assertEqual(_attestation_run_count(engine), 0)
                finally:
                    engine.dispose()

    def test_blocks_malformed_duplicate_or_existing_static_identity(self) -> None:
        from harness.legacy_native_admission_attestation import (
            run_legacy_native_admission_attestation,
        )

        identity = _business_identity()
        cases = {
            "malformed": "not-json",
            "duplicate_identity": {
                "passed": True,
                "evidence": [_identity_evidence(identity), _identity_evidence(identity)],
                "errors": [],
            },
            "already_present": {
                "passed": True,
                "evidence": [_identity_evidence(identity)],
                "errors": [],
            },
        }
        for label, summary in cases.items():
            with self.subTest(label=label):
                engine = _sqlite_engine()
                try:
                    _seed_prior_admission(engine)
                    _replace_static_summary(engine, summary)
                    result = run_legacy_native_admission_attestation(
                        _context(self.root, engine, _issue_valid_token())
                    )
                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertEqual(_attestation_run_count(engine), 0)
                finally:
                    engine.dispose()

    def test_blocks_existing_duplicate_or_noncanonical_receipt(self) -> None:
        from harness.legacy_native_admission_attestation import (
            run_legacy_native_admission_attestation,
        )

        for label, seed in (
            ("existing", lambda engine: _insert_receipt(engine)),
            ("duplicate", lambda engine: _insert_receipt(engine, duplicate=True)),
            ("noncanonical", lambda engine: _insert_receipt(engine, triggered_by="wrong")),
        ):
            with self.subTest(label=label):
                engine = _sqlite_engine()
                try:
                    _seed_prior_admission(engine)
                    seed(engine)
                    result = run_legacy_native_admission_attestation(
                        _context(self.root, engine, _issue_valid_token())
                    )
                    self.assertEqual(result.status, GateStatus.BLOCKED)
                    self.assertFalse(result.passed)
                finally:
                    engine.dispose()

    def test_blocks_receipt_identity_drift_and_does_not_create_maintenance_history(self) -> None:
        from harness.gates.activate_gate import _passed_native_maintenance_validation
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        _seed_registry(self.engine)
        _set_current_candidate_active(self.engine)
        drifted_identity = _business_identity()
        drifted_identity["task_type"] = "weekly_average"
        _insert_receipt(self.engine, business_identity=drifted_identity)
        ctx = _context(self.root, self.engine, _issue_valid_token())
        result = NativeMaintenanceAdmissionGate().run(ctx)
        run_id, errors, history_available = _passed_native_maintenance_validation(
            ctx,
            _CURRENT_VERSION,
        )

        self.assertEqual(result.status, GateStatus.BLOCKED)
        self.assertTrue(
            any("fixed 10Y business identity" in error for error in result.errors),
            result.errors,
        )
        self.assertIsNone(run_id)
        self.assertTrue(history_available)
        self.assertTrue(any("native-maintenance" in error for error in errors), errors)

    def test_blocks_forged_receipt_that_matches_a_drifted_current_config(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        drifted_identity = _business_identity()
        drifted_identity["task_type"] = "weekly_average"
        _seed_registry(self.engine, task_type="weekly_average")
        _set_current_candidate_active(self.engine)
        _insert_receipt(self.engine, business_identity=drifted_identity)
        ctx = _context(self.root, self.engine, _issue_valid_token())
        drifted_ctx = GateContext(
            **{
                **ctx.__dict__,
                "config": SimpleNamespace(
                    **{**ctx.config.__dict__, "task_type": "weekly_average"}
                ),
            }
        )

        result = NativeMaintenanceAdmissionGate().run(drifted_ctx)

        self.assertEqual(result.status, GateStatus.BLOCKED)
        self.assertTrue(
            any("fixed 10Y business identity" in error for error in result.errors),
            result.errors,
        )

    def test_valid_receipt_alone_does_not_satisfy_maintenance_history_for_activation(self) -> None:
        from harness.gates.activate_gate import _passed_native_maintenance_validation
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        _seed_registry(self.engine)
        _set_current_candidate_active(self.engine)
        _insert_receipt(self.engine)
        ctx = _context(self.root, self.engine, _issue_valid_token())
        admission = NativeMaintenanceAdmissionGate().run(ctx)
        run_id, errors, history_available = _passed_native_maintenance_validation(
            ctx,
            _CURRENT_VERSION,
        )

        self.assertEqual(admission.status, GateStatus.PASSED)
        self.assertIsNone(run_id)
        self.assertTrue(history_available)
        self.assertTrue(any("native-maintenance" in error for error in errors), errors)
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM t_harness_runs "
                        "WHERE stage = 'native-maintenance'"
                    )
                ).scalar_one(),
                0,
            )

    def test_persistence_failure_consumes_token_and_never_writes_business_tables(self) -> None:
        from harness.legacy_native_admission_attestation import (
            LegacyNativeAdmissionAttestationPersistenceError,
            run_legacy_native_admission_attestation,
        )

        token = _issue_valid_token()
        with patch(
            "harness.legacy_native_admission_attestation.persist_legacy_native_admission_attestation",
            side_effect=LegacyNativeAdmissionAttestationPersistenceError("db failed"),
        ):
            result = run_legacy_native_admission_attestation(
                _context(self.root, self.engine, token)
            )
        _auth, errors = verify_authorization(
            token,
            scheme_id=_SCHEME_ID,
            action="native_legacy_admission_identity_attest",
            used_store_path=self.root / "reports" / "harness" / ".used_authorization_tokens.json",
        )

        self.assertEqual(result.status, GateStatus.FAILED)
        self.assertFalse(result.passed)
        self.assertEqual(
            {item.key: item.value for item in result.evidence}["token_consumed"],
            True,
        )
        self.assertTrue(any("already used" in error for error in errors), errors)
        self.assertEqual(_attestation_run_count(self.engine), 0)

    def test_successful_command_writes_only_the_two_harness_tables(self) -> None:
        from harness.legacy_native_admission_attestation import (
            run_legacy_native_admission_attestation,
        )

        writes: list[str] = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            normalized = statement.lstrip().lower()
            if normalized.startswith(("insert", "update", "delete")):
                writes.append(normalized)

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            result = run_legacy_native_admission_attestation(
                _context(self.root, self.engine, _issue_valid_token())
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.assertEqual(result.status, GateStatus.PASSED)
        self.assertEqual(len(writes), 2)
        self.assertTrue(all("t_harness_runs" in sql or "t_harness_gate_results" in sql for sql in writes))
        self.assertFalse(any("t_scheme_predictions" in sql for sql in writes))

    def test_cli_dispatches_fixed_command_and_auth_issue_rejects_other_scope(self) -> None:
        from harness.cli import main

        now = "2026-08-04T00:00:00+00:00"
        result = GateResult(
            gate_name="legacy-native-admission-attestation",
            status=GateStatus.PASSED,
            passed=True,
            evidence=[Evidence("checked", True)],
            errors=[],
            started_at=now,
            finished_at=now,
        )
        with patch(
            "harness.cli._run_native_legacy_admission_attestation",
            return_value=result,
        ) as runner:
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "native-legacy-admission-attest",
                        "--scheme-id",
                        _SCHEME_ID,
                        "--authorize",
                        "token",
                        "--project-root",
                        str(self.root),
                    ]
                )
        self.assertEqual(code, 0)
        runner.assert_called_once()

        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(
                [
                    "auth",
                    "issue",
                    "--scheme-id",
                    "other_scheme",
                    "--action",
                    "native_legacy_admission_identity_attest",
                    "--scheme-version",
                    _PRIOR_VERSION,
                    "--harness-run-id",
                    _PRIOR_RUN_ID,
                    "--issued-by",
                    "operator",
                    "--expires-in",
                    "300",
                ]
            )

    def test_cli_reuses_and_disposes_one_attestation_engine(self) -> None:
        from harness.cli import _run_native_legacy_admission_attestation

        engine = MagicMock()
        expected = GateResult(
            gate_name="legacy-native-admission-attestation",
            status=GateStatus.PASSED,
            passed=True,
            evidence=[],
            errors=[],
            started_at="2026-08-04T00:00:00+00:00",
            finished_at="2026-08-04T00:00:00+00:00",
        )
        args = SimpleNamespace(
            project_root=self.root,
            report_dir=self.root / "reports",
            scheme_id=_SCHEME_ID,
            authorize="token",
        )
        with (
            patch("harness.cli.create_engine_from_env", return_value=engine) as create,
            patch("harness.cli._load_config_for_dispatch", return_value=SimpleNamespace()),
            patch(
                "harness.cli.run_legacy_native_admission_attestation",
                return_value=expected,
            ) as run,
        ):
            actual = _run_native_legacy_admission_attestation(args)

        self.assertIs(actual, expected)
        create.assert_called_once()
        ctx = run.call_args.args[0]
        self.assertIs(ctx.engine_factory(), engine)
        engine.dispose.assert_called_once()


def _context(root: Path, engine, token: str) -> GateContext:
    return GateContext(
        scheme_id=_SCHEME_ID,
        predict_date="legacy-admission-attestation",
        project_root=root,
        report_dir=root / "reports",
        config=SimpleNamespace(
            scheme_id=_SCHEME_ID,
            scheme_version=_CURRENT_VERSION,
            runtime_type="native_adapter",
            status="active",
            tenors=["10Y"],
            horizon=6,
            task_type="weekly_point",
            frequency="weekly",
        ),
        authorization=token,
        engine_factory=lambda: engine,
    )


def _write_policy(root: Path) -> None:
    path = root / "deploy" / "onboarding_policy_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "policy_version": "1.0",
                "new_scheme_runtime_type": "blackbox_v2",
                "native_v1_mode": "maintenance_only",
                "legacy_native_scheme_ids": [_SCHEME_ID],
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
                    harness_run_id TEXT PRIMARY KEY,
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
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    harness_run_id TEXT,
                    gate_name TEXT,
                    status TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    summary_json TEXT,
                    report_uri TEXT
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
    return engine


def _seed_prior_admission(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_versions
                    (scheme_id, scheme_version, runtime_type, status)
                VALUES
                    (:scheme_id, :prior_version, 'native_adapter', 'active'),
                    (:scheme_id, :current_version, 'native_adapter', 'draft')
                """
            ),
            {
                "scheme_id": _SCHEME_ID,
                "prior_version": _PRIOR_VERSION,
                "current_version": _CURRENT_VERSION,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO t_harness_runs
                    (harness_run_id, scheme_id, scheme_version, stage, status,
                     started_at, finished_at)
                VALUES
                    (:run_id, :scheme_id, :scheme_version, 'all', 'passed',
                     '2026-06-11 05:55:00', '2026-06-11 05:56:00')
                """
            ),
            {
                "run_id": _PRIOR_RUN_ID,
                "scheme_id": _SCHEME_ID,
                "scheme_version": _PRIOR_VERSION,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO t_harness_gate_results
                    (harness_run_id, gate_name, status, summary_json)
                VALUES
                    (:run_id, 'compare', 'passed', NULL),
                    (:run_id, 'static', 'passed', :summary_json)
                """
            ),
            {
                "run_id": _PRIOR_RUN_ID,
                "summary_json": json.dumps(
                    {"passed": True, "evidence": [], "errors": []}
                ),
            },
        )


def _replace_static_summary(engine, summary: object) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE t_harness_gate_results
                SET summary_json = :summary_json
                WHERE harness_run_id = :run_id
                  AND gate_name = 'static'
                """
            ),
            {
                "run_id": _PRIOR_RUN_ID,
                "summary_json": (
                    summary if isinstance(summary, str) else json.dumps(summary)
                ),
            },
        )


def _attestation_run_count(engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM t_harness_runs "
                    "WHERE stage = 'legacy-native-admission-attestation'"
                )
            ).scalar_one()
        )


def _business_identity() -> dict[str, object]:
    return {
        "scheme_id": _SCHEME_ID,
        "runtime_type": "native_adapter",
        "horizon": 6,
        "task_type": "weekly_point",
        "frequency": "weekly",
        "tenors": ["10Y"],
        "registry_scheme_ids": [f"{_SCHEME_ID}__h6__10Y"],
    }


def _identity_evidence(identity: dict[str, object]) -> dict[str, object]:
    return {
        "key": "native_business_identity",
        "value": identity,
        "detail": None,
    }


def _issue_valid_token() -> str:
    from harness.legacy_native_admission_attestation import (
        LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
    )

    return issue_token(
        _SCHEME_ID,
        LEGACY_NATIVE_ADMISSION_ATTEST_ACTION,
        scheme_version=_PRIOR_VERSION,
        harness_run_id=_PRIOR_RUN_ID,
        issued_by="native-release-owner",
        ttl_seconds=300,
    )


def _expired_token(token: str) -> str:
    from harness.authorization import _sign

    padding = "=" * (-len(token) % 4)
    envelope = json.loads(
        base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
    )
    now = datetime.now(timezone.utc)
    envelope["payload"]["issued_at"] = (now - timedelta(seconds=600)).isoformat()
    envelope["payload"]["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    envelope["sig"] = _sign(envelope["payload"])
    raw = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _set_prior_compare_failed(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE t_harness_gate_results
                SET status = 'failed'
                WHERE harness_run_id = :run_id
                  AND gate_name = 'compare'
                """
            ),
            {"run_id": _PRIOR_RUN_ID},
        )


def _insert_duplicate_prior_compare(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_harness_gate_results
                    (harness_run_id, gate_name, status, summary_json)
                VALUES (:run_id, 'compare', 'passed', NULL)
                """
            ),
            {"run_id": _PRIOR_RUN_ID},
        )


def _insert_receipt(
    engine,
    *,
    business_identity: dict[str, object] | None = None,
    duplicate: bool = False,
    triggered_by: str = "native-legacy-admission-attestation-operator",
) -> None:
    identity = _business_identity() if business_identity is None else business_identity
    payload = {
        "schema_version": "legacy_native_admission_attestation_v1",
        "scope_scheme_id": _SCHEME_ID,
        "assertion": "operator_attests_legacy_native_admission_identity",
        "prior_admitted_scheme_version": _PRIOR_VERSION,
        "prior_harness_run_id": _PRIOR_RUN_ID,
        "business_identity": identity,
        "issued_by": "native-release-owner",
        "issued_at": "2026-08-04T12:00:00+00:00",
        "authorization_token_sha256": "a" * 64,
    }
    summary = json.dumps(
        {
            "passed": True,
            "evidence": [
                {
                    "key": "legacy_native_admission_attestation",
                    "value": payload,
                    "detail": None,
                }
            ],
            "errors": [],
        }
    )
    with engine.begin() as conn:
        rows = [("lna_hr-prior", triggered_by)]
        if duplicate:
            rows.append(("lna_duplicate", triggered_by))
        for run_id, row_triggered_by in rows:
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_runs
                        (harness_run_id, scheme_id, scheme_version, stage, status,
                         started_at, finished_at, triggered_by, code_hash, config_hash)
                    VALUES
                        (:run_id, :scheme_id, NULL,
                         'legacy-native-admission-attestation', 'passed',
                         '2026-08-04 12:00:00', '2026-08-04 12:00:00',
                         :triggered_by, NULL, NULL)
                    """
                ),
                {
                    "run_id": run_id,
                    "scheme_id": _SCHEME_ID,
                    "triggered_by": row_triggered_by,
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_gate_results
                        (harness_run_id, gate_name, status, summary_json)
                    VALUES
                        (:run_id, 'legacy-native-admission-attestation', 'passed',
                         :summary_json)
                    """
                ),
                {"run_id": run_id, "summary_json": summary},
            )


def _seed_registry(engine, *, task_type: str = "weekly_point") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_registry
                    (scheme_id, base_scheme_id, name, description, horizon, task_type,
                     runtime_type, tenors, frequency, target_tenor, schedule_cron,
                     schedule_timezone, status, deployed_at)
                VALUES
                    (:scheme_id, :base_scheme_id, 'Weekly 10Y', 'Weekly 10Y', 6,
                     :task_type, 'native_adapter', '["10Y"]', 'weekly', '10Y',
                     '0 9 * * 6', 'Asia/Shanghai', 'active', '2026-08-04 12:00:00')
                """
            ),
            {
                "scheme_id": f"{_SCHEME_ID}__h6__10Y",
                "base_scheme_id": _SCHEME_ID,
                "task_type": task_type,
            },
        )


def _set_current_candidate_active(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE t_scheme_versions
                SET status = 'active'
                WHERE scheme_id = :scheme_id
                  AND scheme_version = :scheme_version
                """
            ),
            {"scheme_id": _SCHEME_ID, "scheme_version": _CURRENT_VERSION},
        )


def _remove_current_candidate(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM t_scheme_versions
                WHERE scheme_id = :scheme_id
                  AND scheme_version = :scheme_version
                """
            ),
            {"scheme_id": _SCHEME_ID, "scheme_version": _CURRENT_VERSION},
        )


def _set_current_candidate(engine, *, runtime_type: str, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE t_scheme_versions
                SET runtime_type = :runtime_type,
                    status = :status
                WHERE scheme_id = :scheme_id
                  AND scheme_version = :scheme_version
                """
            ),
            {
                "scheme_id": _SCHEME_ID,
                "scheme_version": _CURRENT_VERSION,
                "runtime_type": runtime_type,
                "status": status,
            },
        )


if __name__ == "__main__":
    unittest.main()
