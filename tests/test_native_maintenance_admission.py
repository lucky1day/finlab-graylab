from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, text

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


    def test_blocks_registry_identity_drift_without_repairing_it(self) -> None:
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmissionGate,
        )

        cases = (
            ("identity", "UPDATE t_scheme_registry SET base_scheme_id = 'other_native'"),
            ("lifecycle", "UPDATE t_scheme_registry SET status = 'archived'"),
            ("missing", "DELETE FROM t_scheme_registry"),
        )
        for case, mutation in cases:
            with self.subTest(case=case):
                engine = _sqlite_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root, (_SCHEME_ID,))
                        _insert_registry_row(engine)
                        _seed_prior_admission(engine)
                        with engine.begin() as conn:
                            conn.execute(text(mutation))

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
                    self.assertEqual(count, 0 if case == "missing" else 1)
                finally:
                    engine.dispose()


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
) -> str:
    evidence = [
        {
            "key": "native_business_identity",
            "value": snapshot,
            "detail": None,
        }
    ]
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


def _insert_registry_row(engine, **overrides: object) -> None:
    with engine.begin() as conn:
        _insert_registry_row_conn(conn, _registry_row(**overrides))


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
