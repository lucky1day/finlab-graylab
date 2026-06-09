from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class TenorMappingTests(unittest.TestCase):
    def test_shared_mapping_is_single_source_for_actuals_updaters(self) -> None:
        from shared.tenor_mapping import TENOR_TO_INDICATOR
        from scheduler import daily_actuals_updater, weekly_actuals_updater

        self.assertEqual(TENOR_TO_INDICATOR["10Y"], "TB0YWI0C")
        self.assertIs(daily_actuals_updater.TENOR_TO_INDICATOR, TENOR_TO_INDICATOR)
        self.assertIs(weekly_actuals_updater.TENOR_TO_INDICATOR, TENOR_TO_INDICATOR)

    def test_active_scheme_tenors_are_read_from_config(self) -> None:
        from scheduler.daily_actuals_updater import active_scheme_tenors

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_scheme(root, "daily_live", "daily", "active", ["5Y", "30Y"])
            _write_scheme(root, "daily_paused", "daily", "paused", ["1Y"])
            _write_scheme(root, "weekly_live", "weekly", "active", ["10Y"])

            self.assertEqual(active_scheme_tenors(root, frequency="daily"), ["30Y", "5Y"])


def _write_scheme(root: Path, scheme_id: str, frequency: str, status: str, tenors: list[str]) -> None:
    scheme_dir = root / scheme_id
    scheme_dir.mkdir(parents=True)
    scheme_dir.joinpath("config.yaml").write_text(
        "\n".join(
            [
                f"scheme_id: {scheme_id}",
                "name: Demo",
                "description: Demo",
                "horizon: 1",
                f"tenors: {tenors!r}",
                f"frequency: {frequency}",
                "schedule:",
                '  cron: "25 9 * * 1-5"',
                '  timezone: "Asia/Shanghai"',
                "entry_point: predict.run",
                f"status: {status}",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
