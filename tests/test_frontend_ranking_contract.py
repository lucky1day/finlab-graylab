from __future__ import annotations

from pathlib import Path


def test_ranking_only_displays_selected_range_sample_count() -> None:
    project_root = Path(__file__).resolve().parents[1]
    javascript = (project_root / "frontend" / "aifin-shell.js").read_text(
        encoding="utf-8"
    )
    stylesheet = (project_root / "frontend" / "aifin-shell.css").read_text(
        encoding="utf-8"
    )

    assert 'class="factor-sample-count"' in javascript
    for retired_rule in (
        "lowSampleThresholdForTask",
        "isLowSampleMetric",
        "factor-sample-badge",
        "样本不足",
    ):
        assert retired_rule not in javascript
        assert retired_rule not in stylesheet
