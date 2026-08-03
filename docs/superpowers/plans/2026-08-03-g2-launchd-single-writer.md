# G2 Launchd Single-Writer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将仓库期望拓扑收敛为 launchd one-shot 的 DataBridge、daily、weekly、monthly 与 actuals writer，并使旧 resident scheduler、daily-gray、v2-preflight 不再拥有生产写权。

**Architecture:** `scripts/refresh_data_bridge_current.py --publish` 是唯一 DataBridge/gate producer；其 repo plist 只在 06:30 启动一次，且在进入 publish 前需要一个明确的 launchd one-shot 操作准入标记。新增轻量的 cadence runner 在每次由 launchd 启动时动态发现 active 方案、通过既有 lifecycle/admission 验证并以 `scheduled_live` 执行；它不持有 cron、ledger、epoch 或 startup catch-up。旧路径在源码与 repo plist 中 fail-closed/disabled，但 installed plist、`launchctl`、真实发布与 live 写入仍留给专项生产授权。

**Tech Stack:** Python 3.12、FastAPI 平台现有 scheduler/executor、MySQL read/write repository、macOS launchd plist、`unittest`/`pytest`。

---

## 约束与非目标

- 不读取或改写 installed plist；不运行 `launchctl`；不执行真实 `--publish`、live write、Registry 变更或历史补数。
- 不新增、扩容、迁移或重建 ledger / occurrence / epoch；不把它们作为任何过渡机制。
- `BFL_DATABRIDGE_PRODUCER=launchd-one-shot` 仅是防止误从普通 shell publish 的**操作准入标记**，不是 launchd 身份认证；不能把它或 repo plist 当作真实挂载证据。
- 历史 `daily-gray`、`v2-preflight`、resident `scheduler.main` 代码可以留待 G8 删除，但在 G2 target template/入口中不得继续具有 writer 能力。

## 任务 1：先用测试封住 legacy DataBridge 与 restart 路径

**Files:**

- Modify: `tests/test_scheduler_main.py`
- Modify: `tests/test_v2_daily_preflight.py`
- Modify: `scheduler/main.py`
- Modify: `scheduler/v2_daily_preflight.py`

- [x] **Step 1: 写出 legacy entrypoint 的失败测试。**

  覆盖下列契约：

  ```python
  def test_legacy_data_bridge_writer_is_retired():
      with self.assertRaisesRegex(LegacySchedulerWriterRetired, "launchd one-shot"):
          scheduler_main.run_data_bridge_refresh_job("2026-07-29")

  def test_startup_tasks_never_refresh_or_prediction_catchup():
      scheduler_main.run_startup_tasks(now=known_time)
      refresh.assert_not_called()
      catchup.assert_not_called()

  def test_v2_preflight_is_retired_without_gate_or_restart():
      payload = run_phase("refresh-primary", now=known_time)
      self.assertEqual(payload["status"], "retired")
      write_gate.assert_not_called()
      restart.assert_not_called()
  ```

- [x] **Step 2: 运行红测。**

  Run: `PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_scheduler_main.py tests/test_v2_daily_preflight.py -q`

  Expected: 新增断言因旧 HTTP refresh、startup catch-up 或 preflight restart 仍存在而失败。

- [x] **Step 3: 最小实现 retire 语义。**

  - 从 `scheduler/main.py` 删除 HTTP `DataBridgeClient`/`run_full_refresh` writer 依赖；保留只读 `data_bridge_refresh_is_current()` 仅供诊断，不得由 startup 自动调用。
  - `run_data_bridge_refresh_job()` 明确抛出专用 `LegacySchedulerWriterRetired`，错误信息指向 one-shot publisher；`run_startup_tasks()` 只记录 retired，不执行 refresh 或 prediction catch-up。
  - `scheduler.v2_daily_preflight.run_phase()` 与 `main()` 返回稳定的 `retired` JSON，且不构造 dependencies、不写 gate、不调用 restart。保留旧 helper 仅为 G8 可删除债务，不让正常入口抵达它们。
  - `scheduler.main --run-once data-refresh` 返回非零且不写；`--run-once actuals` 继续保留。

- [x] **Step 4: 运行绿测并进行静态守卫。**

  Run: `PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_scheduler_main.py tests/test_v2_daily_preflight.py -q`

  Run: `rg -n "DataBridgeClient|run_full_refresh" scheduler/main.py`

  Expected: 测试通过；`scheduler/main.py` 中没有 HTTP DataBridge writer 引用。

## 任务 2：为 local MySQL publisher 加操作准入和单进程互斥

**Files:**

- Modify: `scripts/refresh_data_bridge_current.py`
- Modify: `tests/test_data_bridge_cli.py`

- [x] **Step 1: 写出 publish admission/lock 的失败测试。**

  ```python
  def test_publish_rejects_missing_launchd_one_shot_marker():
      exit_code, payload = command.run_command("publish", refresh_date="2026-07-29")
      self.assertEqual(exit_code, 2)
      self.assertEqual(payload["status"], "configuration_error")
      refresh.assert_not_called()

  def test_publish_lock_conflict_never_refreshes_or_writes_ready():
      with patch.object(command, "_publish_lock", return_value=locked_false):
          exit_code, payload = command.run_command("publish", refresh_date="2026-07-29")
      self.assertEqual(exit_code, 1)
      refresh.assert_not_called()
      ready.assert_not_called()
  ```

- [x] **Step 2: 运行红测。**

  Run: `PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_data_bridge_cli.py -q`

  Expected: publish 在无标记下仍可继续，且尚不存在 lock conflict 行为。

- [x] **Step 3: 最小实现。**

  - 仅在 `mode == "publish"` 时要求 `BFL_DATABRIDGE_PRODUCER == "launchd-one-shot"`；dry-run/check-only 不需要该标记。
  - 使用 `DataBridgeRefreshConfig.runtime_root / "v2_scheduler_gate" / "publisher.lock"` 的非阻塞 `flock` 保护整个 gate pre-block → refresh → strict reread → ready/blocked 流程；未获得锁时返回结构化失败，不触及 ready。
  - 保持现有 gate I/O best-effort fail-closed 行为和无敏感错误输出；不检查 installed plist、父进程或 `launchctl`。

- [x] **Step 4: 运行绿测。**

  Run: `PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_data_bridge_cli.py tests/test_v2_daily_gate.py -q`

  Expected: admission、锁冲突、ready/blocked 覆盖和 strict provenance 均通过。

## 任务 3：新增 active-discovery 的 launchd one-shot 预测 runner

**Files:**

- Create: `scheduler/launchd_prediction_runner.py`
- Create: `tests/test_launchd_prediction_runner.py`
- Modify: `scheduler/blackbox_scheduler_admission.py`
- Modify: `scheduler/executor.py`
- Modify: `scheduler/repository.py`
- Modify: `scheduler/main.py`（只抽取可复用的无 APScheduler validation helper；不重新开放 writer）
- Modify: `tests/test_blackbox_scheduler_admission.py`
- Modify: `tests/test_executor_cli.py`
- Modify: `tests/test_executor_run_id.py`

- [x] **Step 1: 写出 cadence runner 的失败测试。**

  ```python
  def test_daily_runner_discovers_active_configs_at_runtime_and_writes_scheduled_live():
      summary = runner.run("daily", predict_date="2026-07-29", algo_env="forecast_env")
      self.assertEqual(summary.discovered, 2)
      execute.assert_has_calls([
          call(native_cfg, "2026-07-29", algo_env="forecast_env", prediction_phase="scheduled_live"),
          call(v2_cfg, "2026-07-29", algo_env="forecast_env", prediction_phase="scheduled_live"),
      ])

  def test_unadmitted_active_candidate_is_reported_but_not_executed():
      summary = runner.run("daily", predict_date="2026-07-29", algo_env="forecast_env")
      self.assertEqual(summary.denied_scheme_ids, ("seven_y_trial",))
      execute.assert_not_called()

  def test_runner_lock_conflict_is_nonzero_and_has_no_execution():
      code = runner.main(["--cadence", "daily", "--predict-date", "2026-07-29"])
      self.assertEqual(code, 1)
      execute.assert_not_called()

  def test_launchd_one_shot_can_create_scheduled_live_without_ledger_item():
      result = execute_scheme(
          admitted_cfg,
          "2026-07-29",
          prediction_phase="scheduled_live",
          scheduled_control_plane="launchd_one_shot",
      )
      self.assertEqual(result.status, "success")
      create_run.assert_called_once_with(
          prediction_phase="scheduled_live",
          schedule_item_id=None,
          scheduled_control_plane="launchd_one_shot",
          ...,
      )
  ```

- [x] **Step 2: 运行红测。**

  Run: `PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_launchd_prediction_runner.py -q`

  Expected: 模块/entrypoint 尚不存在。

- [x] **Step 3: 最小实现。**

  - 在 `blackbox_scheduler_admission` 增加显式 `LAUNCHD_ONE_SHOT` control-plane capability（不能沿用名称含混的 `DIRECT_SCHEDULED`）；把当前已获自然调度许可的 exact identity 按原 admission 迁入该 plane，不能因 active 自动新增 7Y/gray Blackbox。新增或变更 admission 仍必须走既有 exact version/metadata validation。
  - 将 executor/repository 的 `scheduled_live` 写入边界从“daily 必须 ledger schedule_item_id”改为显式 `scheduled_control_plane="launchd_one_shot"`；该值只由新 runner 传入并在 executor 重新做 canonical lifecycle/admission/registry 校验。不得伪造 item、occurrence 或 epoch，也不得放宽 legacy ledger-bound completion 的 final fences。
  - runner 支持 `--cadence daily|weekly|monthly`、`--predict-date`、`--algo-env`，且不解析 cron、无需 RunAtLoad/startup catch-up。每次运行 `discover_schemes(strict=True)`，筛选 `status=active` 与匹配 frequency，基于一次 policy snapshot 逐项执行 lifecycle/Blackbox launchd-one-shot admission；不能用 frozen scheme-id/hash 清单。
  - 通过 `execute_scheme(..., prediction_phase="scheduled_live", scheduled_control_plane="launchd_one_shot")` 写入；日频 non-trading day 返回成功且零执行。所有 cadence 的 Blackbox DataBridge 项均先调用 `require_v2_daily_ready()`，使用该 predict date 与共享日历上一交易日；未获得 admission 的 7Y/gray candidate 只记录为 `denied`、不得读 gate、不得误写 `gray_live` 或被静默吞掉。
  - 使用 runtime 目录下**全局**非阻塞 `flock` 覆盖 discovery、admission、gate 与全批执行，阻止不同 cadence 或手工重入形成第二个 generic scheduled writer；JSON summary 报告 discovered/executed/denied/blocked/skipped/failed，任何 denied/blocked/failed/partial 返回非零。

- [x] **Step 4: 运行绿测与相邻回归。**

  Run: `PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_launchd_prediction_runner.py tests/test_blackbox_scheduler_admission.py tests/test_executor_cli.py tests/test_executor_run_id.py tests/test_scheduler_main.py tests/test_scheduled_executor.py -q`

  Expected: runner 不依赖 APScheduler/ledger/occurrence/epoch，日频 one-shot 可写合格 `scheduled_live` 而非伪造 ledger item，且相邻 schedule contract 测试保持通过。

## 任务 4：落库 repo plist 的目标拓扑与文档契约

**Files:**

- Create: `deploy/launchd/com.bond-factor-lab.data-bridge-refresh.plist`
- Create: `deploy/launchd/com.bond-factor-lab.daily-predictions.plist`
- Create: `deploy/launchd/com.bond-factor-lab.weekly-predictions.plist`
- Create: `deploy/launchd/com.bond-factor-lab.monthly-predictions.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.scheduler.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.daily-gray.plist`
- Modify: `deploy/launchd/com.bond-factor-lab.v2-preflight.plist`
- Modify: `deploy/README.md`
- Modify: `docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md`
- Modify: `tests/test_actuals_launchd.py`
- Create: `tests/test_prediction_launchd.py`

- [x] **Step 1: 写出 plist 契约红测。**

  用 `plistlib.load()` 断言：

  - refresh label 是 `com.bond-factor-lab.data-bridge-refresh`，06:30、`RunAtLoad=false`、无 `KeepAlive`，只调用 `python scripts/refresh_data_bridge_current.py --publish`，环境含 `BFL_DATABRIDGE_PRODUCER=launchd-one-shot`；
  - daily/weekly/monthly 分别为 07:03 工作日、周六 11:30、自然月 15 日 18:00，均调用 `scheduler.launchd_prediction_runner --cadence <value>`，无 `RunAtLoad`/`KeepAlive`；
  - old scheduler/daily-gray/v2-preflight target template 标有 `Disabled=true`，并且没有新的生产日历触发；
  - existing actuals retains exactly 08:30/19:00/23:45 and one writer。

- [x] **Step 2: 运行红测。**

  Run: `PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_actuals_launchd.py tests/test_prediction_launchd.py tests/test_v2_daily_preflight.py -q`

  Expected: 新 plist/disabled target 尚不存在。

- [x] **Step 3: 写入最小 plist 与说明。**

  - 新 plist 使用 service 环境、绝对工作目录、独立 stdout/stderr，均为 one-shot；不把 DSN、凭据、nonce 或实际 installed 值写进 repo。
  - `deploy/README.md` 只记录“repo desired state + 生产授权前只读核对”，禁止提供可直接执行的 bootstrap/bootout/kickstart 命令。
  - 明确 actuals 是当前 one-shot writer；old template disabled 是源码意图，不能声称机器已停。

- [x] **Step 4: 运行绿测和文档契约。**

  Run: `PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_actuals_launchd.py tests/test_prediction_launchd.py tests/test_v2_daily_preflight.py tests/test_onboarding_docs.py -q`

  Expected: plist 拓扑与 launchd-only 文档口径通过，且不产生 installed/loaded 结论。

## 任务 5：阶段复核、证据和提交边界

**Files:**

- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`
- Modify: `docs/superpowers/plans/2026-08-03-g2-launchd-single-writer.md`

- [x] **Step 1: 全量阶段回归。**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest \
    tests/test_data_bridge_cli.py tests/test_data_bridge_mysql_exporter.py \
    tests/test_data_bridge_refresh.py tests/test_v2_daily_gate.py \
    tests/test_v2_daily_preflight.py tests/test_scheduler_main.py \
    tests/test_launchd_prediction_runner.py tests/test_prediction_launchd.py \
    tests/test_actuals_launchd.py tests/test_scheduled_executor.py \
    tests/test_onboarding_docs.py -q
  git diff --check
  ```

- [x] **Step 2: 更新状态记录。**

  将 G2 记为“开发分支代码/模板完成，生产挂载与真实触发未完成”；写清没有执行 real publish、installed plist、launchctl、service restart、scheduled_live 或 G3 backfill。

- [x] **Step 3: 审查并提交本阶段归属文件。**

  提交前先运行 `git status --short` 与 `git branch --show-current`；只暂存本计划列出的 G1/G2 文件，不暂存用户的 `KNOWN_ISSUES_HANDOFF_20260802.md` 或 `_diag_weekly_10y_*.py`。提交后不 merge/push `master`。
