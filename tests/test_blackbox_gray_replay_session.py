from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from shared.blackbox_v2.snapshot import CutoffKeys
from shared.data_bridge.refresh import CurrentDataset, DataBridgeRefreshConfig
from shared.data_bridge.validation import validate_dataset
from shared.input_artifacts import BLACKBOX_SCHEMA_PATH


def _schema_columns() -> dict[str, list[str]]:
    raw = json.loads(BLACKBOX_SCHEMA_PATH.read_text(encoding="utf-8"))
    return {
        filename: list(spec["columns"])
        for filename, spec in raw["files"].items()
    }


def _frame(filename: str, keys: list[str]) -> pd.DataFrame:
    columns = _schema_columns()[filename]
    values: dict[str, list[object]] = {columns[0]: keys}
    for position, column in enumerate(columns[1:], start=1):
        values[column] = [float(position + row) for row in range(len(keys))]
    return pd.DataFrame(values)


def _current() -> CurrentDataset:
    frames = {
        "daily_output.csv": _frame(
            "daily_output.csv",
            ["2026-07-24", "2026-07-31", "2026-08-06"],
        ),
        "weekly_output.csv": _frame(
            "weekly_output.csv",
            ["202630", "202631", "202632"],
        ),
        "monthly_output.csv": _frame(
            "monthly_output.csv",
            ["202606", "202607", "202608"],
        ),
    }
    dataset = validate_dataset(
        frames,
        schema_path=BLACKBOX_SCHEMA_PATH,
    )
    return CurrentDataset(
        state={
            "generation_id": "gray-replay-generation-1",
            "refresh_date": "2026-08-06",
            "schema_version": dataset.schema_version,
            "business_digest": dataset.business_digest,
        },
        dataset=dataset,
    )


def _source_identity(current: CurrentDataset) -> dict[str, object]:
    return {
        "generation_id": str(current.state["generation_id"]),
        "refresh_date": str(current.state["refresh_date"]),
        "schema_version": current.dataset.schema_version,
        "business_digest": current.dataset.business_digest,
        "stable_identity_sha256": "c" * 64,
        "files": [
            {
                "filename": profile.filename,
                "rows": profile.rows,
                "columns": profile.columns,
                "min_key": profile.min_key,
                "max_key": profile.max_key,
                "sha256": profile.sha256,
                "business_hash": profile.business_hash,
            }
            for _, profile in sorted(current.dataset.files.items())
        ],
    }


def _config(root: Path) -> DataBridgeRefreshConfig:
    return DataBridgeRefreshConfig(
        data_root=root / "data-bridge",
        runtime_root=root / "data-bridge-runtime",
        schema_path=BLACKBOX_SCHEMA_PATH,
    )


class BlackboxGrayReplaySessionTests(unittest.TestCase):
    def test_builds_one_physically_clipped_immutable_session(self) -> None:
        from shared.input_artifacts import (
            build_blackbox_gray_replay_session,
        )

        current = _current()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = _config(root)
            with patch(
                "shared.input_artifacts.check_current_dataset",
                return_value=current,
            ) as current_read:
                session = build_blackbox_gray_replay_session(
                    session_id="a" * 64,
                    source_identity=_source_identity(current),
                    request_cutoffs=(
                        CutoffKeys("2026-07-24", "202630", "202606"),
                        CutoffKeys("2026-07-31", "202631", "202607"),
                    ),
                    data_bridge_config=config,
                    output_root=root / "gray-replay",
                )

            current_read.assert_called_once_with(config, strict_read_only=True)
            self.assertEqual(
                pd.read_csv(
                    session.snapshot.data_dir / "daily_output.csv",
                    dtype={"date": "string"},
                )["date"].tolist(),
                ["2026-07-24", "2026-07-31"],
            )
            self.assertEqual(
                pd.read_csv(
                    session.snapshot.data_dir / "weekly_output.csv",
                    dtype={"week_id": "string"},
                )["week_id"].tolist(),
                ["202630", "202631"],
            )
            self.assertEqual(
                pd.read_csv(
                    session.snapshot.data_dir / "monthly_output.csv",
                    dtype={"month_id": "string"},
                )["month_id"].tolist(),
                ["202606", "202607"],
            )
            manifest = json.loads(session.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["session_id"], "a" * 64)
            self.assertEqual(
                manifest["max_cutoffs"],
                {
                    "daily_cutoff_key": "2026-07-31",
                    "weekly_cutoff_key": "202631",
                    "monthly_cutoff_key": "202607",
                },
            )
            self.assertEqual(
                session.manifest_sha256,
                hashlib.sha256(session.manifest_path.read_bytes()).hexdigest(),
            )

    def test_persisted_session_is_unchanged_when_source_frames_mutate(self) -> None:
        from shared.input_artifacts import (
            build_blackbox_gray_replay_session,
        )

        current = _current()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch(
                "shared.input_artifacts.check_current_dataset",
                return_value=current,
            ):
                session = build_blackbox_gray_replay_session(
                    session_id="b" * 64,
                    source_identity=_source_identity(current),
                    request_cutoffs=(
                        CutoffKeys("2026-07-24", "202630", "202606"),
                    ),
                    data_bridge_config=_config(root),
                    output_root=root / "gray-replay",
                )

            daily_path = session.snapshot.data_dir / "daily_output.csv"
            before = daily_path.read_bytes()
            manifest_sha256 = session.manifest_sha256
            current.dataset.frames["daily_output.csv"].loc[0, "S0031525"] = 9999.0

            self.assertEqual(daily_path.read_bytes(), before)
            self.assertEqual(
                hashlib.sha256(session.manifest_path.read_bytes()).hexdigest(),
                manifest_sha256,
            )

    def test_rejects_request_cutoff_absent_from_current_snapshot(self) -> None:
        from shared.input_artifacts import (
            build_blackbox_gray_replay_session,
        )

        current = _current()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch(
                "shared.input_artifacts.check_current_dataset",
                return_value=current,
            ):
                with self.assertRaisesRegex(ValueError, "gray replay cutoff"):
                    build_blackbox_gray_replay_session(
                        session_id="d" * 64,
                        source_identity=_source_identity(current),
                        request_cutoffs=(
                            CutoffKeys("2026-08-05", "202631", "202607"),
                        ),
                        data_bridge_config=_config(root),
                        output_root=root / "gray-replay",
                    )


if __name__ == "__main__":
    unittest.main()
