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
  const children = [];
  const queries = new Map();
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
    appendChild(child) { children.push(child); },
    children,
    contains() { return false; },
    focus() {},
    reset() {},
    close() { element.open = false; },
    showModal() { element.open = true; },
    getBoundingClientRect() { return { right: 100, bottom: 100, top: 50 }; },
    querySelector(selector) {
      if (!queries.has(selector)) queries.set(selector, createElement(id + "-" + selector));
      return queries.get(selector);
    },
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
  const dashboardCalls = [];
  const getElement = (id) => {
    if (!elements.has(id)) {
      const element = createElement(id);
      elements.set(id, element);
      if (id.endsWith("Form")) {
        element.closest = () => getElement(id.replace(/Form$/, "Dialog"));
      }
      if (id.endsWith("Dialog")) {
        const query = element.querySelector;
        element.querySelector = (selector) => selector === "form"
          ? getElement(id.replace(/Dialog$/, "Form")) : query(selector);
      }
    }
    return elements.get(id);
  };
  const document = {
    body: { contains: () => true },
    getElementById: getElement,
    querySelector: () => getElement("factor-view"),
    querySelectorAll: (selector) => selector === ".auth-dialog[open]"
      ? [...elements.values()].filter((element) => element.id.endsWith("Dialog") && element.open) : [],
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
      start(options) { dashboardStarts += 1; dashboardCalls.push({ action: "start", options }); },
      stop(options) { dashboardStops += 1; dashboardCalls.push({ action: "stop", options }); }
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
    Intl,
    Math,
    console
  });
  vm.runInContext(HTTP_SOURCE, context, { filename: "factor-lab-http.js" });
  vm.runInContext(AUTH_SOURCE, context, { filename: "auth-shell.js" });
  return {
    hooks: window.__BFL_AUTH_TEST_HOOKS__,
    getElement,
    dashboardCalls,
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

test("Dashboard view transitions receive the existing authentication identity", async () => {
  const harness = createHarness((url) => Promise.resolve(jsonResponse(200,
    url.endsWith("/api/admin/users") ? { users: [] } : { user: validUser() }
  )));
  await flush();
  const first = harness.dashboardCalls.at(-1).options.identity;
  harness.getElement("authUsersNav")._listeners.get("click")();
  assert.equal(harness.dashboardCalls.at(-1).options.identity, first);
  harness.getElement("factorLabNav")._listeners.get("click")();
  assert.equal(harness.dashboardCalls.at(-1).options.identity, first);
  harness.hooks.showLogin();
  harness.hooks.showAuthenticated(validUser({ id: 3, username: "replacement" }));
  assert.notEqual(harness.dashboardCalls.at(-1).options.identity, first);
});

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

for (const stage of ["headers", "body"]) {
  test(`an old logout cancelled during ${stage} cannot change a replacement identity`, async () => {
    let completeLogout;
    const harness = createHarness((url) => {
      if (url.endsWith("/api/auth/me")) return Promise.resolve(jsonResponse(200, { user: validUser() }));
      if (stage === "headers") return new Promise((resolve) => { completeLogout = resolve; });
      return Promise.resolve({ ok: true, status: 200,
        json: () => new Promise((resolve) => { completeLogout = resolve; }) });
    });
    await flush();
    harness.getElement("authLogoutButton")._listeners.get("click")();
    await flush();
    harness.hooks.showAuthenticated(validUser({ id: 3, username: "replacement" }));
    const login = harness.getElement("authLoginSubmit");
    login.disabled = true;
    await flush();
    assert.equal(harness.hooks.state().user.username, "replacement");
    assert.equal(harness.getElement("aifin-shell").hidden, false);
    assert.equal(login.disabled, true);
    completeLogout(stage === "headers" ? jsonResponse(200, { status: "ok" }) : { status: "ok" });
    await flush();
    assert.equal(harness.hooks.state().user.username, "replacement");
    assert.equal(login.disabled, true);
  });
}

test("an old self-rename login timer cannot clear a replacement identity", async () => {
  const owner = validUser({ is_protected_admin: false });
  const harness = createHarness((url, options) => Promise.resolve(jsonResponse(200,
    url.endsWith("/api/auth/me") ? { user: owner } : options.method === "GET"
      ? { users: [owner] } : { user: validUser({ username: "renamed", is_protected_admin: false }) }
  )));
  await flush();
  harness.getElement("authUsersNav")._listeners.get("click")();
  await flush();
  const button = { dataset: { userAction: "edit", userId: "1" }, disabled: false };
  harness.getElement("authUsersBody")._listeners.get("click")({
    target: { closest: (selector) => selector === "[data-user-action]" ? button : null }
  });
  const form = harness.getElement("authUserEditForm");
  form.elements.username.value = "renamed";
  form._listeners.get("submit")({ preventDefault() {}, currentTarget: form });
  await flush();
  harness.hooks.showAuthenticated(validUser({ id: 3, username: "replacement" }));
  harness.runTimers(150);
  assert.equal(harness.hooks.state().user.username, "replacement");
  assert.equal(harness.getElement("aifin-shell").hidden, false);
});

for (const [formId, endpoint, buttonId] of [
  ["authCreateUserForm", "/api/admin/users", "authCreateUserButton"],
  ["authProfileForm", "/api/auth/update-profile", "authProfileButton"]
]) {
  test(`${formId} coalesces writes and ignores an old dialog completion`, async () => {
    const writes = [];
    const harness = createHarness((url, options) => {
      if (url.endsWith("/api/auth/me")) return Promise.resolve(jsonResponse(200, { user: validUser() }));
      if (url.endsWith(endpoint) && options.method === "POST") {
        return new Promise((resolve) => writes.push(resolve));
      }
      return Promise.resolve(jsonResponse(200, { users: [] }));
    });
    await flush();
    const open = () => harness.getElement(buttonId)._listeners.get("click")();
    const form = harness.getElement(formId);
    const dialog = form.closest("dialog");
    const submit = () => form._listeners.get("submit")({ preventDefault() {}, currentTarget: form });
    open();
    submit();
    submit();
    await flush();
    assert.equal(writes.length, 1);
    assert.equal(dialog.dataset.saving, "true");
    harness.hooks.showLogin();
    harness.hooks.showAuthenticated(validUser({ id: 3, username: "replacement" }));
    open();
    form.elements.fullName.value = "New draft";
    writes[0](jsonResponse(200, { user: validUser({ full_name: "Old result" }) }));
    await flush();
    assert.equal(dialog.open, true);
    assert.equal(form.elements.fullName.value, "New draft");
    assert.equal(harness.hooks.state().user.username, "replacement");
    assert.notEqual(dialog.dataset.saving, "true");
  });

  test(`${formId} restores controls after timeout without retrying the write`, async () => {
    let writes = 0;
    const harness = createHarness((url, options) => {
      if (options.method !== "POST") return Promise.resolve(jsonResponse(200, { user: validUser() }));
      writes += 1;
      return new Promise(() => {});
    });
    await flush();
    harness.getElement(buttonId)._listeners.get("click")();
    const form = harness.getElement(formId);
    form._listeners.get("submit")({ preventDefault() {}, currentTarget: form });
    await flush();
    harness.runTimers(6000);
    await flush();
    assert.notEqual(form.closest("dialog").dataset.saving, "true");
    assert.equal(form.querySelector('[type="submit"]').disabled, false);
    assert.match(form.querySelector(".auth-error").textContent, /请求结果待确认/);
    harness.runTimers(6000);
    assert.equal(writes, 1);
  });
}

test("account creation dates render in Shanghai for explicit and legacy UTC values", async () => {
  const harness = createHarness((url) => Promise.resolve(jsonResponse(200,
    url.endsWith("/api/auth/me") ? { user: validUser() } : {
      users: [validUser({ created_at: "2026-12-31T20:30:00.000000Z" }),
        validUser({ id: 2, created_at: "2026-12-31T20:30:00.000000" })]
    }
  )));
  await flush();
  harness.getElement("authUsersNav")._listeners.get("click")();
  await flush();
  const rows = harness.getElement("authUsersBody").children;
  assert.equal(rows.length, 2);
  for (const row of rows) assert.equal(row.children[5].textContent, "2027-01-01 04:30");
});
