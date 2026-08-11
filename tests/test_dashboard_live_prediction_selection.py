"""实盘 canonical 预测选择的周频判定回归测试。

周频方案在同一 ``target_date`` 上可能存在多行预测（补发、重跑、灰度与正式
实盘并存）。canonical 规则要求先取更新的 ``feature_date``，同 feature_date
再取更早的 ``predict_date``；日频/点位方案才按行 id 取最新一行。

判定该走哪条规则时，Registry ``task_type`` 是唯一权威依据。仅按 ``horizon``
推断会把 ``horizon != 6`` 的周频方案误判成点位方案，从而选出非 canonical 行。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.factor_lab_dashboard_semantics import (  # noqa: E402
    choose_live_prediction_rows,
)

DISPLAY_UNTIL = "2026-08-11"
TARGET_DATE = "2026-07-31"


def _row(
    *,
    row_id: int,
    horizon: int,
    task_type: str | None,
    predict_date: str,
    feature_date: str,
    direction: int,
) -> dict:
    extra: dict[str, object] = {"feature_date": feature_date}
    if task_type is not None:
        extra["task_type"] = task_type
    return {
        "id": row_id,
        "scheme_id": "weekly_10y_lgbm_point_v1",
        "target_tenor": "10Y",
        "horizon": horizon,
        "predict_date": predict_date,
        "feature_date": feature_date,
        "target_date": TARGET_DATE,
        "predicted_direction": direction,
        "extra": extra,
    }


def _rows(*, horizon: int, task_type: str | None) -> list[dict]:
    """同一 target_date 的两行：id 较小的一行 feature_date 更新，是 canonical。"""
    return [
        _row(
            row_id=10,
            horizon=horizon,
            task_type=task_type,
            predict_date="2026-07-25",
            feature_date="2026-07-24",
            direction=1,
        ),
        _row(
            row_id=20,
            horizon=horizon,
            task_type=task_type,
            predict_date="2026-07-26",
            feature_date="2026-07-23",
            direction=-1,
        ),
    ]


class WeeklyCanonicalSelectionTests(unittest.TestCase):
    def _selected(self, rows: list[dict]) -> dict:
        selected = choose_live_prediction_rows(rows, display_until=DISPLAY_UNTIL)
        self.assertEqual(len(selected), 1)
        return dict(selected[0])

    def test_legacy_weekly_horizon_selects_newest_feature_date(self) -> None:
        """存量周频方案（horizon=6）已按 canonical 周频规则选择。"""
        selected = self._selected(_rows(horizon=6, task_type=None))
        self.assertEqual(selected["id"], 10)
        self.assertEqual(selected["feature_date"], "2026-07-24")

    def test_weekly_task_type_selects_newest_feature_date(self) -> None:
        """horizon != 6 的周频方案必须同样按 canonical 周频规则选择。"""
        selected = self._selected(_rows(horizon=1, task_type="weekly_point"))
        self.assertEqual(selected["id"], 10)
        self.assertEqual(selected["feature_date"], "2026-07-24")

    def test_weekly_average_task_type_selects_newest_feature_date(self) -> None:
        """weekly_average 与 weekly_point 适用同一 canonical 规则。"""
        selected = self._selected(_rows(horizon=1, task_type="weekly_average"))
        self.assertEqual(selected["id"], 10)
        self.assertEqual(selected["feature_date"], "2026-07-24")

    def test_point_task_type_keeps_latest_row_rule(self) -> None:
        """点位方案不受影响，仍按最新行 id 选择。"""
        selected = self._selected(_rows(horizon=1, task_type="T+1"))
        self.assertEqual(selected["id"], 20)

    def test_rows_without_task_type_keep_horizon_behaviour(self) -> None:
        """缺 task_type 的存量点位行保持既有行为，不因本次改动变化。"""
        selected = self._selected(_rows(horizon=1, task_type=None))
        self.assertEqual(selected["id"], 20)

    def test_mixed_task_type_rows_are_order_independent(self) -> None:
        """同一点位内混有缺 task_type 的行时，结果不得依赖输入顺序。

        人工补发行可能不带 ``extra.task_type``。周频判定若只看比较的一侧，
        同一批数据换个顺序就会选出相反方向。
        """
        marked = _row(
            row_id=10,
            horizon=1,
            task_type="weekly_point",
            predict_date="2026-07-25",
            feature_date="2026-07-24",
            direction=1,
        )
        unmarked = _row(
            row_id=20,
            horizon=1,
            task_type=None,
            predict_date="2026-07-26",
            feature_date="2026-07-23",
            direction=-1,
        )
        forward = self._selected([marked, unmarked])
        backward = self._selected([unmarked, marked])
        self.assertEqual(forward["id"], backward["id"])
        self.assertEqual(forward["id"], 10)


if __name__ == "__main__":
    unittest.main()
