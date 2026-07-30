from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text

from harness.authorization import (
    authorization_token_hash,
    issue_signal_gap_native_artifact_register_token,
    used_tokens_path,
    verify_signal_gap_native_artifact_register_authorization,
)
from harness.cli import main
from harness.signal_gap_native_artifact import (
    SignalGapNativeArtifactRegistrationError,
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
from scheduler.daily_runtime import DefaultDailyRuntimeServices
from scheduler.repository import (
    create_input_generation,
    register_seal_and_bind_schedule_occurrence_generation,
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
        "manifest_uri": str(context.manifest_path),
        "manifest_sha256": context.manifest_sha256,
        "storage_root_identity": _sha("storage-root"),
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

    def test_prepare_requires_precreated_private_storage_root(self) -> None:
        engine_factory = Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            parent = Path(tmpdir).resolve()
            missing = parent / "missing-signal-gap-root"
            with self.assertRaisesRegex(ValueError, "is missing"):
                prepare_signal_gap_native_artifact(
                    capture_business_date="2026-07-30",
                    feature_date="2026-07-27",
                    output_root=missing,
                    engine_factory=engine_factory,
                )
            public_root = parent / "public-signal-gap-root"
            public_root.mkdir(mode=0o700)
            public_root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                prepare_signal_gap_native_artifact(
                    capture_business_date="2026-07-30",
                    feature_date="2026-07-27",
                    output_root=public_root,
                    engine_factory=engine_factory,
                )
        engine_factory.assert_not_called()

    def test_open_rejects_symlinked_dedicated_storage_root(self) -> None:
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
            parent = Path(tmpdir).resolve()
            storage_root = parent / "signal-gap-native"
            storage_root.mkdir(mode=0o700)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                context = (
                    native_module.create_signal_gap_native_artifact(
                        engine,
                        capture_business_date="2026-07-30",
                        feature_date="2026-07-24",
                        output_root=storage_root,
                        _snapshot_clock=lambda: snapshot_now,
                    )
                )
            alias = parent / "signal-gap-native-alias"
            alias.symlink_to(storage_root, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                native_module.open_signal_gap_native_artifact(
                    alias
                    / context.generation_id
                    / "manifest.json",
                    storage_root=alias,
                )


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
            "manifest_uri":
                "/private/native-gap/"
                f"native-{'a' * 24}/manifest.json",
            "manifest_sha256": _sha("manifest"),
            "storage_root_identity": _sha("storage-root"),
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

        _, copied_errors = (
            verify_signal_gap_native_artifact_register_authorization(
                token,
                historical_predict_date="2026-07-28",
                source_authority={
                    **authority,
                    "manifest_uri":
                        "/private/native-gap-copy/"
                        f"native-{'a' * 24}/manifest.json",
                    "storage_root_identity": _sha(
                        "copied-storage-root"
                    ),
                },
                used_store_path=self.used_path,
            )
        )
        self.assertIn("source authority mismatch", copied_errors)

    def test_register_token_cannot_be_issued_without_hmac_secret(
        self,
    ) -> None:
        authority = {
            "authority_type": "native_current_snapshot_artifact",
            "purpose": "signal_gap_gray_live_current_snapshot",
            "artifact_id": "native-" + "a" * 24,
            "manifest_uri":
                "/private/native-gap/"
                f"native-{'a' * 24}/manifest.json",
            "manifest_sha256": _sha("manifest"),
            "storage_root_identity": _sha("storage-root"),
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
            "manifest_uri":
                "/private/native-gap/"
                f"native-{'a' * 24}/manifest.json",
            "manifest_sha256": _sha("manifest"),
            "storage_root_identity": _sha("storage-root"),
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

    def test_generic_occurrence_registration_rejects_gap_exporter(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "daily ledger"):
            register_seal_and_bind_schedule_occurrence_generation(
                object(),
                occurrence_id=1,
                expected_feature_date="2026-07-29",
                **self._registration(
                    business_date="2026-07-30",
                    feature_date="2026-07-29",
                ),
            )

    def test_generic_generation_creation_rejects_gap_exporter(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "daily ledger"):
            create_input_generation(
                object(),
                **self._registration(),
            )

    def test_daily_recovery_query_ignores_two_gap_artifacts(
        self,
    ) -> None:
        engine = self._engine()
        rows = (
            self._registration(
                generation_id="native-" + "a" * 24,
                feature_date="2026-07-27",
            ),
            self._registration(
                generation_id="native-" + "b" * 24,
                feature_date="2026-07-28",
                dataset_content_id=_sha("gap-two"),
                manifest_sha256=_sha("gap-two-manifest"),
            ),
            self._registration(
                generation_id="native-" + "c" * 24,
                feature_date="2026-07-29",
                exporter_version=
                    native_module.NATIVE_GENERATION_EXPORTER_VERSION,
                dataset_content_id=_sha("ledger"),
                manifest_sha256=_sha("ledger-manifest"),
            ),
        )
        with engine.begin() as conn:
            for row in rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO t_input_generations
                            (generation_id, generation_type, business_date,
                             feature_date, readiness_basis,
                             source_commit_token, dataset_content_id,
                             schema_version, exporter_version, manifest_uri,
                             manifest_sha256, state, sealed_at)
                        VALUES
                            (:generation_id, :generation_type, :business_date,
                             :feature_date, :readiness_basis,
                             :source_commit_token, :dataset_content_id,
                             :schema_version, :exporter_version, :manifest_uri,
                             :manifest_sha256, 'SEALED',
                             '2026-07-30T01:00:00.000000')
                        """
                    ),
                    row,
                )
        services = DefaultDailyRuntimeServices(engine=engine)

        fences = services.find_generation_fences(
            business_date=datetime(2026, 7, 30).date(),
            generation_type="native_source",
        )

        self.assertEqual(
            [row["generation_id"] for row in fences],
            ["native-" + "c" * 24],
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
    @staticmethod
    def _context(storage_root: Path):
        generation_id = "native-" + "a" * 24
        return native_module.NativeGenerationContext(
            generation_id=generation_id,
            generation_type="native_source",
            dataset_content_id=_sha("content"),
            root_dir=storage_root / generation_id,
            manifest_path=(
                storage_root / generation_id / "manifest.json"
            ),
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
                "scheduler.generation_registry."
                "open_signal_gap_native_artifact",
                return_value=(context, _sha("storage-root")),
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
                storage_root=Path("/private/native-a"),
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
                "open_signal_gap_native_artifact",
                return_value=(context, _sha("storage-root")),
            ),
            patch(
                "harness.signal_gap_native_artifact.PROJECT_ROOT",
                Path(tmpdir).resolve(),
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
                storage_root=Path(tmpdir),
                engine_factory=engine_factory,
            )
        engine_factory.assert_not_called()

    def test_register_rejects_relative_manifest_before_artifact_open(
        self,
    ) -> None:
        engine_factory = Mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_root = Path(tmpdir).resolve()
            storage_root.chmod(0o700)
            with self.assertRaisesRegex(ValueError, "must be absolute"):
                register_signal_gap_native_artifact(
                    manifest=Path("relative/manifest.json"),
                    historical_predict_date="2026-07-28",
                    authorize="not-used",
                    storage_root=storage_root,
                    engine_factory=engine_factory,
                )
        engine_factory.assert_not_called()

    def test_db_failure_consumes_token_and_preserves_attempt_audits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve()
            storage_root = project_root / "signal-gap-native"
            storage_root.mkdir(mode=0o700)
            context = self._context(storage_root)
            copied_root = project_root / "copied-signal-gap-native"
            copied_root.mkdir(mode=0o700)
            copied_context = replace(
                context,
                root_dir=copied_root / context.generation_id,
                manifest_path=(
                    copied_root
                    / context.generation_id
                    / "manifest.json"
                ),
            )
            authority = _artifact_authority(context)
            with patch.dict(
                os.environ,
                {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                clear=False,
            ):
                first_token = (
                    issue_signal_gap_native_artifact_register_token(
                        historical_predict_date="2026-07-28",
                        source_authority=authority,
                    )
                )
                second_token = (
                    issue_signal_gap_native_artifact_register_token(
                        historical_predict_date="2026-07-28",
                        source_authority=authority,
                    )
                )
            engine_factory = Mock(
                side_effect=[
                    SimpleNamespace(dispose=Mock()),
                    SimpleNamespace(dispose=Mock()),
                ]
            )
            database_register = Mock(
                side_effect=[
                    RuntimeError("simulated database failure"),
                    context.generation_id,
                ]
            )

            with (
                patch.dict(
                    os.environ,
                    {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                    clear=False,
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "open_signal_gap_native_artifact",
                    side_effect=[
                        (context, _sha("storage-root")),
                        (
                            copied_context,
                            _sha("copied-storage-root"),
                        ),
                        (context, _sha("storage-root")),
                    ],
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "register_gray_gap_native_artifact",
                    database_register,
                ),
                patch(
                    "harness.signal_gap_native_artifact.PROJECT_ROOT",
                    project_root,
                ),
            ):
                with self.assertRaisesRegex(
                    SignalGapNativeArtifactRegistrationError,
                    "after token consumption",
                ) as failed:
                    register_signal_gap_native_artifact(
                        manifest=context.manifest_path,
                        historical_predict_date="2026-07-28",
                        authorize=first_token,
                        storage_root=storage_root,
                        engine_factory=engine_factory,
                    )
                self.assertTrue(failed.exception.token_consumed)
                first_audit = (
                    project_root
                    / "reports"
                    / "harness"
                    / "signal-gap-native-artifact"
                    / context.generation_id
                    / authorization_token_hash(first_token)
                    / "outcome.json"
                )
                first_outcome = json.loads(
                    first_audit.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    first_outcome["status"],
                    "DB_FAILED",
                )
                self.assertTrue(
                    first_outcome["authorization_consumed"]
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "token already used",
                ):
                    register_signal_gap_native_artifact(
                        manifest=context.manifest_path,
                        historical_predict_date="2026-07-28",
                        authorize=first_token,
                        storage_root=copied_root,
                        engine_factory=engine_factory,
                    )

                succeeded = register_signal_gap_native_artifact(
                    manifest=context.manifest_path,
                    historical_predict_date="2026-07-28",
                    authorize=second_token,
                    storage_root=storage_root,
                    engine_factory=engine_factory,
                )

            self.assertEqual(
                succeeded["status"],
                "REGISTERED_SEALED",
            )
            self.assertEqual(database_register.call_count, 2)
            self.assertEqual(engine_factory.call_count, 2)
            self.assertEqual(
                json.loads(first_audit.read_text(encoding="utf-8"))[
                    "status"
                ],
                "DB_FAILED",
            )
            second_audit = (
                project_root
                / "reports"
                / "harness"
                / "signal-gap-native-artifact"
                / context.generation_id
                / authorization_token_hash(second_token)
                / "outcome.json"
            )
            self.assertEqual(
                json.loads(second_audit.read_text(encoding="utf-8"))[
                    "status"
                ],
                "REGISTERED",
            )

    def test_consumed_token_audit_write_failure_reports_consumed(
        self,
    ) -> None:
        from harness.authorization import (
            _atomic_write_json as real_atomic_write_json,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve()
            storage_root = project_root / "signal-gap-native"
            storage_root.mkdir(mode=0o700)
            context = self._context(storage_root)
            authority = _artifact_authority(context)
            with patch.dict(
                os.environ,
                {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                clear=False,
            ):
                token = (
                    issue_signal_gap_native_artifact_register_token(
                        historical_predict_date="2026-07-28",
                        source_authority=authority,
                    )
                )
            write_count = 0

            def fail_after_consumption(path, payload):
                nonlocal write_count
                write_count += 1
                if write_count == 2:
                    raise OSError("simulated audit I/O failure")
                return real_atomic_write_json(path, payload)

            engine_factory = Mock()
            with (
                patch.dict(
                    os.environ,
                    {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                    clear=False,
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "open_signal_gap_native_artifact",
                    return_value=(
                        context,
                        _sha("storage-root"),
                    ),
                ),
                patch(
                    "harness.signal_gap_native_artifact.PROJECT_ROOT",
                    project_root,
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "_atomic_write_json",
                    side_effect=fail_after_consumption,
                ),
                self.assertRaises(
                    SignalGapNativeArtifactRegistrationError,
                ) as failed,
            ):
                register_signal_gap_native_artifact(
                    manifest=context.manifest_path,
                    historical_predict_date="2026-07-28",
                    authorize=token,
                    storage_root=storage_root,
                    engine_factory=engine_factory,
                )

            self.assertEqual(
                failed.exception.failure_code,
                "POST_CONSUMPTION_AUDIT_FAILED",
            )
            self.assertTrue(failed.exception.token_consumed)
            engine_factory.assert_not_called()
            _, replay_errors = (
                verify_signal_gap_native_artifact_register_authorization(
                    token,
                    historical_predict_date="2026-07-28",
                    source_authority=authority,
                    used_store_path=used_tokens_path(project_root),
                )
            )
            self.assertIn(
                "authorization token already used",
                replay_errors,
            )

    def test_release_failure_with_exact_readback_is_sealed_after_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve()
            storage_root = project_root / "signal-gap-native"
            storage_root.mkdir(mode=0o700)
            context = self._context(storage_root)
            authority = _artifact_authority(context)
            with patch.dict(
                os.environ,
                {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                clear=False,
            ):
                token = (
                    issue_signal_gap_native_artifact_register_token(
                        historical_predict_date="2026-07-28",
                        source_authority=authority,
                    )
                )
            engine = SimpleNamespace(dispose=Mock())
            sealed_row = SimpleNamespace(
                generation_id=context.generation_id,
                generation_type=context.generation_type,
                business_date=context.business_date,
                feature_date=context.feature_date,
                readiness_basis=context.readiness_basis,
                source_commit_token=context.source_commit_token,
                dataset_content_id=context.dataset_content_id,
                schema_version=context.schema_version,
                exporter_version=context.exporter_version,
                manifest_uri=str(context.manifest_path),
                manifest_sha256=context.manifest_sha256,
                native_generation_id=None,
                native_manifest_sha256=None,
                state="SEALED",
                sealed_at=datetime(
                    2026,
                    7,
                    30,
                    1,
                    5,
                ),
            )
            with (
                patch.dict(
                    os.environ,
                    {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                    clear=False,
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "open_signal_gap_native_artifact",
                    return_value=(
                        context,
                        _sha("storage-root"),
                    ),
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "register_gray_gap_native_artifact",
                    side_effect=RuntimeError(
                        "RELEASE_LOCK failed after commit"
                    ),
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "read_sealed_input_generation",
                    return_value=sealed_row,
                ) as readback,
                patch(
                    "harness.signal_gap_native_artifact.PROJECT_ROOT",
                    project_root,
                ),
            ):
                result = register_signal_gap_native_artifact(
                    manifest=context.manifest_path,
                    historical_predict_date="2026-07-28",
                    authorize=token,
                    storage_root=storage_root,
                    engine_factory=lambda: engine,
                )

            self.assertEqual(
                result["status"],
                "REGISTERED_SEALED_AFTER_ERROR",
            )
            readback.assert_called_once()
            engine.dispose.assert_called_once()
            outcome = json.loads(
                Path(
                    result["authorization_audit_path"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                outcome["status"],
                "SEALED_AFTER_ERROR",
            )
            self.assertEqual(
                outcome["database_outcome"],
                "SEALED",
            )

    def test_final_audit_failure_reports_database_already_sealed(
        self,
    ) -> None:
        from harness.authorization import (
            _atomic_write_json as real_atomic_write_json,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir).resolve()
            storage_root = project_root / "signal-gap-native"
            storage_root.mkdir(mode=0o700)
            context = self._context(storage_root)
            authority = _artifact_authority(context)
            with patch.dict(
                os.environ,
                {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                clear=False,
            ):
                token = (
                    issue_signal_gap_native_artifact_register_token(
                        historical_predict_date="2026-07-28",
                        source_authority=authority,
                    )
                )
            write_count = 0

            def fail_final_audit(path, payload):
                nonlocal write_count
                write_count += 1
                if write_count == 3:
                    raise OSError("simulated final audit failure")
                return real_atomic_write_json(path, payload)

            engine = SimpleNamespace(dispose=Mock())
            with (
                patch.dict(
                    os.environ,
                    {"HARNESS_AUTH_SECRET": "native-gap-test-secret"},
                    clear=False,
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "open_signal_gap_native_artifact",
                    return_value=(
                        context,
                        _sha("storage-root"),
                    ),
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "register_gray_gap_native_artifact",
                    return_value=context.generation_id,
                ),
                patch(
                    "harness.signal_gap_native_artifact.PROJECT_ROOT",
                    project_root,
                ),
                patch(
                    "harness.signal_gap_native_artifact."
                    "_atomic_write_json",
                    side_effect=fail_final_audit,
                ),
                self.assertRaises(
                    SignalGapNativeArtifactRegistrationError,
                ) as failed,
            ):
                register_signal_gap_native_artifact(
                    manifest=context.manifest_path,
                    historical_predict_date="2026-07-28",
                    authorize=token,
                    storage_root=storage_root,
                    engine_factory=lambda: engine,
                )

            self.assertEqual(
                failed.exception.failure_code,
                "DATABASE_SEALED_AUDIT_FAILED",
            )
            self.assertTrue(failed.exception.token_consumed)
            engine.dispose.assert_called_once()


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
                    "--storage-root",
                    "/private/native-gap",
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
        self.assertEqual(
            register.call_args.kwargs["storage_root"],
            Path("/private/native-gap"),
        )
        self.assertNotIn(
            "project_root",
            register.call_args.kwargs,
        )
        self.assertNotIn(
            "project_root",
            inspect.signature(
                register_signal_gap_native_artifact
            ).parameters,
        )

    def test_register_cli_reports_consumed_token_and_audit_path(
        self,
    ) -> None:
        stdout = io.StringIO()
        audit_path = Path("/private/audit/outcome.json")
        failure = SignalGapNativeArtifactRegistrationError(
            "database registration failed after token consumption",
            failure_code="DATABASE_REGISTRATION_FAILED",
            token_consumed=True,
            audit_path=audit_path,
        )
        with (
            patch(
                "harness.cli.register_signal_gap_native_artifact",
                side_effect=failure,
            ),
            redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "signal-gap-native-artifact",
                    "register",
                    "--manifest",
                    "/private/native-gap/"
                    f"native-{'a' * 24}/manifest.json",
                    "--historical-predict-date",
                    "2026-07-28",
                    "--authorize",
                    "signed-token",
                    "--storage-root",
                    "/private/native-gap",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(
            payload["failure_code"],
            "DATABASE_REGISTRATION_FAILED",
        )
        self.assertTrue(payload["token_consumed"])
        self.assertEqual(payload["audit_path"], str(audit_path))

    def test_register_cli_rejects_caller_project_root(self) -> None:
        stderr = io.StringIO()
        with (
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            main(
                [
                    "signal-gap-native-artifact",
                    "register",
                    "--manifest",
                    "/private/native-gap/"
                    f"native-{'a' * 24}/manifest.json",
                    "--historical-predict-date",
                    "2026-07-28",
                    "--authorize",
                    "signed-token",
                    "--storage-root",
                    "/private/native-gap",
                    "--project-root",
                    "/tmp/fake-project-root",
                ]
            )
        self.assertIn("unrecognized arguments", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
