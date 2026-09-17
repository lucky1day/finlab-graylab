"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const HTTP_SCRIPT_PATH = path.resolve(__dirname, "../frontend/factor-lab-http.js");
const HTTP_SCRIPT_SOURCE = fs.readFileSync(HTTP_SCRIPT_PATH, "utf8");
const SCRIPT_PATH = path.resolve(__dirname, "../frontend/aifin-shell.js");
const SCRIPT_SOURCE = fs.readFileSync(SCRIPT_PATH, "utf8");

function dashboardPayload(snapshotId) {
  return {
    schema_version: "factor-lab-dashboard-v7",
    representation: "summary",
    snapshot_id: snapshotId,
    generated_at: "2026-09-16T12:00:00+08:00",
    display_until: "2026-09-16",
    live_feature_start_date: "2026-06-01",
    monthly_row_fields: [
      "month", "source", "samples", "metric_samples", "correct",
      "predicted_up", "predicted_down", "predicted_flat", "actual_up",
      "actual_down", "actual_flat", "up_true_positive", "down_true_positive"
    ],
    range_row_fields: [
      "source", "samples", "metric_samples", "correct", "predicted_up", "predicted_down",
      "predicted_flat", "actual_up", "actual_down", "actual_flat", "up_true_positive", "down_true_positive"
    ],
    feature_date_bounds: { all: null, backtest: null, live: null },
    selected_feature_range: null,
    target_labels: {},
    schemes: []
  };
}

function okResponse(payload) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: () => Promise.resolve(payload)
  };
}

function createHarness(fetchImpl, elements = {}) {
  const listeners = new Map();
  const scheduledDelays = [];
  const dispatchedEvents = [];
  const document = {
    visibilityState: "visible",
    addEventListener() {},
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
  const context = {
    AbortController,
    CustomEvent: function CustomEvent(type) { this.type = type; },
    Date,
    Error,
    Map,
    Math,
    Number,
    Object,
    Promise,
    Set,
    String,
    Array,
    RegExp,
    URLSearchParams,
    console,
    document,
    fetch: (...args) => context.__fetchImpl(...args),
    clearTimeout,
    performance: { now: () => Date.now() },
    __fetchImpl: fetchImpl
  };
  context.setTimeout = (fn, delay) => {
    scheduledDelays.push(delay);
    const timer = setTimeout(fn, delay);
    if (delay > 1000 && typeof timer.unref === "function") timer.unref();
    return timer;
  };
  context.window = context;
  context.location = { pathname: "/", origin: "https://example.test" };
  context.history = { pushState() {} };
  context.AbortController = AbortController;
  context.__BFL_ENABLE_TEST_HOOKS__ = true;
  context.addEventListener = (type, listener) => listeners.set(type, listener);
  context.dispatchEvent = (event) => {
    dispatchedEvents.push(event.type);
    return true;
  };
  context.requestAnimationFrame = (callback) => context.setTimeout(callback, 0);
  vm.runInNewContext(HTTP_SCRIPT_SOURCE, context, { filename: HTTP_SCRIPT_PATH });
  vm.runInNewContext(SCRIPT_SOURCE, context, { filename: SCRIPT_PATH });
  return {
    context,
    hooks: context.__BFL_TEST_HOOKS__,
    dispatchedEvents,
    scheduledDelays,
    setFetch(nextFetch) { context.__fetchImpl = nextFetch; }
  };
}

function waitFor(predicate, timeoutMs = 250) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    function check() {
      if (predicate()) {
        resolve();
        return;
      }
      if (Date.now() - started >= timeoutMs) {
        reject(new Error("condition was not met before timeout"));
        return;
      }
      setTimeout(check, 2);
    }
    check();
  });
}

function flushPromises() {
  return new Promise((resolve) => setImmediate(resolve));
}

test("a pending fetch times out, enters error state, and schedules retry", async () => {
  const harness = createHarness(() => new Promise(() => {}));
  harness.hooks.setRequestTimeoutMs(12);
  harness.hooks.setRetryDelaysMs([10000]);

  harness.hooks.start();
  await waitFor(() => harness.hooks.state().dataMode === "error");

  const state = harness.hooks.state();
  assert.equal(state.remoteLoading, false);
  assert.equal(state.consecutiveFailures, 1);
  assert.equal(state.hasRefreshTimer, true);
  assert.match(state.apiError, /timed out/);
  harness.hooks.stop();
});

test("timeout remains active while an HTTP 200 response body is pending", async () => {
  const harness = createHarness(() => Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: () => new Promise(() => {})
  }));

  await assert.rejects(
    harness.hooks.fetchJson("/api/factor-lab/dashboard", { timeoutMs: 12 }),
    (error) => error.name === "FactorLabTimeoutError" && error.code === "request_timeout"
  );
});

test("automatic retry can recover from timeout to a fresh snapshot", async () => {
  let calls = 0;
  const harness = createHarness(() => {
    calls += 1;
    return calls === 1
      ? new Promise(() => {})
      : Promise.resolve(okResponse(dashboardPayload("snapshot-recovered")));
  });
  harness.hooks.setRequestTimeoutMs(10);
  harness.hooks.setRetryDelaysMs([5]);

  harness.hooks.start();
  await waitFor(() => harness.hooks.state().snapshotId === "snapshot-recovered");

  const state = harness.hooks.state();
  assert.equal(calls, 2);
  assert.equal(state.dataMode, "fresh");
  assert.equal(state.remoteLoading, false);
  assert.equal(state.consecutiveFailures, 0);
  harness.hooks.stop();
});

test("logout prevents a late in-flight response from restoring old data or retrying", async () => {
  let resolveFetch;
  const harness = createHarness(() => new Promise((resolve) => {
    resolveFetch = resolve;
  }));
  harness.hooks.setRequestTimeoutMs(100);

  harness.hooks.start();
  await waitFor(() => typeof resolveFetch === "function");
  harness.hooks.stop();
  resolveFetch(okResponse(dashboardPayload("old-identity")));
  await flushPromises();

  const state = harness.hooks.state();
  assert.equal(state.authenticated, false);
  assert.equal(state.snapshotId, null);
  assert.equal(state.dataMode, "loading");
  assert.equal(state.hasRefreshTimer, false);
  assert.deepEqual(harness.dispatchedEvents, []);
});

test("a cancelled old request cannot dispatch auth-required from a late 401", async () => {
  let resolveFetch;
  const harness = createHarness(() => new Promise((resolve) => {
    resolveFetch = resolve;
  }));
  const controller = new AbortController();
  const request = harness.hooks.fetchJson("/api/factor-lab/dashboard", {
    signal: controller.signal,
    timeoutMs: 100
  });

  controller.abort("identity-changed");
  await assert.rejects(request, (error) => error.code === "request_aborted");
  resolveFetch({
    ok: false,
    status: 401,
    headers: { get: () => null },
    json: () => Promise.resolve({ error_code: "not_authenticated" })
  });
  await flushPromises();

  assert.deepEqual(harness.dispatchedEvents, []);
});

test("an old request timeout cannot clear or overwrite a new identity request", async () => {
  let calls = 0;
  let harness;
  harness = createHarness((_url, options) => {
    calls += 1;
    if (calls === 1) {
      options.signal.addEventListener("abort", () => {
        if (options.signal.reason !== "request-timeout") return;
        harness.hooks.stop();
        harness.setFetch(() => Promise.resolve(
          okResponse(dashboardPayload("new-identity"))
        ));
        harness.hooks.start();
      }, { once: true });
      return new Promise(() => {});
    }
    return Promise.resolve(okResponse(dashboardPayload("new-identity")));
  });
  harness.hooks.setRequestTimeoutMs(12);
  harness.hooks.setRetryDelaysMs([5]);

  harness.hooks.start();
  await waitFor(() => harness.hooks.state().snapshotId === "new-identity");
  await flushPromises();

  const state = harness.hooks.state();
  assert.equal(state.dataMode, "fresh");
  assert.equal(state.remoteLoading, false);
  assert.equal(state.consecutiveFailures, 0);
  harness.hooks.stop();
});

test("automatic retry is not scheduled before Retry-After", async () => {
  const harness = createHarness(() => Promise.resolve({
    ok: false,
    status: 429,
    headers: { get: () => "2" },
    json: () => Promise.resolve({ error_code: "rate_limited" })
  }));
  harness.hooks.setRequestTimeoutMs(100);
  harness.hooks.setRetryDelaysMs([5]);

  harness.hooks.start();
  await waitFor(() => harness.hooks.state().dataMode === "error");

  assert.ok(
    harness.scheduledDelays.some((delay) => delay >= 1900),
    `expected a Retry-After delay, got ${harness.scheduledDelays.join(", ")}`
  );
  harness.hooks.stop();
});

test("an unrepresentably long Retry-After fails closed without an early timer", async () => {
  const harness = createHarness(() => Promise.resolve({
    ok: false,
    status: 503,
    headers: { get: () => "999999999" },
    json: () => Promise.resolve(null)
  }));
  harness.hooks.setRequestTimeoutMs(100);
  harness.hooks.setRetryDelaysMs([5]);

  harness.hooks.start();
  await waitFor(() => harness.hooks.state().dataMode === "error");

  assert.equal(harness.hooks.state().hasRefreshTimer, false);
  harness.hooks.stop();
});

function schemePayload(taskType, month = "2026-05", source = "backtest") {
  const payload = dashboardPayload("feature-axis");
  payload.target_labels = { "3Y": "3Y国债活跃" };
  payload.feature_date_bounds.all = { start_date: month + "-01", end_date: month + "-28" };
  payload.feature_date_bounds[source] = payload.feature_date_bounds.all;
  payload.schemes = [{
    scheme_id: "example__h1__3Y",
    base_scheme_id: "example",
    name: "示例方案",
    owner: "测试维护人",
    is_production: false,
    description: "",
    horizon: 1,
    task_type: taskType,
    frequency: taskType === "T+1" ? "daily" : "monthly",
    target_tenor: "3Y",
    target_label: "3Y国债活跃",
    status: "active",
    deployed_at: "2026-06-01",
    monthly_rows: [[month, source, 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0]],
    range_rows: [[source, 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0]],
    backtest: null
  }];
  return payload;
}

function detailPayload(rows, month = "2026-05", source = "all") {
  return {
    schema_version: "factor-lab-dashboard-v7",
    representation: "detail",
    snapshot_id: "feature-detail",
    generated_at: "2026-09-16T12:00:00+08:00",
    display_until: "2026-09-16",
    live_feature_start_date: "2026-06-01",
    scheme_id: "example__h1__3Y",
    month,
    source,
    row_fields: ["source", "predict_date", "feature_date", "target_date",
      "predicted_direction", "actual_direction"],
    rows
  };
}

function detailExpectation(payload) {
  return {
    schemeId: payload.scheme_id,
    month: payload.month,
    source: payload.source,
    liveFeatureStartDate: payload.live_feature_start_date
  };
}

test("all task summaries share natural feature months and the same live boundary", () => {
  const { hooks } = createHarness(() => Promise.resolve());
  for (const task of ["T+1", "T+5", "weekly_point", "weekly_average", "monthly",
    "monthly_average", "quarterly_average", "annual_average"]) {
    for (const [month, source] of [["2026-05", "backtest"], ["2026-06", "live"]]) {
      const payload = schemePayload(task, month, source);
      const decoded = hooks.decodeSummary(payload);
      assert.equal(decoded.schemes[0].monthlyRows[0].month, month);
      assert.equal(decoded.schemes[0].monthlyRows[0]._source, source);
      payload.schemes[0].monthly_rows[0][1] = source === "live" ? "backtest" : "live";
      assert.throws(() => hooks.decodeSummary(payload), /feature_date policy/);
    }
  }
});

test("detail validates feature month, source boundary and feature-first ordering", () => {
  const { hooks } = createHarness(() => Promise.resolve());
  const payload = detailPayload([
    ["backtest", "2026-06-01", "2026-05-28", "2026-06-08", 1, 1],
    ["backtest", "2026-06-01", "2026-05-29", "2026-06-05", -1, null]
  ]);
  const expected = detailExpectation(payload);
  const decoded = hooks.decodeDetail(payload, expected);
  assert.equal(decoded[1].featureDate, "2026-05-29");
  assert.equal(decoded[1].targetDate, "2026-06-05");
  assert.equal(decoded[1].actualDirection, null);
  const invalidSource = structuredClone(payload);
  invalidSource.rows[1][0] = "live";
  assert.throws(() => hooks.decodeDetail(invalidSource, expected), /feature_date policy/);
  const invalidMonth = structuredClone(payload);
  invalidMonth.rows[1][2] = "2026-04-30";
  assert.throws(() => hooks.decodeDetail(invalidMonth, expected), /requested month/);
  const invalidOrder = structuredClone(payload);
  invalidOrder.rows.reverse();
  assert.throws(() => hooks.decodeDetail(invalidOrder, expected), /canonically sorted/);
  const boundary = detailPayload([
    ["live", "2026-06-02", "2026-06-01", "2026-06-08", 1, null]
  ], "2026-06", "live");
  assert.equal(hooks.decodeDetail(boundary, detailExpectation(boundary))[0].source, "live");
});

test("verification tables show feature, prediction and actual target dates across years", async () => {
  const cases = [
    ["T+1", "2026-01-04", "2026/01/04", "目标日"],
    ["monthly_average", "2025-12-16", "2026/01", "目标月"],
    ["quarterly_average", "2026-01-01", "2026/Q1", "目标季度"],
    ["annual_average", "2026-01-01", "2026", "目标年度"]
  ];
  for (const [task, targetDate, targetLabel, header] of cases) {
    const elements = Object.fromEntries([
      "factorDailyTableBody", "factorCalendarTitle", "factorCalendarMeta",
      "factorDailyDateHeader", "factorCalendarNote", "factorMonthlyTableBody",
      "factorTrendChart"
    ].map((id) => [id, { innerHTML: "", textContent: "", clientWidth: 700 }]));
    const harness = createHarness(
      () => Promise.resolve(okResponse(schemePayload(task, "2025-12", "backtest"))), elements
    );
    harness.hooks.start();
    await waitFor(() => harness.hooks.state().snapshotId === "feature-axis");
    try {
      const payload = detailPayload([
        ["backtest", "2025-12-16", "2025-12-15", targetDate, 1, null]
      ], "2025-12");
      const rows = harness.hooks.decodeDetail(payload, detailExpectation(payload))
        .map((row) => harness.hooks.detailRow(row, task));
      harness.hooks.renderDailyRows("2025-12", { status: "ready", rows });
      const cells = [...elements.factorDailyTableBody.innerHTML.matchAll(/<td[^>]*>(.*?)<\/td>/g)]
        .map((match) => match[1]);
      assert.equal(cells.length, 6);
      assert.deepEqual(cells.slice(0, 3), ["12/15", "12/16", targetLabel]);
      assert.equal(elements.factorDailyDateHeader.textContent, header);
      assert.match(elements.factorCalendarTitle.textContent, /^2025\/12 特征月/);
      assert.match(elements.factorMonthlyTableBody.innerHTML, /<td>2025\/12<\/td>/);
      assert.match(elements.factorTrendChart.innerHTML, />2025\/12<\/text>/);
      assert.match(cells[5], /is-neutral/);
      for (const state of [{ status: "loading" }, { status: "error" }, { status: "ready", rows: [] }]) {
        harness.hooks.renderDailyRows("2025-12", state);
        assert.match(elements.factorDailyTableBody.innerHTML, /colspan="6"/);
      }
    } finally {
      harness.hooks.stop();
    }
  }
});

function twoMonthPayload(snapshotId, range = null) {
  const payload = schemePayload("T+1");
  payload.snapshot_id = snapshotId;
  payload.feature_date_bounds = {
    all: { start_date: "2026-05-01", end_date: "2026-06-30" },
    backtest: { start_date: "2026-05-01", end_date: "2026-05-29" },
    live: { start_date: "2026-06-01", end_date: "2026-06-30" }
  };
  payload.selected_feature_range = range;
  payload.schemes[0].monthly_rows = [
    ["2026-05", "backtest", 3, 3, 2, 3, 0, 0, 2, 1, 0, 2, 0],
    ["2026-06", "live", 4, 4, 3, 4, 0, 0, 3, 1, 0, 3, 0]
  ];
  payload.schemes[0].range_rows = range ? [
    ["backtest", 1, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0],
    ["live", 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0]
  ] : payload.schemes[0].monthly_rows.map((row) => row.slice(1));
  return payload;
}

test("day range statistics use interval counts while details retain full touched months", async () => {
  const urls = [];
  const range = { start_date: "2026-05-20", end_date: "2026-06-10" };
  const elements = {
    factorMonthlyTableBody: { innerHTML: "", textContent: "" },
    factorDetailRange: { textContent: "" },
    factorTrendChart: { innerHTML: "", textContent: "", clientWidth: 700 }
  };
  const harness = createHarness((url) => {
    urls.push(url);
    return Promise.resolve(okResponse(twoMonthPayload(url.includes("?") ? "range" : "whole", url.includes("?") ? range : null)));
  }, elements);
  harness.hooks.start();
  await waitFor(() => harness.hooks.state().snapshotId === "whole");
  assert.equal(harness.hooks.state().startDate, "2026-05-01");
  assert.equal(harness.hooks.state().endDate, "2026-06-30");
  assert.equal(harness.hooks.selectedMetric().samples, 7);
  await harness.hooks.selectRange("all", range);
  assert.equal(urls.at(-1), "/api/factor-lab/dashboard?start-date=2026-05-20&end-date=2026-06-10");
  assert.equal(harness.hooks.selectedMetric().samples, 2);
  assert.equal(harness.hooks.selectedMetric().overall, 50);
  assert.deepEqual(Array.from(harness.hooks.visibleMonths()), ["2026-05", "2026-06"]);
  assert.equal(harness.hooks.state().startDate, "2026-05-20");
  assert.match(elements.factorMonthlyTableBody.innerHTML, /<td>2026\/05<\/td><td><strong>3<\/strong>/);
  assert.match(elements.factorMonthlyTableBody.innerHTML, /<td>2026\/06<\/td><td><strong>4<\/strong>/);
  assert.doesNotMatch(elements.factorMonthlyTableBody.innerHTML, /实盘口径/);
  assert.match(elements.factorTrendChart.innerHTML, /2026\/05 · 整体准确率 66.7%/);
  assert.match(elements.factorDetailRange.textContent, /^整月详情：2026\/05 至 2026\/06/);
  await harness.hooks.selectRange("live", null);
  assert.equal(urls.at(-1), "/api/factor-lab/dashboard");
  assert.equal(harness.hooks.state().startDate, "2026-06-01");
  assert.equal(harness.hooks.state().endDate, "2026-06-30");
  assert.equal(harness.hooks.selectedMetric().samples, 4);
  assert.deepEqual(Array.from(harness.hooks.visibleMonths()), ["2026-06"]);
  harness.hooks.stop();
});

test("failed or superseded date selections never relabel the committed statistics", async () => {
  const harness = createHarness(() => Promise.resolve(okResponse(twoMonthPayload("whole"))));
  harness.hooks.start();
  await waitFor(() => harness.hooks.state().snapshotId === "whole");
  let resolveFirst;
  harness.setFetch(() => new Promise((resolve) => { resolveFirst = resolve; }));
  const firstRange = { start_date: "2026-05-20", end_date: "2026-06-10" };
  const first = harness.hooks.selectRange("all", firstRange);
  await waitFor(() => typeof resolveFirst === "function");
  assert.equal(harness.hooks.state().startDate, "2026-05-01");
  assert.equal(harness.hooks.selectedMetric().samples, 7);
  harness.setFetch(() => Promise.reject(new Error("offline")));
  await harness.hooks.selectRange("live", null);
  resolveFirst(okResponse(twoMonthPayload("late-range", firstRange)));
  await first;
  assert.equal(harness.hooks.state().snapshotId, "whole");
  assert.equal(harness.hooks.state().startDate, "2026-05-01");
  assert.equal(harness.hooks.state().dataSource, "all");
  assert.equal(harness.hooks.selectedMetric().samples, 7);
  assert.equal(harness.hooks.state().dataMode, "stale");
  harness.hooks.stop();
});

test("a response for another day interval is rejected without replacing the view", async () => {
  const harness = createHarness(() => Promise.resolve(okResponse(twoMonthPayload("whole"))));
  harness.hooks.start();
  await waitFor(() => harness.hooks.state().snapshotId === "whole");
  const range = { start_date: "2026-05-20", end_date: "2026-06-10" };
  await harness.hooks.selectRange("all", range);
  assert.equal(harness.hooks.state().snapshotId, "whole");
  assert.match(harness.hooks.state().apiError, /does not match request/);
  harness.hooks.stop();
});

test("an interval without predictions keeps schemes and full-month detail available", async () => {
  const harness = createHarness(() => Promise.resolve(okResponse(twoMonthPayload("whole"))));
  harness.hooks.start();
  await waitFor(() => harness.hooks.state().snapshotId === "whole");
  const range = { start_date: "2026-05-30", end_date: "2026-05-30" };
  const payload = twoMonthPayload("empty-day", range);
  payload.schemes[0].range_rows = [];
  harness.setFetch(() => Promise.resolve(okResponse(payload)));
  await harness.hooks.selectRange("all", range);
  assert.equal(harness.hooks.state().snapshotId, "empty-day");
  assert.equal(harness.hooks.selectedMetric().samples, 0);
  assert.equal(harness.hooks.selectedMetric().overall, null);
  assert.deepEqual(Array.from(harness.hooks.visibleMonths()), ["2026-05"]);
  harness.hooks.stop();
});

test("trend leaves missing metrics blank, breaks lines across gaps, and preserves actual zero percent", async () => {
  const payload = twoMonthPayload("pending-month");
  payload.feature_date_bounds.all.end_date = "2026-07-31";
  payload.feature_date_bounds.live.end_date = "2026-07-31";
  payload.schemes[0].monthly_rows = [
    ["2026-05", "backtest", 1, 1, 1, 1, 0, 0, 1, 0, 0, 1, 0],
    ["2026-06", "live", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    ["2026-07", "live", 1, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0]
  ];
  payload.schemes[0].range_rows = [
    payload.schemes[0].monthly_rows[0].slice(1),
    payload.schemes[0].monthly_rows[2].slice(1)
  ];
  const chart = { innerHTML: "", textContent: "", clientWidth: 700 };
  const harness = createHarness(() => Promise.resolve(okResponse(payload)), {
    factorMonthlyTableBody: { innerHTML: "", textContent: "" }, factorTrendChart: chart
  });
  harness.hooks.start();
  await waitFor(() => harness.hooks.state().snapshotId === "pending-month");
  assert.match(chart.innerHTML, />2026\/06<\/text>/);
  assert.doesNotMatch(chart.innerHTML, /<title>2026\/06 ·/);
  assert.match(chart.innerHTML, /<title>2026\/07 · 整体准确率 0.0%<\/title>/);
  const overallPath = chart.innerHTML.match(/<path class="factor-trend-line" d="([^"]+)" stroke="#15623f"/)[1];
  assert.equal((overallPath.match(/M/g) || []).length, 2);
  assert.doesNotMatch(overallPath, /L/);
  harness.hooks.stop();
});
