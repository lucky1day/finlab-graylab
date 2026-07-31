# WAVG GAPFLIP V5 Five-Scheme Production Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 PR #20 的五个 `weekly_average` Blackbox V2 方案安全合并到当前开发基线，完成生产技术验收、逐方案专项激活、2025 年以来历史回测及 2026 年 6—7 月灰度入库，并在不启用周度调度的当前授权边界下进入前端生产可见状态。

**Architecture:** 保留 PR 的 Blackbox 两文件和 `api-wind-date-v1` 平台输入，不修改算法内部逻辑；生产环境重新建立自己的 persisted Harness、生命周期和写库证据，不复用测试机口头结论。五个方案共享只读环境/Input 预检并最多两个算法子进程并发，生命周期与持久化事务逐方案串行；scheduler admission 使用 `gray + capabilities=[]` 精确保留身份但拒绝自动调度。

**Tech Stack:** Python 3.12、Blackbox V2 Contract 1.0、DataBridge V1、FastAPI、SQLAlchemy、MySQL 8.0、APScheduler、launchd、`unittest`。

---

## 0. 冻结范围和授权边界

### 五个 exact identity

| 期限 | Base scheme ID | Registry ID | Canonical scheme version |
|---|---|---|---|
| 1Y | `wavg_1y_gapflip_v5` | `wavg_1y_gapflip_v5__h1__1Y` | `68999585142a` |
| 3Y | `wavg_3y_gapflip_v5` | `wavg_3y_gapflip_v5__h1__3Y` | `faba245acaef` |
| 5Y | `wavg_5y_gapflip_v5` | `wavg_5y_gapflip_v5__h1__5Y` | `63ed1291f9d4` |
| 7Y | `wavg_7y_gapflip_v5` | `wavg_7y_gapflip_v5__h1__7Y` | `21951d955f44` |
| 10Y | `wavg_10y_gapflip_v5` | `wavg_10y_gapflip_v5__h1__10Y` | `c1e5a9db6097` |

共同合同固定为：

```text
runtime_type=blackbox_v2
runtime_profile=blackbox-v2-v1
data_schema_version=data-bridge-v1
input_source=data_bridge_current
platform_inputs=[api-wind-date-v1]
algorithm_version=5.0.0
frequency=weekly
task_type=weekly_average
horizon=1
target_rule=target_week_average_yield_vs_feature_week_average_yield
cron=30 11 * * 6
timezone=Asia/Shanghai
timeout_sec=3600
```

### 日期边界

```text
historical feature/predict start = 2025-01-01
gray_target_start               = 2026-06-01
technical gate predict_date     = 2026-07-25
technical gate feature_date     = 2026-07-24
technical gate target_date      = 2026-07-31
```

灰度点使用平台权威日历固定为：

| `predict_date` | `feature_date` | `target_date` |
|---|---|---|
| `2026-05-30` | `2026-05-29` | `2026-06-05` |
| `2026-06-06` | `2026-06-05` | `2026-06-12` |
| `2026-06-13` | `2026-06-12` | `2026-06-18` |
| `2026-06-20` | `2026-06-18` | `2026-06-26` |
| `2026-06-27` | `2026-06-26` | `2026-07-03` |
| `2026-07-04` | `2026-07-03` | `2026-07-10` |
| `2026-07-11` | `2026-07-10` | `2026-07-17` |
| `2026-07-18` | `2026-07-17` | `2026-07-24` |
| `2026-07-25` | `2026-07-24` | `2026-07-31` |

### 当前明确不做

- 不修改五份算法脚本的内部特征、窗口、模型、翻转或结果逻辑。
- 不把 PR body 的测试机文字声明当作生产 persisted Harness 证据。
- 不启动当前未加载的中央 scheduler，不授予 `recurring`、
  `direct_scheduled` 或 `legacy_automatic`。
- 不制造 `scheduled_live`；本轮完成后只允许
  `PRODUCTION_ACTIVATED + API_VISIBLE + HISTORY_AND_GRAY_PERSISTED`。
- 不创建新的 PR；最后按用户授权同步开发分支和 `master`。

---

### Task 1: 冻结 PR #20 并写入实施计划

**Files:**
- Create: `docs/superpowers/plans/2026-07-31-wavg-gapflip-v5-production-onboarding.md`

- [ ] **Step 1: 核对本地修改和分支**

Run:

```bash
git status --short
git branch --show-current
git branch --list
git branch -r --list
```

Expected: 工作树干净，当前分支为
`codex/audit-bugfixes-20260613`，本地和远程开发基线均不早于
`b1225fb36a826633eed1d1068fab192a60c09c30`。

- [ ] **Step 2: 核对 PR exact head 和 patch**

Run:

```bash
git fetch origin pull/20/head:refs/remotes/origin/pr/20
test "$(git rev-parse origin/pr/20)" = \
  "d9657f80286754f6244f4eb9517a4fba93a85043"
gh pr view 20 --repo lucky1day/finlab-graylab \
  --json state,isDraft,mergeable,mergeStateStatus,baseRefName,headRefName,headRefOid
gh pr diff 20 --repo lucky1day/finlab-graylab --name-only
```

Expected: `OPEN + MERGEABLE + CLEAN`；base 是当前开发分支；patch 恰好
15 个 `schemes/wavg_*_gapflip_v5` 文件。

- [ ] **Step 3: 提交计划**

```bash
git add docs/superpowers/plans/2026-07-31-wavg-gapflip-v5-production-onboarding.md
git commit -m "docs: plan wavg gapflip v5 production onboarding"
```

---

### Task 2: 合并 PR #20，保留当前开发基线

**Files:**
- Create: `schemes/wavg_1y_gapflip_v5/config.yaml`
- Create: `schemes/wavg_1y_gapflip_v5/delivery/wavg_1y_gapflip_v5.py`
- Create: `schemes/wavg_1y_gapflip_v5/delivery/wavg_1y_gapflip_v5.json`
- Create: `schemes/wavg_3y_gapflip_v5/config.yaml`
- Create: `schemes/wavg_3y_gapflip_v5/delivery/wavg_3y_gapflip_v5.py`
- Create: `schemes/wavg_3y_gapflip_v5/delivery/wavg_3y_gapflip_v5.json`
- Create: `schemes/wavg_5y_gapflip_v5/config.yaml`
- Create: `schemes/wavg_5y_gapflip_v5/delivery/wavg_5y_gapflip_v5.py`
- Create: `schemes/wavg_5y_gapflip_v5/delivery/wavg_5y_gapflip_v5.json`
- Create: `schemes/wavg_7y_gapflip_v5/config.yaml`
- Create: `schemes/wavg_7y_gapflip_v5/delivery/wavg_7y_gapflip_v5.py`
- Create: `schemes/wavg_7y_gapflip_v5/delivery/wavg_7y_gapflip_v5.json`
- Create: `schemes/wavg_10y_gapflip_v5/config.yaml`
- Create: `schemes/wavg_10y_gapflip_v5/delivery/wavg_10y_gapflip_v5.py`
- Create: `schemes/wavg_10y_gapflip_v5/delivery/wavg_10y_gapflip_v5.json`

- [ ] **Step 1: 合并 exact PR head**

Run:

```bash
git merge --no-ff --no-edit origin/pr/20
```

Expected: 只新增 PR 的 15 个方案文件；当前开发分支已有文档提交保持可达，
没有 reset、rebase 或 checkout 到 PR 的旧基线。

- [ ] **Step 2: 核验交付结构和 canonical version**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_blackbox_v2_intake \
  tests.test_blackbox_v2_discovery
git diff --check HEAD^ HEAD
```

Expected: 测试通过；五个 scheme version 与 Task 0 表格完全一致。

---

### Task 3: 修正生产首次生命周期并增加 exact gray admission

**Files:**
- Modify: `schemes/wavg_1y_gapflip_v5/config.yaml`
- Modify: `schemes/wavg_3y_gapflip_v5/config.yaml`
- Modify: `schemes/wavg_5y_gapflip_v5/config.yaml`
- Modify: `schemes/wavg_7y_gapflip_v5/config.yaml`
- Modify: `schemes/wavg_10y_gapflip_v5/config.yaml`
- Create: `tests/test_wavg_gapflip_v5_onboarding.py`
- Modify: `tests/test_blackbox_scheduler_admission.py`
- Modify: `tests/test_scheduler_main.py`
- Modify: `scheduler/blackbox_scheduler_admission.py`
- Modify: `deploy/blackbox_scheduler_admission_v1.json`

- [ ] **Step 1: 写 exact identity 与 admission 红灯测试**

`tests/test_wavg_gapflip_v5_onboarding.py` 必须逐方案断言：

```text
base ID、composite ID、canonical version、Metadata 字段、
api-wind-date-v1、周六 11:30 cron、3600 秒 timeout、两文件 SHA256
```

`tests/test_blackbox_scheduler_admission.py` 和
`tests/test_scheduler_main.py` 必须逐方案断言：

```text
mode=gray
capabilities=[]
active exact version 仍不获得 automatic/recurring/direct_scheduled
版本漂移继续 fail-closed
```

- [ ] **Step 2: 确认 admission 测试按预期失败**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_wavg_gapflip_v5_onboarding \
  tests.test_blackbox_scheduler_admission \
  tests.test_scheduler_main
```

Expected: 失败原因只包括五个 exact identity 尚未进入 Python/JSON
admission，或生产首次 config 仍错误保留测试机 `shadow`。

- [ ] **Step 3: 将首次生产 config 恢复为 draft**

五份 config 只修改：

```yaml
status: paused
version_status: draft
```

该生命周期字段变化不得改变 Task 0 中的 canonical scheme version。

- [ ] **Step 4: 增加五个 exact gray admission**

Python freeze map 和 JSON 各增加五行等价记录。1Y 记录的完整结构为：

```json
{
  "scheme_id": "wavg_1y_gapflip_v5",
  "scheme_version": "68999585142a",
  "runtime_type": "blackbox_v2",
  "frequency": "weekly",
  "task_type": "weekly_average",
  "horizon": 1,
  "target_tenor": "1Y",
  "mode": "gray",
  "capabilities": []
}
```

3Y、5Y、7Y、10Y 分别使用 Task 0 的 exact version 和 target tenor，
其余字段完全相同。

- [ ] **Step 5: 验证绿灯并提交**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_wavg_gapflip_v5_onboarding \
  tests.test_blackbox_scheduler_admission \
  tests.test_scheduler_main \
  tests.test_blackbox_v2_intake \
  tests.test_blackbox_v2_discovery
git diff --check
```

Expected: 全部通过；五个身份被 policy 精确保留但不能挂 scheduler。

Commit:

```bash
git add schemes/wavg_1y_gapflip_v5/config.yaml \
  schemes/wavg_3y_gapflip_v5/config.yaml \
  schemes/wavg_5y_gapflip_v5/config.yaml \
  schemes/wavg_7y_gapflip_v5/config.yaml \
  schemes/wavg_10y_gapflip_v5/config.yaml \
  tests/test_wavg_gapflip_v5_onboarding.py \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py \
  scheduler/blackbox_scheduler_admission.py \
  deploy/blackbox_scheduler_admission_v1.json
git commit -m "feat: prepare five wavg gapflip schemes for production"
```

---

### Task 4: 生产环境与 persisted 七 Gate

**Files:**
- Runtime output only: `reports/harness/wavg_*_gapflip_v5/`
- Database control plane only: `t_harness_runs`、`t_harness_gate_results`

- [ ] **Step 1: 生产只读 preflight**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/verify_blackbox_v2_environment.py
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/probe_blackbox_v2_sandbox.py
BOND_DAILY_COORDINATOR_MODE=legacy \
  conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/refresh_data_bridge_current.py --check-only \
    --date 2026-07-30
```

Expected: 环境、sandbox、DataBridge generation
`full-20260730-081804-9794ce962c1a` 结构校验通过。若 current 在执行前已
合法切代，则记录新的 exact generation 并用其实际 `refresh_date` 重跑，
不得伪装仍是旧 generation。

- [ ] **Step 2: 五方案 check-only，最多两个并发**

每个方案执行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness onboard wavg_1y_gapflip_v5 \
    --predict-date 2026-07-25 --stage all --check-only \
    --algo-env forecast_env_blackbox_v1 --timeout-sec 1800
```

3Y、5Y、7Y、10Y 只替换为各自 exact base ID。Expected:

```text
7/7 PASS
Backtest 100/100
persist=false
control_plane_persisted=false
business_tables_written=false
```

- [ ] **Step 3: 五方案 persisted all-stage，最多两个并发**

重复 Step 2，但删除 `--check-only`。Expected:

```text
7/7 PASS
t_harness_runs exact stage=all/status=passed
t_harness_gate_results exact 七行且全部 passed
t_scheme_runs/t_scheme_predictions/t_backtest_* 仍为零
```

若任一方案失败，只冻结该方案及其后续生产动作；不改算法、benchmark、
日历或 cutoff 贴合。

---

### Task 5: 逐方案 draft → shadow → active 和完整历史回测

**Files:**
- Lifecycle modifies the five `config.yaml` files to final `active + active`
- Database lifecycle: `t_scheme_versions`、`t_scheme_registry`
- Backtest database: `t_backtest_runs`、`t_backtest_predictions`

- [ ] **Step 1: 串行执行 draft-register**

对每个方案从最新 persisted all-stage 读取 exact
`scheme_version + harness_run_id`，签发一次性 `draft_register` token：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness auth issue \
    --scheme-id wavg_1y_gapflip_v5 \
    --action draft_register \
    --predict-date 2026-07-25 \
    --scheme-version 68999585142a \
    --harness-run-id "$BFL_WAVG_1Y_RUN_ID" \
    --issued-by lucky1day \
    --expires-in 900
```

立即用原 token 执行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate draft-register \
    --scheme-id wavg_1y_gapflip_v5 \
    --predict-date 2026-07-25 \
    --authorize "$BFL_WAVG_1Y_DRAFT_TOKEN"
```

其余四个方案使用自己的 exact ID/version/run/token，禁止复用。

- [ ] **Step 2: 串行执行 shadow-register**

每方案新签发 `shadow_register` token，仍绑定
`predict_date=2026-07-25` 和同一个 latest persisted all-stage run，随后运行
`harness gate shadow-register`。Expected:

```text
config=paused+shadow
version=shadow
registry=paused
业务表仍为零
```

- [ ] **Step 3: 串行执行 ActivationGate**

每方案新签发 `blackbox_activate` token；该 action 不传
`predict-date`，但必须绑定 exact version 和 latest persisted all-stage run。
随后执行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness activate \
    --scheme-id wavg_1y_gapflip_v5 \
    --authorize "$BFL_WAVG_1Y_ACTIVATE_TOKEN"
```

Expected:

```text
config=active+active
version=active
registry=active
registry.deployed_at=2026-07-31
admission mode=gray/capabilities=[]
scheduler job absent
```

- [ ] **Step 4: 串行持久化完整历史**

每方案新签发：

```text
action=backtest_persist
predict_date=2026-06-01
backtest_start_date=2025-01-01
exact version
latest persisted all-stage run
issued_by=lucky1day
TTL=900
```

立即执行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate backtest \
    --scheme-id wavg_1y_gapflip_v5 \
    --predict-date 2026-06-01 \
    --persist \
    --backtest-start-date 2025-01-01 \
    --timeout-sec 1800 \
    --algo-env forecast_env_blackbox_v1 \
    --authorize "$BFL_WAVG_1Y_BACKTEST_TOKEN"
```

其余四个方案逐一执行。数量按实际 HistoricalCase 动态验收，不硬编码
100 或 72；所有历史行必须满足：

```text
predict_date=feature_date
predict_date>=2025-01-01
target_date<2026-06-01
latest run immutable
五个方案之间 scheme/tenor 隔离
```

---

### Task 6: 补齐 45 条 gray live 并验收 API/前端

**Files:**
- Prediction database: `t_scheme_runs`、`t_scheme_predictions`、`t_scheme_run_log`
- Actual database remains updater-owned: `t_scheme_weekly_actuals`

- [ ] **Step 1: 按方案、按时间顺序执行 9 个 gray-backfill**

每个点单独签发 `gray_backfill_write` token，绑定 exact scheme、version、
latest persisted all-stage run 和表格中的 `predict_date`，然后立即执行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate gray-backfill \
    --scheme-id wavg_1y_gapflip_v5 \
    --predict-date 2026-05-30 \
    --prediction-phase gray_live \
    --algo-env forecast_env_blackbox_v1 \
    --authorize "$BFL_WAVG_1Y_GRAY_20260530_TOKEN"
```

每个方案内部必须严格按 Task 0 的九个日期顺序执行；不同方案最多两条 worker
链并行。每点必须精确新增：

```text
1 t_scheme_run
1 t_scheme_prediction
1 t_scheme_run_log
prediction_phase=gray_live
current_snapshot_as_of_not_historical_vintage
```

任一点失败时停止该方案的后续点，禁止覆盖、upsert 或直接调用 repository。

- [ ] **Step 2: 刷新并核验 weekly_average actual**

通过既有 actuals updater 受控刷新，不手工写 actual。核对五个 tenor、
`target_week_average_yield_vs_feature_week_average_yield` 和
`target_date` 事实键；源水位尚未覆盖的目标继续显示 pending，不计错。

- [ ] **Step 3: 验证数据库分区**

每方案必须满足：

```text
latest historical targets < 2026-06-01
9 个 live targets >= 2026-06-01
historical/live target 交集为空
gray run/prediction/log 一一对应
scheduled_live=0
active registry deployed_at 非空
```

- [ ] **Step 4: 验证 API**

核验：

```text
/api/schemes
/api/backtests/factor-lab
/api/metrics/wavg_1y_gapflip_v5__h1__1Y
/api/metrics/wavg_3y_gapflip_v5__h1__3Y
/api/metrics/wavg_5y_gapflip_v5__h1__5Y
/api/metrics/wavg_7y_gapflip_v5__h1__7Y
/api/metrics/wavg_10y_gapflip_v5__h1__10Y
```

Expected: 五个 composite ID active；latest backtest 和 9 个 gray row 可追溯；
三日期、`weekly_average`、`phase_ranges`、pending actual 和样本分母正确。

- [ ] **Step 5: 验证前端**

浏览器强制刷新后核对 1Y/3Y/5Y/7Y/10Y 的 `weekly_average` 格子：

```text
候选名称=GAPFLIP_V5
部署时间来自 Registry
历史和灰度按 target_date 分区
尚无 scheduled_live 时显示“实盘预测目标区间：待产生”
控制台 error=0
```

---

### Task 7: 生产证据、双阶段审查和发布

**Files:**
- Create: `docs/blackbox_v2/records/WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.md`
- Create: `docs/blackbox_v2/records/WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.evidence.json`
- Modify: `docs/blackbox_v2/records/README.md`
- Modify: `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `tests/test_onboarding_docs.py`

- [ ] **Step 1: 写 durable 证据**

记录五个 exact version、两文件摘要、production generation/snapshot、
persisted Harness run、七 Gate、lifecycle token action、历史 run/行数、
九个 gray run/方向、actual 水位、API/前端结果和 scheduler 禁止状态。

结论必须使用：

```text
PRODUCTION_ACTIVATED=true
API_VISIBLE=true
HISTORY_PERSISTED=true
GRAY_LIVE_COMPLETE=true
SCHEDULER_MOUNTED=false
scheduled_live=0
ONBOARDING_COMPLETE=false
PRODUCTION_OBSERVED=false
```

- [ ] **Step 2: 运行目标回归**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_wavg_gapflip_v5_onboarding \
  tests.test_blackbox_scheduler_admission \
  tests.test_scheduler_main \
  tests.test_blackbox_v2_intake \
  tests.test_blackbox_v2_discovery \
  tests.test_blackbox_v2_harness_gates \
  tests.test_blackbox_v2_draft_register \
  tests.test_weekly_actuals \
  tests.test_backend_api \
  tests.test_postonboard_scripts \
  tests.test_onboarding_docs
python -m compileall -q shared scheduler harness backtests backend \
  schemes/wavg_1y_gapflip_v5 \
  schemes/wavg_3y_gapflip_v5 \
  schemes/wavg_5y_gapflip_v5 \
  schemes/wavg_7y_gapflip_v5 \
  schemes/wavg_10y_gapflip_v5
git diff --check
```

- [ ] **Step 3: 双阶段代码审查**

先由 spec reviewer 核对 PR20、用户授权、日期边界、五方案范围、写库增量和
不挂调度边界；通过后再由 quality reviewer 核对代码、测试、文档、DB/API
证据和敏感信息。Critical/Important 必须修复并复审。

- [ ] **Step 4: 提交生产证据**

```bash
git add schemes/wavg_1y_gapflip_v5/config.yaml \
  schemes/wavg_3y_gapflip_v5/config.yaml \
  schemes/wavg_5y_gapflip_v5/config.yaml \
  schemes/wavg_7y_gapflip_v5/config.yaml \
  schemes/wavg_10y_gapflip_v5/config.yaml \
  docs/blackbox_v2/records/WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.md \
  docs/blackbox_v2/records/WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.evidence.json \
  docs/blackbox_v2/records/README.md \
  docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md \
  docs/CURRENT_STATUS.md \
  tests/test_onboarding_docs.py
git commit -m "docs: record five wavg gapflip production activations"
```

- [ ] **Step 5: 推送开发分支并同步 master**

在提交前重新执行 `git status --short` 和分支列表核对。然后：

```bash
git push origin codex/audit-bugfixes-20260613
git checkout master
git pull --ff-only origin master
git merge --ff-only codex/audit-bugfixes-20260613
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_wavg_gapflip_v5_onboarding \
  tests.test_blackbox_scheduler_admission \
  tests.test_scheduler_main \
  tests.test_onboarding_docs
git push origin master
git checkout codex/audit-bugfixes-20260613
```

Expected: 本地开发分支、本地 `master`、远程开发分支和远程 `master`
指向同一最终提交；工作树干净；不创建新 PR。
