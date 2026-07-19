from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared.blackbox_v2.contracts import BlackboxMetadata, load_request, load_requests
from shared.blackbox_v2.snapshot import CutoffKeys


class BlackboxV2RequestFactoryTests(unittest.TestCase):
    def test_builds_daily_live_request_from_platform_calendar(self) -> None:
        from shared.blackbox_v2.requests import build_live_request

        request = build_live_request(
            _metadata("T+5", horizon=5),
            predict_date="2026-07-16",
            calendar=_Calendar(),
            cutoffs=_cutoffs(),
        )

        self.assertEqual(request.predict_date, "2026-07-16")
        self.assertEqual(request.feature_date, "2026-07-15")
        self.assertEqual(request.target_date, "2026-07-22")
        self.assertEqual(request.weekly_cutoff_key, "202627")
        self.assertEqual(
            request.request_id,
            "blackbox_trial:2026-07-16:2026-07-15:2026-07-22",
        )

    def test_builds_weekly_live_request_from_platform_calendar(self) -> None:
        from shared.blackbox_v2.requests import build_live_request

        request = build_live_request(
            _metadata("weekly_point"),
            predict_date="2026-07-20",
            calendar=_Calendar(),
            cutoffs=_cutoffs(),
        )

        self.assertEqual(request.feature_date, "2026-07-17")
        self.assertEqual(request.target_date, "2026-07-24")

    def test_writes_single_and_batch_requests_atomically(self) -> None:
        from shared.blackbox_v2.requests import build_request, write_request, write_requests

        first = build_request(
            scheme_id="blackbox_trial",
            predict_date="2026-07-15",
            feature_date="2026-07-15",
            target_date="2026-07-16",
            cutoffs=_cutoffs(),
        )
        second = build_request(
            scheme_id="blackbox_trial",
            predict_date="2026-07-16",
            feature_date="2026-07-16",
            target_date="2026-07-17",
            cutoffs=_cutoffs(),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            json_path = write_request(first, root / "request.json")
            csv_path = write_requests([first, second], root / "requests.csv")

            parsed_single = load_request(json_path)
            parsed_batch = load_requests(csv_path)

        self.assertEqual(parsed_single, first)
        self.assertEqual(parsed_batch, [first, second])

    def test_rejects_empty_batch(self) -> None:
        from shared.blackbox_v2.requests import write_requests

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ValueError, "at least one"):
                write_requests([], Path(tmpdir) / "requests.csv")


class _Calendar:
    def previous_trading_day(self, value: str) -> str:
        return {
            "2026-07-16": "2026-07-15",
            "2026-07-20": "2026-07-17",
        }[value]

    def nth_trading_day_after(self, value: str, n: int) -> str:
        self.last_n = n
        return "2026-07-22"

    def week_id_for_date(self, value: str) -> int:
        return {"2026-07-17": 202628, "2026-07-20": 202629}[value]

    def week_id_to_last_trading_day(self, value: int) -> str:
        return {202628: "2026-07-17", 202629: "2026-07-24"}[value]

    def next_trading_days(self, value: str, count: int) -> list[str]:
        return ["2026-07-20"]


def _metadata(task_type: str, *, horizon: int = 1) -> BlackboxMetadata:
    target_rules = {
        "T+5": "target_date_yield_vs_feature_date_yield",
        "weekly_point": "target_week_end_yield_vs_feature_week_end_yield",
    }
    frequencies = {"T+5": "daily", "weekly_point": "weekly"}
    return BlackboxMetadata(
        schema_version="1.0",
        scheme_id="blackbox_trial",
        name="Trial",
        algorithm_version="1.0.0",
        target_tenor="10Y",
        task_type=task_type,
        horizon=horizon,
        target_rule=target_rules[task_type],
        frequency=frequencies[task_type],
    )


def _cutoffs() -> CutoffKeys:
    return CutoffKeys(
        daily_cutoff_key="2026-07-15",
        weekly_cutoff_key="202627",
        monthly_cutoff_key="202606",
    )


if __name__ == "__main__":
    unittest.main()
