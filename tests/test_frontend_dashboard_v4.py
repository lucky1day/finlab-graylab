"""Dashboard V4 前端首屏与按需明细合同回归。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHELL_JS = PROJECT_ROOT / "frontend" / "aifin-shell.js"
INDEX_HTML = PROJECT_ROOT / "frontend" / "index.html"


def test_frontend_accepts_only_v4_summary_and_requires_owner() -> None:
    source = SHELL_JS.read_text(encoding="utf-8")

    assert 'DASHBOARD_SCHEMA_VERSION = "factor-lab-dashboard-v4"' in source
    assert "factor-lab-dashboard-v3" not in source
    assert 'payload.representation !== "summary"' in source
    assert '"owner",' in source
    assert "requireDashboardOwner(scheme.owner" in source
    assert '["--", "unknown", "待定"]' in source
    assert "live_rows" not in source
    assert "dailyRowsByMonth" not in source


def test_frontend_renders_owner_column_without_placeholder() -> None:
    shell = SHELL_JS.read_text(encoding="utf-8")
    index = INDEX_HTML.read_text(encoding="utf-8")

    deployment = index.index("<th>部署时间</th>")
    owner = index.index("<th>来源</th>")
    remark = index.index("<th>备注</th>")
    assert deployment < owner < remark
    assert 'requireDashboardOwner(scheme.owner, "ranking scheme owner")' in shell


def test_summary_refresh_invalidates_detail_cache_and_stale_requests() -> None:
    source = SHELL_JS.read_text(encoding="utf-8")

    assert "detailCache: new Map()" in source
    assert 'abort("summary-refreshed")' in source
    assert "detailSeq !== factorLabRuntimeState.detailSeq" in source
    assert "summarySeq !== factorLabRuntimeState.loadSeq" in source
    assert "data-factor-calendar-retry" in source
