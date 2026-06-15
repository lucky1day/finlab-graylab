from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class SchedulerDiscoveryFailClosedTests(unittest.TestCase):
    def test_load_scheme_config_rejects_missing_status(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_scheme_config(Path(tmpdir), status_line=None)

            with self.assertRaisesRegex(ValueError, "status must be active or paused"):
                load_scheme_config(scheme_dir / "config.yaml")

    def test_load_scheme_config_rejects_invalid_frequency(self) -> None:
        from scheduler.discovery import load_scheme_config

        with tempfile.TemporaryDirectory() as tmpdir:
            scheme_dir = _write_scheme_config(Path(tmpdir), frequency="hourly")

            with self.assertRaisesRegex(ValueError, "frequency must be one of daily, weekly, monthly"):
                load_scheme_config(scheme_dir / "config.yaml")

    def test_discover_schemes_skips_invalid_config_in_non_strict_mode(self) -> None:
        from scheduler.discovery import discover_schemes

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_scheme_config(root, scheme_id="valid_daily", status_line="status: active")
            _write_scheme_config(root, scheme_id="missing_status", status_line=None)

            configs = discover_schemes(root, strict=False)

        self.assertEqual([cfg.scheme_id for cfg in configs], ["valid_daily"])


def _write_scheme_config(
    root: Path,
    *,
    scheme_id: str = "demo_daily",
    frequency: str = "daily",
    status_line: str | None = "status: active",
) -> Path:
    scheme_dir = root / scheme_id
    scheme_dir.mkdir(parents=True)
    lines = [
        f"scheme_id: {scheme_id}",
        "name: Demo Daily",
        "description: Demo scheme",
        "horizon: 1",
        "task_type: T+1",
        'tenors: ["10Y"]',
        f"frequency: {frequency}",
        "schedule:",
        "  cron: '3 7 * * 1-5'",
        "  timezone: Asia/Shanghai",
        "entry_point: predict.run",
        "input_spec:",
        "  data_version: shared_data_service_daily.v1",
        '  required_columns: ["date", "TB0YWI0C"]',
    ]
    if status_line is not None:
        lines.append(status_line)
    (scheme_dir / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return scheme_dir


if __name__ == "__main__":
    unittest.main()
