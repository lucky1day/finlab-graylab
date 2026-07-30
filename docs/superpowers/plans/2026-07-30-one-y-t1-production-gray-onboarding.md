# 1Y T+1 Blackbox Production Gray Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 合入 PR #19 的 CompareGate 修复和 `one_y_t1_quote_state_hv_v1`
交付，完成专项 active、历史、连续 gray_live、实际值、API/前端和现有
07:00 launchd gray runner 挂载验收。

**Architecture:** Blackbox 算法文件保持原字节，平台只修改通用 Gate、生命周期
配置、Scheduler admission 和审计文档。生产副作用全部通过 Harness 一次性授权；
现有 `com.bond-factor-lab.daily-gray` 负责后续每天扫描 active daily 方案，
不新增 per-scheme plist，不修改 25/29 ledger policy。

**Tech Stack:** Python 3.12、pytest/unittest、FastAPI、SQLAlchemy/MySQL、
Blackbox V2 Harness、launchd、原生 HTML/JS。

---

### Task 1: 用 TDD 验证并合入 CompareGate 修复

**Files:**
- Modify: `tests/test_blackbox_v2_harness_gates.py`
- Merge: `harness/blackbox_v2/gates.py`

- [x] **Step 1: 增加 prior feature date 自洽断言**

在
`test_comparison_requests_use_distinct_existing_cutoffs` 的现有断言后增加：

```python
prior = requests[0]
self.assertEqual(prior.feature_date, "2026-07-14")
self.assertEqual(prior.feature_date, prior.daily_cutoff_key)
```

- [x] **Step 2: 在未合入 PR 修复的基线上确认测试失败**

Run:

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_blackbox_v2_harness_gates.py::BlackboxV2HarnessGateTests::test_comparison_requests_use_distinct_existing_cutoffs
```

Expected: FAIL，`prior.feature_date` 实际仍为 `2026-07-15`。

- [x] **Step 3: 合入 PR #19**

Run:

```bash
git merge --no-ff origin/pr/19 \
  -m "merge: onboard one year t1 blackbox delivery"
```

Expected: 无冲突，合入 `harness/blackbox_v2/gates.py` 和三份方案文件。

- [x] **Step 4: 确认回归测试转绿**

Run Task 1 Step 2 的相同命令。

Expected: PASS。

- [x] **Step 5: 运行 CompareGate 完整测试文件**

Run:

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_blackbox_v2_harness_gates.py
```

Expected: 全部通过。

### Task 2: 恢复首次生产生命周期并完成技术 Gate

**Files:**
- Modify: `schemes/one_y_t1_quote_state_hv_v1/config.yaml`
- Preserve byte-for-byte:
  `schemes/one_y_t1_quote_state_hv_v1/delivery/one_y_t1_quote_state_hv_v1.py`
- Preserve byte-for-byte:
  `schemes/one_y_t1_quote_state_hv_v1/delivery/one_y_t1_quote_state_hv_v1.json`

- [x] **Step 1: 记录交付摘要并把 config 生命周期恢复为 draft**

Run:

```bash
shasum -a 256 \
  schemes/one_y_t1_quote_state_hv_v1/delivery/one_y_t1_quote_state_hv_v1.py \
  schemes/one_y_t1_quote_state_hv_v1/delivery/one_y_t1_quote_state_hv_v1.json
```

将 `config.yaml` 的 `version_status: shadow` 改成
`version_status: draft`，保持 `status: paused`。生产 DB 当前无本方案记录，
不能继承 PR 工作机的 shadow 声明。

- [x] **Step 2: 验证环境、sandbox 和 DataBridge current**

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

Expected: environment/sandbox PASS，DataBridge `status=ok`，记录 exact
generation 和三频 SHA256。

- [x] **Step 3: 执行零写入七 Gate**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness onboard one_y_t1_quote_state_hv_v1 \
    --predict-date 2026-07-30 \
    --stage all \
    --check-only \
    --algo-env forecast_env_blackbox_v1 \
    --timeout-sec 1800
```

Expected: 7/7 PASS，`100/100 + persist=false`，控制面和业务表均零写。

- [x] **Step 4: 执行 persisted all-stage**

去掉 `--check-only` 重跑同一 all-stage，并记录 exact
`harness_run_id + scheme_version + generation + snapshot`。

Expected: 7/7 PASS；审计库 exact run 一行、七 Gate 七行，业务表零新增。

### Task 3: Draft、Shadow 和生产激活

**Files:**
- Modify: `tests/test_blackbox_scheduler_admission.py`
- Modify: `scheduler/blackbox_scheduler_admission.py`
- Modify: `deploy/blackbox_scheduler_admission_v1.json`

- [x] **Step 1: 用 TDD 声明 exact gray admission**

在测试的 expected policy 集合中加入：

```python
"one_y_t1_quote_state_hv_v1": _expected(
    scheme_id="one_y_t1_quote_state_hv_v1",
    scheme_version="<Task 2 exact version>",
    frequency="daily",
    task_type="T+1",
    horizon=1,
    target_tenor="1Y",
    mode="gray",
    capabilities=NO_CAPABILITIES,
)
```

先运行
`tests/test_blackbox_scheduler_admission.py`，确认因实现/JSON 缺少该项而失败。

- [x] **Step 2: 增加最小 admission 实现**

在 Python policy 和 JSON 中加入完全相同的 exact identity：

```json
{
  "scheme_id": "one_y_t1_quote_state_hv_v1",
  "scheme_version": "<Task 2 exact version>",
  "runtime_type": "blackbox_v2",
  "frequency": "daily",
  "task_type": "T+1",
  "horizon": 1,
  "target_tenor": "1Y",
  "mode": "gray",
  "capabilities": []
}
```

重新运行 admission 测试并确认通过。该 policy 阻断正式 scheduler plane，
但现有 active-daily gray runner 不读取该 policy。

- [x] **Step 3: 执行 draft-register 和 shadow-register**

分别签发 `draft_register`、`shadow_register` 一次性 token，并运行对应 Gate。
每次都绑定 Task 2 exact version、Harness run 和 `predict_date=2026-07-30`。

Expected: config、version、Registry 依次到达 `draft + paused` 和
`shadow + paused`；业务 run/prediction/backtest 为 0。

- [x] **Step 4: 执行 blackbox_activate**

签发 `blackbox_activate` token 后运行 Activation Gate。

Expected: config `active + active`，version/Registry active，
`deployed_at` 非空；active daily discovery 包含本方案。

### Task 4: 完整历史和连续 gray_live

**Files:**
- Runtime evidence only; no algorithm source edits.

- [x] **Step 1: 持久化历史回测**

签发：

```bash
python -m harness auth issue \
  --scheme-id one_y_t1_quote_state_hv_v1 \
  --action backtest_persist \
  --predict-date 2026-06-01 \
  --backtest-start-date 2025-01-01 \
  --scheme-version <exact-version> \
  --harness-run-id <exact-run> \
  --issued-by codex-one-y-t1-production-20260730 \
  --expires-in 900
```

使用 token 执行：

```bash
python -m harness gate backtest \
  --scheme-id one_y_t1_quote_state_hv_v1 \
  --predict-date 2026-06-01 \
  --persist \
  --backtest-start-date 2025-01-01 \
  --timeout-sec 1800 \
  --algo-env forecast_env_blackbox_v1 \
  --authorize "$TOKEN"
```

Expected: 一个 immutable success run，所有
`target_date < 2026-06-01`，monthly metrics 非空。

- [x] **Step 2: 枚举 T+1 灰度目标**

由生产交易日历枚举 `target_date >= 2026-06-01` 且当前可生成的交易日，
逐点反推：

```text
feature_date = 前一交易日
predict_date = target_date
```

保存精确日期清单和前置 DB 计数。

- [x] **Step 3: 逐点授权写入 gray_live**

每个 `predict_date` 单独签发 `gray_backfill_write` token，运行
`harness gate gray-backfill`。任一点失败立即停止；成功点必须各增加
run/prediction/run-log 恰好一行。

- [x] **Step 4: 使用 launchd 同路径做当前 canary**

若当前目标已由 backfill 写入，先用 `--only` 做幂等路径验证；否则让它写入当前点：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m scheduler.daily_gray_runner \
    --predict-date 2026-07-30 \
    --only one_y_t1_quote_state_hv_v1
```

Expected: runner 发现 exact active scheme，返回 success；不产生重复 target。

### Task 5: Actual、API、前端和 launchd 终验

**Files:**
- Create:
  `docs/blackbox_v2/records/ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.md`
- Create:
  `docs/blackbox_v2/records/ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.evidence.json`
- Modify: `docs/blackbox_v2/records/README.md`
- Modify: `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md`

- [x] **Step 1: 运行日频 actual updater**

使用平台现有 actual updater 幂等刷新本方案已到期 target，确认实际为 0 的样本
保留并进入分母，pending 不进入准确率分母。

- [x] **Step 2: 数据库只读验收**

核对：

```text
Registry active + deployed_at
latest historical run success
history target < 2026-06-01
gray target >= 2026-06-01
history/gray target overlap = 0
gray target gaps = 0
scheduled_live = 0
run/prediction/log 一一对应
```

- [x] **Step 3: API 与前端读回**

验证：

```text
/api/schemes
/api/backtests/factor-lab
/api/metrics/one_y_t1_quote_state_hv_v1__h1__1Y
/api/factor-lab/dashboard
```

如服务未加载最新代码，只重启 `com.bond-factor-lab.backend`，不操作
BondProjectPro。

- [x] **Step 4: launchd 挂载验收**

确认 `gui/501/com.bond-factor-lab.daily-gray` 已加载、日历触发为 07:00，
active daily discovery 包含本方案。自然首跑之前状态记为
`MOUNTED_NOT_OBSERVED`；不得伪造运行次数。

- [x] **Step 5: 保存证据并验证**

记录交付 hashes、Gate run、generation/snapshot、生命周期、历史/gray 行数、
API、runner canary 和 launchd 状态。运行：

```bash
python -m json.tool \
  docs/blackbox_v2/records/ONE_Y_T1_QUOTE_STATE_HV_ONBOARDING_20260730.evidence.json
pytest -q tests/test_blackbox_v2_harness_gates.py \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_onboarding_docs.py
git diff --check
```

Expected: 全部通过。

- [x] **Step 6: 提交并同步**

只暂存本方案、修复、测试和记录文件，提交后快进本地 `master`，推送开发分支和
远程 `master`。PR #19 的 head 已成为开发分支祖先后确认其远程状态已合并；不创建
新 PR。
