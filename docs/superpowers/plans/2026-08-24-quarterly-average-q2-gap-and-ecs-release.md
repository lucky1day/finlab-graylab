# 季均 2026/Q2 缺口与 ECS 单次发布 Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** 让季度均值 `2026/Q2` 的 raw target pointer `2026-04-01` 进入平台受控 live gap 范围，补齐五个期限各一条预测，并与已存在的季度 Actual join；同时把已完成的季度前端修改和 gap-scope 修改通过同一个 immutable release 晋级 ECS。

**Architecture:** 普通任务继续按 raw `target_date >= 2026-06-01` 判断 live scope；月均继续按下一自然月身份判断；季均仅把两端日期映射为 `(year, quarter)` 后比较。预测构建、算法、repository、Actuals、API schema 和原始业务键保持不变。发布从精确提交构建两次确定性 archive，只做一次 candidate 验收、一次 ECS 激活、一次 backend restart，随后逐方案执行既有 insert-only `signal-gap-fill`。

**Tech Stack:** Python 3.12、pytest、FastAPI 静态前端、MySQL 8、ECS systemd、immutable source release scripts。

---

### Task 1: 用失败测试锁定季度 live-scope 边界

**Files:**
- Modify: `tests/test_signal_gap_plan.py`
- Test: `tests/test_signal_gap_plan.py`

**Step 1: 增加季度 case helper**

```python
def _quarterly_average_case(
    *, target_date: str = "2026-04-01"
) -> signal_gap_plan.ExpectedSignalCase:
    return signal_gap_plan.ExpectedSignalCase(
        registry_scheme_id="quarterly_avg__h1__5Y",
        base_scheme_id="quarterly_avg",
        runtime_type="blackbox_v2",
        frequency="quarterly",
        task_type="quarterly_average",
        target_tenor="5Y",
        horizon=1,
        predict_date="2026-03-31",
        feature_date="2026-03-31",
        target_date=target_date,
    )
```

**Step 2: 把旧的非月均断言拆成季度与普通任务三条边界测试**

```python
def test_quarterly_average_live_scope_uses_target_quarter_identity() -> None:
    assert signal_gap_plan._case_is_in_platform_live_scope(
        _quarterly_average_case(target_date="2026-04-01")
    )


def test_quarterly_average_live_scope_rejects_prior_quarter() -> None:
    assert not signal_gap_plan._case_is_in_platform_live_scope(
        _quarterly_average_case(target_date="2026-01-01")
    )


def test_ordinary_live_scope_keeps_raw_target_date_boundary() -> None:
    case = replace(
        _monthly_average_case(),
        task_type="T+5",
        frequency="daily",
    )
    assert not signal_gap_plan._case_is_in_platform_live_scope(case)
```

**Step 3: 运行测试并确认红灯**

Run:

```bash
PYTHONPATH=. conda run -n bond_factor_lab_service \
  pytest -q tests/test_signal_gap_plan.py
```

Expected: 新的 `2026-04-01` 季均测试 FAIL；月均、`2026/Q1` 和普通任务测试 PASS。

### Task 2: 实现季度身份比较并完成本地验证

**Files:**
- Modify: `harness/signal_gap_plan.py`
- Modify: `tests/test_signal_gap_plan.py`
- Test: `tests/test_signal_gap_plan.py`
- Test: `tests/test_quarterly_average_frontend.py`

**Step 1: 在现有 live-scope 函数中增加最小季度分支**

```python
def _case_is_in_platform_live_scope(case: ExpectedSignalCase) -> bool:
    """按任务的业务目标身份判断是否进入平台实盘区间。"""
    if case.task_type == "quarterly_average":
        pointer = date.fromisoformat(case.target_date)
        live_start = date.fromisoformat(PLATFORM_LIVE_TARGET_START_DATE)
        target_quarter = (pointer.year, (pointer.month - 1) // 3 + 1)
        live_start_quarter = (
            live_start.year,
            (live_start.month - 1) // 3 + 1,
        )
        return target_quarter >= live_start_quarter
    if case.task_type != "monthly_average":
        return case.target_date >= PLATFORM_LIVE_TARGET_START_DATE
    pointer = date.fromisoformat(case.target_date)
    if pointer.month == 12:
        target_month = f"{pointer.year + 1:04d}-01"
    else:
        target_month = f"{pointer.year:04d}-{pointer.month + 1:02d}"
    return target_month >= PLATFORM_LIVE_TARGET_START_DATE[:7]
```

不提取通用 period abstraction；本次只有季度边界需要新增语义。

**Step 2: 运行聚焦测试**

Run:

```bash
PYTHONPATH=. conda run -n bond_factor_lab_service pytest -q \
  tests/test_signal_gap_plan.py \
  tests/test_signal_gap_fill.py \
  tests/test_quarterly_average_frontend.py \
  tests/test_period_average_requests.py
```

Expected: PASS。

**Step 3: 运行全量测试**

Run:

```bash
PYTHONPATH=. conda run -n bond_factor_lab_service pytest -q
```

Expected: 全部 PASS；不得用 focused tests 代替。

**Step 4: 审查并提交 gap-scope 代码**

```bash
git status --short
git branch --list 'codex/*'
git diff --check
git diff -- harness/signal_gap_plan.py tests/test_signal_gap_plan.py
git add harness/signal_gap_plan.py tests/test_signal_gap_plan.py
git commit -m "fix: include target quarter in live gap scope"
```

Expected: 两份用户未跟踪的月均计划文件不被暂存；提交不包含算法、Actuals 或其他任务修改。

### Task 3: 从精确提交构建唯一确定性 release

**Files:**
- No source changes expected

**Step 1: 核对精确提交和工作树**

```bash
git status --short
git log -6 --oneline --decorate
git diff origin/codex/develop...HEAD --stat
```

Expected: source changes 全部已提交；只保留两份用户已有的未跟踪计划；`master` 未移动。

**Step 2: 构建两次 archive 并比较摘要**

```bash
BFL_RELEASE_ID=$(git rev-parse HEAD)
BFL_BUILD_ROOT=$(mktemp -d /tmp/bfl-quarterly-release.XXXXXX)
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

Expected: 两次 archive SHA-256 相同，两次 manifest SHA-256 相同；archive 不含 `.git`、`outputs/` 或未跟踪文件。

### Task 4: ECS candidate 验收与单次激活

**Files:**
- Runtime candidate only: `/var/tmp/bfl-release-$BFL_RELEASE_ID`

**Step 1: 只读核对现场 authority**

通过 `root@47.103.45.193` 和 `/Users/macstudio0/.ssh/finlab-key.pem` 读取：

```text
current = 4f7cbd8d4297f214ec2b55e048e1d64e075c84ce
previous = 54ecaf0eb40491f62f259c26cae37db0f2b1af06
backend active
五个 timer enabled/active/waiting
数据库为 ECS 本地 authority
```

若现场 `current` 已漂移，则停止激活并重新评估 `--expected-current`。

**Step 2: 上传并预安装 candidate**

上传 archive、manifest、候选提交中的 `install_source_release.py` 与 `build_source_release.py`。使用候选安装器预安装，不传 `--activate`：

```bash
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B \
  /var/tmp/bfl-release-$BFL_RELEASE_ID/install_source_release.py \
  --manifest /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.manifest.json \
  --archive /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.source.tar.gz \
  --expected-archive-sha256 "$BFL_ARCHIVE_SHA256" \
  --deploy-root /opt/bond-factor-lab \
  --runtime-root /var/lib/bond-factor-lab/state
```

Expected: candidate source-tree hash 与 manifest 一致；`current` 未变化。

**Step 3: 在 candidate 上运行聚焦测试与五个只读 gap plan**

候选测试至少覆盖：

```text
tests/test_signal_gap_plan.py
tests/test_signal_gap_fill.py
tests/test_quarterly_average_frontend.py
tests/test_frontend_asset_versions.py
```

加载 ECS service env，但以候选 release 为 `PYTHONPATH/project-root`。对五个 base scheme 执行：

```bash
for BFL_SCHEME_ID in \
  m0_quarterly_avg_1y_v1 \
  m0_quarterly_avg_3y_v1 \
  m0_quarterly_avg_5y_v1 \
  m0_quarterly_avg_7y_v1 \
  m0_quarterly_avg_10y_v1
do
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
    signal-gap-plan \
    --predict-date 2026-03-31 \
    --scheme-id "$BFL_SCHEME_ID"
done
```

Expected: 每个计划恰好 `expected=1`、`actionable=1`、`blocked=0`，action 为 `GRAY_LIVE_GAP`；五个业务键写前均不存在。

**Step 4: 激活同一 candidate 并只重启 backend**

```bash
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -B \
  /var/tmp/bfl-release-$BFL_RELEASE_ID/install_source_release.py \
  --manifest /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.manifest.json \
  --archive /var/tmp/bfl-release-$BFL_RELEASE_ID/$BFL_RELEASE_ID.source.tar.gz \
  --expected-archive-sha256 "$BFL_ARCHIVE_SHA256" \
  --deploy-root /opt/bond-factor-lab \
  --runtime-root /var/lib/bond-factor-lab/state \
  --activate \
  --expected-current 4f7cbd8d4297f214ec2b55e048e1d64e075c84ce
systemctl restart bond-factor-lab-backend.service
```

Expected: `current` 指向新 release，`previous` 指向 `4f7cbd8...`；backend active；root、health、Dashboard、HTML、JS、CSS 均 HTTP 200；五个 timer 状态不变。

### Task 5: 五个方案逐条受控补齐 `2026/Q2`

**Files:**
- Runtime reports only

**Step 1: 逐方案执行 insert-only gap fill**

在 current release 环境中按 `1Y → 3Y → 5Y → 7Y → 10Y` 顺序执行，每次成功读回后再执行下一条：

```bash
for BFL_SCHEME_ID in \
  m0_quarterly_avg_1y_v1 \
  m0_quarterly_avg_3y_v1 \
  m0_quarterly_avg_5y_v1 \
  m0_quarterly_avg_7y_v1 \
  m0_quarterly_avg_10y_v1
do
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python -B -m harness \
    signal-gap-fill \
    --predict-date 2026-03-31 \
    --scheme-id "$BFL_SCHEME_ID" \
    --project-root /opt/bond-factor-lab/current || break
done
```

Expected: 每次 `status=PASSED`、`records_written=1`；任一键已存在或任一命令失败就停止，不覆盖、不删除。

**Step 2: 数据库与 Dashboard 验收**

逐方案确认：

```text
prediction_phase = gray_live
predict_date = feature_date = 2026-03-31
target_date = 2026-04-01
horizon = 1
scheme_version = active exact version
run.status = success
run.records_written = 1
Actual 非空
```

Dashboard 应恰好新增五条 `2026/Q2` 记录；`2026/Q3` 五条仍保留且 Actual 为 `null`。

### Task 6: 最终前端与现场闭环

**Files:**
- No source changes expected

**Step 1: 保持既有 SSH tunnel 并让用户 review**

```text
http://127.0.0.1:18110/
127.0.0.1:18110 -> ECS 127.0.0.1:8100
```

Expected: 用户刷新现有页面即可看到 ECS 新 release。

**Step 2: 前端验收**

五个季均期限均应满足：

```text
汇总与趋势包含 2026/Q2、2026/Q3
2026/Q2 Actual 已 join，不再待验证
2026/Q3 仍待验证
2026/Q2 抽屉标题 = 2026/Q2 季度平均预测明细
预测日 = 03/31
目标季度 = 2026/Q2
```

**Step 3: 最终现场核对与汇报**

再次核对 backend、五个 timer、ECS `current/previous`、HTTP 状态、Dashboard 目标行数和 pending 数。最终报告列出：设计/计划/代码提交、release ID、archive/manifest/source-tree SHA-256、五次 gap-fill 结果、`2026/Q2`/`2026/Q3` Actual 状态，并明确未修改 Actuals 算法、Registry、timer、Nginx、DNS、Mac3、`master` 或远程分支。
