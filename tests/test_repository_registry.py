from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch


class _MappingResult:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows or []

    def mappings(self) -> _MappingResult:
        return self

    def one_or_none(self) -> dict | None:
        if len(self._rows) > 1:
            raise AssertionError("expected at most one row")
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict]:
        return list(self._rows)


class _CaptureConnection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, rows=None) -> _MappingResult:
        sql_text = str(sql)
        self._store.setdefault("calls", []).append((sql_text, rows))
        self._store["sql"] = sql_text
        self._store["rows"] = rows
        if sql_text.lstrip().startswith("SELECT") and "FROM t_scheme_versions" in sql_text:
            version_row = self._store.get("version_row")
            return _MappingResult([version_row] if version_row is not None else [])
        if sql_text.lstrip().startswith("SELECT") and "FROM t_scheme_registry" in sql_text:
            return _MappingResult(self._store.get("registry_rows", []))
        if "INSERT INTO t_scheme_versions" in sql_text:
            current = self._store.get("version_row")
            incoming = {
                key: rows.get(key)
                for key in (
                    "scheme_id",
                    "scheme_version",
                    "runtime_type",
                    "algorithm_version",
                    "contract_version",
                    "runtime_profile",
                    "environment_fingerprint",
                    "data_snapshot_id",
                    "code_hash",
                    "config_hash",
                    "manifest_hash",
                    "git_commit",
                    "status",
                    "approved_by",
                    "approved_at",
                )
            }
            if (
                self._store.get("truncate_datetime_zero")
                and isinstance(incoming.get("approved_at"), datetime)
            ):
                incoming["approved_at"] = incoming["approved_at"].replace(microsecond=0)
            if current is None:
                self._store["version_row"] = incoming
            else:
                preserve_evidence = bool(rows.get("preserve_blackbox_evidence", False))
                preserve_lifecycle = bool(rows.get("preserve_lifecycle", False))
                for key, value in incoming.items():
                    if preserve_evidence:
                        continue
                    if preserve_lifecycle and key in {
                        "runtime_type",
                        "status",
                        "approved_by",
                        "approved_at",
                    }:
                        continue
                    current[key] = value
                if rows.get("trusted_lifecycle"):
                    current.update(incoming)
            return _MappingResult()
        if "INSERT INTO t_scheme_registry" in sql_text:
            registry_by_id = {
                str(row["scheme_id"]): dict(row)
                for row in self._store.get("registry_rows", [])
            }
            for row in rows:
                registry_by_id[str(row["scheme_id"])] = dict(row)
            self._store["registry_rows"] = list(registry_by_id.values())
            return _MappingResult()
        if "INSERT INTO t_scheme_predictions" in sql_text:
            self._store.setdefault("prediction_rows", []).extend(dict(row) for row in rows)
            return _MappingResult()
        return _MappingResult()


class _CaptureBegin:
    def __init__(self, store: dict) -> None:
        self._store = store

    def __enter__(self) -> _CaptureConnection:
        return _CaptureConnection(self._store)

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _CaptureEngine:
    def __init__(
        self,
        *,
        version_row: dict | None = None,
        registry_rows: list[dict] | None = None,
        truncate_datetime_zero: bool = False,
    ) -> None:
        self.store: dict = {
            "version_row": version_row,
            "registry_rows": registry_rows or [],
            "prediction_rows": [],
            "begin_count": 0,
            "truncate_datetime_zero": truncate_datetime_zero,
        }

    def begin(self) -> _CaptureBegin:
        self.store["begin_count"] += 1
        return _CaptureBegin(self.store)


def _blackbox_config(
    *,
    status: str = "active",
    version_status: str = "active",
    scheme_version: str = "abc123def456",
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id="demo_blackbox",
        name="Demo Blackbox",
        description="blackbox scheme",
        horizon=1,
        task_type="T+1",
        tenors=["10Y"],
        frequency="daily",
        schedule=SimpleNamespace(cron="3 7 * * 1-5", timezone="Asia/Shanghai"),
        status=status,
        scheme_version=scheme_version,
        code_hash="c" * 64,
        config_hash="f" * 64,
        manifest_hash="m" * 64,
        runtime_type="blackbox_v2",
        version_status=version_status,
        algorithm_version="1.2.3",
        contract_version="1.0",
        runtime_profile="blackbox-v2-v1",
        environment_fingerprint="e" * 64,
        data_snapshot_id="snapshot-1",
    )


def _call_for(store: dict, sql_fragment: str) -> tuple[str, object]:
    return next(call for call in store["calls"] if sql_fragment in call[0])


def _set_canonical_path(cfg: SimpleNamespace, project_root: Path) -> SimpleNamespace:
    cfg.path = project_root / "schemes" / cfg.scheme_id
    cfg.path.mkdir(parents=True)
    return cfg


class RegistrySyncTests(unittest.TestCase):
    def test_registry_scheme_id_is_composite_for_every_tenor(self) -> None:
        from scheduler.repository import registry_scheme_id

        self.assertEqual(registry_scheme_id("t1_daily", 1, "5Y"), "t1_daily__h1__5Y")
        self.assertEqual(registry_scheme_id("daily_5y_2_v28", 5, "5Y"), "daily_5y_2_v28__h5__5Y")

    def test_registry_sync_only_refreshes_updated_at_when_metadata_changes(self) -> None:
        from scheduler.repository import sync_scheme_registry

        engine = _CaptureEngine()
        scheme = SimpleNamespace(
            scheme_id="demo_weekly_scheme",
            name="Demo Weekly Scheme",
            description="weekly scheme",
            horizon=6,
            task_type="weekly_point",
            tenors=["10Y"],
            frequency="weekly",
            schedule=SimpleNamespace(cron="30 11 * * 6", timezone="Asia/Shanghai"),
            status="active",
            scheme_version="abc123def456",
            code_hash="c" * 64,
            config_hash="f" * 64,
            manifest_hash=None,
            runtime_type="blackbox_v2",
            version_status="shadow",
            algorithm_version="1.2.3",
            contract_version="1.0",
            runtime_profile="blackbox-v2-v1",
        )

        sync_scheme_registry(engine, [scheme])

        sql, rows = _call_for(engine.store, "INSERT INTO t_scheme_registry")
        update_clause = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        self.assertIn("updated_at = IF(", update_clause)
        self.assertLess(update_clause.index("updated_at = IF("), update_clause.index("name = VALUES(name)"))
        self.assertNotIn("updated_at = CURRENT_TIMESTAMP", update_clause)
        self.assertIn("deployed_at = IF(deployed_at IS NULL AND VALUES(status) = 'active'", update_clause)
        self.assertEqual(rows[0]["runtime_type"], "blackbox_v2")

    def test_registry_sync_writes_one_registry_row_per_target_tenor(self) -> None:
        from scheduler.repository import sync_scheme_registry

        engine = _CaptureEngine()
        scheme = SimpleNamespace(
            scheme_id="t5_daily",
            name="T5 Daily",
            description="multi tenor daily scheme",
            horizon=5,
            task_type="T+5",
            tenors=["3Y", "5Y", "7Y", "10Y"],
            frequency="daily",
            schedule=SimpleNamespace(cron="3 7 * * 1-5", timezone="Asia/Shanghai"),
            status="active",
            scheme_version=None,
            code_hash="c" * 64,
            config_hash="f" * 64,
            manifest_hash=None,
        )

        sync_scheme_registry(engine, [scheme])

        sql, rows = _call_for(engine.store, "INSERT INTO t_scheme_registry")
        self.assertNotIn("base_scheme_id, frequency, horizon, target_tenor", sql)
        self.assertEqual(
            [row["scheme_id"] for row in rows],
            [
                "t5_daily__h5__3Y",
                "t5_daily__h5__5Y",
                "t5_daily__h5__7Y",
                "t5_daily__h5__10Y",
            ],
        )
        self.assertEqual({row["base_scheme_id"] for row in rows}, {"t5_daily"})
        self.assertEqual([row["target_tenor"] for row in rows], ["3Y", "5Y", "7Y", "10Y"])
        self.assertEqual({row["task_type"] for row in rows}, {"T+5"})
        self.assertEqual([row["tenors"] for row in rows], ['["3Y"]', '["5Y"]', '["7Y"]', '["10Y"]'])
        self.assertEqual({row["runtime_type"] for row in rows}, {"native_adapter"})

    def test_unknown_active_blackbox_sync_creates_draft_version_and_paused_registry(self) -> None:
        from scheduler.repository import sync_scheme_registry

        engine = _CaptureEngine()

        sync_scheme_registry(engine, [_blackbox_config()])

        _, version_params = _call_for(engine.store, "INSERT INTO t_scheme_versions")
        _, registry_rows = _call_for(engine.store, "INSERT INTO t_scheme_registry")
        self.assertEqual(version_params["status"], "draft")
        self.assertEqual({row["status"] for row in registry_rows}, {"paused"})

    def test_generic_sync_does_not_downgrade_or_clear_approved_blackbox_version(self) -> None:
        from scheduler.repository import sync_scheme_registry

        engine = _CaptureEngine(
            version_row={
                "scheme_id": "demo_blackbox",
                "scheme_version": "abc123def456",
                "runtime_type": "blackbox_v2",
                "status": "active",
                "approved_by": "release-owner",
                "approved_at": datetime(2026, 7, 20, 8, 30),
            }
        )

        sync_scheme_registry(engine, [_blackbox_config(version_status="shadow")])

        version_sql, version_params = _call_for(engine.store, "INSERT INTO t_scheme_versions")
        _, registry_rows = _call_for(engine.store, "INSERT INTO t_scheme_registry")
        self.assertEqual(version_params["status"], "draft")
        self.assertTrue(version_params["preserve_lifecycle"])
        self.assertFalse(version_params["trusted_lifecycle"])
        self.assertIn("approved_by = IF(:trusted_lifecycle", version_sql)
        self.assertIn("approved_at = IF(:trusted_lifecycle", version_sql)
        self.assertEqual({row["status"] for row in registry_rows}, {"active"})

    def test_generic_sync_preserves_all_approval_bound_blackbox_evidence(self) -> None:
        from scheduler.repository import sync_scheme_registry

        approved_at = datetime(2026, 7, 20, 8, 30)
        stored = {
            "scheme_id": "demo_blackbox",
            "scheme_version": "abc123def456",
            "runtime_type": "blackbox_v2",
            "algorithm_version": "1.2.3",
            "contract_version": "1.0",
            "runtime_profile": "blackbox-v2-v1",
            "environment_fingerprint": "e" * 64,
            "data_snapshot_id": "approved-snapshot",
            "code_hash": "c" * 64,
            "config_hash": "f" * 64,
            "manifest_hash": "m" * 64,
            "git_commit": "approved-commit",
            "status": "active",
            "approved_by": "release-owner",
            "approved_at": approved_at,
        }
        engine = _CaptureEngine(version_row=dict(stored))
        cfg = _blackbox_config()
        cfg.environment_fingerprint = None
        cfg.data_snapshot_id = None

        sync_scheme_registry(engine, [cfg])

        self.assertEqual(engine.store["version_row"], stored)
        _, version_params = _call_for(engine.store, "INSERT INTO t_scheme_versions")
        self.assertTrue(version_params["preserve_blackbox_evidence"])

    def test_generic_sync_rejects_conflicting_immutable_blackbox_metadata(self) -> None:
        from scheduler.repository import sync_scheme_registry

        stored = {
            "scheme_id": "demo_blackbox",
            "scheme_version": "abc123def456",
            "runtime_type": "blackbox_v2",
            "algorithm_version": "1.2.3",
            "contract_version": "1.0",
            "runtime_profile": "blackbox-v2-v1",
            "environment_fingerprint": "e" * 64,
            "data_snapshot_id": "snapshot-1",
            "code_hash": "c" * 64,
            "config_hash": "f" * 64,
            "manifest_hash": "m" * 64,
            "git_commit": None,
            "status": "active",
            "approved_by": "release-owner",
            "approved_at": datetime(2026, 7, 20, 8, 30),
        }
        conflicts = {
            "runtime_type": "native_adapter",
            "algorithm_version": "9.9.9",
            "contract_version": "2.0",
            "runtime_profile": "other-profile",
            "environment_fingerprint": "x" * 64,
            "data_snapshot_id": "other-snapshot",
            "code_hash": "x" * 64,
            "config_hash": "y" * 64,
            "manifest_hash": "z" * 64,
        }
        for field, value in conflicts.items():
            with self.subTest(field=field):
                cfg = _blackbox_config()
                setattr(cfg, field, value)
                engine = _CaptureEngine(version_row=dict(stored))

                with self.assertRaisesRegex(
                    ValueError,
                    rf"immutable Blackbox version metadata mismatch.*{field}",
                ):
                    sync_scheme_registry(engine, [cfg])

                self.assertEqual(engine.store["version_row"], stored)

    def test_trusted_blackbox_lifecycle_atomically_writes_shadow_and_paused_registry(self) -> None:
        from scheduler.repository import apply_blackbox_lifecycle_state

        engine = _CaptureEngine()
        cfg = _blackbox_config(status="paused", version_status="shadow")

        state = apply_blackbox_lifecycle_state(
            engine,
            cfg,
            version_status="shadow",
            registry_status="paused",
        )

        self.assertEqual(engine.store["begin_count"], 1)
        self.assertEqual(engine.store["version_row"]["status"], "shadow")
        self.assertEqual(engine.store["version_row"]["environment_fingerprint"], "e" * 64)
        self.assertEqual(engine.store["version_row"]["data_snapshot_id"], "snapshot-1")
        self.assertEqual(engine.store["version_row"]["code_hash"], "c" * 64)
        self.assertEqual(engine.store["version_row"]["config_hash"], "f" * 64)
        self.assertEqual(engine.store["version_row"]["manifest_hash"], "m" * 64)
        self.assertEqual({row["status"] for row in engine.store["registry_rows"]}, {"paused"})
        self.assertEqual(state.version_status, "shadow")
        self.assertEqual(state.registry_status, "paused")
        self.assertEqual(state.environment_fingerprint, "e" * 64)
        self.assertEqual(state.data_snapshot_id, "snapshot-1")

    def test_trusted_blackbox_lifecycle_normalizes_aware_approval_to_mysql_utc(self) -> None:
        from scheduler.repository import apply_blackbox_lifecycle_state

        engine = _CaptureEngine()
        approved_at = datetime(
            2026,
            7,
            20,
            16,
            30,
            tzinfo=timezone(timedelta(hours=8)),
        )

        state = apply_blackbox_lifecycle_state(
            engine,
            _blackbox_config(),
            version_status="active",
            registry_status="active",
            approved_by="release-owner",
            approved_at=approved_at,
        )

        expected_mysql_value = datetime(2026, 7, 20, 8, 30)
        self.assertEqual(engine.store["version_row"]["approved_at"], expected_mysql_value)
        self.assertIsNone(engine.store["version_row"]["approved_at"].tzinfo)
        self.assertEqual(state.approved_at, expected_mysql_value)

    def test_trusted_blackbox_activation_accepts_mysql_datetime_zero_readback(self) -> None:
        from scheduler.repository import apply_blackbox_lifecycle_state

        engine = _CaptureEngine(truncate_datetime_zero=True)
        approved_at = datetime(
            2026,
            7,
            20,
            16,
            30,
            45,
            987654,
            tzinfo=timezone(timedelta(hours=8)),
        )

        state = apply_blackbox_lifecycle_state(
            engine,
            _blackbox_config(),
            version_status="active",
            registry_status="active",
            approved_by="release-owner",
            approved_at=approved_at,
        )

        expected_mysql_value = datetime(2026, 7, 20, 8, 30, 45)
        self.assertEqual(engine.store["version_row"]["approved_at"], expected_mysql_value)
        self.assertEqual(state.approved_at, expected_mysql_value)

    def test_native_active_sync_behavior_is_unchanged(self) -> None:
        from scheduler.repository import sync_scheme_registry

        engine = _CaptureEngine()
        cfg = SimpleNamespace(
            scheme_id="native_daily",
            name="Native Daily",
            description="native scheme",
            horizon=1,
            task_type="T+1",
            tenors=["5Y"],
            frequency="daily",
            schedule=SimpleNamespace(cron="3 7 * * 1-5", timezone="Asia/Shanghai"),
            status="active",
            scheme_version="native-version-1",
            code_hash="c" * 64,
            config_hash="f" * 64,
            manifest_hash=None,
            runtime_type="native_adapter",
            version_status="active",
        )

        sync_scheme_registry(engine, [cfg])

        _, version_params = _call_for(engine.store, "INSERT INTO t_scheme_versions")
        _, registry_rows = _call_for(engine.store, "INSERT INTO t_scheme_registry")
        self.assertEqual(version_params["status"], "active")
        self.assertEqual({row["status"] for row in registry_rows}, {"active"})


class BlackboxExecutionApprovalRepositoryTests(unittest.TestCase):
    def _approved_version_row(self, **updates) -> dict:
        row = {
            "scheme_id": "demo_blackbox",
            "scheme_version": "abc123def456",
            "runtime_type": "blackbox_v2",
            "status": "active",
            "approved_by": "release-owner",
            "approved_at": datetime(2026, 7, 20, 8, 30),
        }
        row.update(updates)
        return row

    def _active_registry_row(self, **updates) -> dict:
        row = {
            "scheme_id": "demo_blackbox__h1__10Y",
            "base_scheme_id": "demo_blackbox",
            "runtime_type": "blackbox_v2",
            "status": "active",
            "task_type": "T+1",
            "target_tenor": "10Y",
            "horizon": 1,
        }
        row.update(updates)
        return row

    def test_exact_approved_version_and_registry_identity_are_executable(self) -> None:
        from dataclasses import FrozenInstanceError

        from scheduler.repository import read_blackbox_execution_approval

        engine = _CaptureEngine(
            version_row=self._approved_version_row(),
            registry_rows=[self._active_registry_row()],
        )

        approval = read_blackbox_execution_approval(engine, _blackbox_config())

        self.assertTrue(approval.executable)
        self.assertEqual(approval.reason, "approved")
        self.assertEqual(approval.version_status, "active")
        self.assertEqual(approval.approved_by, "release-owner")
        self.assertEqual(approval.approved_at, datetime(2026, 7, 20, 8, 30))
        self.assertEqual(approval.base_scheme_id, "demo_blackbox")
        self.assertEqual(approval.scheme_version, "abc123def456")
        self.assertEqual(approval.version_runtime_type, "blackbox_v2")
        self.assertEqual(approval.registry_scheme_ids, ("demo_blackbox__h1__10Y",))
        with self.assertRaises(FrozenInstanceError):
            approval.reason = "mutated"

    def test_exact_approval_rejects_active_config_with_shadow_version(self) -> None:
        from scheduler.repository import read_blackbox_execution_approval

        engine = _CaptureEngine(
            version_row=self._approved_version_row(),
            registry_rows=[self._active_registry_row()],
        )

        approval = read_blackbox_execution_approval(
            engine,
            _blackbox_config(status="active", version_status="shadow"),
        )

        self.assertFalse(approval.executable)
        self.assertEqual(
            approval.reason,
            "config version_status is shadow, expected active",
        )

    def test_exact_approval_fails_closed_for_each_missing_or_mismatched_state(self) -> None:
        from scheduler.repository import read_blackbox_execution_approval

        cases = [
            (
                "missing exact version",
                None,
                [self._active_registry_row()],
                "exact version not found: scheme_id=demo_blackbox scheme_version=abc123def456",
            ),
            (
                "version mismatch",
                self._approved_version_row(scheme_version="different-version"),
                [self._active_registry_row()],
                "version identity mismatch: expected demo_blackbox/abc123def456, got demo_blackbox/different-version",
            ),
            (
                "wrong version runtime",
                self._approved_version_row(runtime_type="native_adapter"),
                [self._active_registry_row()],
                "version runtime_type is native_adapter, expected blackbox_v2",
            ),
            (
                "shadow version",
                self._approved_version_row(status="shadow"),
                [self._active_registry_row()],
                "version status is shadow, expected active",
            ),
            (
                "missing approver",
                self._approved_version_row(approved_by=None),
                [self._active_registry_row()],
                "version approved_by is null",
            ),
            (
                "missing approval time",
                self._approved_version_row(approved_at=None),
                [self._active_registry_row()],
                "version approved_at is null",
            ),
            (
                "empty approver",
                self._approved_version_row(approved_by="   "),
                [self._active_registry_row()],
                "version approved_by is empty",
            ),
            (
                "non-datetime approval time",
                self._approved_version_row(approved_at="2026-07-20 08:30:00"),
                [self._active_registry_row()],
                "version approved_at must be datetime",
            ),
            (
                "missing registry",
                self._approved_version_row(),
                [],
                "registry row missing: demo_blackbox__h1__10Y",
            ),
            (
                "registry id mismatch",
                self._approved_version_row(),
                [self._active_registry_row(scheme_id="demo_blackbox__h1__5Y")],
                "registry scheme_id mismatch: expected demo_blackbox__h1__10Y, got demo_blackbox__h1__5Y",
            ),
            (
                "registry base mismatch",
                self._approved_version_row(),
                [self._active_registry_row(base_scheme_id="other")],
                "registry base_scheme_id mismatch for demo_blackbox__h1__10Y: expected demo_blackbox, got other",
            ),
            (
                "registry paused",
                self._approved_version_row(),
                [self._active_registry_row(status="paused")],
                "registry demo_blackbox__h1__10Y status is paused, expected active",
            ),
            (
                "registry runtime mismatch",
                self._approved_version_row(),
                [self._active_registry_row(runtime_type="native_adapter")],
                "registry demo_blackbox__h1__10Y runtime_type is native_adapter, expected blackbox_v2",
            ),
            (
                "registry task mismatch",
                self._approved_version_row(),
                [self._active_registry_row(task_type="T+5")],
                "registry demo_blackbox__h1__10Y task_type is T+5, expected T+1",
            ),
            (
                "registry tenor mismatch",
                self._approved_version_row(),
                [self._active_registry_row(target_tenor="5Y")],
                "registry demo_blackbox__h1__10Y target_tenor is 5Y, expected 10Y",
            ),
            (
                "registry horizon mismatch",
                self._approved_version_row(),
                [self._active_registry_row(horizon=5)],
                "registry demo_blackbox__h1__10Y horizon is 5, expected 1",
            ),
        ]
        for label, version_row, registry_rows, expected_reason in cases:
            with self.subTest(label=label):
                engine = _CaptureEngine(version_row=version_row, registry_rows=registry_rows)
                approval = read_blackbox_execution_approval(engine, _blackbox_config())
                self.assertFalse(approval.executable)
                self.assertEqual(approval.reason, expected_reason)

    def test_exact_approval_rejects_duplicate_expected_tenors_and_registry_ids(self) -> None:
        from scheduler.repository import read_blackbox_execution_approval

        cfg = _blackbox_config()
        cfg.tenors = ["10Y", "10Y"]
        engine = _CaptureEngine(
            version_row=self._approved_version_row(),
            registry_rows=[self._active_registry_row()],
        )

        approval = read_blackbox_execution_approval(engine, cfg)

        self.assertFalse(approval.executable)
        self.assertEqual(
            approval.reason,
            "duplicate expected tenors/Registry ids: "
            "tenors=['10Y'], registry_ids=['demo_blackbox__h1__10Y']",
        )


class _Result:
    lastrowid = 101


class _RunConnection:
    def __init__(self, store: dict) -> None:
        self._store = store

    def execute(self, sql, params=None) -> _Result | _MappingResult:
        sql_text = str(sql)
        self._store.setdefault("calls", []).append((sql_text, params))
        self._store["sql"] = sql_text
        self._store["params"] = params
        if sql_text.lstrip().startswith("SELECT"):
            return _MappingResult()
        return _Result()


class _RunBegin:
    def __init__(self, store: dict) -> None:
        self._store = store

    def __enter__(self) -> _RunConnection:
        return _RunConnection(self._store)

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _RunEngine:
    def __init__(self) -> None:
        self.store: dict = {}

    def begin(self) -> _RunBegin:
        return _RunBegin(self.store)


class ImmutablePredictionRepositoryTests(unittest.TestCase):
    def test_attach_run_data_snapshot_updates_only_the_run_audit_row(self) -> None:
        from scheduler.repository import attach_run_data_snapshot

        engine = _RunEngine()
        attach_run_data_snapshot(engine, run_id=101, data_snapshot_id="snapshot-1")

        self.assertIn("UPDATE t_scheme_runs", engine.store["sql"])
        self.assertEqual(engine.store["params"], {"run_id": 101, "data_snapshot_id": "snapshot-1"})

    def test_create_scheme_run_inserts_running_row_and_returns_run_id(self) -> None:
        from scheduler.repository import create_scheme_run

        engine = _RunEngine()
        run_id = create_scheme_run(
            engine,
            scheme_id="t1_daily",
            predict_date="2026-06-05",
            scheme_version="abc123",
            run_type="active",
            prediction_phase="scheduled_live",
            input_artifact_id="artifact-1",
            data_snapshot_id="snapshot-1",
        )

        self.assertEqual(run_id, 101)
        sql = engine.store["sql"]
        params = engine.store["params"]
        self.assertIn("INSERT INTO t_scheme_runs", sql)
        self.assertEqual(params["scheme_id"], "t1_daily")
        self.assertEqual(params["scheme_version"], "abc123")
        self.assertEqual(params["predict_date"], "2026-06-05")
        self.assertEqual(params["run_type"], "active")
        self.assertEqual(params["prediction_phase"], "scheduled_live")
        self.assertEqual(params["status"], "running")
        self.assertEqual(params["input_artifact_id"], "artifact-1")
        self.assertEqual(params["data_snapshot_id"], "snapshot-1")
        self.assertEqual(params["runtime_type"], "native_adapter")

    def test_insert_run_predictions_upserts_prediction_semantics(self) -> None:
        from scheduler.repository import insert_run_predictions
        from shared.models import PredictionRecord

        engine = _RunEngine()
        records = [
            PredictionRecord(
                scheme_id="t1_daily",
                target_tenor="10Y",
                horizon=1,
                predict_date="2026-06-05",
                target_date="2026-06-06",
                feature_date="2026-06-04",
                prediction_phase="scheduled_live",
                predicted_direction=1,
                confidence=0.8,
                extra={"feature_date": "2026-06-04"},
            )
        ]

        written = insert_run_predictions(engine, 101, records, scheme_version="abc123")

        self.assertEqual(written, 1)
        sql = engine.store["sql"]
        rows = engine.store["params"]
        self.assertIn("INSERT INTO t_scheme_predictions", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("run_id = VALUES(run_id)", sql)
        self.assertIn("scheme_version = VALUES(scheme_version)", sql)
        self.assertIn("predict_date = VALUES(predict_date)", sql)
        self.assertIn("feature_date = VALUES(feature_date)", sql)
        self.assertIn("prediction_phase = VALUES(prediction_phase)", sql)
        self.assertEqual(rows[0]["run_id"], 101)
        self.assertEqual(rows[0]["scheme_version"], "abc123")
        self.assertEqual(rows[0]["feature_date"], "2026-06-04")
        self.assertEqual(rows[0]["prediction_phase"], "scheduled_live")

    def test_insert_run_predictions_requires_feature_date_and_phase(self) -> None:
        from scheduler.repository import insert_run_predictions
        from shared.models import PredictionRecord

        engine = _RunEngine()
        record = PredictionRecord(
            scheme_id="t1_daily",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-06-05",
            target_date="2026-06-06",
            predicted_direction=1,
        )

        with self.assertRaisesRegex(ValueError, "feature_date"):
            insert_run_predictions(engine, 101, [record], scheme_version="abc123")

    def test_final_blackbox_insert_locks_revalidates_and_writes_in_one_transaction(self) -> None:
        from scheduler.repository import insert_approved_blackbox_predictions
        from shared.models import PredictionRecord

        version_row = {
            "scheme_id": "demo_blackbox",
            "scheme_version": "abc123def456",
            "runtime_type": "blackbox_v2",
            "status": "active",
            "approved_by": "release-owner",
            "approved_at": datetime(2026, 7, 20, 8, 30),
        }
        registry_row = {
            "scheme_id": "demo_blackbox__h1__10Y",
            "base_scheme_id": "demo_blackbox",
            "runtime_type": "blackbox_v2",
            "status": "active",
            "task_type": "T+1",
            "target_tenor": "10Y",
            "horizon": 1,
        }
        engine = _CaptureEngine(version_row=version_row, registry_rows=[registry_row])
        record = PredictionRecord(
            scheme_id="demo_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="scheduled_live",
            predicted_direction=1,
            extra={"feature_date": "2026-07-17", "prediction_phase": "scheduled_live"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _set_canonical_path(_blackbox_config(), Path(tmpdir))
            with patch("scheduler.repository.load_scheme_config", return_value=cfg):
                written = insert_approved_blackbox_predictions(
                    engine,
                    cfg,
                    101,
                    [record],
                    scheme_version="abc123def456",
                )

        self.assertEqual(written, 1)
        self.assertEqual(engine.store["begin_count"], 1)
        version_select, _ = _call_for(engine.store, "FROM t_scheme_versions")
        registry_select, _ = _call_for(engine.store, "FROM t_scheme_registry")
        self.assertIn("FOR UPDATE", version_select)
        self.assertIn("FOR UPDATE", registry_select)
        self.assertEqual(len(engine.store["prediction_rows"]), 1)
        self.assertEqual(
            engine.store["prediction_rows"][0]["scheme_version"],
            "abc123def456",
        )

    def test_final_blackbox_insert_rejects_missing_canonical_config_path(self) -> None:
        from scheduler.repository import insert_approved_blackbox_predictions
        from shared.models import PredictionRecord

        engine = _CaptureEngine()
        record = PredictionRecord(
            scheme_id="demo_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="scheduled_live",
            predicted_direction=1,
        )

        with self.assertRaisesRegex(RuntimeError, "canonical config path is required"):
            insert_approved_blackbox_predictions(
                engine,
                _blackbox_config(),
                101,
                [record],
                scheme_version="abc123def456",
            )

        self.assertEqual(engine.store["begin_count"], 0)
        self.assertEqual(engine.store["prediction_rows"], [])

    def test_final_blackbox_insert_holds_lifecycle_lock_through_prediction_insert(self) -> None:
        from scheduler import repository
        from shared.blackbox_v2.lifecycle import LifecycleState, perform_lifecycle_transition
        from shared.models import PredictionRecord

        version_row = {
            "scheme_id": "demo_blackbox",
            "scheme_version": "abc123def456",
            "runtime_type": "blackbox_v2",
            "status": "active",
            "approved_by": "release-owner",
            "approved_at": datetime(2026, 7, 20, 8, 30),
        }
        registry_row = {
            "scheme_id": "demo_blackbox__h1__10Y",
            "base_scheme_id": "demo_blackbox",
            "runtime_type": "blackbox_v2",
            "status": "active",
            "task_type": "T+1",
            "target_tenor": "10Y",
            "horizon": 1,
        }
        engine = _CaptureEngine(version_row=version_row, registry_rows=[registry_row])
        record = PredictionRecord(
            scheme_id="demo_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="scheduled_live",
            predicted_direction=1,
        )
        insert_entered = threading.Event()
        allow_insert = threading.Event()
        transition_started = threading.Event()
        transition_finished = threading.Event()
        original_insert = repository._insert_run_predictions_conn

        def blocking_insert(*args, **kwargs):
            insert_entered.set()
            if not allow_insert.wait(timeout=2):
                raise TimeoutError("test did not release prediction insert")
            return original_insert(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = root / "schemes" / "demo_blackbox"
            scheme_dir.mkdir(parents=True)
            config_path = scheme_dir / "config.yaml"
            config_path.write_text(
                "status: active\nversion_status: active\n",
                encoding="utf-8",
            )
            cfg = _blackbox_config()
            cfg.path = scheme_dir
            lifecycle_state = {
                "value": LifecycleState("active", "active", "active", "active")
            }
            paused = LifecycleState("paused", "paused", "paused", "paused")

            def run_transition():
                transition_started.set()
                result = perform_lifecycle_transition(
                    project_root=root,
                    config_path=config_path,
                    action="pause",
                    scheme_id=cfg.scheme_id,
                    scheme_version=cfg.scheme_version,
                    harness_run_id="hr_pause",
                    previous=lifecycle_state["value"],
                    target=paused,
                    compensation=lifecycle_state["value"],
                    token_hash="a" * 64,
                    consume_authorization=lambda: None,
                    apply_database=lambda state: lifecycle_state.__setitem__("value", state),
                    read_state=lambda: lifecycle_state["value"],
                )
                transition_finished.set()
                return result

            with (
                patch("scheduler.repository.load_scheme_config", return_value=cfg),
                patch(
                    "scheduler.repository._insert_run_predictions_conn",
                    side_effect=blocking_insert,
                ),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                writer = pool.submit(
                    repository.insert_approved_blackbox_predictions,
                    engine,
                    cfg,
                    101,
                    [record],
                    scheme_version="abc123def456",
                )
                self.assertTrue(insert_entered.wait(timeout=1))
                transition = pool.submit(run_transition)
                self.assertTrue(transition_started.wait(timeout=1))
                self.assertFalse(
                    transition_finished.wait(timeout=0.2),
                    "lifecycle transition interleaved between final revalidation and insert",
                )
                allow_insert.set()
                self.assertEqual(writer.result(timeout=2), 1)
                transition.result(timeout=2)

        self.assertTrue(transition_finished.is_set())
        self.assertEqual(len(engine.store["prediction_rows"]), 1)

    def test_final_blackbox_insert_rejects_pending_reconciliation_before_db_access(self) -> None:
        from scheduler.repository import insert_approved_blackbox_predictions
        from shared.blackbox_v2.lifecycle import LifecycleJournal, LifecycleState, write_journal
        from shared.models import PredictionRecord

        engine = _CaptureEngine()
        record = PredictionRecord(
            scheme_id="demo_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="scheduled_live",
            predicted_direction=1,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = _set_canonical_path(_blackbox_config(), root)
            state = LifecycleState("active", "active", "active", "active")
            pending = LifecycleJournal.prepare(
                action="pause",
                scheme_id=cfg.scheme_id,
                scheme_version=cfg.scheme_version,
                harness_run_id="hr_pending_reconcile",
                previous=state,
                target=LifecycleState("paused", "paused", "paused", "paused"),
                token_hash="a" * 64,
            ).transition("config_written")
            write_journal(root, pending)

            with (
                patch("scheduler.repository.load_scheme_config", return_value=cfg),
                self.assertRaisesRegex(RuntimeError, "lifecycle journal"),
            ):
                insert_approved_blackbox_predictions(
                    engine,
                    cfg,
                    101,
                    [record],
                    scheme_version=cfg.scheme_version,
                )

        self.assertEqual(engine.store["begin_count"], 0)
        self.assertEqual(engine.store["prediction_rows"], [])

    def test_final_blackbox_insert_rejects_revoked_approval_without_predictions(self) -> None:
        from scheduler.repository import insert_approved_blackbox_predictions
        from shared.models import PredictionRecord

        engine = _CaptureEngine(
            version_row={
                "scheme_id": "demo_blackbox",
                "scheme_version": "abc123def456",
                "runtime_type": "blackbox_v2",
                "status": "shadow",
                "approved_by": None,
                "approved_at": None,
            },
            registry_rows=[
                {
                    "scheme_id": "demo_blackbox__h1__10Y",
                    "base_scheme_id": "demo_blackbox",
                    "runtime_type": "blackbox_v2",
                    "status": "active",
                    "task_type": "T+1",
                    "target_tenor": "10Y",
                    "horizon": 1,
                }
            ],
        )
        record = PredictionRecord(
            scheme_id="demo_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="scheduled_live",
            predicted_direction=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _set_canonical_path(_blackbox_config(), Path(tmpdir))
            with (
                patch("scheduler.repository.load_scheme_config", return_value=cfg),
                self.assertRaisesRegex(
                    RuntimeError,
                    "Blackbox V2 version is not production-approved: "
                    "version status is shadow, expected active",
                ),
            ):
                insert_approved_blackbox_predictions(
                    engine,
                    cfg,
                    101,
                    [record],
                    scheme_version="abc123def456",
                )

        self.assertEqual(engine.store["prediction_rows"], [])

    def test_final_blackbox_insert_rejects_shadow_config_version_without_predictions(self) -> None:
        from scheduler.repository import insert_approved_blackbox_predictions
        from shared.models import PredictionRecord

        engine = _CaptureEngine(
            version_row={
                "scheme_id": "demo_blackbox",
                "scheme_version": "abc123def456",
                "runtime_type": "blackbox_v2",
                "status": "active",
                "approved_by": "release-owner",
                "approved_at": datetime(2026, 7, 20, 8, 30),
            },
            registry_rows=[
                {
                    "scheme_id": "demo_blackbox__h1__10Y",
                    "base_scheme_id": "demo_blackbox",
                    "runtime_type": "blackbox_v2",
                    "status": "active",
                    "task_type": "T+1",
                    "target_tenor": "10Y",
                    "horizon": 1,
                }
            ],
        )
        record = PredictionRecord(
            scheme_id="demo_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="scheduled_live",
            predicted_direction=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _set_canonical_path(
                _blackbox_config(status="active", version_status="shadow"),
                Path(tmpdir),
            )
            with (
                patch("scheduler.repository.load_scheme_config", return_value=cfg),
                self.assertRaisesRegex(
                    RuntimeError,
                    "canonical config changed before final write.*current=.*active/shadow",
                ),
            ):
                insert_approved_blackbox_predictions(
                    engine,
                    cfg,
                    101,
                    [record],
                    scheme_version="abc123def456",
                )

        self.assertEqual(engine.store["prediction_rows"], [])

    def test_final_blackbox_insert_rejects_disk_config_version_drift(self) -> None:
        from scheduler.repository import insert_approved_blackbox_predictions
        from shared.models import PredictionRecord

        drifted = _blackbox_config(scheme_version="drifted-version")
        engine = _CaptureEngine(
            version_row={
                "scheme_id": "demo_blackbox",
                "scheme_version": "abc123def456",
                "runtime_type": "blackbox_v2",
                "status": "active",
                "approved_by": "release-owner",
                "approved_at": datetime(2026, 7, 20, 8, 30),
            },
            registry_rows=[],
        )
        record = PredictionRecord(
            scheme_id="demo_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="scheduled_live",
            predicted_direction=1,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _set_canonical_path(_blackbox_config(), Path(tmpdir))
            with (
                patch(
                    "scheduler.repository.load_scheme_config",
                    return_value=drifted,
                    create=True,
                ),
                self.assertRaisesRegex(RuntimeError, "canonical config changed before final write"),
            ):
                insert_approved_blackbox_predictions(
                    engine,
                    cfg,
                    101,
                    [record],
                    scheme_version="abc123def456",
                )

        self.assertEqual(engine.store["begin_count"], 0)
        self.assertEqual(engine.store["prediction_rows"], [])

    def test_upsert_scheme_version_writes_version_hashes(self) -> None:
        from scheduler.repository import upsert_scheme_version

        engine = _RunEngine()
        cfg = SimpleNamespace(
            scheme_id="t1_daily",
            scheme_version="abc123def456",
            code_hash="c" * 64,
            config_hash="f" * 64,
            manifest_hash=None,
            status="active",
            runtime_type="blackbox_v2",
            version_status="shadow",
            algorithm_version="1.2.3",
            contract_version="1.0",
            runtime_profile="blackbox-v2-v1",
            environment_fingerprint="e" * 64,
            data_snapshot_id="snapshot-1",
        )

        version = upsert_scheme_version(engine, cfg)

        self.assertEqual(version, "abc123def456")
        sql = engine.store["sql"]
        params = engine.store["params"]
        self.assertIn("INSERT INTO t_scheme_versions", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertEqual(params["scheme_id"], "t1_daily")
        self.assertEqual(params["scheme_version"], "abc123def456")
        self.assertEqual(params["code_hash"], "c" * 64)
        self.assertEqual(params["config_hash"], "f" * 64)
        self.assertIsNone(params["manifest_hash"])
        self.assertEqual(params["status"], "draft")
        self.assertTrue(params["preserve_lifecycle"])
        self.assertFalse(params["trusted_lifecycle"])
        self.assertEqual(params["runtime_type"], "blackbox_v2")
        self.assertEqual(params["algorithm_version"], "1.2.3")
        self.assertEqual(params["contract_version"], "1.0")
        self.assertEqual(params["runtime_profile"], "blackbox-v2-v1")
        self.assertEqual(params["environment_fingerprint"], "e" * 64)
        self.assertEqual(params["data_snapshot_id"], "snapshot-1")


if __name__ == "__main__":
    unittest.main()
