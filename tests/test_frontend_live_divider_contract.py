from __future__ import annotations

import pytest

from tests.frontend_test_support import call_frontend_hook


def _live_divider_text(scheme: dict[str, object]) -> str:
    return str(call_frontend_hook("liveDividerTextForTest", scheme))


@pytest.mark.parametrize(
    ("scheme", "expected"),
    [
        (
            {
                "deploymentDate": "2026/07/30",
                "dailyRowsByMonth": {
                    "2026-07": [
                        {
                            "_source": "live",
                            "predictDate": "2026-07-30",
                            "targetDate": "2026-08-05",
                            "predictionPhase": "scheduled_live",
                        }
                    ],
                    "2026-08": [
                        {
                            "_source": "live",
                            "predictDate": "2026-08-01",
                            "targetDate": "2026-08-07",
                            "predictionPhase": "gray_live",
                        }
                    ],
                },
            },
            "实盘预测目标区间：2026-08-07开始",
        ),
        (
            {
                "deploymentDate": "2026/08/06",
                "dailyRowsByMonth": {
                    "2026-08": [
                        {
                            "_source": "live",
                            "predictDate": "2026-08-08",
                            "targetDate": "2026-08-14",
                            "predictionPhase": "scheduled_live",
                        }
                    ]
                },
            },
            "实盘预测目标区间：2026-08-14开始",
        ),
        (
            {
                "deploymentDate": "2026/07/27",
                "dailyRowsByMonth": {
                    "2026-07": [
                        {
                            "_source": "live",
                            "predictDate": "2026-07-15",
                            "targetDate": "2026-08-15",
                            "predictionPhase": "gray_live",
                        }
                    ]
                },
            },
            "实盘预测目标区间：待产生",
        ),
    ],
    ids=("gray-live-after-deployment", "scheduled-live-after-deployment", "no-post-deployment-live-row"),
)
def test_live_divider_uses_first_post_deployment_live_target(
    scheme: dict[str, object], expected: str
) -> None:
    assert _live_divider_text(scheme) == expected
