"""单个 target 的日历边界不得让整份信号缺口报告不可用。

`_expected` 逐 (target, predict_date) 构造预测上下文。日历尚未覆盖某个
predict_date 的目标周或后续交易日时，平台当初同样产不出该信号——它不是缺口。

此前该情形被升级成整份报告的 `SignalGapReportError`，而 dashboard
（`backend/factor_lab_dashboard.py`）不做隔离，异常直穿到
`backend/main.py` 的兜底 except，整个因子实验室页面变成 503——registry、
指标、回测全部不可用，而不是只有 signal_status 这一列降级。

对照：`harness/signal_gap_plan` 面对完全相同的异常是逐 target 记 blocker 并
continue，两个模块对同一件事的失败模式本不该相反。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.signal_gap_report import SignalTarget, _expected  # noqa: E402

TRADING_DAYS = ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04")


class _BoundaryCalendar:
    """日历只覆盖到 TRADING_DAYS[-1]，之后的 horizon 推进会耗尽。"""

    def predict_dates(self, frequency: str, start_date: str, end_date: str):
        return tuple(d for d in TRADING_DAYS if start_date <= d <= end_date)

    def previous_trading_day(self, value: str) -> str:
        earlier = [d for d in TRADING_DAYS if d < str(value)[:10]]
        if not earlier:
            raise ValueError(f"no previous trading day before {value}")
        return earlier[-1]

    def nth_trading_day_after(self, value: str, n: int) -> str:
        later = [d for d in TRADING_DAYS if d > str(value)[:10]]
        if len(later) < n:
            raise ValueError("not enough following trading days")
        return later[n - 1]


def _target(scheme_id: str, horizon: int) -> SignalTarget:
    return SignalTarget(
        registry_scheme_id=f"{scheme_id}__h{horizon}__10Y",
        base_scheme_id=scheme_id,
        runtime_type="blackbox_v2",
        frequency="daily",
        task_type="T+1" if horizon == 1 else "T+5",
        target_tenor="10Y",
        horizon=horizon,
        available_after="2026-06-01",
        scheme_version="v1",
    )


class CalendarBoundaryIsolationTests(unittest.TestCase):
    def test_boundary_target_does_not_break_whole_report(self) -> None:
        """horizon=5 在日历末端算不出 target_date，不得影响 horizon=1。"""
        near = _target("near", 1)
        far = _target("far", 5)
        cases = _expected(
            [near, far], _BoundaryCalendar(), "2026-06-01", "2026-06-04"
        )
        produced = {case.base_scheme_id for case in cases}
        self.assertIn("near", produced)

    def test_computable_cases_are_unchanged(self) -> None:
        """可计算的 case 全部保留，数量与内容不受边界 target 影响。"""
        near = _target("near", 1)
        far = _target("far", 5)
        alone = _expected([near], _BoundaryCalendar(), "2026-06-01", "2026-06-04")
        together = _expected(
            [near, far], _BoundaryCalendar(), "2026-06-01", "2026-06-04"
        )
        near_only = [c for c in together if c.base_scheme_id == "near"]
        self.assertEqual([c.key for c in alone], [c.key for c in near_only])

    def test_uncomputable_case_is_not_reported_as_gap(self) -> None:
        """算不出上下文的日期不计入 expected：平台当初也产不出该信号。"""
        far = _target("far", 5)
        cases = _expected([far], _BoundaryCalendar(), "2026-06-01", "2026-06-04")
        self.assertEqual(cases, ())


if __name__ == "__main__":
    unittest.main()
