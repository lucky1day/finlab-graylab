from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PUBLISHER = "liwei_0616_10y01_full_oos_k3_div_k10"
GENERATION_ID = "native-" + "a" * 24


def _native_binding() -> dict[str, str]:
    return {
        "generation_id": GENERATION_ID,
        "manifest_sha256": "b" * 64,
        "dataset_content_id": "c" * 64,
        "business_date": "2026-08-08",
        "feature_date": "2026-08-04",
        "schema_version": "native-generation-v1",
        "exporter_version": "native-signal-gap-current-snapshot-v1",
    }


class SignalGapCachePrewarmPermitTests(unittest.TestCase):
    def _issue_permit(self, root: Path):
        from shared.liwei_0616_signal_gap_prewarm import (
            create_signal_gap_cache_prewarm_permit,
        )

        cache_root = root / ".phase-a-cache" / GENERATION_ID
        return create_signal_gap_cache_prewarm_permit(
            artifact_root=root,
            cache_root=cache_root,
            publisher_scheme_id=PUBLISHER,
            native_generation=_native_binding(),
            expires_at=(
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat(),
            authorization_token_sha256="d" * 64,
        )

    def test_permit_is_bound_to_artifact_and_consumed_once(self) -> None:
        from shared.input_artifacts import NATIVE_MANIFEST_PATH_ENV
        from shared.liwei_0616_signal_gap_prewarm import (
            PREWARM_CAPABILITY_ENV,
            PREWARM_PERMIT_ENV,
            consume_signal_gap_cache_prewarm_permit,
        )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            generation_dir = root / GENERATION_ID
            generation_dir.mkdir()
            permit = self._issue_permit(root)
            cache_root = root / ".phase-a-cache" / GENERATION_ID
            environment = {
                NATIVE_MANIFEST_PATH_ENV: str(
                    generation_dir / "manifest.json"
                ),
                PREWARM_PERMIT_ENV: str(permit.path),
                PREWARM_CAPABILITY_ENV: permit.capability,
            }
            with patch.dict(os.environ, environment, clear=True):
                result = consume_signal_gap_cache_prewarm_permit(
                    cache_root=cache_root,
                    cache_consumer_id=PUBLISHER,
                    native_generation=_native_binding(),
                )
                with self.assertRaisesRegex(RuntimeError, "already consumed"):
                    consume_signal_gap_cache_prewarm_permit(
                        cache_root=cache_root,
                        cache_consumer_id=PUBLISHER,
                        native_generation=_native_binding(),
                    )

            self.assertEqual(result["authorization_token_sha256"], "d" * 64)
            self.assertFalse(permit.path.exists())
            self.assertTrue(
                permit.path.with_name(permit.path.name + ".consumed").exists()
            )

    def test_permit_rejects_wrong_cache_root_and_wrong_publisher(self) -> None:
        from shared.input_artifacts import NATIVE_MANIFEST_PATH_ENV
        from shared.liwei_0616_signal_gap_prewarm import (
            PREWARM_CAPABILITY_ENV,
            PREWARM_PERMIT_ENV,
            consume_signal_gap_cache_prewarm_permit,
            create_signal_gap_cache_prewarm_permit,
        )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, "publisher"):
                create_signal_gap_cache_prewarm_permit(
                    artifact_root=root,
                    cache_root=root / ".phase-a-cache" / GENERATION_ID,
                    publisher_scheme_id="liwei_0616_5y01_full_oos_k3_div_k10",
                    native_generation=_native_binding(),
                    expires_at=(
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    ).isoformat(),
                    authorization_token_sha256="d" * 64,
                )
            generation_dir = root / GENERATION_ID
            generation_dir.mkdir()
            permit = self._issue_permit(root)
            with patch.dict(
                os.environ,
                {
                    NATIVE_MANIFEST_PATH_ENV: str(
                        generation_dir / "manifest.json"
                    ),
                    PREWARM_PERMIT_ENV: str(permit.path),
                    PREWARM_CAPABILITY_ENV: permit.capability,
                },
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "cache_root"):
                    consume_signal_gap_cache_prewarm_permit(
                        cache_root=(
                            root
                            / ".phase-a-cache"
                            / ("native-" + "b" * 24)
                        ),
                        cache_consumer_id=PUBLISHER,
                        native_generation=_native_binding(),
                    )

    def test_permit_is_required_at_cache_write_sink(self) -> None:
        from shared.input_artifacts import NATIVE_MANIFEST_PATH_ENV
        from shared.liwei_0616_signal_gap_prewarm import (
            consume_signal_gap_cache_prewarm_permit,
        )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            generation_dir = root / GENERATION_ID
            generation_dir.mkdir()
            self._issue_permit(root)
            with patch.dict(
                os.environ,
                {
                    NATIVE_MANIFEST_PATH_ENV: str(
                        generation_dir / "manifest.json"
                    ),
                },
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "permit is missing"):
                    consume_signal_gap_cache_prewarm_permit(
                        cache_root=root / ".phase-a-cache" / GENERATION_ID,
                        cache_consumer_id=PUBLISHER,
                        native_generation=_native_binding(),
                    )

    def test_phase_a_cache_consumes_permit_before_creating_family(self) -> None:
        import pandas as pd

        from shared import liwei_0616_phase_a_cache as cache
        from shared.liwei_0616_cache_contract import (
            CACHE_MUTATION_POLICY_ENV,
            CACHE_MUTATION_POLICY_PREWARM,
        )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            cache_root = root / ".phase-a-cache" / GENERATION_ID
            spec = cache.PhaseACacheSpec(
                cache_family="prewarm-test",
                tenor="10Y",
                publisher_consumer_id=PUBLISHER,
                baselines=(),
                baseline_configs={},
                source_ic_screen_start="2020-01-01",
                horizon=5,
                purge_gap=0,
            )
            with (
                patch.dict(
                    os.environ,
                    {CACHE_MUTATION_POLICY_ENV: CACHE_MUTATION_POLICY_PREWARM},
                    clear=True,
                ),
                patch.object(cache, "_validate_cache_publisher_identity"),
                patch.object(cache, "_validate_daily_dependency_proof"),
                patch.object(
                    cache,
                    "_resolve_native_generation_binding",
                    return_value=_native_binding(),
                ),
                patch.object(
                    cache,
                    "consume_signal_gap_cache_prewarm_permit",
                    side_effect=RuntimeError("permit-sentinel"),
                ) as consume,
                self.assertRaisesRegex(RuntimeError, "permit-sentinel"),
            ):
                cache.prepare_phase_a_caches(
                    spec=spec,
                    daily_df=pd.DataFrame(),
                    weekly_df=pd.DataFrame(),
                    monthly_df=pd.DataFrame(),
                    test_ranges=(),
                    train_missing=lambda _baseline, _ranges: {},
                    cache_consumer_id=PUBLISHER,
                    native_generation=_native_binding(),
                    cache_root=cache_root,
                )

            consume.assert_called_once_with(
                cache_root=cache_root,
                cache_consumer_id=PUBLISHER,
                native_generation=_native_binding(),
            )
            self.assertFalse(
                (cache_root / "prewarm-test" / "10y").exists()
            )


if __name__ == "__main__":
    unittest.main()
