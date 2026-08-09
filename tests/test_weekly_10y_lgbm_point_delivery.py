"""weekly_10y_lgbm_point_v1 对扩展周频 header 的兼容性回归测试。"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import shutil
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEEKLY_SCHEMA_PATH = PROJECT_ROOT / "shared/blackbox_v2/data_bridge_v1_schema.json"
DELIVERY_SCRIPT = (
    PROJECT_ROOT
    / "schemes/weekly_10y_lgbm_point_v1/delivery/weekly_10y_lgbm_point_v1.py"
)
WEEKLY_FILENAME = "weekly_output.csv"
TARGET_COLUMN = "TB0YWI1C"
ADDITIONAL_TENORS = ("TB3YWI1C", "TB7YWI1C")
COMPATIBILITY_SCRIPT = """
import json
from pathlib import Path
import runpy
import sys

import pandas as pd

delivery = runpy.run_path(sys.argv[1])
assert delivery["WEEKLY_FILENAME"] == sys.argv[5]
assert delivery["TARGET_COLUMN"] == sys.argv[6]
baseline = delivery["load_weekly_data"](Path(sys.argv[2]))
extended = delivery["load_weekly_data"](Path(sys.argv[3]))
baseline_features = delivery["build_features"](baseline)
extended_features = delivery["build_features"](extended)
pd.testing.assert_frame_equal(baseline_features, extended_features)
assert delivery["predict_direction"](baseline) == delivery["predict_direction"](extended)
request = json.loads(sys.argv[4])
assert delivery["generate_results"]([request], baseline) == delivery["generate_results"](
    [request], extended
)
for missing_column in (delivery["TARGET_COLUMN"], delivery["FROZEN_FEATURES"][0]):
    try:
        delivery["build_features"](baseline.drop(columns=missing_column))
    except ValueError as exc:
        assert "missing required model columns" in str(exc)
        assert missing_column in str(exc)
    else:
        raise AssertionError("build_features accepted a missing required model column")
"""


def _baseline_header() -> list[str]:
    schema = json.loads(WEEKLY_SCHEMA_PATH.read_text(encoding="utf-8"))
    header = schema["files"][WEEKLY_FILENAME]["columns"]
    assert len(header) == 575
    assert header[0] == "week_id"
    return header


def _week_ids() -> list[str]:
    return [
        *(f"2019{week:02d}" for week in range(1, 53)),
        *(f"2020{week:02d}" for week in range(1, 9)),
    ]


def _write_weekly_csv(data_dir: Path, header: list[str]) -> None:
    data_dir.mkdir()
    with (data_dir / WEEKLY_FILENAME).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row_index, week_id in enumerate(_week_ids()):
            values = []
            for column_index, column in enumerate(header[1:], start=1):
                if column == TARGET_COLUMN:
                    value = 2.0 + 0.1 * (row_index % 2)
                else:
                    value = column_index + row_index / 1000
                values.append(f"{value:.6f}")
            writer.writerow((week_id, *values))


def _request(weekly_cutoff_key: str) -> dict[str, str]:
    return {
        "request_id": "header-compatibility",
        "predict_date": "2020-03-02",
        "feature_date": "2020-02-28",
        "target_date": "2020-03-09",
        "daily_cutoff_key": "2020-02-28",
        "weekly_cutoff_key": weekly_cutoff_key,
        "monthly_cutoff_key": "202002",
    }


def _blackbox_python() -> Path:
    conda = shutil.which("conda")
    assert conda is not None, "Blackbox V2 test requires conda"
    completed = subprocess.run(
        [
            conda,
            "run",
            "--no-capture-output",
            "-n",
            "forecast_env_blackbox_v1",
            "python",
            "-c",
            "import sys; print(sys.executable)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        "cannot resolve blackbox-v2-v1 Python runtime:\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    return Path(completed.stdout.strip().splitlines()[-1])


def _runtime_env() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def test_extended_weekly_header_preserves_delivery_behavior(tmp_path: Path) -> None:
    baseline_header = _baseline_header()
    assert not set(ADDITIONAL_TENORS).intersection(baseline_header)
    extended_header = [*baseline_header, *ADDITIONAL_TENORS]
    assert tuple(extended_header[len(baseline_header) :]) == ADDITIONAL_TENORS

    baseline_dir = tmp_path / "baseline"
    extended_dir = tmp_path / "extended"
    _write_weekly_csv(baseline_dir, baseline_header)
    _write_weekly_csv(extended_dir, extended_header)

    request = _request(_week_ids()[-1])
    completed = subprocess.run(
        [
            str(_blackbox_python()),
            "-B",
            "-c",
            COMPATIBILITY_SCRIPT,
            str(DELIVERY_SCRIPT),
            str(baseline_dir),
            str(extended_dir),
            json.dumps(request),
            WEEKLY_FILENAME,
            TARGET_COLUMN,
        ],
        check=False,
        capture_output=True,
        cwd=PROJECT_ROOT,
        env=_runtime_env(),
        text=True,
    )

    assert completed.returncode == 0, (
        "forecast_env_blackbox_v1 compatibility check failed:\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
