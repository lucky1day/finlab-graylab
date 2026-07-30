# cgb_causal_wk_1y Production Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `cgb_causal_wk_1y` 原样接入 Blackbox V2，完成周度生产准入，并准备按 `2025-01-01 / 2026-06-01 / 2026-08-01` 三个边界执行回测、灰度和正式调度。

**Architecture:** 复用现有 Intake、`api-wind-date-v1` 组合输入、Harness 授权 Gate、repository 和周度 recurring scheduler；不新增平台抽象，不修改算法。开发分支先形成 exact version `05022a0eeec7` 的可验证生产配置，生产写库只在分支验证后且用户明确确认合并 `master` 后执行。

**Tech Stack:** Python 3.12、Blackbox V2 Contract 1.0、FastAPI/SQLAlchemy、APScheduler、`pytest`/`unittest`。

## Global Constraints

- 交付脚本和 Metadata 必须原字节保存，SHA256 分别为 `90f3abcc1501eb7173c706fc2ad5d76fda89bf9004c88ee976b98378bbb6d450` 与 `efc8e03c5db98c33f0b830d62cc4465f7f890b5a783de68fee58e4ae19d4162b`。
- 只验证平台输入、Contract 输出、确定性、截止隔离和写库链路，不验证算法内部逻辑或效果。
- 历史回测从 `2025-01-01` 开始且只允许 `target_date < 2026-06-01`。
- `target_date >= 2026-06-01` 至 `2026-07-31` 的应有周度点全部写为 `gray_live`。
- 首条 `scheduled_live` 只能由 `2026-08-01` 自然调度产生，三日期为 `2026-08-01 / 2026-07-31 / 2026-08-07`。
- 未经再次明确确认，不合并或覆盖 `master`，不推送远程。

---

### Task 1: Intake 两文件并冻结 exact identity

**Files:**
- Create: `schemes/cgb_causal_wk_1y/config.yaml`
- Create: `schemes/cgb_causal_wk_1y/delivery/cgb_causal_wk_1y.py`
- Create: `schemes/cgb_causal_wk_1y/delivery/cgb_causal_wk_1y.json`
- Create: `tests/test_cgb_causal_wk_1y_onboarding.py`

**Interfaces:**
- Consumes: 上游目录 `/Users/macstudio0/Downloads/1Y周度_cgb_causal_wk_1y/delivery`
- Produces: `SchemeConfig` identity `cgb_causal_wk_1y@05022a0eeec7`

- [ ] **Step 1: 写 identity 红灯测试**

```python
def test_delivery_and_platform_identity_are_frozen() -> None:
    cfg = load_scheme_config(
        PROJECT_ROOT / "schemes/cgb_causal_wk_1y/config.yaml"
    )
    assert cfg.scheme_version == "05022a0eeec7"
    assert cfg.platform_inputs == ("api-wind-date-v1",)
    assert cfg.schedule.cron == "30 11 * * 6"
    assert cfg.task_type == "weekly_point"
    assert cfg.tenors == ["1Y"]
    assert _sha256(cfg.delivery_script) == (
        "90f3abcc1501eb7173c706fc2ad5d76fda89bf9004c88ee976b98378bbb6d450"
    )
    assert _sha256(cfg.delivery_metadata) == (
        "efc8e03c5db98c33f0b830d62cc4465f7f890b5a783de68fee58e4ae19d4162b"
    )
```

- [ ] **Step 2: 确认测试因方案尚不存在而失败**

Run:

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_cgb_causal_wk_1y_onboarding.py
```

Expected: FAIL，错误指向缺少 `schemes/cgb_causal_wk_1y/config.yaml`。

- [ ] **Step 3: 用 Intake 原子生成方案目录**

先用 `mktemp -d` 创建私有目录，只复制精确 `.py + .json`，再运行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness intake-blackbox \
    --delivery-dir "$BFL_CGB_WK_INTAKE_DIR" \
    --project-root /Users/macstudio0/bond-factor-lab/.worktrees/blackbox-v2-cgb-causal-wk-1y-onboarding-20260730 \
    --runtime-profile blackbox-v2-v1 \
    --data-schema-version data-bridge-v1 \
    --platform-input api-wind-date-v1
```

- [ ] **Step 4: 验证测试、字节摘要和 StaticGate**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_cgb_causal_wk_1y_onboarding.py \
  tests/test_blackbox_v2_intake.py \
  tests/test_blackbox_v2_discovery.py

conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate static \
    --scheme-id cgb_causal_wk_1y \
    --predict-date 2026-07-25 \
    --algo-env forecast_env_blackbox_v1
```

Expected: exact version `05022a0eeec7`，测试和 StaticGate 全部 PASS。

- [ ] **Step 5: 提交 Intake**

```bash
git add schemes/cgb_causal_wk_1y tests/test_cgb_causal_wk_1y_onboarding.py
git commit -m "feat: intake cgb causal weekly 1y"
```

### Task 2: 加入周度生产 scheduler 精确准入

**Files:**
- Modify: `scheduler/blackbox_scheduler_admission.py`
- Modify: `deploy/blackbox_scheduler_admission_v1.json`
- Modify: `tests/test_blackbox_scheduler_admission.py`
- Modify: `tests/test_scheduler_main.py`

**Interfaces:**
- Consumes: `cgb_causal_wk_1y@05022a0eeec7`
- Produces: formal weekly capabilities `legacy_automatic + recurring + direct_scheduled`

- [ ] **Step 1: 先扩展 expected identity 测试**

在两份测试的 formal weekly identity 中加入：

```python
("cgb_causal_wk_1y", "05022a0eeec7"): _expected_admission(
    mode="formal",
    frequency="weekly",
    task_type="weekly_point",
    horizon=1,
    target_tenor="1Y",
    capabilities=FORMAL_WEEKLY_CAPABILITIES,
)
```

并增加 scheduler wrapper 断言：exact version 可执行，版本漂移和 paused
状态均 fail-closed。

- [ ] **Step 2: 确认 policy 测试红灯**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py
```

Expected: FAIL，指出 deployed policy/expected identity 缺少
`cgb_causal_wk_1y@05022a0eeec7`。

- [ ] **Step 3: 增加机器准入**

在 Python freeze map 和 JSON 中加入完全相同的 formal weekly entry：

```json
{
  "scheme_id": "cgb_causal_wk_1y",
  "scheme_version": "05022a0eeec7",
  "runtime_type": "blackbox_v2",
  "frequency": "weekly",
  "task_type": "weekly_point",
  "horizon": 1,
  "target_tenor": "1Y",
  "mode": "formal",
  "capabilities": [
    "legacy_automatic",
    "recurring",
    "direct_scheduled"
  ]
}
```

- [ ] **Step 4: 跑 admission 和 scheduler 回归**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py \
  tests/test_cgb_causal_wk_1y_onboarding.py
```

Expected: PASS；scheme paused 时不实际挂载，active exact version 时按周六
`11:30` 原始 cron 进入 recurring 调度组。

- [ ] **Step 5: 提交生产准入**

```bash
git add scheduler/blackbox_scheduler_admission.py \
  deploy/blackbox_scheduler_admission_v1.json \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py
git commit -m "feat: admit cgb causal weekly 1y scheduler"
```

### Task 3: 技术 Gate、分支验收与生产切换检查点

**Files:**
- Create: `docs/blackbox_v2/records/CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md`
- Create: `docs/blackbox_v2/records/CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.evidence.json`
- Modify: `docs/blackbox_v2/records/README.md`
- Modify: `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `tests/test_onboarding_docs.py`

**Interfaces:**
- Consumes: exact scheme identity、最新完整 DataBridge generation、Harness Gate
- Produces: 可复核分支和生产操作清单；此任务本身不合并 `master`

- [ ] **Step 1: 环境与 DataBridge 只读预检**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/verify_blackbox_v2_environment.py
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/probe_blackbox_v2_sandbox.py
```

读取 current state 后，使用其精确 `refresh_date` 执行
`scripts/refresh_data_bridge_current.py --check-only`；不得发布新 generation。

- [ ] **Step 2: 执行七 Gate 技术验收**

使用最新可运行周度信号日 `2026-07-25`：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness onboard cgb_causal_wk_1y \
    --predict-date 2026-07-25 \
    --stage all \
    --check-only \
    --algo-env forecast_env_blackbox_v1 \
    --timeout-sec 1800
```

Expected: 7/7 PASS、Backtest 100/100、`persist=false`、控制面和业务表零写入。
若交付算法因最新快照或 cutoff 失败，停止上线，不修改算法贴结果。

- [ ] **Step 3: 记录证据并执行完整回归**

证据必须记录 exact version、两文件 SHA、generation/snapshot、七 Gate、
`gray_target_start=2026-06-01`、首条正式三日期和“未验证算法逻辑”边界。

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q
python -m compileall -q shared scheduler harness backtests backend \
  schemes/cgb_causal_wk_1y
git diff --check
```

- [ ] **Step 4: 提交证据并请求 master 合并确认**

```bash
git add docs/blackbox_v2/records docs/CURRENT_STATUS.md \
  tests/test_onboarding_docs.py
git commit -m "docs: record cgb causal weekly 1y readiness"
```

此时报告 commit、测试和 Gate；等待用户明确确认后才合并 `master`。

- [ ] **Step 5: 确认后执行受控生产动作**

按当前 SOP 顺序签发 exact scheme/version/run 的一次性 token，并执行：

1. shadow 登记；
2. `blackbox_activate`；
3. `backtest_persist`：`--backtest-start-date 2025-01-01`，
   `--predict-date 2026-06-01`；
4. 从平台日历枚举 `2026-06-01 <= target_date <= 2026-07-31` 的每个周度点，
   每点独立 `gray_backfill_write` token 和 `gray-backfill` Gate；
5. 重启/重载生产 scheduler 并验证 exact identity 已挂载；
6. 验证 Registry、三组 API、前端、actual join 和 backtest/live 零重叠。

任一步失败立即停止；不跳过 gray 缺口，不直接改库，不把人工执行标记为
`scheduled_live`。

- [ ] **Step 6: 观察首条自然调度**

在 `2026-08-01` 自然调度后验证：

```text
predict_date=2026-08-01
feature_date=2026-07-31
target_date=2026-08-07
prediction_phase=scheduled_live
```

只有 run、log、prediction、API 和前端均可追溯时，状态才从
`Onboarding Complete` 更新为 `Production Observed`。
