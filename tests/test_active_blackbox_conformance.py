from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V2_SCHEME_IDS = (
    "seven_y_current55_lgbm_001_v2",
    "seven_y_current55_lgbm_002_v2",
)
HEADER_COMPATIBILITY_SCRIPT = (
    PROJECT_ROOT
    / "schemes"
    / "one_y_t1_quote_state_hv_v1"
    / "delivery"
    / "one_y_t1_quote_state_hv_v1.py"
)
DATA_BRIDGE_SCHEMA = (
    PROJECT_ROOT / "shared" / "blackbox_v2" / "data_bridge_v1_schema.json"
)
REQUEST_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "daily_cutoff_key",
    "weekly_cutoff_key",
    "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)
WEEKLY_COLS = (
    "S0114089",
    "N1355677",
    "V0135838",
    "V0184553",
    "W0192843",
    "X0100205",
    "Y0110594",
    "HWW00001",
    "HWW00002",
    "HWW00003",
)
MONTHLY_COLS = (
    "M0000545",
    "M0041340",
    "M0041341",
    "M0041342",
    "M0061518",
    "M0096870",
    "M0317126",
    "M0009970",
    "M0009973",
    "M0001227",
)
DAILY_NUMERIC_COLS = (
    "TB7YWI0C",
    "TB1YWI0C",
    "TB3YWI0C",
    "TB5YWI0C",
    "TB0YWI0C",
    "DR007IBC",
    "DR007IB0",
    "DRS00001",
    "DRS00002",
    "SH000300",
    "CSI26901",
    "CSI39501",
    "IFCFE00C",
    "USDCNH0C",
    "USDCNH00",
    "USDCNH0H",
    "USDCNH0L",
    "S0031525",
    "AGSHF01C",
    "AUSHF00C",
    "ZNSHF00C",
    "G0006352",
    "G0006353",
    "G0266632",
    "G0266641",
    "SWR00001",
    "SWR00002",
    "M0000005",
    "M0000271",
    "M0048486",
    "G0003956",
)


@dataclass(frozen=True)
class DataBridgeFixture:
    """本测试自行生成的四文件 DataBridge 输入。"""

    data_dir: Path
    dates: tuple[date, ...]
    week_by_date: dict[str, str]
    cutoff_index: int


class ActiveBlackboxConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        conda = shutil.which("conda")
        if conda is None:
            raise RuntimeError("Blackbox V2 test requires conda")
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
        if completed.returncode != 0:
            raise RuntimeError(
                "cannot resolve blackbox-v2-v1 Python runtime:\n"
                f"stdout={completed.stdout}\nstderr={completed.stderr}"
            )
        cls.blackbox_python = Path(completed.stdout.strip().splitlines()[-1])

    def test_delivery_accepts_additive_databridge_columns(self) -> None:
        """未消费的新增业务列不得让现役交付拒绝兼容 Snapshot。"""
        schema = json.loads(DATA_BRIDGE_SCHEMA.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            for filename, specification in schema["files"].items():
                columns = [*specification["columns"], "UNUSED_ADDITIVE_FACTOR"]
                with (data_dir / filename).open(
                    "w", encoding="utf-8", newline=""
                ) as handle:
                    csv.writer(handle).writerow(columns)

            completed = self._run_header_validation(data_dir)

        self._assert_cli_success(completed)

    def test_delivery_rejects_changed_databridge_time_key(self) -> None:
        """增量列兼容不能放宽 DataBridge 时间键。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "weekly_output.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(["unexpected", "week_id"])
            code = (
                "import runpy, sys; from pathlib import Path; "
                "namespace = runpy.run_path(sys.argv[1]); "
                "namespace['_validate_schema_header'](Path(sys.argv[2]))"
            )
            completed = subprocess.run(
                [
                    str(self.blackbox_python),
                    "-B",
                    "-c",
                    code,
                    str(HEADER_COMPATIBILITY_SCRIPT),
                    str(path),
                ],
                check=False,
                capture_output=True,
                cwd=PROJECT_ROOT,
                env=self._runtime_env(),
                text=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("does not match data-bridge-v1 schema", completed.stderr)

    def test_cutoff_t_cli_succeeds_without_a_target_daily_row(self) -> None:
        """T+1 在仅到 feature_date 的四文件输入上仍返回 Contract 输出。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            full = self._write_data_bridge_fixture(workdir / "full")
            request = self._request_for(full)
            clipped_dir = workdir / "clipped"
            self._copy_fixture_through_feature_date(full, request, clipped_dir)
            with (clipped_dir / "daily_output.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                daily_dates = {row["date"] for row in csv.DictReader(handle)}
            self.assertNotIn(request["target_date"], daily_dates)

            for scheme_id in V2_SCHEME_IDS:
                with self.subTest(scheme_id=scheme_id):
                    script = self._script_for(scheme_id)
                    request_path = workdir / f"{scheme_id}-request.json"
                    output_path = workdir / f"{scheme_id}-predict.json"
                    request_path.write_text(
                        json.dumps(request), encoding="utf-8"
                    )
                    completed = self._run_cli(
                        script,
                        "predict",
                        "--request",
                        request_path,
                        "--data-dir",
                        clipped_dir,
                        "--output",
                        output_path,
                        cwd=workdir,
                    )
                    self._assert_cli_success(completed)
                    result = json.loads(output_path.read_text(encoding="utf-8"))
                    self.assertEqual(tuple(result), RESULT_FIELDS)
                    self.assertEqual(
                        {field: result[field] for field in RESULT_FIELDS[:-1]},
                        {field: request[field] for field in RESULT_FIELDS[:-1]},
                    )
                    self.assertIn(result["predicted_direction"], (-1, 0, 1))

                    requests_path = workdir / f"{scheme_id}-requests.csv"
                    backtest_output = workdir / f"{scheme_id}-backtest.csv"
                    self._write_request_csv(requests_path, [request])
                    backtest = self._run_cli(
                        script,
                        "backtest",
                        "--requests",
                        requests_path,
                        "--data-dir",
                        clipped_dir,
                        "--output",
                        backtest_output,
                        cwd=workdir,
                    )
                    self._assert_cli_success(backtest)
                    with backtest_output.open(encoding="utf-8", newline="") as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(tuple(rows[0]), RESULT_FIELDS)
                    self.assertEqual(
                        {field: rows[0][field] for field in RESULT_FIELDS[:-1]},
                        {field: request[field] for field in RESULT_FIELDS[:-1]},
                    )
                    self.assertIn(rows[0]["predicted_direction"], {"-1", "0", "1"})

    def _script_for(self, scheme_id: str) -> Path:
        return PROJECT_ROOT / "schemes" / scheme_id / "delivery" / f"{scheme_id}.py"

    def _runtime_env(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        return environment

    def _run_cli(
        self,
        script: Path,
        *arguments: str | Path,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.blackbox_python), "-B", str(script), *map(str, arguments)],
            check=False,
            capture_output=True,
            cwd=cwd,
            env=self._runtime_env(),
            text=True,
        )

    def _assert_cli_success(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            completed.returncode,
            0,
            "Blackbox CLI failed:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )

    def _run_header_validation(
        self, data_dir: Path
    ) -> subprocess.CompletedProcess[str]:
        code = (
            "import runpy, sys; from pathlib import Path; "
            "namespace = runpy.run_path(sys.argv[1]); root = Path(sys.argv[2]); "
            "[namespace['_validate_schema_header'](root / name) for name in "
            "('daily_output.csv', 'weekly_output.csv', 'monthly_output.csv')]"
        )
        return subprocess.run(
            [
                str(self.blackbox_python),
                "-B",
                "-c",
                code,
                str(HEADER_COMPATIBILITY_SCRIPT),
                str(data_dir),
            ],
            check=False,
            capture_output=True,
            cwd=PROJECT_ROOT,
            env=self._runtime_env(),
            text=True,
        )

    def _write_data_bridge_fixture(self, data_dir: Path) -> DataBridgeFixture:
        data_dir.mkdir()
        dates = self._business_days(date(2020, 1, 2), 1500)
        week_by_date = {
            current.isoformat(): (
                f"{current.isocalendar().year:04d}{current.isocalendar().week:02d}"
            )
            for current in dates
        }
        daily_rows: list[dict[str, str]] = []
        for index, current in enumerate(dates):
            row = {"date": current.isoformat()}
            for column_index, column in enumerate(DAILY_NUMERIC_COLS):
                value = self._daily_value(index, column_index)
                row[column] = f"{value:.8f}"
            daily_rows.append(row)
        self._write_csv(
            data_dir / "daily_output.csv",
            ("date", *DAILY_NUMERIC_COLS),
            daily_rows,
        )

        week_ids = tuple(dict.fromkeys(week_by_date[current.isoformat()] for current in dates))
        weekly_rows = [
            {
                "week_id": week_id,
                **{
                    column: f"{self._period_value(index, column_index):.8f}"
                    for column_index, column in enumerate(WEEKLY_COLS)
                },
            }
            for index, week_id in enumerate(week_ids)
        ]
        self._write_csv(
            data_dir / "weekly_output.csv", ("week_id", *WEEKLY_COLS), weekly_rows
        )

        month_ids = tuple(dict.fromkeys(current.strftime("%Y%m") for current in dates))
        monthly_rows = [
            {
                "month_id": month_id,
                **{
                    column: f"{self._period_value(index, column_index):.8f}"
                    for column_index, column in enumerate(MONTHLY_COLS)
                },
            }
            for index, month_id in enumerate(month_ids)
        ]
        self._write_csv(
            data_dir / "monthly_output.csv",
            ("month_id", *MONTHLY_COLS),
            monthly_rows,
        )
        self._write_csv(
            data_dir / "api_wind_date.csv",
            ("rdate", "week_id"),
            [
                {"rdate": current.isoformat(), "week_id": week_by_date[current.isoformat()]}
                for current in dates
            ],
        )

        cutoff_index = next(
            index
            for index in range(1150, len(dates) - 80)
            if dates[index].year != dates[index + 1].year
        )
        return DataBridgeFixture(
            data_dir=data_dir,
            dates=tuple(dates),
            week_by_date=week_by_date,
            cutoff_index=cutoff_index,
        )

    def _request_for(self, fixture: DataBridgeFixture) -> dict[str, str]:
        feature_date = fixture.dates[fixture.cutoff_index]
        target_date = fixture.dates[fixture.cutoff_index + 1]
        return {
            "request_id": "cutoff-t",
            "predict_date": target_date.isoformat(),
            "feature_date": feature_date.isoformat(),
            "target_date": target_date.isoformat(),
            "daily_cutoff_key": feature_date.isoformat(),
            "weekly_cutoff_key": fixture.week_by_date[feature_date.isoformat()],
            "monthly_cutoff_key": feature_date.strftime("%Y%m"),
        }

    def _copy_fixture_through_feature_date(
        self,
        fixture: DataBridgeFixture,
        request: dict[str, str],
        destination: Path,
    ) -> None:
        destination.mkdir()
        for filename, key, cutoff in (
            ("daily_output.csv", "date", request["daily_cutoff_key"]),
            ("weekly_output.csv", "week_id", request["weekly_cutoff_key"]),
            ("monthly_output.csv", "month_id", request["monthly_cutoff_key"]),
            ("api_wind_date.csv", "rdate", request["daily_cutoff_key"]),
        ):
            source = fixture.data_dir / filename
            with source.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames
                self.assertIsNotNone(fieldnames)
                rows = [row for row in reader if row[key] <= cutoff]
            self._write_csv(destination / filename, tuple(fieldnames or ()), rows)

    def _mutate_only_post_cutoff_values(
        self,
        data_dir: Path,
        request: dict[str, str],
    ) -> bool:
        changed = False
        for filename, key, cutoff in (
            ("daily_output.csv", "date", request["daily_cutoff_key"]),
            ("weekly_output.csv", "week_id", request["weekly_cutoff_key"]),
            ("monthly_output.csv", "month_id", request["monthly_cutoff_key"]),
        ):
            path = data_dir / filename
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames
                self.assertIsNotNone(fieldnames)
                rows = list(reader)
            for row_index, row in enumerate(rows):
                if row[key] <= cutoff:
                    continue
                changed = True
                for column_index, column in enumerate((fieldnames or ())[1:]):
                    row[column] = f"{999999 + row_index + column_index:.3f}"
            self._write_csv(path, tuple(fieldnames or ()), rows)
        return changed

    def _write_request_csv(
        self,
        path: Path,
        requests: list[dict[str, str]],
    ) -> None:
        self._write_csv(path, REQUEST_FIELDS, requests)

    @staticmethod
    def _write_csv(
        path: Path,
        fieldnames: tuple[str, ...],
        rows: list[dict[str, str]],
    ) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _business_days(start: date, count: int) -> list[date]:
        result: list[date] = []
        current = start
        while len(result) < count:
            if current.weekday() < 5:
                result.append(current)
            current += timedelta(days=1)
        return result

    @staticmethod
    def _daily_value(index: int, column_index: int) -> float:
        return (
            1.0
            + column_index * 0.11
            + index * 0.0007
            + math.sin((index + 1) * (column_index + 2) * 0.017) * 0.08
            + (0.012 if index % 2 else -0.009)
        )

    @staticmethod
    def _period_value(index: int, column_index: int) -> float:
        return (
            5.0
            + column_index * 0.2
            + index * 0.015
            + math.cos((index + 1) * (column_index + 3) * 0.11) * 0.1
        )

if __name__ == "__main__":
    unittest.main()
