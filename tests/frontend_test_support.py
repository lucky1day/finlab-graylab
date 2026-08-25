from __future__ import annotations

import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
JAVASCRIPT_PATH = PROJECT_ROOT / "frontend" / "aifin-shell.js"

_NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const [shellPath, hookName, serializedArgs] = process.argv.slice(1);
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
try {
  const hook = window.__factorLabTestHooks[hookName];
  if (typeof hook !== "function") throw new Error(`missing hook: ${hookName}`);
  const value = hook(...JSON.parse(serializedArgs));
  process.stdout.write(JSON.stringify({ ok: true, value }));
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, message: String(error.message || error) }));
}
"""


def _hook_payload(hook_name: str, *args: object) -> dict[str, object]:
    result = subprocess.run(
        [
            "node",
            "-e",
            _NODE_HARNESS,
            str(JAVASCRIPT_PATH),
            hook_name,
            json.dumps(args, ensure_ascii=False),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def call_frontend_hook(hook_name: str, *args: object) -> object:
    payload = _hook_payload(hook_name, *args)
    assert payload["ok"], payload["message"]
    return payload["value"]


def call_frontend_hook_error(hook_name: str, *args: object) -> str:
    payload = _hook_payload(hook_name, *args)
    assert not payload["ok"], payload
    return str(payload["message"])
