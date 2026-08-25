from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text

from harness.context import GateContext
from harness.operation import build_direct_operation
from harness.result import GateStatus
from scheduler.discovery import load_scheme_config
from scheduler.repository import _expected_registry_identity
from tests.harness_control_plane import create_harness_control_plane_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class NativeActivationValidationTests(unittest.TestCase):
    def test_current_full_all_history_returns_initial_admission_profile(self) -> None:
        from harness.gates.activate_gate import (
            REQUIRED_ACTIVATE_GATES,
            _resolve_native_activation_validation,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine, ctx = _full_all_fixture(root)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO t_harness_runs VALUES "
                        "('hr-full', 'demo_daily', 'v-current', 'all', 'passed', "
                        "'2026-08-04 12:00:00')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO t_harness_gate_results "
                        "(harness_run_id, gate_name, status) "
                        "VALUES ('hr-full', :gate_name, 'passed')"
                    ),
                    [{"gate_name": name} for name in REQUIRED_ACTIVATE_GATES],
                )
            validation, errors = _resolve_native_activation_validation(
                ctx,
                "v-current",
            )

        self.assertEqual(errors, [])
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertEqual(validation.validation_profile, "full_initial_onboarding_v1")
        self.assertEqual(validation.validation_harness_run_id, "hr-full")
        self.assertEqual(validation.validation_stage, "all")
        self.assertIsNone(validation.prior_admitted_scheme_version)
        self.assertEqual(validation.registry_scheme_ids, ())
        self.assertEqual(validation.benchmark_validation, "passed_initial_admission")

    def test_activation_gate_records_maintenance_profile_evidence(self) -> None:
        from harness.gates.activate_gate import ActivationGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine, cfg, ctx, registry_ids = _maintenance_fixture(root)
            try:
                _seed_maintenance_run(engine, cfg.scheme_version)
                operation = build_direct_operation(
                    cfg.scheme_id,
                    "activate",
                    scheme_version=cfg.scheme_version,
                    issued_by="native-release-owner",
                )
                ctx = GateContext(
                    scheme_id=ctx.scheme_id,
                    predict_date=ctx.predict_date,
                    project_root=ctx.project_root,
                    report_dir=ctx.report_dir,
                    config=cfg,
                    engine_factory=ctx.engine_factory,
                    operation=operation,
                )
                with (
                    patch(
                        "harness.gates.activate_gate._db_engine",
                        return_value=engine,
                    ),
                    patch(
                        "harness.gates.activate_gate._sync_registry_after_activation",
                        return_value=cfg.scheme_version,
                    ) as sync,
                ):
                    result = ActivationGate().run(ctx)
            finally:
                engine.dispose()

        self.assertEqual(result.status, GateStatus.PASSED)
        sync.assert_called_once()
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(
            evidence["validation_profile"],
            "native_post_admission_revision_v1",
        )
        self.assertEqual(evidence["validation_harness_run_id"], "hr-maintenance")
        self.assertEqual(evidence["validation_stage"], "native-maintenance")
        self.assertEqual(
            evidence["prior_admitted_scheme_version"],
            "prior-native-version",
        )
        self.assertEqual(evidence["admission_registry_scheme_ids"], list(registry_ids))
        self.assertEqual(
            evidence["benchmark_validation"],
            "not_run_post_admission",
        )


def _maintenance_fixture(root: Path):
    _write_native_policy(root)
    cfg = _write_native_scheme(root)
    engine = create_harness_control_plane_engine(root / "activation-control.db")
    _, registry_ids = _expected_registry_identity(cfg)
    _insert_active_registry_rows(engine, cfg, registry_status="paused")
    _seed_current_candidate(engine, cfg, status="draft")
    _seed_prior_admission(engine, cfg)
    _seed_successful_backtest(engine, cfg.scheme_id)
    ctx = GateContext(
        scheme_id=cfg.scheme_id,
        predict_date="2026-08-04",
        project_root=root,
        report_dir=root / "reports",
        config=cfg,
        engine_factory=lambda: engine,
    )
    return engine, cfg, ctx, registry_ids


def _write_native_policy(root: Path) -> None:
    policy_path = root / "deploy" / "onboarding_policy_v1.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(
            {
                "policy_version": "1.0",
                "new_scheme_runtime_type": "blackbox_v2",
                "native_v1_mode": "maintenance_only",
                "legacy_native_scheme_ids": ["t5_daily"],
            }
        ),
        encoding="utf-8",
    )


def _write_native_scheme(root: Path):
    config_path = root / "schemes" / "t5_daily" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    source = (PROJECT_ROOT / "schemes" / "t5_daily" / "config.yaml").read_text(
        encoding="utf-8"
    )
    config_path.write_text(source, encoding="utf-8")
    return load_scheme_config(config_path)


def _full_all_fixture(
    root: Path,
) -> tuple[object, GateContext]:
    engine = create_harness_control_plane_engine(root / "activation-control.db")
    config_path = root / "schemes" / "demo_daily" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "scheme_id: demo_daily\n"
        "backtest:\n"
        "  benchmark_required: false\n",
        encoding="utf-8",
    )
    return engine, GateContext(
        scheme_id="demo_daily",
        predict_date="2026-07-06",
        project_root=root,
        report_dir=root / "reports",
        engine_factory=lambda: engine,
    )


def _insert_active_registry_rows(engine, cfg, *, registry_status: str) -> None:
    expected_tenors, registry_ids = _expected_registry_identity(cfg)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_registry ("
                "scheme_id, base_scheme_id, name, description, horizon, task_type, "
                "runtime_type, tenors, frequency, target_tenor, schedule_cron, "
                "schedule_timezone, status, deployed_at) VALUES ("
                ":scheme_id, :base_scheme_id, :name, :description, :horizon, "
                ":task_type, :runtime_type, :tenors, :frequency, :target_tenor, "
                ":schedule_cron, :schedule_timezone, :status, :deployed_at)"
            ),
            [
                {
                    "scheme_id": registry_id,
                    "base_scheme_id": cfg.scheme_id,
                    "name": cfg.name,
                    "description": cfg.description,
                    "horizon": cfg.horizon,
                    "task_type": cfg.task_type,
                    "runtime_type": "native_adapter",
                    "tenors": json.dumps([target_tenor]),
                    "frequency": cfg.frequency,
                    "target_tenor": target_tenor,
                    "schedule_cron": cfg.schedule.cron,
                    "schedule_timezone": cfg.schedule.timezone,
                    "status": registry_status,
                    "deployed_at": "2026-08-01 10:00:00",
                }
                for target_tenor, registry_id in zip(expected_tenors, registry_ids)
            ],
        )


def _seed_prior_admission(engine, cfg) -> None:
    from harness.gates.native_maintenance_admission_gate import (
        NATIVE_BUSINESS_IDENTITY_EVIDENCE_KEY,
        native_business_identity_snapshot,
    )

    identity = native_business_identity_snapshot(
        scheme_id=cfg.scheme_id,
        runtime_type=cfg.runtime_type,
        horizon=cfg.horizon,
        task_type=cfg.task_type,
        frequency=cfg.frequency,
        tenors=cfg.tenors,
    )
    summary_json = json.dumps(
        {
            "passed": True,
            "evidence": [
                {
                    "key": NATIVE_BUSINESS_IDENTITY_EVIDENCE_KEY,
                    "value": identity,
                }
            ],
            "errors": [],
        }
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, status) VALUES "
                "('t5_daily', 'prior-native-version', 'native_adapter', 'active')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_runs VALUES "
                "('hr-prior', 't5_daily', 'prior-native-version', 'all', 'passed', "
                "'2026-08-03 12:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_gate_results "
                "(harness_run_id, gate_name, status) VALUES "
                "('hr-prior', 'compare', 'passed')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_gate_results "
                "(harness_run_id, gate_name, status, summary_json) VALUES "
                "('hr-prior', 'static', 'passed', :summary_json)"
            ),
            {"summary_json": summary_json},
        )


def _seed_current_candidate(engine, cfg, *, status: str) -> None:
    approved_by = "existing-release-owner" if status == "active" else None
    approved_at = "2026-08-04 10:00:00" if status == "active" else None
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_scheme_versions "
                "(scheme_id, scheme_version, runtime_type, status, code_hash, "
                "config_hash, manifest_hash, approved_by, approved_at) VALUES "
                "(:scheme_id, :scheme_version, 'native_adapter', :status, "
                ":code_hash, :config_hash, :manifest_hash, :approved_by, :approved_at)"
            ),
            {
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "status": status,
                "code_hash": cfg.code_hash,
                "config_hash": cfg.config_hash,
                "manifest_hash": cfg.manifest_hash,
                "approved_by": approved_by,
                "approved_at": approved_at,
            },
        )


def _seed_maintenance_run(
    engine,
    scheme_version: str,
) -> None:
    from harness.gates.native_maintenance_admission_gate import (
        NATIVE_MAINTENANCE_SEQUENCE,
    )

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_harness_runs VALUES "
                "('hr-maintenance', 't5_daily', :scheme_version, "
                "'native-maintenance', 'passed', '2026-08-04 12:00:00')"
            ),
            {"scheme_version": scheme_version},
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_gate_results "
                "(harness_run_id, gate_name, status) VALUES "
                "('hr-maintenance', :gate_name, 'passed')"
            ),
            [
                {"gate_name": gate_name}
                for gate_name in NATIVE_MAINTENANCE_SEQUENCE
            ],
        )


def _seed_successful_backtest(
    engine,
    scheme_id: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_backtest_runs "
                "(id, benchmark_id, scheme_id, data_source, status, updated_at) "
                "VALUES (101, 'native-current', :scheme_id, "
                "'framework_db_aligned', 'success', '2026-08-10 10:00:00')"
            ),
            {"scheme_id": scheme_id},
        )
        conn.execute(
            text(
                "INSERT INTO t_backtest_predictions (id, run_id) "
                "VALUES (1, 101)"
            )
        )
