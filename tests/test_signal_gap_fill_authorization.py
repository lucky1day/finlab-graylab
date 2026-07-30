from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness.authorization import (
    issue_signal_gap_fill_token,
    issue_token,
    mark_token_used,
    verify_signal_gap_fill_authorization,
)
from harness.cli import main


PLAN_SHA = "a" * 64
TARGETS = (
    {
        "registry_scheme_id": "demo__h1__1Y",
        "base_scheme_id": "demo",
        "target_tenor": "1Y",
        "horizon": 1,
        "task_type": "T+1",
        "predict_date": "2026-07-28",
        "feature_date": "2026-07-27",
        "target_date": "2026-07-29",
        "prediction_phase": "gray_live",
    },
)
AUTHORITY = {
    "authority_type": "native_current_snapshot_artifact",
    "artifact_id": "native-" + "b" * 24,
    "manifest_sha256": "c" * 64,
    "feature_date": "2026-07-27",
    "cutoff_date": "2026-07-27",
    "vintage_disclaimer":
        "current_snapshot_as_of_not_historical_vintage",
}
ARCHIVED_AUTHORITY = {
    "authority_type": "native_archived_generation",
    "generation_id": "native-" + "d" * 24,
    "manifest_sha256": "e" * 64,
    "business_date": "2026-07-28",
    "feature_date": "2026-07-27",
    "cutoff_date": "2026-07-27",
    "replay_mode": "historical_sealed_generation_replay",
}


class SignalGapFillAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.used_path = Path(self.tempdir.name) / "used.json"
        self.secret = patch.dict(
            os.environ,
            {"HARNESS_AUTH_SECRET": "test-signal-gap-secret"},
            clear=False,
        )
        self.secret.start()

    def tearDown(self) -> None:
        self.secret.stop()
        self.tempdir.cleanup()

    def _issue(self, **overrides) -> str:
        values = {
            "plan_sha256": PLAN_SHA,
            "base_scheme_id": "demo",
            "predict_date": "2026-07-28",
            "target_keys": TARGETS,
            "scheme_version": "version-1",
            "source_authority": AUTHORITY,
            "ttl_seconds": 900,
        }
        values.update(overrides)
        return issue_signal_gap_fill_token(**values)

    def test_token_is_hmac_signed_and_binds_complete_group_scope(self) -> None:
        token = self._issue()

        auth, errors = verify_signal_gap_fill_authorization(
            token,
            plan_sha256=PLAN_SHA,
            base_scheme_id="demo",
            predict_date="2026-07-28",
            target_keys=TARGETS,
            scheme_version="version-1",
            source_authority=AUTHORITY,
            used_store_path=self.used_path,
        )

        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)
        self.assertEqual(auth.plan_sha256, PLAN_SHA)
        self.assertEqual(auth.signal_gap_target_keys, TARGETS)
        self.assertEqual(auth.source_authority, AUTHORITY)

    def test_archived_native_generation_authority_is_bound_exactly(
        self,
    ) -> None:
        token = self._issue(source_authority=ARCHIVED_AUTHORITY)

        auth, errors = verify_signal_gap_fill_authorization(
            token,
            plan_sha256=PLAN_SHA,
            base_scheme_id="demo",
            predict_date="2026-07-28",
            target_keys=TARGETS,
            scheme_version="version-1",
            source_authority=ARCHIVED_AUTHORITY,
            used_store_path=self.used_path,
        )

        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)
        self.assertEqual(auth.source_authority, ARCHIVED_AUTHORITY)

    def test_archived_native_generation_rejects_replay_or_schema_drift(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "archived Native replay_mode is invalid",
        ):
            self._issue(
                source_authority={
                    **ARCHIVED_AUTHORITY,
                    "replay_mode": "historical_as_of_replay",
                }
            )
        with self.assertRaisesRegex(
            ValueError,
            "archived Native source authority schema is invalid",
        ):
            self._issue(
                source_authority={
                    **ARCHIVED_AUTHORITY,
                    "vintage_disclaimer":
                        "current_snapshot_as_of_not_historical_vintage",
                }
            )

    def test_archived_native_business_date_must_equal_predict_date(
        self,
    ) -> None:
        authority = {
            **ARCHIVED_AUTHORITY,
            "business_date": "2026-07-29",
        }
        with self.assertRaisesRegex(
            ValueError,
            "archived Native business_date must equal predict_date",
        ):
            self._issue(source_authority=authority)

        token = self._issue(source_authority=ARCHIVED_AUTHORITY)

        _, errors = verify_signal_gap_fill_authorization(
            token,
            plan_sha256=PLAN_SHA,
            base_scheme_id="demo",
            predict_date="2026-07-28",
            target_keys=TARGETS,
            scheme_version="version-1",
            source_authority=authority,
            used_store_path=self.used_path,
        )

        self.assertIn(
            "archived Native business_date must equal predict_date",
            errors,
        )

    def test_scope_drift_and_old_gray_token_are_rejected(self) -> None:
        token = self._issue()
        changed_targets = (
            {**TARGETS[0], "target_tenor": "5Y"},
        )

        _, errors = verify_signal_gap_fill_authorization(
            token,
            plan_sha256=PLAN_SHA,
            base_scheme_id="demo",
            predict_date="2026-07-28",
            target_keys=changed_targets,
            scheme_version="version-1",
            source_authority=AUTHORITY,
            used_store_path=self.used_path,
        )
        old_token = issue_token(
            "demo",
            "gray_backfill_write",
            "2026-07-28",
            ttl_seconds=900,
        )
        _, old_errors = verify_signal_gap_fill_authorization(
            old_token,
            plan_sha256=PLAN_SHA,
            base_scheme_id="demo",
            predict_date="2026-07-28",
            target_keys=TARGETS,
            scheme_version="version-1",
            source_authority=AUTHORITY,
            used_store_path=self.used_path,
        )

        self.assertIn("target multiset mismatch", errors)
        self.assertTrue(
            any("action mismatch" in error for error in old_errors)
        )

    def test_requires_secret_and_ttl_at_most_900_seconds(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most 900"):
            self._issue(ttl_seconds=901)

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "HMAC"):
                self._issue()

    def test_token_is_one_time(self) -> None:
        token = self._issue()
        auth, errors = verify_signal_gap_fill_authorization(
            token,
            plan_sha256=PLAN_SHA,
            base_scheme_id="demo",
            predict_date="2026-07-28",
            target_keys=TARGETS,
            scheme_version="version-1",
            source_authority=AUTHORITY,
            used_store_path=self.used_path,
        )
        self.assertEqual(errors, [])
        mark_token_used(auth, self.used_path)

        _, replay_errors = verify_signal_gap_fill_authorization(
            token,
            plan_sha256=PLAN_SHA,
            base_scheme_id="demo",
            predict_date="2026-07-28",
            target_keys=TARGETS,
            scheme_version="version-1",
            source_authority=AUTHORITY,
            used_store_path=self.used_path,
        )
        self.assertIn("authorization token already used", replay_errors)

    def test_cli_issue_requires_scheme_version_before_reading_scope_files(
        self,
    ) -> None:
        stderr = StringIO()
        with (
            patch(
                "harness.cli._read_json_file",
                side_effect=AssertionError("scope file must not be read"),
            ),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(
                [
                    "auth",
                    "issue",
                    "--scheme-id",
                    "demo",
                    "--action",
                    "signal_gap_fill_write",
                    "--predict-date",
                    "2026-07-28",
                    "--plan-sha256",
                    PLAN_SHA,
                    "--base-scheme-id",
                    "demo",
                    "--target-keys-json",
                    "/not/read/targets.json",
                    "--source-authority-json",
                    "/not/read/source.json",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("requires --scheme-version", stderr.getvalue())
