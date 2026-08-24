from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from harness.operation import build_direct_operation
from harness.context import GateContext
from harness.result import GateStatus
from scheduler.discovery import load_scheme_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def issue_token(
    scheme_id: str,
    action: str,
    predict_date: str | None = None,
    *,
    scheme_version: str,
    issued_by: str = "test-operator",
    **_ignored,
):
    return build_direct_operation(
        scheme_id,
        action,
        predict_date,
        scheme_version=scheme_version,
        issued_by=issued_by,
    )


class ActivationGateHistoryTests(unittest.TestCase):
    def test_native_gate_history_uses_harness_id_tiebreaker_for_same_finished_second(self) -> None:
        from harness.gates.activate_gate import REQUIRED_ACTIVATE_GATES, _verify_gate_history

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE t_harness_runs (harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, stage TEXT, status TEXT, finished_at TEXT)"))
            conn.execute(text("CREATE TABLE t_harness_gate_results (harness_run_id TEXT, gate_name TEXT, status TEXT)"))
            conn.execute(
                text("INSERT INTO t_harness_runs VALUES ('hr_a', 'demo_daily', 'v1', 'all', 'passed', '2026-07-06 12:00:00'), ('hr_z', 'demo_daily', 'v1', 'all', 'passed', '2026-07-06 12:00:00')")
            )
            conn.execute(
                text("INSERT INTO t_harness_gate_results VALUES (:run_id, :gate_name, :status)"),
                [
                    {
                        "run_id": harness_run_id,
                        "gate_name": gate_name,
                        "status": "failed" if harness_run_id == "hr_a" else "passed",
                    }
                    for harness_run_id in ("hr_a", "hr_z")
                    for gate_name in REQUIRED_ACTIVATE_GATES
                ],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = project_root / "schemes" / "demo_daily"
            scheme_dir.mkdir(parents=True)
            (scheme_dir / "config.yaml").write_text(
                "scheme_id: demo_daily\nbacktest:\n  benchmark_required: false\n",
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-07-06",
                project_root=project_root,
                report_dir=project_root / "reports",
            )
            try:
                with patch("harness.gates.activate_gate._db_engine", return_value=engine):
                    errors = _verify_gate_history(ctx, "v1")
            finally:
                engine.dispose()

        self.assertEqual(errors, [])

    def test_benchmark_required_compare_skipped_blocks_activation(self) -> None:
        from harness.gates.activate_gate import REQUIRED_ACTIVATE_GATES, _verify_gate_history

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_harness_runs (
                        harness_run_id TEXT,
                        scheme_id TEXT,
                        scheme_version TEXT,
                        stage TEXT,
                        status TEXT,
                        finished_at TEXT
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
                        status TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_runs
                        (harness_run_id, scheme_id, scheme_version, stage, status, finished_at)
                    VALUES ('hr_demo', 'demo_daily', 'v1', 'all', 'passed', '2026-07-06 12:00:00')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO t_harness_gate_results (harness_run_id, gate_name, status)
                    VALUES (:run_id, :gate_name, :status)
                    """
                ),
                [
                    {
                        "run_id": "hr_demo",
                        "gate_name": gate_name,
                        "status": "skipped" if gate_name == "compare" else "passed",
                    }
                    for gate_name in REQUIRED_ACTIVATE_GATES
                ],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            scheme_dir = project_root / "schemes" / "demo_daily"
            scheme_dir.mkdir(parents=True)
            (scheme_dir / "config.yaml").write_text(
                "\n".join(
                    [
                        "scheme_id: demo_daily",
                        "backtest:",
                        "  benchmark_required: true",
                    ]
                ),
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-07-06",
                project_root=project_root,
                report_dir=project_root / "reports",
            )

            try:
                with patch("harness.gates.activate_gate._db_engine", return_value=engine):
                    errors = _verify_gate_history(ctx, "v1")
            finally:
                engine.dispose()

        self.assertTrue(any("CompareGate status is skipped" in error for error in errors), errors)


class NativeActivationValidationTests(unittest.TestCase):
    def test_current_full_all_history_returns_initial_admission_profile(self) -> None:
        from harness.gates.activate_gate import (
            REQUIRED_ACTIVATE_GATES,
            _resolve_native_activation_validation,
        )

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE t_harness_runs ("
                    "harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, "
                    "stage TEXT, status TEXT, finished_at TEXT)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE t_harness_gate_results ("
                    "harness_run_id TEXT, gate_name TEXT, status TEXT)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO t_harness_runs VALUES "
                    "('hr-full', 'demo_daily', 'v-current', 'all', 'passed', "
                    "'2026-08-04 12:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO t_harness_gate_results VALUES "
                    "(:run_id, :gate_name, 'passed')"
                ),
                [
                    {"run_id": "hr-full", "gate_name": gate_name}
                    for gate_name in REQUIRED_ACTIVATE_GATES
                ],
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = root / "schemes" / "demo_daily"
            scheme_dir.mkdir(parents=True)
            (scheme_dir / "config.yaml").write_text(
                "scheme_id: demo_daily\nbacktest:\n  benchmark_required: false\n",
                encoding="utf-8",
            )
            ctx = GateContext(
                scheme_id="demo_daily",
                predict_date="2026-08-04",
                project_root=root,
                report_dir=root / "reports",
            )
            with patch("harness.gates.activate_gate._db_engine", return_value=engine):
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
        self.assertEqual(
            validation.benchmark_validation,
            "passed_initial_admission",
        )

    def test_full_all_history_rejects_duplicate_or_extra_gate_rows(self) -> None:
        from harness.gates.activate_gate import (
            REQUIRED_ACTIVATE_GATES,
            _passed_full_all_validation,
        )

        for case, extra_gate in (
            ("duplicate", "static"),
            ("legacy_seventh_gate", "api-readiness"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                engine = _activation_sqlite_engine(root)
                scheme_dir = root / "schemes" / "demo_daily"
                scheme_dir.mkdir(parents=True)
                (scheme_dir / "config.yaml").write_text(
                    "scheme_id: demo_daily\nbacktest:\n  benchmark_required: false\n",
                    encoding="utf-8",
                )
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO t_harness_runs VALUES ("
                            "'hr-full', 'demo_daily', 'v-current', 'all', "
                            "'passed', '2026-08-10 12:00:00')"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO t_harness_gate_results "
                            "(harness_run_id, gate_name, status) VALUES "
                            "('hr-full', :gate_name, 'passed')"
                        ),
                        [
                            {"gate_name": gate_name}
                            for gate_name in (*REQUIRED_ACTIVATE_GATES, extra_gate)
                        ],
                    )
                ctx = GateContext(
                    scheme_id="demo_daily",
                    predict_date="2026-08-10",
                    project_root=root,
                    report_dir=root / "reports",
                    engine_factory=lambda: engine,
                )
                try:
                    validation, errors, database_available = (
                        _passed_full_all_validation(ctx, "v-current")
                    )
                finally:
                    engine.dispose()

                self.assertIsNone(validation)
                self.assertTrue(database_available)
                self.assertTrue(
                    any("exact" in error for error in errors),
                    errors,
                )

    def test_maintenance_revalidates_admission_after_five_gate_history(self) -> None:
        from harness.gates.activate_gate import (
            _resolve_native_activation_validation,
        )
        from harness.gates.native_maintenance_admission_gate import (
            NativeMaintenanceAdmission,
        )

        order: list[str] = []
        ctx = GateContext(
            scheme_id="t5_daily",
            predict_date="2026-08-04",
            project_root=Path("/tmp/native-maintenance-order"),
            report_dir=Path("/tmp/native-maintenance-order/reports"),
        )

        def full_validation(_ctx, _version):
            order.append("full")
            return None, ["no current full admission"], True

        def maintenance_validation(_ctx, _version):
            order.append("maintenance")
            return "hr-maintenance", [], True

        def admission_validation(_ctx):
            order.append("admission")
            return (
                NativeMaintenanceAdmission(
                    prior_admitted_scheme_version="prior-native-version",
                    prior_harness_run_id="hr-prior",
                    registry_scheme_ids=("t5_daily__h5__10Y",),
                    current_candidate_runtime_type="native_adapter",
                    current_candidate_status="draft",
                    registry_lifecycle="paused",
                ),
                [],
            )

        with (
            patch(
                "harness.gates.activate_gate._passed_full_all_validation",
                side_effect=full_validation,
            ),
            patch(
                "harness.gates.activate_gate._passed_native_maintenance_validation",
                side_effect=maintenance_validation,
            ),
            patch(
                "harness.gates.native_maintenance_admission_gate."
                "verify_native_maintenance_admission",
                side_effect=admission_validation,
            ),
        ):
            validation, errors = _resolve_native_activation_validation(
                ctx,
                "candidate-version",
            )

        self.assertEqual(errors, [])
        self.assertIsNotNone(validation)
        self.assertEqual(order, ["full", "maintenance", "admission"])

    def test_resolver_accepts_complete_native_maintenance_profile(self) -> None:
        from harness.gates.activate_gate import (
            _resolve_native_activation_validation,
            _verify_gate_history,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine, cfg, ctx, registry_ids = _maintenance_fixture(root)
            try:
                _seed_maintenance_run(engine, cfg.scheme_version)
                with patch(
                    "harness.gates.activate_gate._db_engine",
                    return_value=engine,
                ):
                    validation, errors = _resolve_native_activation_validation(
                        ctx,
                        cfg.scheme_version,
                    )
                    legacy_history_errors = _verify_gate_history(
                        ctx,
                        cfg.scheme_version,
                    )
            finally:
                engine.dispose()

        self.assertEqual(errors, [])
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertEqual(
            validation.validation_profile,
            "native_post_admission_revision_v1",
        )
        self.assertEqual(validation.validation_harness_run_id, "hr-maintenance")
        self.assertEqual(validation.validation_stage, "native-maintenance")
        self.assertEqual(
            validation.prior_admitted_scheme_version,
            "prior-native-version",
        )
        self.assertEqual(validation.registry_scheme_ids, registry_ids)
        self.assertEqual(
            validation.benchmark_validation,
            "not_run_post_admission",
        )
        self.assertTrue(legacy_history_errors)
        self.assertTrue(
            any("no passed 'all'" in error for error in legacy_history_errors),
            legacy_history_errors,
        )

    def test_resolver_accepts_draft_candidate_with_paused_registry(self) -> None:
        """预激活 draft 候选可在 paused Registry 上完成维护验证。"""
        from harness.gates.activate_gate import _resolve_native_activation_validation

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine, cfg, ctx, _ = _maintenance_fixture(
                root,
                registry_status="paused",
                current_version_status="draft",
            )
            try:
                _seed_maintenance_run(engine, cfg.scheme_version)
                validation, errors = _resolve_native_activation_validation(
                    ctx,
                    cfg.scheme_version,
                )
            finally:
                engine.dispose()

        self.assertEqual(errors, [])
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertEqual(
            validation.validation_profile,
            "native_post_admission_revision_v1",
        )

    def test_activation_lifecycle_disposes_factory_engine(self) -> None:
        from harness.gates.activate_gate import _native_activation_lifecycle_preflight

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine, cfg, ctx, _ = _maintenance_fixture(root)
            try:
                with (
                    patch(
                        "harness.gates.activate_gate._db_engine",
                        return_value=None,
                    ) as db_engine,
                    patch.object(engine, "dispose", wraps=engine.dispose) as dispose,
                ):
                    lifecycle, errors = _native_activation_lifecycle_preflight(ctx)

                    self.assertEqual(errors, [])
                    self.assertIsNotNone(lifecycle)
                    db_engine.assert_not_called()
                    dispose.assert_called_once_with()
            finally:
                engine.dispose()

    def test_activation_lifecycle_disposes_factory_engine_on_read_error(self) -> None:
        from harness.gates.activate_gate import _native_activation_lifecycle_preflight

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine, _cfg, ctx, _ = _maintenance_fixture(root)
            try:
                with (
                    patch.object(
                        engine,
                        "begin",
                        side_effect=RuntimeError("control-plane read failed"),
                    ),
                    patch.object(engine, "dispose", wraps=engine.dispose) as dispose,
                ):
                    lifecycle, errors = _native_activation_lifecycle_preflight(ctx)

                    self.assertIsNone(lifecycle)
                    self.assertTrue(
                        any("control-plane read failed" in error for error in errors),
                        errors,
                    )
                    dispose.assert_called_once_with()
            finally:
                engine.dispose()

    def test_resolver_rejects_missing_maintenance_gate(self) -> None:
        from harness.gates.activate_gate import _resolve_native_activation_validation

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine, cfg, ctx, _ = _maintenance_fixture(root)
            try:
                _seed_maintenance_run(
                    engine,
                    cfg.scheme_version,
                    missing_gate="dry-run",
                )
                with patch(
                    "harness.gates.activate_gate._db_engine",
                    return_value=engine,
                ):
                    validation, errors = _resolve_native_activation_validation(
                        ctx,
                        cfg.scheme_version,
                    )
            finally:
                engine.dispose()

        self.assertIsNone(validation)
        self.assertTrue(
            any("native-maintenance" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("dry-run" in error for error in errors),
            errors,
        )

    def test_resolver_rejects_duplicate_or_legacy_extra_maintenance_gate(self) -> None:
        from harness.gates.activate_gate import _resolve_native_activation_validation

        for case, extra_gate in (
            ("duplicate", "static"),
            ("legacy_sixth_gate", "api-readiness"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                engine, cfg, ctx, _ = _maintenance_fixture(root)
                try:
                    _seed_maintenance_run(
                        engine,
                        cfg.scheme_version,
                        extra_gates=(extra_gate,),
                    )
                    validation, errors = _resolve_native_activation_validation(
                        ctx,
                        cfg.scheme_version,
                    )
                finally:
                    engine.dispose()

                self.assertIsNone(validation)
                self.assertTrue(
                    any("exact" in error for error in errors),
                    errors,
                )

    def test_resolver_rejects_missing_or_invalid_current_admission(self) -> None:
        from harness.gates.activate_gate import _resolve_native_activation_validation

        cases = (
            ("missing_prior", False, "active", "prior active Native"),
            ("registry_archived", True, "archived", "registry"),
        )
        for case, include_prior, registry_status, expected_error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                engine, cfg, ctx, _ = _maintenance_fixture(
                    root,
                    include_prior=include_prior,
                    registry_status=registry_status,
                )
                try:
                    _seed_maintenance_run(engine, cfg.scheme_version)
                    with patch(
                        "harness.gates.activate_gate._db_engine",
                        return_value=engine,
                    ):
                        validation, errors = _resolve_native_activation_validation(
                            ctx,
                            cfg.scheme_version,
                        )
                finally:
                    engine.dispose()

                self.assertIsNone(validation)
                self.assertTrue(
                    any(expected_error in error for error in errors),
                    errors,
                )

    def test_activation_gate_records_maintenance_profile_evidence(self) -> None:
        from harness.gates.activate_gate import ActivationGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine, cfg, ctx, registry_ids = _maintenance_fixture(
                root,
                registry_status="paused",
                current_version_status="draft",
            )
            try:
                _seed_maintenance_run(engine, cfg.scheme_version)
                token = issue_token(
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
                    operation=token,
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
        self.assertTrue(result.passed)
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


def _maintenance_fixture(
    root: Path,
    *,
    include_prior: bool = True,
    registry_status: str = "active",
    current_version_status: str = "active",
    config_status: str = "active",
    backtest_prediction_count: int | None = 1,
):
    _write_native_policy(root)
    cfg = _write_native_scheme(root, status=config_status)
    engine = _activation_sqlite_engine(root)
    _, registry_ids = _expected_registry_identity(cfg)
    _insert_active_registry_rows(engine, cfg, registry_status=registry_status)
    _seed_current_candidate(
        engine,
        cfg,
        status=current_version_status,
    )
    if include_prior:
        _seed_prior_admission(engine, cfg)
    if backtest_prediction_count is not None:
        _seed_successful_backtest(
            engine,
            cfg.scheme_id,
            prediction_count=backtest_prediction_count,
        )
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


def _write_active_native_scheme(root: Path):
    return _write_native_scheme(root, status="active")


def _write_native_scheme(root: Path, *, status: str):
    config_path = root / "schemes" / "t5_daily" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    source = (PROJECT_ROOT / "schemes" / "t5_daily" / "config.yaml").read_text(
        encoding="utf-8"
    )
    if status not in {"active", "paused"}:
        raise ValueError(f"unsupported fixture config status: {status}")
    source = source.replace("\nstatus: active\n", f"\nstatus: {status}\n", 1)
    config_path.write_text(source, encoding="utf-8")
    return load_scheme_config(config_path)


def _activation_sqlite_engine(root: Path):
    engine = create_engine(f"sqlite:///{root / 'activation-control.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE t_harness_runs ("
                "harness_run_id TEXT, scheme_id TEXT, scheme_version TEXT, "
                "stage TEXT, status TEXT, finished_at TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE t_harness_gate_results ("
                "harness_run_id TEXT, gate_name TEXT, status TEXT, summary_json TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE t_scheme_versions ("
                "scheme_id TEXT, scheme_version TEXT, runtime_type TEXT, status TEXT, "
                "code_hash TEXT, config_hash TEXT, manifest_hash TEXT, "
                "approved_by TEXT, approved_at TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE t_scheme_registry ("
                "scheme_id TEXT, base_scheme_id TEXT, name TEXT, description TEXT, "
                "horizon INTEGER, task_type TEXT, runtime_type TEXT, tenors TEXT, "
                "frequency TEXT, target_tenor TEXT, schedule_cron TEXT, "
                "schedule_timezone TEXT, status TEXT, deployed_at TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE t_backtest_runs ("
                "id INTEGER PRIMARY KEY, benchmark_id TEXT, scheme_id TEXT, "
                "data_source TEXT, status TEXT, updated_at TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE t_backtest_predictions ("
                "id INTEGER PRIMARY KEY, run_id INTEGER)"
            )
        )
    return engine


def _expected_registry_identity(cfg):
    from scheduler.repository import _expected_registry_identity as repository_identity

    return repository_identity(cfg)


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
                    "detail": None,
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
    *,
    missing_gate: str | None = None,
    extra_gates: tuple[str, ...] = (),
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
                for gate_name in (*NATIVE_MAINTENANCE_SEQUENCE, *extra_gates)
                if gate_name != missing_gate
            ],
        )


def _seed_full_all_run(engine, scheme_version: str) -> None:
    from harness.gates.activate_gate import REQUIRED_ACTIVATE_GATES

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_harness_runs VALUES "
                "('hr-full', 't5_daily', :scheme_version, "
                "'all', 'passed', '2026-08-10 12:00:00')"
            ),
            {"scheme_version": scheme_version},
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_gate_results "
                "(harness_run_id, gate_name, status) VALUES "
                "('hr-full', :gate_name, 'passed')"
            ),
            [{"gate_name": gate_name} for gate_name in REQUIRED_ACTIVATE_GATES],
        )


def _seed_legacy_full_all_run(engine, scheme_version: str) -> None:
    from harness.gates.activate_gate import REQUIRED_ACTIVATE_GATES

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO t_harness_runs VALUES "
                "('hr-legacy-full', 't5_daily', :scheme_version, "
                "'all', 'passed', '2026-08-10 12:00:00')"
            ),
            {"scheme_version": scheme_version},
        )
        conn.execute(
            text(
                "INSERT INTO t_harness_gate_results "
                "(harness_run_id, gate_name, status) VALUES "
                "('hr-legacy-full', :gate_name, 'passed')"
            ),
            [
                {"gate_name": gate_name}
                for gate_name in (*REQUIRED_ACTIVATE_GATES, "api-readiness")
            ],
        )


def _seed_legacy_maintenance_run(engine, scheme_version: str) -> None:
    _seed_maintenance_run(
        engine,
        scheme_version,
        extra_gates=("api-readiness",),
    )


def _read_activation_lifecycle_rows(engine, scheme_version: str):
    with engine.begin() as conn:
        version = conn.execute(
            text(
                "SELECT approved_by, approved_at FROM t_scheme_versions "
                "WHERE scheme_id = 't5_daily' AND scheme_version = :scheme_version"
            ),
            {"scheme_version": scheme_version},
        ).one()
        registry = tuple(
            conn.execute(
                text(
                    "SELECT scheme_id, status FROM t_scheme_registry "
                    "WHERE base_scheme_id = 't5_daily' ORDER BY scheme_id"
                )
            ).fetchall()
        )
    return (version[0], version[1]), registry


def _read_activation_version_legacy_fields(engine, scheme_version: str):
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT code_hash, config_hash, manifest_hash, approved_by, approved_at "
                "FROM t_scheme_versions WHERE scheme_id = 't5_daily' "
                "AND scheme_version = :scheme_version"
            ),
            {"scheme_version": scheme_version},
        ).one()
    return tuple(row)


def _seed_successful_backtest(
    engine,
    scheme_id: str,
    *,
    prediction_count: int,
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
        if prediction_count:
            conn.execute(
                text(
                    "INSERT INTO t_backtest_predictions (id, run_id) "
                    "VALUES (:id, 101)"
                ),
                [{"id": index + 1} for index in range(prediction_count)],
            )


if __name__ == "__main__":
    unittest.main()
