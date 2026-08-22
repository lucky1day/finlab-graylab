from __future__ import annotations

import ast
import base64
import concurrent.futures
import hashlib
import io
import inspect
import json
import multiprocessing
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import harness.authorization as authorization
from harness.authorization import (
    AuthorizationSecretError,
    AuthorizationTokenAlreadyUsedError,
    authorization_token_hash,
    issue_token,
    mark_token_used,
    parse_token,
    used_tokens_path,
    verify_authorization,
)


SECRET = "test-secret-value"
SIDE_EFFECT_ACTIONS = getattr(authorization, "SIDE_EFFECT_ACTIONS", frozenset())
HARNESS_RUN_SCOPED_ACTIONS = getattr(
    authorization,
    "HARNESS_RUN_SCOPED_ACTIONS",
    frozenset(),
)


def _consume_token_in_process(token: str, used_path: str, results) -> None:
    try:
        mark_token_used(parse_token(token), Path(used_path))
    except AuthorizationTokenAlreadyUsedError:
        results.put("replayed")
    except Exception as exc:  # noqa: BLE001
        results.put(f"error:{type(exc).__name__}")
    else:
        results.put("consumed")


class AuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_secret = os.environ.get("HARNESS_AUTH_SECRET")
        os.environ["HARNESS_AUTH_SECRET"] = SECRET
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        if self._prev_secret is None:
            os.environ.pop("HARNESS_AUTH_SECRET", None)
        else:
            os.environ["HARNESS_AUTH_SECRET"] = self._prev_secret
        self._tmp.cleanup()

    def _used_path(self) -> Path:
        return self.root / "reports" / "harness" / ".used_authorization_tokens.json"

    @staticmethod
    def _decode_token(token: str) -> dict:
        padding = "=" * (-len(token) % 4)
        return json.loads(
            base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
        )

    @staticmethod
    def _encode_token(value: dict, *, canonical: bool = True) -> str:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=canonical,
            separators=(",", ":") if canonical else None,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _token(self, action: str = "activate", **kwargs) -> str:
        if action in {
            "live_write",
            "backtest_persist",
            "draft_register",
            "shadow_register",
            "blackbox_revision_activate",
        }:
            kwargs.setdefault("predict_date", "2026-07-20")
        kwargs.setdefault("scheme_version", "version-1")
        return issue_token("trial", action, **kwargs)

    def test_side_effect_action_set_is_exact_and_gap_authorization_is_removed(self) -> None:
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
        self.assertNotIn("plan_sha256", authorization.Authorization.__dataclass_fields__)
        self.assertNotIn("signal_gap_target_keys", authorization.Authorization.__dataclass_fields__)
        self.assertNotIn("source_authority", authorization.Authorization.__dataclass_fields__)

    def test_side_effect_gates_delegate_security_checks_to_unified_verifier(self) -> None:
        from harness.blackbox_v2 import (
            activation,
            draft_register,
            gates,
            lifecycle_bootstrap,
            revision_activation,
        )
        from harness.gates import activate_gate, backtest_gate, live_gate

        sources = "\n".join(
            inspect.getsource(module)
            for module in (
                activate_gate,
                live_gate,
                backtest_gate,
                activation,
                draft_register,
                gates,
                lifecycle_bootstrap,
                revision_activation,
            )
        )
        self.assertNotIn("authorization_signing_enabled", sources)
        self.assertNotIn("required_future_expiry_errors", sources)
        self.assertIn("verify_authorization", sources)

    def test_issue_requires_secret_and_verify_fails_closed_without_it(self) -> None:
        token = self._token()
        os.environ.pop("HARNESS_AUTH_SECRET", None)

        with self.assertRaises(AuthorizationSecretError):
            self._token()
        parsed, errors = verify_authorization(
            token,
            scheme_id="trial",
            action="activate",
            scheme_version="version-1",
            used_store_path=self._used_path(),
        )

        self.assertIsNone(parsed)
        self.assertTrue(any("HARNESS_AUTH_SECRET" in error for error in errors), errors)

    def test_issue_rejects_unknown_action_and_nonempty_issuer_is_universal(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown authorization action"):
            issue_token("trial", "unknown")

        for issued_by in (None, "", "   "):
            with self.subTest(issued_by=issued_by), self.assertRaisesRegex(
                ValueError,
                "issued_by must be a non-empty string",
            ):
                issue_token(
                    "trial",
                    "activate",
                    scheme_version="version-1",
                    issued_by=issued_by,
                )

    def test_date_bound_actions_require_canonical_date_and_other_actions_reject_dates(self) -> None:
        date_actions = {
            "live_write",
            "backtest_persist",
            "draft_register",
            "shadow_register",
            "blackbox_revision_activate",
        }
        for action in date_actions:
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

        for action in SIDE_EFFECT_ACTIONS - date_actions:
            with self.subTest(action=action), self.assertRaisesRegex(
                ValueError,
                "does not accept predict_date",
            ):
                issue_token(
                    "trial",
                    action,
                    "2026-07-20",
                    scheme_version="version-1",
                )

    def test_verify_binds_exact_version_date_and_harness_run(self) -> None:
        token = self._token(
            "shadow_register",
            harness_run_id="hr-passed",
        )
        _, errors = verify_authorization(
            token,
            scheme_id="trial",
            action="shadow_register",
            predict_date="2026-07-21",
            scheme_version="version-2",
            harness_run_id="hr-other",
            used_store_path=self._used_path(),
        )

        joined = "\n".join(errors)
        self.assertIn("predict_date mismatch", joined)
        self.assertIn("scheme_version mismatch", joined)
        self.assertIn("harness_run_id mismatch", joined)

    def test_backtest_token_binds_canonical_start_date(self) -> None:
        token = self._token(
            "backtest_persist",
            backtest_start_date="2025-02-03",
        )
        auth = parse_token(token)
        self.assertEqual(auth.backtest_start_date, "2025-02-03")

        _, errors = verify_authorization(
            token,
            scheme_id="trial",
            action="backtest_persist",
            predict_date="2026-07-20",
            scheme_version="version-1",
            backtest_start_date="2025-01-01",
            used_store_path=self._used_path(),
        )
        self.assertIn("backtest_start_date mismatch", "\n".join(errors))

    def test_envelope_and_payload_schema_are_exact(self) -> None:
        original = self._decode_token(self._token("live_write"))
        self.assertEqual(set(original), {"payload", "sig"})
        variants = {
            "bare_payload": original["payload"],
            "unsigned": {"payload": original["payload"]},
            "envelope_extra": {**original, "extra": "unexpected"},
            "payload_extra": {
                **original,
                "payload": {**original["payload"], "extra": "unexpected"},
            },
            "payload_missing": {
                **original,
                "payload": {
                    key: value
                    for key, value in original["payload"].items()
                    if key != "nonce"
                },
            },
        }
        for label, value in variants.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError,
                "schema is invalid",
            ):
                parse_token(self._encode_token(value))

    def test_noncanonical_base64_and_json_are_rejected(self) -> None:
        token = self._token("live_write")
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + padding).encode("ascii"))
        variants = {
            "padding": token + "=",
            "suffix": token + "!",
            "whitespace": " " + token,
            "json_whitespace": base64.urlsafe_b64encode(b" " + raw)
            .decode("ascii")
            .rstrip("="),
            "json_key_order": self._encode_token(self._decode_token(token), canonical=False),
        }
        for label, candidate in variants.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError,
                "canonical",
            ):
                parse_token(candidate)

    def test_tampered_signature_is_rejected(self) -> None:
        envelope = self._decode_token(self._token())
        envelope["payload"]["scheme_id"] = "evil"
        parsed, errors = verify_authorization(
            self._encode_token(envelope),
            scheme_id="evil",
            action="activate",
            scheme_version="version-1",
            used_store_path=self._used_path(),
        )
        self.assertIsNotNone(parsed)
        self.assertIn("signature is invalid", "\n".join(errors))

    def test_authorization_object_cannot_bypass_raw_envelope_verification(self) -> None:
        token = self._token()
        parsed, errors = verify_authorization(
            parse_token(token),
            scheme_id="trial",
            action="activate",
            scheme_version="version-1",
            used_store_path=self._used_path(),
        )
        self.assertIsNone(parsed)
        self.assertEqual(errors, ["authorization requires the original raw token string"])
        self.assertNotIn(token, "\n".join(errors))

    def test_replayed_token_is_rejected(self) -> None:
        token = self._token()
        auth, errors = verify_authorization(
            token,
            scheme_id="trial",
            action="activate",
            scheme_version="version-1",
            used_store_path=self._used_path(),
        )
        self.assertEqual(errors, [])
        mark_token_used(auth, self._used_path())
        _, replay_errors = verify_authorization(
            token,
            scheme_id="trial",
            action="activate",
            scheme_version="version-1",
            used_store_path=self._used_path(),
        )
        self.assertIn("already used", "\n".join(replay_errors))

    def test_same_token_concurrent_process_consumers_have_exactly_one_winner(self) -> None:
        token = self._token("live_write")
        context = multiprocessing.get_context("fork")
        results = context.Queue()
        processes = [
            context.Process(
                target=_consume_token_in_process,
                args=(token, str(self._used_path()), results),
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
        self.assertEqual(outcomes.count("replayed"), len(processes) - 1, outcomes)
        stored = json.loads(self._used_path().read_text(encoding="utf-8"))
        self.assertEqual(stored, [hashlib.sha256(token.encode("utf-8")).hexdigest()])

    def test_distinct_concurrent_tokens_do_not_lose_used_hashes(self) -> None:
        tokens = [
            issue_token(
                f"scheme_{index}",
                "live_write",
                "2026-07-20",
                scheme_version=f"version-{index}",
            )
            for index in range(32)
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [
                executor.submit(mark_token_used, parse_token(token), self._used_path())
                for token in tokens
            ]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(
            len(json.loads(self._used_path().read_text(encoding="utf-8"))),
            len(tokens),
        )

    def test_mark_used_fails_closed_for_invalid_store_and_duplicate(self) -> None:
        token = self._token("live_write")
        auth = parse_token(token)
        self._used_path().parent.mkdir(parents=True)
        original = '{"unexpected": "shape"}\n'
        self._used_path().write_text(original, encoding="utf-8")
        with self.assertRaises(ValueError):
            mark_token_used(auth, self._used_path())
        self.assertEqual(self._used_path().read_text(encoding="utf-8"), original)

        self._used_path().unlink()
        mark_token_used(auth, self._used_path())
        with self.assertRaises(AuthorizationTokenAlreadyUsedError) as caught:
            mark_token_used(auth, self._used_path())
        self.assertNotIn(token, str(caught.exception))
        self.assertEqual(authorization_token_hash(auth), authorization_token_hash(token))

    def test_cli_action_choices_are_exact_and_token_is_only_stdout(self) -> None:
        from harness.cli import _build_parser, main

        parser = _build_parser()
        auth_parser = next(
            action for action in parser._actions if action.dest == "command"
        ).choices["auth"]
        issue_parser = next(
            action for action in auth_parser._actions if action.dest == "auth_command"
        ).choices["issue"]
        action_arg = next(
            action for action in issue_parser._actions if action.dest == "action"
        )
        self.assertEqual(action_arg.choices, sorted(SIDE_EFFECT_ACTIONS))

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "auth",
                    "issue",
                    "--scheme-id",
                    "trial",
                    "--action",
                    "activate",
                    "--scheme-version",
                    "version-1",
                    "--issued-by",
                    "operator",
                ]
            )
        self.assertEqual(exit_code, 0)
        stdout = output.getvalue().strip()
        self.assertNotIn(SECRET, stdout)
        self.assertEqual(parse_token(stdout).action, "activate")

    def test_cli_reuses_exact_harness_run_scoped_actions_without_literals(self) -> None:
        from harness import cli

        expected = frozenset(
            {
                "draft_register",
                "shadow_register",
                "blackbox_activate",
                "blackbox_lifecycle_bootstrap",
                "blackbox_reconcile",
                "blackbox_revision_activate",
            }
        )
        self.assertEqual(HARNESS_RUN_SCOPED_ACTIONS, expected)

        cli_source = inspect.getsource(cli)
        self.assertIn("HARNESS_RUN_SCOPED_ACTIONS", cli_source)
        literal_strings = {
            node.value
            for node in ast.walk(ast.parse(cli_source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertTrue(expected.isdisjoint(literal_strings))

        for action in expected:
            argv = [
                "auth",
                "issue",
                "--scheme-id",
                "trial",
                "--action",
                action,
                "--scheme-version",
                "version-1",
                "--issued-by",
                "operator",
            ]
            if action in authorization.EXACT_PREDICT_DATE_ACTIONS:
                argv.extend(["--predict-date", "2026-07-20"])
            with self.subTest(action=action), self.assertRaises(SystemExit):
                cli.main(argv)

    def test_used_tokens_path_location(self) -> None:
        self.assertEqual(
            used_tokens_path(Path("/root")),
            Path("/root/reports/harness/.used_authorization_tokens.json"),
        )


if __name__ == "__main__":
    unittest.main()
