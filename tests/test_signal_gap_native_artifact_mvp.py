from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text

from harness.authorization import (
    issue_signal_gap_native_artifact_register_token,
    verify_signal_gap_native_artifact_register_authorization,
)
from harness.cli import main
from harness.signal_gap_native_artifact import (
    prepare_signal_gap_native_artifact,
    register_signal_gap_native_artifact,
)
from harness.signal_gap_plan import (
    ExpectedSignalCase,
    InputGeneration,
    _native_generation_eligibility,
)
from scheduler.generation_registry import (
    register_gray_gap_native_artifact,
)
from scheduler.repository import (
    register_sealed_gray_gap_native_generation,
)
from shared import native_input_generation as native_module
from tests.test_native_input_generation import (
    _Engine,
    _patched_source_readers,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _artifact_authority(context) -> dict[str, str]:
    return {
        "authority_type": "native_current_snapshot_artifact",
        "purpose": "signal_gap_gray_live_current_snapshot",
        "artifact_id": context.generation_id,
        "manifest_sha256": context.manifest_sha256,
        "dataset_content_id": context.dataset_content_id,
        "source_commit_token": context.source_commit_token,
        "capture_business_date": context.business_date,
        "feature_date": context.feature_date,
        "exporter_version": context.exporter_version,
        "vintage_disclaimer":
            "current_snapshot_as_of_not_historical_vintage",
    }


def _generation_row(context, **changes: object) -> InputGeneration:
    values: dict[str, object] = {
        "generation_id": context.generation_id,
        "generation_type": context.generation_type,
        "business_date": context.business_date,
        "feature_date": context.feature_date,
        "readiness_basis": context.readiness_basis,
        "source_commit_token": context.source_commit_token,
        "dataset_content_id": context.dataset_content_id,
        "schema_version": context.schema_version,
        "exporter_version": context.exporter_version,
        "manifest_uri": str(context.manifest_path.resolve()),
        "manifest_sha256": context.manifest_sha256,
        "native_generation_id": None,
        "native_manifest_sha256": None,
        "state": "SEALED",
        "sealed_at": "2026-07-30T01:00:00.000000",
    }
    values.update(changes)
    return InputGeneration(**values)


def _case() -> ExpectedSignalCase:
    return ExpectedSignalCase(
        registry_scheme_id="demo__h5__10Y",
        base_scheme_id="demo",
        runtime_type="native_adapter",
        frequency="daily",
        task_type="T+5",
        target_tenor="10Y",
        horizon=5,
        predict_date="2026-07-28",
        feature_date="2026-07-27",
        target_date="2026-08-03",
        segment="live",
    )


class SignalGapNativeArtifactPrepareTests(unittest.TestCase):
    def test_prepare_uses_real_capture_date_and_historical_feature_cutoff(
        self,
    ) -> None:
        engine = _Engine()
        patches, _ = _patched_source_readers(
            native_module,
            engine.connection,
            evidence_timestamp="2026-07-30T00:15:00.000000",
        )
        snapshot_now = datetime(
            2026,
            7,
            30,
            1,
            0,
            tzinfo=timezone.utc,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                context = (
                    native_module.create_signal_gap_native_artifact(
                        engine,
                        capture_business_date="2026-07-30",
                        feature_date="2026-07-24",
                        output_root=Path(tmpdir),
                        _snapshot_clock=lambda: snapshot_now,
                    )
                )

            self.assertEqual(context.business_date, "2026-07-30")
            self.assertEqual(context.feature_date, "2026-07-24")
            self.assertEqual(
                context.exporter_version,
                native_module.SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
            )
            self.assertEqual(
                context.cutoffs["daily"],
                "2026-07-24",
            )

    def test_prepare_rejects_backdated_capture_business_date(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "capture_business_date must equal current",
        ):
            native_module.create_signal_gap_native_artifact(
                object(),
                capture_business_date="2026-07-29",
                feature_date="2026-07-27",
                output_root=Path("/not-used"),
                _snapshot_clock=lambda: datetime(
                    2026,
                    7,
                    30,
                    1,
                    0,
                    tzinfo=timezone.utc,
                ),
            )

    def test_prepare_rejects_relative_storage_root_before_database(
        self,
    ) -> None:
        engine_factory = Mock()
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            prepare_signal_gap_native_artifact(
                capture_business_date="2026-07-30",
                feature_date="2026-07-27",
                output_root=Path("relative-native-root"),
                engine_factory=engine_factory,
            )
        engine_factory.assert_not_called()


class SignalGapNativeArtifactAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.used_path = Path(self.tempdir.name) / "used.json"
        self.secret = patch.dict(
            os.environ,
            {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
            clear=False,
        )
        self.secret.start()

    def tearDown(self) -> None:
        self.secret.stop()
        self.tempdir.cleanup()

    def test_register_token_binds_complete_artifact_provenance(self) -> None:
        authority = {
            "authority_type": "native_current_snapshot_artifact",
            "purpose": "signal_gap_gray_live_current_snapshot",
            "artifact_id": "native-" + "a" * 24,
            "manifest_sha256": _sha("manifest"),
            "dataset_content_id": _sha("content"),
            "source_commit_token": _sha("evidence"),
            "capture_business_date": "2026-07-30",
            "feature_date": "2026-07-27",
            "exporter_version":
                native_module.SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
            "vintage_disclaimer":
                "current_snapshot_as_of_not_historical_vintage",
        }
        token = issue_signal_gap_native_artifact_register_token(
            historical_predict_date="2026-07-28",
            source_authority=authority,
        )

        auth, errors = (
            verify_signal_gap_native_artifact_register_authorization(
                token,
                historical_predict_date="2026-07-28",
                source_authority=authority,
                used_store_path=self.used_path,
            )
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)
        self.assertEqual(auth.source_authority, authority)

        _, drift_errors = (
            verify_signal_gap_native_artifact_register_authorization(
                token,
                historical_predict_date="2026-07-28",
                source_authority={
                    **authority,
                    "dataset_content_id": _sha("drift"),
                },
                used_store_path=self.used_path,
            )
        )
        self.assertIn("source authority mismatch", drift_errors)

    def test_register_token_cannot_be_issued_without_hmac_secret(
        self,
    ) -> None:
        authority = {
            "authority_type": "native_current_snapshot_artifact",
            "purpose": "signal_gap_gray_live_current_snapshot",
            "artifact_id": "native-" + "a" * 24,
            "manifest_sha256": _sha("manifest"),
            "dataset_content_id": _sha("content"),
            "source_commit_token": _sha("evidence"),
            "capture_business_date": "2026-07-30",
            "feature_date": "2026-07-27",
            "exporter_version":
                native_module.SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
            "vintage_disclaimer":
                "current_snapshot_as_of_not_historical_vintage",
        }
        with (
            patch.dict(
                os.environ,
                {"HARNESS_AUTH_SECRET": ""},
                clear=False,
            ),
            self.assertRaisesRegex(ValueError, "requires HMAC signing"),
        ):
            issue_signal_gap_native_artifact_register_token(
                historical_predict_date="2026-07-28",
                source_authority=authority,
            )

    def test_auth_issue_cli_binds_source_authority_json(self) -> None:
        authority = {
            "authority_type": "native_current_snapshot_artifact",
            "purpose": "signal_gap_gray_live_current_snapshot",
            "artifact_id": "native-" + "a" * 24,
            "manifest_sha256": _sha("manifest"),
            "dataset_content_id": _sha("content"),
            "source_commit_token": _sha("evidence"),
            "capture_business_date": "2026-07-30",
            "feature_date": "2026-07-27",
            "exporter_version":
                native_module.SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
            "vintage_disclaimer":
                "current_snapshot_as_of_not_historical_vintage",
        }
        authority_path = Path(self.tempdir.name) / "authority.json"
        authority_path.write_text(
            json.dumps(authority),
            encoding="utf-8",
        )
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "auth",
                    "issue",
                    "--scheme-id",
                    "signal-gap-native-artifact",
                    "--action",
                    "signal_gap_native_artifact_register",
                    "--predict-date",
                    "2026-07-28",
                    "--source-authority-json",
                    str(authority_path),
                ]
            )

        self.assertEqual(exit_code, 0)
        token = stdout.getvalue().strip()
        auth, errors = (
            verify_signal_gap_native_artifact_register_authorization(
                token,
                historical_predict_date="2026-07-28",
                source_authority=authority,
                used_store_path=self.used_path,
            )
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(auth)


class SignalGapNativeArtifactRepositoryTests(unittest.TestCase):
    def _engine(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE t_input_generations (
                        generation_id TEXT PRIMARY KEY,
                        generation_type TEXT NOT NULL,
                        business_date TEXT NOT NULL,
                        feature_date TEXT NOT NULL,
                        readiness_basis TEXT NOT NULL,
                        source_commit_token TEXT NOT NULL,
                        dataset_content_id TEXT NOT NULL,
                        schema_version TEXT NOT NULL,
                        exporter_version TEXT NOT NULL,
                        manifest_uri TEXT NOT NULL,
                        manifest_sha256 TEXT NOT NULL,
                        native_generation_id TEXT,
                        native_manifest_sha256 TEXT,
                        state TEXT NOT NULL,
                        sealed_at TEXT,
                        invalidated_at TEXT,
                        invalid_reason TEXT,
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
            )
            for table in (
                "t_schedule_occurrences",
                "t_schedule_items",
                "t_schedule_item_targets",
            ):
                conn.execute(
                    text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                )
        return engine

    def _registration(self, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "generation_id": "native-" + "a" * 24,
            "generation_type": "native_source",
            "business_date": "2026-07-30",
            "feature_date": "2026-07-27",
            "readiness_basis": "CLOCK_CONTRACT",
            "source_commit_token": _sha("evidence"),
            "dataset_content_id": _sha("content"),
            "schema_version": "native-generation-v1",
            "exporter_version":
                native_module.SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
            "manifest_uri": "/private/native-a/manifest.json",
            "manifest_sha256": _sha("manifest"),
        }
        values.update(changes)
        return values

    def test_register_is_atomic_idempotent_and_has_zero_ledger_side_effects(
        self,
    ) -> None:
        engine = self._engine()
        registration = self._registration()

        first = register_sealed_gray_gap_native_generation(
            engine,
            historical_predict_date="2026-07-28",
            **registration,
        )
        second = register_sealed_gray_gap_native_generation(
            engine,
            historical_predict_date="2026-07-28",
            **registration,
        )

        self.assertEqual(first, registration["generation_id"])
        self.assertEqual(second, registration["generation_id"])
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT state, business_date, feature_date,
                           exporter_version, sealed_at
                    FROM t_input_generations
                    """
                )
            ).mappings().one()
            self.assertEqual(row["state"], "SEALED")
            self.assertEqual(row["business_date"], "2026-07-30")
            self.assertEqual(row["feature_date"], "2026-07-27")
            self.assertIsNotNone(row["sealed_at"])
            for table in (
                "t_schedule_occurrences",
                "t_schedule_items",
                "t_schedule_item_targets",
            ):
                self.assertEqual(
                    conn.execute(
                        text(f"SELECT COUNT(*) FROM {table}")
                    ).scalar_one(),
                    0,
                )
        engine.dispose()

    def test_register_rejects_second_identity_for_same_feature(self) -> None:
        engine = self._engine()
        register_sealed_gray_gap_native_generation(
            engine,
            historical_predict_date="2026-07-28",
            **self._registration(),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "unique SEALED authority",
        ):
            register_sealed_gray_gap_native_generation(
                engine,
                historical_predict_date="2026-07-28",
                **self._registration(
                    generation_id="native-" + "b" * 24,
                    dataset_content_id=_sha("other-content"),
                ),
            )
        engine.dispose()

    def test_register_rejects_non_clock_snapshot_authority(self) -> None:
        engine = self._engine()
        with self.assertRaisesRegex(ValueError, "CLOCK_CONTRACT"):
            register_sealed_gray_gap_native_generation(
                engine,
                historical_predict_date="2026-07-28",
                **self._registration(readiness_basis="UPSTREAM_SEAL"),
            )
        with engine.connect() as conn:
            self.assertEqual(
                conn.execute(
                    text("SELECT COUNT(*) FROM t_input_generations")
                ).scalar_one(),
                0,
            )
        engine.dispose()


class SignalGapNativeArtifactPlannerTests(unittest.TestCase):
    def test_planner_accepts_only_unique_later_capture_authority(self) -> None:
        row = InputGeneration(
            generation_id="native-" + "a" * 24,
            generation_type="native_source",
            business_date="2026-07-30",
            feature_date="2026-07-27",
            readiness_basis="CLOCK_CONTRACT",
            source_commit_token=_sha("evidence"),
            dataset_content_id=_sha("content"),
            schema_version="native-generation-v1",
            exporter_version=
                native_module.SIGNAL_GAP_NATIVE_EXPORTER_VERSION,
            manifest_uri="/private/native-a/manifest.json",
            manifest_sha256=_sha("manifest"),
            native_generation_id=None,
            native_manifest_sha256=None,
            state="SEALED",
            sealed_at="2026-07-30T01:00:00.000000",
        )
        authority = {
            "database": {
                "generation_id": row.generation_id,
                "dataset_content_id": row.dataset_content_id,
            },
            "artifact": {
                "generation_id": row.generation_id,
                "manifest_sha256": row.manifest_sha256,
                "source_commit_token": row.source_commit_token,
            },
        }

        action, observed, reason = _native_generation_eligibility(
            _case(),
            (row,),
            artifact_verifier=lambda _row: (authority, None),
        )
        self.assertEqual(action, "GRAY_LIVE_GAP")
        self.assertEqual(observed, authority)
        self.assertEqual(reason, "LIVE_BUSINESS_KEY_MISSING")

        blocked, _, blocked_reason = _native_generation_eligibility(
            _case(),
            (
                replace(row, business_date="2026-07-28"),
            ),
            artifact_verifier=lambda _row: (authority, None),
        )
        self.assertEqual(blocked, "BLOCKED_NO_GENERATION")
        self.assertEqual(
            blocked_reason,
            "NO_EXACT_NATIVE_GENERATION",
        )

        eligible, _, _ = _native_generation_eligibility(
            _case(),
            (
                row,
                replace(
                    row,
                    generation_id="native-" + "b" * 24,
                    exporter_version="native-generation-exporter-v1",
                ),
            ),
            artifact_verifier=lambda _row: (authority, None),
        )
        self.assertEqual(eligible, "GRAY_LIVE_GAP")


class SignalGapNativeArtifactRegistryTests(unittest.TestCase):
    def test_registry_rehashes_before_standalone_registration(self) -> None:
        context = native_module.NativeGenerationContext(
            generation_id="native-" + "a" * 24,
            generation_type="native_source",
            dataset_content_id=_sha("content"),
            root_dir=Path("/private/native-a"),
            manifest_path=Path("/private/native-a/manifest.json"),
            manifest_sha256=_sha("manifest"),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            source_commit_token=_sha("evidence"),
            readiness_basis="CLOCK_CONTRACT",
            schema_version="native-generation-v1",
            exporter_version=(
                native_module.SIGNAL_GAP_NATIVE_EXPORTER_VERSION
            ),
            created_at="2026-07-30T01:00:00.000000Z",
            sealed_at="2026-07-30T01:01:00.000000Z",
            _cutoffs_json="{}",
            _manifest_json="{}",
            _frames={},
        )

        with (
            patch(
                "scheduler.generation_registry.open_native_generation",
                return_value=context,
            ) as opener,
            patch(
                "scheduler.generation_registry."
                "register_sealed_gray_gap_native_generation",
                return_value=context.generation_id,
            ) as atomic_register,
        ):
            result = register_gray_gap_native_artifact(
                object(),
                context,
                historical_predict_date="2026-07-28",
            )

        self.assertEqual(result, context.generation_id)
        opener.assert_called_once()
        atomic_register.assert_called_once()
        self.assertNotIn(
            "occurrence_id",
            atomic_register.call_args.kwargs,
        )

    def test_register_rejects_invalid_hmac_before_opening_database(
        self,
    ) -> None:
        context = native_module.NativeGenerationContext(
            generation_id="native-" + "a" * 24,
            generation_type="native_source",
            dataset_content_id=_sha("content"),
            root_dir=Path("/private/native-a"),
            manifest_path=Path("/private/native-a/manifest.json"),
            manifest_sha256=_sha("manifest"),
            business_date="2026-07-30",
            feature_date="2026-07-27",
            source_commit_token=_sha("evidence"),
            readiness_basis="CLOCK_CONTRACT",
            schema_version="native-generation-v1",
            exporter_version=(
                native_module.SIGNAL_GAP_NATIVE_EXPORTER_VERSION
            ),
            created_at="2026-07-30T01:00:00.000000Z",
            sealed_at="2026-07-30T01:01:00.000000Z",
            _cutoffs_json="{}",
            _manifest_json="{}",
            _frames={},
        )
        engine_factory = Mock()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch(
                "harness.signal_gap_native_artifact."
                "open_native_generation",
                return_value=context,
            ),
            patch.dict(
                os.environ,
                {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                clear=False,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "authorization failed",
            ),
        ):
            register_signal_gap_native_artifact(
                manifest=Path(tmpdir),
                historical_predict_date="2026-07-28",
                authorize="invalid-token",
                project_root=Path(tmpdir),
                engine_factory=engine_factory,
            )
        engine_factory.assert_not_called()

    def test_register_rejects_relative_manifest_before_artifact_open(
        self,
    ) -> None:
        engine_factory = Mock()
        with (
            patch(
                "harness.signal_gap_native_artifact."
                "open_native_generation",
            ) as opener,
            self.assertRaisesRegex(ValueError, "must be absolute"),
        ):
            register_signal_gap_native_artifact(
                manifest=Path("relative/manifest.json"),
                historical_predict_date="2026-07-28",
                authorize="not-used",
                project_root=Path("/private/project"),
                engine_factory=engine_factory,
            )
        opener.assert_not_called()
        engine_factory.assert_not_called()


class SignalGapNativeArtifactCliTests(unittest.TestCase):
    def test_prepare_cli_has_explicit_capture_and_feature_scope(self) -> None:
        stdout = io.StringIO()
        with (
            patch(
                "harness.cli.prepare_signal_gap_native_artifact",
                return_value={"status": "PREPARED"},
            ) as prepare,
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "signal-gap-native-artifact",
                    "prepare",
                    "--capture-business-date",
                    "2026-07-30",
                    "--feature-date",
                    "2026-07-27",
                    "--output-root",
                    "/private/native",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("PREPARED", stdout.getvalue())
        self.assertEqual(
            prepare.call_args.kwargs["capture_business_date"],
            "2026-07-30",
        )
        self.assertEqual(
            prepare.call_args.kwargs["feature_date"],
            "2026-07-27",
        )

    def test_register_cli_requires_manifest_predict_date_and_token(self) -> None:
        stdout = io.StringIO()
        with (
            patch(
                "harness.cli.register_signal_gap_native_artifact",
                return_value={"status": "REGISTERED"},
            ) as register,
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "signal-gap-native-artifact",
                    "register",
                    "--manifest",
                    "/private/native/manifest.json",
                    "--historical-predict-date",
                    "2026-07-28",
                    "--authorize",
                    "signed-token",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("REGISTERED", stdout.getvalue())
        self.assertEqual(
            register.call_args.kwargs["historical_predict_date"],
            "2026-07-28",
        )
        self.assertEqual(
            register.call_args.kwargs["authorize"],
            "signed-token",
        )


if __name__ == "__main__":
    unittest.main()
