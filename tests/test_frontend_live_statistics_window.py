"""前端“仅实盘”口径的静态回归约束。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"


def _source() -> str:
    return SHELL_JS.read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


def test_live_statistics_uses_summary_monthly_rows() -> None:
    source = _source()
    assert "FACTOR_LAB_LIVE_TARGET_START_DATE" not in source
    visible_rows = _section(
        source,
        "function getVisibleRowsForScheme(scheme)",
        "function aggregateScheme(scheme)",
    )
    assert "scheme.monthlyRows.filter" in visible_rows
    assert "dailyRowsByMonth" not in visible_rows


def test_live_month_range_comes_from_target_dates_not_existing_rows() -> None:
    source = _source()
    decoder = _section(
        source,
        "function decodeDashboardPayload(payload)",
        "function decodeDashboardDetailPayload",
    )
    assert 'var displayUntil = requireDashboardIsoDate(payload.display_until' in decoder
    assert "payload.live_target_start_date" in decoder
    assert "displayUntil: displayUntil" in decoder
    assert "liveTargetStartDate: liveTargetStartDate" in decoder

    available_months = _section(
        source,
        "function getFactorAvailableMonths()",
        "function factorMonthRange",
    )
    assert 'if (src === "live")' in available_months
    assert "dashboardLiveDisplayStartMonth(" in available_months
    assert "task.taskType" in available_months
    assert "committed.displayUntil.slice(0, 7)" in available_months


def test_live_detail_is_fetched_on_demand_from_same_dashboard_path() -> None:
    source = _source()
    detail_loader = _section(
        source,
        "function loadFactorCalendarDetail",
        "function openFactorCalendar",
    )
    assert '"/api/factor-lab/dashboard" + query' in detail_loader
    assert '"&month=" + encodeURIComponent(month)' in detail_loader
    assert '"&source=" + encodeURIComponent(source)' in detail_loader
    assert "cache.has(key)" in detail_loader
