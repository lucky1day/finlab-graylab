"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const { createJsonClient } = require("../frontend/factor-lab-http.js");

function createClient(fetchImpl, overrides) {
  return createJsonClient(Object.assign({
    fetch: fetchImpl,
    resolveUrl: (url) => "/bond-factor-lab" + url,
    AbortController,
    setTimeout,
    clearTimeout,
    defaultTimeoutMs: 20,
    maxRetryDelayMs: 2147483647
  }, overrides || {}));
}

test("HTTP client resolves the public URL and reads JSON", async () => {
  let request;
  const fetchJson = createClient((url, options) => {
    request = { url, options };
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ snapshot_id: "snapshot-1" })
    });
  });

  assert.deepEqual(
    await fetchJson("/api/factor-lab/dashboard"),
    { snapshot_id: "snapshot-1" }
  );
  assert.equal(request.url, "/bond-factor-lab/api/factor-lab/dashboard");
  assert.equal(request.options.cache, "no-store");
  assert.equal(request.options.credentials, "same-origin");
  assert.equal(request.options.method, "GET");
  assert.equal(request.options.headers.Accept, "application/json");
});

test("HTTP client serializes authenticated JSON write options", async () => {
  let request;
  const fetchJson = createClient((url, options) => {
    request = { url, options };
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true })
    });
  });

  await fetchJson("/api/auth/change-password", {
    method: "POST",
    body: { current_password: "old", new_password: "new" },
    headers: { "X-Test": "yes" }
  });

  assert.equal(request.options.method, "POST");
  assert.equal(request.options.credentials, "same-origin");
  assert.equal(request.options.headers["Content-Type"], "application/json");
  assert.equal(request.options.headers["X-Test"], "yes");
  assert.equal(
    request.options.body,
    JSON.stringify({ current_password: "old", new_password: "new" })
  );
});

test("HTTP client owns the timeout through response body reading", async () => {
  const fetchJson = createClient(() => Promise.resolve({
    ok: true,
    status: 200,
    json: () => new Promise(() => {})
  }));

  await assert.rejects(
    fetchJson("/api/factor-lab/dashboard", { timeoutMs: 5 }),
    (error) => error.code === "request_timeout" && error.timeoutMs === 5
  );
});

test("HTTP client clears timers and external abort listeners on success and timeout", async () => {
  function trackedResources() {
    let timerCallback = null;
    const resource = {
      added: 0,
      removed: 0,
      cleared: 0,
      signal: {
        aborted: false,
        reason: undefined,
        addEventListener(type) {
          assert.equal(type, "abort");
          resource.added += 1;
        },
        removeEventListener(type) {
          assert.equal(type, "abort");
          resource.removed += 1;
        }
      },
      setTimeout(callback) {
        timerCallback = callback;
        return "timer-token";
      },
      clearTimeout(token) {
        assert.equal(token, "timer-token");
        resource.cleared += 1;
      },
      fireTimer() {
        assert.ok(timerCallback);
        timerCallback();
      }
    };
    return resource;
  }

  const successResources = trackedResources();
  const successfulFetch = createClient(() => Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve({ ok: true })
  }), {
    setTimeout: successResources.setTimeout,
    clearTimeout: successResources.clearTimeout
  });
  await successfulFetch("/api/factor-lab/dashboard", {
    signal: successResources.signal
  });
  assert.deepEqual(
    [successResources.added, successResources.removed, successResources.cleared],
    [1, 1, 1]
  );

  const timeoutResources = trackedResources();
  const pendingFetch = createClient(() => new Promise(() => {}), {
    setTimeout: timeoutResources.setTimeout,
    clearTimeout: timeoutResources.clearTimeout
  });
  const pendingRequest = pendingFetch("/api/factor-lab/dashboard", {
    signal: timeoutResources.signal
  });
  timeoutResources.fireTimer();
  await assert.rejects(pendingRequest, (error) => error.code === "request_timeout");
  assert.deepEqual(
    [timeoutResources.added, timeoutResources.removed, timeoutResources.cleared],
    [1, 1, 1]
  );
});

test("HTTP client reports cancellation without raising an auth event", async () => {
  let unauthorizedEvents = 0;
  const controller = new AbortController();
  const fetchJson = createClient(() => new Promise(() => {}), {
    onUnauthorized: () => { unauthorizedEvents += 1; }
  });

  const request = fetchJson("/api/factor-lab/dashboard", {
    signal: controller.signal,
    timeoutMs: 100
  });
  controller.abort("identity-changed");

  await assert.rejects(request, (error) =>
    error.code === "request_aborted" && error.reason === "identity-changed"
  );
  assert.equal(unauthorizedEvents, 0);
});

test("HTTP client preserves Retry-After and unauthorized semantics", async () => {
  let calls = 0;
  let unauthorizedEvents = 0;
  const fetchJson = createClient(() => {
    calls += 1;
    return Promise.resolve({
      ok: false,
      status: calls === 1 ? 429 : 401,
      headers: { get: () => calls === 1 ? "2" : null },
      json: () => Promise.resolve({ error_code: "request_failed" })
    });
  }, {
    onUnauthorized: () => { unauthorizedEvents += 1; }
  });

  const before = Date.now();
  await assert.rejects(
    fetchJson("/api/factor-lab/dashboard"),
    (error) => error.status === 429 && error.retryAfterAt >= before + 1900
  );
  await assert.rejects(
    fetchJson("/api/factor-lab/dashboard"),
    (error) => error.status === 401
  );
  assert.equal(unauthorizedEvents, 1);
});

test("HTTP client parses HTTP-date and fails closed on unsafe Retry-After", async () => {
  const retryDate = new Date(Date.now() + 3000).toUTCString();
  const values = [retryDate, "2026-09-16", "99999999999999999999"];
  const fetchJson = createClient(() => {
    const value = values.shift();
    return Promise.resolve({
      ok: false,
      status: 429,
      headers: { get: (name) => name === "Retry-After" ? value : null },
      json: () => Promise.resolve({ error_code: "rate_limited" })
    });
  });

  await assert.rejects(
    fetchJson("/api/factor-lab/dashboard"),
    (error) => error.retryAfter === retryDate && error.retryAfterAt >= Date.parse(retryDate)
  );
  await assert.rejects(
    fetchJson("/api/factor-lab/dashboard"),
    (error) => error.retryAfter === "2026-09-16" && error.retryAfterAt === undefined
  );
  await assert.rejects(
    fetchJson("/api/factor-lab/dashboard"),
    (error) => error.retryAfterAt === Infinity
  );
});

test("401 invalidates the current identity before a pending error body", async () => {
  let unauthorizedEvents = 0;
  const fetchJson = createClient(() => Promise.resolve({
    ok: false,
    status: 401,
    headers: { get: (name) => name === "X-Request-ID" ? "request-401" : null },
    json: () => new Promise(() => {})
  }), {
    onUnauthorized: () => { unauthorizedEvents += 1; }
  });

  const request = fetchJson("/api/auth/me", { timeoutMs: 5 });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(unauthorizedEvents, 1);
  await assert.rejects(
    request,
    (error) => error.code === "request_timeout" &&
      error.status === 401 && error.requestId === "request-401"
  );
});

test("429 keeps Retry-After metadata when the error body times out", async () => {
  const fetchJson = createClient(() => Promise.resolve({
    ok: false,
    status: 429,
    headers: {
      get(name) {
        if (name === "Retry-After") return "60";
        if (name === "X-Request-ID") return "request-429";
        return null;
      }
    },
    json: () => new Promise(() => {})
  }));

  await assert.rejects(
    fetchJson("/api/factor-lab/dashboard", { timeoutMs: 5 }),
    (error) => error.code === "request_timeout" &&
      error.status === 429 && error.requestId === "request-429" &&
      Number.isFinite(error.retryAfterAt)
  );
});
