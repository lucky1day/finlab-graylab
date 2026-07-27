from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext


class BlackboxDraftRegisterGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_secret = os.environ.get("HARNESS_AUTH_SECRET")
        os.environ["HARNESS_AUTH_SECRET"] = "draft-register-test-secret"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = self._scaffold()

    def tearDown(self) -> None:
        if self._previous_secret is None:
            os.environ.pop("HARNESS_AUTH_SECRET", None)
        else:
            os.environ["HARNESS_AUTH_SECRET"] = self._previous_secret
        self._tmp.cleanup()

    def _scaffold(self):
        from scheduler.discovery import load_scheme_config
        from shared.blackbox_v2.intake import intake_delivery

        delivery = self.root / "incoming"
        delivery.mkdir()
        (delivery / "trial_10y.py").write_text("import argparse\n", encoding="utf-8")
        (delivery / "trial_10y.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "scheme_id": "trial_10y",
                    "name": "Trial",
                    "description": "test delivery",
                    "algorithm_version": "1.0.0",
                    "target_tenor": "10Y",
                    "task_type": "T+1",
                    "horizon": 1,
                    "target_rule": "target_date_yield_vs_feature_date_yield",
                }
            ),
            encoding="utf-8",
        )
        scheme_dir = intake_delivery(delivery, schemes_root=self.root / "schemes")
        return load_scheme_config(scheme_dir / "config.yaml")

    def _passed_run(self):
        return SimpleNamespace(
            harness_run_id="hr_passed",
            predict_date="2026-07-20",
            data_snapshot_id="snapshot-1",
            environment_fingerprint="e" * 64,
        )

    def _token(
        self,
        *,
        scheme_version: str | None = None,
        harness_run_id: str = "hr_passed",
        issued_by: str = "onboarding-owner",
    ) -> str:
        from harness.authorization import issue_token

        return issue_token(
            self.cfg.scheme_id,
            "draft_register",
            predict_date="2026-07-20",
            scheme_version=scheme_version or self.cfg.scheme_version,
            harness_run_id=harness_run_id,
            ttl_seconds=60,
            issued_by=issued_by,
        )

    def _ctx(self, token: str | None) -> GateContext:
        return GateContext(
            scheme_id=self.cfg.scheme_id,
            predict_date="2026-07-20",
            project_root=self.root,
            report_dir=self.root / "reports" / "draft-register",
            config=self.cfg,
            authorization=token,
            engine_factory=lambda: SimpleNamespace(dispose=lambda: None),
        )

    def test_requires_token_before_database_use(self) -> None:
        from harness.blackbox_v2.draft_register import BlackboxDraftRegisterGate

        result = BlackboxDraftRegisterGate().run(self._ctx(None))

        self.assertFalse(result.passed)
        self.assertIn("original signed token", "\n".join(result.errors))

    def test_rejects_unsigned_authorization(self) -> None:
        from harness.authorization import issue_token
        from harness.blackbox_v2.draft_register import BlackboxDraftRegisterGate

        os.environ.pop("HARNESS_AUTH_SECRET", None)
        unsigned = issue_token(
            self.cfg.scheme_id,
            "draft_register",
            predict_date="2026-07-20",
            scheme_version=self.cfg.scheme_version,
            harness_run_id="hr_passed",
            ttl_seconds=60,
            issued_by="onboarding-owner",
        )
        os.environ["HARNESS_AUTH_SECRET"] = "draft-register-test-secret"

        result = BlackboxDraftRegisterGate().run(self._ctx(unsigned))

        self.assertFalse(result.passed)
        self.assertIn("signing mode", "\n".join(result.errors))

    def test_gate_rejects_non_string_operator_before_database_use(self) -> None:
        from harness.blackbox_v2.draft_register import BlackboxDraftRegisterGate

        malformed_auth = SimpleNamespace(
            issued_at="2026-07-20T00:00:00+00:00",
            expires_at="2026-07-20T00:01:00+00:00",
            issued_by=None,
            scheme_version=self.cfg.scheme_version,
        )
        with (
            patch(
                "harness.blackbox_v2.draft_register.verify_authorization",
                return_value=(malformed_auth, []),
            ),
            patch(
                "harness.blackbox_v2.draft_register.required_future_expiry_errors",
                return_value=[],
            ),
        ):
            result = BlackboxDraftRegisterGate().run(self._ctx("signed-token"))

        self.assertFalse(result.passed)
        self.assertEqual(result.status.value, "blocked")
        self.assertIn("non-empty string", "\n".join(result.errors))

    def test_rejects_wrong_version_and_latest_run(self) -> None:
        from harness.blackbox_v2.draft_register import BlackboxDraftRegisterGate

        for token in (
            self._token(scheme_version="wrong-version"),
            self._token(harness_run_id="hr_old"),
        ):
            with self.subTest(token=token[-12:]), patch(
                "harness.blackbox_v2.draft_register._verify_passed_all",
                return_value=self._passed_run(),
            ):
                result = BlackboxDraftRegisterGate().run(self._ctx(token))
            self.assertFalse(result.passed)

    def test_rejects_wrong_latest_predict_date_and_overlong_token(self) -> None:
        from harness.blackbox_v2.draft_register import BlackboxDraftRegisterGate

        cases = (
            (
                self._token(),
                SimpleNamespace(
                    **{
                        **self._passed_run().__dict__,
                        "predict_date": "2026-07-21",
                    }
                ),
            ),
            (
                __import__(
                    "harness.authorization",
                    fromlist=["issue_token"],
                ).issue_token(
                    self.cfg.scheme_id,
                    "draft_register",
                    predict_date="2026-07-20",
                    scheme_version=self.cfg.scheme_version,
                    harness_run_id="hr_passed",
                    ttl_seconds=901,
                    issued_by="onboarding-owner",
                ),
                self._passed_run(),
            ),
        )
        for token, passed in cases:
            with self.subTest(token=token[-12:]), patch(
                "harness.blackbox_v2.draft_register._verify_passed_all",
                return_value=passed,
            ):
                result = BlackboxDraftRegisterGate().run(self._ctx(token))
            self.assertFalse(result.passed)

    def test_success_uses_passed_evidence_and_reports_no_business_or_activation_write(self) -> None:
        from harness.blackbox_v2.draft_register import BlackboxDraftRegisterGate
        from scheduler.repository import BlackboxLifecycleState

        state = BlackboxLifecycleState(
            scheme_id=self.cfg.scheme_id,
            scheme_version=self.cfg.scheme_version,
            runtime_type="blackbox_v2",
            version_status="draft",
            registry_status="paused",
            environment_fingerprint="e" * 64,
            data_snapshot_id="snapshot-1",
            code_hash=self.cfg.code_hash,
            config_hash=self.cfg.config_hash,
            manifest_hash=self.cfg.manifest_hash,
            approved_by=None,
            approved_at=None,
            registry_scheme_ids=(f"{self.cfg.scheme_id}__h1__10Y",),
        )
        token = self._token()
        config_before = (self.cfg.path / "config.yaml").read_bytes()
        with (
            patch(
                "harness.blackbox_v2.draft_register._verify_passed_all",
                return_value=self._passed_run(),
            ),
            patch(
                "harness.blackbox_v2.draft_register.register_blackbox_draft_identity",
                return_value=state,
            ) as register,
        ):
            result = BlackboxDraftRegisterGate().run(self._ctx(token))

        self.assertTrue(result.passed, result.errors)
        enriched = register.call_args.args[1]
        self.assertEqual(enriched.environment_fingerprint, "e" * 64)
        self.assertEqual(enriched.data_snapshot_id, "snapshot-1")
        self.assertEqual(
            register.call_args.kwargs["expected_harness_run_id"],
            "hr_passed",
        )
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["action"], "draft_register")
        self.assertEqual(evidence["before"]["identity_exists"], False)
        self.assertEqual(evidence["after"]["version_status"], "draft")
        self.assertFalse(evidence["business_tables_written"])
        self.assertFalse(evidence["activation_performed"])
        self.assertEqual((self.cfg.path / "config.yaml").read_bytes(), config_before)

    def test_repository_identity_conflict_fails_closed(self) -> None:
        from harness.blackbox_v2.draft_register import BlackboxDraftRegisterGate

        with (
            patch(
                "harness.blackbox_v2.draft_register._verify_passed_all",
                return_value=self._passed_run(),
            ),
            patch(
                "harness.blackbox_v2.draft_register.register_blackbox_draft_identity",
                side_effect=ValueError("draft registration identity conflict"),
            ),
        ):
            result = BlackboxDraftRegisterGate().run(self._ctx(self._token()))

        self.assertFalse(result.passed)
        self.assertIn("identity conflict", "\n".join(result.errors))


class _Result:
    def __init__(self, *, scalar=None, rows=None, row=None) -> None:
        self._scalar = scalar
        self._rows = [] if rows is None else rows
        self._row = row

    def scalar_one(self):
        return self._scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one_or_none(self):
        return self._row


class _Context:
    def __init__(self, value) -> None:
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


class _DraftRegisterEngine:
    def __init__(
        self,
        *,
        latest_harness_run_id="hr_passed",
        version_conflicts=None,
        fail_registry_insert=False,
        version_readback_overrides=None,
        registry_readback_overrides=None,
    ) -> None:
        self.latest_harness_run_id = latest_harness_run_id
        self.version_conflicts = version_conflicts or []
        self.fail_registry_insert = fail_registry_insert
        self.version_readback_overrides = version_readback_overrides or {}
        self.registry_readback_overrides = registry_readback_overrides or {}
        self.version = None
        self.registry = []
        self.sql: list[str] = []
        self.released = False
        self.begin_count = 0
        self.lock_connection = SimpleNamespace(execute=self._lock_execute)

    def connect(self):
        return _Context(self.lock_connection)

    def begin(self):
        engine = self

        class Transaction:
            def __enter__(self):
                engine.begin_count += 1
                self.before = (engine.version, list(engine.registry))
                return SimpleNamespace(execute=engine._execute)

            def __exit__(self, exc_type, *_args):
                if exc_type is not None:
                    engine.version, engine.registry = self.before
                return False

        return Transaction()

    def _lock_execute(self, statement, _params=None):
        sql = str(statement)
        self.sql.append(sql)
        if "RELEASE_LOCK" in sql:
            self.released = True
        return _Result(scalar=1)

    def _execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.sql.append(sql)
        if "draft registration latest passed all-stage fence" in sql:
            row = (
                None
                if self.latest_harness_run_id is None
                else {"harness_run_id": self.latest_harness_run_id}
            )
            return _Result(row=row)
        if "draft registration version identity conflicts" in sql:
            return _Result(rows=self.version_conflicts)
        if "draft registration Registry identity conflicts" in sql:
            return _Result(rows=[])
        if sql.startswith("INSERT INTO t_scheme_versions"):
            self.version = dict(params)
            return _Result()
        if sql.startswith("INSERT INTO t_scheme_registry"):
            if self.fail_registry_insert:
                raise RuntimeError("registry insert failed")
            rows = params if isinstance(params, list) else [params]
            self.registry = [dict(row) for row in rows]
            return _Result()
        if "FROM t_scheme_versions" in sql:
            if self.version is None:
                return _Result(row=None)
            row = dict(self.version)
            row["approved_by"] = None
            row["approved_at"] = None
            row.update(self.version_readback_overrides)
            return _Result(row=row)
        if "FROM t_scheme_registry" in sql:
            rows = []
            for stored in self.registry:
                row = dict(stored)
                row.update(self.registry_readback_overrides)
                rows.append(row)
            return _Result(rows=rows)
        raise AssertionError(sql)


class BlackboxDraftRegisterRepositoryTests(unittest.TestCase):
    def _cfg(self):
        from scheduler.discovery import SchemeConfig, SchemeSchedule

        return SchemeConfig(
            scheme_id="trial_10y",
            name="Trial",
            description="test delivery",
            horizon=1,
            task_type="T+1",
            tenors=["10Y"],
            frequency="daily",
            schedule=SchemeSchedule("0 7 * * 1-5"),
            entry_point="blackbox_v2",
            status="paused",
            path=Path("/tmp/trial_10y"),
            code_hash="c" * 64,
            config_hash="f" * 64,
            manifest_hash="m" * 64,
            scheme_version="v1",
            runtime_type="blackbox_v2",
            version_status="draft",
            algorithm_version="1.0.0",
            contract_version="1.0",
            runtime_profile="blackbox-v2-v1",
            data_schema_version="data-bridge-v1",
            target_rule="target_date_yield_vs_feature_date_yield",
            delivery_script=None,
            delivery_metadata=None,
            environment_fingerprint="e" * 64,
            data_snapshot_id="snapshot-1",
        )

    def test_exact_absent_identity_is_created_in_one_transaction_and_read_back(self) -> None:
        from scheduler.repository import register_blackbox_draft_identity

        engine = _DraftRegisterEngine()
        state = register_blackbox_draft_identity(
            engine,
            self._cfg(),
            expected_harness_run_id="hr_passed",
        )

        self.assertEqual(engine.begin_count, 1)
        self.assertTrue(engine.released)
        self.assertEqual(state.version_status, "draft")
        self.assertEqual(state.registry_status, "paused")
        self.assertEqual(state.registry_scheme_ids, ("trial_10y__h1__10Y",))

    def test_existing_identity_is_rejected_without_insert(self) -> None:
        from scheduler.repository import register_blackbox_draft_identity

        engine = _DraftRegisterEngine(
            version_conflicts=[{"scheme_id": "trial_10y", "scheme_version": "old"}]
        )
        with self.assertRaisesRegex(ValueError, "identity conflict"):
            register_blackbox_draft_identity(
                engine,
                self._cfg(),
                expected_harness_run_id="hr_passed",
            )

        self.assertIsNone(engine.version)
        self.assertEqual(engine.registry, [])
        self.assertTrue(engine.released)

    def test_version_readback_rejects_creator_and_git_commit_tampering(self) -> None:
        from scheduler.repository import register_blackbox_draft_identity

        cases = (
            {"created_by": "unexpected-writer"},
            {"git_commit": "unexpected-commit"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                engine = _DraftRegisterEngine(
                    version_readback_overrides=overrides,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "version readback mismatch",
                ):
                    register_blackbox_draft_identity(
                        engine,
                        self._cfg(),
                        expected_harness_run_id="hr_passed",
                    )
                self.assertIsNone(engine.version)
                self.assertEqual(engine.registry, [])
                self.assertTrue(engine.released)

    def test_registry_readback_rejects_inserted_metadata_tampering(self) -> None:
        from scheduler.repository import register_blackbox_draft_identity

        cases = (
            {"name": "Unexpected"},
            {"description": "unexpected"},
            {"tenors": '["5Y"]'},
            {"frequency": "weekly"},
            {"schedule_cron": "0 0 * * *"},
            {"schedule_timezone": "UTC"},
            {"deployed_at": "2026-07-27"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                engine = _DraftRegisterEngine(
                    registry_readback_overrides=overrides,
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Registry readback mismatch",
                ):
                    register_blackbox_draft_identity(
                        engine,
                        self._cfg(),
                        expected_harness_run_id="hr_passed",
                    )
                self.assertIsNone(engine.version)
                self.assertEqual(engine.registry, [])
                self.assertTrue(engine.released)

    def test_registry_failure_rolls_back_version_and_releases_lock(self) -> None:
        from scheduler.repository import register_blackbox_draft_identity

        engine = _DraftRegisterEngine(fail_registry_insert=True)
        with self.assertRaisesRegex(RuntimeError, "registry insert failed"):
            register_blackbox_draft_identity(
                engine,
                self._cfg(),
                expected_harness_run_id="hr_passed",
            )

        self.assertIsNone(engine.version)
        self.assertEqual(engine.registry, [])
        self.assertTrue(engine.released)

    def test_latest_all_stage_drift_is_rejected_before_any_insert(self) -> None:
        from scheduler.repository import register_blackbox_draft_identity

        engine = _DraftRegisterEngine(latest_harness_run_id="hr_newer")

        with self.assertRaisesRegex(
            RuntimeError,
            "latest passed all-stage harness run changed",
        ):
            register_blackbox_draft_identity(
                engine,
                self._cfg(),
                expected_harness_run_id="hr_passed",
            )

        self.assertIsNone(engine.version)
        self.assertEqual(engine.registry, [])
        self.assertTrue(engine.released)


if __name__ == "__main__":
    unittest.main()
