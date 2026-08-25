from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from harness.context import GateContext
from harness.result import GateStatus
from tests.harness_control_plane import create_harness_control_plane_engine


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

        engine = create_harness_control_plane_engine()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                _write_policy(root)
                _insert_registry_row(engine, status="paused", deployed_at=None)
                _seed_prior_admission(engine)
                _seed_current_candidate(engine)

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
                engine = create_harness_control_plane_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root)
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
                engine = create_harness_control_plane_engine()
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        root = Path(tmpdir)
                        _write_policy(root)
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


def _context(root: Path, engine) -> GateContext:
    return GateContext(
        scheme_id=_SCHEME_ID,
        predict_date="2026-08-04",
        project_root=root,
        report_dir=root / "reports",
        config=SimpleNamespace(
            scheme_id=_SCHEME_ID,
            scheme_version=_CURRENT_VERSION,
            runtime_type="native_adapter",
            status="active",
            tenors=["10Y"],
            horizon=1,
            task_type="T+1",
            frequency="daily",
        ),
        engine_factory=lambda: engine,
    )


def _write_policy(root: Path) -> None:
    policy_path = root / "deploy" / "onboarding_policy_v1.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
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


def _seed_prior_admission(
    engine,
    *,
    scheme_version: str = "prior-v1",
    harness_run_id: str = "hr-prior",
    finished_at: str = "2026-08-01 10:00:00",
    static_summary_json: str | None = None,
    include_static_result: bool = True,
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
                    (:harness_run_id, :scheme_id, :scheme_version, 'all', 'passed', :finished_at)
                """
            ),
            {
                "harness_run_id": harness_run_id,
                "scheme_id": _SCHEME_ID,
                "scheme_version": scheme_version,
                "finished_at": finished_at,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO t_harness_gate_results
                    (harness_run_id, gate_name, status)
                VALUES
                    (:harness_run_id, 'compare', 'passed')
                """
            ),
            {"harness_run_id": harness_run_id},
        )
        if include_static_result:
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
                    "summary_json": (
                        _summary_json(_NATIVE_BUSINESS_IDENTITY)
                        if static_summary_json is None
                        else static_summary_json
                    ),
                },
            )


def _seed_current_candidate(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO t_scheme_versions
                    (scheme_id, scheme_version, runtime_type, status)
                VALUES
                    (:scheme_id, :scheme_version, 'native_adapter', 'draft')
                """
            ),
            {
                "scheme_id": _SCHEME_ID,
                "scheme_version": _CURRENT_VERSION,
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


def _insert_registry_row(engine, **overrides: object) -> None:
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
    with engine.begin() as conn:
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
