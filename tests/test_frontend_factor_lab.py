from __future__ import annotations

import json
import subprocess
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SCRIPT = PROJECT_ROOT / "frontend" / "aifin-shell.js"
FRONTEND_INDEX = PROJECT_ROOT / "frontend" / "index.html"
FRONTEND_CSS = PROJECT_ROOT / "frontend" / "aifin-shell.css"


def _css_rule(selector: str) -> str:
    """读取指定 CSS selector 的声明块，供静态布局契约测试使用。"""
    css = FRONTEND_CSS.read_text(encoding="utf-8")
    marker = selector + " {"
    start = css.index(marker) + len(marker)
    end = css.index("\n}", start)
    return css[start:end]


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
    def test_topbar_status_label_displays_online(self) -> None:
        html = FRONTEND_INDEX.read_text(encoding="utf-8")

        self.assertIn("<span>OnLine</span>", html)
        self.assertNotIn("<span>LOCAL</span>", html)

    def test_hero_summary_layout_allows_long_scheme_names_without_squeezing_title(self) -> None:
        hero_rule = _css_rule(".factor-lab-hero")
        summary_card_rule = _css_rule(".factor-lab-summary div")
        summary_value_rule = _css_rule(".factor-lab-summary strong")
        heading_rule = _css_rule(".factor-lab-hero h2")

        self.assertIn("grid-template-columns: minmax(260px, 0.8fr) minmax(0, 1.6fr);", hero_rule)
        self.assertIn("min-width: 0;", summary_card_rule)
        self.assertIn("white-space: normal;", summary_value_rule)
        self.assertIn("overflow-wrap: anywhere;", summary_value_rule)
        self.assertNotIn("white-space: nowrap;", summary_value_rule)
        self.assertIn("word-break: keep-all;", heading_rule)

    def test_api_urls_and_routes_use_public_base_path_when_served_under_prefix(self) -> None:
        result = _run_factor_lab_hook(
            """
            window.location.pathname = "/bond-factor-lab/";
            const calls = [];
            const responses = {
              "/bond-factor-lab/api/schemes": { target_labels: { "5Y": "5Y国债活跃" }, schemes: [] },
              "/bond-factor-lab/api/backtests/factor-lab": { target_labels: { "5Y": "5Y国债活跃" }, schemes: [] }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              calls.push(url);
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            return {
              apiHealthUrl: hooks.apiUrlForTest("/api/health"),
              normalizedRoot: hooks.normalizeRouteForTest("/bond-factor-lab/"),
              normalizedFactorLab: hooks.normalizeRouteForTest("/bond-factor-lab/factor-lab"),
              publicRoute: hooks.routeUrlForTest("/"),
              calls
            };
            """
        )

        self.assertEqual(result["apiHealthUrl"], "/bond-factor-lab/api/health")
        self.assertEqual(result["normalizedRoot"], "/")
        self.assertEqual(result["normalizedFactorLab"], "/factor-lab")
        self.assertEqual(result["publicRoute"], "/bond-factor-lab/")
        self.assertEqual(
            result["calls"],
            ["/bond-factor-lab/api/schemes", "/bond-factor-lab/api/backtests/factor-lab"],
        )

    def test_task_matrix_default_targets_include_1y_active_treasury(self) -> None:
        result = _run_factor_lab_hook(
            """
            hooks.renderTaskOverviewForTest();
            return {
              counts: hooks.getTaskSchemeCountsForTest(),
              matrixHtml: document.getElementById("factorTaskMatrixBody").innerHTML
            };
            """
        )

        self.assertIn("1Y|T+1", result["counts"])
        self.assertIn("1Y|weekly_average", result["counts"])
        self.assertIn("1Y国债活跃", result["matrixHtml"])
        self.assertLess(
            result["matrixHtml"].index("1Y国债活跃"),
            result["matrixHtml"].index("3Y国债活跃"),
        )

    def test_initial_render_does_not_show_mock_candidates_before_api_returns(self) -> None:
        result = _run_factor_lab_hook(
            """
            return {
              dataMode: hooks.getFactorLabState().dataMode,
              rankingHtml: document.getElementById("factorRankingBody").innerHTML,
              metaText: document.getElementById("factorRankingMeta").textContent,
              selectedScheme: hooks.getSelectedScheme()
            };
            """
        )

        self.assertEqual(result["dataMode"], "loading")
        self.assertNotIn("mock", result["rankingHtml"])
        self.assertNotIn("F-v", result["rankingHtml"])
        self.assertIsNone(result["selectedScheme"])

    def test_candidate_ranking_uses_deployment_date_and_remark_columns(self) -> None:
        html = FRONTEND_INDEX.read_text(encoding="utf-8")
        self.assertIn("<th>部署时间</th>", html)
        self.assertIn("<th>备注</th>", html)
        self.assertNotIn("<th>最近运行</th>", html)
        self.assertNotIn("<th>状态</th>", html)

        result = _run_factor_lab_hook(
            """
            const metric = {
              overall: 80,
              correct: 8,
              samples: 10,
              metricSamples: 10,
              upPrecision: 75,
              downPrecision: 70
            };
            let missingDeploymentError = "";
            try {
              hooks.renderSchemeRankingRowForTest(
                {
                  id: "scheme-a",
                  name: "测试方案",
                  latestRun: "06-10",
                  status: "active",
                  remark: ""
                },
                0,
                metric
              );
            } catch (error) {
              missingDeploymentError = String(error && error.message ? error.message : error);
            }
            const weeklyRowHtml = hooks.renderSchemeRankingRowForTest(
              {
                id: "weekly-a",
                scheme_id: "weekly_5y_direct_0529",
                name: "0529周度5Y独立规则投票 · 5Y国债活跃",
                deploymentDate: "2026/06/01"
              },
              0,
              metric
            );
            const weekly7YRowHtml = hooks.renderSchemeRankingRowForTest(
              {
                id: "weekly_7y_cross_d_overlay_0529",
                schemeId: "weekly_7y_cross_d_overlay_0529",
                name: "0529周度7Y Cross-D叠加 · 7Y国债活跃",
                deploymentDate: "2026/06/01"
              },
              0,
              metric
            );
            const weekly10YRowHtml = hooks.renderSchemeRankingRowForTest(
              {
                id: "weekly_10y_d_overlay_0529",
                schemeId: "weekly_10y_d_overlay_0529",
                name: "0529周度10Y D-overlay · 10Y国债活跃",
                deploymentDate: "2026/06/01"
              },
              0,
              metric
            );
            return {
              missingDeploymentError,
              weeklyRowHtml,
              weekly7YRowHtml,
              weekly10YRowHtml,
              deploymentDate: hooks.getSchemeDeploymentDate({}),
              weekly7YDeploymentDateFromSchemeId: hooks.getSchemeDeploymentDate({
                schemeId: "weekly_7y_cross_d_overlay_0529",
                deploymentDate: "2026/06/01"
              }),
              weekly10YDeploymentDateFromSchemeId: hooks.getSchemeDeploymentDate({
                schemeId: "weekly_10y_d_overlay_0529",
                deploymentDate: "2026/06/01"
              }),
              customDeploymentDate: hooks.getSchemeDeploymentDate({ deployed_at: "2026-06-02" }),
              remark: hooks.getSchemeRemark({ remark: "人工备注" })
            };
            """
        )

        self.assertIn("missing deployed_at", result["missingDeploymentError"])
        self.assertEqual(result["deploymentDate"], "")
        self.assertEqual(result["weekly7YDeploymentDateFromSchemeId"], "2026/06/01")
        self.assertEqual(result["weekly10YDeploymentDateFromSchemeId"], "2026/06/01")
        self.assertEqual(result["customDeploymentDate"], "2026/06/02")
        self.assertEqual(result["remark"], "人工备注")
        self.assertIn("2026/06/01", result["weeklyRowHtml"])
        self.assertIn("2026/06/01", result["weekly7YRowHtml"])
        self.assertIn("2026/06/01", result["weekly10YRowHtml"])
        self.assertNotIn("factor-status-pill", result["weeklyRowHtml"])
        self.assertNotIn("active", result["weeklyRowHtml"])

    def test_sort_ranking_schemes_supports_metric_and_direction(self) -> None:
        result = _run_factor_lab_hook(
            """
            function makeRows(upTp, upFp, downTp, downFp) {
              const rows = [];
              for (let i = 0; i < upTp; i++) rows.push({ predictedDirection: 1, actualDirection: 1 });
              for (let i = 0; i < upFp; i++) rows.push({ predictedDirection: 1, actualDirection: -1 });
              for (let i = 0; i < downTp; i++) rows.push({ predictedDirection: -1, actualDirection: -1 });
              for (let i = 0; i < downFp; i++) rows.push({ predictedDirection: -1, actualDirection: 1 });
              return rows;
            }
            const schemes = [
              { id: "a", monthlyRows: [], dailyRowsByMonth: { "2025-01": makeRows(1, 1, 5, 3) } },
              { id: "b", monthlyRows: [], dailyRowsByMonth: { "2025-01": makeRows(3, 1, 25, 11) } },
              { id: "c", monthlyRows: [], dailyRowsByMonth: { "2025-01": makeRows(9, 1, 9, 11) } }
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

    def test_flat_predictions_count_as_samples_but_not_metric_denominator(self) -> None:
        result = _run_factor_lab_hook(
            """
            const rows = [
              { predictedDirection: 1, actualDirection: 1 },
              { predictedDirection: -1, actualDirection: -1 },
              { predictedDirection: 1, actualDirection: 1 },
              { predictedDirection: 1, actualDirection: -1 },
              { predictedDirection: -1, actualDirection: 1 },
              { predictedDirection: 1, actualDirection: -1 },
              { predictedDirection: -1, actualDirection: 1 },
              { predictedDirection: 0, actualDirection: 0 }
            ];
            const scheme = {
              id: "flat-demo",
              name: "平信号示例",
              deploymentDate: "2026/06/04",
              dailyRowsByMonth: { "2025-05": rows },
              monthlyRows: []
            };
            const metric = hooks.aggregateScheme(scheme);
            const rowHtml = hooks.renderSchemeRankingRowForTest(scheme, 0, metric);
            var monthlyOnlyError = "";
            try {
              hooks.aggregateScheme({
                id: "flat-monthly-demo",
                monthlyRows: [{
                  month: "2025-05",
                  samples: 8,
                  metricSamples: 7,
                  correct: 3,
                  actualCounts: { up: 4, down: 3, flat: 1 },
                  predictedCounts: { up: 4, down: 3, flat: 1 },
                  metricActualCounts: { up: 4, down: 3, flat: 0 },
                  metricPredictedCounts: { up: 4, down: 3, flat: 0 },
                  upPrecision: 50,
                  upRecall: 50,
                  downPrecision: 33.3333333333,
                  downRecall: 33.3333333333
                }],
                dailyRowsByMonth: {}
              });
            } catch (error) {
              monthlyOnlyError = String(error && error.message || error);
            }
            return {
              metric,
              monthlyOnlyError,
              rowHtml
            };
            """
        )

        self.assertEqual(result["metric"]["samples"], 8)
        self.assertEqual(result["metric"]["metricSamples"], 7)
        self.assertEqual(result["metric"]["correct"], 3)
        self.assertAlmostEqual(result["metric"]["overall"], 3 / 7 * 100)
        self.assertIn("detail rows", result["monthlyOnlyError"])
        self.assertIn("3/7", result["rowHtml"])
        self.assertNotIn("3/8", result["rowHtml"])

    def test_monthly_metric_without_metric_denominator_sources_fails_closed(self) -> None:
        result = _run_factor_lab_hook(
            """
            try {
              hooks.aggregateScheme({
                id: "bad-monthly-row",
                monthlyRows: [{
                  month: "2025-05",
                  samples: 8,
                  correct: 3,
                  overall: 37.5,
                  metricActualCounts: { up: 0, down: 0, flat: 0 },
                  metricPredictedCounts: { up: 0, down: 0, flat: 0 }
                }],
                dailyRowsByMonth: {}
              });
              return { ok: true, message: "" };
            } catch (error) {
              return { ok: false, message: String(error && error.message || error) };
            }
            """
        )

        self.assertFalse(result["ok"])
        self.assertIn("detail rows", result["message"])

    def test_monthly_metric_without_metric_actual_dist_fails_closed(self) -> None:
        result = _run_factor_lab_hook(
            """
            try {
              hooks.aggregateScheme({
                id: "bad-monthly-metric-actual-dist",
                monthlyRows: [{
                  month: "2025-05",
                  samples: 8,
                  metricSamples: 7,
                  correct: 3,
                  overall: 42.9,
                  actualDist: { up: 4, down: 3, flat: 1 },
                  predictedDist: { up: 4, down: 3, flat: 1 },
                  metricPredictedCounts: { up: 4, down: 3, flat: 0 }
                }],
                dailyRowsByMonth: {}
              });
              return { ok: true, message: "" };
            } catch (error) {
              return { ok: false, message: String(error && error.message || error) };
            }
            """
        )

        self.assertFalse(result["ok"])
        self.assertIn("detail rows", result["message"])

    def test_api_monthly_metric_without_detail_rows_fails_closed(self) -> None:
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": { target_labels: { "5Y": "5Y国债活跃" }, schemes: [] },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [{
                  id: "bad-api-monthly-metric-predicted-dist",
                  scheme_id: "demo__h5__5Y",
                  base_scheme_id: "demo",
                  target_tenor: "5Y",
                  target_label: "5Y国债活跃",
                  horizon: 5,
                  task_type: "T+5",
                  frequency: "daily",
                  deployed_at: "2026-06-04",
                  monthly_metrics: [{
                    month: "2025-05",
                    samples: 8,
                    metric_samples: 7,
                    correct: 3,
                    accuracy: 42.9,
                    actual_dist: { up: 4, down: 3, flat: 1 },
                    predicted_dist: { up: 4, down: 3, flat: 1 },
                    metric_actual_dist: { up: 4, down: 3, flat: 0 }
                  }],
                  daily_rows: []
                }]
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            return {
              dataMode: hooks.getFactorLabState().dataMode,
              selectedScheme: hooks.getSelectedScheme()
            };
            """
        )

        self.assertEqual(result["dataMode"], "live-error")
        self.assertIsNone(result["selectedScheme"])

    def test_api_monthly_metrics_are_ignored_when_detail_rows_exist(self) -> None:
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": { target_labels: { "5Y": "5Y国债活跃" }, schemes: [] },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [{
                  id: "detail-derived-monthly-metric",
                  scheme_id: "demo__h5__5Y",
                  base_scheme_id: "demo",
                  name: "Detail Derived",
                  target_tenor: "5Y",
                  target_label: "5Y国债活跃",
                  horizon: 5,
                  task_type: "T+5",
                  frequency: "daily",
                  deployed_at: "2026-06-04",
                  monthly_metrics: [{
                    month: "2026-05",
                    samples: 99,
                    metric_samples: 99,
                    correct: 99,
                    accuracy: 100,
                    overall: 100,
                    up_precision: 100,
                    up_recall: 100,
                    down_precision: 100,
                    down_recall: 100,
                    actual_dist: { up: 99, down: 0, flat: 0 },
                    predicted_dist: { up: 99, down: 0, flat: 0 },
                    metric_actual_dist: { up: 99, down: 0, flat: 0 },
                    metric_predicted_dist: { up: 99, down: 0, flat: 0 }
                  }],
                  daily_rows: [
                    { predict_date: "2026-05-01", feature_date: "2026-05-01", target_date: "2026-05-08",
                      predicted_direction: 1, actual_direction: 1, is_correct: true },
                    { predict_date: "2026-05-02", feature_date: "2026-05-02", target_date: "2026-05-09",
                      predicted_direction: -1, actual_direction: 1, is_correct: false },
                    { predict_date: "2026-05-03", feature_date: "2026-05-03", target_date: "2026-05-10",
                      predicted_direction: 0, actual_direction: -1, is_correct: false }
                  ]
                }]
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var scheme = hooks.getSelectedScheme();
            var aggregate = hooks.aggregateScheme(scheme);
            return {
              dataMode: hooks.getFactorLabState().dataMode,
              monthlyRows: scheme.monthlyRows,
              aggregate
            };
            """
        )

        self.assertEqual(result["dataMode"], "backtest")
        self.assertEqual(len(result["monthlyRows"]), 1)
        self.assertEqual(result["monthlyRows"][0]["samples"], 3)
        self.assertEqual(result["monthlyRows"][0]["metricSamples"], 2)
        self.assertEqual(result["monthlyRows"][0]["correct"], 1)
        self.assertAlmostEqual(result["monthlyRows"][0]["overall"], 50)
        self.assertEqual(result["aggregate"]["samples"], 3)
        self.assertEqual(result["aggregate"]["metricSamples"], 2)
        self.assertEqual(result["aggregate"]["correct"], 1)
        self.assertAlmostEqual(result["aggregate"]["overall"], 50)

    def test_aggregate_scheme_filters_detail_rows_by_target_month(self) -> None:
        result = _run_factor_lab_hook(
            """
            hooks.setFactorLabStateForTest({
              startMonth: "2026-05",
              endMonth: "2026-05",
              dataSource: "all"
            });
            const scheme = {
              id: "month-filter-demo",
              monthlyRows: [],
              dailyRowsByMonth: {
                "2026-05": [
                  { predictedDirection: 1, actualDirection: 1 },
                  { predictedDirection: -1, actualDirection: 1 }
                ],
                "2026-06": [
                  { predictedDirection: 1, actualDirection: -1 },
                  { predictedDirection: 1, actualDirection: -1 }
                ]
              }
            };
            return hooks.aggregateScheme(scheme);
            """
        )

        self.assertEqual(result["samples"], 2)
        self.assertEqual(result["metricSamples"], 2)
        self.assertEqual(result["correct"], 1)
        self.assertAlmostEqual(result["overall"], 50)

    def test_aggregate_scheme_filters_detail_rows_by_source(self) -> None:
        result = _run_factor_lab_hook(
            """
            const scheme = {
              id: "source-filter-demo",
              monthlyRows: [],
              dailyRowsByMonth: {
                "2026-05": [
                  { _source: "backtest", predictedDirection: 1, actualDirection: 1 },
                  { _source: "live", predictedDirection: 1, actualDirection: -1 }
                ]
              }
            };
            hooks.setFactorLabStateForTest({
              startMonth: "2026-05",
              endMonth: "2026-05",
              dataSource: "backtest"
            });
            const backtestMetric = hooks.aggregateScheme(scheme);
            hooks.setFactorLabStateForTest({ dataSource: "live" });
            const liveMetric = hooks.aggregateScheme(scheme);
            hooks.setFactorLabStateForTest({ dataSource: "all" });
            const allMetric = hooks.aggregateScheme(scheme);
            return { backtestMetric, liveMetric, allMetric };
            """
        )

        self.assertEqual(result["backtestMetric"]["samples"], 1)
        self.assertEqual(result["backtestMetric"]["correct"], 1)
        self.assertEqual(result["liveMetric"]["samples"], 1)
        self.assertEqual(result["liveMetric"]["correct"], 0)
        self.assertEqual(result["allMetric"]["samples"], 2)
        self.assertEqual(result["allMetric"]["correct"], 1)

    def test_ranking_row_without_metric_samples_fails_closed(self) -> None:
        result = _run_factor_lab_hook(
            """
            try {
              hooks.renderSchemeRankingRowForTest(
                { id: "bad-render-row", name: "bad-render-row" },
                0,
                { samples: 8, correct: 3, overall: 37.5, upPrecision: 50, downPrecision: 25 }
              );
              return { ok: true, message: "" };
            } catch (error) {
              return { ok: false, message: String(error && error.message || error) };
            }
            """
        )

        self.assertFalse(result["ok"])
        self.assertIn("metricSamples", result["message"])

    def test_flat_prediction_daily_result_displays_dash(self) -> None:
        result = _run_factor_lab_hook(
            """
            return {
              flatCorrect: hooks.renderDailyResultForTest({
                predicted: "平",
                predictedDirection: 0,
                actual: "平",
                actualDirection: 0,
                correct: true
              }),
              flatWrong: hooks.renderDailyResultForTest({
                predicted: "平",
                predictedDirection: 0,
                actual: "涨",
                actualDirection: 1,
                correct: false
              }),
              downWrong: hooks.renderDailyResultForTest({
                predicted: "跌",
                predictedDirection: -1,
                actual: "涨",
                actualDirection: 1,
                correct: false
              }),
              pending: hooks.renderDailyResultForTest({
                predicted: "涨",
                predictedDirection: 1,
                actual: "--",
                actualDirection: null,
                correct: null
              })
            };
            """
        )

        self.assertIn(">-<", result["flatCorrect"])
        self.assertNotIn("✓", result["flatCorrect"])
        self.assertNotIn("×", result["flatCorrect"])
        self.assertIn(">-<", result["flatWrong"])
        self.assertNotIn("✓", result["flatWrong"])
        self.assertNotIn("×", result["flatWrong"])
        self.assertIn("×", result["downWrong"])
        self.assertIn("?", result["pending"])

    def test_low_sample_badge_uses_task_frequency_threshold(self) -> None:
        result = _run_factor_lab_hook(
            """
            return {
              dailyLow: hooks.isLowSampleMetric({ samples: 29 }, { taskKey: "10Y|T+1" }),
              dailyBoundary: hooks.isLowSampleMetric({ samples: 30 }, { taskKey: "10Y|T+1" }),
              weeklyLow: hooks.isLowSampleMetric({ samples: 2 }, { taskKey: "10Y|weekly_point" }),
              weeklyBoundary: hooks.isLowSampleMetric({ samples: 3 }, { taskKey: "10Y|weekly_point" }),
              weeklyTwoSamples: hooks.isLowSampleMetric({ samples: 2 }, { taskKey: "10Y|weekly_point" }),
              empty: hooks.isLowSampleMetric({ samples: 0 }, { taskKey: "10Y|weekly_point" })
            };
            """
        )

        self.assertTrue(result["dailyLow"])
        self.assertFalse(result["dailyBoundary"])
        self.assertTrue(result["weeklyLow"])
        self.assertFalse(result["weeklyBoundary"])
        self.assertTrue(result["weeklyTwoSamples"])
        self.assertFalse(result["empty"])


class FactorLabTrendChartTests(unittest.TestCase):
    def test_trend_chart_layout_expands_and_thins_axis_for_long_ranges(self) -> None:
        result = _run_factor_lab_hook(
            """
            const layout = hooks.trendChartLayoutForTest(124, 960);
            return {
              width: layout.width,
              labelStep: layout.labelStep,
              isScrollable: layout.isScrollable,
              firstVisible: hooks.trendMonthLabelVisibleForTest(0, 124, layout.labelStep),
              secondVisible: hooks.trendMonthLabelVisibleForTest(1, 124, layout.labelStep),
              lastVisible: hooks.trendMonthLabelVisibleForTest(123, 124, layout.labelStep)
            };
            """
        )

        self.assertGreater(result["width"], 960)
        self.assertGreater(result["labelStep"], 1)
        self.assertTrue(result["isScrollable"])
        self.assertTrue(result["firstVisible"])
        self.assertFalse(result["secondVisible"])
        self.assertTrue(result["lastVisible"])


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
    def test_live_and_backtest_merge_when_both_present(self) -> None:
        """实时和回测都有数据时，合并展示，无数据丢失。"""
        result = _run_factor_lab_hook(
            """
            const calls = [];
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "t1_daily__h1__5Y",
                    base_scheme_id: "t1_daily",
                    target_tenor: "5Y",
                    name: "T+1 实盘",
                    status: "active",
                    horizon: 1,
                    task_type: "T+1",
                    frequency: "daily",
                    deployed_at: "2026-06-04"
                  }
                ]
              },
              "/api/metrics/t1_daily__h1__5Y": {
                scheme_id: "t1_daily__h1__5Y",
                base_scheme_id: "t1_daily",
                target_tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  {
                    run_id: "r1",
                    scheme_version: "v1",
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
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    id: "bt:t1_daily:fw:5Y",
                    scheme_id: "t1_daily__h1__5Y",
                    base_scheme_id: "t1_daily",
                    scheme_name: "t1_daily",
                    name: "T+1 回测基准",
                    target_tenor: "5Y",
                    target_label: "5Y国债活跃",
                    horizon: 1,
                    task_type: "T+1",
                    frequency: "daily",
                    status: "complete",
                    deployed_at: "2026-06-04",
                    benchmark_label: "model_muti_0529",
                    data_source_label: "framework_db_aligned",
	                    monthly_metrics: [
	                      { month: "2026-05", samples: 30, metric_samples: 26, correct: 20, accuracy: 76.9, overall: 76.9,
	                        up_precision: 70, up_recall: 65, down_precision: 60, down_recall: 55,
	                        actual_dist: { up: 15, down: 10, flat: 5 },
	                        predicted_dist: { up: 14, down: 12, flat: 4 },
	                        metric_actual_dist: { up: 15, down: 10, flat: 1 },
	                        metric_predicted_dist: { up: 14, down: 12, flat: 0 } }
	                    ],
                    daily_rows: [
                      { predict_date: "2026-05-20", feature_date: "2026-05-20", target_date: "2026-05-21",
                        predicted_direction: 1, actual_direction: 1, is_correct: true }
                    ]
                  }
                ]
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              calls.push(url);
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var state = hooks.getFactorLabState();
            var scheme = hooks.getSelectedScheme();
            return {
              calls: calls,
              dataMode: state.dataMode,
              selectedTaskKey: state.selectedTaskKey,
              endMonth: state.endMonth,
              selectedSchemeName: scheme && scheme.name,
              hasLiveSince: !!(scheme && scheme.liveSinceDate),
              liveSinceDate: scheme && scheme.liveSinceDate,
              months: scheme ? scheme.monthlyRows.map(function (r) { return r.month; }) : [],
              backtestEndMonth: scheme ? scheme.backtestEndMonth : ""
            };
            """
        )

        # 两者都有 → 合并模式
        self.assertEqual(result["dataMode"], "merged")
        self.assertTrue(result["hasLiveSince"])
        self.assertEqual(result["liveSinceDate"], "2026-06-09")
        # 5月和6月都应存在（回测5月 + 实盘6月）
        self.assertIn("2026-05", result["months"])
        self.assertIn("2026-06", result["months"])
        # 两者接口都调了
        self.assertIn("/api/schemes", result["calls"])
        self.assertIn("/api/backtests/factor-lab", result["calls"])

    def test_daily_v28_backtest_and_live_start_are_visible_together(self) -> None:
        """V28 新 benchmark 的回测月度行应与实盘发出起点一起展示。"""
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "daily_5y_2_v28__h5__5Y",
                    base_scheme_id: "daily_5y_2_v28",
                    target_tenor: "5Y",
                    name: "V28日频5Y方案2",
                    status: "active",
                    horizon: 5,
                    task_type: "T+5",
                    frequency: "daily",
                    deployed_at: "2026-06-04"
                  }
                ]
	              },
	              "/api/metrics/daily_5y_2_v28__h5__5Y": {
	                scheme_id: "daily_5y_2_v28__h5__5Y",
	                base_scheme_id: "daily_5y_2_v28",
	                target_tenor: "5Y",
	                target_label: "5Y国债活跃",
	                phase_ranges: [
	                  { prediction_phase: "gray_live", start_predict_date: "2026-05-26", end_predict_date: "2026-06-11",
	                    start_target_date: "2026-06-01", end_target_date: "2026-06-17", rows: 13 },
	                  { prediction_phase: "scheduled_live", start_predict_date: "2026-06-12", end_predict_date: "2026-06-12",
	                    start_target_date: "2026-06-18", end_target_date: "2026-06-18", rows: 1 }
	                ],
	                monthly_metrics: [
                  { month: "2026-06", samples: 8, metric_samples: 5, correct: 4, accuracy: 80.0, overall: 80.0,
                    up_precision: 75, up_recall: 75, down_precision: 100, down_recall: 25,
                    actual_dist: { up: 4, down: 4, flat: 0 },
                    predicted_dist: { up: 4, down: 1, flat: 3 },
                    metric_actual_dist: { up: 4, down: 1, flat: 0 },
                    metric_predicted_dist: { up: 4, down: 1, flat: 0 } }
                ],
                daily_rows: [
                  {
                    target_tenor: "5Y",
                    horizon: 5,
	                    predict_date: "2026-05-26",
	                    feature_date: "2026-05-25",
	                    target_date: "2026-06-01",
	                    prediction_phase: "gray_live",
                    predicted_direction: -1,
                    actual_direction: -1,
                    is_correct: true,
                    confidence: 1.0
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    id: "v28_daily_5y_2:daily_5y_2_v28__h5__5Y:framework_db_aligned",
                    run_id: 92,
                    benchmark_id: "v28_daily_5y_2",
                    scheme_id: "daily_5y_2_v28__h5__5Y",
                    base_scheme_id: "daily_5y_2_v28",
                    scheme_name: "V28日频5Y方案2",
                    name: "V28日频5Y方案2 · 5Y国债活跃",
                    target_tenor: "5Y",
                    target_label: "5Y国债活跃",
                    horizon: 5,
                    task_type: "T+5",
                    frequency: "daily",
                    status: "complete",
                    deployed_at: "2026-06-04",
                    benchmark_label: "v28_daily_5y_2",
                    data_source_label: "framework_db_aligned",
                    monthly_metrics: [
	                      { month: "2026-04", samples: 21, metric_samples: 20, correct: 11, accuracy: 55.0, overall: 55.0,
	                        up_precision: 0, up_recall: 0, down_precision: 61.1, down_recall: 78.6,
	                        actual_dist: { up: 5, down: 14, flat: 2 },
	                        predicted_dist: { up: 2, down: 18, flat: 1 },
	                        metric_actual_dist: { up: 5, down: 14, flat: 1 },
	                        metric_predicted_dist: { up: 2, down: 18, flat: 0 } }
	                    ],
                    daily_rows: [
                      { predict_date: "2026-04-16", feature_date: "2026-04-16", target_date: "2026-04-23",
                        predicted_direction: -1, actual_direction: -1, is_correct: true }
                    ]
                  }
                ]
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var scheme = hooks.getSelectedScheme();
            return {
              dataMode: hooks.getFactorLabState().dataMode,
	              selectedTaskKey: hooks.getFactorLabState().selectedTaskKey,
	              liveSinceDate: scheme && scheme.liveSinceDate,
	              phaseRanges: scheme && scheme.phaseRanges,
	              months: scheme ? scheme.monthlyRows.map(function (r) { return r.month + ":" + r._source; }) : [],
	              detailMeta: document.getElementById("factorDetailMeta").textContent
            };
            """
        )

        self.assertEqual(result["dataMode"], "merged")
        self.assertEqual(result["selectedTaskKey"], "5Y|T+5")
        self.assertEqual(result["liveSinceDate"], "2026-05-26")
        self.assertEqual(result["phaseRanges"][0]["prediction_phase"], "gray_live")
        self.assertEqual(result["months"], ["2026-04:backtest", "2026-06:live"])
        self.assertIn("实盘发出起点 2026-05-26", result["detailMeta"])
        self.assertIn("灰度实盘 2026-05-26 至 2026-06-11", result["detailMeta"])
        self.assertIn("正式调度起点 2026-06-12", result["detailMeta"])

    def test_same_month_backtest_and_live_split_into_two_rows(self) -> None:
        """同月既有回测又有实盘时，应展示两行（回测行 + 实盘行），不覆盖。"""
        result = _run_factor_lab_hook(
            """
            const calls = [];
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "t1_daily__h1__5Y",
                    base_scheme_id: "t1_daily",
                    target_tenor: "5Y",
                    name: "T+1 实盘",
                    status: "active",
                    horizon: 1,
                    task_type: "T+1",
                    frequency: "daily",
                    deployed_at: "2026-06-04"
                  }
                ]
              },
              "/api/metrics/t1_daily__h1__5Y": {
                scheme_id: "t1_daily__h1__5Y",
                base_scheme_id: "t1_daily",
                target_tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [
                  { month: "2026-05", samples: 5, metric_samples: 5, correct: 4, accuracy: 80.0, overall: 80.0,
                    up_precision: 100, up_recall: 80, down_precision: 0, down_recall: 0,
                    actual_dist: { up: 4, down: 1, flat: 0 },
                    predicted_dist: { up: 4, down: 1, flat: 0 },
                    metric_actual_dist: { up: 4, down: 1, flat: 0 },
                    metric_predicted_dist: { up: 4, down: 1, flat: 0 } }
                ],
                daily_rows: [
                  { predict_date: "2026-05-27", target_date: "2026-05-28", target_tenor: "5Y", horizon: 1,
                    predicted_direction: 1, actual_direction: 1, is_correct: true, confidence: 0.7 }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    id: "bt:t1_daily:fw:5Y",
                    scheme_id: "t1_daily__h1__5Y",
                    base_scheme_id: "t1_daily",
                    scheme_name: "t1_daily",
                    name: "T+1 回测基准",
                    target_tenor: "5Y",
                    target_label: "5Y国债活跃",
                    horizon: 1,
                    task_type: "T+1",
                    frequency: "daily",
                    status: "complete",
                    deployed_at: "2026-06-04",
                    benchmark_label: "model_muti_0529",
                    data_source_label: "framework_db_aligned",
                    monthly_metrics: [
                      { month: "2026-05", samples: 20, metric_samples: 18, correct: 12, accuracy: 66.7, overall: 66.7,
                        up_precision: 55, up_recall: 50, down_precision: 50, down_recall: 45,
                        actual_dist: { up: 10, down: 8, flat: 2 },
                        predicted_dist: { up: 9, down: 9, flat: 2 },
                        metric_actual_dist: { up: 10, down: 8, flat: 0 },
                        metric_predicted_dist: { up: 9, down: 9, flat: 0 } }
                    ],
                    daily_rows: [
                      { predict_date: "2026-05-01", feature_date: "2026-05-01", target_date: "2026-05-02",
                        predicted_direction: 1, actual_direction: 1, is_correct: true },
                      { predict_date: "2026-05-02", feature_date: "2026-05-02", target_date: "2026-05-05",
                        predicted_direction: -1, actual_direction: -1, is_correct: true },
                      { predict_date: "2026-05-03", feature_date: "2026-05-03", target_date: "2026-05-06",
                        predicted_direction: 1, actual_direction: -1, is_correct: false }
                    ]
                  }
                ]
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              calls.push(url);
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var scheme = hooks.getSelectedScheme();
            var rows = scheme ? scheme.monthlyRows : [];
            return {
              rowCount: rows.length,
              sources: rows.map(function (r) { return r._source; }),
              backtestAccuracy: rows.filter(function (r) { return r._source === "backtest"; })
                .map(function (r) { return r.overall; }),
              liveAccuracy: rows.filter(function (r) { return r._source === "live"; })
                .map(function (r) { return r.overall; }),
            };
            """
        )

        # 同月应有两行：backtest + live
        self.assertEqual(result["rowCount"], 2)
        self.assertEqual(result["sources"], ["backtest", "live"])
        # 月度指标只按明细重算，不读取 monthly_metrics 中的旧汇总数值。
        self.assertAlmostEqual(result["backtestAccuracy"][0], 2 / 3 * 100)
        self.assertEqual(result["liveAccuracy"], [100])

    def test_weekly_live_uses_single_predict_date_start_semantics(self) -> None:
        """周度和日度统一用第一条 predict_date 作为实盘发出起点。"""
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "weekly_5y_direct_0529__h6__5Y",
                    base_scheme_id: "weekly_5y_direct_0529",
                    target_tenor: "5Y",
                    name: "周度实盘",
                    status: "active",
                    horizon: 6,
                    task_type: "weekly_point",
                    frequency: "weekly",
                    deployed_at: "2026-06-04",
                    last_run: { date: "2026-06-11", status: "success" }
                  }
                ]
              },
              "/api/metrics/weekly_5y_direct_0529__h6__5Y": {
                scheme_id: "weekly_5y_direct_0529__h6__5Y",
                base_scheme_id: "weekly_5y_direct_0529",
                target_tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [
                  { month: "2026-06", samples: 1, metric_samples: 1, correct: 1, accuracy: 100, overall: 100,
                    up_precision: 100, up_recall: 100, down_precision: null, down_recall: null,
                    actual_dist: { up: 1, down: 0, flat: 0 },
                    predicted_dist: { up: 1, down: 0, flat: 0 },
                    metric_actual_dist: { up: 1, down: 0, flat: 0 },
                    metric_predicted_dist: { up: 1, down: 0, flat: 0 } }
                ],
                daily_rows: [
                  {
                    target_tenor: "5Y",
                    horizon: 6,
                    predict_date: "2026-06-11",
                    feature_date: "2026-05-29",
                    target_date: "2026-06-05",
                    predicted_direction: 1,
                    actual_direction: 1,
                    is_correct: true,
                    confidence: 0.51
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: []
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var scheme = hooks.getSelectedScheme();
            return {
              dataMode: hooks.getFactorLabState().dataMode,
              liveSinceDate: scheme && scheme.liveSinceDate,
              liveMetricSinceDate: scheme && scheme.liveMetricSinceDate,
              dividerText: hooks.liveDividerTextForTest(scheme, { frequency: "weekly" }),
              months: scheme ? scheme.monthlyRows.map(function (r) { return r.month; }) : [],
              dailyMonths: scheme ? Object.keys(scheme.dailyRowsByMonth).sort() : []
            };
            """
        )

        self.assertEqual(result["dataMode"], "live")
        self.assertEqual(result["liveSinceDate"], "2026-06-11")
        self.assertEqual(result["liveMetricSinceDate"], "2026-06-11")
        self.assertEqual(result["dividerText"], "实盘发出起点 2026-06-11")
        self.assertEqual(result["months"], ["2026-06"])
        self.assertEqual(result["dailyMonths"], ["2026-06"])

    def test_live_registry_rows_use_target_tenor_and_composite_metrics_url(self) -> None:
        """新版 registry row 只有 target_tenor；前端不得再拼 ?tenor=。"""
        result = _run_factor_lab_hook(
            """
            const requests = [];
            const responses = {
              "/api/schemes": [
                {
                  scheme_id: "t5_daily__h5__3Y",
                  base_scheme_id: "t5_daily",
                  target_tenor: "3Y",
                  name: "T5 3Y",
                  status: "active",
                  horizon: 5,
                  task_type: "T+5",
                  frequency: "daily",
                  deployed_at: "2026-06-04",
                  last_run: { date: "2026-06-12", status: "success" }
                },
                {
                  scheme_id: "t5_daily__h5__5Y",
                  base_scheme_id: "t5_daily",
                  target_tenor: "5Y",
                  name: "T5 5Y",
                  status: "active",
                  horizon: 5,
                  task_type: "T+5",
                  frequency: "daily",
                  deployed_at: "2026-06-04",
                  last_run: { date: "2026-06-12", status: "success" }
                }
              ],
              "/api/metrics/t5_daily__h5__3Y": {
                scheme_id: "t5_daily__h5__3Y",
                base_scheme_id: "t5_daily",
                target_tenor: "3Y",
                target_label: "3Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  { predict_date: "2026-06-12", target_date: "2026-06-18",
                    predicted_direction: 1, actual_direction: null, is_correct: null }
                ]
              },
              "/api/metrics/t5_daily__h5__5Y": {
                scheme_id: "t5_daily__h5__5Y",
                base_scheme_id: "t5_daily",
                target_tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  { predict_date: "2026-06-12", target_date: "2026-06-18",
                    predicted_direction: -1, actual_direction: null, is_correct: null }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "3Y": "3Y国债活跃", "5Y": "5Y国债活跃" },
                schemes: []
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              requests.push(url);
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var scheme = hooks.getSelectedScheme();
            return {
              dataMode: hooks.getFactorLabState().dataMode,
              requests,
              selectedSchemeId: scheme && scheme.schemeId,
              deploymentDate: scheme && scheme.deploymentDate
            };
            """
        )

        self.assertEqual(result["dataMode"], "live")
        self.assertIn("/api/metrics/t5_daily__h5__3Y", result["requests"])
        self.assertIn("/api/metrics/t5_daily__h5__5Y", result["requests"])
        self.assertFalse(any("?tenor=" in url for url in result["requests"]))
        self.assertIn(result["selectedSchemeId"], {"t5_daily__h5__3Y", "t5_daily__h5__5Y"})
        self.assertEqual(result["deploymentDate"], "2026/06/04")

    def test_live_registry_row_missing_deployed_at_fails_closed(self) -> None:
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": [
                {
                  scheme_id: "t5_daily__h5__5Y",
                  base_scheme_id: "t5_daily",
                  target_tenor: "5Y",
                  name: "T5 5Y",
                  status: "active",
                  horizon: 5,
                  task_type: "T+5",
                  frequency: "daily"
                }
              ],
              "/api/metrics/t5_daily__h5__5Y": {
                scheme_id: "t5_daily__h5__5Y",
                base_scheme_id: "t5_daily",
                target_tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  { predict_date: "2026-06-12", target_date: "2026-06-18",
                    predicted_direction: 1, actual_direction: null, is_correct: null }
                ]
              },
              "/api/backtests/factor-lab": { target_labels: { "5Y": "5Y国债活跃" }, schemes: [] }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            var loaded = await hooks.loadFactorLabData({ force: true });
            return {
              loaded,
              dataMode: hooks.getFactorLabState().dataMode,
              apiError: hooks.getFactorLabState().apiError
            };
            """
        )

        self.assertFalse(result["loaded"])
        self.assertEqual(result["dataMode"], "live-error")
        self.assertIn("missing deployed_at", result["apiError"])

    def test_pending_actual_is_not_rendered_as_flat(self) -> None:
        """actual_direction 为 null 时应显示待验证，而不是被 JS Number(null) 变成平。"""
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "t5_daily__h5__5Y",
                    base_scheme_id: "t5_daily",
                    target_tenor: "5Y",
                    name: "T5 实盘",
                    status: "active",
                    horizon: 5,
                    task_type: "T+5",
                    frequency: "daily",
                    deployed_at: "2026-06-04",
                    last_run: { date: "2026-06-10", status: "success" }
                  }
                ]
              },
              "/api/metrics/t5_daily__h5__5Y": {
                scheme_id: "t5_daily__h5__5Y",
                base_scheme_id: "t5_daily",
                target_tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  {
                    target_tenor: "5Y",
                    horizon: 5,
                    predict_date: "2026-06-10",
                    target_date: "2026-06-16",
                    predicted_direction: 1,
                    actual_direction: null,
                    is_correct: null,
                    confidence: 0.41
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: []
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var scheme = hooks.getSelectedScheme();
            var row = scheme.dailyRowsByMonth["2026-06"][0];
            return {
              actual: row.actual,
              actualDirection: row.actualDirection,
              correct: row.correct,
              aggregate: hooks.aggregateScheme(scheme)
            };
            """
        )

        self.assertEqual(result["actual"], "待验证")
        self.assertIsNone(result["actualDirection"])
        self.assertIsNone(result["correct"])
        self.assertEqual(result["aggregate"]["samples"], 0)

    def test_daily_horizon_detail_uses_target_date_as_display(self) -> None:
        """日频 T+N 明细按交易日(target_date)展示和分组，与 predict_date 解耦。"""
        html = FRONTEND_INDEX.read_text(encoding="utf-8")
        self.assertIn("<th>预测日</th>", html)
        self.assertIn("<th id=\"factorDailyDateHeader\">目标日</th>", html)

        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "t5_daily__h5__5Y",
                    base_scheme_id: "t5_daily",
                    target_tenor: "5Y",
                    name: "T5 实盘",
                    status: "active",
                    horizon: 5,
                    task_type: "T+5",
                    frequency: "daily",
                    deployed_at: "2026-06-04",
                    last_run: { date: "2026-05-29", status: "success" }
                  }
                ]
              },
              "/api/metrics/t5_daily__h5__5Y": {
                scheme_id: "t5_daily__h5__5Y",
                base_scheme_id: "t5_daily",
                target_tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  {
                    target_tenor: "5Y",
                    horizon: 5,
                    predict_date: "2026-05-29",
                    target_date: "2026-06-05",
                    predicted_direction: 1,
                    actual_direction: 1,
                    is_correct: true,
                    confidence: 0.62
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: []
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var scheme = hooks.getSelectedScheme();
            hooks.renderFactorDailyRowsForTest("2026-06");
            var row = scheme.dailyRowsByMonth["2026-06"][0];
            return {
              header: document.getElementById("factorDailyDateHeader").textContent,
              dailyHtml: document.getElementById("factorDailyTableBody").innerHTML,
              months: Object.keys(scheme.dailyRowsByMonth).sort(),
              day: row.day,
              predictDate: row.predictDate,
              targetDate: row.targetDate,
              liveSinceDate: scheme.liveSinceDate,
              liveMetricSinceDate: scheme.liveMetricSinceDate
            };
            """
        )

        # 明细按 target_date 的月份(6月)分组
        self.assertEqual(result["months"], ["2026-06"])
        self.assertEqual(result["header"], "目标日")
        self.assertIn("05/29", result["dailyHtml"])
        self.assertIn("06/05", result["dailyHtml"])
        # day 取自 target_date
        self.assertEqual(result["day"], "06/05")
        self.assertEqual(result["predictDate"], "2026-05-29")
        self.assertEqual(result["targetDate"], "2026-06-05")
        self.assertEqual(result["liveSinceDate"], "2026-05-29")
        self.assertEqual(result["liveMetricSinceDate"], "2026-05-29")

    def test_weekly_point_detail_header_uses_target_date_display(self) -> None:
        """周度单点方案按 target_date 展示下周最后一个交易日。"""
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": {
                target_labels: { "10Y": "10Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "weekly_10y_d_overlay_0529__h6__10Y",
                    base_scheme_id: "weekly_10y_d_overlay_0529",
                    target_tenor: "10Y",
                    name: "0529周度10Y D-overlay",
                    status: "active",
                    horizon: 6,
                    task_type: "weekly_point",
                    frequency: "weekly",
                    deployed_at: "2026-06-01",
                    last_run: { date: "2026-06-13", status: "success" }
                  }
                ]
              },
              "/api/metrics/weekly_10y_d_overlay_0529__h6__10Y": {
                scheme_id: "weekly_10y_d_overlay_0529__h6__10Y",
                base_scheme_id: "weekly_10y_d_overlay_0529",
                target_tenor: "10Y",
                target_label: "10Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  {
                    target_tenor: "10Y",
                    horizon: 6,
                    predict_date: "2026-06-13",
                    feature_date: "2026-06-12",
                    target_date: "2026-06-18",
                    predicted_direction: -1,
                    actual_direction: -1,
                    is_correct: true,
                    confidence: 0.32
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "10Y": "10Y国债活跃" },
                schemes: []
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            hooks.setFactorLabStateForTest({
              selectedTaskKey: "10Y|weekly_point",
              selectedSchemeId: "weekly_10y_d_overlay_0529__h6__10Y",
              dataSource: "all",
              startMonth: "2026-06",
              endMonth: "2026-06"
            });
            hooks.renderFactorDailyRowsForTest("2026-06");
            var row = hooks.getSelectedScheme().dailyRowsByMonth["2026-06"][0];
            return {
              title: document.getElementById("factorCalendarTitle").textContent,
              header: document.getElementById("factorDailyDateHeader").textContent,
              note: document.getElementById("factorCalendarNote").textContent,
              dailyHtml: document.getElementById("factorDailyTableBody").innerHTML,
              day: row.day,
              predictDate: row.predictDate,
              targetDate: row.targetDate
            };
            """
        )

        self.assertEqual(result["title"], "2026-06 周度验证表")
        self.assertEqual(result["header"], "目标日")
        self.assertIn("目标日为下周最后一个交易日", result["note"])
        self.assertIn("06/13", result["dailyHtml"])
        self.assertIn("06/18", result["dailyHtml"])
        self.assertEqual(result["day"], "06/18")
        self.assertEqual(result["predictDate"], "2026-06-13")
        self.assertEqual(result["targetDate"], "2026-06-18")

    def test_weekly_average_detail_header_uses_target_week_display(self) -> None:
        """周平均方案仍按目标周展示，避免与周度单点混淆。"""
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": {
                target_labels: { "10Y": "10Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "weekly_avg__h6__10Y",
                    base_scheme_id: "weekly_avg",
                    target_tenor: "10Y",
                    name: "周平均方案",
                    status: "active",
                    horizon: 6,
                    task_type: "weekly_average",
                    frequency: "weekly",
                    deployed_at: "2026-06-01",
                    last_run: { date: "2026-06-13", status: "success" }
                  }
                ]
              },
              "/api/metrics/weekly_avg__h6__10Y": {
                scheme_id: "weekly_avg__h6__10Y",
                base_scheme_id: "weekly_avg",
                target_tenor: "10Y",
                target_label: "10Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  {
                    target_tenor: "10Y",
                    horizon: 6,
                    predict_date: "2026-06-13",
                    feature_date: "2026-06-12",
                    target_date: "2026-06-18",
                    predicted_direction: -1,
                    actual_direction: -1,
                    is_correct: true,
                    confidence: 0.32
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "10Y": "10Y国债活跃" },
                schemes: []
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            hooks.setFactorLabStateForTest({
              selectedTaskKey: "10Y|weekly_average",
              selectedSchemeId: "weekly_avg__h6__10Y",
              dataSource: "all",
              startMonth: "2026-06",
              endMonth: "2026-06"
            });
            hooks.renderFactorDailyRowsForTest("2026-06");
            return {
              header: document.getElementById("factorDailyDateHeader").textContent,
              note: document.getElementById("factorCalendarNote").textContent,
              dailyHtml: document.getElementById("factorDailyTableBody").innerHTML
            };
            """
        )

        self.assertEqual(result["header"], "目标周")
        self.assertIn("目标周按该周最后可验证交易日标记", result["note"])
        self.assertIn("06/13", result["dailyHtml"])
        self.assertIn("06/18", result["dailyHtml"])

    def test_daily_detail_empty_state_spans_prediction_and_target_date_columns(self) -> None:
        """每日明细空状态应覆盖新增的预测日和目标日两列。"""
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "t1_daily__h1__5Y",
                    base_scheme_id: "t1_daily",
                    target_tenor: "5Y",
                    name: "T1 实盘",
                    status: "active",
                    horizon: 1,
                    task_type: "T+1",
                    frequency: "daily",
                    deployed_at: "2026-06-04",
                    last_run: { date: "2026-06-09", status: "success" }
                  }
                ]
              },
              "/api/metrics/t1_daily__h1__5Y": {
                scheme_id: "t1_daily__h1__5Y",
                base_scheme_id: "t1_daily",
                target_tenor: "5Y",
                target_label: "5Y国债活跃",
                monthly_metrics: [],
                daily_rows: [
                  {
                    target_tenor: "5Y",
                    horizon: 1,
                    predict_date: "2026-06-09",
                    target_date: "2026-06-10",
                    predicted_direction: 1,
                    actual_direction: 1,
                    is_correct: true,
                    confidence: 0.62
                  }
                ]
              },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: []
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}) }
              });
            };
            globalThis.fetch = window.fetch;
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            hooks.setFactorLabStateForTest({
              selectedTaskKey: "5Y|T+1",
              selectedSchemeId: "t1_daily__h1__5Y",
              dataSource: "backtest",
              startMonth: "2026-06",
              endMonth: "2026-06"
            });
            hooks.renderFactorDailyRowsForTest("2026-06");
            return {
              html: document.getElementById("factorDailyTableBody").innerHTML
            };
            """
        )

        self.assertIn('colspan="5"', result["html"])
        self.assertIn("当前月份暂无每日明细", result["html"])

    def test_backtest_only_when_live_has_no_schemes(self) -> None:
        """实盘无方案时回退纯回测模式。"""
        result = _run_factor_lab_hook(
            """
            var calls = [];
            var responses = {
              "/api/schemes": { target_labels: { "5Y": "5Y国债活跃" }, schemes: [] },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    id: "bt:x:fw:5Y",
                    scheme_id: "backtest_demo__h1__5Y",
                    base_scheme_id: "backtest_demo",
                    name: "Backtest Demo",
                    target_tenor: "5Y",
                    target_label: "5Y国债活跃",
                    horizon: 1,
                    task_type: "T+1",
                    frequency: "daily",
                    status: "complete",
                    deployed_at: "2026-06-04",
                    monthly_metrics: [
	                      { month: "2026-05", samples: 1, metric_samples: 1, correct: 1, accuracy: 100, overall: 100,
	                        up_precision: 100, up_recall: 100, down_precision: null, down_recall: null,
	                        actual_dist: { up: 1, down: 0, flat: 0 },
	                        predicted_dist: { up: 1, down: 0, flat: 0 },
	                        metric_actual_dist: { up: 1, down: 0, flat: 0 },
	                        metric_predicted_dist: { up: 1, down: 0, flat: 0 } }
	                    ],
                    daily_rows: [
                      { predict_date: "2026-05-01", feature_date: "2026-05-01", target_date: "2026-05-02",
                        predicted_direction: 1, actual_direction: 1, is_correct: true }
                    ]
                  }
                ]
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              calls.push(url);
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var state = hooks.getFactorLabState();
            var scheme = hooks.getSelectedScheme();
            return {
              calls: calls,
              dataMode: state.dataMode,
              selectedSchemeName: scheme && scheme.name,
              hasLiveSince: !!(scheme && scheme.liveSinceDate),
              months: scheme ? scheme.monthlyRows.map(function (r) { return r.month; }) : []
            };
            """
        )

        self.assertEqual(result["dataMode"], "backtest")
        self.assertEqual(result["selectedSchemeName"], "Backtest Demo")
        self.assertFalse(result["hasLiveSince"])  # 纯回测无实盘起点
        self.assertEqual(result["months"], ["2026-05"])
        self.assertIn("/api/backtests/factor-lab", result["calls"])

    def test_backtest_scheme_missing_deployed_at_fails_closed(self) -> None:
        result = _run_factor_lab_hook(
            """
            const responses = {
              "/api/schemes": { target_labels: { "5Y": "5Y国债活跃" }, schemes: [] },
              "/api/backtests/factor-lab": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    id: "bt:x:fw:5Y",
                    scheme_id: "backtest_demo__h1__5Y",
                    base_scheme_id: "backtest_demo",
                    name: "Backtest Demo",
                    target_tenor: "5Y",
                    target_label: "5Y国债活跃",
                    horizon: 1,
                    task_type: "T+1",
                    frequency: "daily",
                    status: "complete",
                    monthly_metrics: [],
                    daily_rows: [
                      { predict_date: "2026-05-01", feature_date: "2026-05-01", target_date: "2026-05-02",
                        predicted_direction: 1, actual_direction: 1, is_correct: true }
                    ]
                  }
                ]
              }
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              var payload = responses[url];
              return Promise.resolve({
                ok: Boolean(payload),
                status: payload ? 200 : 404,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            context.fetch = window.fetch;

            var loaded = await hooks.loadFactorLabData({ force: true });
            return {
              loaded,
              dataMode: hooks.getFactorLabState().dataMode,
              apiError: hooks.getFactorLabState().apiError
            };
            """
        )

        self.assertFalse(result["loaded"])
        self.assertEqual(result["dataMode"], "live-error")
        self.assertIn("missing deployed_at", result["apiError"])

    def test_merge_does_not_lose_live_when_backtest_fails(self) -> None:
        """回测接口500时实盘方案仍然展示，且回测失败不丢数据。"""
        result = _run_factor_lab_hook(
            """
            var calls = [];
            var responses = {
              "/api/schemes": {
                target_labels: { "5Y": "5Y国债活跃" },
                schemes: [
                  {
                    scheme_id: "t1_daily__h1__5Y",
                    base_scheme_id: "t1_daily",
                    target_tenor: "5Y",
                    name: "T+1 实盘",
                    status: "active",
                    horizon: 1,
                    task_type: "T+1",
                    frequency: "daily",
                    deployed_at: "2026-06-04"
                  }
                ]
              },
              "/api/metrics/t1_daily__h1__5Y": {
                scheme_id: "t1_daily__h1__5Y",
                base_scheme_id: "t1_daily",
                target_tenor: "5Y",
                monthly_metrics: [],
                daily_rows: [
                  {
                    predict_date: "2026-06-09",
                    target_date: "2026-06-10",
                    target_tenor: "5Y",
                    horizon: 1,
                    predicted_direction: 1,
                    actual_direction: 1,
                    is_correct: true,
                    confidence: 0.8
                  }
                ]
              },
              "/api/backtests/factor-lab": null  // 回测接口500
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              calls.push(url);
              var payload = responses[url];
              var ok = payload !== undefined;
              return Promise.resolve({
                ok: ok,
                status: payload ? 200 : 500,
                json: function () { return Promise.resolve(payload || {}); }
              });
            };
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var state = hooks.getFactorLabState();
            var scheme = hooks.getSelectedScheme();
            return {
              calls: calls,
              dataMode: state.dataMode,
              selectedSchemeName: scheme && scheme.name,
              hasLiveSince: !!(scheme && scheme.liveSinceDate),
              endMonth: state.endMonth,
              months: scheme ? scheme.monthlyRows.map(function (r) { return r.month; }) : []
            };
            """
        )

        # 实盘存活 → live 模式（不是 merged 因为回测挂了）
        self.assertEqual(result["dataMode"], "live")
        self.assertEqual(result["selectedSchemeName"], "T+1 实盘")
        self.assertTrue(result["hasLiveSince"])
        self.assertEqual(result["endMonth"], "2026-06")
        self.assertIn("2026-06", result["months"])

    def test_both_fail_produces_error(self) -> None:
        """两者都失败时进入错误状态。"""
        result = _run_factor_lab_hook(
            """
            var calls = [];
            var responses = {
              "/api/schemes": null,
              "/api/backtests/factor-lab": null
            };
            window.fetch = function (url) {
              if (url instanceof Request) url = url.url;
              calls.push(url);
              return Promise.resolve({
                ok: false,
                status: 500,
                json: function () { return Promise.resolve({}); }
              });
            };
            context.fetch = window.fetch;

            await hooks.loadFactorLabData({ force: true });
            var state = hooks.getFactorLabState();
            var scheme = hooks.getSelectedScheme();
            return {
              calls: calls,
              dataMode: state.dataMode,
              hasError: !!state.apiError,
              errorMessage: state.apiError || "",
              selectedSchemeName: scheme && scheme.name
            };
            """
        )

        self.assertEqual(result["dataMode"], "live-error")
        self.assertIsNone(result["selectedSchemeName"])


if __name__ == "__main__":
    unittest.main()
