from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

import harness.authorization as authorization
from harness.authorization import (
    AuthorizationTokenAlreadyUsedError,
    authorization_token_hash,
    issue_token,
    mark_token_used,
    parse_token,
    used_tokens_path,
    verify_authorization,
    write_authorization_audit,
)


SIDE_EFFECT_ACTIONS = authorization.SIDE_EFFECT_ACTIONS


def _consume_in_process(operation: str, used_path: str, results) -> None:
    try:
        mark_token_used(parse_token(operation), Path(used_path))
    except AuthorizationTokenAlreadyUsedError:
        results.put("replayed")
    except Exception as exc:  # noqa: BLE001
        results.put(f"error:{type(exc).__name__}")
    else:
        results.put("consumed")


class DirectAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _used_path(self) -> Path:
        return self.root / "reports" / "harness" / ".used_authorization_tokens.json"

    @staticmethod
    def _decode(value: str) -> dict:
        padding = "=" * (-len(value) % 4)
        return json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode(
                "utf-8"
            )
        )

    @staticmethod
    def _encode(value: dict, *, canonical: bool = True) -> str:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=canonical,
            separators=(",", ":") if canonical else None,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _operation(self, action: str = "activate", **kwargs) -> str:
        if action in authorization.EXACT_PREDICT_DATE_ACTIONS:
            kwargs.setdefault("predict_date", "2026-07-20")
        kwargs.setdefault("scheme_version", "version-1")
        kwargs.setdefault("issued_by", "operator")
        return issue_token("trial", action, **kwargs)

    def test_side_effect_action_set_is_exact(self) -> None:
        self.assertEqual(
            SIDE_EFFECT_ACTIONS,
            frozenset(
                {
                    "activate",
                    "live_write",
                    "backtest_persist",
                    "draft_register",
                    "shadow_register",
                    "blackbox_activate",
                    "blackbox_lifecycle_bootstrap",
                    "blackbox_reconcile",
                    "blackbox_revision_activate",
                }
            ),
        )

    def test_no_secret_is_required_or_read(self) -> None:
        operation = self._operation()
        auth, errors = verify_authorization(
            operation,
            scheme_id="trial",
            action="activate",
            scheme_version="version-1",
            used_store_path=self._used_path(),
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)
        self.assertNotIn("HARNESS_AUTH_SECRET", Path(authorization.__file__).read_text())

    def test_issue_rejects_unknown_action_and_blank_operator(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown authorization action"):
            issue_token("trial", "unknown", scheme_version="version-1")
        for operator in (None, "", "   "):
            with self.subTest(operator=operator), self.assertRaisesRegex(
                ValueError,
                "issued_by must be a non-empty string",
            ):
                issue_token(
                    "trial",
                    "activate",
                    scheme_version="version-1",
                    issued_by=operator,
                )

    def test_date_bound_actions_and_backtest_start_are_exact(self) -> None:
        for action in authorization.EXACT_PREDICT_DATE_ACTIONS:
            for value in (None, "", "2026-7-20", " 2026-07-20"):
                with self.subTest(action=action, value=value), self.assertRaisesRegex(
                    ValueError,
                    "predict_date",
                ):
                    issue_token(
                        "trial",
                        action,
                        value,
                        scheme_version="version-1",
                    )
        operation = self._operation(
            "backtest_persist",
            backtest_start_date="2025-02-03",
        )
        _, errors = verify_authorization(
            operation,
            scheme_id="trial",
            action="backtest_persist",
            predict_date="2026-07-20",
            scheme_version="version-1",
            backtest_start_date="2025-01-01",
            used_store_path=self._used_path(),
        )
        self.assertIn("backtest_start_date mismatch", "\n".join(errors))

    def test_verify_binds_scope_and_system_selected_harness_run(self) -> None:
        automatic = self._operation("shadow_register")
        auth, errors = verify_authorization(
            automatic,
            scheme_id="trial",
            action="shadow_register",
            predict_date="2026-07-20",
            scheme_version="version-1",
            harness_run_id="hr-system-selected",
            used_store_path=self._used_path(),
        )
        self.assertEqual(errors, [])
        self.assertEqual(auth.harness_run_id, "hr-system-selected")

        explicit_wrong = self._operation(
            "shadow_register",
            harness_run_id="hr-wrong",
        )
        _, errors = verify_authorization(
            explicit_wrong,
            scheme_id="trial",
            action="shadow_register",
            predict_date="2026-07-20",
            scheme_version="version-1",
            harness_run_id="hr-system-selected",
            used_store_path=self._used_path(),
        )
        self.assertIn("harness_run_id mismatch", "\n".join(errors))

    def test_run_scoped_actions_require_canonical_expected_harness_run(self) -> None:
        for action in authorization.HARNESS_RUN_SCOPED_ACTIONS:
            operation = self._operation(action)
            predict_date = (
                "2026-07-20"
                if action in authorization.EXACT_PREDICT_DATE_ACTIONS
                else None
            )
            for harness_run_id in (None, "", "   ", " hr-not-canonical "):
                with self.subTest(
                    action=action,
                    harness_run_id=harness_run_id,
                ):
                    auth, errors = verify_authorization(
                        operation,
                        scheme_id="trial",
                        action=action,
                        predict_date=predict_date,
                        scheme_version="version-1",
                        harness_run_id=harness_run_id,
                        used_store_path=self._used_path(),
                    )
                    self.assertIsNotNone(auth)
                    self.assertIsNone(auth.harness_run_id)
                    self.assertIn(
                        "expected harness_run_id must be a non-empty string",
                        "\n".join(errors),
                    )

    def test_non_run_scoped_actions_do_not_require_expected_harness_run(self) -> None:
        actions = SIDE_EFFECT_ACTIONS - authorization.HARNESS_RUN_SCOPED_ACTIONS
        for action in actions:
            operation = self._operation(action)
            predict_date = (
                "2026-07-20"
                if action in authorization.EXACT_PREDICT_DATE_ACTIONS
                else None
            )
            with self.subTest(action=action):
                auth, errors = verify_authorization(
                    operation,
                    scheme_id="trial",
                    action=action,
                    predict_date=predict_date,
                    scheme_version="version-1",
                    harness_run_id=None,
                    used_store_path=self._used_path(),
                )
                self.assertEqual(errors, [])
                self.assertIsNotNone(auth)
                self.assertIsNone(auth.harness_run_id)

    def test_envelope_is_unsigned_canonical_payload_only(self) -> None:
        original = self._decode(self._operation("live_write"))
        self.assertEqual(set(original), {"payload"})
        self.assertNotIn("sig", original)
        for value in (
            original["payload"],
            {**original, "extra": "unexpected"},
            {**original, "payload": {**original["payload"], "extra": "unexpected"}},
        ):
            with self.assertRaisesRegex(ValueError, "schema is invalid"):
                parse_token(self._encode(value))

        operation = self._operation("live_write")
        for candidate in (
            operation + "=",
            operation + "!",
            " " + operation,
            self._encode(self._decode(operation), canonical=False),
        ):
            with self.assertRaisesRegex(ValueError, "canonical"):
                parse_token(candidate)

    def test_scope_tampering_is_rejected_by_expected_context(self) -> None:
        envelope = self._decode(self._operation())
        envelope["payload"]["scheme_id"] = "evil"
        parsed, errors = verify_authorization(
            self._encode(envelope),
            scheme_id="trial",
            action="activate",
            scheme_version="version-1",
            used_store_path=self._used_path(),
        )
        self.assertIsNotNone(parsed)
        self.assertIn("scheme_id mismatch", "\n".join(errors))

    def test_replay_store_has_exactly_one_concurrent_winner(self) -> None:
        operation = self._operation("live_write")
        context = multiprocessing.get_context("fork")
        results = context.Queue()
        processes = [
            context.Process(
                target=_consume_in_process,
                args=(operation, str(self._used_path()), results),
            )
            for _ in range(8)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        outcomes = [results.get(timeout=2) for _ in processes]
        self.assertEqual(outcomes.count("consumed"), 1, outcomes)
        self.assertEqual(outcomes.count("replayed"), 7, outcomes)
        self.assertEqual(
            json.loads(self._used_path().read_text()),
            [hashlib.sha256(operation.encode()).hexdigest()],
        )

    def test_distinct_operations_do_not_lose_used_hashes(self) -> None:
        operations = [
            issue_token(
                f"scheme_{index}",
                "live_write",
                "2026-07-20",
                scheme_version=f"version-{index}",
            )
            for index in range(16)
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    mark_token_used,
                    parse_token(operation),
                    self._used_path(),
                )
                for operation in operations
            ]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(len(json.loads(self._used_path().read_text())), 16)

    def test_audit_records_direct_mode_without_raw_operation(self) -> None:
        operation = self._operation("shadow_register")
        auth, errors = verify_authorization(
            operation,
            scheme_id="trial",
            action="shadow_register",
            predict_date="2026-07-20",
            scheme_version="version-1",
            harness_run_id="hr-latest",
            used_store_path=self._used_path(),
        )
        self.assertEqual(errors, [])
        path = write_authorization_audit(auth, self.root / "audit")
        payload = json.loads(path.read_text())
        self.assertEqual(payload["authorization_mode"], "direct_operator_command_v1")
        self.assertEqual(payload["harness_run_id"], "hr-latest")
        self.assertNotIn("token", payload)
        self.assertEqual(
            payload["operation_id_sha256"],
            authorization_token_hash(operation),
        )

    def test_used_operation_path_remains_host_scoped(self) -> None:
        self.assertEqual(
            used_tokens_path(Path("/root")),
            Path("/root/reports/harness/.used_authorization_tokens.json"),
        )


if __name__ == "__main__":
    unittest.main()
