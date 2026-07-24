from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _Scalar:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value


class _Connection:
    def __init__(self, engine: "_Engine") -> None:
        self._engine = engine

    def execute(self, sql):
        statement = str(sql).strip()
        self._engine.queries.append(statement)
        if self._engine.fail_health_check and statement.upper() == "SELECT 1":
            raise RuntimeError("database unavailable")
        if statement.upper() == "SELECT 1":
            return _Scalar(1)
        return _Scalar(self._engine._schema)


class _Engine:
    def __init__(
        self,
        *,
        schema: str,
        host: str = "127.0.0.1",
        fail_health_check: bool = False,
    ) -> None:
        self._schema = schema
        self.fail_health_check = fail_health_check
        self.queries: list[str] = []
        self.url = SimpleNamespace(
            get_backend_name=lambda: "mysql",
            host=host,
            port=3306,
            database=schema,
            username="secret-user",
            password="secret-password",
        )

    @contextmanager
    def connect(self):
        yield _Connection(self)


class ServiceInstanceIdentityTests(unittest.TestCase):
    def test_fingerprint_binds_database_commit_profile_and_nonce_without_leaking_raw_values(self) -> None:
        from shared.service_instance import build_service_instance_identity

        root = Path("/tmp/test-project")
        fingerprint_secret = "service-fingerprint-secret-for-tests"
        with patch("shared.service_instance._git_commit", return_value="a" * 40):
            baseline = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=root,
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-one",
                fingerprint_secret=fingerprint_secret,
            )
            wrong_db = build_service_instance_identity(
                _Engine(schema="bbv2_cert_two"),
                project_root=root,
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-one",
                fingerprint_secret=fingerprint_secret,
            )
            wrong_profile = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=root,
                runtime_profile="other-profile",
                instance_nonce="instance-secret-one",
                fingerprint_secret=fingerprint_secret,
            )
            wrong_nonce = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=root,
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-two",
                fingerprint_secret=fingerprint_secret,
            )
        with patch("shared.service_instance._git_commit", return_value="b" * 40):
            wrong_commit = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=root,
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-one",
                fingerprint_secret=fingerprint_secret,
            )

        fingerprints = {
            item["fingerprint"]
            for item in (baseline, wrong_db, wrong_profile, wrong_nonce, wrong_commit)
        }
        self.assertEqual(len(fingerprints), 5)
        self.assertEqual(set(baseline), {"fingerprint_version", "fingerprint"})
        self.assertEqual(baseline["fingerprint_version"], "2")
        serialized = json.dumps(baseline, sort_keys=True)
        for secret in (
            "bbv2_cert_one",
            "instance-secret-one",
            fingerprint_secret,
            "secret-user",
            "secret-password",
        ):
            self.assertNotIn(secret, serialized)

    def test_fingerprint_is_authenticated_and_changes_with_service_secret(self) -> None:
        from shared.service_instance import build_service_instance_identity

        with patch("shared.service_instance._git_commit", return_value="a" * 40):
            first = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=Path("/tmp/test-project"),
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-one",
                fingerprint_secret="first-service-secret-for-tests",
            )
            second = build_service_instance_identity(
                _Engine(schema="bbv2_cert_one"),
                project_root=Path("/tmp/test-project"),
                runtime_profile="blackbox-v2-v1",
                instance_nonce="instance-secret-one",
                fingerprint_secret="second-service-secret-for-tests",
            )

        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_health_returns_safe_service_identity(self) -> None:
        from backend import main

        engine = _Engine(schema="bbv2_cert_one")
        identity = {
            "fingerprint_version": "2",
            "fingerprint": "f" * 64,
        }
        original_dashboard_health = main._dashboard_health_snapshot()
        main._reset_dashboard_health_for_tests("ready", None)
        try:
            with (
                patch.object(main, "get_engine", return_value=engine),
                patch.object(
                    main,
                    "build_service_instance_identity",
                    return_value=identity,
                ) as build,
                patch.dict(
                    "os.environ",
                    {
                        "BOND_FACTOR_LAB_RUNTIME_PROFILE": "blackbox-v2-v1",
                        "BOND_FACTOR_LAB_INSTANCE_NONCE": "explicit-instance",
                        "HARNESS_AUTH_SECRET": (
                            "shared-formal-gate-secret-for-tests"
                        ),
                    },
                ),
            ):
                result = main.health()
        finally:
            main._reset_dashboard_health_for_tests(
                original_dashboard_health["status"],
                original_dashboard_health["error_code"],
            )

        self.assertEqual(
            result,
            {
                "status": "ok",
                "service_instance": identity,
                "dashboard_snapshot": {"status": "ready", "error_code": None},
                "daily_schedule": {
                    "mode": "legacy",
                    "overall": "not_enabled",
                    "reasons": ["LEDGER_ROLLOUT_DISABLED"],
                },
            },
        )
        self.assertEqual(engine.queries[0].upper(), "SELECT 1")
        self.assertEqual(build.call_args.kwargs["instance_nonce"], "explicit-instance")
        self.assertEqual(
            build.call_args.kwargs["fingerprint_secret"],
            "shared-formal-gate-secret-for-tests",
        )

    def test_health_without_fingerprint_secret_exposes_no_enumerable_components(self) -> None:
        from backend import main

        engine = _Engine(schema="native_service")
        original_dashboard_health = main._dashboard_health_snapshot()
        main._reset_dashboard_health_for_tests("ready", None)
        try:
            with (
                patch.object(main, "get_engine", return_value=engine),
                patch.dict("os.environ", {}, clear=True),
            ):
                result = main.health()
        finally:
            main._reset_dashboard_health_for_tests(
                original_dashboard_health["status"],
                original_dashboard_health["error_code"],
            )

        self.assertEqual(
            result,
            {
                "status": "ok",
                "service_instance": {"fingerprint_version": "2", "fingerprint": None},
                "dashboard_snapshot": {"status": "ready", "error_code": None},
                "daily_schedule": {
                    "mode": "legacy",
                    "overall": "not_enabled",
                    "reasons": ["LEDGER_ROLLOUT_DISABLED"],
                },
            },
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("database_identity_sha256", serialized)
        self.assertNotIn("nonce_sha256", serialized)
        self.assertEqual(engine.queries, ["SELECT 1"])

    def test_health_dashboard_degraded_uses_stable_code_without_internal_error(self) -> None:
        from backend import main

        engine = _Engine(schema="native_service")
        original_dashboard_health = main._dashboard_health_snapshot()
        main._reset_dashboard_health_for_tests(
            "degraded",
            "dashboard_snapshot_unavailable",
        )
        try:
            with (
                patch.object(main, "get_engine", return_value=engine),
                patch.dict("os.environ", {}, clear=True),
            ):
                result = main.health()
        finally:
            main._reset_dashboard_health_for_tests(
                original_dashboard_health["status"],
                original_dashboard_health["error_code"],
            )

        self.assertEqual(
            result["dashboard_snapshot"],
            {
                "status": "degraded",
                "error_code": "dashboard_snapshot_unavailable",
            },
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("mysql", serialized.casefold())
        self.assertNotIn("traceback", serialized.casefold())

    def test_health_without_fingerprint_secret_still_fails_when_database_is_unavailable(self) -> None:
        from backend import main

        engine = _Engine(schema="native_service", fail_health_check=True)
        with (
            patch.object(main, "get_engine", return_value=engine),
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "database unavailable"),
        ):
            main.health()

        self.assertEqual(engine.queries, ["SELECT 1"])

    def test_health_ledger_mode_projects_frozen_occurrence_and_surfaces_error(
        self,
    ) -> None:
        from types import SimpleNamespace

        from backend import main

        engine = _Engine(schema="native_service")
        epoch_identity = {
            "epoch": 7,
            "mode": "ledger",
            "record_sha256": "e" * 64,
        }
        checked_at = datetime(
            2026,
            7,
            24,
            0,
            0,
            tzinfo=timezone.utc,
        )
        heartbeat = SimpleNamespace(
            service_name="daily-coordinator",
            state="RUNNING",
            occurrence_id=91,
            heartbeat_at=checked_at - timedelta(seconds=15),
            details={
                "business_date": "2026-07-24",
                "coordinator_mode": "ledger",
                "daily_coordinator_epoch": dict(epoch_identity),
            },
        )
        snapshot = SimpleNamespace(
            occurrence=SimpleNamespace(
                predict_date="2026-07-24",
                policy_json={
                    "daily_coordinator_epoch": dict(epoch_identity),
                },
            ),
            items=(
                SimpleNamespace(item=SimpleNamespace(item_id=11)),
                SimpleNamespace(item=SimpleNamespace(item_id=12)),
            )
        )
        projected = {
            "overall": "error",
            "reasons": ["DATABRIDGE_READY_V2_INCOMPLETE"],
        }
        with (
            patch.object(main, "get_engine", return_value=engine),
            patch.object(
                main,
                "_daily_coordinator_mode",
                return_value="ledger",
            ),
            patch.object(
                main,
                "require_current_daily_coordinator_identity",
                return_value=SimpleNamespace(
                    mode="ledger",
                    policy_payload=lambda: dict(epoch_identity),
                ),
            ),
            patch.object(
                main,
                "read_scheduler_heartbeat",
                return_value=heartbeat,
            ),
            patch.object(
                main,
                "read_schedule_occurrence_snapshot",
                return_value=snapshot,
            ),
            patch.object(
                main,
                "read_schedule_health_envelope",
                side_effect=("envelope-11", "envelope-12"),
            ) as read_envelope,
            patch.object(
                main,
                "project_daily_health",
                return_value=projected,
            ) as project,
            patch.object(
                main,
                "datetime",
                MagicMock(
                    wraps=datetime,
                    **{"now.return_value": checked_at},
                ),
            ),
            patch.dict("os.environ", {}, clear=True),
        ):
            result = main.health()

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["daily_schedule"],
            {"mode": "ledger", **projected},
        )
        self.assertEqual(
            [call.kwargs["item_id"] for call in read_envelope.call_args_list],
            [11, 12],
        )
        self.assertEqual(
            project.call_args.kwargs["execution_envelopes"],
            ("envelope-11", "envelope-12"),
        )
