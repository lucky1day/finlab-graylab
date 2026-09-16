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
    schema_version: "factor-lab-dashboard-v6",
    representation: "summary",
    snapshot_id: snapshotId,
    generated_at: "2026-09-16T12:00:00+08:00",
    display_until: "2026-09-16",
    live_target_start_date: "2026-06-01",
    monthly_row_fields: [
      "month", "source", "samples", "metric_samples", "correct",
      "predicted_up", "predicted_down", "predicted_flat", "actual_up",
      "actual_down", "actual_flat", "up_true_positive", "down_true_positive"
    ],
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

function createHarness(fetchImpl) {
  const listeners = new Map();
  const scheduledDelays = [];
  const dispatchedEvents = [];
  const document = {
    visibilityState: "visible",
    addEventListener() {},
    getElementById() { return null; },
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
  await new Promise((resolve) => setTimeout(resolve, 10));

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
  await new Promise((resolve) => setTimeout(resolve, 10));

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
  await new Promise((resolve) => setTimeout(resolve, 10));

  const state = harness.hooks.state();
  assert.equal(state.dataMode, "fresh");
  assert.equal(state.remoteLoading, false);
  assert.equal(state.consecutiveFailures, 0);
  harness.hooks.stop();
});

test("Retry-After delta seconds and HTTP-date are exposed as retry metadata", async () => {
  const retryDate = new Date(Date.now() + 3000).toUTCString();
  const values = ["2", retryDate];
  const harness = createHarness(() => Promise.resolve({
    ok: false,
    status: 429,
    headers: { get: () => values.shift() },
    json: () => Promise.resolve({ error_code: "rate_limited" })
  }));

  const beforeDelta = Date.now();
  await assert.rejects(
    harness.hooks.fetchJson("/api/factor-lab/dashboard", { timeoutMs: 100 }),
    (error) => error.retryAfter === "2" && error.retryAfterAt >= beforeDelta + 1900
  );
  await assert.rejects(
    harness.hooks.fetchJson("/api/factor-lab/dashboard", { timeoutMs: 100 }),
    (error) => error.retryAfter === retryDate && error.retryAfterAt >= Date.parse(retryDate)
  );
});

test("invalid HTTP-date is ignored and unsafe delta seconds fail closed", async () => {
  const values = ["2026-09-16", "99999999999999999999"];
  const harness = createHarness(() => Promise.resolve({
    ok: false,
    status: 429,
    headers: { get: () => values.shift() },
    json: () => Promise.resolve({ error_code: "rate_limited" })
  }));

  await assert.rejects(
    harness.hooks.fetchJson("/api/factor-lab/dashboard", { timeoutMs: 100 }),
    (error) => error.retryAfter === "2026-09-16" &&
      error.retryAfterAt === undefined
  );
  await assert.rejects(
    harness.hooks.fetchJson("/api/factor-lab/dashboard", { timeoutMs: 100 }),
    (error) => error.retryAfterAt === Infinity
  );
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
