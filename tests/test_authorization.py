from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from harness.authorization import (
    AuthorizationSecretError,
    issue_token,
    mark_token_used,
    used_tokens_path,
    verify_authorization,
)


SECRET = "test-secret-value"


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

    def test_issue_requires_secret(self) -> None:
        os.environ.pop("HARNESS_AUTH_SECRET", None)
        with self.assertRaises(AuthorizationSecretError):
            issue_token("t5_daily", "activate")

    def test_hmac_verify_success(self) -> None:
        token = issue_token("t5_daily", "activate", predict_date="2025-01-02")
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
