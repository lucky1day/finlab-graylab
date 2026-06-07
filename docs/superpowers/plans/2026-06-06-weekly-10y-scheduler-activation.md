# Weekly 10Y Scheduler Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `weekly_10y_d_overlay` 从已完成受控 live 写库验收的 `paused` 状态，推进到可由 launchd scheduler 每周六 11:30 自动调度。

**Architecture:** 不改源数据表，不跑 broad scheduler run-once，不触发其他方案。唯一的功能配置变更是将 `schemes/weekly_10y_d_overlay/config.yaml` 的 `status` 从 `paused` 改为 `active`，并通过 scheduler 重启让 APScheduler 重新发现该方案。

**Tech Stack:** Python 3.12, conda `forecast_env`, conda `bond_factor_lab_service`, MySQL 8.0 `bond_db`, FastAPI, APScheduler, launchd.

---

## Safety Boundary

- 允许写入: 仅由自动调度或专用 live writer 写入 `scheme_id='weekly_10y_d_overlay'` 的 `t_scheme_predictions` 和 `t_scheme_run_log`。
- 允许 registry 变化: scheduler 重启会同步 `t_scheme_registry`；代码需保证未变化的方案不刷新 `updated_at`，本次只允许 `weekly_10y_d_overlay` 因 `paused -> active` 产生 registry 变化。
- 禁止写入: `api_wind_daily`、`api_wind_weekly`、`api_wind_derivative_daily`、`api_wind_derivative_weekly`、`t_trade_calendar`、其他方案的 prediction/run_log/actuals。
- 禁止命令: `scripts/apply_migrations.py`、`scheduler.executor` broad run、`scheduler.actuals_updater`、`scheduler.main --run-once`、`POST /api/schemes/{scheme_id}/trigger`。
- 允许只读命令: readiness、dry-run、`SELECT` 计数、API health/metrics 查询、launchd 状态和日志查看。

## File Map

- Modify: `/Users/macstudio0/bond-factor-lab/schemes/weekly_10y_d_overlay/config.yaml`
  - Responsibility: weekly 10Y 方案的调度时间和启停状态。
- Modify: `/Users/macstudio0/bond-factor-lab/tests/test_weekly_10y_integration.py`
  - Responsibility: 固化 weekly 10Y 配置预期，激活后应断言 `status: active`。
- Modify: `/Users/macstudio0/bond-factor-lab/scheduler/repository.py`
  - Responsibility: registry 同步只在方案元数据真实变化时刷新 `updated_at`。
- Create: `/Users/macstudio0/bond-factor-lab/tests/test_repository_registry.py`
  - Responsibility: 防止 registry 同步对未变化方案产生无意义更新时间刷新。
- Modify: `/Users/macstudio0/bond-factor-lab/docs/CURRENT_STATUS.md`
  - Responsibility: 记录上线状态、启用时间、验证证据。
- Modify: `/Users/macstudio0/bond-factor-lab/docs/IMPLEMENTATION_PLAN.md`
  - Responsibility: 将周度 live 剩余项从“待启用”推进到“已启用/待观察首次自动运行”。
- Modify: `/Users/macstudio0/bond-factor-lab/docs/WEEKLY_LIVE_ROLLOUT_PLAN.md`
  - Responsibility: 勾选自动调度启用步骤，保留回滚命令。

---

### Task 0: Registry Sync Safety Guard

**Files:**
- Modify: `/Users/macstudio0/bond-factor-lab/scheduler/repository.py`
- Create: `/Users/macstudio0/bond-factor-lab/tests/test_repository_registry.py`

- [x] **Step 1: Write failing test for unchanged registry rows**

`tests/test_repository_registry.py` asserts that `sync_scheme_registry()` emits an `updated_at = IF(...)` guard before metadata assignments and no longer contains unconditional `updated_at = CURRENT_TIMESTAMP`.

- [x] **Step 2: Verify the test fails against the old SQL**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m unittest tests.test_repository_registry
```

Observed:

```text
FAIL because the old SQL used unconditional updated_at = CURRENT_TIMESTAMP.
```

- [x] **Step 3: Update registry UPSERT to refresh `updated_at` only when metadata changes**

`scheduler/repository.py` now computes the metadata diff before assigning new values, so unchanged `t1_daily` / `t5_daily` registry rows are not refreshed when scheduler restarts.

- [x] **Step 4: Verify tests and MySQL syntax**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m unittest tests.test_repository_registry
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python <temporary-table syntax check>
```

Observed:

```text
tests.test_repository_registry OK
temp registry upsert syntax ok
```

### Task 1: Pre-Activation Readiness Snapshot

**Files:**
- Read: `/Users/macstudio0/bond-factor-lab/schemes/weekly_10y_d_overlay/config.yaml`
- Read: `/Users/macstudio0/bond-factor-lab/scripts/check_weekly_10y_readiness.py`
- Read: `/Users/macstudio0/bond-factor-lab/scheduler/scheme_runner.py`

- [ ] **Step 1: Confirm current weekly config is still paused**

Run:

```bash
sed -n '1,40p' /Users/macstudio0/bond-factor-lab/schemes/weekly_10y_d_overlay/config.yaml
```

Expected:

```text
cron: "30 11 * * 6"
status: paused
```

- [ ] **Step 2: Capture platform table counts before activation**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -c "from sqlalchemy import text; from scheduler.repository import create_engine_from_env; engine=create_engine_from_env(); sql=text(\"\"\"SELECT 't_scheme_predictions' table_name, COUNT(*) row_count FROM t_scheme_predictions UNION ALL SELECT 't_scheme_run_log', COUNT(*) FROM t_scheme_run_log UNION ALL SELECT 't_scheme_actuals', COUNT(*) FROM t_scheme_actuals UNION ALL SELECT 't_scheme_weekly_actuals', COUNT(*) FROM t_scheme_weekly_actuals UNION ALL SELECT 'weekly_live_predictions', COUNT(*) FROM t_scheme_predictions WHERE scheme_id='weekly_10y_d_overlay'\"\"\"); conn=engine.connect(); [print(dict(r._mapping)) for r in conn.execute(sql)]; conn.close()"
```

Expected:

```text
Prints five count rows. No INSERT, UPDATE, DELETE, ALTER, or DROP occurs.
```

- [ ] **Step 3: Run read-only weekly readiness for the target Saturday**

For the current launch date `2026-06-06`, run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m scripts.check_weekly_10y_readiness --predict-date 2026-06-06
```

Expected:

```text
ready=true
scheme_id=weekly_10y_d_overlay
feature_date=2026-06-05
target_date=2026-06-12
```

- [ ] **Step 4: Run read-only dry-run**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id weekly_10y_d_overlay --predict-date 2026-06-06
```

Expected:

```text
Returns one PredictionRecord JSON row with target_tenor=10Y, horizon=6, predict_date=2026-06-06, target_date=2026-06-12.
```

- [ ] **Step 5: Re-capture table counts and compare**

Run the same command from Step 2.

Expected:

```text
All five counts match Step 2 exactly. If any count changes, stop and investigate before activation.
```

---

### Task 2: Activate Only the Weekly 10Y Scheme

**Files:**
- Modify: `/Users/macstudio0/bond-factor-lab/schemes/weekly_10y_d_overlay/config.yaml`
- Modify: `/Users/macstudio0/bond-factor-lab/tests/test_weekly_10y_integration.py`

- [ ] **Step 1: Write the failing test expectation**

Modify `/Users/macstudio0/bond-factor-lab/tests/test_weekly_10y_integration.py` in `test_scheme_files_exist_and_config_declares_weekly_10y`:

```python
self.assertIn("status: active", text)
```

Remove the old line:

```python
self.assertIn("status: paused", text)
```

- [ ] **Step 2: Run the test to verify it fails before config activation**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m unittest tests.test_weekly_10y_integration.Weekly10YIntegrationTests.test_scheme_files_exist_and_config_declares_weekly_10y
```

Expected:

```text
FAIL because config.yaml still contains status: paused.
```

- [ ] **Step 3: Activate only this scheme**

Modify `/Users/macstudio0/bond-factor-lab/schemes/weekly_10y_d_overlay/config.yaml`:

```yaml
status: active
```

Keep the schedule unchanged:

```yaml
schedule:
  cron: "30 11 * * 6"
  timezone: "Asia/Shanghai"
```

- [ ] **Step 4: Verify discovery sees weekly scheme as active**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -c "from scheduler.discovery import discover_schemes; s={x.scheme_id:x for x in discover_schemes()}; w=s['weekly_10y_d_overlay']; print(w.schedule.cron, w.status, w.schedule.timezone)"
```

Expected:

```text
30 11 * * 6 active Asia/Shanghai
```

- [ ] **Step 5: Run weekly integration tests**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m unittest tests.test_weekly_10y_integration
```

Expected:

```text
Ran 12 tests
OK
```

---

### Task 3: Restart Scheduler and Verify Job Registration

**Files:**
- Read: `/Users/macstudio0/bond-factor-lab/deploy/launchd/com.bond-factor-lab.scheduler.plist`
- Read: `/tmp/bond-factor-lab-scheduler.err`
- Read: `/tmp/bond-factor-lab-scheduler.log`

- [ ] **Step 1: Confirm launchd currently manages scheduler**

Run:

```bash
launchctl list | rg 'com.bond-factor-lab.scheduler|PID|Status'
```

Expected:

```text
Shows com.bond-factor-lab.scheduler.
```

- [ ] **Step 2: Restart only the scheduler service**

Run:

```bash
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler
```

Expected:

```text
Command exits 0. Backend is not restarted in this step.
```

- [ ] **Step 3: Confirm scheduler registered weekly job**

Run:

```bash
tail -n 160 /tmp/bond-factor-lab-scheduler.err
```

Expected:

```text
Scheduled scheme weekly_10y_d_overlay at 30 11 * * 6
```

- [ ] **Step 4: Confirm no immediate unintended weekly write occurred**

Run the count query from Task 1 Step 2.

Expected:

```text
weekly_live_predictions is unchanged immediately after restart unless the restart happened exactly inside the scheduled fire window.
```

---

### Task 4: API and Frontend Smoke Check

**Files:**
- Read: `/Users/macstudio0/bond-factor-lab/backend/main.py`
- Read: `/Users/macstudio0/bond-factor-lab/frontend/aifin-shell.js`

- [ ] **Step 1: Verify backend health**

Run:

```bash
curl -sS http://127.0.0.1:8100/api/health
```

Expected:

```json
{"status":"ok"}
```

- [ ] **Step 2: Verify weekly metrics endpoint**

Run:

```bash
curl -sS 'http://127.0.0.1:8100/api/metrics/weekly_10y_d_overlay?tenor=10Y'
```

Expected:

```text
Returns JSON. Missing future actual for target_date=2026-06-12 must not produce a 500.
```

- [ ] **Step 3: Verify factor lab endpoint still includes weekly matrix data**

Run:

```bash
curl -sS http://127.0.0.1:8100/api/backtests/factor-lab
```

Expected:

```text
Returns JSON containing weekly_10y_d_overlay or the weekly 10Y backtest matrix data.
```

- [ ] **Step 4: Browser check**

Open:

```text
http://127.0.0.1:8100/
```

Expected:

```text
The page still shows the title "预测准确率矩阵" and the weekly 10Y cell remains visible.
```

---

### Task 5: First Automatic Run Observation

**Files:**
- Read: `/tmp/bond-factor-lab-scheduler.err`
- Read: `/tmp/bond-factor-lab-scheduler.log`

- [ ] **Step 1: Before the next Saturday 11:30 run, record counts**

Run the count query from Task 1 Step 2.

Expected:

```text
Counts are recorded before the scheduled run.
```

- [ ] **Step 2: After Saturday 11:30, inspect scheduler logs**

Run:

```bash
tail -n 220 /tmp/bond-factor-lab-scheduler.err
```

Expected:

```text
The log shows weekly_10y_d_overlay ran through scheduler discovery and completed without triggering other paused schemes.
```

- [ ] **Step 3: Verify only weekly 10Y live tables changed**

Run the count query from Task 1 Step 2.

Expected:

```text
t_scheme_predictions increases by 0 or 1 because prediction writes use UPSERT.
t_scheme_run_log increases by 1 for scheme_id='weekly_10y_d_overlay'.
t_scheme_actuals and t_scheme_weekly_actuals are unchanged by this prediction run.
```

- [ ] **Step 4: Verify latest weekly prediction row**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -c "from sqlalchemy import text; from scheduler.repository import create_engine_from_env; engine=create_engine_from_env(); sql=text(\"\"\"SELECT scheme_id, predict_date, target_date, target_tenor, horizon, predicted_direction, model_version FROM t_scheme_predictions WHERE scheme_id='weekly_10y_d_overlay' ORDER BY created_at DESC LIMIT 3\"\"\"); conn=engine.connect(); [print(dict(r._mapping)) for r in conn.execute(sql)]; conn.close()"
```

Expected:

```text
Rows belong only to scheme_id='weekly_10y_d_overlay'.
target_tenor is 10Y.
horizon is 6.
```

---

### Task 6: Documentation Closeout and Rollback Path

**Files:**
- Modify: `/Users/macstudio0/bond-factor-lab/docs/CURRENT_STATUS.md`
- Modify: `/Users/macstudio0/bond-factor-lab/docs/IMPLEMENTATION_PLAN.md`
- Modify: `/Users/macstudio0/bond-factor-lab/docs/WEEKLY_LIVE_ROLLOUT_PLAN.md`

- [ ] **Step 1: Update current status after activation**

In `/Users/macstudio0/bond-factor-lab/docs/CURRENT_STATUS.md`, update the weekly status paragraph to state:

```markdown
`weekly_10y_d_overlay` 已从 `paused` 切换为 `active`，调度时间为周六 11:30（`30 11 * * 6`），对齐旧实盘 weekly `multi` 首轮预测时间。
```

- [ ] **Step 2: Update implementation checklist**

In `/Users/macstudio0/bond-factor-lab/docs/IMPLEMENTATION_PLAN.md`, mark the activation item as done:

```markdown
- [x] 验收通过后，已把 `schemes/weekly_10y_d_overlay/config.yaml` 的 `status` 从 `paused` 改为 `active`。
- [x] 已重启 scheduler 并确认注册 `Scheduled scheme weekly_10y_d_overlay at 30 11 * * 6`。
```

- [ ] **Step 3: Update rollout plan**

In `/Users/macstudio0/bond-factor-lab/docs/WEEKLY_LIVE_ROLLOUT_PLAN.md`, add the exact activation timestamp and verification commands used.

- [ ] **Step 4: Record rollback command**

Add this rollback section to `/Users/macstudio0/bond-factor-lab/docs/WEEKLY_LIVE_ROLLOUT_PLAN.md`:

````markdown
## Rollback

If weekly automatic scheduling must be disabled:

1. Change `schemes/weekly_10y_d_overlay/config.yaml` back to `status: paused`.
2. Restart scheduler:

```bash
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler
```

3. Verify scheduler logs no longer register `weekly_10y_d_overlay`.
4. Do not delete existing `t_scheme_predictions` or `t_scheme_run_log`; keep audit history intact.
````

- [ ] **Step 5: Run final verification**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m unittest tests.test_weekly_10y_integration
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m unittest tests.test_weekly_10y_live_ops tests.test_weekly_actuals tests.test_weekly_metrics tests.test_backtest_factor_lab_readonly
```

Expected:

```text
Both commands exit 0 and report OK.
```

---

## Self-Review

- Spec coverage: The plan covers readiness, activation, scheduler restart, API/frontend smoke check, first automatic run observation, documentation, and rollback.
- Placeholder scan: No placeholder markers or vague implementation steps remain.
- Type consistency: The scheme id is consistently `weekly_10y_d_overlay`; cron is consistently `30 11 * * 6`; target tenor is consistently `10Y`; horizon is consistently `6`.
- Safety check: No task writes source tables or runs broad scheduler commands. The only activation write is the config status change, followed by controlled scheduler restart.
