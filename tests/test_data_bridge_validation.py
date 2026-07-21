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

    def test_accepts_added_business_columns_and_includes_them_in_digest(self) -> None:
        from shared.data_bridge.validation import validate_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            schema = _write_schema(Path(tmpdir))
            frames = _frames()
            for frame in frames.values():
                frame["new_factor"] = ["10", "20"]
            first = validate_dataset(frames, schema_path=schema)

            changed = {name: frame.copy() for name, frame in frames.items()}
            changed["daily_output.csv"].loc[1, "new_factor"] = "21"
            second = validate_dataset(changed, schema_path=schema)

        self.assertEqual(first.files["daily_output.csv"].columns, 3)
        self.assertEqual(
            list(first.frames["weekly_output.csv"].columns),
            ["week_id", "factor", "new_factor"],
        )
        self.assertNotEqual(first.business_digest, second.business_digest)

    def test_additive_schema_policy_still_rejects_incompatible_headers(self) -> None:
        from shared.data_bridge.validation import DataBridgeValidationError, validate_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)

            missing = _frames()
            missing["daily_output.csv"] = missing["daily_output.csv"][["date"]]
            with self.assertRaisesRegex(DataBridgeValidationError, "missing baseline columns"):
                validate_dataset(missing, schema_path=schema)

            key_not_first = _frames()
            key_not_first["daily_output.csv"] = key_not_first["daily_output.csv"][["factor", "date"]]
            with self.assertRaisesRegex(DataBridgeValidationError, "must start with date"):
                validate_dataset(key_not_first, schema_path=schema)

            duplicate = _frames()
            duplicate["daily_output.csv"] = pd.DataFrame(
                [
                    ["2026/07/17 00:00", "1", "10"],
                    ["2026/07/18 00:00", "2", "20"],
                ],
                columns=["date", "factor", "factor"],
            )
            with self.assertRaisesRegex(DataBridgeValidationError, "duplicate columns"):
                validate_dataset(duplicate, schema_path=schema)

            added_non_numeric = _frames()
            added_non_numeric["daily_output.csv"]["new_factor"] = ["10", "bad"]
            with self.assertRaisesRegex(DataBridgeValidationError, "new_factor must contain only finite"):
                validate_dataset(added_non_numeric, schema_path=schema)

            ordered_frames = _ordered_frames()
            ordered_schema = _write_schema(root, frames=ordered_frames, filename="ordered-schema.json")
            ordered_frames["daily_output.csv"] = ordered_frames["daily_output.csv"][
                ["date", "second_factor", "first_factor"]
            ]
            with self.assertRaisesRegex(DataBridgeValidationError, "baseline column order"):
                validate_dataset(ordered_frames, schema_path=ordered_schema)

    def test_rejects_schema_baseline_that_does_not_start_with_time_key(self) -> None:
        from shared.data_bridge.validation import DataBridgeValidationError, validate_dataset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = _write_schema(root)
            payload = json.loads(schema.read_text(encoding="utf-8"))
            payload["files"]["daily_output.csv"]["columns"] = ["factor"]
            schema.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                DataBridgeValidationError,
                "baseline must start with date",
            ):
                validate_dataset(_frames(), schema_path=schema)

    def test_directory_reader_rejects_duplicate_raw_csv_headers(self) -> None:
        from shared.data_bridge.validation import (
            DataBridgeValidationError,
            read_dataset_directory,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "daily_output.csv").write_text(
                "date,factor,factor\n2026-07-18,1,2\n",
                encoding="utf-8",
            )
            (root / "weekly_output.csv").write_text(
                "week_id,factor\n202629,1\n",
                encoding="utf-8",
            )
            (root / "monthly_output.csv").write_text(
                "month_id,factor\n202607,1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DataBridgeValidationError, "duplicate columns"):
                read_dataset_directory(root)


def _frames() -> dict[str, pd.DataFrame]:
    return {
        "daily_output.csv": pd.DataFrame(
            {"date": ["2026/07/17 00:00", "2026/07/18 00:00"], "factor": ["1", "2"]}
        ),
        "weekly_output.csv": pd.DataFrame({"week_id": ["202628", "202629"], "factor": ["3", "4"]}),
        "monthly_output.csv": pd.DataFrame({"month_id": ["202606", "202607"], "factor": ["5", ""]}),
    }


def _ordered_frames() -> dict[str, pd.DataFrame]:
    frames = _frames()
    frames["daily_output.csv"] = pd.DataFrame(
        {
            "date": ["2026/07/17 00:00", "2026/07/18 00:00"],
            "first_factor": ["1", "2"],
            "second_factor": ["3", "4"],
        }
    )
    return frames


def _write_schema(
    root: Path,
    *,
    frames: dict[str, pd.DataFrame] | None = None,
    filename: str = "schema.json",
) -> Path:
    frames = frames or _frames()
    path = root / filename
    path.write_text(
        json.dumps(
            {
                "schema_version": "data-bridge-v1",
                "files": {
                    name: {"columns": list(frame.columns)}
                    for name, frame in frames.items()
                },
            }
        ),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
