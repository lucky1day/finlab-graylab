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

from dataclasses import replace
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.signal_gap_report import (  # noqa: E402
    SignalGapReport,
    SignalTarget,
    _expected,
    latest_due_signal_statuses,
)

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
        cases, _targets = _expected(
            [near, far], _BoundaryCalendar(), "2026-06-01", "2026-06-04"
        )
        produced = {case.base_scheme_id for case in cases}
        self.assertIn("near", produced)

    def test_computable_cases_are_unchanged(self) -> None:
        """可计算的 case 全部保留，数量与内容不受边界 target 影响。"""
        near = _target("near", 1)
        far = _target("far", 5)
        alone, _ = _expected([near], _BoundaryCalendar(), "2026-06-01", "2026-06-04")
        together, _ = _expected(
            [near, far], _BoundaryCalendar(), "2026-06-01", "2026-06-04"
        )
        near_only = [c for c in together if c.base_scheme_id == "near"]
        self.assertEqual([c.key for c in alone], [c.key for c in near_only])

    def test_broken_target_is_flagged_not_silently_not_due(self) -> None:
        """日历算不出上下文的 target 必须以 failure_category 可见。

        静默跳过会让它在 latest_due_signal_statuses 里变成 not_due，
        与"日历数据缺行"无法区分。
        """
        far = _target("far", 5)
        cases, targets = _expected(
            [far], _BoundaryCalendar(), "2026-06-01", "2026-06-04"
        )
        self.assertEqual(cases, ())
        self.assertEqual(
            [t.failure_category for t in targets],
            ["calendar_context_unavailable"],
        )

    def test_healthy_target_keeps_no_failure_category(self) -> None:
        near = _target("near", 1)
        _cases, targets = _expected(
            [near], _BoundaryCalendar(), "2026-06-01", "2026-06-04"
        )
        self.assertEqual([t.failure_category for t in targets], [None])

    def test_partial_tail_failure_takes_priority_over_stale_present_case(self) -> None:
        """尾端日历失败不得被此前已经存在的信号状态掩盖。"""
        target = _target("partial", 2)
        cases, targets = _expected(
            [target], _BoundaryCalendar(), "2026-06-01", "2026-06-04"
        )
        self.assertGreater(len(cases), 0)
        self.assertEqual(targets[0].failure_category, "calendar_context_unavailable")

        statuses = latest_due_signal_statuses(
            SignalGapReport(
                "2026-06-01",
                "2026-06-04",
                targets,
                cases,
                cases,
                (),
            )
        )

        self.assertEqual(statuses[0].state, "missing")
        self.assertEqual(
            statuses[0].failure_category,
            "calendar_context_unavailable",
        )

    def test_partial_tail_failure_preserves_known_missing_history(self) -> None:
        """尾端失败可见时仍保留此前已确认的未修复缺口证据。"""
        target = _target("partial-missing", 2)
        cases, targets = _expected(
            [target], _BoundaryCalendar(), "2026-06-01", "2026-06-04"
        )
        known_missing = replace(cases[0], failure_category="no_run")

        status = latest_due_signal_statuses(
            SignalGapReport(
                "2026-06-01",
                "2026-06-04",
                targets,
                cases,
                (),
                (known_missing,),
            )
        )[0]

        self.assertEqual(status.state, "missing")
        self.assertEqual(status.failure_category, "calendar_context_unavailable")
        self.assertEqual(status.open_missing_count, 1)
        self.assertEqual(
            status.latest_missing_predict_date,
            known_missing.predict_date,
        )

