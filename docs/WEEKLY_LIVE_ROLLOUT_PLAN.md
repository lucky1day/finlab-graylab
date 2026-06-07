# Weekly 10Y Live Rollout Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不影响源表和现有生产数据的前提下，把 `weekly_10y_d_overlay` 从 paused 历史回测方案推进到可受控 live 写库并可自动调度的周度方案。

**Architecture:** 周度方案继续沿用现有 `schemes/weekly_10y_d_overlay/predict.py` 标准入口。live 启用分三道门: 源数据 readiness、只读 dry-run、受控写库验收；全部通过后才允许把 `config.yaml.status` 改为 `active` 并重启 scheduler。

**Tech Stack:** Python 3.12, conda `forecast_env`, conda `bond_factor_lab_service`, MySQL 8.0 `bond_db`, FastAPI backend, APScheduler launchd scheduler.

---

## Current Status

截至 `2026-06-06`，周度 live 预测已通过 readiness、dry-run、受控 live 写库验收，并已启用自动 scheduler 调度。

- `weekly_10y_d_overlay` 当前 `status: active`。
- 调度时间已预设为 `30 11 * * 6`，对齐旧实盘 cron `30 11 * * 6 bash /Users/macstudio0/bondprojectpro/forecast_project/weekly_project/run_weekly_pipeline.sh multi` 的周六 11:30 首轮预测时间；旧脚本 16:00 / 22:00 为检查和必要补跑节点。
- live 预测按 `feature_date` 从源表反查实际 `week_id`；`2026-06-06` 的源表 `feature_week_id=202621`。
- 若 `api_wind_derivative_weekly` 未物化本周 `TB0YWI3C/TB1YWI3C/TB5YWI3C`，adapter 会只读 `api_wind_daily`，按 weekly close 定义在内存中补齐，不写回源表。
- `2026-06-06` readiness 已返回 `ready=true`，dry-run 已返回 1 条 10Y 预测。
- `2026-06-06` 受控 live 写库已成功，`t_scheme_predictions` 中已有 1 条 `weekly_10y_d_overlay` live 记录。
- `2026-06-06 15:05` 已重启 scheduler，日志确认 `Scheduled scheme weekly_10y_d_overlay at 30 11 * * 6`。
- `2026-06-06 15:24` 已通过单方案 scheduler 手动补跑，返回 `SchemeRunResult(... status='success', records_written=1 ...)`。
- 周度 scheduler job 已设置 `force=True`，避免周六预测被通用非交易日判断跳过；日度方案仍不强制。
- registry 同步已加保护: 未变化方案不刷新 `t_scheme_registry.updated_at`；本次重启后 `t1_daily` / `t5_daily` registry 时间未变，仅周度方案同步为 `active` / `30 11 * * 6`。
- 已新增只读 readiness 命令: `scripts/check_weekly_10y_readiness.py`。
- 已新增受控 live 写库命令: `scripts/write_weekly_10y_live_prediction.py`；该命令会先执行 readiness 和 dry-run，失败则不写库。

## Safety Rules

- 不修改 `api_wind_*`、`api_wind_derivative_*`、`t_trade_calendar` 等源表。
- 不在 readiness 未通过前写 `t_scheme_predictions` / `t_scheme_run_log` 的周度 live 记录。
- 不用 `scheduler.executor --include-paused` 做 readiness 检查；该路径会写 run_log。
- 不把 `weekly_10y_d_overlay` 改为 `active`，直到受控 live 写库验收通过；当前该条件已满足并已启用。
- 只读核验只使用 `SELECT`、`information_schema` 和 `scheduler.scheme_runner` dry-run。
- 可写步骤只允许在明确验收窗口内写 `scheme_id='weekly_10y_d_overlay'` 对应的一条预测和一条 run_log；执行前后必须记录核心表行数。

## Phase 1: Source Readiness

- [x] **Step 1: 计算待预测周六的 feature week**

使用周度实盘 week_id 规则:

| predict_date | feature_week_id | feature_date | target_week_id | target_date |
|--------------|-----------------|--------------|----------------|-------------|
| `2026-05-30` | `202620` | `2026-05-29` | `202621` | `2026-06-05` |
| `2026-06-06` | `202621` | `2026-06-05` | `202622` | `2026-06-12` |

- [x] **Step 2: 只读检查关键指标代码覆盖**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m scripts.check_weekly_10y_readiness --predict-date <target-saturday>
```

Expected before continuing:

```text
ready=true
```

Actual 2026-06-06 result:

```text
ready=true
predict_date=2026-06-06
feature_week_id=202621
feature_date=2026-06-05
target_week_id=202622
target_date=2026-06-12
latest_complete_required_week_id=202621
latest_supported_predict_date=2026-06-06
```

- [x] **Step 3: 如果 latest complete week 不足，停止**

Stop condition:

```text
latest_complete_required_week_id < target feature_week_id
```

Action:

- 不写任何 live 预测。
- 保持 `weekly_10y_d_overlay.status=paused`。
- 更新 `docs/CURRENT_STATUS.md` 记录最新阻塞周。

Actual 2026-06-06 result: 未触发停止条件。

## Phase 1.5: Upstream Schedule Gate

- [x] **Step 1: 确认上游 weekly cron 仍是 multi 模式**

已在 2026-06-06 通过 `crontab -l` 只读复核，当前机器仍配置以下 weekly 实盘任务:

Reference cron:

```cron
30 11 * * 6 bash /Users/macstudio0/bondprojectpro/forecast_project/weekly_project/run_weekly_pipeline.sh multi
```

Reference script behavior:

```text
11:30 run_main_process
16:00 check_db_results; rerun if W1Y/W5Y/W10Y missing
22:00 check_db_results; rerun if W1Y/W5Y/W10Y missing
multi_final check_db_results
```

- [x] **Step 2: 保持 BFL 周度 live cron 对齐上游首轮预测**

Expected:

```yaml
schedule:
  cron: "30 11 * * 6"
  timezone: "Asia/Shanghai"
```

Rationale:

- `30 11 * * 6` 与旧实盘 weekly `multi` 首轮预测时间一致。
- 旧脚本内部的 16:00 / 22:00 是检查和补跑，不作为 BFL 首轮 live cron。
- 2026-06-06 配置解析结果为 `cron=30 11 * * 6`、`status=active`、`timezone=Asia/Shanghai`。
- 如果上游 cron 改动，先同步 BFL cron，再执行 active。

## Phase 2: Dry-Run Gate

- [x] **Step 1: 跑目标周六 dry-run**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id weekly_10y_d_overlay --predict-date <target-saturday>
```

Expected:

```json
[
  {
    "scheme_id": "weekly_10y_d_overlay",
    "target_tenor": "10Y",
    "horizon": 6,
    "predict_date": "<target-saturday>",
    "target_date": "<next-week-last-trading-day>",
    "predicted_direction": -1,
    "extra": {
      "feature_week_id": <target feature_week_id>,
      "target_week_id": <target week_id>
    }
  }
]
```

Actual 2026-06-06 result:

```text
scheme_id=weekly_10y_d_overlay
target_tenor=10Y
horizon=6
predict_date=2026-06-06
target_date=2026-06-12
predicted_direction=-1
confidence=0.28
extra.feature_week_id=202621
extra.target_week_id=202622
```

- [x] **Step 2: 核验 dry-run 不写库**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -c "from sqlalchemy import text; from scheduler.repository import create_engine_from_env; engine = create_engine_from_env();
with engine.connect() as conn:
    print('weekly_live_predictions=', conn.execute(text('SELECT COUNT(*) FROM t_scheme_predictions WHERE scheme_id=:scheme_id'), {'scheme_id':'weekly_10y_d_overlay'}).scalar_one())
    print('run_log_rows=', conn.execute(text('SELECT COUNT(*) FROM t_scheme_run_log')).scalar_one())"
```

Expected:

```text
weekly_live_predictions=0
run_log_rows=<unchanged baseline>
```

Actual 2026-06-06 result: dry-run 前后核心表行数不变。

## Phase 3: Controlled Live Write Validation

Only start this phase after Phase 1 and Phase 2 pass.

- [x] **Step 1: Record pre-write baseline**

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -c "from sqlalchemy import text; from scheduler.repository import create_engine_from_env; engine = create_engine_from_env();
with engine.connect() as conn:
    for table in ['t_scheme_predictions','t_scheme_run_log','t_scheme_actuals','t_scheme_weekly_actuals']:
        print(table, conn.execute(text('SELECT COUNT(*) FROM ' + table)).scalar_one())"
```

- [x] **Step 2: Perform one controlled write**

Preferred implementation path:

Run:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m scripts.write_weekly_10y_live_prediction --predict-date <target-saturday>
```

Expected command behavior:

1. Reject any `--scheme-id` other than `weekly_10y_d_overlay`.
2. Re-run readiness against source tables using only SELECT.
3. Run `scheduler.scheme_runner` in `forecast_env` and validate exactly one 10Y weekly record.
4. Write exactly one `t_scheme_predictions` row with `scheme_id='weekly_10y_d_overlay'` via UPSERT.
5. Write exactly one `t_scheme_run_log` row for `weekly_10y_d_overlay`.
6. Do not change `config.yaml.status` in this step.

Do not use broad scheduler run-once commands for this phase.

- [x] **Step 3: Verify post-write row counts**

Expected:

```text
t_scheme_predictions: +1 or unchanged by UPSERT for the same predict_date
t_scheme_run_log: +1
t_scheme_actuals: unchanged
t_scheme_weekly_actuals: unchanged
source tables: unchanged
```

- Actual 2026-06-06 controlled writer result before manual scheduler catch-up:

```text
t_scheme_predictions=7
t_scheme_run_log=3
t_scheme_actuals=13900
t_scheme_weekly_actuals=794
weekly_live_predictions=1
prediction: predict_date=2026-06-06, target_date=2026-06-12, predicted_direction=-1, confidence=0.28
```

- [x] **Step 4: Verify API metrics does not error**

Run:

```bash
curl -sS 'http://127.0.0.1:8100/api/metrics/weekly_10y_d_overlay?tenor=10Y'
```

Expected:

- API returns HTTP 200.
- If target actual is not yet available, samples may remain `0`.
- Once `t_scheme_weekly_actuals` has the matching target week, metrics should count the sample.

Actual 2026-06-06 result:

```text
/api/health -> 200
/api/metrics/weekly_10y_d_overlay?tenor=10Y -> 200
/api/backtests/factor-lab -> 200
metrics daily_rows=1, summary_samples=0 because target actual for 2026-06-12 is not available yet
factor-lab weekly backtest returns 68.9% over 45 samples after wind_export(1) common input switch
```

## Phase 4: Activate Scheduler

Only start this phase after controlled live write validation passes.

- [x] **Step 1: Change status**

Modify:

```yaml
status: active
```

in:

```text
schemes/weekly_10y_d_overlay/config.yaml
```

- [x] **Step 2: Restart scheduler**

Run:

```bash
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler
```

- [x] **Step 3: Confirm scheduled jobs**

Run:

```bash
tail -n 120 /tmp/bond-factor-lab-scheduler.err
```

Expected:

```text
Scheduled scheme weekly_10y_d_overlay at 30 11 * * 6
```

Actual 2026-06-06 result:

```text
2026-06-06 15:05:15 INFO __main__: Scheduled scheme weekly_10y_d_overlay at 30 11 * * 6
```

Registry verification after restart:

```text
t1_daily updated_at unchanged: 2026-06-05 22:11:06
t5_daily updated_at unchanged: 2026-06-05 22:11:06
weekly_10y_d_overlay status=active, schedule_cron=30 11 * * 6, updated_at=2026-06-06 15:05:15
```

Immediate write check after restart, before manual scheduler catch-up:

```text
t_scheme_predictions=7
t_scheme_run_log=3
t_scheme_actuals=13900
t_scheme_weekly_actuals=794
weekly_live_predictions=1
```

Manual scheduler catch-up after missed 11:30 window:

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -m scheduler.main \
  --run-once predictions \
  --scheme-id weekly_10y_d_overlay \
  --date 2026-06-06 \
  --force
```

Actual 2026-06-06 result:

```text
SchemeRunResult(scheme_id='weekly_10y_d_overlay', status='success', records_written=1, duration_sec=24.9306, error_msg=None)
t_scheme_predictions=7
t_scheme_run_log=4
t_scheme_actuals=13900
t_scheme_weekly_actuals=794
weekly_live_predictions=1
weekly_run_log=2
```

- [ ] **Step 4: Observe next Saturday trigger**

This is the only remaining weekly live rollout observation item.

After the next scheduled run:

```sql
SELECT scheme_id, run_date, status, error_msg, created_at
FROM t_scheme_run_log
WHERE scheme_id='weekly_10y_d_overlay'
ORDER BY id DESC
LIMIT 5;
```

Expected:

```text
status='success'
```

Additional checks:

```text
t_scheme_predictions: +1 or unchanged by UPSERT
t_scheme_run_log: +1 for scheme_id='weekly_10y_d_overlay'
t_scheme_actuals: unchanged by prediction run
t_scheme_weekly_actuals: unchanged by prediction run
source tables: unchanged
```

## Phase 4.5: Target Actual Observation

- [ ] **Step 1: Wait for target actual**

Target pair:

```text
predict_date=2026-06-06
target_date=2026-06-12
target_week_id=202622
```

Expected after upstream actuals are available:

```text
/api/metrics/weekly_10y_d_overlay?tenor=10Y
```

returns the 2026-06-06 live sample with non-null `actual_direction` / `is_correct`, and summary samples increases from 0.

## Phase 5: Documentation Closeout

- [x] Update `docs/CURRENT_STATUS.md` with the new live status.
- [x] Update `docs/TEST_PLAN.md` Phase 3.5 and Phase 4 write validation records.
- [x] Update `docs/TEST_MACHINE_BASELINE.md` with row counts and scheduler status.
- [x] Keep `docs/WEEKLY_LIVE_ROLLOUT_PLAN.md` as the audit trail for the rollout.

## Rollback

If weekly automatic scheduling must be disabled:

1. Change `schemes/weekly_10y_d_overlay/config.yaml` back to `status: paused`.
2. Restart scheduler:

```bash
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler
```

3. Verify scheduler logs no longer register `weekly_10y_d_overlay`.
4. Do not delete existing `t_scheme_predictions` or `t_scheme_run_log`; keep audit history intact.
