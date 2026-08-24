# Monthly Average June Gap and Detail Format Implementation Plan

> **状态：已完成并完成跨主机闭环。** ECS 已补齐五条目标月 2026/06 Prediction；前端最终使用预测日 `MM/DD`、目标月 `YYYY/MM`。同一 exact release `053d562fc40d7ecf5596f56f1beb00e4a3b58178` 后续只晋级 Mac3 一次，Mac3 本地 run `3685–3689` 补齐相同五条 Prediction，并从本地权威日频数据生成到期 Actual。ECS 与公网 Mac3 的 M0 live 业务键和值最终零差异。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 ECS 为五个月均方案补齐目标月 `2026-06` 的 `gray_live` 记录，并把月均预测明细中的预测日和目标月分别显示为 `MM/DD`、`YYYY/MM`。

**Architecture:** 保留 Contract 内部 `target_date` 和现有数据库业务键，仅在 signal-gap live-scope 判断中为 `monthly_average` 计算下一自然月，使既有受控 gap-fill 能识别 `2026-05-15` 信号。前端保留 ISO 原始值用于排序和分组，只在月均抽屉 HTML 渲染边界格式化。发布继续使用确定性 immutable archive，业务写入仅经现有 `signal-gap-fill` 和 `scheduler.repository`。

**Tech Stack:** Python 3.12、pytest、原生 JavaScript、Node.js 测试 harness、FastAPI 静态前端、MySQL 8、ECS systemd、immutable release scripts。

---

### Task 1: 修正月均 signal-gap live scope

**Files:**
- Modify: `harness/signal_gap_plan.py:220-265`
- Modify: `tests/test_signal_gap_plan.py:350-390`

- [ ] **Step 1: 写月均边界失败测试**

在 `tests/test_signal_gap_plan.py` 增加精确 case 构造和边界断言：

```python
def _monthly_average_case(
    *, target_date: str = "2026-05-16"
) -> signal_gap_plan.ExpectedSignalCase:
    return signal_gap_plan.ExpectedSignalCase(
        registry_scheme_id="monthly_avg__h1__5Y",
        base_scheme_id="monthly_avg",
        runtime_type="blackbox_v2",
        frequency="monthly",
        task_type="monthly_average",
        target_tenor="5Y",
        horizon=1,
        predict_date="2026-05-15",
        feature_date="2026-05-15",
        target_date=target_date,
    )


def test_monthly_average_live_scope_uses_display_target_month() -> None:
    assert signal_gap_plan._case_is_in_platform_live_scope(
        _monthly_average_case()
    )


def test_non_monthly_live_scope_keeps_raw_target_date_boundary() -> None:
    case = replace(
        _monthly_average_case(),
        task_type="quarterly_average",
    )
    assert not signal_gap_plan._case_is_in_platform_live_scope(case)
```

- [ ] **Step 2: 运行测试并确认失败原因**

Run:

```bash
pytest -q tests/test_signal_gap_plan.py::test_monthly_average_live_scope_uses_display_target_month tests/test_signal_gap_plan.py::test_non_monthly_live_scope_keeps_raw_target_date_boundary
```

Expected: FAIL，提示 `harness.signal_gap_plan` 尚无 `_case_is_in_platform_live_scope`。

- [ ] **Step 3: 实现最小任务感知边界函数**

在 `harness/signal_gap_plan.py` 增加：

```python
def _case_is_in_platform_live_scope(case: ExpectedSignalCase) -> bool:
    if case.task_type != "monthly_average":
        return case.target_date >= PLATFORM_LIVE_TARGET_START_DATE
    pointer = date.fromisoformat(case.target_date)
    if pointer.month == 12:
        target_month = f"{pointer.year + 1:04d}-01"
    else:
        target_month = f"{pointer.year:04d}-{pointer.month + 1:02d}"
    return target_month >= PLATFORM_LIVE_TARGET_START_DATE[:7]
```

将 `read_signal_gap_snapshot` 中：

```python
if case.target_date >= PLATFORM_LIVE_TARGET_START_DATE:
```

替换为：

```python
if _case_is_in_platform_live_scope(case):
```

- [ ] **Step 4: 运行 signal-gap 相关测试**

Run:

```bash
pytest -q tests/test_signal_gap_plan.py tests/test_signal_gap_fill.py tests/test_signal_gap_fill_cli.py
```

Expected: 全部 PASS；月均 `2026-05-16` 进入 live scope，非月均原行为不变。

- [ ] **Step 5: 提交 gap 修复**

```bash
git status --short
git add harness/signal_gap_plan.py tests/test_signal_gap_plan.py
git diff --cached --check
git commit -m "fix: include first monthly average live month"
```

Expected: 提交仅包含上述两个文件。

### Task 2: 修改月均预测明细日期格式

**Files:**
- Modify: `frontend/aifin-shell.js:271-290`
- Modify: `frontend/aifin-shell.js:2675-2690`
- Modify: `frontend/aifin-shell.js:2940-2998`
- Modify: `frontend/index.html`（仅 content hash query）
- Modify: `tests/test_monthly_average_frontend_target_month.py:75-110`

- [ ] **Step 1: 写展示格式失败测试**

在 `tests/test_monthly_average_frontend_target_month.py` 增加：

```python
def test_monthly_average_detail_formats_predict_date_and_target_month() -> None:
    assert _call_hook(
        "formatMonthlyAveragePredictDate", "2026-06-15"
    ) == "06/15"
    assert _call_hook(
        "formatMonthlyAverageTargetMonth", "2025-02"
    ) == "2025/02"


@pytest.mark.parametrize("value", ["", "2026-6-15", "2026-02-30", None])
def test_monthly_average_predict_date_format_rejects_invalid_values(
    value: str | None,
) -> None:
    assert "ISO date" in _call_hook_error(
        "formatMonthlyAveragePredictDate", value
    )


@pytest.mark.parametrize("value", ["", "2025-2", "2025-13", None])
def test_monthly_average_target_month_format_rejects_invalid_values(
    value: str | None,
) -> None:
    assert "target month" in _call_hook_error(
        "formatMonthlyAverageTargetMonth", value
    )
```

在 `test_static_monthly_average_copy_has_no_daily_detail_label` 增加静态渲染断言，确保月均分支调用两个 formatter，而非修改视图模型原始值：

```python
assert "formatMonthlyAveragePredictDate(row.predictDate)" in javascript
assert (
    "formatMonthlyAverageTargetMonth(row.targetMonth || month)"
    in javascript
)
```

- [ ] **Step 2: 运行测试并确认失败原因**

Run:

```bash
pytest -q tests/test_monthly_average_frontend_target_month.py
```

Expected: 新用例 FAIL，提示缺少两个 formatter hook；已有目标月映射用例继续 PASS。

- [ ] **Step 3: 实现纯展示 formatter**

在 `frontend/aifin-shell.js` 的目标月 helper 附近增加：

```javascript
function formatMonthlyAveragePredictDate(predictDate) {
  var value = requireDashboardIsoDate(
    predictDate,
    "monthly_average predict_date"
  );
  return value.slice(5, 7) + "/" + value.slice(8, 10);
}

function formatMonthlyAverageTargetMonth(targetMonth) {
  var value = String(targetMonth || "").trim();
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(value)) {
    throw dashboardDataError(
      "monthly_average target month must use YYYY-MM"
    );
  }
  return value.slice(0, 4) + "/" + value.slice(5, 7);
}
```

月均明细渲染改为：

```javascript
html += '<td class="mono">' + escapeHtml(
  formatMonthlyAveragePredictDate(row.predictDate)
) + '</td>';
html += '<td class="mono">' + escapeHtml(
  formatMonthlyAverageTargetMonth(row.targetMonth || month)
) + '</td>';
```

在 `window.__factorLabTestHooks` 暴露两个 formatter。不得修改 `row.predictDate`、`row.targetMonth`、主表月份或抽屉标题。

- [ ] **Step 4: 更新静态资源 content hash 并运行前端测试**

计算 `frontend/aifin-shell.js` SHA-256，并把 `frontend/index.html` 中对应 query 更新为新摘要。然后运行：

```bash
pytest -q \
  tests/test_monthly_average_frontend_target_month.py \
  tests/test_frontend_*.py \
  tests/test_dashboard_*.py \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_period_average_dashboard.py
```

Expected: 全部 PASS；视图模型仍保存 ISO 原始值，月均明细 formatter 返回 `06/15` 和 `2025/02`。

- [ ] **Step 5: 提交前端格式修复**

```bash
git status --short
git add frontend/aifin-shell.js frontend/index.html tests/test_monthly_average_frontend_target_month.py
git diff --cached --check
git commit -m "fix: format monthly average detail dates"
```

Expected: 提交仅包含前端、hash query 和对应测试。

### Task 3: 本地完整验证与 immutable release

**Files:**
- No source changes expected

- [ ] **Step 1: 运行完整测试套件**

```bash
pytest -q
```

Expected: 全部 PASS；不得以只跑 focused tests 代替完整验证。

- [ ] **Step 2: 核对工作树和精确提交**

```bash
git status --short
git log -5 --oneline --decorate
git diff origin/codex/develop...HEAD --stat
```

Expected: 仅保留用户已有的未跟踪计划文档；source changes 已提交，`master` 未移动。

- [ ] **Step 3: 构建两次确定性 archive**

使用 `scripts/build_source_release.py` 从精确 HEAD 在干净临时 worktree 构建两次：

```bash
BFL_RELEASE_ID=$(git rev-parse HEAD)
BFL_BUILD_ROOT=$(mktemp -d)
git worktree add --detach "$BFL_BUILD_ROOT/worktree" "$BFL_RELEASE_ID"
python -B "$BFL_BUILD_ROOT/worktree/scripts/build_source_release.py" \
  --project-root "$BFL_BUILD_ROOT/worktree" \
  --output-dir "$BFL_BUILD_ROOT/build-1"
python -B "$BFL_BUILD_ROOT/worktree/scripts/build_source_release.py" \
  --project-root "$BFL_BUILD_ROOT/worktree" \
  --output-dir "$BFL_BUILD_ROOT/build-2"
shasum -a 256 "$BFL_BUILD_ROOT"/build-{1,2}/*.source.tar.gz
shasum -a 256 "$BFL_BUILD_ROOT"/build-{1,2}/*.manifest.json
```

记录并比较：

```text
release_id = exact git SHA
archive_sha256_1 == archive_sha256_2
manifest_sha256_1 == manifest_sha256_2
```

Expected: 两次 archive 与 manifest 摘要逐字节一致；archive 不包含 `.git`、`outputs/` 或未跟踪计划。

- [ ] **Step 4: ECS 只读 preflight 与安装验证**

通过 `root@47.103.45.193` 和 `/Users/macstudio0/.ssh/finlab-key.pem`：

1. 读取当前 `current`、`previous`、backend、五个 timer 和数据库身份。
2. 上传 archive、manifest 及候选版本的 `install_source_release.py`、`build_source_release.py` 到仅用于本次发布的 `/var/tmp/bfl-release-$BFL_RELEASE_ID`。
3. 核对 release manifest、安装后 source-tree SHA-256 和 targeted tests。

预安装命令必须使用候选安装器和 ECS 固定路径：

```bash
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B \
  /var/tmp/bfl-release-$BFL_RELEASE_ID/install_source_release.py \
  --manifest /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.manifest.json \
  --archive /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.source.tar.gz \
  --expected-archive-sha256 "$BFL_ARCHIVE_SHA256" \
  --deploy-root /opt/bond-factor-lab \
  --runtime-root /var/lib/bond-factor-lab/state
```

Expected: 不修改 installed unit/timer；数据库身份仍为 ECS 本地 authority。

- [ ] **Step 5: 晋级 ECS 并验证服务**

使用同一候选安装器原子更新 ECS `current`/`previous`：

```bash
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B \
  /var/tmp/bfl-release-$BFL_RELEASE_ID/install_source_release.py \
  --manifest /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.manifest.json \
  --archive /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.source.tar.gz \
  --expected-archive-sha256 "$BFL_ARCHIVE_SHA256" \
  --deploy-root /opt/bond-factor-lab \
  --runtime-root /var/lib/bond-factor-lab/state \
  --activate \
  --expected-current "$BFL_EXPECTED_CURRENT"
```

然后仅执行：

```bash
systemctl restart bond-factor-lab-backend.service
```

Expected: backend active；root、health、dashboard、HTML、JS、CSS HTTP 200；五个 timer 仍为 enabled/active/waiting。

### Task 4: 五个方案逐条受控补数

**Files:**
- Runtime reports only: external ECS runtime report root

- [ ] **Step 1: 对五个业务键做写前只读核对**

逐方案核对：

```text
scheme_id = m0_monthly_avg_mid_{1y,3y,5y,7y,10y}_v1
target_tenor = {1Y,3Y,5Y,7Y,10Y}
horizon = 1
predict_date = 2026-05-15
feature_date = 2026-05-15
target_date = 2026-05-16
prediction count = 0
active exact version = config exact version
Registry status = active
```

Expected: 五个目标业务键均不存在；任一键存在或身份漂移则停止写入。

- [ ] **Step 2: 逐方案运行只读 gap plan**

在 ECS 加载与 backend 相同的 release 环境，并对五个 exact ID 分别运行：

```bash
set -a
. /etc/bond-factor-lab/bond-factor-lab.env
. /opt/bond-factor-lab/current/.bfl-release.env
set +a
cd /opt/bond-factor-lab/current
for BFL_SCHEME_ID in \
  m0_monthly_avg_mid_1y_v1 \
  m0_monthly_avg_mid_3y_v1 \
  m0_monthly_avg_mid_5y_v1 \
  m0_monthly_avg_mid_7y_v1 \
  m0_monthly_avg_mid_10y_v1
do
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
    signal-gap-plan \
    --predict-date 2026-05-15 \
    --scheme-id "$BFL_SCHEME_ID"
done
```

Expected: 每个计划均为 `READY`，`expected=1`、`actionable=1`、`blocked=0`、action 为 `GRAY_LIVE_GAP`。

- [ ] **Step 3: 逐方案运行受控 gap fill**

按 `1Y → 3Y → 5Y → 7Y → 10Y` 顺序，每次只执行；每个命令成功并完成读回后才能执行下一个：

```bash
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
  signal-gap-fill \
  --predict-date 2026-05-15 \
  --scheme-id m0_monthly_avg_mid_1y_v1 \
  --project-root /opt/bond-factor-lab/current

/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
  signal-gap-fill \
  --predict-date 2026-05-15 \
  --scheme-id m0_monthly_avg_mid_3y_v1 \
  --project-root /opt/bond-factor-lab/current

/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
  signal-gap-fill \
  --predict-date 2026-05-15 \
  --scheme-id m0_monthly_avg_mid_5y_v1 \
  --project-root /opt/bond-factor-lab/current

/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
  signal-gap-fill \
  --predict-date 2026-05-15 \
  --scheme-id m0_monthly_avg_mid_7y_v1 \
  --project-root /opt/bond-factor-lab/current

/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
  signal-gap-fill \
  --predict-date 2026-05-15 \
  --scheme-id m0_monthly_avg_mid_10y_v1 \
  --project-root /opt/bond-factor-lab/current
```

Expected: 每次 `status=PASSED`、`records_written=1`。每次完成后立即只读核验；任一次失败即停止，不执行后续 scheme。

- [ ] **Step 4: 写后数据库和 API 验收**

逐方案确认：

```text
prediction_phase = gray_live
predict_date = feature_date = 2026-05-15
target_date = 2026-05-16
run.status = success
run.records_written = 1
scheme_version = active exact version
```

再读取 Dashboard，确认五个 scheme 的业务月份序列均增加且只增加 `2026-06`，无重复目标月。

### Task 5: 浏览器最终验收

**Files:**
- No source changes expected

- [ ] **Step 1: 打开 ECS loopback 前端**

保持 SSH local forward：

```text
127.0.0.1:18110 -> ECS 127.0.0.1:8100
```

在应用内浏览器打开 `http://127.0.0.1:18110/`。

- [ ] **Step 2: 逐个检查五个月均期限**

Expected for every tenor:

```text
月份序列包含 2026-06
2026-06 抽屉行的前两列 = 05/15 | 2026/06
该行预测方向等于 gap-fill 算法返回值，实际方向和结果按当前 Actual 状态显示
原 predict_date 2026-06-15 显示为 06/15
```

- [ ] **Step 3: 核对浏览器和服务无回归**

Expected: 控制台无 warning/error；Dashboard schema 正常；83 个 active composite 格子不丢失；backend、health 和五个 timer 状态不变。

- [ ] **Step 4: 汇报精确证据**

最终报告必须列出：代码提交、release ID、archive/manifest/source-tree SHA-256、ECS current/previous、五个新 prediction/run 读回、测试结果、HTTP 状态和浏览器显示结果，同时明确未修改 Actuals、Registry、timer、Nginx、DNS、Mac3 或 `master`。
