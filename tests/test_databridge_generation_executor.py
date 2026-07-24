from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_databridge_input_generation import (
    SCHEMA_PATH,
    _current_dataset,
    _native_cutoff_context,
)


class DataBridgeGenerationExecutorTests(unittest.TestCase):
    def _generation(self, root: Path):
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        native = _native_cutoff_context()
        generation = create_databridge_generation(
            _current_dataset(),
            native_generation=native,
            business_date="2026-07-24",
            feature_date="2026-07-23",
            output_root=root,
            schema_path=SCHEMA_PATH,
        )
        return native, generation

    def test_v2_execution_uses_bound_generation_and_frozen_cutoffs_only(
        self,
    ) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess
        from shared.models import PredictionRecord

        cfg = SimpleNamespace(
            scheme_id="blackbox_demo",
            delivery_script=Path("/tmp/blackbox_demo.py"),
            delivery_metadata=Path("/tmp/blackbox_demo.json"),
        )
        metadata = SimpleNamespace(frequency="daily", horizon=1)
        record = PredictionRecord(
            scheme_id="blackbox_demo",
            target_tenor="10Y",
            horizon=1,
            predict_date="2026-07-24",
            feature_date="2026-07-23",
            target_date="2026-07-27",
            predicted_direction=1,
            extra={"data_snapshot_id": "placeholder"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            native, generation = self._generation(Path(tmpdir))
            with (
                patch(
                    "scheduler.executor.load_metadata",
                    return_value=metadata,
                ),
                patch(
                    "scheduler.executor.open_blackbox_input_snapshot",
                    side_effect=AssertionError(
                        "must not read mutable DataBridge current"
                    ),
                ),
                patch(
                    "scheduler.executor.open_native_generation",
                    return_value=native,
                ),
                patch(
                    "scheduler.executor.resolve_blackbox_input_cutoffs",
                    side_effect=AssertionError("must not query live DB"),
                ),
                patch(
                    "scheduler.executor.build_daily_live_context",
                    return_value=SimpleNamespace(
                        feature_date="2026-07-23"
                    ),
                ),
                patch(
                    "scheduler.executor.build_live_request",
                    return_value={"request": "frozen"},
                ) as request_builder,
                patch(
                    "scheduler.blackbox_v2_runner.run_blackbox_predict",
                    return_value=record,
                ) as runner,
            ):
                records = run_blackbox_scheme_subprocess(
                    cfg,
                    "2026-07-24",
                    engine=object(),
                    algo_env="blackbox-env",
                    timeout_sec=120,
                    databridge_generation=generation,
                    calendar_generation=native,
                )

        self.assertEqual(len(records), 1)
        request_builder.assert_called_once()
        self.assertEqual(
            request_builder.call_args.kwargs["cutoffs"],
            generation.cutoffs,
        )
        self.assertEqual(
            runner.call_args.kwargs["data_dir"],
            generation.data_dir,
        )
        self.assertEqual(
            runner.call_args.kwargs["data_snapshot_id"],
            generation.snapshot.snapshot_id,
        )
        extra = records[0].extra
        self.assertEqual(
            extra["data_generation_id"],
            generation.generation_id,
        )
        self.assertEqual(
            extra["data_generation_manifest_sha256"],
            generation.manifest_sha256,
        )
        self.assertEqual(
            extra["native_generation_id"],
            native.generation_id,
        )
        self.assertEqual(extra["weekly_cutoff_key"], "202629")
        self.assertEqual(extra["monthly_cutoff_key"], "202607")

    def test_v2_execution_rejects_wrong_calendar_generation(self) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess

        cfg = SimpleNamespace(
            scheme_id="blackbox_demo",
            delivery_script=Path("/tmp/blackbox_demo.py"),
            delivery_metadata=Path("/tmp/blackbox_demo.json"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            native, generation = self._generation(Path(tmpdir))
            mismatched = replace(
                native,
                generation_id="native-wrong-generation",
            )
            with self.assertRaisesRegex(
                ValueError,
                "linked Native generation",
            ):
                run_blackbox_scheme_subprocess(
                    cfg,
                    "2026-07-24",
                    engine=object(),
                    algo_env="blackbox-env",
                    timeout_sec=120,
                    databridge_generation=generation,
                    calendar_generation=mismatched,
                )

    def test_v2_execution_rejects_wrong_calendar_manifest_hash(
        self,
    ) -> None:
        from scheduler.executor import run_blackbox_scheme_subprocess

        cfg = SimpleNamespace(
            scheme_id="blackbox_demo",
            delivery_script=Path("/tmp/blackbox_demo.py"),
            delivery_metadata=Path("/tmp/blackbox_demo.json"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            native, generation = self._generation(Path(tmpdir))
            mismatched = replace(
                native,
                manifest_sha256="0" * 64,
            )
            with self.assertRaisesRegex(
                ValueError,
                "linked Native manifest",
            ):
                run_blackbox_scheme_subprocess(
                    cfg,
                    "2026-07-24",
                    engine=object(),
                    algo_env="blackbox-env",
                    timeout_sec=120,
                    databridge_generation=generation,
                    calendar_generation=mismatched,
                )


if __name__ == "__main__":
    unittest.main()
