from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _scheme_root(root: Path) -> Path:
    destination = root / "schemes" / "daily_5y_2_v28"
    destination.mkdir(parents=True)
    source = PROJECT_ROOT / "schemes" / "daily_5y_2_v28"
    shutil.copy2(source / "config.yaml", destination / "config.yaml")
    shutil.copy2(source / "predict.py", destination / "predict.py")
    return destination


def _expectation(
    *,
    cfg,
    runtime_type: str = "native_adapter",
):
    from scheduler.repository import ScheduledCompletionExpectation

    return ScheduledCompletionExpectation(
        run_id=901,
        item_id=801,
        occurrence_id=701,
        base_scheme_id=cfg.scheme_id,
        runtime_type=runtime_type,
        generation_id=(
            "native-0123456789abcdef01234567"
            if runtime_type == "native_adapter"
            else "databridge-0123456789abcdef0123"
        ),
        manifest_uri="/tmp/frozen-generation/manifest.json",
        manifest_sha256="a" * 64,
        generation_dataset_content_id="c" * 64,
        generation_schema_version="test-generation-v1",
        generation_exporter_version="test-exporter-v1",
        feature_date="2026-07-23",
        business_date="2026-07-24",
        scheme_version=cfg.scheme_version,
        code_sha256=cfg.code_hash,
        config_sha256=cfg.config_hash,
        native_generation_id=(
            "native-fedcba9876543210fedcba98"
            if runtime_type == "blackbox_v2"
            else None
        ),
        native_manifest_uri=(
            "/tmp/frozen-native/manifest.json"
            if runtime_type == "blackbox_v2"
            else None
        ),
        native_manifest_sha256=(
            "b" * 64
            if runtime_type == "blackbox_v2"
            else None
        ),
        native_dataset_content_id=(
            "d" * 64
            if runtime_type == "blackbox_v2"
            else None
        ),
        native_schema_version=(
            "test-native-generation-v1"
            if runtime_type == "blackbox_v2"
            else None
        ),
        native_exporter_version=(
            "test-native-exporter-v1"
            if runtime_type == "blackbox_v2"
            else None
        ),
        native_feature_date=(
            "2026-07-23"
            if runtime_type == "blackbox_v2"
            else None
        ),
        native_business_date=(
            "2026-07-24"
            if runtime_type == "blackbox_v2"
            else None
        ),
    )


class ScheduledCompletionVerifierTests(unittest.TestCase):
    def test_native_verifier_rehashes_generation_and_scheme(self) -> None:
        from scheduler.completion_verifier import (
            FilesystemScheduledCompletionVerifier,
        )
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme = _scheme_root(root)
            cfg = load_scheme_config(scheme / "config.yaml")
            expectation = _expectation(cfg=cfg)
            generation = SimpleNamespace(
                generation_id=expectation.generation_id,
                manifest_sha256=expectation.manifest_sha256,
                dataset_content_id=(
                    expectation.generation_dataset_content_id
                ),
                schema_version=expectation.generation_schema_version,
                exporter_version=expectation.generation_exporter_version,
                feature_date=expectation.feature_date,
            )
            verifier = FilesystemScheduledCompletionVerifier(
                project_root=root,
                databridge_schema_path=root / "schema.json",
            )
            with patch(
                "scheduler.completion_verifier.open_native_generation",
                return_value=generation,
            ) as open_generation:
                evidence = verifier.verify(expectation)

        open_generation.assert_called_once_with(
            Path(expectation.manifest_uri),
            expected_generation_id=expectation.generation_id,
            expected_manifest_sha256=expectation.manifest_sha256,
            expected_business_date=expectation.business_date,
            expected_feature_date=expectation.feature_date,
        )
        self.assertEqual(evidence.observed_generation_id, expectation.generation_id)
        self.assertEqual(evidence.code_sha256, expectation.code_sha256)
        self.assertEqual(evidence.config_sha256, expectation.config_sha256)
        self.assertEqual(evidence.scheme_version, expectation.scheme_version)

    def test_verifier_rejects_actual_code_tamper(self) -> None:
        from scheduler.completion_verifier import (
            FilesystemScheduledCompletionVerifier,
            ScheduledVerificationError,
        )
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme = _scheme_root(root)
            cfg = load_scheme_config(scheme / "config.yaml")
            expectation = _expectation(cfg=cfg)
            with (scheme / "predict.py").open("a", encoding="utf-8") as handle:
                handle.write("\nTAMPERED = True\n")
            generation = SimpleNamespace(
                generation_id=expectation.generation_id,
                manifest_sha256=expectation.manifest_sha256,
                dataset_content_id=(
                    expectation.generation_dataset_content_id
                ),
                schema_version=expectation.generation_schema_version,
                exporter_version=expectation.generation_exporter_version,
                feature_date=expectation.feature_date,
            )
            verifier = FilesystemScheduledCompletionVerifier(
                project_root=root,
                databridge_schema_path=root / "schema.json",
            )
            with (
                patch(
                    "scheduler.completion_verifier.open_native_generation",
                    return_value=generation,
                ),
                self.assertRaisesRegex(
                    ScheduledVerificationError,
                    "code_sha256|scheme_version",
                ),
            ):
                verifier.verify(expectation)

    def test_verifier_propagates_generation_hash_tamper_as_failure(self) -> None:
        from scheduler.completion_verifier import (
            FilesystemScheduledCompletionVerifier,
            ScheduledVerificationError,
        )
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme = _scheme_root(root)
            cfg = load_scheme_config(scheme / "config.yaml")
            expectation = _expectation(cfg=cfg)
            verifier = FilesystemScheduledCompletionVerifier(
                project_root=root,
                databridge_schema_path=root / "schema.json",
            )
            with (
                patch(
                    "scheduler.completion_verifier.open_native_generation",
                    side_effect=ValueError("manifest hash mismatch"),
                ),
                self.assertRaisesRegex(
                    ScheduledVerificationError,
                    "manifest hash mismatch",
                ),
            ):
                verifier.verify(expectation)

    def test_verifier_rejects_reopened_generation_content_identity_drift(
        self,
    ) -> None:
        from scheduler.completion_verifier import (
            FilesystemScheduledCompletionVerifier,
            ScheduledVerificationError,
        )
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = load_scheme_config(
                _scheme_root(root) / "config.yaml"
            )
            expectation = _expectation(cfg=cfg)
            generation = SimpleNamespace(
                generation_id=expectation.generation_id,
                manifest_sha256=expectation.manifest_sha256,
                dataset_content_id="f" * 64,
                schema_version=expectation.generation_schema_version,
                exporter_version=expectation.generation_exporter_version,
                feature_date=expectation.feature_date,
            )
            verifier = FilesystemScheduledCompletionVerifier(
                project_root=root,
                databridge_schema_path=root / "schema.json",
            )
            with (
                patch(
                    "scheduler.completion_verifier.open_native_generation",
                    return_value=generation,
                ),
                self.assertRaisesRegex(
                    ScheduledVerificationError,
                    "generation_dataset_content_id",
                ),
            ):
                verifier.verify(expectation)

    def test_blackbox_verifier_uses_databridge_schema_and_exact_dates(
        self,
    ) -> None:
        from scheduler.completion_verifier import (
            FilesystemScheduledCompletionVerifier,
        )
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme = _scheme_root(root)
            cfg = load_scheme_config(scheme / "config.yaml")
            expectation = _expectation(
                cfg=cfg,
                runtime_type="blackbox_v2",
            )
            schema_path = root / "schema.json"
            schema_path.write_text("{}", encoding="utf-8")
            generation = SimpleNamespace(
                generation_id=expectation.generation_id,
                manifest_sha256=expectation.manifest_sha256,
                dataset_content_id=(
                    expectation.generation_dataset_content_id
                ),
                schema_version=expectation.generation_schema_version,
                exporter_version=expectation.generation_exporter_version,
                feature_date=expectation.feature_date,
                native_generation_id=expectation.native_generation_id,
                native_manifest_sha256=expectation.native_manifest_sha256,
            )
            native_generation = SimpleNamespace(
                generation_id=expectation.native_generation_id,
                manifest_sha256=expectation.native_manifest_sha256,
                dataset_content_id=expectation.native_dataset_content_id,
                schema_version=expectation.native_schema_version,
                exporter_version=expectation.native_exporter_version,
                feature_date=expectation.native_feature_date,
            )
            blackbox_cfg = SimpleNamespace(
                scheme_id=cfg.scheme_id,
                runtime_type="blackbox_v2",
                scheme_version=cfg.scheme_version,
                code_hash=cfg.code_hash,
                config_hash=cfg.config_hash,
            )
            verifier = FilesystemScheduledCompletionVerifier(
                project_root=root,
                databridge_schema_path=schema_path,
            )
            with (
                patch(
                    "scheduler.completion_verifier.open_databridge_generation",
                    return_value=generation,
                ) as open_generation,
                patch(
                    "scheduler.completion_verifier.open_native_generation",
                    return_value=native_generation,
                ) as open_native,
                patch(
                    "scheduler.completion_verifier.load_scheme_config",
                    return_value=blackbox_cfg,
                ),
            ):
                verifier.verify(expectation)

        open_generation.assert_called_once_with(
            Path(expectation.manifest_uri),
            expected_generation_id=expectation.generation_id,
            expected_manifest_sha256=expectation.manifest_sha256,
            expected_business_date=expectation.business_date,
            expected_feature_date=expectation.feature_date,
            schema_path=schema_path.resolve(),
        )
        open_native.assert_called_once_with(
            Path(expectation.native_manifest_uri),
            expected_generation_id=expectation.native_generation_id,
            expected_manifest_sha256=expectation.native_manifest_sha256,
            expected_business_date=expectation.native_business_date,
            expected_feature_date=expectation.native_feature_date,
        )

    def test_blackbox_verifier_rejects_linked_native_exporter_drift(
        self,
    ) -> None:
        from scheduler.completion_verifier import (
            FilesystemScheduledCompletionVerifier,
            ScheduledVerificationError,
        )
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = load_scheme_config(
                _scheme_root(root) / "config.yaml"
            )
            expectation = _expectation(
                cfg=cfg,
                runtime_type="blackbox_v2",
            )
            generation = SimpleNamespace(
                generation_id=expectation.generation_id,
                manifest_sha256=expectation.manifest_sha256,
                dataset_content_id=(
                    expectation.generation_dataset_content_id
                ),
                schema_version=expectation.generation_schema_version,
                exporter_version=expectation.generation_exporter_version,
                feature_date=expectation.feature_date,
                native_generation_id=expectation.native_generation_id,
                native_manifest_sha256=(
                    expectation.native_manifest_sha256
                ),
            )
            native_generation = SimpleNamespace(
                generation_id=expectation.native_generation_id,
                manifest_sha256=expectation.native_manifest_sha256,
                dataset_content_id=expectation.native_dataset_content_id,
                schema_version=expectation.native_schema_version,
                exporter_version="wrong-native-exporter",
                feature_date=expectation.native_feature_date,
            )
            blackbox_cfg = SimpleNamespace(
                scheme_id=cfg.scheme_id,
                runtime_type="blackbox_v2",
                scheme_version=cfg.scheme_version,
                code_hash=cfg.code_hash,
                config_hash=cfg.config_hash,
            )
            verifier = FilesystemScheduledCompletionVerifier(
                project_root=root,
                databridge_schema_path=root / "schema.json",
            )
            with (
                patch(
                    "scheduler.completion_verifier."
                    "open_databridge_generation",
                    return_value=generation,
                ),
                patch(
                    "scheduler.completion_verifier.open_native_generation",
                    return_value=native_generation,
                ),
                patch(
                    "scheduler.completion_verifier.load_scheme_config",
                    return_value=blackbox_cfg,
                ),
                self.assertRaisesRegex(
                    ScheduledVerificationError,
                    "native_exporter_version",
                ),
            ):
                verifier.verify(expectation)

    def test_verifier_rejects_unknown_runtime_and_unsafe_scheme_id(self) -> None:
        from dataclasses import replace

        from scheduler.completion_verifier import (
            FilesystemScheduledCompletionVerifier,
            ScheduledVerificationError,
        )
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = load_scheme_config(
                _scheme_root(root) / "config.yaml"
            )
            verifier = FilesystemScheduledCompletionVerifier(
                project_root=root,
                databridge_schema_path=root / "schema.json",
            )
            for expectation in (
                replace(_expectation(cfg=cfg), runtime_type="legacy"),
                replace(
                    _expectation(cfg=cfg),
                    base_scheme_id="../escape",
                ),
            ):
                with self.assertRaises(ScheduledVerificationError):
                    verifier.verify(expectation)


if __name__ == "__main__":
    unittest.main()
