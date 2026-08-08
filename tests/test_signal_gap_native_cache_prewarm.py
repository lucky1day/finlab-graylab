from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PUBLISHER = "liwei_0616_10y01_full_oos_k3_div_k10"


def _context(root: Path) -> SimpleNamespace:
    generation_id = "native-" + "a" * 24
    return SimpleNamespace(
        generation_id=generation_id,
        generation_type="native_source",
        readiness_basis="CLOCK_CONTRACT",
        exporter_version="native-signal-gap-current-snapshot-v1",
        manifest_path=root / generation_id / "manifest.json",
        manifest_sha256="b" * 64,
        dataset_content_id="c" * 64,
        source_commit_token="d" * 64,
        business_date="2026-08-08",
        feature_date="2026-08-04",
        schema_version="native-generation-v1",
    )


def _native_binding(context: SimpleNamespace) -> dict[str, str]:
    return {
        "generation_id": context.generation_id,
        "manifest_sha256": context.manifest_sha256,
        "dataset_content_id": context.dataset_content_id,
        "business_date": context.business_date,
        "feature_date": context.feature_date,
        "schema_version": context.schema_version,
        "exporter_version": context.exporter_version,
    }


def _sealed_row(context: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
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
        sealed_at="2026-08-08T00:00:00+00:00",
        invalidated_at=None,
        invalid_reason=None,
    )


class SignalGapNativeCachePrewarmTests(unittest.TestCase):
    def test_describe_prewarm_derives_authority_and_cache_root(
        self,
    ) -> None:
        from harness import signal_gap_native_artifact as module

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage_root = Path(temp_dir)
            context = _context(storage_root)
            with patch.object(
                module,
                "open_signal_gap_native_artifact",
                return_value=(context, "e" * 64),
            ), patch.object(
                module,
                "_require_exact_sealed_native_generation",
            ):
                result = module.describe_signal_gap_native_cache_prewarm(
                    manifest=context.manifest_path,
                    historical_predict_date="2026-08-05",
                    publisher_scheme_id=PUBLISHER,
                    storage_root=storage_root,
                )

        self.assertEqual(result["status"], "PREWARM_AUTHORITY_READY")
        self.assertEqual(
            result["cache_root"],
            str(storage_root / ".phase-a-cache" / context.generation_id),
        )
        self.assertEqual(
            result["source_authority"]["publisher_scheme_id"],
            PUBLISHER,
        )

    def test_prewarm_authorization_binds_exact_publisher_and_artifact(
        self,
    ) -> None:
        from harness.authorization import (
            issue_signal_gap_native_cache_prewarm_token,
            verify_signal_gap_native_cache_prewarm_authorization,
        )
        from harness import signal_gap_native_artifact as module

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            context = _context(root)
            authority = module.native_cache_prewarm_authority(
                context,
                storage_root_identity="e" * 64,
                publisher_scheme_id=PUBLISHER,
                historical_predict_date="2026-08-05",
            )
            with patch.dict(
                os.environ,
                {"HARNESS_AUTH_SECRET": "test-cache-prewarm-secret"},
                clear=False,
            ):
                token = issue_signal_gap_native_cache_prewarm_token(
                    historical_predict_date="2026-08-05",
                    source_authority=authority,
                    issued_by="test-operator",
                )
                auth, errors = (
                    verify_signal_gap_native_cache_prewarm_authorization(
                        token,
                        historical_predict_date="2026-08-05",
                        source_authority=authority,
                        used_store_path=root / "used.json",
                    )
                )
                altered = {
                    **authority,
                    "publisher_scheme_id": "other_publisher",
                }
                _mismatched, mismatch_errors = (
                    verify_signal_gap_native_cache_prewarm_authorization(
                        token,
                        historical_predict_date="2026-08-05",
                        source_authority=altered,
                        used_store_path=root / "used.json",
                    )
                )

        self.assertIsNotNone(auth)
        self.assertEqual(errors, [])
        self.assertIn(
            "signal-gap Native cache prewarm authority purpose is invalid",
            mismatch_errors,
        )

    def test_prewarm_rejects_unapproved_publisher_before_consumption(
        self,
    ) -> None:
        from harness import signal_gap_native_artifact as module

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage_root = Path(temp_dir)
            context = _context(storage_root)
            auth = SimpleNamespace(token="prewarm-token")
            with (
                patch.object(
                    module,
                    "open_signal_gap_native_artifact",
                    return_value=(context, "e" * 64),
                ),
                patch.object(
                    module,
                    "_require_exact_sealed_native_generation",
                ),
                patch.object(
                    module,
                    "verify_signal_gap_native_cache_prewarm_authorization",
                    return_value=(auth, []),
                ),
                patch.object(module, "mark_token_used") as consume,
                patch.object(module, "run_configured_scheme") as runner,
            ):
                with self.assertRaisesRegex(
                    module.SignalGapNativeCachePrewarmError,
                    "not approved",
                ) as caught:
                    module.prewarm_signal_gap_native_cache(
                        manifest=context.manifest_path,
                        historical_predict_date="2026-08-05",
                        publisher_scheme_id="unapproved_publisher",
                        authorize="opaque-token",
                        storage_root=storage_root,
                    )

        self.assertEqual(caught.exception.failure_code, "PUBLISHER_NOT_APPROVED")
        consume.assert_not_called()
        runner.assert_not_called()

    def test_prewarm_runs_only_publisher_against_derived_cache_root(
        self,
    ) -> None:
        from harness import signal_gap_native_artifact as module

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage_root = Path(temp_dir)
            context = _context(storage_root)
            cache_root = (
                storage_root / ".phase-a-cache" / context.generation_id
            )
            record = SimpleNamespace(
                scheme_id=PUBLISHER,
                extra={
                    "phase_a_cache": {
                        "published": True,
                        "generation_acceptance": {
                            "status": "ACCEPTED",
                            "native_generation": _native_binding(context),
                        },
                    }
                },
            )
            cfg = SimpleNamespace(
                scheme_id=PUBLISHER,
                runtime_type="native_adapter",
            )
            auth = SimpleNamespace(
                token="prewarm-token",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            permit = SimpleNamespace(
                path=(
                    cache_root
                    / ".prewarm-permit-AbCdEf1234567890.json"
                ),
                capability="permit_capability",
                expires_at=auth.expires_at,
            )
            with (
                patch.object(
                    module,
                    "open_signal_gap_native_artifact",
                    return_value=(context, "e" * 64),
                ),
                patch.object(
                    module,
                    "_require_exact_sealed_native_generation",
                ),
                patch.object(
                    module,
                    "verify_signal_gap_native_cache_prewarm_authorization",
                    return_value=(auth, []),
                    create=True,
                ),
                patch.object(
                    module,
                    "load_scheme_config",
                    return_value=cfg,
                    create=True,
                ),
                patch.object(
                    module,
                    "run_configured_scheme",
                    return_value=[record],
                    create=True,
                ) as runner,
                patch.object(module, "mark_token_used"),
                patch.object(module, "write_authorization_audit"),
                patch.object(
                    module,
                    "create_signal_gap_cache_prewarm_permit",
                    return_value=permit,
                ),
                patch.object(module, "used_tokens_path", return_value=storage_root / "used.json"),
                patch.object(module, "PROJECT_ROOT", storage_root),
            ):
                result = module.prewarm_signal_gap_native_cache(
                    manifest=context.manifest_path,
                    historical_predict_date="2026-08-05",
                    publisher_scheme_id=PUBLISHER,
                    authorize="opaque-token",
                    storage_root=storage_root,
                )

        self.assertEqual(result["status"], "PREWARMED_PUBLISHED")
        self.assertEqual(result["cache_root"], str(cache_root))
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], cfg)
        self.assertEqual(runner.call_args.args[1], "2026-08-05")
        self.assertEqual(
            runner.call_args.kwargs["native_execution_mode"],
            "signal_gap_cache_prewarm",
        )
        self.assertEqual(
            runner.call_args.kwargs["phase_a_cache_root"],
            cache_root,
        )
        self.assertEqual(
            runner.call_args.kwargs["phase_a_cache_prewarm_permit"],
            permit.path,
        )
        self.assertEqual(
            runner.call_args.kwargs[
                "phase_a_cache_prewarm_capability"
            ],
            permit.capability,
        )

    def test_prewarm_rejects_artifact_without_exact_sealed_fence(
        self,
    ) -> None:
        from harness import signal_gap_native_artifact as module

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage_root = Path(temp_dir)
            context = _context(storage_root)
            engine = SimpleNamespace(dispose=lambda: None)
            with (
                patch.object(
                    module,
                    "open_signal_gap_native_artifact",
                    return_value=(context, "e" * 64),
                ),
                patch.object(
                    module,
                    "read_sealed_input_generation",
                    return_value=SimpleNamespace(
                        **{
                            **_sealed_row(context).__dict__,
                            "manifest_sha256": "f" * 64,
                        }
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    module.SignalGapNativeCachePrewarmError,
                    "REGISTERED_SEALED",
                ) as caught:
                    module.describe_signal_gap_native_cache_prewarm(
                        manifest=context.manifest_path,
                        historical_predict_date="2026-08-05",
                        publisher_scheme_id=PUBLISHER,
                        storage_root=storage_root,
                        engine_factory=lambda: engine,
                    )

        self.assertEqual(
            caught.exception.failure_code,
            "ARTIFACT_NOT_REGISTERED_SEALED",
        )

    def test_prewarm_authority_rejects_other_approved_tenor_publisher(
        self,
    ) -> None:
        from harness import signal_gap_native_artifact as module

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            context = _context(Path(temp_dir))
            with self.assertRaisesRegex(ValueError, "publisher"):
                module.native_cache_prewarm_authority(
                    context,
                    storage_root_identity="e" * 64,
                    publisher_scheme_id=(
                        "liwei_0616_5y01_full_oos_k3_div_k10"
                    ),
                    historical_predict_date="2026-08-05",
                )

    def test_prewarm_rejects_invalidated_sealed_generation(self) -> None:
        from harness import signal_gap_native_artifact as module

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage_root = Path(temp_dir)
            context = _context(storage_root)
            engine = SimpleNamespace(dispose=lambda: None)
            with (
                patch.object(
                    module,
                    "open_signal_gap_native_artifact",
                    return_value=(context, "e" * 64),
                ),
                patch.object(
                    module,
                    "read_sealed_input_generation",
                    return_value=SimpleNamespace(
                        **{
                            **_sealed_row(context).__dict__,
                            "invalidated_at": "2026-08-08T01:00:00+00:00",
                            "invalid_reason": "operator_invalidated",
                        }
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    module.SignalGapNativeCachePrewarmError,
                    "REGISTERED_SEALED",
                ) as caught:
                    module.describe_signal_gap_native_cache_prewarm(
                        manifest=context.manifest_path,
                        historical_predict_date="2026-08-05",
                        publisher_scheme_id=PUBLISHER,
                        storage_root=storage_root,
                        engine_factory=lambda: engine,
                    )

        self.assertEqual(
            caught.exception.failure_code,
            "ARTIFACT_NOT_REGISTERED_SEALED",
        )

    def test_prewarm_audit_omits_raw_hmac_and_child_capability(
        self,
    ) -> None:
        from harness import signal_gap_native_artifact as module
        from harness.authorization import (
            issue_signal_gap_native_cache_prewarm_token,
        )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage_root = Path(temp_dir)
            context = _context(storage_root)
            cache_root = (
                storage_root / ".phase-a-cache" / context.generation_id
            )
            authority = module.native_cache_prewarm_authority(
                context,
                storage_root_identity="e" * 64,
                publisher_scheme_id=PUBLISHER,
                historical_predict_date="2026-08-05",
            )
            record = SimpleNamespace(
                scheme_id=PUBLISHER,
                extra={
                    "phase_a_cache": {
                        "published": True,
                        "generation_acceptance": {
                            "status": "ACCEPTED",
                            "native_generation": _native_binding(context),
                        },
                    }
                },
            )
            permit = SimpleNamespace(
                path=(
                    cache_root
                    / ".prewarm-permit-AbCdEf1234567890.json"
                ),
                capability="child-capability-sentinel",
                expires_at="2099-01-01T00:00:00+00:00",
            )
            cfg = SimpleNamespace(
                scheme_id=PUBLISHER,
                runtime_type="native_adapter",
            )
            with patch.dict(
                os.environ,
                {"HARNESS_AUTH_SECRET": "test-cache-prewarm-secret"},
                clear=False,
            ):
                token = issue_signal_gap_native_cache_prewarm_token(
                    historical_predict_date="2026-08-05",
                    source_authority=authority,
                    issued_by="test-operator",
                )
                with (
                    patch.object(
                        module,
                        "open_signal_gap_native_artifact",
                        return_value=(context, "e" * 64),
                    ),
                    patch.object(
                        module,
                        "_require_exact_sealed_native_generation",
                    ),
                    patch.object(
                        module,
                        "load_scheme_config",
                        return_value=cfg,
                    ),
                    patch.object(
                        module,
                        "run_configured_scheme",
                        return_value=[record],
                    ),
                    patch.object(
                        module,
                        "create_signal_gap_cache_prewarm_permit",
                        return_value=permit,
                    ),
                    patch.object(module, "PROJECT_ROOT", storage_root),
                ):
                    result = module.prewarm_signal_gap_native_cache(
                        manifest=context.manifest_path,
                        historical_predict_date="2026-08-05",
                        publisher_scheme_id=PUBLISHER,
                        authorize=token,
                        storage_root=storage_root,
                    )

            audit_files = sorted(storage_root.rglob("*.json"))
            audit_contents = "\n".join(
                path.read_text(encoding="utf-8")
                for path in audit_files
            )

        self.assertEqual(result["status"], "PREWARMED_PUBLISHED")
        self.assertNotIn(token, audit_contents)
        self.assertNotIn(permit.capability, audit_contents)


if __name__ == "__main__":
    unittest.main()
