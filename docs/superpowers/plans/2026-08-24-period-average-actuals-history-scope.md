# Period-Average Actuals History Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让自然 Actuals 刷新只计算 `target_date >= 2025-01-01` 的周期均值 Actuals，并在 ECS 补齐 2026/06、2026/07 五个期限共 10 条月均 Actual。

**Architecture:** 保留 `shared.actual_facts` 的通用范围参数和严格桶完整性校验，只在 `scheduler.actuals_runner` 的自然调度调用边界传入平台既有历史政策起点。发布使用精确提交构建的确定性 immutable archive；业务写入仅由已安装的 Actuals one-shot 经 repository 完成。

**Tech Stack:** Python 3.12、unittest/pytest、SQLAlchemy、MySQL 8.0、systemd one-shot、原生 FastAPI 前端

---

## 文件结构

- Modify: `scheduler/actuals_runner.py` — 为自然周期均值 Actuals 刷新绑定 `2025-01-01` 起点。
- Modify: `tests/test_calendar_coverage_fail_closed.py` — 锁定交易日、非交易日和未覆盖日期下的调用边界。
- Modify: `tests/test_period_average_actuals.py` — 锁定起点以前缺口忽略、起点以内缺口仍 fail-closed。
- Create: `docs/superpowers/plans/2026-08-24-period-average-actuals-history-scope.md` — 本实施计划。

## Task 1：先锁定自然 Actuals 调用参数

**Files:**
- Modify: `tests/test_calendar_coverage_fail_closed.py:304-338`
- Test: `tests/test_calendar_coverage_fail_closed.py`

- [ ] **Step 1：把非交易日测试改成精确断言周期均值起止范围**

将现有只检查 `end_date` 的断言替换为：

```python
period.assert_called_once_with(
    start_date="2025-01-01",
    end_date=COVERED_HOLIDAY,
)
```

- [ ] **Step 2：把交易日测试改成精确断言周期均值起止范围**

将现有只检查 `end_date` 的断言替换为：

```python
period.assert_called_once_with(
    start_date="2025-01-01",
    end_date=COVERED_TRADING,
)
```

- [ ] **Step 3：运行测试并确认先失败**

Run:

```bash
pytest -q \
  tests/test_calendar_coverage_fail_closed.py::ActualsRunnerCoverageTests::test_covered_holiday_still_rolls_back_to_previous_trading_day \
  tests/test_calendar_coverage_fail_closed.py::ActualsRunnerCoverageTests::test_covered_trading_day_uses_run_date
```

Expected: 两个测试因实际调用只有 `end_date`、缺少 `start_date` 而失败。

## Task 2：在自然调度边界传入平台历史起点

**Files:**
- Modify: `scheduler/actuals_runner.py:18-20`
- Modify: `scheduler/actuals_runner.py:77-80`
- Test: `tests/test_calendar_coverage_fail_closed.py`

- [ ] **Step 1：定义周期均值 Actuals 自然刷新起点**

在时区常量旁增加：

```python
PERIOD_AVERAGE_ACTUALS_START_DATE = "2025-01-01"
```

- [ ] **Step 2：只修改周期均值 Actuals 调用**

将调用改为：

```python
period_average_written = update_period_average_actuals(
    start_date=PERIOD_AVERAGE_ACTUALS_START_DATE,
    end_date=target_date,
)
```

日频、周频和普通月频调用保持原样。

- [ ] **Step 3：重新运行 Task 1 测试**

Run:

```bash
pytest -q \
  tests/test_calendar_coverage_fail_closed.py::ActualsRunnerCoverageTests::test_covered_holiday_still_rolls_back_to_previous_trading_day \
  tests/test_calendar_coverage_fail_closed.py::ActualsRunnerCoverageTests::test_covered_trading_day_uses_run_date
```

Expected: `2 passed`。

- [ ] **Step 4：运行 Actuals 入口完整覆盖测试**

Run:

```bash
pytest -q tests/test_calendar_coverage_fail_closed.py
```

Expected: 文件内全部测试通过；未覆盖日仍在任何 updater 调用前 fail-closed。

## Task 3：锁定范围外缺口不会阻断、范围内缺口仍会阻断

**Files:**
- Modify: `tests/test_period_average_actuals.py:70-104`
- Test: `tests/test_period_average_actuals.py`

- [ ] **Step 1：增加范围外缺口回归测试**

增加：

```python
def test_period_average_actual_ignores_missing_bucket_before_start_date() -> None:
    calendar = _calendar_rows("2024-01-01", "2025-06-30")
    complete = _yield_rows(calendar, lambda _day: 2.0)
    rows = [item for item in complete if item["trade_date"] != "2024-02-01"]

    records = build_period_average_actual_records_from_rows(
        rows,
        calendar,
        task_types=("quarterly_average",),
        start_date="2025-01-01",
    )

    assert [item.target_date for item in records] == ["2025-04-01"]
```

该断言证明 2024 年缺口不影响 2025 年政策范围；第一条可完成的 2025 年季度 Actual
使用 feature 桶锚点 `2025-03-31` 的下一自然日指针 `2025-04-01`。

- [ ] **Step 2：增加范围内缺口回归测试**

增加：

```python
def test_period_average_actual_rejects_missing_bucket_after_start_date() -> None:
    calendar = _calendar_rows("2024-01-01", "2025-06-30")
    complete = _yield_rows(calendar, lambda _day: 2.0)
    rows = [item for item in complete if item["trade_date"] != "2025-02-03"]

    with pytest.raises(ValueError, match="missing trading dates"):
        build_period_average_actual_records_from_rows(
            rows,
            calendar,
            task_types=("quarterly_average",),
            start_date="2025-01-01",
        )
```

- [ ] **Step 3：运行周期均值 Actuals 测试**

Run:

```bash
pytest -q tests/test_period_average_actuals.py
```

Expected: 全部通过；范围过滤和严格完整性同时成立。

## Task 4：本地回归、提交与确定性 release

**Files:**
- Modify: `scheduler/actuals_runner.py`
- Modify: `tests/test_calendar_coverage_fail_closed.py`
- Modify: `tests/test_period_average_actuals.py`

- [ ] **Step 1：运行聚焦回归**

Run:

```bash
pytest -q \
  tests/test_calendar_coverage_fail_closed.py \
  tests/test_period_average_actuals.py \
  tests/test_period_average_requests.py \
  tests/test_period_average_dashboard.py \
  tests/test_scheme_metrics_actual_join.py
```

Expected: 全部通过。

- [ ] **Step 2：运行完整测试套件**

Run:

```bash
pytest -q
```

Expected: 全部通过，无新增 warning 或失败。

- [ ] **Step 3：检查改动边界并提交**

Run:

```bash
git diff --check
git status --short
git diff -- scheduler/actuals_runner.py \
  tests/test_calendar_coverage_fail_closed.py \
  tests/test_period_average_actuals.py
```

Expected: 只包含本计划的调用参数和测试；两份用户未跟踪计划文件不被暂存。

Commit:

```bash
git add scheduler/actuals_runner.py \
  tests/test_calendar_coverage_fail_closed.py \
  tests/test_period_average_actuals.py
git commit -m "fix: bound period-average actuals history"
```

- [ ] **Step 4：从精确提交构建两次确定性 archive**

在临时目录为精确 HEAD 创建 detached worktree，再构建两次：

```bash
BFL_BUILD_ROOT=$(mktemp -d /tmp/bfl-period-actuals-release.XXXXXX)
BFL_RELEASE_ID=$(git rev-parse HEAD)
git worktree add --detach "$BFL_BUILD_ROOT/worktree" "$BFL_RELEASE_ID"
python -B "$BFL_BUILD_ROOT/worktree/scripts/build_source_release.py" \
  --project-root "$BFL_BUILD_ROOT/worktree" \
  --output-dir "$BFL_BUILD_ROOT/build-1"
python -B "$BFL_BUILD_ROOT/worktree/scripts/build_source_release.py" \
  --project-root "$BFL_BUILD_ROOT/worktree" \
  --output-dir "$BFL_BUILD_ROOT/build-2"
shasum -a 256 "$BFL_BUILD_ROOT"/build-1/* "$BFL_BUILD_ROOT"/build-2/*
```

Expected: 两次 archive 摘要相同，两次 manifest 摘要相同；archive 不含 `.git`、
`outputs/` 或未跟踪文件。保留 release ID、archive SHA-256、manifest SHA-256。

## Task 5：ECS 候选验证、激活与受控 Actuals 刷新

**Files:**
- No repository file changes.
- ECS immutable release root: `/opt/bond-factor-lab/releases/$BFL_RELEASE_ID`
- ECS runtime state: `/var/lib/bond-factor-lab/state`

- [ ] **Step 1：上传候选 archive、manifest 和同版本安装器**

先从本地构建输出记录并导出：

```bash
BFL_RELEASE_ID=$(git rev-parse HEAD)
BFL_ARCHIVE_SHA256=$(shasum -a 256 \
  "$BFL_BUILD_ROOT/build-1/$BFL_RELEASE_ID.source.tar.gz" | awk '{print $1}')
```

将 `build-1` 的 archive、manifest 以及候选提交中的
`scripts/install_source_release.py`、`scripts/build_source_release.py` 上传到：

```text
/var/tmp/bfl-release-$BFL_RELEASE_ID/
```

Expected: ECS 文件 SHA-256 与本地记录完全一致。

- [ ] **Step 2：预安装但不激活**

使用候选版本安装器执行：

```bash
/opt/miniconda3/envs/bond_factor_lab_service/bin/python \
  /var/tmp/bfl-release-$BFL_RELEASE_ID/install_source_release.py \
  --archive /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.source.tar.gz \
  --manifest /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.manifest.json \
  --expected-archive-sha256 "$BFL_ARCHIVE_SHA256" \
  --deploy-root /opt/bond-factor-lab \
  --runtime-root /var/lib/bond-factor-lab/state
```

Expected: 安装器返回预安装成功、installed source-tree SHA-256 与 archive tree 一致，
`current` 尚未改变。

- [ ] **Step 3：在候选 release 上运行测试和只读 10 条构建验证**

使用候选 release、ECS service env 和外置 pytest cache 运行 Task 4 聚焦测试。随后只读调用：

```python
build_period_average_actual_records(
    engine,
    task_types=("monthly_average",),
    start_date="2026-05-16",
    end_date="2026-07-15",
    tenors=("1Y", "3Y", "5Y", "7Y", "10Y"),
)
```

Expected: 恰好返回 10 条；目标指针覆盖前端目标月 2026/06 和 2026/07，每期限各
2 条；此步骤不写库。

- [ ] **Step 4：激活候选 release**

再次调用同版本安装器，追加：

```bash
BFL_PREVIOUS_COMMIT=$(basename "$(readlink -f /opt/bond-factor-lab/current)")
/opt/miniconda3/envs/bond_factor_lab_service/bin/python \
  /var/tmp/bfl-release-$BFL_RELEASE_ID/install_source_release.py \
  --archive /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.source.tar.gz \
  --manifest /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.manifest.json \
  --expected-archive-sha256 "$BFL_ARCHIVE_SHA256" \
  --deploy-root /opt/bond-factor-lab \
  --runtime-root /var/lib/bond-factor-lab/state \
  --activate \
  --expected-current "$BFL_PREVIOUS_COMMIT"
```

其他 identity 和 SHA 参数完全相同。

Expected: `current` 指向新 release，`previous` 指向修复前 release，manifest 与
source-tree 校验通过。

- [ ] **Step 5：运行已安装 Actuals one-shot**

Run:

```bash
systemctl start bond-factor-lab-actuals.service
systemctl show bond-factor-lab-actuals.service \
  -p Result -p ExecMainStatus -p ActiveState -p SubState
journalctl -u bond-factor-lab-actuals.service -n 80 --no-pager
```

Expected: `Result=success`、`ExecMainStatus=0`，日志包含非零
`period_average_records`；不替换 unit/timer，不改变 timer 触发。

- [ ] **Step 6：只读核对 10 条 Actuals 和 Dashboard**

查询 `t_scheme_period_average_actuals` 中月均规则、五个期限、原始目标指针对应
2026/06 与 2026/07 的记录，并请求：

```text
GET http://127.0.0.1:8100/api/factor-lab/dashboard
```

Expected:

- 五个期限各 2 条，共 10 条；方向分别为：
  - 1Y：2026/06 `+1`，2026/07 `-1`
  - 3Y：2026/06 `-1`，2026/07 `+1`
  - 5Y：2026/06 `-1`，2026/07 `-1`
  - 7Y：2026/06 `-1`，2026/07 `-1`
  - 10Y：2026/06 `-1`，2026/07 `+1`
- Dashboard 对应 10 条不再是“待验证”。
- `bond-factor-lab-actuals.timer` 仍为 `enabled/active/waiting`。
- 其他四个 ECS timer、Registry、Nginx、DNS、Mac3、`master` 均未改变。

- [ ] **Step 7：通过现有 SSH tunnel 交付用户 review**

确认 `127.0.0.1:18110` tunnel 仍连到 ECS `127.0.0.1:8100`。让用户刷新现有
浏览器标签页 review；若应用浏览器安全策略阻止自动刷新，不尝试其他浏览器绕过。

## 最终交付记录

最终报告必须包含：设计提交、实施计划提交、代码提交、release ID、archive/
manifest/source-tree SHA-256、ECS current/previous、测试结果、Actuals one-shot 状态、
10 条月均 Actuals 读回和 Dashboard 状态；同时明确未修改历史源数据、日历、
systemd unit/timer、Registry、Nginx、DNS、Mac3、`master` 或两份用户未跟踪计划。
