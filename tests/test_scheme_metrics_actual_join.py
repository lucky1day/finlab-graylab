"""`/api/metrics` 对重复 actual 事实行的处理必须确定且 fail-closed。

``t_scheme_weekly_actuals``、``t_scheme_monthly_actuals`` 与周期均值 actual 表的唯一键是
``(tenor, predict_date, target_rule)``，而 join 键是
``(tenor, target_date, target_rule)``——两者不同，因此同一事实键可以合法地
出现多行（月频当前库内即有 130 组）。

扇出的明细行会被 ``choose_live_prediction_rows`` 按点位折叠，所以样本数不会
翻倍；真正的问题是**方向冲突时保留哪一条不确定**——两行来自同一条预测，
行 id 相同，比较分不出胜负，结果取决于数据库返回顺序。Dashboard 侧遇到同样
情况会抛 ``DashboardDataError``，metrics 侧不应更宽松。

日频表 ``t_scheme_actuals`` 的唯一键与 join 键相同，结构上不会重复。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services import scheme_metrics  # noqa: E402
from shared.prediction_context import (  # noqa: E402
    MONTHLY_TARGET_RULE,
    WEEKLY_TARGET_RULE,
)
from shared.task_specs import TASK_COMBINATIONS  # noqa: E402

TARGET_DATE = "2026-07-31"


def _engine(*, task_type: str, horizon: int):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE t_scheme_registry (scheme_id TEXT, base_scheme_id TEXT,"
                " name TEXT, description TEXT, horizon INT, task_type TEXT,"
                " frequency TEXT, target_tenor TEXT, schedule_cron TEXT,"
                " schedule_timezone TEXT, status TEXT, deployed_at TEXT,"
                " created_at TEXT, updated_at TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_registry VALUES ('demo__h1__10Y','demo','n','d',"
                f":horizon,:task_type,'weekly','10Y','0 0 * * *','Asia/Shanghai',"
                "'active','2026-01-01','2026-01-01','2026-01-01')"
            ),
            {"horizon": horizon, "task_type": task_type},
        )
        conn.execute(
            text(
                "CREATE TABLE t_target_registry (target_code TEXT, display_name TEXT,"
                " asset_class TEXT, target_type TEXT, sort_order INT, status TEXT,"
                " extra TEXT, created_at TEXT, updated_at TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO t_target_registry VALUES "
                "('10Y','10年国债','bond','yield',1,'active',NULL,NULL,NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE t_scheme_predictions (id INTEGER PRIMARY KEY, run_id INT,"
                " scheme_version TEXT, scheme_id TEXT, target_tenor TEXT, horizon INT,"
                " predict_date TEXT, feature_date TEXT, target_date TEXT,"
                " prediction_phase TEXT, predicted_direction INT, confidence REAL,"
                " model_version TEXT, extra TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO t_scheme_predictions VALUES (1,1,'v1','demo','10Y',"
                f":horizon,'2026-07-25','2026-07-24','{TARGET_DATE}','scheduled_live',"
                "1,0.6,'m1','{}')"
            ),
            {"horizon": horizon},
        )
        conn.execute(
            text(
                "CREATE TABLE t_scheme_actuals (tenor TEXT, trade_date TEXT,"
                " direction_1d INT, direction_5d INT)"
            )
        )
        for name, direction_col in (
            ("t_scheme_weekly_actuals", "direction_weekly"),
            ("t_scheme_monthly_actuals", "direction_monthly"),
            ("t_scheme_period_average_actuals", "actual_direction"),
        ):
            conn.execute(
                text(
                    f"CREATE TABLE {name} (tenor TEXT, predict_date TEXT,"
                    f" target_date TEXT, target_rule TEXT, {direction_col} INT)"
                )
            )
    return engine


def _insert_actuals(engine, table: str, column: str, rule: str, *directions) -> None:
    """同一事实键、不同 predict_date 的多行——唯一键允许，join 键重复。"""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {table} (tenor, predict_date, target_date,"
                f" target_rule, {column}) VALUES (:t,:p,:d,:r,:v)"
            ),
            [
                {
                    "t": "10Y",
                    "p": f"2026-07-{25 + index}",
                    "d": TARGET_DATE,
                    "r": rule,
                    "v": value,
                }
                for index, value in enumerate(directions)
            ],
        )


class WeeklyActualJoinTests(unittest.TestCase):
    def _metrics(self, *directions):
        engine = _engine(task_type="weekly_point", horizon=6)
        self.addCleanup(engine.dispose)
        _insert_actuals(
            engine,
            "t_scheme_weekly_actuals",
            "direction_weekly",
            WEEKLY_TARGET_RULE,
            *directions,
        )
        return lambda: scheme_metrics(engine, "demo__h1__10Y")

    def test_single_fact_matches(self) -> None:
        """对照组：无重复时行为不变。"""
        result = self._metrics(-1)()
        self.assertEqual(len(result["daily_rows"]), 1)
        self.assertEqual(result["daily_rows"][0]["actual_direction"], -1)

    def test_duplicate_same_direction_collapses(self) -> None:
        """方向一致的重复行折叠为一条，不影响样本数。"""
        result = self._metrics(1, 1)()
        self.assertEqual(len(result["daily_rows"]), 1)
        self.assertEqual(result["daily_rows"][0]["actual_direction"], 1)
        self.assertEqual(result["summary"]["samples"], 1)

    def test_conflicting_directions_fail_closed(self) -> None:
        """方向冲突必须报错，不得静默取任意一条。"""
        with self.assertRaises(ValueError) as caught:
            self._metrics(1, -1)()
        self.assertIn("conflicting weekly actual directions", str(caught.exception))


class MonthlyActualJoinTests(unittest.TestCase):
    def _metrics(self, *directions):
        engine = _engine(task_type="monthly", horizon=30)
        self.addCleanup(engine.dispose)
        _insert_actuals(
            engine,
            "t_scheme_monthly_actuals",
            "direction_monthly",
            MONTHLY_TARGET_RULE,
            *directions,
        )
        return lambda: scheme_metrics(engine, "demo__h1__10Y")

    def test_duplicate_same_direction_collapses(self) -> None:
        result = self._metrics(1, 1)()
        self.assertEqual(len(result["daily_rows"]), 1)
        self.assertEqual(result["daily_rows"][0]["actual_direction"], 1)

    def test_conflicting_directions_fail_closed(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._metrics(1, -1)()
        self.assertIn("conflicting monthly actual directions", str(caught.exception))


class PeriodAverageActualJoinTests(unittest.TestCase):
    def _metrics(self, task_type: str, *directions):
        engine = _engine(task_type=task_type, horizon=1)
        self.addCleanup(engine.dispose)
        _insert_actuals(
            engine,
            "t_scheme_period_average_actuals",
            "actual_direction",
            TASK_COMBINATIONS[task_type][1],
            *directions,
        )
        return lambda: scheme_metrics(engine, "demo__h1__10Y")

    def test_each_period_task_joins_its_own_target_rule(self) -> None:
        for task_type in (
            "monthly_average",
            "quarterly_average",
            "annual_average",
        ):
            with self.subTest(task_type=task_type):
                result = self._metrics(task_type, -1)()
                self.assertEqual(len(result["daily_rows"]), 1)
                self.assertEqual(result["daily_rows"][0]["actual_direction"], -1)

    def test_duplicate_same_direction_collapses(self) -> None:
        result = self._metrics("quarterly_average", 1, 1)()
        self.assertEqual(len(result["daily_rows"]), 1)
        self.assertEqual(result["summary"]["samples"], 1)

    def test_conflicting_directions_fail_closed(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._metrics("annual_average", 1, -1)()
        self.assertIn(
            "conflicting period_average actual directions",
            str(caught.exception),
        )


if __name__ == "__main__":
    unittest.main()
