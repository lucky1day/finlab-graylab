from __future__ import annotations

import csv
import hashlib
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
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V2_SCHEME_IDS = (
    "seven_y_current55_lgbm_001_v2",
    "seven_y_current55_lgbm_002_v2",
)
V1_DELIVERY_SHA256 = {
    "schemes/seven_y_current55_lgbm_001_v1/delivery/seven_y_current55_lgbm_001_v1.py": (
        "cdf4ccc0f15712b776b23ed0b576d43ded928b19f3316c2279cbfe404fb5549e"
    ),
    "schemes/seven_y_current55_lgbm_001_v1/delivery/seven_y_current55_lgbm_001_v1.json": (
        "6648239aa19b389d694706a98ceecfa6f1b882412d0056dee6a5475a24e71df6"
    ),
    "schemes/seven_y_current55_lgbm_002_v1/delivery/seven_y_current55_lgbm_002_v1.py": (
        "cdf4ccc0f15712b776b23ed0b576d43ded928b19f3316c2279cbfe404fb5549e"
    ),
    "schemes/seven_y_current55_lgbm_002_v1/delivery/seven_y_current55_lgbm_002_v1.json": (
        "95562c9039a0697e0c88feafd418804e9744f4cdd7b737a33bf94e025a065d01"
    ),
}
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


class SevenYCurrent55LgbmV2DeliveryTests(unittest.TestCase):
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

    def test_001_v2_two_file_delivery_exists_without_replacing_v1(self) -> None:
        """001 本地因果 V2 身份必须作为独立的 Blackbox 交付存在。"""
        self._assert_two_file_delivery("seven_y_current55_lgbm_001_v2")

    def test_002_v2_two_file_delivery_exists_without_replacing_v1(self) -> None:
        """002 本地因果 V2 身份必须作为独立的 Blackbox 交付存在。"""
        self._assert_two_file_delivery("seven_y_current55_lgbm_002_v2")

    def test_v1_hashes_are_frozen_and_v2_metadata_has_separate_identity(self) -> None:
        """V1 交付保持冻结，已激活 V2 保持独立的本地因果身份。"""
        for relative_path, expected_hash in V1_DELIVERY_SHA256.items():
            with self.subTest(relative_path=relative_path):
                actual_hash = hashlib.sha256(
                    (PROJECT_ROOT / relative_path).read_bytes()
                ).hexdigest()
                self.assertEqual(actual_hash, expected_hash)

        for scheme_id in V2_SCHEME_IDS:
            with self.subTest(scheme_id=scheme_id):
                scheme_dir = PROJECT_ROOT / "schemes" / scheme_id
                delivery = scheme_dir / "delivery"
                metadata = json.loads(
                    (delivery / f"{scheme_id}.json").read_text(encoding="utf-8")
                )
                config_text = (scheme_dir / "config.yaml").read_text(encoding="utf-8")
                self.assertEqual(metadata["scheme_id"], scheme_id)
                self.assertEqual(metadata["algorithm_version"], "2.0.0")
                self.assertIn("本地因果V2", metadata["name"])
                self.assertIn("feature_date", metadata["description"])
                self.assertNotIn("持久", metadata["description"])
                self.assertNotIn("--cache-dir", metadata["description"])
                self.assertIn("runtime_type: blackbox_v2", config_text)
                self.assertIn("input_source: data_bridge_current", config_text)
                self.assertIn("runtime_profile: blackbox-v2-v1", config_text)
                self.assertIn("data_schema_version: data-bridge-v1", config_text)
                self.assertIn("  - api-wind-date-v1", config_text)
                self.assertIn("status: active", config_text)
                self.assertIn("version_status: active", config_text)

    def test_v2_parameters_and_help_have_no_persistent_cache_interface(self) -> None:
        """002 V2 保持长窗常量，两个 V2 都不能暴露或写入持久缓存。"""
        expected = {
            "seven_y_current55_lgbm_001_v2": {
                "IS_LONG_WINDOW": False,
                "THRESHOLD": 0.55,
                "FLIP_BELOW": 0.30,
                "FLIP_LOOKBACK": 1,
                "TRAIN_WINDOW": 756,
                "FEATURE_WINDOW": 756,
            },
            "seven_y_current55_lgbm_002_v2": {
                "IS_LONG_WINDOW": True,
                "THRESHOLD": 0.52,
                "FLIP_BELOW": 0.50,
                "FLIP_LOOKBACK": 3,
                "TRAIN_WINDOW": 1008,
                "FEATURE_WINDOW": 1008,
            },
        }
        forbidden_source_fragments = (
            "pickle",
            ".blackbox_model_cache",
            "--cache-dir",
            "persist_models",
            "cache_dir",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            for scheme_id in V2_SCHEME_IDS:
                with self.subTest(scheme_id=scheme_id):
                    script = self._script_for(scheme_id)
                    source = script.read_text(encoding="utf-8")
                    for fragment in forbidden_source_fragments:
                        self.assertNotIn(fragment, source)
                    self.assertIn('"_002_"', source)

                    top_help = self._run_cli(script, "--help", cwd=workdir)
                    predict_help = self._run_cli(
                        script,
                        "predict",
                        "--help",
                        cwd=workdir,
                    )
                    self._assert_cli_success(top_help)
                    self._assert_cli_success(predict_help)
                    self.assertNotIn("--cache-dir", top_help.stdout)
                    self.assertNotIn("--cache-dir", predict_help.stdout)
                    self.assertEqual(
                        self._runtime_parameters(script, cwd=workdir),
                        expected[scheme_id],
                    )

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

    def test_feature_state_controls_refit_selection_purge_and_flip_labels(self) -> None:
        """模型/筛选状态属于 feature_date，训练和翻转都使用 H=1 标签。"""
        probe = """
import json
import runpy
import sys
from pathlib import Path

import numpy as np

script, data_dir, request_path = sys.argv[1:]
namespace = runpy.run_path(script)
request = json.loads(Path(request_path).read_text(encoding="utf-8"))
snapshot = namespace["clipped"](namespace["read_snapshot"](Path(data_dir)), request)
engine = namespace["Engine"](snapshot)
feature_index = engine.positions[request["feature_date"]]

class ProbeModel:
    def __init__(self, **_kwargs):
        self.fit_indices = []
        self.prediction_indices = []

    def fit(self, features, _labels):
        self.fit_indices = [int(value) for value in features[:, 0].tolist()]
        return self

    def predict_proba(self, features):
        self.prediction_indices = [int(value) for value in features[:, 0].tolist()]
        return np.array([[0.1, 0.9]], dtype="float64")

namespace["Engine"].__init__.__globals__["LGBMClassifier"] = ProbeModel
engine.x[:, 0] = np.arange(len(engine.x), dtype="float32")
real_select_features = engine._select_features
def select_for_probe(state_index):
    real_select_features(state_index)
    return np.array([0], dtype="int32")
engine._select_features = select_for_probe
engine.predict(request)
feature_month = engine.dates[feature_index].strftime("%Y-%m")
model, _selected = engine.models[feature_month]

flip_engine = namespace["Engine"](snapshot)
flip_engine.raw_signal = lambda _state_index: 1
flip_engine.truth = np.full(len(flip_engine.truth), -1.0, dtype="float64")
flip_engine.target_by_pred = np.full(
    len(flip_engine.target_by_pred), 1.0, dtype="float64"
)
flip_signal = flip_engine.predict(request)

print(json.dumps({
    "feature_index": feature_index,
    "feature_month": feature_month,
    "model_months": sorted(engine.models),
    "selected_years": sorted(engine.selected_by_year),
    "fit_indices": model.fit_indices,
    "prediction_indices": model.prediction_indices,
    "target_is_h1_shift": bool(np.array_equal(
        engine.target_by_pred[:-1], engine.truth[1:], equal_nan=True
    )),
    "flip_signal": flip_signal,
}, sort_keys=True))
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            fixture = self._write_data_bridge_fixture(workdir / "full")
            request = self._request_for(fixture)
            clipped_dir = workdir / "clipped"
            self._copy_fixture_through_feature_date(fixture, request, clipped_dir)
            request_path = workdir / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")

            for scheme_id in V2_SCHEME_IDS:
                with self.subTest(scheme_id=scheme_id):
                    completed = subprocess.run(
                        [
                            str(self.blackbox_python),
                            "-B",
                            "-c",
                            probe,
                            str(self._script_for(scheme_id)),
                            str(clipped_dir),
                            str(request_path),
                        ],
                        check=False,
                        capture_output=True,
                        cwd=workdir,
                        env=self._runtime_env(),
                        text=True,
                    )
                    self._assert_cli_success(completed)
                    state = json.loads(completed.stdout.strip().splitlines()[-1])
                    self.assertIn(state["feature_month"], state["model_months"])
                    self.assertIn(
                        int(request["feature_date"][:4]), state["selected_years"]
                    )
                    self.assertTrue(state["fit_indices"])
                    self.assertTrue(
                        all(
                            index < state["feature_index"] - 1
                            for index in state["fit_indices"]
                        )
                    )
                    self.assertEqual(
                        state["prediction_indices"], [state["feature_index"]]
                    )
                    self.assertTrue(state["target_is_h1_shift"])
                    self.assertEqual(state["flip_signal"], 1)

    def test_future_mutation_is_invariant_and_no_cache_files_are_created(self) -> None:
        """严格 cutoff 后的四频值不会影响输出，独立进程也不留下缓存。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            workdir = Path(tmpdir)
            fixture = self._write_data_bridge_fixture(workdir / "data")
            request = self._request_for(fixture)
            request_path = workdir / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            initial_files = self._relative_files(workdir)
            baselines: dict[str, bytes] = {}

            for scheme_id in V2_SCHEME_IDS:
                with self.subTest(scheme_id=scheme_id):
                    script = self._script_for(scheme_id)
                    first_output = workdir / f"{scheme_id}-first.json"
                    second_output = workdir / f"{scheme_id}-second.json"
                    for output_path in (first_output, second_output):
                        completed = self._run_cli(
                            script,
                            "predict",
                            "--request",
                            request_path,
                            "--data-dir",
                            fixture.data_dir,
                            "--output",
                            output_path,
                            cwd=workdir,
                        )
                        self._assert_cli_success(completed)
                    baselines[scheme_id] = first_output.read_bytes()
                    self.assertEqual(
                        baselines[scheme_id], second_output.read_bytes()
                    )

            changed = self._mutate_only_post_cutoff_values(fixture.data_dir, request)
            self.assertTrue(changed)
            for scheme_id in V2_SCHEME_IDS:
                with self.subTest(scheme_id=f"{scheme_id}-mutated"):
                    mutated_output = workdir / f"{scheme_id}-mutated.json"
                    mutated = self._run_cli(
                        self._script_for(scheme_id),
                        "predict",
                        "--request",
                        request_path,
                        "--data-dir",
                        fixture.data_dir,
                        "--output",
                        mutated_output,
                        cwd=workdir,
                    )
                    self._assert_cli_success(mutated)
                    self.assertEqual(baselines[scheme_id], mutated_output.read_bytes())

            expected_files = initial_files | {
                f"{scheme_id}-{suffix}.json"
                for scheme_id in V2_SCHEME_IDS
                for suffix in ("first", "second", "mutated")
            }
            self.assertEqual(self._relative_files(workdir), expected_files)
            self.assertFalse(any(workdir.rglob("*.pkl")))
            self.assertFalse(any(workdir.rglob(".blackbox_model_cache")))

    def _script_for(self, scheme_id: str) -> Path:
        return PROJECT_ROOT / "schemes" / scheme_id / "delivery" / f"{scheme_id}.py"

    def _assert_two_file_delivery(self, scheme_id: str) -> None:
        delivery = PROJECT_ROOT / "schemes" / scheme_id / "delivery"
        self.assertTrue(
            (delivery / f"{scheme_id}.py").is_file(),
            f"missing V2 delivery script for {scheme_id}",
        )
        self.assertTrue(
            (delivery / f"{scheme_id}.json").is_file(),
            f"missing V2 delivery metadata for {scheme_id}",
        )

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

    def _runtime_parameters(self, script: Path, *, cwd: Path) -> dict[str, Any]:
        code = (
            "import json, runpy, sys; "
            "namespace = runpy.run_path(sys.argv[1]); "
            "print(json.dumps({name: namespace[name] for name in "
            "('IS_LONG_WINDOW', 'THRESHOLD', 'FLIP_BELOW', 'FLIP_LOOKBACK', "
            "'TRAIN_WINDOW', 'FEATURE_WINDOW')}, sort_keys=True))"
        )
        completed = subprocess.run(
            [str(self.blackbox_python), "-B", "-c", code, str(script)],
            check=False,
            capture_output=True,
            cwd=cwd,
            env=self._runtime_env(),
            text=True,
        )
        self._assert_cli_success(completed)
        return json.loads(completed.stdout.strip().splitlines()[-1])

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

    @staticmethod
    def _relative_files(root: Path) -> set[str]:
        return {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
