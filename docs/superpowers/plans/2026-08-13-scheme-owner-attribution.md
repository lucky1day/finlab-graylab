# 方案归属同事展示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 前端「候选方案排行」表格新增「来源」列，显示每个方案由哪位算法同事交付（姓名缩写）。

**Architecture:** 归属记录在版本控制文件 `deploy/scheme_owner_v1.json`，按 registry composite `scheme_id` 索引。后端 dashboard 构建时读入并作为 `owner` 字段进入 scheme payload，前端在排行表渲染为一列。不新增数据库表或列，不触碰任何参与版本哈希的文件。

**Tech Stack:** Python 3.12 / FastAPI / 原生 JS（无构建步骤）/ pytest

## Global Constraints

- 不得修改任何 `schemes/*/config.yaml`、`schemes/*/delivery/*.json`、`schemes/*/predict.py`、`schemes/*/core/**`——这些文件参与 `config_hash` / `manifest_hash` / `code_hash`，改动会变更 `scheme_version`，导致 active Registry 行被同步降级为 `paused`。
- 不新增数据库表、列或 migration。
- 映射文件的键是 registry composite `scheme_id`（形如 `weekly_10y_lgbm_point_v1__h1__10Y`），不是 `base_scheme_id`。同一算法的多个期限各占一条，不做前缀推导或继承。
- 映射文件缺失、`schema_version` 不符、JSON 非法：一律 fail-closed 抛错，不返回空表。
- 单个方案未登记归属：`owner` 取空字符串，前端显示 `--`，不报错。
- 环境命令前缀：`/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service`
- 全量测试基线为 `662 passed`（`pytest tests/ -q`）。

---

### Task 1: 归属映射文件与 loader

**Files:**
- Create: `deploy/scheme_owner_v1.json`
- Create: `backend/scheme_owner.py`
- Test: `tests/test_scheme_owner.py`

**Interfaces:**
- Produces: `backend.scheme_owner.load_scheme_owners(project_root: Path | None = None) -> dict[str, str]`
  返回 `{composite_scheme_id: owner_initials}`。文件路径固定为 `<project_root>/deploy/scheme_owner_v1.json`，`project_root` 缺省为仓库根。
- Produces: `backend.scheme_owner.SchemeOwnerError(RuntimeError)`

- [ ] **Step 1: 创建映射文件骨架**

`deploy/scheme_owner_v1.json`（存量归属由用户后续填写，先留空 owners）：

```json
{
  "schema_version": "scheme-owner-v1",
  "owners": {}
}
```

- [ ] **Step 2: 写失败测试**

`tests/test_scheme_owner.py`:

```python
"""方案归属映射的读取契约。

归属是平台对方案的登记信息，不参与任何计算、gate 或 join。文件本身缺失或
格式非法必须 fail-closed——否则「全部未登记」与「配置坏了」不可区分。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scheme_owner import SchemeOwnerError, load_scheme_owners  # noqa: E402


def _write(root: Path, payload: object) -> None:
    target = root / "deploy"
    target.mkdir(parents=True, exist_ok=True)
    (target / "scheme_owner_v1.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


class LoadSchemeOwnersTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_reads_composite_scheme_id_mapping(self) -> None:
        _write(
            self.root,
            {
                "schema_version": "scheme-owner-v1",
                "owners": {"demo__h1__10Y": "LW"},
            },
        )
        self.assertEqual(
            load_scheme_owners(self.root), {"demo__h1__10Y": "LW"}
        )

    def test_empty_owners_is_valid(self) -> None:
        _write(self.root, {"schema_version": "scheme-owner-v1", "owners": {}})
        self.assertEqual(load_scheme_owners(self.root), {})

    def test_missing_file_fails_closed(self) -> None:
        with self.assertRaises(SchemeOwnerError):
            load_scheme_owners(self.root)

    def test_wrong_schema_version_fails_closed(self) -> None:
        _write(self.root, {"schema_version": "other", "owners": {}})
        with self.assertRaises(SchemeOwnerError):
            load_scheme_owners(self.root)

    def test_invalid_json_fails_closed(self) -> None:
        target = self.root / "deploy"
        target.mkdir(parents=True, exist_ok=True)
        (target / "scheme_owner_v1.json").write_text("{oops", encoding="utf-8")
        with self.assertRaises(SchemeOwnerError):
            load_scheme_owners(self.root)

    def test_non_string_owner_fails_closed(self) -> None:
        _write(
            self.root,
            {"schema_version": "scheme-owner-v1", "owners": {"demo__h1__10Y": 7}},
        )
        with self.assertRaises(SchemeOwnerError):
            load_scheme_owners(self.root)

    def test_repository_file_is_loadable(self) -> None:
        """仓库中的实际文件必须始终可读，否则 dashboard 会整体 fail-closed。"""
        self.assertIsInstance(load_scheme_owners(), dict)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/test_scheme_owner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.scheme_owner'`

- [ ] **Step 4: 实现 loader**

`backend/scheme_owner.py`:

```python
"""方案归属同事的只读登记。

归属是平台对方案的登记信息，按 registry composite ``scheme_id`` 索引。它不
参与任何计算、gate 或 join，也不进入 ``scheme_version``；因此记录在版本控制
文件中，而不是方案配置或数据库。
"""

from __future__ import annotations

import json
from pathlib import Path

OWNER_SCHEMA_VERSION = "scheme-owner-v1"
OWNER_FILE_RELATIVE_PATH = Path("deploy") / "scheme_owner_v1.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SchemeOwnerError(RuntimeError):
    """归属登记文件缺失或不满足读取契约。"""


def load_scheme_owners(project_root: Path | None = None) -> dict[str, str]:
    """读取 composite scheme_id -> 姓名缩写 的登记映射。"""
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    path = root / OWNER_FILE_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SchemeOwnerError(f"scheme owner registry is unreadable: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SchemeOwnerError(f"scheme owner registry is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise SchemeOwnerError("scheme owner registry must be a JSON object")
    if payload.get("schema_version") != OWNER_SCHEMA_VERSION:
        raise SchemeOwnerError(
            "scheme owner registry schema_version must be "
            f"{OWNER_SCHEMA_VERSION!r}, got {payload.get('schema_version')!r}"
        )
    owners = payload.get("owners")
    if not isinstance(owners, dict):
        raise SchemeOwnerError("scheme owner registry owners must be an object")
    result: dict[str, str] = {}
    for scheme_id, owner in owners.items():
        if not isinstance(scheme_id, str) or not scheme_id.strip():
            raise SchemeOwnerError(f"scheme owner registry has invalid key: {scheme_id!r}")
        if not isinstance(owner, str) or not owner.strip():
            raise SchemeOwnerError(
                f"scheme owner registry has invalid owner for {scheme_id}: {owner!r}"
            )
        result[scheme_id] = owner.strip()
    return result
```

- [ ] **Step 5: 运行测试确认通过**

Run: `/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/test_scheme_owner.py -q`
Expected: PASS — `7 passed`

- [ ] **Step 6: 提交**

```bash
git add deploy/scheme_owner_v1.json backend/scheme_owner.py tests/test_scheme_owner.py
git commit -m "feat(backend): add version-controlled scheme owner registry"
```

---

### Task 2: dashboard payload 增加 owner 字段

**Files:**
- Modify: `backend/factor_lab_dashboard_semantics.py`（`SCHEME_FIELDS` 白名单）
- Modify: `backend/factor_lab_dashboard.py`（`_registry_dto` 与其调用处）
- Test: `tests/test_scheme_owner_payload.py`

**Interfaces:**
- Consumes: `backend.scheme_owner.load_scheme_owners`
- Produces: dashboard payload 中每个 scheme 含 `owner: str`（未登记为 `""`）
- Produces: `_registry_dto(row: Mapping[str, Any], owners: Mapping[str, str]) -> dict[str, Any]`

- [ ] **Step 1: 写失败测试**

`tests/test_scheme_owner_payload.py`:

```python
"""dashboard scheme payload 的 owner 字段契约。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.factor_lab_dashboard import _registry_dto  # noqa: E402
from backend.factor_lab_dashboard_semantics import SCHEME_FIELDS  # noqa: E402


def _row() -> dict:
    return {
        "scheme_id": "demo__h1__10Y",
        "base_scheme_id": "demo",
        "name": "demo scheme",
        "description": "d",
        "horizon": 1,
        "task_type": "T+1",
        "frequency": "daily",
        "target_tenor": "10Y",
        "status": "active",
        "deployed_at": "2026-01-01",
    }


class SchemeOwnerPayloadTests(unittest.TestCase):
    def test_owner_is_a_scheme_payload_field(self) -> None:
        self.assertIn("owner", SCHEME_FIELDS)

    def test_registered_scheme_carries_owner(self) -> None:
        dto = _registry_dto(_row(), {"demo__h1__10Y": "LW"})
        self.assertEqual(dto["owner"], "LW")

    def test_unregistered_scheme_gets_empty_owner(self) -> None:
        dto = _registry_dto(_row(), {})
        self.assertEqual(dto["owner"], "")

    def test_owner_lookup_uses_composite_not_base_id(self) -> None:
        """按 base_scheme_id 登记不生效——键必须是 composite scheme_id。"""
        dto = _registry_dto(_row(), {"demo": "LW"})
        self.assertEqual(dto["owner"], "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/test_scheme_owner_payload.py -q`
Expected: FAIL — `_registry_dto() takes 1 positional argument but 2 were given`

- [ ] **Step 3: 白名单加入 owner**

`backend/factor_lab_dashboard_semantics.py`，在 `SCHEME_FIELDS` 中 `"name"` 之后加一行：

```python
SCHEME_FIELDS = {
    "scheme_id",
    "base_scheme_id",
    "name",
    "owner",
    "description",
```

- [ ] **Step 4: `_registry_dto` 接收并输出 owner**

`backend/factor_lab_dashboard.py`，把签名改为接收映射，并在返回字典中 `"name"` 之后加入 `owner`：

```python
def _registry_dto(
    row: Mapping[str, Any],
    owners: Mapping[str, str],
) -> dict[str, Any]:
```

返回字典中：

```python
        "name": _required_text(row.get("name"), field="registry name"),
        "owner": str(owners.get(scheme_id) or ""),
        "description": str(row.get("description") or ""),
```

- [ ] **Step 5: 调用处传入映射**

`backend/factor_lab_dashboard.py:139` 附近，把

```python
    registry = [_registry_dto(row) for row in registry_rows]
```

改为

```python
    scheme_owners = load_scheme_owners()
    registry = [_registry_dto(row, scheme_owners) for row in registry_rows]
```

并在文件顶部 import 区加入：

```python
from backend.scheme_owner import load_scheme_owners
```

- [ ] **Step 6: 运行测试确认通过**

Run: `/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/test_scheme_owner_payload.py -q`
Expected: PASS — `4 passed`

- [ ] **Step 7: 全量回归**

Run: `/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/ -q`
Expected: PASS，总数为基线 662 + 本任务及 Task 1 新增用例数，无新增失败。

- [ ] **Step 8: 提交**

```bash
git add backend/factor_lab_dashboard.py backend/factor_lab_dashboard_semantics.py tests/test_scheme_owner_payload.py
git commit -m "feat(backend): expose scheme owner in dashboard payload"
```

---

### Task 3: 前端排行表新增「来源」列

**Files:**
- Modify: `frontend/index.html`（表头 + 资源哈希）
- Modify: `frontend/aifin-shell.js`（payload 解析、合并对象、行渲染、空态 colspan）
- Test: `tests/test_frontend_owner_column_contract.py`

**Interfaces:**
- Consumes: dashboard payload 的 `scheme.owner`（Task 2 产出）

- [ ] **Step 1: 写失败测试**

`tests/test_frontend_owner_column_contract.py`:

```python
"""排行表「来源」列的结构契约。

表头列数、数据行 <td> 数、空态 colspan 三者必须一致，否则表格会错位。
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "frontend"


def _ranking_header_cells() -> list[str]:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    table = html.split('class="factor-ranking-table"', 1)[1]
    thead = table.split("</thead>", 1)[0]
    return re.findall(r"<th[^>]*>(.*?)</th>", thead, re.S)


def test_header_declares_owner_column() -> None:
    cells = _ranking_header_cells()
    assert any("来源" in cell for cell in cells), cells


def test_row_cell_count_matches_header() -> None:
    js = (FRONTEND / "aifin-shell.js").read_text(encoding="utf-8")
    body = js.split("function renderSchemeRankingRow", 1)[1].split(
        "\n  function ", 1
    )[0]
    assert body.count("'<td") + body.count('"<td') == len(_ranking_header_cells())


def test_empty_state_colspan_matches_header() -> None:
    js = (FRONTEND / "aifin-shell.js").read_text(encoding="utf-8")
    body = js.split("function renderSchemeRanking(", 1)[1].split(
        "\n  function ", 1
    )[0]
    match = re.search(r'colspan="(\d+)" class="factor-empty-cell"', body)
    assert match is not None
    assert int(match.group(1)) == len(_ranking_header_cells())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/test_frontend_owner_column_contract.py -q`
Expected: FAIL — `test_header_declares_owner_column` 断言失败（表头无「来源」）

- [ ] **Step 3: 表头插入列**

`frontend/index.html`，在 `<th>部署时间</th>` 与 `<th>备注</th>` 之间插入：

```html
                      <th>来源</th>
```

- [ ] **Step 4: payload 解析透传 owner**

`frontend/aifin-shell.js`，在 `decodedScheme` 对象里 `name:` 之后加入：

```js
        owner: typeof scheme.owner === "string" ? scheme.owner : "",
```

- [ ] **Step 5: 合并对象透传 owner**

`frontend/aifin-shell.js` 中排行表使用的合并对象（含 `deploymentDate: formatDeploymentDate(scheme.deployedAt)` 的那个 `tasks[taskKey].push({...})`），在 `name: scheme.name,` 之后加入：

```js
        owner: scheme.owner || "",
```

- [ ] **Step 6: 行渲染新增单元格**

`frontend/aifin-shell.js` 的 `renderSchemeRankingRow`，在部署时间 `<td>` 之后、备注 `<td>` 之前插入：

```js
      '<td class="mono">' + escapeHtml(scheme.owner || "--") + '</td>' +
```

- [ ] **Step 7: 空态 colspan 同步**

`frontend/aifin-shell.js` 中排行表空态：

```js
      body.innerHTML = '<tr><td colspan="9" class="factor-empty-cell">该任务格子下暂无方案</td></tr>';
```

- [ ] **Step 8: 同步资源哈希**

Run:

```bash
/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python - <<'PY'
import hashlib, pathlib, re
js = pathlib.Path("frontend/aifin-shell.js")
digest = hashlib.sha256(js.read_bytes()).hexdigest()
index = pathlib.Path("frontend/index.html")
text = index.read_text(encoding="utf-8")
updated = re.sub(r'(src="aifin-shell\.js\?v=)[0-9a-f]{64}(")', r"\g<1>" + digest + r"\g<2>", text)
assert updated != text
index.write_text(updated, encoding="utf-8")
print("hash synced:", digest)
PY
```

- [ ] **Step 9: 运行测试确认通过**

Run: `/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/test_frontend_owner_column_contract.py tests/test_frontend_asset_versions.py -q`
Expected: PASS — `4 passed`

- [ ] **Step 10: JS 语法校验**

Run: `node --check frontend/aifin-shell.js`
Expected: 无输出（语法正确）

- [ ] **Step 11: 全量回归**

Run: `/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/ -q`
Expected: PASS，无新增失败。

- [ ] **Step 12: 提交**

```bash
git add frontend/index.html frontend/aifin-shell.js tests/test_frontend_owner_column_contract.py
git commit -m "feat(frontend): show scheme owner column in ranking table"
```

---

### Task 4: 真实数据端到端核对

**Files:**
- 无文件改动；仅验证

- [ ] **Step 1: 确认 dashboard 构建带出 owner 字段**

Run:

```bash
/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python - <<'PY'
import sys
sys.path.insert(0, "/Users/macstudio0/bond-factor-lab")
from backend.factor_lab_dashboard import build_factor_lab_dashboard
from shared.data_service import create_sqlalchemy_engine

engine = create_sqlalchemy_engine()
payload = build_factor_lab_dashboard(engine)
schemes = payload["schemes"]
print("scheme 总数:", len(schemes))
print("含 owner 字段:", sum(1 for s in schemes if "owner" in s))
print("已登记归属:", sum(1 for s in schemes if s.get("owner")))
print("样例:", [(s["scheme_id"], s["owner"]) for s in schemes[:3]])
engine.dispose()
PY
```

Expected: `scheme 总数 48`；`含 owner 字段 48`；映射文件为空时 `已登记归属 0`，样例 owner 均为 `""`。

- [ ] **Step 2: 确认 payload 校验通过**

Run:

```bash
/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/test_factor_lab_dashboard_api.py tests/test_dashboard_gate.py -q
```

Expected: PASS，无新增失败。

- [ ] **Step 3: rebase 到最新开发分支并重跑全量**

```bash
git fetch origin --prune
git rebase origin/codex/audit-bugfixes-20260613
/Users/macstudio0/miniconda3/bin/conda run --no-capture-output -n bond_factor_lab_service python -m pytest tests/ -q
```

Expected: PASS，无新增失败。

---

## 后续（本计划不含）

修订 `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`，要求交付 JSON 声明 owner，并在 intake 阶段登记进 `deploy/scheme_owner_v1.json`。该项独立设计与提交。
