# 3Y ADYN T+1 Dual-Scheme Production Gray Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 合入 PR #21 的两个 3Y T+1 Blackbox V2 方案，完成专项生产灰度入库，
并挂载到现有每天 07:00 的 launchctl daily-gray 任务。

**Architecture:** 两份算法交付保持原字节，平台只处理生命周期、Harness、Registry、
回测、gray 写库、admission 与审计。调度复用
`com.bond-factor-lab.daily-gray` 的 active-daily 扫描，不新增 per-scheme plist，
不修改 25/29 ledger policy。

**Tech Stack:** Python 3.12、pytest、FastAPI、SQLAlchemy/MySQL、Blackbox V2
Harness、DataBridge V1、launchd。

## Global Constraints

- 当前开发分支为 `codex/audit-bugfixes-20260613`，生产分支为 `master`。
- 两个 scheme ID 为 `three_y_adyn_lb2_k1_v1` 和
  `three_y_adyn_lb1_k3_v1`。
- scheme version 分别为 `47c7c1776db0` 和 `98233f0cb9ef`。
- `gray_target_start=2026-06-01`，历史起点为 `2025-01-01`。
- 只写 `gray_live`；本批不得制造 `scheduled_live`。
- 交付算法内部逻辑不修改，平台只验证接口、确定性、截止隔离和标准结果。
- 生产副作用只通过 Harness 一次性专项授权执行，不直写 SQL。
- `outputs/` 和其它未跟踪产物不得进入提交。

---

### Task 1: 三方合并 PR #21 并恢复首次生命周期

**Files:**
- Merge: `origin/codex/onboard-three-y-adyn-t1-20260731`
- Modify: `schemes/three_y_adyn_lb2_k1_v1/config.yaml`
- Modify: `schemes/three_y_adyn_lb1_k3_v1/config.yaml`
- Preserve: 两个 `delivery/*.py + delivery/*.json`

**Interfaces:**
- Consumes: PR head `a8b9ec42fb336e8f5270fb341e3850915e5faa9d`
- Produces: 当前开发分支上的两个 `paused + draft` Blackbox V2 identity

- [ ] **Step 1: 重新核对工作区和分支**

Run:

```bash
git status --short
git branch --show-current
git rev-parse HEAD
git rev-parse origin/codex/onboard-three-y-adyn-t1-20260731
```

Expected: 工作区干净；当前分支为 `codex/audit-bugfixes-20260613`；PR head 为
`a8b9ec42fb336e8f5270fb341e3850915e5faa9d`。

- [ ] **Step 2: 执行三方合并**

Run:

```bash
git merge --no-ff origin/codex/onboard-three-y-adyn-t1-20260731 \
  -m "merge: onboard two three year adyn t1 schemes"
```

Expected: 两个 delivery 被新增；当前五个 `wavg_*_gapflip_v5` 文件和 evidence
保持不变；admission 的双方追加同时保留。

- [ ] **Step 3: 恢复首次生产生命周期**

将两个 `config.yaml` 精确改为：

```yaml
status: paused
version_status: draft
```

其余字段不变。执行：

```bash
shasum -a 256 \
  schemes/three_y_adyn_lb2_k1_v1/delivery/three_y_adyn_lb2_k1_v1.py \
  schemes/three_y_adyn_lb2_k1_v1/delivery/three_y_adyn_lb2_k1_v1.json \
  schemes/three_y_adyn_lb1_k3_v1/delivery/three_y_adyn_lb1_k3_v1.py \
  schemes/three_y_adyn_lb1_k3_v1/delivery/three_y_adyn_lb1_k3_v1.json
```

Expected: delivery 与 PR tree object 原字节一致。

- [ ] **Step 4: 验证 admission 和 discovery**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_daily_gray_runner.py \
  tests/test_scheduler_main.py
```

Expected: 全部通过；两个方案被 admission 的三个正式 plane 拒绝，但可被
`daily_gray_runner._daily_active_schemes()` 在激活后发现。

### Task 2: 环境、DataBridge 和七 Gate

**Files:**
- Runtime evidence: `reports/harness/three_y_adyn_*/`

**Interfaces:**
- Consumes: 两个 `paused + draft` config 和 DataBridge `2026-07-30` generation
- Produces: 每方案 exact persisted `harness_run_id` 和七个 passed Gate

- [ ] **Step 1: 运行冻结环境和 sandbox preflight**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/verify_blackbox_v2_environment.py
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/probe_blackbox_v2_sandbox.py
BOND_DAILY_COORDINATOR_MODE=legacy \
  conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/refresh_data_bridge_current.py --check-only --date 2026-07-30
```

Expected: environment、sandbox、current 完整性均通过，并记录 generation、
refresh date、三频 SHA 和 business digest。

- [ ] **Step 2: 对两个方案执行零写入七 Gate**

Run:

```bash
for scheme_id in three_y_adyn_lb2_k1_v1 three_y_adyn_lb1_k3_v1; do
  conda run --no-capture-output -n bond_factor_lab_service \
    python -m harness onboard "$scheme_id" \
      --predict-date 2026-07-30 \
      --stage all \
      --check-only \
      --algo-env forecast_env_blackbox_v1 \
      --timeout-sec 1800
done
```

Expected: 每方案 7/7 PASS；Backtest 为 `100/100 + persist=false`；
`control_plane_persisted=false`、`business_tables_written=false`。

- [ ] **Step 3: 对两个方案执行 persisted all-stage**

去掉 `--check-only`，使用同一 `predict-date` 和环境重跑。每方案从最新
`onboard_report.json` 读取并保存：

```text
harness_run_id
scheme_version
generation_id
combined_snapshot_id
overall_passed=true
```

Expected: 审计库每个 exact run 一行，七 Gate 七行且全部 passed；业务表仍为零。

### Task 3: Draft、Shadow、回测和 Activation

**Files:**
- Lifecycle state: `config.yaml`、`t_scheme_versions`、`t_scheme_registry`

**Interfaces:**
- Consumes: Task 2 persisted all-stage identities
- Produces: active composite Registry
  `three_y_adyn_lb2_k1_v1__h1__3Y` 和
  `three_y_adyn_lb1_k3_v1__h1__3Y`

- [ ] **Step 1: 逐方案 draft-register**

对每个方案从其 report 取得 exact run/version，签发 `draft_register` token，再执行
`python -m harness gate draft-register`。operator 固定为
`codex-pr21-production-20260731`，TTL 为 900 秒。

Expected: config/版本/Registry 为 `draft + paused`，业务 run/prediction/backtest
仍为零。

- [ ] **Step 2: 逐方案 shadow-register**

用相同 exact run/version 单独签发 `shadow_register` token 并执行 Gate。

Expected: config/版本为 `shadow`，composite Registry 为 `paused`；两方案 token
不可互换或重放。

- [ ] **Step 3: 持久化完整历史回测**

对每个方案签发：

```bash
python -m harness auth issue \
  --scheme-id "$scheme_id" \
  --action backtest_persist \
  --predict-date 2026-06-01 \
  --backtest-start-date 2025-01-01 \
  --scheme-version "$scheme_version" \
  --harness-run-id "$harness_run_id" \
  --issued-by codex-pr21-production-20260731 \
  --expires-in 900
```

使用各自 token 执行：

```bash
python -m harness gate backtest \
  --scheme-id "$scheme_id" \
  --predict-date 2026-06-01 \
  --persist \
  --backtest-start-date 2025-01-01 \
  --timeout-sec 1800 \
  --algo-env forecast_env_blackbox_v1 \
  --authorize "$token"
```

Expected: 每方案追加一个 immutable success run；所有 target 严格小于
`2026-06-01`，monthly metrics 非空。

- [ ] **Step 4: 逐方案 blackbox-activate**

签发 `blackbox_activate` token 并执行 Activation Gate。

Expected: config 和版本为 `active`；composite Registry 为 `active` 且
`deployed_at=2026-07-31`；active daily discovery 包含两个方案。

### Task 4: 连续 gray_live 与 daily-gray canary

**Files:**
- Runtime evidence only; no algorithm source edits

**Interfaces:**
- Consumes: active schemes、平台交易日历、latest valid DataBridge current
- Produces: 连续 gray_live 和 daily-gray 执行证据

- [ ] **Step 1: 枚举精确 T+1 灰度日期**

从平台交易日历枚举 `target_date >= 2026-06-01` 且当前 DataBridge 可生成的
交易日，逐点反推 `feature_date=前一交易日`、`predict_date=target_date`。
保存日期清单；不得从部署日向后枚举。

- [ ] **Step 2: 按日期和方案逐点 gray-backfill**

每个 `scheme_id + predict_date` 单独签发 `gray_backfill_write` token，再执行：

```bash
python -m harness gate gray-backfill \
  --scheme-id "$scheme_id" \
  --predict-date "$predict_date" \
  --prediction-phase gray_live \
  --algo-env forecast_env_blackbox_v1 \
  --authorize "$token"
```

Expected: insert-only；每点 run/prediction/run-log 各增加一行；任一点失败立即停止
该方案后续日期。

- [ ] **Step 3: 使用现有 runner 做双方案 canary**

保留最新一个当前可运行目标给 runner，执行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m scheduler.daily_gray_runner \
    --predict-date 2026-07-30 \
    --only three_y_adyn_lb2_k1_v1,three_y_adyn_lb1_k3_v1
```

Expected: `total=2 success=2 failed=0 skipped=0`；两条均为 `gray_live`；
不产生 `scheduled_live`。

### Task 5: Actual、API、前端和 launchctl

**Files:**
- Create:
  `docs/blackbox_v2/records/THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.md`
- Create:
  `docs/blackbox_v2/records/THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.evidence.json`
- Modify: `docs/blackbox_v2/records/README.md`
- Modify: `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md`
- Modify: `docs/CURRENT_STATUS.md`

**Interfaces:**
- Consumes: production database and active backend
- Produces: durable evidence and externally visible schemes

- [ ] **Step 1: 幂等刷新日频 actual**

运行平台日频 actual updater，确认实际方向为 0 的样本保留；只在指标聚合时剔除
预测方向为 0 的样本。

- [ ] **Step 2: 数据库分区验收**

逐方案核对：

```text
Registry active + deployed_at 非空
latest backtest success
history target < 2026-06-01
gray target >= 2026-06-01
history/gray overlap = 0
gray gaps = 0
scheduled_live = 0
run/prediction/log 一一对应
```

- [ ] **Step 3: API 和前端验收**

验证：

```text
/api/schemes
/api/backtests/factor-lab
/api/metrics/three_y_adyn_lb2_k1_v1__h1__3Y
/api/metrics/three_y_adyn_lb1_k3_v1__h1__3Y
/api/factor-lab/dashboard
```

Expected: 两个 3Y T+1 候选可见，名称和 description 来自 Metadata，历史与
gray target 零重叠，pending actual 显示待验证。

- [ ] **Step 4: 验收 daily-gray LaunchAgent**

先比较已安装 plist 与仓库模板的 Program、环境和 07:00 触发。若任务尚未加载，
使用 `bootstrap` 加载；若已加载且模板一致，不为制造运行证据而立即
`kickstart` 全部 active daily，尤其不能在当日 DataBridge current 尚未就绪时
触发预期失败。执行只读核验：

```bash
launchctl print gui/501/com.bond-factor-lab.daily-gray
```

Expected: 仍为单一 `com.bond-factor-lab.daily-gray`，无 per-scheme plist，
`StartCalendarInterval=07:00`，自然首跑前状态为 `MOUNTED_NOT_OBSERVED`。

### Task 6: 回归、提交、master 和远程同步

**Files:**
- All files from Tasks 1–5

**Interfaces:**
- Consumes: fully verified development branch
- Produces: identical local/remote development and master SHAs

- [ ] **Step 1: 运行定向和全量回归**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_daily_gray_runner.py \
  tests/test_scheduler_main.py \
  tests/test_onboarding_docs.py
git diff --check
python -m json.tool \
  docs/blackbox_v2/records/THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.evidence.json
```

Expected: 全部通过，JSON 合法，diff 无空白错误。

- [ ] **Step 2: 精确暂存并提交**

先运行 `git status --short`，只暂存 PR #21、两份生命周期 config、测试、计划和
本批 evidence；不得暂存 `outputs/`。提交信息：

```text
feat: activate two three year adyn t1 schemes
```

- [ ] **Step 3: 同步生产分支并推送**

用户已明确授权发布到 `master`。将本地 `master` 快进到开发分支相同提交，推送：

```bash
git push origin codex/audit-bugfixes-20260613
git push origin master
```

Expected: local dev、local master、origin dev、origin master 四个 SHA 完全一致；
PR #21 显示 merged；不创建新 PR。
