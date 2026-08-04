from __future__ import annotations

import csv
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DELIVERY_SCRIPT = (
    PROJECT_ROOT
    / "schemes"
    / "one_y_t1_quote_state_hv_v1"
    / "delivery"
    / "one_y_t1_quote_state_hv_v1.py"
)
SCHEMA_PATH = PROJECT_ROOT / "shared" / "blackbox_v2" / "data_bridge_v1_schema.json"


class OneYT1QuoteStateHvV1DeliveryTests(unittest.TestCase):
    """1Y T+1 交付必须遵守 DataBridge 的增量列兼容约定。"""

    @classmethod
    def setUpClass(cls) -> None:
        conda = shutil.which("conda")
        if conda is None:
            raise RuntimeError("Blackbox V2 delivery test requires conda")
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

    def test_accepts_unused_additive_columns_for_all_databridge_files(self) -> None:
        """基线列不变时，未消费的新增业务列不能阻断交付启动。"""
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            for filename, specification in schema["files"].items():
                columns = [*specification["columns"], "UNUSED_ADDITIVE_FACTOR"]
                with (data_dir / filename).open("w", encoding="utf-8", newline="") as handle:
                    csv.writer(handle).writerow(columns)

            completed = self._validate_headers(data_dir)

        self.assertEqual(
            completed.returncode,
            0,
            "delivery rejected compatible additive DataBridge headers:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )

    def test_still_rejects_a_changed_time_key(self) -> None:
        """增量兼容不能放宽每个文件的首列时间键。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "weekly_output.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(["unexpected", "week_id"])
            completed = self._validate_header(path)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("does not match data-bridge-v1 schema", completed.stderr)

    def _validate_headers(self, data_dir: Path) -> subprocess.CompletedProcess[str]:
        code = (
            "import runpy, sys; "
            "from pathlib import Path; "
            "namespace = runpy.run_path(sys.argv[1]); "
            "root = Path(sys.argv[2]); "
            "[namespace['_validate_schema_header'](root / name) for name in "
            "('daily_output.csv', 'weekly_output.csv', 'monthly_output.csv')]"
        )
        return self._run(code, data_dir)

    def _validate_header(self, path: Path) -> subprocess.CompletedProcess[str]:
        code = (
            "import runpy, sys; "
            "from pathlib import Path; "
            "namespace = runpy.run_path(sys.argv[1]); "
            "namespace['_validate_schema_header'](Path(sys.argv[2]))"
        )
        return self._run(code, path)

    def _run(self, code: str, input_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.blackbox_python), "-B", "-c", code, str(DELIVERY_SCRIPT), str(input_path)],
            check=False,
            capture_output=True,
            cwd=PROJECT_ROOT,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
