"""前端“仅实盘”口径的静态回归约束。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"


def _source() -> str:
    return SHELL_JS.read_text(encoding="utf-8")


def _section(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


def test_live_statistics_uses_confirmed_target_date_window() -> None:
    source = _source()
    assert 'var FACTOR_LAB_LIVE_TARGET_START_DATE = "2026-06-01";' in source

    predicate = _section(
        source,
        "function isLiveStatisticsRow(row)",
        "function factorDetailRowsForMonth",
    )
    assert 'row._source === "live"' in predicate
    assert "DASHBOARD_LIVE_PHASES.indexOf(row.predictionPhase) !== -1" in predicate
    assert "row.targetDate >= FACTOR_LAB_LIVE_TARGET_START_DATE" in predicate
    assert "row.targetDate <= displayUntil" in predicate


def test_live_month_range_comes_from_target_dates_not_existing_rows() -> None:
    source = _source()
    decoder = _section(
        source,
        "function decodeDashboardPayload(payload)",
        "function dashboardDetailRow",
    )
    assert 'var displayUntil = requireDashboardIsoDate(payload.display_until' in decoder
    assert "displayUntil: displayUntil" in decoder

    available_months = _section(
        source,
        "function getFactorAvailableMonths()",
        "function factorMonthRange",
    )
    assert 'if (src === "live")' in available_months
    assert "FACTOR_LAB_LIVE_TARGET_START_DATE.slice(0, 7)" in available_months
    assert "committed.displayUntil.slice(0, 7)" in available_months


def test_live_metrics_and_detail_share_the_same_filter() -> None:
    source = _source()
    detail_rows = _section(
        source,
        "function factorDetailRowsForMonth",
        "function getVisibleRawDailyRowsForScheme",
    )
    assert "isLiveStatisticsRow(row)" in detail_rows
    assert "row.targetDate.slice(0, 7) === month" in detail_rows

    visible_rows = _section(
        source,
        "function getVisibleRowsForScheme",
        "function isLiveStatisticsRow",
    )
    assert "getVisibleRawDailyRowsForScheme(scheme)" in visible_rows
    assert "row.targetDate.slice(0, 7)" in visible_rows

