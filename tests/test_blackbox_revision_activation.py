from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.authorization import issue_token
from harness.context import GateContext


class _Begin:
    def __enter__(self):
        return object()

    def __exit__(self, exc_type, exc, traceback):
        return None


class _Engine:
    def begin(self):
        return _Begin()


class _DisposableEngine(_Engine):
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id="demo_blackbox",
        name="Demo Blackbox",
        description="demo",
        scheme_version="candidate-version",
        runtime_type="blackbox_v2",
        status="active",
        version_status="active",
        runtime_profile="blackbox-v2-v1",
        horizon=1,
        task_type="T+1",
        tenors=["1Y"],
        frequency="daily",
        path=root / "schemes" / "demo_blackbox",
    )


class BlackboxRevisionActivationTests(unittest.TestCase):
    def test_repository_revision_switch_commits_or_rolls_back_as_one_transaction(self) -> None:
        """真实事务必须留下唯一 active，后置失败则完整回滚。"""
        from sqlalchemy import create_engine, text

        from scheduler import repository as scheduler_repository
        from scheduler.repository import activate_blackbox_revision

        pending_versions = (
            "draft-version",
            "paused-version",
            "shadow-version",
            "validated-version",
        )
        initial_states = [
            ("draft-version", "draft"),
            ("paused-version", "paused"),
            ("prior-version", "active"),
            ("shadow-version", "shadow"),
            ("validated-version", "validated"),
        ]
        activated_states = [
            ("candidate-version", "active"),
            ("draft-version", "retired"),
            ("paused-version", "retired"),
            ("prior-version", "retired"),
            ("shadow-version", "retired"),
            ("validated-version", "retired"),
        ]

        def build_engine_and_config():
            engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                future=True,
                connect_args={
                    "detect_types": sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                },
            )
            cfg = _config(Path("/tmp"))
            cfg.algorithm_version = "1.2.3"
            cfg.contract_version = "1.0"
            cfg.environment_fingerprint = "e" * 64
            cfg.data_snapshot_id = "snapshot-candidate"
            cfg.code_hash = "c" * 64
            cfg.config_hash = "f" * 64
            cfg.manifest_hash = "m" * 64
            approved_at = datetime(2026, 8, 4, 9, 30, 0)
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    """
                    CREATE TABLE t_harness_runs (
                        harness_run_id TEXT PRIMARY KEY,
                        scheme_id TEXT,
                        scheme_version TEXT,
                        stage TEXT,
                        status TEXT,
                        finished_at timestamp
                    )
                    """
                )
                conn.exec_driver_sql(
                    """
                    CREATE TABLE t_scheme_versions (
                        scheme_id TEXT NOT NULL,
                        scheme_version TEXT NOT NULL,
                        runtime_type TEXT,
                        algorithm_version TEXT,
                        contract_version TEXT,
                        runtime_profile TEXT,
                        environment_fingerprint TEXT,
                        data_snapshot_id TEXT,
                        code_hash TEXT,
                        config_hash TEXT,
                        manifest_hash TEXT,
                        git_commit TEXT,
                        status TEXT,
                        created_by TEXT,
                        approved_by TEXT,
                        approved_at timestamp,
                        PRIMARY KEY (scheme_id, scheme_version)
                    )
                    """
                )
                conn.exec_driver_sql(
                    """
                    CREATE TABLE t_scheme_registry (
                        scheme_id TEXT PRIMARY KEY,
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
                        deployed_at timestamp
                    )
                    """
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO t_harness_runs
                            (harness_run_id, scheme_id, scheme_version, stage, status, finished_at)
                        VALUES
                            ('hr_candidate', :scheme_id, :scheme_version, 'all', 'passed', :finished_at)
                        """
                    ),
                    {
                        "scheme_id": cfg.scheme_id,
                        "scheme_version": cfg.scheme_version,
                        "finished_at": approved_at,
                    },
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO t_scheme_versions
                            (scheme_id, scheme_version, runtime_type, algorithm_version,
                             contract_version, runtime_profile, environment_fingerprint,
                             data_snapshot_id, code_hash, config_hash, manifest_hash,
                             git_commit, status, created_by, approved_by, approved_at)
                        VALUES
                            (:scheme_id, 'prior-version', 'blackbox_v2', '1.0.0',
                             '1.0', :runtime_profile, 'prior-environment',
                             'prior-snapshot', 'p', 'q', 'r', NULL, 'active',
                             'scheduler.discovery', 'prior-operator', :approved_at)
                        """
                    ),
                    {
                        "scheme_id": cfg.scheme_id,
                        "runtime_profile": cfg.runtime_profile,
                        "approved_at": approved_at,
                    },
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO t_scheme_versions
                            (scheme_id, scheme_version, runtime_type, status, created_by)
                        VALUES
                            (:scheme_id, 'draft-version', 'blackbox_v2',
                             'draft', 'scheduler.discovery'),
                            (:scheme_id, 'validated-version', 'blackbox_v2',
                             'validated', 'scheduler.discovery'),
                            (:scheme_id, 'shadow-version', 'blackbox_v2',
                             'shadow', 'scheduler.discovery'),
                            (:scheme_id, 'paused-version', 'blackbox_v2',
                             'paused', 'scheduler.discovery')
                        """
                    ),
                    {"scheme_id": cfg.scheme_id},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO t_scheme_registry
                            (scheme_id, base_scheme_id, name, description, horizon,
                             task_type, runtime_type, tenors, frequency, target_tenor,
                             schedule_cron, schedule_timezone, status, deployed_at)
                        VALUES
                            ('demo_blackbox__h1__1Y', :scheme_id, :name, :description,
                             1, 'T+1', 'blackbox_v2', '["1Y"]', 'daily', '1Y',
                             '3 7 * * 1-5', 'Asia/Shanghai', 'active', :approved_at)
                        """
                    ),
                    {
                        "scheme_id": cfg.scheme_id,
                        "name": cfg.name,
                        "description": cfg.description,
                        "approved_at": approved_at,
                    },
                )
            return engine, cfg

        def sqlite_candidate_upsert(conn, cfg, *, trusted_status, approved_by, approved_at):
            conn.execute(
                text(
                    """
                    INSERT INTO t_scheme_versions
                        (scheme_id, scheme_version, runtime_type, algorithm_version,
                         contract_version, runtime_profile, environment_fingerprint,
                         data_snapshot_id, code_hash, config_hash, manifest_hash,
                         git_commit, status, created_by, approved_by, approved_at)
                    VALUES
                        (:scheme_id, :scheme_version, :runtime_type, :algorithm_version,
                         :contract_version, :runtime_profile, :environment_fingerprint,
                         :data_snapshot_id, :code_hash, :config_hash, :manifest_hash,
                         NULL, :status, 'scheduler.discovery', :approved_by, :approved_at)
                    """
                ),
                {
                    "scheme_id": cfg.scheme_id,
                    "scheme_version": cfg.scheme_version,
                    "runtime_type": cfg.runtime_type,
                    "algorithm_version": cfg.algorithm_version,
                    "contract_version": cfg.contract_version,
                    "runtime_profile": cfg.runtime_profile,
                    "environment_fingerprint": cfg.environment_fingerprint,
                    "data_snapshot_id": cfg.data_snapshot_id,
                    "code_hash": cfg.code_hash,
                    "config_hash": cfg.config_hash,
                    "manifest_hash": cfg.manifest_hash,
                    "status": trusted_status,
                    "approved_by": approved_by,
                    "approved_at": approved_at,
                },
            )
            return cfg.scheme_version

        def read_version_states(engine):
            with engine.begin() as conn:
                rows = conn.execute(
                    text(
                        "SELECT scheme_version, status FROM t_scheme_versions "
                        "ORDER BY scheme_version"
                    )
                ).mappings().all()
            return [(row["scheme_version"], row["status"]) for row in rows]

        def activate(engine, cfg, *, pending=pending_versions):
            return activate_blackbox_revision(
                engine,
                cfg,
                prior_scheme_version="prior-version",
                pending_scheme_versions=pending,
                expected_harness_run_id="hr_candidate",
                approved_by="revision-test-operator",
                approved_at=datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc),
            )

        mismatch_engine, mismatch_cfg = build_engine_and_config()
        with (
            patch(
                "scheduler.repository._blackbox_draft_register_advisory_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scheduler.repository._upsert_scheme_version_conn",
                side_effect=sqlite_candidate_upsert,
            ) as candidate_upsert,
            self.assertRaisesRegex(RuntimeError, "pending versions changed before commit"),
        ):
            activate(mismatch_engine, mismatch_cfg, pending=pending_versions[:-1])
        candidate_upsert.assert_not_called()
        self.assertEqual(read_version_states(mismatch_engine), initial_states)
        mismatch_engine.dispose()

        engine, cfg = build_engine_and_config()
        with (
            patch(
                "scheduler.repository._blackbox_draft_register_advisory_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scheduler.repository._upsert_scheme_version_conn",
                side_effect=sqlite_candidate_upsert,
            ),
        ):
            state = activate(engine, cfg)
        self.assertEqual(state.scheme_version, cfg.scheme_version)
        self.assertEqual(read_version_states(engine), activated_states)
        engine.dispose()

        rollback_engine, rollback_cfg = build_engine_and_config()
        original_read_versions = (
            scheduler_repository._read_scheme_version_rows_for_base_conn
        )
        read_calls = 0

        def fail_on_final_readback(conn, scheme_id, *, for_update):
            nonlocal read_calls
            rows = original_read_versions(
                conn,
                scheme_id,
                for_update=for_update,
            )
            read_calls += 1
            if read_calls == 2:
                self.assertEqual(
                    [(row["scheme_version"], row["status"]) for row in rows],
                    activated_states,
                )
                raise RuntimeError("injected final version readback failure")
            return rows

        with (
            patch(
                "scheduler.repository._blackbox_draft_register_advisory_lock",
                return_value=nullcontext(),
            ),
            patch(
                "scheduler.repository._upsert_scheme_version_conn",
                side_effect=sqlite_candidate_upsert,
            ),
            patch(
                "scheduler.repository._read_scheme_version_rows_for_base_conn",
                side_effect=fail_on_final_readback,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected final version readback failure",
            ),
        ):
            activate(rollback_engine, rollback_cfg)
        self.assertEqual(read_calls, 2)
        self.assertEqual(read_version_states(rollback_engine), initial_states)
        rollback_engine.dispose()

    def test_active_recertified_revision_atomically_replaces_the_single_active_version(self) -> None:
        """已上线同身份修订只能通过新版本全量 Gate 后原子切换。"""
        from harness.blackbox_v2.gates import PassedAllRun
        from harness.blackbox_v2.revision_activation import BlackboxRevisionActivateGate
        from scheduler.repository import (
            BlackboxLifecycleState,
            BlackboxRevisionActivationPreflight,
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"HARNESS_AUTH_SECRET": "revision-test-secret"},
        ):
            root = Path(tmpdir)
            cfg = _config(root)
            passed = PassedAllRun(
                harness_run_id="hr_candidate",
                report_uri=root / "reports" / "all",
                data_snapshot_id="snapshot-candidate",
                generation_id="generation-candidate",
                runtime_profile="blackbox-v2-v1",
                environment_fingerprint="e" * 64,
            )
            token = issue_token(
                cfg.scheme_id,
                "blackbox_revision_activate",
                "2026-08-04",
                scheme_version=cfg.scheme_version,
                harness_run_id=passed.harness_run_id,
                ttl_seconds=300,
                issued_by="revision-test-operator",
            )
            ctx = GateContext(
                scheme_id=cfg.scheme_id,
                predict_date="2026-08-04",
                project_root=root,
                report_dir=root / "reports" / "revision-activate",
                config=cfg,
                authorization=token,
                engine_factory=_Engine,
            )
            preflight = BlackboxRevisionActivationPreflight(
                prior_scheme_version="prior-version",
                pending_scheme_versions=("shadow-version", "validated-version"),
                registry_scheme_ids=("demo_blackbox__h1__10Y",),
            )
            activated = BlackboxLifecycleState(
                scheme_id=cfg.scheme_id,
                scheme_version=cfg.scheme_version,
                runtime_type="blackbox_v2",
                version_status="active",
                registry_status="active",
                environment_fingerprint=passed.environment_fingerprint,
                data_snapshot_id=passed.data_snapshot_id,
                code_hash="c" * 64,
                config_hash="f" * 64,
                manifest_hash="m" * 64,
                approved_by="revision-test-operator",
                approved_at=None,
            )
            with (
                patch(
                    "harness.blackbox_v2.revision_activation._verify_passed_all",
                    return_value=passed,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._reload_pinned_canonical",
                    return_value=cfg,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._passed_run_predict_date",
                    return_value="2026-08-04",
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._environment_fingerprint",
                    return_value="e" * 64,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation.read_blackbox_revision_activation_preflight",
                    return_value=preflight,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation.activate_blackbox_revision",
                    return_value=activated,
                ) as activate,
                patch(
                    "harness.blackbox_v2.revision_activation.mark_token_used"
                ),
                patch(
                    "harness.blackbox_v2.revision_activation.write_authorization_audit",
                    return_value=root / "authorization.json",
                ),
            ):
                result = BlackboxRevisionActivateGate().run(ctx)

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(result.gate_name, "revision-activate")
        self.assertEqual(
            activate.call_args.kwargs["prior_scheme_version"],
            "prior-version",
        )
        self.assertEqual(
            activate.call_args.kwargs["pending_scheme_versions"],
            ("shadow-version", "validated-version"),
        )
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["prior_scheme_version"], "prior-version")
        self.assertEqual(evidence["retired_scheme_version"], "prior-version")
        self.assertEqual(
            evidence["retired_pending_versions"],
            ["shadow-version", "validated-version"],
        )
        self.assertEqual(evidence["registry_status"], "active")

    def test_revision_activation_refuses_a_candidate_without_a_clean_prior_identity(self) -> None:
        """候选已存在或 prior 不唯一时，不得调用原子切换。"""
        from harness.blackbox_v2.gates import PassedAllRun
        from harness.blackbox_v2.revision_activation import BlackboxRevisionActivateGate

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"HARNESS_AUTH_SECRET": "revision-test-secret"},
        ):
            root = Path(tmpdir)
            cfg = _config(root)
            passed = PassedAllRun(
                harness_run_id="hr_candidate",
                report_uri=root / "reports" / "all",
                data_snapshot_id="snapshot-candidate",
                generation_id="generation-candidate",
                runtime_profile="blackbox-v2-v1",
                environment_fingerprint="e" * 64,
            )
            token = issue_token(
                cfg.scheme_id,
                "blackbox_revision_activate",
                "2026-08-04",
                scheme_version=cfg.scheme_version,
                harness_run_id=passed.harness_run_id,
                ttl_seconds=300,
                issued_by="revision-test-operator",
            )
            ctx = GateContext(
                scheme_id=cfg.scheme_id,
                predict_date="2026-08-04",
                project_root=root,
                report_dir=root / "reports" / "revision-activate",
                config=cfg,
                authorization=token,
                engine_factory=_Engine,
            )
            with (
                patch(
                    "harness.blackbox_v2.revision_activation._verify_passed_all",
                    return_value=passed,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._reload_pinned_canonical",
                    return_value=cfg,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._passed_run_predict_date",
                    return_value="2026-08-04",
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._environment_fingerprint",
                    return_value="e" * 64,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation.read_blackbox_revision_activation_preflight",
                    side_effect=ValueError("exact candidate version already exists"),
                ),
                patch(
                    "harness.blackbox_v2.revision_activation.activate_blackbox_revision"
                ) as activate,
            ):
                result = BlackboxRevisionActivateGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("exact candidate version already exists", "\n".join(result.errors))
        activate.assert_not_called()

    def test_revision_activation_repins_under_lifecycle_lock_before_token_use(self) -> None:
        """token 消费前的配置漂移必须阻断，且不得写审计或切换版本。"""
        from harness.blackbox_v2.gates import PassedAllRun
        from harness.blackbox_v2.revision_activation import BlackboxRevisionActivateGate
        from scheduler.repository import (
            BlackboxLifecycleState,
            BlackboxRevisionActivationPreflight,
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"HARNESS_AUTH_SECRET": "revision-test-secret"},
        ):
            root = Path(tmpdir)
            cfg = _config(root)
            passed = PassedAllRun(
                harness_run_id="hr_candidate",
                report_uri=root / "reports" / "all",
                data_snapshot_id="snapshot-candidate",
                generation_id="generation-candidate",
                runtime_profile="blackbox-v2-v1",
                environment_fingerprint="e" * 64,
            )
            token = issue_token(
                cfg.scheme_id,
                "blackbox_revision_activate",
                "2026-08-04",
                scheme_version=cfg.scheme_version,
                harness_run_id=passed.harness_run_id,
                ttl_seconds=300,
                issued_by="revision-test-operator",
            )
            ctx = GateContext(
                scheme_id=cfg.scheme_id,
                predict_date="2026-08-04",
                project_root=root,
                report_dir=root / "reports" / "revision-activate",
                config=cfg,
                authorization=token,
                engine_factory=_Engine,
            )
            preflight = BlackboxRevisionActivationPreflight(
                prior_scheme_version="prior-version",
                pending_scheme_versions=(),
                registry_scheme_ids=("demo_blackbox__h1__10Y",),
            )
            activated = BlackboxLifecycleState(
                scheme_id=cfg.scheme_id,
                scheme_version=cfg.scheme_version,
                runtime_type="blackbox_v2",
                version_status="active",
                registry_status="active",
                environment_fingerprint=passed.environment_fingerprint,
                data_snapshot_id=passed.data_snapshot_id,
                code_hash="c" * 64,
                config_hash="f" * 64,
                manifest_hash="m" * 64,
                approved_by="revision-test-operator",
                approved_at=None,
            )
            with (
                patch(
                    "harness.blackbox_v2.revision_activation._verify_passed_all",
                    return_value=passed,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._reload_pinned_canonical",
                    side_effect=(
                        cfg,
                        ValueError("canonical drift immediately before token use"),
                    ),
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._passed_run_predict_date",
                    return_value="2026-08-04",
                ),
                patch(
                    "harness.blackbox_v2.revision_activation._environment_fingerprint",
                    return_value="e" * 64,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation.read_blackbox_revision_activation_preflight",
                    return_value=preflight,
                ),
                patch(
                    "harness.blackbox_v2.revision_activation.lifecycle_operation_lock",
                    return_value=nullcontext(),
                ) as lifecycle_lock,
                patch(
                    "harness.blackbox_v2.revision_activation.activate_blackbox_revision",
                    return_value=activated,
                ) as activate,
                patch(
                    "harness.blackbox_v2.revision_activation.mark_token_used"
                ) as mark_used,
                patch(
                    "harness.blackbox_v2.revision_activation.write_authorization_audit"
                ) as write_audit,
            ):
                result = BlackboxRevisionActivateGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertIn("canonical drift immediately before token use", "\n".join(result.errors))
        lifecycle_lock.assert_called_once_with(root, cfg.scheme_id)
        activate.assert_not_called()
        mark_used.assert_not_called()
        write_audit.assert_not_called()

    def test_registry_sync_skips_unapproved_blackbox_revision_candidate(self) -> None:
        """后台同步不能把 active 旧 Registry 自动降为 paused。"""
        from scheduler.repository import sync_scheme_registry

        cfg = _config(Path("/tmp"))
        with (
            patch(
                "scheduler.repository._blackbox_identity_requires_controlled_revision_conn",
                return_value=True,
            ) as needs_controlled_revision,
            patch(
                "scheduler.repository._upsert_discovered_scheme_version_conn"
            ) as upsert,
            patch("scheduler.repository._sync_scheme_registry_conn") as sync,
        ):
            sync_scheme_registry(_Engine(), [cfg])

        needs_controlled_revision.assert_called_once()
        upsert.assert_not_called()
        sync.assert_not_called()

    def test_registry_sync_skips_any_existing_identity_even_if_candidate_is_paused(self) -> None:
        """同身份冲突不能依赖 config 状态落入普通 discovery。"""
        from scheduler.repository import sync_scheme_registry

        cfg = _config(Path("/tmp"))
        cfg.status = "paused"
        cfg.version_status = "draft"
        with (
            patch(
                "scheduler.repository._blackbox_identity_requires_controlled_revision_conn",
                return_value=True,
            ) as needs_controlled_revision,
            patch(
                "scheduler.repository._upsert_discovered_scheme_version_conn"
            ) as upsert,
            patch("scheduler.repository._sync_scheme_registry_conn") as sync,
        ):
            sync_scheme_registry(_Engine(), [cfg])

        needs_controlled_revision.assert_called_once()
        upsert.assert_not_called()
        sync.assert_not_called()

    def test_revision_registry_identity_rejects_cadence_drift(self) -> None:
        """同 base/key 但 cadence 不同，不能当作同一 revision。"""
        from scheduler.repository import _blackbox_revision_registry_identity_error

        cfg = _config(Path("/tmp"))
        error = _blackbox_revision_registry_identity_error(
            cfg,
            ("1Y",),
            ("demo_blackbox__h1__1Y",),
            [
                {
                    "scheme_id": "demo_blackbox__h1__1Y",
                    "base_scheme_id": "demo_blackbox",
                    "runtime_type": "blackbox_v2",
                    "status": "active",
                    "task_type": "T+1",
                    "target_tenor": "1Y",
                    "horizon": 1,
                    "frequency": "weekly",
                    "tenors": ["1Y"],
                }
            ],
            expected_status="active",
        )

        self.assertIn("frequency", error or "")

    def test_preflight_failure_still_disposes_the_engine(self) -> None:
        """在 token 消费前返回的阻断也必须释放连接池。"""
        from harness.blackbox_v2.revision_activation import BlackboxRevisionActivateGate

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {"HARNESS_AUTH_SECRET": "revision-test-secret"},
        ):
            root = Path(tmpdir)
            cfg = _config(root)
            token = issue_token(
                cfg.scheme_id,
                "blackbox_revision_activate",
                "2026-08-04",
                scheme_version=cfg.scheme_version,
                harness_run_id="hr_candidate",
                ttl_seconds=300,
                issued_by="revision-test-operator",
            )
            engine = _DisposableEngine()
            ctx = GateContext(
                scheme_id=cfg.scheme_id,
                predict_date="2026-08-04",
                project_root=root,
                report_dir=root / "reports" / "revision-activate",
                config=cfg,
                authorization=token,
                engine_factory=lambda: engine,
            )
            with patch(
                "harness.blackbox_v2.revision_activation._reload_pinned_canonical",
                side_effect=ValueError("canonical drift"),
            ):
                result = BlackboxRevisionActivateGate().run(ctx)

        self.assertFalse(result.passed)
        self.assertTrue(engine.disposed)


if __name__ == "__main__":
    unittest.main()
