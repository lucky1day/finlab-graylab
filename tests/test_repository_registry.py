from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


class _MappingResult:
    def __init__(self, rows: list[dict] | None = None, *, rowcount: int = 1) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> _MappingResult:
        return self

    def one_or_none(self) -> dict | None:
        if len(self._rows) > 1:
            raise AssertionError("expected at most one row")
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict]:
        return list(self._rows)


class _CaptureConnection:
    schema_capabilities = {"daily_schedule_ledger": False}

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


class _AtomicConnection(_CaptureConnection):
    def execute(self, sql, rows=None):
        sql_text = str(sql)
        compact_sql = " ".join(sql_text.split())
        fail_stage = self._store.get("fail_stage")
        if sql_text.lstrip().startswith("SELECT") and "FROM t_scheme_runs" in sql_text:
            self._store.setdefault("calls", []).append((sql_text, rows))
            run_row = self._store.get("run_row")
            return _MappingResult([run_row] if run_row is not None else [])
        if (
            sql_text.lstrip().startswith("SELECT")
            and "FROM t_scheme_predictions" in sql_text
            and "WHERE run_id = :run_id" in sql_text
        ):
            self._store.setdefault("calls", []).append((sql_text, rows))
            run_id = int(rows["run_id"])
            prediction_rows = [
                row
                for row in self._store.get("prediction_rows", [])
                if int(row.get("run_id") or 0) == run_id
            ]
            return _MappingResult(prediction_rows)
        if (
            compact_sql.startswith(
                "SELECT scheme_id, target_tenor, horizon, target_date "
                "FROM t_scheme_predictions"
            )
            and "WHERE scheme_id = :scheme_id" in compact_sql
            and "AND target_tenor = :target_tenor" in compact_sql
            and "AND horizon = :horizon" in compact_sql
            and "AND target_date = :target_date" in compact_sql
        ):
            self._store.setdefault("calls", []).append((sql_text, rows))
            prediction_rows = [
                {
                    key: row[key]
                    for key in (
                        "scheme_id",
                        "target_tenor",
                        "horizon",
                        "target_date",
                    )
                }
                for row in self._store.get("prediction_rows", [])
                if row["scheme_id"] == rows["scheme_id"]
                and row["target_tenor"] == rows["target_tenor"]
                and int(row["horizon"]) == int(rows["horizon"])
                and str(row["target_date"]) == str(rows["target_date"])
            ]
            return _MappingResult(prediction_rows)
        if "INSERT INTO t_scheme_predictions" in sql_text and fail_stage == "prediction":
            raise RuntimeError("injected prediction failure")
        if (
            "INSERT INTO t_scheme_predictions" in sql_text
            and "ON DUPLICATE KEY UPDATE" not in sql_text
        ):
            existing_keys = {
                (
                    row["scheme_id"],
                    row["target_tenor"],
                    row["horizon"],
                    row["target_date"],
                )
                for row in self._store.get("prediction_rows", [])
            }
            incoming_keys = {
                (
                    row["scheme_id"],
                    row["target_tenor"],
                    row["horizon"],
                    row["target_date"],
                )
                for row in rows
            }
            if existing_keys & incoming_keys:
                raise RuntimeError("duplicate prediction target key")
        result = super().execute(sql, rows)
        if "UPDATE t_scheme_runs" in sql_text:
            if "SET data_snapshot_id" in sql_text:
                if fail_stage == "snapshot":
                    raise RuntimeError("injected snapshot failure")
                if self._store["run_row"].get("data_snapshot_id") is not None:
                    return _MappingResult(rowcount=0)
                self._store["run_row"]["data_snapshot_id"] = rows[
                    "data_snapshot_id"
                ]
                return result
            if "SET status = 'success'" in sql_text:
                if fail_stage == "run":
                    raise RuntimeError("injected run failure")
                if fail_stage == "run_missing":
                    return _MappingResult(rowcount=0)
                self._store["run_row"].update(
                    {
                        "status": "success",
                        "records_returned": rows["records_returned"],
                        "records_written": rows["records_written"],
                        "error_message": None,
                    }
                )
                return result
            if fail_stage == "run":
                raise RuntimeError("injected run failure")
            if fail_stage == "run_missing":
                return _MappingResult(rowcount=0)
            self._store["run_row"].update(
                {
                    "status": rows["status"],
                    "records_returned": rows["records_returned"],
                    "records_written": rows["records_written"],
                    "error_message": rows["error_message"],
                }
            )
        if "INSERT INTO t_scheme_run_log" in sql_text:
            if fail_stage == "log":
                raise RuntimeError("injected log failure")
            self._store.setdefault("run_log_rows", []).append(dict(rows))
        return result


class _AtomicBegin:
    def __init__(self, engine: "_AtomicEngine") -> None:
        self._engine = engine
        self._staged = deepcopy(engine.store)

    def __enter__(self) -> _AtomicConnection:
        self._engine.begin_count += 1
        return _AtomicConnection(self._staged)

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._engine.store.clear()
            self._engine.store.update(self._staged)


class _AtomicEngine:
    def __init__(self, *, fail_stage: str | None = None) -> None:
        self.store = {
            "version_row": {
                "scheme_id": "demo_blackbox",
                "scheme_version": "abc123def456",
                "runtime_type": "blackbox_v2",
                "status": "active",
                "approved_by": "release-owner",
                "approved_at": datetime(2026, 7, 20, 8, 30),
            },
            "registry_rows": [
                {
                    "scheme_id": "demo_blackbox__h1__10Y",
                    "base_scheme_id": "demo_blackbox",
                    "runtime_type": "blackbox_v2",
                    "status": "active",
                    "frequency": "daily",
                    "task_type": "T+1",
                    "target_tenor": "10Y",
                    "horizon": 1,
                }
            ],
            "prediction_rows": [],
            "run_log_rows": [],
            "run_row": {
                "run_id": 101,
                "scheme_id": "demo_blackbox",
                "scheme_version": "abc123def456",
                "runtime_type": "blackbox_v2",
                "run_type": "active",
                "prediction_phase": "scheduled_live",
                "predict_date": "2026-07-20",
                "status": "running",
                "records_expected": 1,
                "records_returned": None,
                "records_written": None,
                "error_message": None,
                "data_snapshot_id": None,
                "schedule_item_id": None,
            },
            "fail_stage": fail_stage,
        }
        self.begin_count = 0

    def begin(self) -> _AtomicBegin:
        return _AtomicBegin(self)


def _native_atomic_engine(*, fail_stage: str | None = None) -> _AtomicEngine:
    engine = _AtomicEngine(fail_stage=fail_stage)
    engine.store["version_row"] = {
        "scheme_id": "native_daily",
        "scheme_version": "native-version-1",
        "runtime_type": "native_adapter",
        "status": "active",
        "approved_by": "native-release-owner",
        "approved_at": datetime(2026, 7, 20, 8, 30),
    }
    engine.store["registry_rows"] = [_native_registry_row()]
    engine.store["run_row"].update(
        {
            "scheme_id": "native_daily",
            "scheme_version": "native-version-1",
            "runtime_type": "native_adapter",
            "prediction_phase": "gray_live",
        }
    )
    return engine


def _blackbox_config(
    *,
    status: str = "active",
    version_status: str = "active",
    scheme_version: str = "abc123def456",
    tenors: tuple[str, ...] = ("10Y",),
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id="demo_blackbox",
        name="Demo Blackbox",
        description="blackbox scheme",
        horizon=1,
        task_type="T+1",
        tenors=list(tenors),
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


def _native_config(
    *,
    status: str = "active",
    scheme_version: str = "native-version-1",
    tenors: tuple[str, ...] = ("5Y",),
) -> SimpleNamespace:
    return SimpleNamespace(
        scheme_id="native_daily",
        name="Native Daily",
        description="native scheme",
        horizon=1,
        task_type="T+1",
        tenors=list(tenors),
        frequency="daily",
        schedule=SimpleNamespace(cron="3 7 * * 1-5", timezone="Asia/Shanghai"),
        status=status,
        scheme_version=scheme_version,
        code_hash="c" * 64,
        config_hash="f" * 64,
        manifest_hash=None,
        runtime_type="native_adapter",
        version_status=status,
    )


def _native_registry_row(**updates) -> dict:
    row = {
        "scheme_id": "native_daily__h1__5Y",
        "base_scheme_id": "native_daily",
        "runtime_type": "native_adapter",
        "status": "active",
        "frequency": "daily",
        "task_type": "T+1",
        "target_tenor": "5Y",
        "horizon": 1,
    }
    row.update(updates)
    return row


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



    def test_unknown_active_native_sync_creates_draft_version_and_paused_registry(self) -> None:
        from scheduler.repository import sync_scheme_registry

        engine = _CaptureEngine()

        sync_scheme_registry(engine, [_native_config()])

        _, version_params = _call_for(engine.store, "INSERT INTO t_scheme_versions")
        _, registry_rows = _call_for(engine.store, "INSERT INTO t_scheme_registry")
        self.assertEqual(version_params["status"], "draft")
        self.assertEqual({row["status"] for row in registry_rows}, {"paused"})





    def test_trusted_native_activation_writes_exact_approval_and_target_registry_atomically(self) -> None:
        from scheduler.repository import apply_native_activation_state

        engine = _CaptureEngine()
        approved_at = datetime(
            2026,
            7,
            20,
            16,
            30,
            45,
            tzinfo=timezone(timedelta(hours=8)),
        )

        activated_version = apply_native_activation_state(
            engine,
            _native_config(),
            approved_by="native-release-owner",
            approved_at=approved_at,
        )

        self.assertEqual(activated_version, "native-version-1")
        self.assertEqual(engine.store["begin_count"], 1)
        self.assertEqual(engine.store["version_row"]["status"], "active")
        self.assertEqual(
            engine.store["version_row"]["approved_by"],
            "native-release-owner",
        )
        self.assertEqual(
            engine.store["version_row"]["approved_at"],
            datetime(2026, 7, 20, 8, 30, 45),
        )
        self.assertEqual(
            {row["scheme_id"] for row in engine.store["registry_rows"]},
            {"native_daily__h1__5Y"},
        )
        self.assertEqual(
            {row["status"] for row in engine.store["registry_rows"]},
            {"active"},
        )





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

    def test_lifecycle_read_returns_validated_registry_identities(self) -> None:
        from scheduler.repository import read_blackbox_lifecycle_state

        engine = _CaptureEngine(
            version_row={
                **self._approved_version_row(),
                "environment_fingerprint": "e" * 64,
                "data_snapshot_id": "snapshot-1",
                "code_hash": "c" * 64,
                "config_hash": "f" * 64,
                "manifest_hash": "m" * 64,
            },
            registry_rows=[self._active_registry_row()],
        )

        state = read_blackbox_lifecycle_state(
            engine,
            _blackbox_config(),
        )

        self.assertEqual(
            state.registry_scheme_ids,
            ("demo_blackbox__h1__10Y",),
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
    schema_capabilities = {"daily_schedule_ledger": False}

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
    def test_scheduled_live_run_creation_without_one_shot_plane_fails_closed(
        self,
    ) -> None:
        from scheduler.repository import create_scheme_run

        with self.assertRaisesRegex(
            RuntimeError,
            "requires an installed one-shot control plane",
        ):
            create_scheme_run(
                _RunEngine(),
                scheme_id="t1_daily",
                predict_date="2026-07-24",
                prediction_phase="scheduled_live",
            )


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
            scheduled_control_plane="launchd_one_shot",
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

    def test_create_scheme_run_accepts_systemd_one_shot_for_scheduled_live(
        self,
    ) -> None:
        from scheduler.repository import create_scheme_run

        engine = _RunEngine()
        run_id = create_scheme_run(
            engine,
            scheme_id="linux_daily",
            predict_date="2026-08-17",
            prediction_phase="scheduled_live",
            scheduled_control_plane="systemd_one_shot",
        )

        self.assertEqual(run_id, 101)
        self.assertEqual(
            engine.store["params"]["prediction_phase"],
            "scheduled_live",
        )


    def test_active_native_completion_inserts_prediction_and_finishes_atomically(self) -> None:
        from scheduler.repository import complete_active_native_run
        from shared.models import PredictionRecord

        engine = _native_atomic_engine()
        records = [
            PredictionRecord(
                scheme_id="native_daily",
                target_tenor="5Y",
                horizon=1,
                predict_date="2026-07-20",
                target_date="2026-07-21",
                feature_date="2026-07-17",
                prediction_phase="gray_live",
                predicted_direction=1,
                confidence=0.8,
                extra={"feature_date": "2026-07-17"},
            )
        ]

        status, written, error_message = complete_active_native_run(
            engine,
            _native_config(),
            run_id=101,
            records=records,
            scheme_version="native-version-1",
            records_returned=1,
            run_date="2026-07-20",
            duration_sec=2.5,
        )

        self.assertEqual((status, written, error_message), ("success", 1, None))
        self.assertEqual(engine.begin_count, 1)
        self.assertEqual(written, 1)
        sql, rows = _call_for(engine.store, "INSERT INTO t_scheme_predictions")
        self.assertIn("INSERT INTO t_scheme_predictions", sql)
        self.assertNotIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertNotIn("ON CONFLICT", sql)
        self.assertEqual(rows[0]["run_id"], 101)
        self.assertEqual(rows[0]["scheme_version"], "native-version-1")
        self.assertEqual(rows[0]["feature_date"], "2026-07-17")
        self.assertEqual(rows[0]["prediction_phase"], "gray_live")
        self.assertEqual(engine.store["prediction_rows"], rows)
        self.assertEqual(engine.store["run_row"]["status"], "success")
        self.assertEqual(engine.store["run_row"]["records_written"], 1)
        self.assertEqual(len(engine.store["run_log_rows"]), 1)
        self.assertEqual(engine.store["run_log_rows"][0]["status"], "success")

    def test_active_native_duplicate_completion_skips_without_mutating_prediction(
        self,
    ) -> None:
        from scheduler.repository import (
            PREDICTION_KEYS_ALREADY_EXIST,
            complete_active_native_run,
        )
        from shared.models import PredictionRecord

        engine = _native_atomic_engine()
        original = {
            "id": 701,
            "run_id": 88,
            "scheme_version": "native-version-old",
            "scheme_id": "native_daily",
            "target_tenor": "5Y",
            "horizon": 1,
            "predict_date": "2026-07-19",
            "feature_date": "2026-07-16",
            "target_date": "2026-07-21",
            "prediction_phase": "gray_live",
            "predicted_direction": -1,
            "confidence": 0.2,
            "model_version": "old-native-model",
            "extra": '{"source":"original"}',
            "created_at": datetime(2026, 7, 19, 9, 0),
            "updated_at": datetime(2026, 7, 19, 9, 0),
        }
        engine.store["prediction_rows"] = [deepcopy(original)]
        record = PredictionRecord(
            scheme_id="native_daily",
            target_tenor="5Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="gray_live",
            predicted_direction=1,
            confidence=0.95,
            model_version="new-native-model",
            extra={"source": "rerun"},
        )

        result = complete_active_native_run(
            engine,
            _native_config(),
            run_id=101,
            records=[record],
            scheme_version="native-version-1",
            records_returned=1,
            run_date="2026-07-20",
            duration_sec=2.5,
        )

        self.assertEqual(
            result,
            ("skipped", 0, PREDICTION_KEYS_ALREADY_EXIST),
        )
        self.assertEqual(engine.store["prediction_rows"], [original])
        self.assertFalse(
            any(
                "INSERT INTO t_scheme_predictions" in sql
                for sql, _rows in engine.store["calls"]
            )
        )
        self.assertEqual(engine.store["run_row"]["status"], "skipped")
        self.assertEqual(engine.store["run_row"]["records_written"], 0)
        self.assertEqual(
            engine.store["run_row"]["error_message"],
            PREDICTION_KEYS_ALREADY_EXIST,
        )
        self.assertEqual(engine.store["run_log_rows"][0]["status"], "skipped")
        self.assertEqual(
            engine.store["run_log_rows"][0]["error_msg"],
            PREDICTION_KEYS_ALREADY_EXIST,
        )

    def test_active_native_partial_prediction_conflict_fails_without_writes(
        self,
    ) -> None:
        from scheduler.repository import (
            PARTIAL_PREDICTION_KEY_CONFLICT,
            complete_active_native_run,
        )
        from shared.models import PredictionRecord

        engine = _native_atomic_engine()
        engine.store["registry_rows"] = [
            _native_registry_row(),
            _native_registry_row(
                scheme_id="native_daily__h1__10Y",
                target_tenor="10Y",
            ),
        ]
        engine.store["run_row"]["records_expected"] = 2
        original = {
            "id": 701,
            "run_id": 88,
            "scheme_version": "native-version-old",
            "scheme_id": "native_daily",
            "target_tenor": "5Y",
            "horizon": 1,
            "predict_date": "2026-07-19",
            "feature_date": "2026-07-16",
            "target_date": "2026-07-21",
            "prediction_phase": "gray_live",
            "predicted_direction": -1,
            "confidence": 0.2,
            "model_version": "old-native-model",
            "extra": '{"source":"original"}',
            "updated_at": datetime(2026, 7, 19, 9, 0),
        }
        engine.store["prediction_rows"] = [deepcopy(original)]
        records = [
            PredictionRecord(
                scheme_id="native_daily",
                target_tenor=tenor,
                horizon=1,
                predict_date="2026-07-20",
                target_date="2026-07-21",
                feature_date="2026-07-17",
                prediction_phase="gray_live",
                predicted_direction=1,
            )
            for tenor in ("5Y", "10Y")
        ]

        status, written, error_message = complete_active_native_run(
            engine,
            _native_config(tenors=("5Y", "10Y")),
            run_id=101,
            records=records,
            scheme_version="native-version-1",
            records_returned=2,
            run_date="2026-07-20",
            duration_sec=2.5,
        )

        self.assertEqual((status, written), ("failed", 0))
        self.assertTrue(error_message.startswith(PARTIAL_PREDICTION_KEY_CONFLICT))
        self.assertIn("existing=", error_message)
        self.assertIn("native_daily/5Y/h1/2026-07-21", error_message)
        self.assertIn("missing=", error_message)
        self.assertIn("native_daily/10Y/h1/2026-07-21", error_message)
        self.assertEqual(engine.store["prediction_rows"], [original])
        self.assertFalse(
            any(
                "INSERT INTO t_scheme_predictions" in sql
                for sql, _rows in engine.store["calls"]
            )
        )
        self.assertEqual(engine.store["run_row"]["status"], "failed")
        self.assertEqual(engine.store["run_row"]["records_written"], 0)
        self.assertEqual(engine.store["run_log_rows"][0]["status"], "failed")
        self.assertEqual(
            engine.store["run_log_rows"][0]["error_msg"],
            error_message,
        )


    def test_active_native_completion_requires_feature_date(self) -> None:
        from scheduler.repository import complete_active_native_run
        from shared.models import PredictionRecord

        engine = _native_atomic_engine()
        record = PredictionRecord(
            scheme_id="native_daily",
            target_tenor="5Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            prediction_phase="gray_live",
            predicted_direction=1,
        )

        with self.assertRaisesRegex(ValueError, "feature_date"):
            complete_active_native_run(
                engine,
                _native_config(),
                run_id=101,
                records=[record],
                scheme_version="native-version-1",
                records_returned=1,
                run_date="2026-07-20",
                duration_sec=2.5,
            )

        self.assertEqual(engine.store["prediction_rows"], [])
        self.assertEqual(engine.store["run_row"]["status"], "running")
        self.assertEqual(engine.store["run_log_rows"], [])







    def test_blackbox_completion_locks_revalidates_and_commits_atomically(self) -> None:
        from scheduler.repository import complete_approved_blackbox_run
        from shared.models import PredictionRecord

        engine = _AtomicEngine()
        engine.store["run_row"]["predict_date"] = date(2026, 7, 20)
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
            with patch("scheduler.repository.load_scheme_config", return_value=cfg):
                result = complete_approved_blackbox_run(
                    engine,
                    cfg,
                    run_id=101,
                    records=[record],
                    scheme_version=cfg.scheme_version,
                    records_returned=1,
                    run_date="2026-07-20",
                    duration_sec=2.5,
                )

        self.assertEqual(result, ("success", 1, None))
        self.assertEqual(engine.begin_count, 1)
        self.assertEqual(len(engine.store["prediction_rows"]), 1)
        self.assertEqual(
            engine.store["prediction_rows"][0]["scheme_version"],
            "abc123def456",
        )
        self.assertEqual(engine.store["run_row"]["status"], "success")
        self.assertEqual(engine.store["run_row"]["records_written"], 1)
        self.assertEqual(len(engine.store["run_log_rows"]), 1)
        self.assertEqual(engine.store["run_log_rows"][0]["status"], "success")
        calls = engine.store["calls"]
        version_index = next(
            index
            for index, (sql, _rows) in enumerate(calls)
            if "FROM t_scheme_versions" in sql
        )
        registry_index = next(
            index
            for index, (sql, _rows) in enumerate(calls)
            if "FROM t_scheme_registry" in sql
        )
        run_index = next(
            index
            for index, (sql, _rows) in enumerate(calls)
            if "FROM t_scheme_runs" in sql
        )
        prediction_index = next(
            index
            for index, (sql, _rows) in enumerate(calls)
            if "INSERT INTO t_scheme_predictions" in sql
        )
        key_select_indexes = [
            index
            for index, (sql, _rows) in enumerate(calls)
            if "SELECT scheme_id, target_tenor, horizon, target_date" in sql
            and "FROM t_scheme_predictions" in sql
        ]
        self.assertIn("FOR UPDATE", calls[version_index][0])
        self.assertIn("FOR UPDATE", calls[registry_index][0])
        self.assertIn("FOR UPDATE", calls[run_index][0])
        self.assertTrue(key_select_indexes)
        self.assertTrue(
            all("FOR UPDATE" in calls[index][0] for index in key_select_indexes)
        )
        self.assertLess(version_index, registry_index)
        self.assertLess(registry_index, run_index)
        self.assertLess(run_index, key_select_indexes[0])
        self.assertLess(key_select_indexes[-1], prediction_index)




    def test_blackbox_duplicate_completion_skips_without_mutating_prediction(
        self,
    ) -> None:
        from scheduler.repository import (
            PREDICTION_KEYS_ALREADY_EXIST,
            complete_approved_blackbox_run,
        )
        from shared.models import PredictionRecord

        engine = _AtomicEngine()
        original = {
            "id": 801,
            "run_id": 77,
            "scheme_version": "blackbox-version-old",
            "scheme_id": "demo_blackbox",
            "target_tenor": "10Y",
            "horizon": 1,
            "predict_date": "2026-07-19",
            "feature_date": "2026-07-16",
            "target_date": "2026-07-21",
            "prediction_phase": "scheduled_live",
            "predicted_direction": -1,
            "confidence": 0.15,
            "model_version": "old-blackbox-model",
            "extra": '{"source":"original"}',
            "created_at": datetime(2026, 7, 19, 9, 0),
            "updated_at": datetime(2026, 7, 19, 9, 0),
        }
        engine.store["prediction_rows"] = [deepcopy(original)]
        rerun = PredictionRecord(
            scheme_id="demo_blackbox",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-20",
            target_date="2026-07-21",
            feature_date="2026-07-17",
            prediction_phase="scheduled_live",
            predicted_direction=1,
            confidence=0.98,
            model_version="new-blackbox-model",
            extra={"source": "rerun"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _set_canonical_path(_blackbox_config(), Path(tmpdir))
            with patch("scheduler.repository.load_scheme_config", return_value=cfg):
                result = complete_approved_blackbox_run(
                    engine,
                    cfg,
                    run_id=101,
                    records=[rerun],
                    scheme_version=cfg.scheme_version,
                    records_returned=1,
                    run_date="2026-07-20",
                    duration_sec=1.0,
                    precommit_validator=lambda _conn: self.fail(
                        "validator must not run for skipped predictions"
                    ),
                )

        self.assertEqual(
            result,
            ("skipped", 0, PREDICTION_KEYS_ALREADY_EXIST),
        )
        self.assertEqual(engine.store["prediction_rows"], [original])
        self.assertFalse(
            any(
                "INSERT INTO t_scheme_predictions" in sql
                for sql, _rows in engine.store["calls"]
            )
        )
        self.assertEqual(engine.store["run_row"]["status"], "skipped")
        self.assertEqual(engine.store["run_row"]["records_written"], 0)
        self.assertEqual(
            engine.store["run_row"]["error_message"],
            PREDICTION_KEYS_ALREADY_EXIST,
        )
        self.assertEqual(engine.store["run_log_rows"][0]["status"], "skipped")
        self.assertEqual(
            engine.store["run_log_rows"][0]["error_msg"],
            PREDICTION_KEYS_ALREADY_EXIST,
        )

    def test_blackbox_partial_prediction_conflict_fails_without_writes(
        self,
    ) -> None:
        from scheduler.repository import (
            PARTIAL_PREDICTION_KEY_CONFLICT,
            complete_approved_blackbox_run,
        )
        from shared.models import PredictionRecord

        engine = _AtomicEngine()
        registry_template = engine.store["registry_rows"][0]
        engine.store["registry_rows"] = [
            {
                **registry_template,
                "scheme_id": "demo_blackbox__h1__5Y",
                "target_tenor": "5Y",
            },
            deepcopy(registry_template),
        ]
        engine.store["run_row"]["records_expected"] = 2
        original = {
            "id": 801,
            "run_id": 77,
            "scheme_version": "blackbox-version-old",
            "scheme_id": "demo_blackbox",
            "target_tenor": "5Y",
            "horizon": 1,
            "predict_date": "2026-07-19",
            "feature_date": "2026-07-16",
            "target_date": "2026-07-21",
            "prediction_phase": "scheduled_live",
            "predicted_direction": -1,
            "confidence": 0.15,
            "model_version": "old-blackbox-model",
            "extra": '{"source":"original"}',
            "updated_at": datetime(2026, 7, 19, 9, 0),
        }
        engine.store["prediction_rows"] = [deepcopy(original)]
        records = [
            PredictionRecord(
                scheme_id="demo_blackbox",
                target_tenor=tenor,
                horizon=1,
                predict_date="2026-07-20",
                target_date="2026-07-21",
                feature_date="2026-07-17",
                prediction_phase="scheduled_live",
                predicted_direction=1,
            )
            for tenor in ("5Y", "10Y")
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = _set_canonical_path(
                _blackbox_config(tenors=("5Y", "10Y")),
                Path(tmpdir),
            )
            with patch("scheduler.repository.load_scheme_config", return_value=cfg):
                status, written, error_message = complete_approved_blackbox_run(
                    engine,
                    cfg,
                    run_id=101,
                    records=records,
                    scheme_version=cfg.scheme_version,
                    records_returned=2,
                    run_date="2026-07-20",
                    duration_sec=1.0,
                    precommit_validator=lambda _conn: self.fail(
                        "validator must not run for failed predictions"
                    ),
                )

        self.assertEqual((status, written), ("failed", 0))
        self.assertTrue(error_message.startswith(PARTIAL_PREDICTION_KEY_CONFLICT))
        self.assertIn("existing=", error_message)
        self.assertIn("demo_blackbox/5Y/h1/2026-07-21", error_message)
        self.assertIn("missing=", error_message)
        self.assertIn("demo_blackbox/10Y/h1/2026-07-21", error_message)
        self.assertEqual(engine.store["prediction_rows"], [original])
        self.assertFalse(
            any(
                "INSERT INTO t_scheme_predictions" in sql
                for sql, _rows in engine.store["calls"]
            )
        )
        self.assertEqual(engine.store["run_row"]["status"], "failed")
        self.assertEqual(engine.store["run_row"]["records_written"], 0)
        self.assertEqual(engine.store["run_log_rows"][0]["status"], "failed")
        self.assertEqual(
            engine.store["run_log_rows"][0]["error_msg"],
            error_message,
        )


    def test_blackbox_success_completion_rolls_back_every_stage_failure(self) -> None:
        from scheduler.repository import complete_approved_blackbox_run
        from shared.models import PredictionRecord

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
        cases = {
            "prediction": "injected prediction failure",
            "run": "injected run failure",
            "run_missing": "run update affected 0 rows",
            "log": "injected log failure",
            "live_gate_validation": "injected live_gate_validation failure",
        }
        for stage, expected_error in cases.items():
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmpdir:
                engine = _AtomicEngine(fail_stage=stage)
                cfg = _set_canonical_path(_blackbox_config(), Path(tmpdir))
                with (
                    patch("scheduler.repository.load_scheme_config", return_value=cfg),
                    self.assertRaisesRegex(RuntimeError, expected_error),
                ):
                    complete_approved_blackbox_run(
                        engine,
                        cfg,
                        run_id=101,
                        records=[record],
                        scheme_version=cfg.scheme_version,
                        records_returned=1,
                        run_date="2026-07-20",
                        duration_sec=2.5,
                        precommit_validator=(
                            (lambda _conn: (_ for _ in ()).throw(
                                RuntimeError("injected live_gate_validation failure")
                            ))
                            if stage == "live_gate_validation"
                            else None
                        ),
                    )

                self.assertEqual(engine.store["prediction_rows"], [])
                self.assertEqual(engine.store["run_row"]["status"], "running")
                self.assertIsNone(engine.store["run_row"]["records_written"])
                self.assertEqual(engine.store["run_log_rows"], [])











    def test_blackbox_completion_rejects_pending_reconciliation_before_db_access(self) -> None:
        from scheduler.repository import complete_approved_blackbox_run
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
                operation_scope_sha256="a" * 64,
            ).transition("config_written")
            write_journal(root, pending)

            with (
                patch("scheduler.repository.load_scheme_config", return_value=cfg),
                self.assertRaisesRegex(RuntimeError, "lifecycle journal"),
            ):
                complete_approved_blackbox_run(
                    engine,
                    cfg,
                    run_id=101,
                    records=[record],
                    scheme_version=cfg.scheme_version,
                    records_returned=1,
                    run_date="2026-07-20",
                    duration_sec=2.5,
                )

        self.assertEqual(engine.store["begin_count"], 0)
        self.assertEqual(engine.store["prediction_rows"], [])

    def test_blackbox_completion_rejects_revoked_approval_without_commit(self) -> None:
        from scheduler.repository import complete_approved_blackbox_run
        from shared.models import PredictionRecord

        engine = _AtomicEngine()
        engine.store["version_row"].update(
            {
                "status": "shadow",
                "approved_by": None,
                "approved_at": None,
            }
        )
        original_run = deepcopy(engine.store["run_row"])
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
                complete_approved_blackbox_run(
                    engine,
                    cfg,
                    run_id=101,
                    records=[record],
                    scheme_version="abc123def456",
                    records_returned=1,
                    run_date="2026-07-20",
                    duration_sec=2.5,
                )

        self.assertEqual(engine.begin_count, 1)
        self.assertEqual(engine.store["prediction_rows"], [])
        self.assertEqual(engine.store["run_row"], original_run)
        self.assertEqual(engine.store["run_log_rows"], [])


    def test_blackbox_completion_rejects_disk_scheme_version_drift_before_db_access(self) -> None:
        from scheduler.repository import complete_approved_blackbox_run
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
                complete_approved_blackbox_run(
                    engine,
                    cfg,
                    run_id=101,
                    records=[record],
                    scheme_version="abc123def456",
                    records_returned=1,
                    run_date="2026-07-20",
                    duration_sec=2.5,
                )

        self.assertEqual(engine.store["begin_count"], 0)
        self.assertEqual(engine.store["prediction_rows"], [])



if __name__ == "__main__":
    unittest.main()
