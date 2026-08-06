"""CGB V1.28 周频批量适配层的截止身份隔离回归测试。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DELIVERY_PATH = (
    PROJECT_ROOT
    / "schemes"
    / "cgb_causal_wk_1y_v128"
    / "delivery"
    / "cgb_causal_wk_1y_v128.py"
)


def _load_delivery_module():
    module_name = "_test_cgb_causal_wk_1y_v128_delivery"
    spec = importlib.util.spec_from_file_location(module_name, DELIVERY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class CgbCausalWeeklyBatchCutoffIdentityTests(unittest.TestCase):
    """仅替换耗时算法管线，保留真实截断和批量适配逻辑。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.delivery = _load_delivery_module()

    def setUp(self) -> None:
        self.frames = self._frames()
        self._log_patcher = patch.object(self.delivery, "_log")
        self._log_patcher.start()
        self.addCleanup(self._log_patcher.stop)

    @staticmethod
    def _frames() -> dict:
        start = pd.Timestamp("2026-01-05")
        weekly_rows: list[dict[str, int]] = []
        daily_rows: list[dict[str, object]] = []
        calendar_rows: list[dict[str, object]] = []
        for week_index in range(8):
            week_id = 202601 + week_index
            weekly_rows.append({"week_id": week_id})
            for business_day in range(5):
                value = start + pd.Timedelta(days=week_index * 7 + business_day)
                date_text = value.strftime("%Y-%m-%d")
                daily_rows.append({"date": date_text, "marker": len(daily_rows)})
                calendar_rows.append({"rdate": date_text, "week_id": week_id})

        daily = pd.DataFrame(daily_rows)
        calendar = pd.DataFrame(calendar_rows)
        return {
            "weekly": pd.DataFrame(weekly_rows),
            "daily": daily,
            "daily_key": pd.to_datetime(daily["date"]),
            "calendar": calendar,
            "calendar_rdate": pd.to_datetime(calendar["rdate"]),
        }

    @staticmethod
    def _request(
        request_id: str,
        *,
        week_index: int,
        business_day: int = 4,
        monthly_cutoff_key: str = "202601",
    ) -> dict[str, str]:
        feature_date = (
            pd.Timestamp("2026-01-05")
            + pd.Timedelta(days=week_index * 7 + business_day)
        ).strftime("%Y-%m-%d")
        return {
            "request_id": request_id,
            "predict_date": feature_date,
            "feature_date": feature_date,
            "target_date": "2026-12-31",
            "daily_cutoff_key": feature_date,
            "weekly_cutoff_key": f"{202601 + week_index:06d}",
            "monthly_cutoff_key": monthly_cutoff_key,
        }

    @staticmethod
    def _fake_pipeline(calls: list[str]):
        """模拟因果 walk-forward：每一周只由该周最后可见日决定。"""

        def run(cut: dict, label: str) -> pd.DataFrame:
            calls.append(label)
            calendar = cut["api_wind_date.csv"].copy()
            calendar["rdate"] = pd.to_datetime(calendar["rdate"])
            by_date = dict(
                zip(
                    calendar["rdate"],
                    pd.to_numeric(calendar["week_id"], errors="raise").astype(int),
                )
            )
            daily_dates = pd.to_datetime(cut["daily_output.csv"]["date"])
            daily_weeks = daily_dates.map(by_date)
            latest_date_by_week = (
                pd.DataFrame({"week_id": daily_weeks, "date": daily_dates})
                .groupby("week_id", sort=True)["date"]
                .max()
            )
            rows: list[dict[str, object]] = []
            for week_id in cut["weekly_output.csv"]["week_id"].astype(int):
                latest = latest_date_by_week.loc[week_id]
                direction = 1 if int(latest.day) % 2 else -1
                probability = 0.75 if direction == 1 else 0.25
                rows.append(
                    {
                        "week_id": int(week_id),
                        "pred_label": direction,
                        "prob_up": probability,
                        "center_pred_label": direction,
                        "center_prob_up": probability,
                    }
                )
            return pd.DataFrame(rows)

        return run

    def _individual_directions(self, requests: list[dict[str, str]]) -> list[int]:
        calls: list[str] = []
        with patch.object(
            self.delivery,
            "_run_pipeline",
            side_effect=self._fake_pipeline(calls),
        ):
            return [self.delivery.predict_one(self.frames, request) for request in requests]

    def test_same_week_different_daily_cutoffs_never_attempt_one_pass(self) -> None:
        """同一周日度截止不同，绝不可按 week_id 复用一行。"""
        requests = [
            self._request("monday", week_index=0, business_day=0),
            self._request("tuesday", week_index=0, business_day=1),
            *[
                self._request(f"full-{index}", week_index=index)
                for index in range(1, 5)
            ],
        ]
        expected = self._individual_directions(requests)
        calls: list[str] = []

        with patch.object(
            self.delivery,
            "_run_pipeline",
            side_effect=self._fake_pipeline(calls),
        ):
            actual = self.delivery.predict_batch(self.frames, requests)

        self.assertEqual(actual, expected)
        self.assertFalse(any(label.startswith("one-pass") for label in calls))
        self.assertEqual(calls, [request["request_id"] for request in requests])

    def test_same_week_different_monthly_cutoffs_never_attempt_one_pass(self) -> None:
        """即使算法当前不读月表，批量身份仍必须是完整三重截止键。"""
        requests = [
            self._request("month-a", week_index=0, monthly_cutoff_key="202601"),
            self._request("month-b", week_index=0, monthly_cutoff_key="202602"),
            *[
                self._request(f"full-{index}", week_index=index)
                for index in range(1, 5)
            ],
        ]
        calls: list[str] = []

        with patch.object(
            self.delivery,
            "_run_pipeline",
            side_effect=self._fake_pipeline(calls),
        ):
            actual = self.delivery.predict_batch(self.frames, requests)

        self.assertEqual(actual, self._individual_directions(requests))
        self.assertFalse(any(label.startswith("one-pass") for label in calls))

    def test_incomplete_prior_week_never_attempts_one_pass(self) -> None:
        """前一周若只截到周内某日，后续周的 one-pass 会泄漏同周余下日。"""
        requests = [
            self._request("incomplete", week_index=0, business_day=0),
            *[
                self._request(f"full-{index}", week_index=index)
                for index in range(1, 6)
            ],
        ]
        expected = self._individual_directions(requests)
        calls: list[str] = []

        with patch.object(
            self.delivery,
            "_run_pipeline",
            side_effect=self._fake_pipeline(calls),
        ):
            actual = self.delivery.predict_batch(self.frames, requests)

        self.assertEqual(actual, expected)
        self.assertFalse(any(label.startswith("one-pass") for label in calls))

    def test_duplicate_full_signatures_run_once_then_fan_out(self) -> None:
        """完全相同的三重截止键只计算一次，并按原始请求顺序回填。"""
        requests = [
            self._request(f"duplicate-{index}", week_index=0)
            for index in range(6)
        ]
        expected = self._individual_directions(requests)
        calls: list[str] = []

        with patch.object(
            self.delivery,
            "_run_pipeline",
            side_effect=self._fake_pipeline(calls),
        ):
            actual = self.delivery.predict_batch(self.frames, requests)

        self.assertEqual(actual, expected)
        self.assertEqual(
            sum(label.startswith("one-pass") for label in calls),
            0,
        )
        self.assertEqual(
            sum(label.startswith("verify:") for label in calls),
            0,
        )
        self.assertEqual(calls, ["duplicate-0"])

    def test_six_complete_weeks_use_one_pass_with_three_verifications(self) -> None:
        """各周均到该周最后实际日时，一次 walk-forward 可覆盖六周。"""
        requests = [
            self._request(f"week-{index}", week_index=index)
            for index in range(6)
        ]
        expected = self._individual_directions(requests)
        calls: list[str] = []

        with patch.object(
            self.delivery,
            "_run_pipeline",
            side_effect=self._fake_pipeline(calls),
        ):
            actual = self.delivery.predict_batch(self.frames, requests)

        self.assertEqual(actual, expected)
        self.assertEqual(
            sum(label.startswith("one-pass") for label in calls),
            1,
        )
        self.assertEqual(
            sum(label.startswith("verify:") for label in calls),
            3,
        )

    def test_reversed_hundred_request_conflict_has_identical_mapping(self) -> None:
        """冲突批次不依赖输入顺序：正反 100 条均逐条隔离。"""
        requests = [
            self._request(
                f"request-{index:03d}",
                week_index=0,
                business_day=index % 2,
            )
            for index in range(100)
        ]

        def execute(batch: list[dict[str, str]]) -> tuple[list[int], list[str]]:
            calls: list[str] = []
            with patch.object(
                self.delivery,
                "_run_pipeline",
                side_effect=self._fake_pipeline(calls),
            ):
                return self.delivery.predict_batch(self.frames, batch), calls

        forward, forward_calls = execute(requests)
        reversed_requests = list(reversed(requests))
        backward, backward_calls = execute(reversed_requests)

        self.assertEqual(
            dict(zip((item["request_id"] for item in requests), forward)),
            dict(
                zip(
                    (item["request_id"] for item in reversed_requests),
                    backward,
                )
            ),
        )
        self.assertFalse(any(label.startswith("one-pass") for label in forward_calls))
        self.assertFalse(any(label.startswith("one-pass") for label in backward_calls))
        self.assertEqual(forward_calls, ["request-000", "request-001"])
        self.assertEqual(backward_calls, ["request-000", "request-001"])


if __name__ == "__main__":
    unittest.main()
