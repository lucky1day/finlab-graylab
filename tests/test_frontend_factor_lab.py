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
        const result = (async function () {{
        {script}
        }})();
        Promise.resolve(result)
          .then((value) => process.stdout.write(JSON.stringify(value)))
          .catch((error) => {{
            console.error(error && error.stack ? error.stack : error);
            process.exit(1);
          }});
        """
    )
    completed = subprocess.run(
        ["node", "-e", node_script],
        check=False,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "node factor lab hook failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
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


class FactorLabCompareRemovedTests(unittest.TestCase):
    def test_compare_matrix_hook_is_not_exposed(self) -> None:
        result = _run_factor_lab_hook(
            """
            return {
              hasCompareHook: Object.prototype.hasOwnProperty.call(hooks, "buildLocalCompareMatrix")
            };
            """
        )

        self.assertFalse(result["hasCompareHook"])


class FactorLabLifecycleRemovedTests(unittest.TestCase):
    def test_lifecycle_card_hook_is_not_exposed(self) -> None:
        result = _run_factor_lab_hook(
            """
            return {
              hasLifecycleHook: Object.prototype.hasOwnProperty.call(hooks, "normalizeLifecycleCards")
            };
            """
        )

        self.assertFalse(result["hasLifecycleHook"])


class FactorLabRealtimeDataTests(unittest.TestCase):
    def test_live_metrics_take_priority_and_advance_latest_month(self) -> None:
        result = _run_factor_lab_hook(
            """
            const calls = [];
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "t1_daily",
                    name: "T+1 Live",
                    status: "active",
                    horizon: 1,
                    frequency: "daily",
                    tenors: ["5Y"]
                  }
                ]
              },
              "/api/metrics/t1_daily?tenor=5Y": {
                scheme_id: "t1_daily",
                tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  {
                    run_id: "run-20260609",
                    scheme_version: "live-v1",
                    target_tenor: "5Y",
                    horizon: 1,
                    predict_date: "2026-06-09",
                    target_date: "2026-06-10",
                    predicted_direction: 1,
                    actual_direction: null,
                    is_correct: null,
                    confidence: 0.73
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                schemes: [
                  {
                    id: "backtest-demo",
                    scheme_id: "backtest_demo",
                    name: "Backtest Demo",
                    tenor: "5Y",
                    target_label: "5Y国债活跃",
                    horizon: 1,
                    frequency: "daily",
                    status: "complete",
                    monthly_metrics: [
                      { month: "2026-05", samples: 1, correct: 1, accuracy: 100 }
                    ],
                    daily_rows: []
                  }
                ]
              }
            };
            window.fetch = function (url) {
              calls.push(url);
              const payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            const state = hooks.getFactorLabState();
            const scheme = hooks.getSelectedScheme();
            return {
              calls,
              dataMode: state.dataMode,
              selectedTaskKey: state.selectedTaskKey,
              startMonth: state.startMonth,
              endMonth: state.endMonth,
              selectedSchemeName: scheme && scheme.name,
              months: scheme ? scheme.monthlyRows.map((row) => row.month) : [],
              juneDailyCount: scheme && scheme.dailyRowsByMonth["2026-06"]
                ? scheme.dailyRowsByMonth["2026-06"].length
                : 0
            };
            """
        )

        self.assertEqual(result["dataMode"], "live")
        self.assertEqual(result["selectedTaskKey"], "5Y|daily|T+1")
        self.assertEqual(result["selectedSchemeName"], "T+1 Live")
        self.assertEqual(result["endMonth"], "2026-06")
        self.assertIn("2026-06", result["months"])
        self.assertEqual(result["juneDailyCount"], 1)
        self.assertNotIn("/api/backtests/factor-lab", result["calls"])

    def test_backtest_data_is_fallback_when_live_has_no_schemes(self) -> None:
        result = _run_factor_lab_hook(
            """
            const calls = [];
            const responses = {
              "/api/schemes": { target_labels: { "5Y": "5Y国债活跃" }, schemes: [] },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    id: "backtest-demo",
                    scheme_id: "backtest_demo",
                    name: "Backtest Demo",
                    tenor: "5Y",
                    target_label: "5Y国债活跃",
                    horizon: 1,
                    frequency: "daily",
                    status: "complete",
                    monthly_metrics: [
                      { month: "2026-05", samples: 1, correct: 1, accuracy: 100 }
                    ],
                    daily_rows: []
                  }
                ]
              }
            };
            window.fetch = function (url) {
              calls.push(url);
              const payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            const state = hooks.getFactorLabState();
            const scheme = hooks.getSelectedScheme();
            return {
              calls,
              dataMode: state.dataMode,
              selectedTaskKey: state.selectedTaskKey,
              selectedSchemeName: scheme && scheme.name,
              months: scheme ? scheme.monthlyRows.map((row) => row.month) : []
            };
            """
        )

        self.assertEqual(result["dataMode"], "backtest")
        self.assertEqual(result["selectedTaskKey"], "5Y|daily|T+1")
        self.assertEqual(result["selectedSchemeName"], "Backtest Demo")
        self.assertEqual(result["months"], ["2026-05"])
        self.assertEqual(result["calls"], ["/api/schemes", "/api/backtests/factor-lab"])

    def test_live_metric_error_does_not_fall_back_to_stale_backtest(self) -> None:
        result = _run_factor_lab_hook(
            """
            const calls = [];
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "t1_daily",
                    name: "T+1 Live",
                    status: "active",
                    horizon: 1,
                    frequency: "daily",
                    tenors: ["5Y"]
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                schemes: [
                  {
                    id: "backtest-demo",
                    scheme_id: "backtest_demo",
                    name: "Backtest Demo",
                    tenor: "5Y",
                    target_label: "5Y国债活跃",
                    horizon: 1,
                    frequency: "daily",
                    status: "complete",
                    monthly_metrics: [
                      { month: "2026-05", samples: 1, correct: 1, accuracy: 100 }
                    ],
                    daily_rows: []
                  }
                ]
              }
            };
            window.fetch = function (url) {
              calls.push(url);
              const payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 500,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            const state = hooks.getFactorLabState();
            const scheme = hooks.getSelectedScheme();
            return {
              calls,
              dataMode: state.dataMode,
              selectedSchemeName: scheme && scheme.name
            };
            """
        )

        self.assertEqual(result["dataMode"], "live-error")
        self.assertIsNone(result["selectedSchemeName"])
        self.assertNotIn("/api/backtests/factor-lab", result["calls"])


if __name__ == "__main__":
    unittest.main()
