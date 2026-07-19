from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd


class DataBridgeValidationTests(unittest.TestCase):
    def test_accepts_databridge_dates_and_builds_normalized_profiles(self) -> None:
        from shared.data_bridge.validation import validate_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            schema = _write_schema(Path(tmpdir))
            dataset = validate_dataset(_frames(), schema_path=schema, expected_daily_date="2026-07-18")

        daily = dataset.files["daily_output.csv"]
        self.assertEqual(daily.min_key, "2026-07-17")
        self.assertEqual(daily.max_key, "2026-07-18")
        self.assertEqual(daily.rows, 2)
        self.assertEqual(daily.columns, 2)
        self.assertEqual(len(dataset.business_digest), 64)

    def test_rejects_invalid_factor_and_removed_historical_key(self) -> None:
        from shared.data_bridge.validation import DataBridgeValidationError, validate_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            schema = _write_schema(Path(tmpdir))
            frames = _frames()
            frames["weekly_output.csv"].loc[1, "factor"] = "not-a-number"
            with self.assertRaisesRegex(DataBridgeValidationError, "numeric or empty"):
                validate_dataset(frames, schema_path=schema)

            frames = _frames()
            previous = {
                "daily_output.csv": {"2026-07-16", "2026-07-17", "2026-07-18"},
                "weekly_output.csv": {"202628", "202629"},
                "monthly_output.csv": {"202606", "202607"},
            }
            with self.assertRaisesRegex(DataBridgeValidationError, "historical keys disappeared"):
                validate_dataset(frames, schema_path=schema, previous_keys=previous)

    def test_business_digest_ignores_csv_rendering_but_detects_value_change(self) -> None:
        from shared.data_bridge.validation import validate_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            schema = _write_schema(Path(tmpdir))
            first = validate_dataset(_frames(), schema_path=schema)
            equivalent = _frames()
            equivalent["daily_output.csv"]["factor"] = ["1.0", "2.000"]
            second = validate_dataset(equivalent, schema_path=schema)
            changed = _frames()
            changed["daily_output.csv"].loc[1, "factor"] = "2.1"
            third = validate_dataset(changed, schema_path=schema)

        self.assertEqual(first.business_digest, second.business_digest)
        self.assertNotEqual(first.business_digest, third.business_digest)


def _frames() -> dict[str, pd.DataFrame]:
    return {
        "daily_output.csv": pd.DataFrame(
            {"date": ["2026/07/17 00:00", "2026/07/18 00:00"], "factor": ["1", "2"]}
        ),
        "weekly_output.csv": pd.DataFrame({"week_id": ["202628", "202629"], "factor": ["3", "4"]}),
        "monthly_output.csv": pd.DataFrame({"month_id": ["202606", "202607"], "factor": ["5", ""]}),
    }


def _write_schema(root: Path) -> Path:
    path = root / "schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "data-bridge-v1",
                "files": {
                    "daily_output.csv": {"columns": ["date", "factor"]},
                    "weekly_output.csv": {"columns": ["week_id", "factor"]},
                    "monthly_output.csv": {"columns": ["month_id", "factor"]},
                },
            }
        ),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
