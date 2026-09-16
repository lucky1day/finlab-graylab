"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

if (process.argv.length < 5) {
  throw new Error("expected page-ordered HTTP, Dashboard, and auth scripts");
}

function element(id) {
  const fields = new Map();
  const value = {
    id,
    hidden: false,
    open: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    value: "",
    type: "text",
    dataset: {},
    style: { removeProperty() {}, setProperty() {} },
    classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; }
    },
    addEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    appendChild() {},
    contains() { return false; },
    focus() {},
    reset() {},
    close() { value.open = false; },
    showModal() { value.open = true; },
    getBoundingClientRect() { return { right: 100, bottom: 100, top: 50 }; },
    querySelector() { return element(id + "-child"); },
    querySelectorAll() { return []; },
    closest() { return element(id + "-closest"); }
  };
  value.elements = new Proxy({}, {
    get(_target, name) {
      if (!fields.has(name)) fields.set(name, element(id + "-" + String(name)));
      return fields.get(name);
    }
  });
  return value;
}

const elements = new Map();
const getElement = (id) => {
  if (!elements.has(id)) elements.set(id, element(id));
  return elements.get(id);
};
const document = {
  visibilityState: "visible",
  body: { contains: () => true },
  getElementById: getElement,
  querySelector: () => getElement("factor-view"),
  querySelectorAll: () => [],
  createElement: (tag) => element(tag),
  addEventListener() {}
};
const user = {
  id: 1,
  username: "proxy-smoke",
  role: "user",
  status: "active",
  is_protected_admin: false,
  must_change_password: false,
  created_at: "2026-09-16T00:00:00Z",
  full_name: null,
  organization_name: null
};
const window = {
  __BFL_ENABLE_TEST_HOOKS__: true,
  __BFL_ENABLE_AUTH_TEST_HOOKS__: true,
  location: { pathname: "/bond-factor-lab/", origin: "https://bond.finailab.cn" },
  history: { pushState() {} },
  document,
  AbortController,
  CustomEvent: function CustomEvent(type) { this.type = type; },
  URLSearchParams,
  performance: { now: () => Date.now() },
  fetch: () => Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: () => Promise.resolve({ user })
  }),
  innerWidth: 1200,
  innerHeight: 800,
  addEventListener() {},
  dispatchEvent() { return true; },
  requestAnimationFrame(callback) { callback(); },
  setTimeout,
  clearTimeout
};
window.window = window;
window.self = window;
const context = vm.createContext({
  window,
  self: window,
  document,
  AbortController,
  CustomEvent: window.CustomEvent,
  URLSearchParams,
  Promise,
  Object,
  Error,
  Number,
  String,
  Boolean,
  Array,
  RegExp,
  Map,
  Set,
  Date,
  Math,
  console,
  setTimeout,
  clearTimeout,
  performance: window.performance,
  fetch: window.fetch,
  location: window.location,
  history: window.history
});

for (const scriptPath of process.argv.slice(2)) {
  vm.runInContext(fs.readFileSync(scriptPath, "utf8"), context, {
    filename: scriptPath
  });
  if (scriptPath.endsWith("aifin-shell.js")) {
    assert.ok(window.BondFactorLabDashboard);
    const dashboard = window.BondFactorLabDashboard;
    let starts = 0;
    window.BondFactorLabDashboard = {
      start() { starts += 1; },
      stop() { dashboard.stop(); },
      count() { return starts; }
    };
  }
}

setImmediate(() => {
  setImmediate(() => {
    assert.ok(window.BondFactorLabHttp);
    assert.ok(window.__BFL_AUTH_TEST_HOOKS__);
    assert.equal(window.__BFL_AUTH_TEST_HOOKS__.state().user.username, "proxy-smoke");
    assert.equal(window.BondFactorLabDashboard.count(), 1);
  });
});
