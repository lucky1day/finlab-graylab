from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from tests.test_databridge_input_generation import (
    SCHEMA_PATH,
    _current_dataset,
    _frame,
    _native_cutoff_context,
)
from tests.test_native_input_generation import (
    _Connection,
    _Engine,
    _Rows,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V2_IDS = (
    "one_y_t5_liq_excess_a_v1",
    "one_y_t5_liq_excess_a_w252_l7_v1",
    "one_y_t5_liq_excess_a_w350_l7_v1",
    "one_y_t5_liq_excess_b_w252_l7_v1",
)


class _FrozenCalendarConnection(_Connection):
    def execute(
        self,
        statement: object,
        params: dict[str, object] | None = None,
    ) -> _Rows:
        sql = str(statement)
        if "FROM api_wind_date" in sql:
            return _Rows(
                [
                    {
                        "rdate": day,
                        "week_id": pd.Timestamp(day).strftime("%Y%W"),
                    }
                    for day in (
                        "2026-07-23",
                        "2026-07-24",
                        "2026-07-27",
                        "2026-07-28",
                        "2026-07-29",
                        "2026-07-30",
                    )
                ]
            )
        if "FROM t_trade_calendar" in sql:
            return _Rows(
                [
                    {"rdate": day, "trade_flag": "1"}
                    for day in (
                        "2026-07-23",
                        "2026-07-24",
                        "2026-07-27",
                        "2026-07-28",
                        "2026-07-29",
                        "2026-07-30",
                    )
                ]
            )
        return super().execute(statement, params)


class _FrozenCalendarEngine(_Engine):
    def __init__(self) -> None:
        super().__init__()
        self.connection = _FrozenCalendarConnection()


class _NoLiveDatabase:
    def connect(self):
        raise AssertionError("V2 frozen execution must not connect to live DB")


def _delivery_dataset():
    from shared.data_bridge.refresh import CurrentDataset
    from shared.data_bridge.validation import validate_dataset

    end = date(2026, 7, 23)
    dates = pd.bdate_range(end=end, periods=500)
    daily = _frame(
        "daily_output.csv",
        [value.date().isoformat() for value in dates],
    )
    index = np.arange(len(daily), dtype=float)
    series = {
        "TB1YWI0C": 2.0 + 0.015 * np.sin(index / 4.0) + index * 0.00002,
        "TB3YWI0C": 2.2 + 0.012 * np.sin(index / 5.0 + 0.3),
        "TB5YWI0C": 2.4 + 0.010 * np.sin(index / 6.0 + 0.6),
        "TB7YWI0C": 2.6 + 0.009 * np.sin(index / 7.0 + 0.9),
        "TB0YWI0C": 2.8 + 0.008 * np.sin(index / 8.0 + 1.2),
        "DR007IBC": 1.8 + 0.020 * np.sin(index / 9.0),
        "USDCNH0C": 7.0 + 0.010 * np.sin(index / 10.0),
        "SH000300": 3000.0 + index + 20.0 * np.sin(index / 11.0),
        "IFCFE00C": 3000.0 + index + 18.0 * np.sin(index / 12.0),
        "S0031525": 2.0 + 0.005 * np.sin(index / 13.0),
        "AUSHF00C": 450.0 + 0.2 * index + np.sin(index / 14.0),
    }
    for column, values in series.items():
        daily[column] = values
    frames = {
        "daily_output.csv": daily,
        "weekly_output.csv": _frame(
            "weekly_output.csv",
            ["202628", "202629"],
        ),
        "monthly_output.csv": _frame(
            "monthly_output.csv",
            ["202606", "202607"],
        ),
    }
    dataset = validate_dataset(
        frames,
        schema_path=SCHEMA_PATH,
        expected_daily_date="2026-07-23",
    )
    baseline = _current_dataset()
    state = dict(baseline.state)
    state["business_digest"] = dataset.business_digest
    state["files"] = {
        filename: {
            "sha256": profile.sha256,
            "rows": profile.rows,
            "columns": profile.columns,
        }
        for filename, profile in dataset.files.items()
    }
    return CurrentDataset(
        state=MappingProxyType(state),
        dataset=dataset,
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

    def _delivery_generation(self, root: Path):
        from shared.databridge_input_generation import (
            create_databridge_generation,
        )

        native = _native_cutoff_context(
            engine=_FrozenCalendarEngine(),
        )
        generation = create_databridge_generation(
            _delivery_dataset(),
            native_generation=native,
            business_date="2026-07-24",
            feature_date="2026-07-23",
            output_root=root,
            schema_path=SCHEMA_PATH,
        )
        return native, generation

    def test_all_real_v2_deliveries_use_same_frozen_generation(
        self,
    ) -> None:
        from scheduler.discovery import load_scheme_config
        from scheduler.executor import (
            _effective_timeout_sec,
            run_blackbox_scheme_subprocess,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            native, generation = self._delivery_generation(Path(tmpdir))
            self.assertEqual(
                {path.name for path in generation.data_dir.iterdir()},
                {
                    "daily_output.csv",
                    "weekly_output.csv",
                    "monthly_output.csv",
                },
            )
            results = {}
            with (
                patch(
                    "scheduler.executor.open_blackbox_input_snapshot",
                    side_effect=AssertionError(
                        "must not read mutable DataBridge current"
                    ),
                ),
                patch(
                    "scheduler.executor.resolve_blackbox_input_cutoffs",
                    side_effect=AssertionError("must not query live DB"),
                ),
            ):
                for scheme_id in V2_IDS:
                    cfg = load_scheme_config(
                        PROJECT_ROOT / "schemes" / scheme_id / "config.yaml"
                    )
                    self.assertEqual(
                        _effective_timeout_sec(cfg, 120),
                        120,
                    )
                    first = run_blackbox_scheme_subprocess(
                        cfg,
                        "2026-07-24",
                        engine=_NoLiveDatabase(),
                        algo_env="forecast_env_blackbox_v1",
                        timeout_sec=120,
                        databridge_generation=generation,
                        calendar_generation=native,
                    )
                    second = run_blackbox_scheme_subprocess(
                        cfg,
                        "2026-07-24",
                        engine=_NoLiveDatabase(),
                        algo_env="forecast_env_blackbox_v1",
                        timeout_sec=120,
                        databridge_generation=generation,
                        calendar_generation=native,
                    )
                    self.assertEqual(first, second)
                    self.assertEqual(len(first), 1)
                    record = first[0]
                    self.assertEqual(record.scheme_id, scheme_id)
                    self.assertEqual(record.target_tenor, "1Y")
                    self.assertEqual(record.horizon, 5)
                    self.assertEqual(record.predict_date, "2026-07-24")
                    self.assertEqual(record.feature_date, "2026-07-23")
                    self.assertEqual(record.target_date, "2026-07-30")
                    self.assertIn(record.predicted_direction, {-1, 1})
                    self.assertEqual(
                        record.extra["data_generation_id"],
                        generation.generation_id,
                    )
                    self.assertEqual(
                        record.extra[
                            "data_generation_manifest_sha256"
                        ],
                        generation.manifest_sha256,
                    )
                    self.assertEqual(
                        record.extra["native_generation_id"],
                        native.generation_id,
                    )
                    self.assertEqual(
                        record.extra["data_snapshot_id"],
                        generation.snapshot.snapshot_id,
                    )
                    results[scheme_id] = record

        self.assertEqual(set(results), set(V2_IDS))
        self.assertEqual(
            {
                record.extra["data_generation_id"]
                for record in results.values()
            },
            {generation.generation_id},
        )

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
