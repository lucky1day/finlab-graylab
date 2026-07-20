from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext


_TARGET_REGISTRY_BASELINE = [
    {
        "target_code": "1Y",
        "display_name": "1Y国债活跃",
        "asset_class": "bond",
        "target_type": "active_treasury",
        "sort_order": 10,
        "status": "active",
        "extra": {"legacy_tenor": "1Y"},
    },
    {
        "target_code": "3Y",
        "display_name": "3Y国债活跃",
        "asset_class": "bond",
        "target_type": "active_treasury",
        "sort_order": 30,
        "status": "active",
        "extra": {"legacy_tenor": "3Y"},
    },
    {
        "target_code": "5Y",
        "display_name": "5Y国债活跃",
        "asset_class": "bond",
        "target_type": "active_treasury",
        "sort_order": 50,
        "status": "active",
        "extra": {"legacy_tenor": "5Y"},
    },
    {
        "target_code": "7Y",
        "display_name": "7Y国债活跃",
        "asset_class": "bond",
        "target_type": "active_treasury",
        "sort_order": 70,
        "status": "active",
        "extra": {"legacy_tenor": "7Y"},
    },
    {
        "target_code": "10Y",
        "display_name": "10Y国债活跃",
        "asset_class": "bond",
        "target_type": "active_treasury",
        "sort_order": 100,
        "status": "active",
        "extra": {"legacy_tenor": "10Y"},
    },
]


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one(self):
        return self.value


class _MappingResult:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return [dict(row) for row in self.rows]


class _BootstrapConnection:
    def __init__(
        self,
        *,
        schema: str,
        counts: dict[str, int],
        target_rows: list[dict] | None = None,
    ) -> None:
        self.schema = schema
        self.counts = counts
        self.target_rows = [
            {**row, "extra": dict(row["extra"])}
            for row in (target_rows if target_rows is not None else _TARGET_REGISTRY_BASELINE)
        ]
        self.calls: list[str] = []
        self.lock_acquired = False

    def execute(self, sql, params=None):
        statement = str(sql)
        self.calls.append(statement)
        if "GET_LOCK" in statement:
            self.lock_acquired = True
            return _ScalarResult(1)
        if "RELEASE_LOCK" in statement:
            self.lock_acquired = False
            return _ScalarResult(1)
        if "SELECT DATABASE()" in statement:
            return _ScalarResult(self.schema)
        if "FROM t_target_registry" in statement:
            return _MappingResult(self.target_rows)
        for table, count in self.counts.items():
            if f"FROM `{table}`" in statement:
                return _ScalarResult(count)
        raise AssertionError(f"unexpected SQL: {statement}")


class _BootstrapEngine:
    def __init__(
        self,
        *,
        schema: str,
        counts: dict[str, int],
        target_rows: list[dict] | None = None,
    ) -> None:
        self.connection = _BootstrapConnection(
            schema=schema,
            counts=counts,
            target_rows=target_rows,
        )
        self.begin_count = 0
        self.disposed = False

    @contextmanager
    def connect(self):
        yield self.connection

    @contextmanager
    def begin(self):
        self.begin_count += 1
        yield self.connection

    def dispose(self) -> None:
        self.disposed = True


class _AdvisoryConnection:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._owned = False

    def execute(self, sql, params=None):
        statement = str(sql)
        if "GET_LOCK" in statement:
            timeout = float((params or {}).get("timeout_sec", 0))
            self._owned = self._lock.acquire(timeout=timeout)
            return _ScalarResult(1 if self._owned else 0)
        if "RELEASE_LOCK" in statement:
            if self._owned:
                self._owned = False
                self._lock.release()
                return _ScalarResult(1)
            return _ScalarResult(0)
        raise AssertionError(f"unexpected advisory SQL: {statement}")


class _ConcurrentBootstrapEngine(_BootstrapEngine):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.advisory_lock = threading.Lock()

    @contextmanager
    def connect(self):
        yield _AdvisoryConnection(self.advisory_lock)


class BlackboxBootstrapTests(unittest.TestCase):
    def _scaffold(self, root: Path):
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        delivery = root / "incoming"
        delivery.mkdir()
        (delivery / "trial_10y.py").write_text("import argparse\n", encoding="utf-8")
        (delivery / "trial_10y.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "scheme_id": "trial_10y",
                    "name": "Trial",
                    "algorithm_version": "1.0.0",
                    "target_tenor": "10Y",
                    "task_type": "T+1",
                    "horizon": 1,
                    "target_rule": "target_date_yield_vs_feature_date_yield",
                }
            ),
            encoding="utf-8",
        )
        scheme_dir = intake_delivery(delivery, schemes_root=root / "schemes")
        return load_scheme_config(scheme_dir / "config.yaml")

    def test_repository_bootstrap_uses_one_transaction_and_only_draft_paused(self) -> None:
        from scheduler.repository import (
            BLACKBOX_BOOTSTRAP_EMPTY_TABLES,
            bootstrap_blackbox_control_plane,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._scaffold(Path(tmpdir))
            engine = _BootstrapEngine(
                schema="bbv2_cert_20260720",
                counts={table: 0 for table in BLACKBOX_BOOTSTRAP_EMPTY_TABLES},
            )
            version_row = {
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "runtime_type": "blackbox_v2",
                "status": "draft",
                "approved_by": None,
                "approved_at": None,
            }
            registry_rows = [
                {
                    "scheme_id": f"{cfg.scheme_id}__h1__10Y",
                    "base_scheme_id": cfg.scheme_id,
                    "runtime_type": "blackbox_v2",
                    "status": "paused",
                    "task_type": "T+1",
                    "target_tenor": "10Y",
                    "horizon": 1,
                }
            ]
            with (
                patch("scheduler.repository._upsert_scheme_version_conn") as upsert,
                patch("scheduler.repository._sync_scheme_registry_conn") as sync,
                patch("scheduler.repository._read_scheme_version_conn", return_value=version_row),
                patch(
                    "scheduler.repository._read_blackbox_registry_rows_conn",
                    return_value=registry_rows,
                ),
            ):
                state = bootstrap_blackbox_control_plane(
                    engine,
                    cfg,
                    expected_schema="bbv2_cert_20260720",
                )

        self.assertEqual(engine.begin_count, 1)
        self.assertTrue(engine.disposed is False)
        self.assertEqual(state.schema_name, "bbv2_cert_20260720")
        self.assertEqual(state.version_status, "draft")
        self.assertEqual(state.registry_status, "paused")
        self.assertEqual(state.target_registry_baseline["status"], "passed")
        self.assertEqual(state.target_registry_baseline["row_count"], 5)
        self.assertEqual(
            state.target_registry_baseline["target_codes"],
            ["1Y", "3Y", "5Y", "7Y", "10Y"],
        )
        self.assertRegex(state.target_registry_baseline["sha256"], r"^[0-9a-f]{64}$")
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.kwargs["trusted_status"], "draft")
        sync.assert_called_once()
        self.assertEqual(
            set(sync.call_args.kwargs["effective_statuses"].values()),
            {"paused"},
        )
        self.assertFalse(engine.connection.lock_acquired)
        self.assertTrue(any("GET_LOCK" in call for call in engine.connection.calls))
        self.assertTrue(any("RELEASE_LOCK" in call for call in engine.connection.calls))

    def test_concurrent_repository_bootstrap_allows_only_one_initializer(self) -> None:
        from scheduler.repository import (
            BLACKBOX_BOOTSTRAP_EMPTY_TABLES,
            BlackboxBootstrapLockTimeout,
            bootstrap_blackbox_control_plane,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._scaffold(Path(tmpdir))
            engine = _ConcurrentBootstrapEngine(
                schema="bbv2_cert_20260720",
                counts={table: 0 for table in BLACKBOX_BOOTSTRAP_EMPTY_TABLES},
            )
            first_inside = threading.Event()
            release_first = threading.Event()
            version_row = {
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "runtime_type": "blackbox_v2",
                "status": "draft",
                "approved_by": None,
                "approved_at": None,
            }
            registry_rows = [
                {
                    "scheme_id": f"{cfg.scheme_id}__h1__10Y",
                    "base_scheme_id": cfg.scheme_id,
                    "runtime_type": "blackbox_v2",
                    "status": "paused",
                    "task_type": "T+1",
                    "target_tenor": "10Y",
                    "horizon": 1,
                }
            ]

            def blocking_upsert(*_args, **_kwargs):
                first_inside.set()
                self.assertTrue(release_first.wait(timeout=2))

            with (
                patch("scheduler.repository._upsert_scheme_version_conn", side_effect=blocking_upsert),
                patch("scheduler.repository._sync_scheme_registry_conn"),
                patch("scheduler.repository._read_scheme_version_conn", return_value=version_row),
                patch(
                    "scheduler.repository._read_blackbox_registry_rows_conn",
                    return_value=registry_rows,
                ),
                ThreadPoolExecutor(max_workers=2) as pool,
            ):
                first = pool.submit(
                    bootstrap_blackbox_control_plane,
                    engine,
                    cfg,
                    expected_schema="bbv2_cert_20260720",
                    lock_timeout_sec=1,
                )
                self.assertTrue(first_inside.wait(timeout=1))
                second = pool.submit(
                    bootstrap_blackbox_control_plane,
                    engine,
                    cfg,
                    expected_schema="bbv2_cert_20260720",
                    lock_timeout_sec=0.05,
                )
                with self.assertRaises(BlackboxBootstrapLockTimeout):
                    second.result(timeout=1)
                release_first.set()
                self.assertEqual(first.result(timeout=2).version_status, "draft")

        self.assertFalse(engine.advisory_lock.locked())

    def test_bootstrap_releases_advisory_lock_after_preflight_failure(self) -> None:
        from scheduler.repository import (
            BLACKBOX_BOOTSTRAP_EMPTY_TABLES,
            bootstrap_blackbox_control_plane,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._scaffold(Path(tmpdir))
            counts = {table: 0 for table in BLACKBOX_BOOTSTRAP_EMPTY_TABLES}
            counts["t_scheme_runs"] = 1
            engine = _ConcurrentBootstrapEngine(
                schema="bbv2_cert_20260720",
                counts=counts,
            )
            with self.assertRaisesRegex(RuntimeError, "t_scheme_runs"):
                bootstrap_blackbox_control_plane(
                    engine,
                    cfg,
                    expected_schema="bbv2_cert_20260720",
                    lock_timeout_sec=0.1,
                )

        self.assertFalse(engine.advisory_lock.locked())

    def test_repository_bootstrap_rejects_each_nonempty_writable_table_before_writes(self) -> None:
        from scheduler import repository

        bootstrap_blackbox_control_plane = repository.bootstrap_blackbox_control_plane
        empty_tables = set(repository.BLACKBOX_BOOTSTRAP_EMPTY_TABLES)
        baseline_tables = set(
            getattr(repository, "BLACKBOX_BOOTSTRAP_BASELINE_TABLES", ())
        )
        guarded_tables = set(
            getattr(repository, "BLACKBOX_BOOTSTRAP_GUARDED_TABLES", ())
        )
        migration_tables: set[str] = set()
        migration_pattern = re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(t_[A-Za-z0-9_]+)",
            re.IGNORECASE,
        )
        for migration in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            migration_tables.update(
                migration_pattern.findall(migration.read_text(encoding="utf-8"))
            )

        self.assertEqual(empty_tables & baseline_tables, set())
        self.assertEqual(guarded_tables, empty_tables | baseline_tables)
        self.assertEqual(guarded_tables, migration_tables)
        target_insert_pattern = re.compile(
            r"\('([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)',\s*"
            r"(\d+),\s*'([^']+)',\s*JSON_OBJECT\('legacy_tenor',\s*'([^']+)'\)\)"
        )
        target_migrations = "\n".join(
            (Path(__file__).parents[1] / "migrations" / name).read_text(encoding="utf-8")
            for name in ("003_target_registry.sql", "013_add_1y_target_registry.sql")
        )
        migrated_target_rows = [
            {
                "target_code": target_code,
                "display_name": display_name,
                "asset_class": asset_class,
                "target_type": target_type,
                "sort_order": int(sort_order),
                "status": status,
                "extra": {"legacy_tenor": legacy_tenor},
            }
            for (
                target_code,
                display_name,
                asset_class,
                target_type,
                sort_order,
                status,
                legacy_tenor,
            ) in target_insert_pattern.findall(target_migrations)
        ]
        self.assertEqual(
            sorted(migrated_target_rows, key=lambda row: row["sort_order"]),
            _TARGET_REGISTRY_BASELINE,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._scaffold(Path(tmpdir))
            for nonempty_table in repository.BLACKBOX_BOOTSTRAP_EMPTY_TABLES:
                with self.subTest(table=nonempty_table):
                    counts = {
                        table: 0 for table in repository.BLACKBOX_BOOTSTRAP_EMPTY_TABLES
                    }
                    counts[nonempty_table] = 1
                    engine = _BootstrapEngine(schema="bbv2_cert_20260720", counts=counts)
                    with (
                        patch("scheduler.repository._upsert_scheme_version_conn") as upsert,
                        patch("scheduler.repository._sync_scheme_registry_conn") as sync,
                    ):
                        with self.assertRaisesRegex(RuntimeError, nonempty_table):
                            bootstrap_blackbox_control_plane(
                                engine,
                                cfg,
                                expected_schema="bbv2_cert_20260720",
                            )

                    upsert.assert_not_called()
                    sync.assert_not_called()

    def test_repository_bootstrap_rejects_any_target_registry_baseline_drift(self) -> None:
        from scheduler.repository import (
            BLACKBOX_BOOTSTRAP_EMPTY_TABLES,
            bootstrap_blackbox_control_plane,
        )

        def changed_rows(field: str, value) -> list[dict]:
            rows = [{**row, "extra": dict(row["extra"])} for row in _TARGET_REGISTRY_BASELINE]
            rows[2][field] = value
            return rows

        cases = {
            "extra_target": _TARGET_REGISTRY_BASELINE
            + [
                {
                    "target_code": "30Y",
                    "display_name": "30Y国债活跃",
                    "asset_class": "bond",
                    "target_type": "active_treasury",
                    "sort_order": 300,
                    "status": "active",
                    "extra": {"legacy_tenor": "30Y"},
                }
            ],
            "missing_target": _TARGET_REGISTRY_BASELINE[:-1],
            "display_name": changed_rows("display_name", "错误名称"),
            "asset_class": changed_rows("asset_class", "other"),
            "target_type": changed_rows("target_type", "other"),
            "sort_order": changed_rows("sort_order", 51),
            "status": changed_rows("status", "paused"),
            "extra": changed_rows("extra", {"legacy_tenor": "WRONG"}),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._scaffold(Path(tmpdir))
            version_row = {
                "scheme_id": cfg.scheme_id,
                "scheme_version": cfg.scheme_version,
                "runtime_type": "blackbox_v2",
                "status": "draft",
                "approved_by": None,
                "approved_at": None,
            }
            registry_rows = [
                {
                    "scheme_id": f"{cfg.scheme_id}__h1__10Y",
                    "base_scheme_id": cfg.scheme_id,
                    "runtime_type": "blackbox_v2",
                    "status": "paused",
                    "task_type": "T+1",
                    "target_tenor": "10Y",
                    "horizon": 1,
                }
            ]
            for case, target_rows in cases.items():
                with self.subTest(case=case):
                    engine = _BootstrapEngine(
                        schema="bbv2_cert_20260720",
                        counts={table: 0 for table in BLACKBOX_BOOTSTRAP_EMPTY_TABLES},
                        target_rows=target_rows,
                    )
                    with (
                        patch("scheduler.repository._upsert_scheme_version_conn") as upsert,
                        patch("scheduler.repository._sync_scheme_registry_conn") as sync,
                        patch(
                            "scheduler.repository._read_scheme_version_conn",
                            return_value=version_row,
                        ),
                        patch(
                            "scheduler.repository._read_blackbox_registry_rows_conn",
                            return_value=registry_rows,
                        ),
                        self.assertRaisesRegex(
                            RuntimeError,
                            "target_registry baseline mismatch",
                        ),
                    ):
                        bootstrap_blackbox_control_plane(
                            engine,
                            cfg,
                            expected_schema="bbv2_cert_20260720",
                        )

                    upsert.assert_not_called()
                    sync.assert_not_called()

    def test_bootstrap_gate_writes_audit_and_exposes_no_activation_control(self) -> None:
        from harness.blackbox_v2.bootstrap import BlackboxBootstrapGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            engine = _BootstrapEngine(schema="bbv2_cert_20260720", counts={})
            ctx = GateContext(
                cfg.scheme_id,
                "bootstrap",
                root,
                root / "reports" / "bootstrap",
                config=cfg,
                engine_factory=lambda: engine,
                expected_empty_schema="bbv2_cert_20260720",
            )
            state = SimpleNamespace(
                schema_name="bbv2_cert_20260720",
                scheme_id=cfg.scheme_id,
                scheme_version=cfg.scheme_version,
                version_status="draft",
                registry_status="paused",
                table_counts={"t_scheme_versions": 0},
                target_registry_baseline={
                    "status": "passed",
                    "baseline_version": "migrations-003-013",
                    "row_count": 5,
                    "target_codes": ["1Y", "3Y", "5Y", "7Y", "10Y"],
                    "sha256": "a" * 64,
                },
            )
            with patch(
                "harness.blackbox_v2.bootstrap.bootstrap_blackbox_control_plane",
                return_value=state,
            ):
                result = BlackboxBootstrapGate().run(ctx)
            self.assertTrue(result.passed, result.errors)
            self.assertTrue(result.report_path and result.report_path.is_file())
            audit = json.loads(result.report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                audit["target_state"],
                {"version": "draft", "registry": "paused"},
            )
            self.assertFalse(audit["activation_performed"])
            self.assertFalse(audit["business_writes_performed"])
            self.assertEqual(audit["target_registry_baseline"]["status"], "passed")
            target_evidence = next(
                item.value
                for item in result.evidence
                if item.key == "target_registry_baseline"
            )
            self.assertEqual(target_evidence["sha256"], "a" * 64)
            self.assertEqual(audit["status"], "completed")

    def test_bootstrap_gate_persists_failed_target_baseline_evidence(self) -> None:
        from harness.blackbox_v2.bootstrap import BlackboxBootstrapGate
        from scheduler.repository import BLACKBOX_BOOTSTRAP_EMPTY_TABLES

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            drifted_targets = [
                {**row, "extra": dict(row["extra"])}
                for row in _TARGET_REGISTRY_BASELINE
            ]
            drifted_targets[0]["status"] = "paused"
            engine = _BootstrapEngine(
                schema="bbv2_cert_20260720",
                counts={table: 0 for table in BLACKBOX_BOOTSTRAP_EMPTY_TABLES},
                target_rows=drifted_targets,
            )
            ctx = GateContext(
                cfg.scheme_id,
                "bootstrap",
                root,
                root / "reports" / "bootstrap",
                config=cfg,
                engine_factory=lambda: engine,
                expected_empty_schema="bbv2_cert_20260720",
            )

            result = BlackboxBootstrapGate().run(ctx)

            self.assertFalse(result.passed)
            evidence = {item.key: item.value for item in result.evidence}
            self.assertEqual(evidence["target_registry_baseline"]["status"], "failed")
            self.assertEqual(
                evidence["target_registry_baseline"]["changed"]["1Y"]["status"],
                {"expected": "active", "actual": "paused"},
            )
            audit_path = ctx.report_dir / "bootstrap_result.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit["status"], "failed")
            self.assertEqual(audit["target_registry_baseline"], evidence["target_registry_baseline"])
            self.assertFalse(audit["business_writes_performed"])

    def test_bootstrap_keeps_prepared_audit_if_completion_write_fails(self) -> None:
        from harness.blackbox_v2 import bootstrap as bootstrap_module
        from harness.blackbox_v2.bootstrap import BlackboxBootstrapGate

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = self._scaffold(root)
            engine = _BootstrapEngine(schema="bbv2_cert_20260720", counts={})
            ctx = GateContext(
                cfg.scheme_id,
                "bootstrap",
                root,
                root / "reports" / "bootstrap",
                config=cfg,
                engine_factory=lambda: engine,
                expected_empty_schema="bbv2_cert_20260720",
            )
            state = SimpleNamespace(
                schema_name="bbv2_cert_20260720",
                scheme_id=cfg.scheme_id,
                scheme_version=cfg.scheme_version,
                version_status="draft",
                registry_status="paused",
                table_counts={"t_scheme_versions": 0},
                target_registry_baseline={
                    "status": "passed",
                    "baseline_version": "migrations-003-013",
                    "row_count": 5,
                    "target_codes": ["1Y", "3Y", "5Y", "7Y", "10Y"],
                    "sha256": "a" * 64,
                },
            )
            real_write = bootstrap_module._atomic_write_json
            writes = 0

            def fail_completion(path, payload):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("injected completion audit failure")
                real_write(path, payload)

            with (
                patch(
                    "harness.blackbox_v2.bootstrap.bootstrap_blackbox_control_plane",
                    return_value=state,
                ),
                patch(
                    "harness.blackbox_v2.bootstrap._atomic_write_json",
                    side_effect=fail_completion,
                ),
            ):
                result = BlackboxBootstrapGate().run(ctx)

            audit_path = ctx.report_dir / "bootstrap_result.json"
            self.assertFalse(result.passed)
            self.assertTrue(audit_path.is_file())
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit["status"], "prepared")
            self.assertEqual(
                audit["target_state"],
                {"version": "draft", "registry": "paused"},
            )

    def test_bootstrap_cli_requires_explicit_expected_empty_schema(self) -> None:
        from harness.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            [
                "gate",
                "bootstrap",
                "--scheme-id",
                "trial_10y",
                "--expected-empty-schema",
                "bbv2_cert_20260720",
            ]
        )

        self.assertEqual(args.expected_empty_schema, "bbv2_cert_20260720")


if __name__ == "__main__":
    unittest.main()
