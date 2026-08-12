"""生产健康检查在日历覆盖耗尽时必须 fail-closed。

`CalendarService.is_trading_day` 对未收录日期返回 False 是既定契约。健康检查
据此把「日历没续期」与「今天是节假日」判成同一件事：`evaluate_daily_health`
在 `is_trading_day` 为假时提前返回，跳过其后全部预测缺失判定。于是日历一旦
耗尽，当天预测**全缺失**时这个脚本仍报无发现——它正是用来发现生产缺口的
工具，却恰好在缺口最可能发生时失明。

同一根因还有第二条路径：`_expected_daily_feature_date` 无法解析上一交易日时
返回 None，`_source_blocked_schemes` 随即返回空列表，source 阻塞分析被静默跳过。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 覆盖 2026-06-01..2026-06-07；之后无任何日历行。
CALENDAR_ROWS = [
    ("2026-06-01", "1"),
    ("2026-06-02", "1"),
    ("2026-06-03", "0"),  # 覆盖范围内的真实假期
    ("2026-06-04", "1"),
    ("2026-06-05", "1"),
    ("2026-06-06", "0"),
    ("2026-06-07", "0"),
]
COVERED_HOLIDAY = "2026-06-03"
UNCOVERED = "2026-07-01"


class LoadSnapshotCoverageTests(unittest.TestCase):
    """日历未覆盖时必须在读任何业务表之前失败。"""

    def setUp(self) -> None:
        # 只建日历表：守卫若不前置，load_snapshot 会先撞上缺失的业务表，
        # 抛出的就不是 ValueError，本测试据此同时锁定「失败」与「前置」。
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE t_trade_calendar "
                    "(rdate TEXT PRIMARY KEY, trade_flag TEXT)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO t_trade_calendar (rdate, trade_flag) "
                    "VALUES (:rdate, :trade_flag)"
                ),
                [{"rdate": r, "trade_flag": f} for r, f in CALENDAR_ROWS],
            )
        self.addCleanup(self.engine.dispose)

    def test_uncovered_predict_date_fails_closed_before_any_query(self) -> None:
        from scripts import check_production_daily_health as health

        with self.assertRaises(ValueError) as ctx:
            health.load_snapshot(self.engine, predict_date=UNCOVERED)
        self.assertIn(UNCOVERED, str(ctx.exception))


class EvaluateDailyHealthTests(unittest.TestCase):
    """确认早退跳过的正是预测缺失判定，且真实节假日语义不变。"""

    @staticmethod
    def _snapshot(*, is_trading_day: bool, predictions_count: int):
        from scripts.check_production_daily_health import DailyHealthSnapshot

        return DailyHealthSnapshot(
            predict_date="2026-06-04",
            expected_feature_date="2026-06-02",
            is_trading_day=is_trading_day,
            active_daily_base_schemes=("demo",),
            successful_daily_run_schemes=(),
            predictions_count=predictions_count,
            run_prediction_counts=(),
            prediction_date_checks=(),
            actual_watermarks=(),
        )

    def _codes(self, snapshot) -> set[str]:
        from scripts.check_production_daily_health import evaluate_daily_health

        return {finding.code for finding in evaluate_daily_health(snapshot)}

    def test_trading_day_without_predictions_is_reported(self) -> None:
        codes = self._codes(
            self._snapshot(is_trading_day=True, predictions_count=0)
        )
        self.assertIn("daily_predictions_missing", codes)

    def test_non_trading_day_keeps_skipping_prediction_checks(self) -> None:
        """真实节假日的既有语义不变——修复不得把节假日也一起报错。"""
        codes = self._codes(
            self._snapshot(is_trading_day=False, predictions_count=0)
        )
        self.assertNotIn("daily_predictions_missing", codes)


if __name__ == "__main__":
    unittest.main()
