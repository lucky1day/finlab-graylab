from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness.context import GateContext
from harness.gates.input_gate import InputGate


class InputGateMonthlyTests(unittest.TestCase):
    def test_monthly_primary_input_uses_monthly_cutoff_not_previous_trading_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scheme_dir = root / "schemes" / "demo_monthly"
            scheme_dir.mkdir(parents=True)
            (scheme_dir / "config.yaml").write_text(
                "\n".join(
                    [
                        "scheme_id: demo_monthly",
                        "name: Demo Monthly",
                        "description: Demo",
                        "horizon: 30",
                        "task_type: monthly",
                        'tenors: ["10Y"]',
                        "frequency: monthly",
                        "schedule:",
                        '  cron: "0 18 15 * *"',
                        '  timezone: "Asia/Shanghai"',
                        "entry_point: predict.run",
                        "status: paused",
                        "input_spec:",
                        "  data_version: shared_data_service_monthly.v1",
                        '  required_columns: ["month_id", "M0000001"]',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            artifact = SimpleNamespace(
                path=Path("/tmp/monthly.csv"),
                source="shared_data_service_monthly",
                data_version="shared_data_service_monthly.v1",
                row_count=1,
                column_count=2,
                columns=["month_id", "M0000001"],
                date_coverage={"field": "month_id", "start": "202604", "end": "202604"},
                quality_flags={},
            )
            engine = SimpleNamespace(dispose=lambda: None)

            with (
                patch("harness.gates.input_gate.get_calendar", return_value=_MonthlyCalendar()),
                patch("harness.gates.input_gate.build_monthly_input_artifact", return_value=artifact) as build_monthly,
            ):
                result = InputGate().run(
                    GateContext(
                        scheme_id="demo_monthly",
                        predict_date="2026-04-15",
                        project_root=root,
                        report_dir=root / "reports",
                        engine_factory=lambda: engine,
                    )
                )

        self.assertTrue(result.passed, result.errors)
        kwargs = build_monthly.call_args.kwargs
        self.assertEqual(kwargs["predict_date"], "2026-04-15")
        self.assertEqual(kwargs["end_date"], "2026-04-15")
        evidence = {item.key: item.value for item in result.evidence}
        self.assertEqual(evidence["feature_date"], "2026-04-15")
        self.assertIsNone(evidence["feature_week_id"])


class _MonthlyCalendar:
    def previous_trading_day(self, day: str) -> str:
        return {"2026-04-15": "2026-04-14"}[day]

    def week_id_for_date(self, day: str) -> int | None:
        return None

    def is_trading_day(self, day: str) -> bool:
        return day in {"2026-04-15", "2026-05-15"}

    def next_trading_days(self, day: str, count: int) -> list[str]:
        return []


if __name__ == "__main__":
    unittest.main()
