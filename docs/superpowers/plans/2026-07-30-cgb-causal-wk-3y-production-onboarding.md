# cgb_causal_wk_3y Production Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `cgb_causal_wk_3y@4b8db29b2f74` 原样接入 Blackbox V2，完成生产激活、2025-01-01 起的历史回测、2026 年 6–7 月灰度补齐和前端验收，同时保持周度 scheduler 不挂载。

**Architecture:** 复用现有 Intake、`api-wind-date-v1` 组合输入、Harness 生命周期、持久化回测、gray-backfill 和前端核验入口。方案是全新身份，不新增 repository 或删除工具；生产副作用由主线按 exact version/run/token 串行执行，独立的身份、运行手册和数据核查可并行完成。

**Tech Stack:** Python 3.12、Blackbox V2 Contract 1.0、SQLAlchemy/MySQL、FastAPI、Harness、pytest。

---

## Global Constraints

- 上游 Python SHA256 固定为
  `849c2fe6e24343230ddcc17212979f981144cb03f44d0ff9b1457f96fa7d0383`。
- 上游 Metadata SHA256 固定为
  `84b09972a583314c401228957a83d9195821ed0fbab93040cfb76e2e3c4bbb38`。
- Canonical scheme version 固定为 `4b8db29b2f74`。
- 只接收 `.py + .json`；上游 `api_wind_date.csv` 不进入 delivery，平台配置声明
  `platform_inputs: [api-wind-date-v1]`。
- 不修改 CV2.0.0-rc1 的 156 周窗口、104 周半衰期、0.8 上行权重或低置信反转。
- 历史从 `2025-01-01` 构造，只持久化 `target_date < 2026-06-01`。
- 灰度只写 `2026-06-01 <= target_date <= 2026-07-31`。
- Scheduler admission 固定为 `gray + capabilities=[]`，不产生 `scheduled_live`。
- 生产授权已由用户明确给出；仍必须逐 action 使用 exact 一次性 token。
- 不创建 PR；完成后合并开发分支并快进本地/远程 `master`。

### Task 1: TDD Intake 并冻结 exact identity

**Files:**
- Create: `tests/test_cgb_causal_wk_3y_onboarding.py`
- Create: `schemes/cgb_causal_wk_3y/config.yaml`
- Create: `schemes/cgb_causal_wk_3y/delivery/cgb_causal_wk_3y.py`
- Create: `schemes/cgb_causal_wk_3y/delivery/cgb_causal_wk_3y.json`

- [ ] **Step 1: 写缺失方案的红灯测试**

```python
def test_delivery_bytes_and_exact_version_are_frozen() -> None:
    config = load_scheme_config(CONFIG_PATH)
    assert config.scheme_version == "4b8db29b2f74"
    assert _sha256(config.delivery_script) == SCRIPT_SHA256
    assert _sha256(config.delivery_metadata) == METADATA_SHA256


def test_weekly_platform_contract_uses_authoritative_calendar() -> None:
    config = load_scheme_config(CONFIG_PATH)
    assert config.platform_inputs == ("api-wind-date-v1",)
    assert config.schedule.cron == "30 11 * * 6"
    assert config.schedule.timezone == "Asia/Shanghai"
    assert config.task_type == "weekly_point"
    assert config.horizon == 1
    assert config.tenors == ["3Y"]
```

- [ ] **Step 2: 运行红灯**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_cgb_causal_wk_3y_onboarding.py
```

Expected: FAIL，错误为缺少
`schemes/cgb_causal_wk_3y/config.yaml`。

- [ ] **Step 3: 用私有两文件目录执行 Intake**

用 `mktemp -d` 创建目录，只复制上游 `.py + .json`，再运行：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m harness intake-blackbox \
  --delivery-dir "$CGB3Y_INTAKE_DIR" \
  --project-root \
  /Users/macstudio0/bond-factor-lab/.worktrees/blackbox-v2-3y-weekly-onboarding-20260730 \
  --runtime-profile blackbox-v2-v1 \
  --data-schema-version data-bridge-v1 \
  --platform-input api-wind-date-v1
```

- [ ] **Step 4: 运行绿色 identity/Intake/discovery 回归**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_cgb_causal_wk_3y_onboarding.py \
  tests/test_blackbox_v2_intake.py \
  tests/test_blackbox_v2_discovery.py
```

Expected: 全部 PASS，config 为 `paused + draft`，exact version 为
`4b8db29b2f74`。

- [ ] **Step 5: 提交 Intake**

```bash
git add schemes/cgb_causal_wk_3y \
  tests/test_cgb_causal_wk_3y_onboarding.py
git commit -m "feat: intake cgb causal weekly 3y"
```

### Task 2: TDD 增加 exact gray admission

**Files:**
- Modify: `scheduler/blackbox_scheduler_admission.py`
- Modify: `deploy/blackbox_scheduler_admission_v1.json`
- Modify: `tests/test_blackbox_scheduler_admission.py`
- Modify: `tests/test_scheduler_main.py`

- [ ] **Step 1: 先把 3Y exact identity 加入 expected 测试**

```python
(
    "cgb_causal_wk_3y",
    "4b8db29b2f74",
): _expected_admission(
    mode="gray",
    frequency="weekly",
    task_type="weekly_point",
    horizon=1,
    target_tenor="3Y",
    capabilities=NO_CAPABILITIES,
)
```

Scheduler 测试必须把该身份归入 gray 集合，并证明不会进入 recurring、
startup catch-up 或 direct scheduled。

- [ ] **Step 2: 运行红灯**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py
```

Expected: FAIL，指出 Python freeze map 和部署 JSON 缺少 3Y identity。

- [ ] **Step 3: 增加机器准入**

Python 与 JSON 均写入：

```json
{
  "scheme_id": "cgb_causal_wk_3y",
  "scheme_version": "4b8db29b2f74",
  "runtime_type": "blackbox_v2",
  "frequency": "weekly",
  "task_type": "weekly_point",
  "horizon": 1,
  "target_tenor": "3Y",
  "mode": "gray",
  "capabilities": []
}
```

- [ ] **Step 4: 运行绿色 admission/scheduler 回归**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py \
  tests/test_cgb_causal_wk_3y_onboarding.py
```

- [ ] **Step 5: 提交准入**

```bash
git add scheduler/blackbox_scheduler_admission.py \
  deploy/blackbox_scheduler_admission_v1.json \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py
git commit -m "feat: isolate weekly 3y scheduler admission"
```

### Task 3: 技术 Gate、同代输入和 persisted all-stage

**Files:**
- Create: `docs/blackbox_v2/records/CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.md`
- Create: `docs/blackbox_v2/records/CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.evidence.json`
- Modify: `docs/blackbox_v2/records/README.md`
- Modify: `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md`
- Modify: `tests/test_onboarding_docs.py`

- [ ] **Step 1: 只读核对身份占用、环境和 DataBridge current**

数据库预检必须确认 base/composite/version/run/prediction/backtest 均为零。
执行：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  scripts/verify_blackbox_v2_environment.py
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  scripts/probe_blackbox_v2_sandbox.py
```

读取 `data/data_bridge/current/state.json` 的 exact refresh date，并以
`--check-only` 核对，不发布新 generation。

- [ ] **Step 2: 执行 check-only all-stage**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m harness onboard cgb_causal_wk_3y \
  --predict-date 2026-07-25 \
  --stage all \
  --check-only \
  --project-root \
  /Users/macstudio0/bond-factor-lab/.worktrees/blackbox-v2-3y-weekly-onboarding-20260730 \
  --algo-env forecast_env_blackbox_v1 \
  --timeout-sec 1800
```

Expected: 7/7 PASS、100/100 技术回测、零控制面和业务写入。

- [ ] **Step 3: 记录上游 snapshot 与生产 generation 的边界**

使用 `/tmp` 输出运行上游 `verify_vs_reference.py`，保留其 0725 snapshot
内 `376/376` 结论；平台报告只把同代生产 generation 的输出作为生产真值。
必须记录 `data_vintage_mismatch`、随包周历非严格升序、缺少平台
`202625` 以及 CV2 低置信反转的 snapshot 敏感性；不得把随包日历送入平台
provider。

- [ ] **Step 4: 执行 persisted all-stage**

用相同 predict date、generation、环境和 exact scheme version 再运行一次
不带 `--check-only` 的 `onboard --stage all`。Expected: 7/7 PASS，
`t_harness_runs` 存在 exact `stage=all/status=passed`，七个 gate result
全部 passed。

- [ ] **Step 5: 写技术证据、运行文档门禁并提交**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_onboarding_docs.py \
  tests/test_cgb_causal_wk_3y_onboarding.py
git diff --check
git add docs/blackbox_v2/records tests/test_onboarding_docs.py
git commit -m "docs: record weekly 3y technical readiness"
```

### Task 4: Draft、Shadow 与生产 Activation

**Files:**
- Modify: `schemes/cgb_causal_wk_3y/config.yaml`
- Modify: production evidence record from Task 3

- [ ] **Step 1: 加载授权密钥并签发 draft token**

只从仓库 `.env` 加载 `HARNESS_AUTH_SECRET`，不得打印密钥。Token 固定绑定：

```text
scheme_id=cgb_causal_wk_3y
scheme_version=4b8db29b2f74
predict_date=2026-07-25
action=draft_register
issued_by=codex-cgb3y-production-20260730
```

执行 `harness auth issue` 后立即调用 `harness gate draft-register`。

- [ ] **Step 2: 签发并消费 shadow token**

使用同一 exact persisted all-stage run，action 改为 `shadow_register`，
随后运行 `harness gate shadow-register`。核对 config 为
`paused + shadow`，Registry 为 paused，业务表仍为零。

- [ ] **Step 3: 签发并消费 activation token**

action 为 `blackbox_activate`，运行：

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m harness activate \
  --scheme-id cgb_causal_wk_3y \
  --project-root \
  /Users/macstudio0/bond-factor-lab/.worktrees/blackbox-v2-3y-weekly-onboarding-20260730 \
  --authorize "$CGB3Y_ACTIVATE_TOKEN"
```

`blackbox_activate` token 只绑定 exact scheme/version/Harness run 和 operator，
不伪造额外 predict date。

- [ ] **Step 4: 读回生命周期**

必须满足：

```text
config=active+active
version=4b8db29b2f74 active
registry=cgb_causal_wk_3y__h1__3Y active
deployed_at 非空
scheduler admission=gray/capabilities=[]
scheduled_live=0
```

- [ ] **Step 5: 提交生命周期变化**

```bash
git add schemes/cgb_causal_wk_3y/config.yaml \
  docs/blackbox_v2/records
git commit -m "feat: activate cgb causal weekly 3y"
```

### Task 5: 持久化历史回测并核对前端 canonical run

**Files:**
- Modify: production evidence record from Task 3

- [ ] **Step 1: 签发 exact backtest token**

Token action `backtest_persist`，绑定：

```text
predict_date=2026-06-01
backtest_start_date=2025-01-01
scheme_version=4b8db29b2f74
harness_run_id=$CGB3Y_HARNESS_RUN_ID
```

`CGB3Y_HARNESS_RUN_ID` 必须从 Task 3 数据库读回的唯一 exact
`stage=all/status=passed` 记录赋值，不得从本地报告文件猜测。

- [ ] **Step 2: 执行完整持久化回测**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m harness gate backtest \
  --scheme-id cgb_causal_wk_3y \
  --predict-date 2026-06-01 \
  --persist \
  --backtest-start-date 2025-01-01 \
  --timeout-sec 1800 \
  --algo-env forecast_env_blackbox_v1 \
  --project-root \
  /Users/macstudio0/bond-factor-lab/.worktrees/blackbox-v2-3y-weekly-onboarding-20260730 \
  --authorize "$CGB3Y_BACKTEST_TOKEN"
```

- [ ] **Step 3: 只读核对历史**

核对 latest successful run、72 条 prediction、17 个月度指标、
`target_date < 2026-06-01`、与 live target 零重叠。实际数量必须与平台
calendar 枚举一致，不用固定 100 代替完整数量。

- [ ] **Step 4: 核对 API canonical 选择**

`/api/backtests/factor-lab` 必须选中刚写入 run，并使用
`blackbox_v2_current_snapshot_as_of` 数据源；Registry composite row
必须出现在 `/api/schemes`。

### Task 6: 逐点补齐 2026 年 6–7 月 gray_live

**Files:**
- Modify: production evidence record from Task 3

- [ ] **Step 1: 从平台权威日历枚举目标**

按 `target_date` 枚举 2026-06-01 至 2026-07-31 的应有周度目标，
必须得到 9 个目标；feature/target 依次为：

```text
2026-05-29 → 2026-06-05
2026-06-05 → 2026-06-12
2026-06-12 → 2026-06-18
2026-06-18 → 2026-06-26
2026-06-26 → 2026-07-03
2026-07-03 → 2026-07-10
2026-07-10 → 2026-07-17
2026-07-17 → 2026-07-24
2026-07-24 → 2026-07-31
```

逐条记录平台计算出的 `predict_date/feature_date/target_date`；不得从部署日、
ISO 周或简单 `+7` 猜测。

- [ ] **Step 2: 对每个点独立签发 token**

每个点 action 均为 `gray_backfill_write`，绑定 exact
`predict_date + scheme_version + harness_run_id`，TTL 900 秒。

- [ ] **Step 3: 按时间顺序执行 gray-backfill**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m harness gate gray-backfill \
  --scheme-id cgb_causal_wk_3y \
  --predict-date "$CGB3Y_GRAY_PREDICT_DATE" \
  --prediction-phase gray_live \
  --algo-env forecast_env_blackbox_v1 \
  --project-root \
  /Users/macstudio0/bond-factor-lab/.worktrees/blackbox-v2-3y-weekly-onboarding-20260730 \
  --authorize "$CGB3Y_GRAY_TOKEN"
```

任一点失败即停止后续点；不得重放 token 或覆盖已有 target。

- [ ] **Step 4: 读回 gray 数据**

要求每个目标 run/prediction/log 一一对应，全部 exact version，
历史/live target 重叠为零，旧或其他方案行数不变，`scheduled_live=0`。

### Task 7: 前端、证据、回归和 Git 发布

**Files:**
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/blackbox_v2/records/CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.md`
- Modify: `docs/blackbox_v2/records/CGB_CAUSAL_WK_3Y_ONBOARDING_20260730.evidence.json`
- Modify: `docs/blackbox_v2/records/README.md`
- Modify: `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md`
- Modify: `docs/superpowers/plans/README.md`

- [ ] **Step 1: 前端与 API 逐格验收**

运行 `scripts.verify_frontend_db`，显式传入新 canonical backtest run。
核对 metrics 的 `phase_ranges.gray_live`、dashboard 三个口径、部署时间和
3Y/weekly_point 格子；浏览器缓存不是验收依据。

- [ ] **Step 2: 写最终证据**

记录 exact version、两文件摘要、persisted harness run、generation、
combined snapshot、历史 run/count、gray run IDs/count、API/frontend
结果、CV2 snapshot 敏感性和 scheduler deferred。

- [ ] **Step 3: 最终回归**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_cgb_causal_wk_3y_onboarding.py \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py \
  tests/test_onboarding_docs.py \
  tests/test_backend_api.py \
  tests/test_postonboard_scripts.py
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m compileall -q scheduler harness scripts schemes/cgb_causal_wk_3y
git diff --check
```

- [ ] **Step 4: 提交最终证据**

```bash
git add docs/CURRENT_STATUS.md docs/blackbox_v2/records \
  docs/superpowers/plans/README.md
git commit -m "docs: finalize weekly 3y production onboarding"
```

- [ ] **Step 5: 合并与同步**

把 feature branch 快进合并到
`codex/audit-bugfixes-20260613`，在合并态重新跑目标回归；随后快进本地
`master`。推送开发分支、feature branch 和 `master`，不创建 PR。
