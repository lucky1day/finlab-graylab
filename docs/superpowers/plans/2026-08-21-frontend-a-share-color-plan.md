# Frontend A-Share Color Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一任务格子、候选排行、逐月表现和每日明细的 A 股红绿灰颜色语义，并在 ECS 灰度页面完成真实浏览器验收。

**Architecture:** `frontend/aifin-shell.js` 保留一个基于原始数值的百分比分类函数，并增加独立的方向分类函数；三个百分比组件复用前者，结果状态保持独立。CSS 只在目标组件范围内映射现有红、绿、灰变量。完成 TDD、内容哈希更新和回归后，从精确提交构建一份 immutable release，先晋级 ECS，不触碰 Mac3。

**Tech Stack:** 原生 JavaScript、CSS、HTML、Node `vm` 测试、pytest、immutable source release、systemd、Codex in-app Browser。

---

### Task 1: 建立前端颜色契约测试

**Files:**
- Create: `tests/test_frontend_color_contract.py`
- Modify: `tests/test_frontend_asset_versions.py`

- [ ] **Step 1: 写 Node hook 边界测试**

新增 Node `vm` harness，调用 `window.__factorLabTestHooks`，断言：

```python
assert _call_hook("getMetricClassForTest", 59.9) == "metric-low"
assert _call_hook("getMetricClassForTest", 60.0) == "metric-high"
assert _call_hook("getMetricClassForTest", 60.1) == "metric-high"
assert _call_hook("getMetricClassForTest", None) == "metric-empty"
assert _call_hook("getDirectionClassForTest", "涨") == "direction-up"
assert _call_hook("getDirectionClassForTest", "跌") == "direction-down"
assert _call_hook("getDirectionClassForTest", "平") == "direction-neutral"
```

结果状态断言 `true -> is-correct/✓`、`false -> is-wrong/×`，以及 `null`、`?`、`--`、未知值均为 `is-neutral/?`。

- [ ] **Step 2: 写组件与 CSS 静态合同**

断言任务格子、排行和逐月表调用 `getMetricClass`；目标 CSS 的 high/low/empty 分别使用 `var(--negative)`、`var(--accent)`、`var(--text-tertiary)`；目标百分比组件不再出现 `metric-warn`，方向 up/down/neutral 颜色正确，趋势图五个固定颜色仍为：

```text
#15623f #2f7ba1 #b98728 #d62828 #6f5aa8
```

- [ ] **Step 3: 收紧 CSS 内容哈希测试**

扩展 `tests/test_frontend_asset_versions.py` 的 HTML parser，同时采集 stylesheet，要求：

```python
assert parser.stylesheets == [f"aifin-shell.css?v={stylesheet_hash}"]
assert parser.scripts == [f"aifin-shell.js?v={javascript_hash}"]
```

- [ ] **Step 4: 运行 RED**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest \
  tests/test_frontend_color_contract.py \
  tests/test_frontend_asset_versions.py -q
```

Expected: 新 hook/类名不存在且旧 asset hash 未更新，测试失败。

### Task 2: 最小实现公共颜色映射

**Files:**
- Modify: `frontend/aifin-shell.js`
- Modify: `frontend/aifin-shell.css`

- [ ] **Step 1: 实现百分比公共分类**

把 `getMetricClass` 改成只按原始值分类：

```javascript
function getMetricClass(value) {
  if (value === null || value === undefined || value === "") return "metric-empty";
  var numeric = Number(value);
  if (!Number.isFinite(numeric)) return "metric-empty";
  return numeric >= 60 ? "metric-high" : "metric-low";
}
```

任务格子的 `<span class="factor-task-top ...">`、排行三列和逐月五列全部调用这个函数；删除任务格子独立 `is-accuracy-highlighted` 判断。

- [ ] **Step 2: 实现方向和结果分类**

```javascript
function getDirectionClass(value) {
  if (value === "涨") return "direction-up";
  if (value === "跌") return "direction-down";
  return "direction-neutral";
}

function renderDailyResult(row) {
  if (row.correct !== true && row.correct !== false) {
    return '<span class="factor-result-dot is-neutral">?</span>';
  }
  return '<span class="factor-result-dot ' +
    (row.correct ? "is-correct" : "is-wrong") + '">' +
    (row.correct ? "✓" : "×") + "</span>";
}
```

预测方向和实际方向均调用 `getDirectionClass`；向测试 hooks 暴露 `getMetricClassForTest` 和 `getDirectionClassForTest`。

- [ ] **Step 3: 只在目标组件映射现有颜色**

```css
.factor-task-top.metric-high,
.factor-ranking-table .metric-high,
.factor-month-table .metric-high { color: var(--negative); }

.factor-task-top.metric-low,
.factor-ranking-table .metric-low,
.factor-month-table .metric-low { color: var(--accent); }

.factor-task-top.metric-empty,
.factor-ranking-table .metric-empty,
.factor-month-table .metric-empty { color: var(--text-tertiary); }

.factor-daily-table .direction-up { color: var(--negative); }
.factor-daily-table .direction-down { color: var(--accent); }
.factor-daily-table .direction-neutral { color: var(--text-tertiary); }
```

保留 `is-correct` 绿色、`is-wrong` 红色、`is-neutral` 灰色；不修改趋势图、普通字段或其它黄色 UI。

- [ ] **Step 4: 运行功能 GREEN**

Run Task 1 的定向测试。Expected: 除 asset hash 外颜色合同全部通过。

### Task 3: 更新静态资源版本并完成本地验证

**Files:**
- Modify: `frontend/index.html`

- [ ] **Step 1: 计算并写入内容哈希**

分别对 `aifin-shell.css` 和 `aifin-shell.js` 计算 SHA-256，把 `index.html` 的两个 `?v=` 更新为精确内容哈希。

- [ ] **Step 2: 运行完整前端测试**

```bash
conda run -n bond_factor_lab_service python -m pytest \
  tests/test_frontend_color_contract.py \
  tests/test_frontend_asset_versions.py \
  tests/test_frontend_ranking_contract.py \
  tests/test_frontend_live_divider_contract.py \
  tests/test_frontend_no_mock_financial_data.py \
  tests/test_frontend_owner_column_contract.py \
  tests/test_frontend_ranking_remark_detail_contract.py \
  tests/test_factor_lab_dashboard_api.py -q
```

Expected: 全部通过。

- [ ] **Step 3: 范围审计**

确认 diff 仅含 JS/CSS/index、两份相关测试和临时 spec/plan；指标公式、排序、筛选、API、后端、趋势图颜色没有 diff。

- [ ] **Step 4: 删除临时 spec/plan 并提交源码候选**

删除两份 `docs/superpowers` 文档并移除空目录；提交消息：

```text
fix(frontend): use A-share red-green metric colors
```

### Task 4: 全量回归与 R4 immutable release

**Files:** No additional source changes.

- [ ] **Step 1: 全量验证**

运行 full pytest、JavaScript syntax check、`git diff --check`、status clean。

- [ ] **Step 2: 推送 develop 并冻结 release**

只非 force 推送 `codex/develop`；从精确源码提交双构建 deterministic archive，创建 annotated tag：

```text
bfl-source-r4-frontend-colors-20260821
```

- [ ] **Step 3: ECS 预安装与 CAS**

复用现有 immutable installer：严格 preflight，writer idle/timer 有余量，no-activate 预安装并验证；随后 CAS `current`、只重启 Backend。不得替换 unit、daemon-reload、写 DB 或触碰 Mac3。

- [ ] **Step 4: 激活后读回**

验证新 current/previous、Backend cwd/env/health、五 timer、五 writer、11 unit、数据库关键表/migration/Registry/binlog均未发生非预期变化，并保留可执行回滚材料。

### Task 5: ECS 浏览器验收与文档收口

**Files:**
- Modify after deployment: `AGENTS.md`
- Modify after deployment: `CLAUDE.md`
- Modify after deployment: `docs/CURRENT_STATUS.md`
- Modify after deployment: `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md`

- [ ] **Step 1: 建立只读 ECS loopback 浏览入口**

使用临时 SSH local forward 打开 ECS `127.0.0.1:8100`，不开放公网端口。

- [ ] **Step 2: 实际页面验收**

依次检查任务格子、排行、逐月、每日明细方向、结果状态、边界 hook、无黄色百分比、普通字段和趋势图未改。保存浏览器截图或 DOM/computed-style 证据。

- [ ] **Step 3: 更新长期事实并清理临时物料**

记录 ECS 新 current/previous/tag 和浏览器验收结果；Mac3、生产域名、DB authority、Writer保持不变。清理本地 build、临时 tunnel 和 inbound；不删除 release/rollback。

- [ ] **Step 4: 最终提交、推送和验证**

提交长期文档，非 force 推送 develop；再次运行定向测试、远端 ref/tag、双主机只读状态和工作树清洁检查。
