"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const HTTP_SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "factor-lab-http.js"),
  "utf8"
);
const AUTH_SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "auth-shell.js"),
  "utf8"
);

function validUser(overrides) {
  return Object.assign({
    id: 1,
    username: "owner",
    role: "admin",
    status: "active",
    is_protected_admin: true,
    must_change_password: false,
    created_at: "2026-09-16T00:00:00Z",
    full_name: null,
    organization_name: null
  }, overrides || {});
}

function jsonResponse(status, body, headers) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (headers || {})[name] || null },
    json: () => Promise.resolve(body)
  };
}

function createElement(id) {
  const listeners = new Map();
  const fields = new Map();
  const element = {
    id,
    hidden: false,
    open: false,
    disabled: false,
    textContent: "",
    value: "",
    type: "text",
    dataset: {},
    style: {},
    classList: {
      add() {},
      remove() {},
      contains() { return false; },
      toggle() {}
    },
    addEventListener(type, callback) { listeners.set(type, callback); },
    setAttribute() {},
    removeAttribute() {},
    appendChild() {},
    contains() { return false; },
    focus() {},
    reset() {},
    close() { element.open = false; },
    showModal() { element.open = true; },
    getBoundingClientRect() { return { right: 100, bottom: 100, top: 50 }; },
    querySelector() { return createElement(id + "-child"); },
    querySelectorAll() { return []; },
    closest() { return createElement(id + "-closest"); },
    _listeners: listeners
  };
  element.elements = new Proxy({}, {
    get(_target, name) {
      if (!fields.has(name)) fields.set(name, createElement(id + "-" + String(name)));
      return fields.get(name);
    }
  });
  return element;
}

function createHarness(fetchImpl) {
  const elements = new Map();
  const timers = new Map();
  let timerSequence = 0;
  let dashboardStarts = 0;
  let dashboardStops = 0;
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, createElement(id));
    return elements.get(id);
  };
  const document = {
    body: { contains: () => true },
    getElementById: getElement,
    querySelector: () => getElement("factor-view"),
    querySelectorAll: () => [],
    createElement: (tag) => createElement(tag),
    addEventListener() {}
  };
  const window = {
    __BFL_ENABLE_AUTH_TEST_HOOKS__: true,
    location: { pathname: "/bond-factor-lab/" },
    document,
    AbortController,
    fetch: fetchImpl,
    innerWidth: 1200,
    innerHeight: 800,
    addEventListener() {},
    requestAnimationFrame(callback) { callback(); },
    setTimeout(callback, delay) {
      const token = ++timerSequence;
      timers.set(token, { callback, delay });
      return token;
    },
    clearTimeout(token) { timers.delete(token); },
    BondFactorLabDashboard: {
      start() { dashboardStarts += 1; },
      stop() { dashboardStops += 1; }
    }
  };
  window.window = window;
  window.self = window;
  const context = vm.createContext({
    window,
    self: window,
    document,
    AbortController,
    Promise,
    Object,
    Error,
    Number,
    String,
    Boolean,
    Array,
    Map,
    Set,
    Date,
    Math,
    console
  });
  vm.runInContext(HTTP_SOURCE, context, { filename: "factor-lab-http.js" });
  vm.runInContext(AUTH_SOURCE, context, { filename: "auth-shell.js" });
  return {
    hooks: window.__BFL_AUTH_TEST_HOOKS__,
    getElement,
    runTimers(delay) {
      for (const [token, timer] of [...timers.entries()]) {
        if (timer.delay === delay) {
          timers.delete(token);
          timer.callback();
        }
      }
    },
    dashboardCounts: () => ({ starts: dashboardStarts, stops: dashboardStops })
  };
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

test("auth shell validates reads and accepts create-user write payloads", async () => {
  const requests = [];
  let malformedUsers = false;
  const harness = createHarness((url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/api/auth/me")) {
      return Promise.resolve(jsonResponse(200, { user: validUser() }));
    }
    if (url.endsWith("/api/admin/users") && options.method === "POST") {
      return Promise.resolve(jsonResponse(200, { user: validUser({ id: 2, username: "new-user", is_protected_admin: false }) }));
    }
    return Promise.resolve(jsonResponse(200, malformedUsers ? { users: [{}] } : { users: [validUser()] }));
  });
  await flush();
  await flush();

  const created = await harness.hooks.apiRequest("/api/admin/users", {
    method: "POST",
    body: { username: "new-user" }
  });
  assert.equal(created.user.username, "new-user");
  assert.equal(requests.at(-1).options.credentials, "same-origin");
  assert.equal(requests.at(-1).options.body, JSON.stringify({ username: "new-user" }));

  malformedUsers = true;
  await assert.rejects(
    harness.hooks.apiRequest("/api/admin/users"),
    (error) => error.message === "invalid_auth_payload"
  );
});

test("an old identity's late 401 cannot log out the new identity", async () => {
  let resolveOldRequest;
  const harness = createHarness((url) => {
    if (url.endsWith("/api/auth/me")) {
      return Promise.resolve(jsonResponse(200, { user: validUser() }));
    }
    return new Promise((resolve) => { resolveOldRequest = resolve; });
  });
  await flush();
  await flush();

  const oldRequest = harness.hooks.apiRequest("/api/admin/users");
  await flush();
  harness.hooks.showAuthenticated(validUser({ id: 3, username: "replacement", is_protected_admin: false }));
  resolveOldRequest({
    ok: false,
    status: 401,
    headers: { get: () => null },
    json: () => Promise.resolve({ error_code: "unauthorized" })
  });

  await assert.rejects(oldRequest, (error) => error.code === "request_aborted");
  await flush();
  assert.equal(harness.hooks.state().user.username, "replacement");
});

test("a timed-out authentication write reports an unknown outcome", async () => {
  const harness = createHarness((url) => {
    if (url.endsWith("/api/auth/me")) {
      return Promise.resolve(jsonResponse(200, { user: validUser() }));
    }
    return new Promise(() => {});
  });
  await flush();
  await flush();

  const request = harness.hooks.apiRequest("/api/auth/change-password", {
    method: "POST",
    body: { current_password: "old", new_password: "Newpass1" }
  });
  harness.runTimers(6000);
  await assert.rejects(
    request,
    (error) => error.code === "request_timeout" && error.errorCode === "request_outcome_unknown"
  );
});

test("showing the login gate clears identity-owned dashboard state", async () => {
  const harness = createHarness((url) => Promise.resolve(
    jsonResponse(200, { user: validUser(), path: url })
  ));
  await flush();
  await flush();

  const before = harness.hooks.state().identitySeq;
  harness.hooks.showLogin();
  assert.equal(harness.hooks.state().identitySeq, before + 1);
  assert.equal(harness.hooks.state().user, null);
  assert.equal(harness.getElement("authUsersBody").textContent, "");
  assert.ok(harness.dashboardCounts().stops >= 1);
});

test("logout keeps login unavailable until its Set-Cookie response completes", async () => {
  let resolveLogout;
  const harness = createHarness((url) => {
    if (url.endsWith("/api/auth/me")) {
      return Promise.resolve(jsonResponse(200, { user: validUser() }));
    }
    if (url.endsWith("/api/auth/logout")) {
      return new Promise((resolve) => { resolveLogout = resolve; });
    }
    throw new Error("unexpected request");
  });
  await flush();
  await flush();

  harness.getElement("authLogoutButton")._listeners.get("click")();
  await flush();

  assert.equal(harness.getElement("authLoading").hidden, false);
  assert.equal(harness.getElement("authLogin").hidden, true);
  assert.equal(harness.hooks.state().user, null);

  resolveLogout(jsonResponse(200, { status: "ok" }));
  await flush();
  await flush();

  assert.equal(harness.getElement("authLoading").hidden, true);
  assert.equal(harness.getElement("authLogin").hidden, false);
});
