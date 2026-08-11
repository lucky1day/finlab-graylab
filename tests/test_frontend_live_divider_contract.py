from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


_NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const [shellPath, serializedScheme] = process.argv.slice(1);
const document = {
  visibilityState: "visible",
  getElementById() { return null; },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  addEventListener() {}
};
const window = {
  location: { pathname: "/", origin: "http://localhost" },
  history: { pushState() {} },
  addEventListener() {}
};
const context = vm.createContext({ document, window, console, Promise, Map, Set });

vm.runInContext(fs.readFileSync(shellPath, "utf8"), context, { filename: shellPath });
process.stdout.write(
  window.__factorLabTestHooks.liveDividerTextForTest(JSON.parse(serializedScheme))
);
"""


def _live_divider_text(scheme: dict[str, object]) -> str:
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "node",
            "-e",
            _NODE_HARNESS,
            str(project_root / "frontend" / "aifin-shell.js"),
            json.dumps(scheme),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "Node divider harness failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result.stdout


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
