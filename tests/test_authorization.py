from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from harness.authorization import (
    AuthorizationSecretError,
    AuthorizationTokenAlreadyUsedError,
    authorization_signing_enabled,
    authorization_token_hash,
    issue_token,
    mark_token_used,
    parse_token,
    required_future_expiry_errors,
    used_tokens_path,
    verify_authorization,
)


SECRET = "test-secret-value"


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
    def _encode_token(value: dict) -> str:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def test_authorization_signing_enabled_reflects_secret_configuration(self) -> None:
        self.assertTrue(authorization_signing_enabled())
        os.environ.pop("HARNESS_AUTH_SECRET", None)
        self.assertFalse(authorization_signing_enabled())

    def test_backtest_persist_token_binds_start_date(self) -> None:
        token = issue_token(
            "trial",
            "backtest_persist",
            predict_date="2026-07-20",
            backtest_start_date="2025-02-03",
        )

        auth = parse_token(token)
        self.assertEqual(auth.backtest_start_date, "2025-02-03")
        _, errors = verify_authorization(
            token,
            scheme_id="trial",
            action="backtest_persist",
            predict_date="2026-07-20",
            backtest_start_date="2025-01-01",
            used_store_path=self._used_path(),
        )

        self.assertIn("backtest_start_date mismatch", "\n".join(errors))

    def test_backtest_persist_token_defaults_start_date(self) -> None:
        auth = parse_token(
            issue_token("trial", "backtest_persist", predict_date="2026-07-20")
        )

        self.assertEqual(auth.backtest_start_date, "2025-01-01")

    def test_backtest_persist_token_requires_predict_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "predict_date"):
            issue_token("trial", "backtest_persist")

    def test_backtest_persist_verification_rejects_null_predict_date(self) -> None:
        from harness.authorization import _sign

        envelope = self._decode_token(
            issue_token("trial", "backtest_persist", predict_date="2026-07-20")
        )
        envelope["payload"]["predict_date"] = None
        envelope["sig"] = _sign(envelope["payload"])
        token = self._encode_token(envelope)

        _, errors = verify_authorization(
            token,
            scheme_id="trial",
            action="backtest_persist",
            predict_date="2026-07-20",
            backtest_start_date="2025-01-01",
            used_store_path=self._used_path(),
        )

        self.assertIn("predict_date is required", "\n".join(errors))

    def test_gray_backfill_token_requires_canonical_predict_date(self) -> None:
        for value in (None, "", "2026-5-26", " 2026-05-26"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "gray_backfill_write predict_date",
            ):
                issue_token(
                    "trial",
                    "gray_backfill_write",
                    predict_date=value,
                )

    def test_gray_backfill_verification_requires_exact_predict_date(self) -> None:
        token = issue_token(
            "trial",
            "gray_backfill_write",
            predict_date="2026-05-26",
        )

        for context_date in (None, "2026-05-27", "2026-5-26"):
            with self.subTest(context_date=context_date):
                _auth, errors = verify_authorization(
                    token,
                    scheme_id="trial",
                    action="gray_backfill_write",
                    predict_date=context_date,
                    used_store_path=self._used_path(),
                )

                self.assertTrue(errors)
                self.assertIn("predict_date", "\n".join(errors))

    def test_draft_register_token_requires_canonical_predict_date(self) -> None:
        for value in (None, "", "2026-7-20", " 2026-07-20"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "draft_register predict_date",
            ):
                issue_token(
                    "trial",
                    "draft_register",
                    predict_date=value,
                )

    def test_draft_register_issue_requires_real_nonempty_operator(self) -> None:
        for issued_by in (None, "", "   "):
            with self.subTest(issued_by=issued_by), self.assertRaisesRegex(
                ValueError,
                "issued_by must be a non-empty string",
            ):
                issue_token(
                    "trial",
                    "draft_register",
                    predict_date="2026-07-20",
                    issued_by=issued_by,
                )

    def test_draft_register_verify_rejects_non_string_operator_payload(self) -> None:
        from harness.authorization import _sign

        envelope = self._decode_token(
            issue_token(
                "trial",
                "draft_register",
                predict_date="2026-07-20",
                issued_by="operator",
            )
        )
        envelope["payload"]["issued_by"] = None
        envelope["sig"] = _sign(envelope["payload"])
        token = self._encode_token(envelope)

        _auth, errors = verify_authorization(
            token,
            scheme_id="trial",
            action="draft_register",
            predict_date="2026-07-20",
            used_store_path=self._used_path(),
        )

        self.assertIn(
            "draft_register issued_by must be a non-empty string",
            errors,
        )

    def test_non_backtest_token_schema_is_unchanged(self) -> None:
        envelope = self._decode_token(issue_token("trial", "blackbox_activate"))

        self.assertNotIn("backtest_start_date", envelope["payload"])

    def test_issue_without_secret_yields_plaintext_token(self) -> None:
        # 软默认：未配置 HARNESS_AUTH_SECRET 时仍可签发 token（明文确认闸），不报错。
        os.environ.pop("HARNESS_AUTH_SECRET", None)
        token = issue_token("t5_daily", "activate", predict_date="2025-01-02")
        self.assertTrue(token)
        self.assertEqual(set(self._decode_token(token)), {"payload"})
        # 无密钥时校验跳过签名，但一次性 + 作用域绑定仍生效。
        auth, errors = verify_authorization(
            token,
            scheme_id="t5_daily",
            action="activate",
            predict_date="2025-01-02",
            used_store_path=self._used_path(),
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)
        # 作用域不符仍应被拒。
        _, mismatch_errors = verify_authorization(
            token,
            scheme_id="t1_daily",
            action="activate",
            used_store_path=self._used_path(),
        )
        self.assertTrue(any("scheme_id mismatch" in e for e in mismatch_errors), mismatch_errors)

    def test_hmac_verify_success(self) -> None:
        token = issue_token("t5_daily", "activate", predict_date="2025-01-02")
        self.assertEqual(set(self._decode_token(token)), {"payload", "sig"})
        auth, errors = verify_authorization(
            token,
            scheme_id="t5_daily",
            action="activate",
            predict_date="2025-01-02",
            used_store_path=self._used_path(),
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)
        self.assertEqual(auth.scheme_id, "t5_daily")

    def test_unsigned_authorization_rejects_alternate_payload_representations(self) -> None:
        signed_token = issue_token("t5_daily", "live_write", predict_date="2025-01-02")
        os.environ.pop("HARNESS_AUTH_SECRET", None)
        used = self._used_path()
        token = issue_token("t5_daily", "live_write", predict_date="2025-01-02")
        envelope = self._decode_token(token)
        auth, errors = verify_authorization(
            token,
            scheme_id="t5_daily",
            action="live_write",
            predict_date="2025-01-02",
            used_store_path=used,
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)
        mark_token_used(auth, used)

        variants = {
            "bare_payload": self._encode_token(envelope["payload"]),
            "arbitrary_signature": self._encode_token(
                {"payload": envelope["payload"], "sig": "not-a-signature"}
            ),
            "signed_envelope": signed_token,
        }
        for label, candidate in variants.items():
            with self.subTest(label=label):
                parsed, candidate_errors = verify_authorization(
                    candidate,
                    scheme_id="t5_daily",
                    action="live_write",
                    predict_date="2025-01-02",
                    used_store_path=used,
                )
                self.assertIsNone(parsed)
                self.assertTrue(
                    any("authorization token envelope does not match signing mode" in error
                        for error in candidate_errors),
                    candidate_errors,
                )
                self.assertNotIn(candidate, "\n".join(candidate_errors))

    def test_signed_authorization_rejects_unsigned_envelope(self) -> None:
        token = issue_token("t5_daily", "activate")
        envelope = self._decode_token(token)
        unsigned = self._encode_token({"payload": envelope["payload"]})

        parsed, errors = verify_authorization(
            unsigned,
            scheme_id="t5_daily",
            action="activate",
            used_store_path=self._used_path(),
        )

        self.assertIsNone(parsed)
        self.assertTrue(
            any("authorization token envelope does not match signing mode" in error
                for error in errors),
            errors,
        )
        self.assertNotIn(unsigned, "\n".join(errors))

    def test_authorization_object_cannot_bypass_envelope_verification(self) -> None:
        token = issue_token("t5_daily", "activate")
        auth = parse_token(token)

        parsed, errors = verify_authorization(
            auth,
            scheme_id="t5_daily",
            action="activate",
            used_store_path=self._used_path(),
        )

        self.assertIsNone(parsed)
        self.assertEqual(errors, ["authorization requires the original raw token string"])
        self.assertNotIn(token, "\n".join(errors))

    def test_tampered_token_rejected(self) -> None:
        token = issue_token("t5_daily", "activate")
        padding = "=" * (-len(token) % 4)
        decoded = json.loads(base64.urlsafe_b64decode((token + padding).encode()).decode())
        decoded["payload"]["scheme_id"] = "evil"  # tamper without re-signing
        raw = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()
        tampered = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        _, errors = verify_authorization(
            tampered,
            scheme_id="evil",
            action="activate",
            used_store_path=self._used_path(),
        )
        self.assertTrue(any("signature is invalid" in e for e in errors), errors)

    def test_expired_token_rejected(self) -> None:
        token = issue_token("t5_daily", "activate", ttl_seconds=-1)
        _, errors = verify_authorization(
            token,
            scheme_id="t5_daily",
            action="activate",
            used_store_path=self._used_path(),
        )
        self.assertTrue(any("expired" in e for e in errors), errors)

    def test_native_authorization_keeps_existing_long_ttl_behavior(self) -> None:
        token = issue_token("t5_daily", "activate", ttl_seconds=3600)

        auth, errors = verify_authorization(
            token,
            scheme_id="t5_daily",
            action="activate",
            used_store_path=self._used_path(),
        )

        self.assertIsNotNone(auth)
        self.assertEqual(errors, [])

    def test_blackbox_privileged_expiry_rejects_long_original_ttl_near_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        issued_at = (now - timedelta(hours=2)).isoformat()
        expires_at = (now + timedelta(minutes=5)).isoformat()

        errors = required_future_expiry_errors(issued_at, expires_at)

        self.assertTrue(any("900 seconds" in error for error in errors), errors)

    def test_blackbox_privileged_expiry_validates_issued_at(self) -> None:
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(minutes=5)).isoformat()
        cases = {
            "missing": None,
            "empty": "",
            "malformed": "not-a-timestamp",
            "naive": now.replace(tzinfo=None).isoformat(),
            "future": (now + timedelta(minutes=2)).isoformat(),
        }

        for label, issued_at in cases.items():
            with self.subTest(label=label):
                errors = required_future_expiry_errors(issued_at, expires_at)
                self.assertTrue(any("issued_at" in error for error in errors), errors)

    def test_blackbox_privileged_expiry_accepts_short_offset_aware_window(self) -> None:
        now = datetime.now(timezone.utc)
        issued_at = (now - timedelta(seconds=10)).astimezone(
            timezone(timedelta(hours=8))
        ).isoformat()
        expires_at = (now + timedelta(minutes=5)).astimezone(
            timezone(timedelta(hours=8))
        ).isoformat()

        self.assertEqual(required_future_expiry_errors(issued_at, expires_at), [])

    def test_replayed_token_rejected(self) -> None:
        used = self._used_path()
        token = issue_token("t5_daily", "activate")
        auth, errors = verify_authorization(
            token, scheme_id="t5_daily", action="activate", used_store_path=used
        )
        self.assertEqual(errors, [])
        mark_token_used(auth, used)
        _, errors2 = verify_authorization(
            token, scheme_id="t5_daily", action="activate", used_store_path=used
        )
        self.assertTrue(any("already used" in e for e in errors2), errors2)

    def test_noncanonical_token_variants_are_rejected_after_canonical_token_is_used(self) -> None:
        used = self._used_path()
        token = issue_token("t5_daily", "live_write", predict_date="2025-01-02")
        auth, errors = verify_authorization(
            token,
            scheme_id="t5_daily",
            action="live_write",
            predict_date="2025-01-02",
            used_store_path=used,
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)
        mark_token_used(auth, used)

        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + padding).encode("ascii"))
        noncanonical_json = base64.urlsafe_b64encode(b" " + raw).decode("ascii").rstrip("=")
        variants = {
            "ignored_suffix": token + "!",
            "padding": token + "=",
            "leading_whitespace": " " + token,
            "embedded_whitespace": token[:8] + " " + token[8:],
            "trailing_newline": token + "\n",
            "noncanonical_json": noncanonical_json,
        }

        for label, candidate in variants.items():
            with self.subTest(label=label):
                _, candidate_errors = verify_authorization(
                    candidate,
                    scheme_id="t5_daily",
                    action="live_write",
                    predict_date="2025-01-02",
                    used_store_path=used,
                )
                self.assertTrue(
                    any("invalid authorization token" in error for error in candidate_errors),
                    candidate_errors,
                )
                self.assertNotIn(candidate, "\n".join(candidate_errors))

    def test_canonical_envelope_and_payload_schema_are_exact(self) -> None:
        used = self._used_path()
        token = issue_token("t5_daily", "live_write", predict_date="2025-01-02")
        auth = parse_token(token)
        mark_token_used(auth, used)

        padding = "=" * (-len(token) % 4)
        original = json.loads(
            base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
        )
        variants = {}

        envelope_extra = dict(original)
        envelope_extra["extra"] = "ignored-by-signature"
        variants["envelope_extra"] = envelope_extra

        payload_extra = json.loads(json.dumps(original))
        payload_extra["payload"]["extra"] = "unexpected"
        variants["payload_extra"] = payload_extra

        payload_missing = json.loads(json.dumps(original))
        payload_missing["payload"].pop("expires_at")
        variants["payload_missing"] = payload_missing

        for label, envelope in variants.items():
            with self.subTest(label=label):
                raw = json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                candidate = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

                parsed, errors = verify_authorization(
                    candidate,
                    scheme_id="t5_daily",
                    action="live_write",
                    predict_date="2025-01-02",
                    used_store_path=used,
                )

                self.assertIsNone(parsed)
                self.assertTrue(
                    any("invalid authorization token" in error for error in errors),
                    errors,
                )
                self.assertNotIn(candidate, "\n".join(errors))

    def test_legacy_bare_payload_schema_is_exact(self) -> None:
        token = issue_token("t5_daily", "live_write")
        padding = "=" * (-len(token) % 4)
        envelope = json.loads(
            base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
        )
        payload = envelope["payload"]

        for label, mutate in {
            "extra": lambda value: value.__setitem__("extra", "unexpected"),
            "missing": lambda value: value.pop("expires_at"),
        }.items():
            with self.subTest(label=label):
                candidate_payload = dict(payload)
                mutate(candidate_payload)
                raw = json.dumps(
                    candidate_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                candidate = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

                with self.assertRaisesRegex(ValueError, "schema is invalid") as caught:
                    parse_token(candidate)

                self.assertNotIn(candidate, str(caught.exception))

    def test_same_token_concurrent_process_consumers_have_exactly_one_winner(self) -> None:
        token = issue_token("t5_daily", "live_write", predict_date="2025-01-02")
        used = self._used_path()
        context = multiprocessing.get_context("fork")
        results = context.Queue()
        processes = [
            context.Process(
                target=_consume_token_in_process,
                args=(token, str(used), results),
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
        stored = json.loads(used.read_text(encoding="utf-8"))
        self.assertEqual(stored, [hashlib.sha256(token.encode("utf-8")).hexdigest()])
        self.assertEqual(stored, [authorization_token_hash(token)])

    def test_distinct_concurrent_tokens_do_not_lose_used_hashes(self) -> None:
        used = self._used_path()
        tokens = [issue_token(f"scheme_{index}", "live_write") for index in range(32)]
        auths = [parse_token(token) for token in tokens]

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(mark_token_used, auth, used) for auth in auths]
            for future in futures:
                future.result(timeout=10)

        stored = json.loads(used.read_text(encoding="utf-8"))
        self.assertEqual(len(stored), len(tokens))

    def test_mark_token_used_rejects_malformed_store_without_overwriting_it(self) -> None:
        used = self._used_path()
        used.parent.mkdir(parents=True)
        original = '{"unexpected": "shape"}\n'
        used.write_text(original, encoding="utf-8")
        auth = parse_token(issue_token("t5_daily", "live_write"))

        with self.assertRaises(ValueError):
            mark_token_used(auth, used)

        self.assertEqual(used.read_text(encoding="utf-8"), original)

    def test_lock_failure_fails_closed_without_exposing_token(self) -> None:
        used = self._used_path()
        token = issue_token("t5_daily", "live_write")
        auth = parse_token(token)

        with patch("harness.authorization.fcntl.flock", side_effect=OSError("lock unavailable")):
            with self.assertRaises(OSError) as caught:
                mark_token_used(auth, used)

        self.assertFalse(used.exists())
        self.assertNotIn(token, str(caught.exception))

    def test_replay_error_does_not_expose_raw_token(self) -> None:
        used = self._used_path()
        token = issue_token("t5_daily", "live_write")
        auth = parse_token(token)
        mark_token_used(auth, used)

        with self.assertRaises(AuthorizationTokenAlreadyUsedError) as caught:
            mark_token_used(auth, used)

        self.assertNotIn(token, str(caught.exception))

    def test_action_and_scheme_mismatch_rejected(self) -> None:
        token = issue_token("t5_daily", "activate")
        _, errors = verify_authorization(
            token,
            scheme_id="other_scheme",
            action="live_write",
            used_store_path=self._used_path(),
        )
        self.assertTrue(any("scheme_id mismatch" in e for e in errors), errors)
        self.assertTrue(any("action mismatch" in e for e in errors), errors)

    def test_used_tokens_path_location(self) -> None:
        self.assertEqual(
            used_tokens_path(Path("/root")),
            Path("/root/reports/harness/.used_authorization_tokens.json"),
        )


if __name__ == "__main__":
    unittest.main()
