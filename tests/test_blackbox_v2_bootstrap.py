from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one(self):
        return self.value


class _BootstrapConnection:
    def __init__(self, *, schema: str, counts: dict[str, int]) -> None:
        self.schema = schema
        self.counts = counts
        self.calls: list[str] = []

    def execute(self, sql, params=None):
        statement = str(sql)
        self.calls.append(statement)
        if "SELECT DATABASE()" in statement:
            return _ScalarResult(self.schema)
        for table, count in self.counts.items():
            if f"FROM `{table}`" in statement:
                return _ScalarResult(count)
        raise AssertionError(f"unexpected SQL: {statement}")


class _BootstrapEngine:
    def __init__(self, *, schema: str, counts: dict[str, int]) -> None:
        self.connection = _BootstrapConnection(schema=schema, counts=counts)
        self.begin_count = 0
        self.disposed = False

    @contextmanager
    def begin(self):
        self.begin_count += 1
        yield self.connection

    def dispose(self) -> None:
        self.disposed = True


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
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.kwargs["trusted_status"], "draft")
        sync.assert_called_once()
        self.assertEqual(
            set(sync.call_args.kwargs["effective_statuses"].values()),
            {"paused"},
        )

    def test_repository_bootstrap_rejects_nonempty_schema_before_writes(self) -> None:
        from scheduler.repository import (
            BLACKBOX_BOOTSTRAP_EMPTY_TABLES,
            bootstrap_blackbox_control_plane,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._scaffold(Path(tmpdir))
            counts = {table: 0 for table in BLACKBOX_BOOTSTRAP_EMPTY_TABLES}
            counts["t_scheme_versions"] = 1
            engine = _BootstrapEngine(schema="bbv2_cert_20260720", counts=counts)
            with (
                patch("scheduler.repository._upsert_scheme_version_conn") as upsert,
                patch("scheduler.repository._sync_scheme_registry_conn") as sync,
            ):
                with self.assertRaisesRegex(RuntimeError, "must be empty"):
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
            self.assertEqual(audit["status"], "completed")

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
