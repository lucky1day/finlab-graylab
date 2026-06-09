from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SCRIPT = PROJECT_ROOT / "frontend" / "aifin-shell.js"


def _run_factor_lab_hook(script: str) -> dict:
    """在 Node VM 中加载前端 IIFE，并返回测试钩子的执行结果。"""
    node_script = textwrap.dedent(
        f"""
        const fs = require("fs");
        const vm = require("vm");

        function makeElement(id) {{
          return {{
            id,
            dataset: {{}},
            style: {{}},
            innerHTML: "",
            textContent: "",
            value: "",
            disabled: false,
            classList: {{
              toggle: function () {{}},
              add: function () {{}},
              remove: function () {{}}
            }},
            setAttribute: function () {{}},
            getAttribute: function () {{ return ""; }},
            appendChild: function () {{}},
            remove: function () {{}},
            addEventListener: function () {{}},
            focus: function () {{}},
            querySelector: function () {{ return null; }},
            querySelectorAll: function () {{ return []; }},
            getBoundingClientRect: function () {{
              return {{ left: 0, right: 0, top: 0, width: 0, height: 0 }};
            }}
          }};
        }}

        const elements = new Map();
        const document = {{
          documentElement: {{ clientWidth: 1200, clientHeight: 800 }},
          getElementById: function (id) {{
            if (!elements.has(id)) elements.set(id, makeElement(id));
            return elements.get(id);
          }},
          querySelector: function () {{ return null; }},
          querySelectorAll: function () {{ return []; }},
          createElement: makeElement
        }};
        const window = {{
          document,
          location: {{ origin: "http://localhost", pathname: "/factor-lab" }},
          history: {{ pushState: function () {{}} }},
          addEventListener: function () {{}},
          setInterval: function () {{ return 1; }},
          setTimeout: function (callback) {{ if (typeof callback === "function") callback(); }},
          requestAnimationFrame: function (callback) {{ callback(); }},
          innerWidth: 1200,
          innerHeight: 800,
          fetch: null,
          matchMedia: null
        }};
        const context = {{
          window,
          document,
          console,
          setInterval: window.setInterval,
          setTimeout: window.setTimeout
        }};
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(fs.readFileSync({json.dumps(str(FRONTEND_SCRIPT))}, "utf8"), context);
        const hooks = context.window.__factorLabTestHooks;
        if (!hooks) throw new Error("missing factor lab test hooks");
        const result = (function () {{
        {script}
        }})();
        process.stdout.write(JSON.stringify(result));
        """
    )
    completed = subprocess.run(
        ["node", "-e", node_script],
        check=True,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    return json.loads(completed.stdout)


class FactorLabRankingTests(unittest.TestCase):
    def test_sort_ranking_schemes_supports_metric_and_direction(self) -> None:
        result = _run_factor_lab_hook(
            """
            const schemes = [
              { id: "a", monthlyRows: [{ month: "2025-01", samples: 10, correct: 6, overall: 60, upPrecision: 50, downPrecision: 70 }] },
              { id: "b", monthlyRows: [{ month: "2025-01", samples: 40, correct: 28, overall: 70, upPrecision: 75, downPrecision: 60 }] },
              { id: "c", monthlyRows: [{ month: "2025-01", samples: 30, correct: 18, overall: 60, upPrecision: 90, downPrecision: 30 }] }
            ];
            return {
              overallDesc: hooks.sortRankingSchemes(schemes, "overall", "desc").map((item) => item.id),
              upAsc: hooks.sortRankingSchemes(schemes, "upPrecision", "asc").map((item) => item.id),
              samplesAsc: hooks.sortRankingSchemes(schemes, "samples", "asc").map((item) => item.id)
            };
            """
        )

        self.assertEqual(result["overallDesc"], ["b", "a", "c"])
        self.assertEqual(result["upAsc"], ["a", "b", "c"])
        self.assertEqual(result["samplesAsc"], ["a", "c", "b"])

    def test_low_sample_badge_uses_30_sample_threshold(self) -> None:
        result = _run_factor_lab_hook(
            """
            return {
              low: hooks.isLowSampleMetric({ samples: 29 }),
              boundary: hooks.isLowSampleMetric({ samples: 30 }),
              empty: hooks.isLowSampleMetric({ samples: 0 })
            };
            """
        )

        self.assertTrue(result["low"])
        self.assertFalse(result["boundary"])
        self.assertFalse(result["empty"])


class FactorLabCompareTests(unittest.TestCase):
    def test_build_compare_matrix_groups_tasks_by_frequency(self) -> None:
        result = _run_factor_lab_hook(
            """
            const tasks = {
              "5Y|daily|T+5": [
                { id: "alpha", name: "Alpha", monthlyRows: [{ month: "2025-01", samples: 10, correct: 7, overall: 70, upPrecision: 80, downPrecision: 50 }] }
              ],
              "10Y|daily|T+5": [
                { id: "alpha", name: "Alpha", monthlyRows: [{ month: "2025-01", samples: 10, correct: 6, overall: 60, upPrecision: 55, downPrecision: 65 }] },
                { id: "beta", name: "Beta", monthlyRows: [{ month: "2025-01", samples: 8, correct: 4, overall: 50, upPrecision: 40, downPrecision: 60 }] }
              ],
              "5Y|weekly|NEXT_MONDAY": [
                { id: "weekly", name: "Weekly", monthlyRows: [{ month: "2025-01", samples: 4, correct: 4, overall: 100, upPrecision: 100, downPrecision: null }] }
              ]
            };
            return hooks.buildLocalCompareMatrix(tasks, "daily", "overall");
            """
        )

        self.assertEqual(result["frequency"], "daily")
        self.assertEqual(result["metric"], "overall")
        self.assertEqual(result["tenors"], ["5Y", "10Y"])
        self.assertEqual([item["scheme_id"] for item in result["schemes"]], ["alpha", "beta"])
        alpha = result["schemes"][0]
        self.assertEqual(alpha["cells"]["5Y"]["value"], 70)
        self.assertEqual(alpha["cells"]["10Y"]["value"], 60)
        self.assertEqual(alpha["cells"]["5Y"]["className"], "metric-good")
        self.assertEqual(result["schemes"][1]["cells"]["10Y"]["className"], "metric-bad")


class FactorLabCalibrationTests(unittest.TestCase):
    def test_build_calibration_buckets_skips_empty_and_pending_rows(self) -> None:
        result = _run_factor_lab_hook(
            """
            return hooks.buildCalibrationBuckets([
              { confidence: 0.05, correct: true },
              { confidence: 0.15, correct: false },
              { confidence: 0.65, correct: true },
              { confidence: 0.72, correct: true },
              { confidence: null, correct: true },
              { confidence: 0.88, correct: null }
            ], 5);
            """
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["bucket"], "0.0-0.2")
        self.assertEqual(result[0]["samples"], 2)
        self.assertEqual(result[0]["hitRate"], 50.0)
        self.assertEqual(result[0]["avgConfidence"], 10.0)
        self.assertEqual(result[1]["bucket"], "0.6-0.8")
        self.assertEqual(result[1]["samples"], 2)
        self.assertEqual(result[1]["hitRate"], 100.0)


if __name__ == "__main__":
    unittest.main()
