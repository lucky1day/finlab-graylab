# Blackbox V2 日级数据 Gate 与 Scheduler 重启管理设计

日期：2026-07-21
状态：已实施并安装；等待下一交易日自然时钟观察

## 1. 背景与目标

平台当前由同一个 `scheduler.main` 进程承载 Native V1、Blackbox V2、DataBridge 刷新和 actual 刷新。2026-07-21 未在 07:03 前按计划重启 scheduler，导致新激活的四个 V2 方案没有进入进程启动时生成的 APScheduler 任务表；同一旧进程还在 07:03 使用内存中的旧 discovery 代码回写 Registry，把短名称覆盖成长名称。

本设计建立机器执行的日级时间管理，目标是：

1. DataBridge 每天 06:00 开始首次刷新。
2. 06:30 检查，未更新时于 06:35 再次刷新。
3. 07:00 做最终校验；成功才重启 scheduler，失败则不重启、不补跑并记录告警。
4. DataBridge 失败只阻断依赖 `data_bridge_current` 的 Blackbox V2，不影响 Native V1。
5. 防止长时间运行的旧 scheduler 在单个预测任务开始前回写 Registry 元数据。

## 2. 方案选择

### 2.1 排除：失败时停止共享 scheduler

V1 与 V2 当前共用一个 scheduler。停止进程会同时停止全部 V1 日频、周频、月频预测和 actual 刷新，不符合故障域只限于 V2 的要求。

### 2.2 采用：共享 scheduler + V2 独立日级 Gate

保留单进程调度架构，在 V2 `scheduled_live` 边界增加日级就绪凭证。V1 不读取该凭证。07:00 校验成功后重启共享 scheduler 的目的仅是重新加载当前代码、配置和任务表；校验失败时旧 scheduler 继续服务 V1，但 V2 任务因缺少当天就绪凭证而 fail-closed。

### 2.3 暂不采用：拆分 V1/V2 scheduler 进程

独立进程的隔离最彻底，但需要重新划分 actual、Registry 同步、startup catchup 和部署职责，迁移风险超出本次时间管理修复范围。

## 3. 每日时序

所有时间使用 `Asia/Shanghai`。

| 时间 | 动作 | 成功结果 | 失败结果 |
|---|---|---|---|
| 06:00 | V2 DataBridge 首次全量刷新 | 原子发布当天 generation | 保留上一成功 generation，记录 attempt |
| 06:30 | 第一次完整校验 | 记录检查通过，不重复刷新 | 记录 stale/invalid，等待 06:35 |
| 06:35 | 条件重试 | 若 06:30 未通过，执行第二次全量刷新；若已通过则跳过 | 记录第二次失败，保留旧 generation |
| 07:00 | 最终完整校验 | 写入当天 `ready` 凭证，随后受控重启 scheduler | 写入当天 `blocked` 凭证；不重启、不补跑 |
| 07:03 起 | 正常预测错峰 | V1 正常运行；V2 只有凭证有效时运行 | V1 正常运行；V2 跳过且留下结构化日志 |

launchd 负责触发四个时间点。控制器必须使用单实例锁；若上一次刷新仍在运行，后续触发不得并发启动第二次刷新。延迟或唤醒补触发只能执行当前时间对应的检查阶段，不能把 07:00 之后的迟到执行解释为获得重启授权。

## 4. V2 日级就绪凭证

凭证存放在 DataBridge runtime root 下的独立目录，由原子写替换生成，不进入 Git：

```text
backtest_artifacts/data_bridge_refresh/v2_scheduler_gate/YYYY-MM-DD.json
```

最少字段：

```json
{
  "schema_version": "v2-scheduler-gate-v1",
  "run_date": "2026-07-22",
  "status": "ready",
  "checked_at": "2026-07-22T07:00:00+08:00",
  "generation_id": "full-20260722-...",
  "refresh_date": "2026-07-22",
  "expected_daily_date": "2026-07-21",
  "business_digest": "...",
  "restart": {
    "requested": true,
    "label": "com.bond-factor-lab.scheduler"
  },
  "checks": []
}
```

`blocked` 凭证还必须记录失败类型、DataBridge `last_attempt`、缺失或不一致的文件、水位和摘要。凭证不保存密钥、数据库口令或授权 token。

V2 `scheduled_live` 执行必须同时满足：

1. `runtime_type=blackbox_v2` 且 `input_source=data_bridge_current`。
2. 凭证 `run_date` 等于当前 `predict_date`。
3. 凭证 `status=ready`。
4. 凭证的 `generation_id`、`refresh_date`、`business_digest` 与当前 DataBridge state 一致。
5. 当前三频文件、schema、摘要和日频最大键仍通过现有 `check_current_dataset` 校验。

任一条件失败时，V2 返回受控 `skipped`，不调用算法、不创建实盘预测、不自动补跑。`gray_live`、Harness no-persist 和专项授权回测不由此日级凭证授权，仍走各自现有 Gate。

## 5. V1/V2 隔离边界

- Native V1 不读取 V2 凭证，也不依赖 DataBridge current。
- V2 日级 Gate 只应用于 scheduler 产生的 `scheduled_live`，包含正常 cron 与 startup catchup。
- 07:00 校验失败不停止共享 scheduler；V1 任务和 actual 刷新继续运行。
- V2 当天被阻断后，即使 DataBridge 在 07:00 后恢复，也不自动把凭证改为 ready；任何补偿必须走明确授权流程。
- 07:00 成功后的 scheduler 重启发生在首个 V1 日频任务 07:03 之前。若无法在安全窗口内完成并验证，则当天 V2 保持 blocked，V1 使用原进程继续运行。

## 6. Scheduler 重启与验证

控制器不直接管理任意 PID，只允许操作固定 label：

```text
com.bond-factor-lab.scheduler
```

重启流程：

1. 记录旧 worker PID 和启动时间。
2. 使用当前用户的 launchd domain 执行受控 `kickstart -k`。
3. 等待新进程进入运行态，但最晚不得越过安全窗口。
4. 验证 worker PID/启动时间已变化。
5. 验证 scheduler 日志出现当前启动批次和所有 active base scheme 的有效 cron；四个目标 V2 必须挂载。
6. 验证 Registry 与当前配置一致，短名称不得被旧进程覆盖。

重启命令失败或验证不完整时，将当天凭证最终状态改为 `blocked`。已经开始的 V1 任务不得被控制器追杀；控制器不执行递归重启。

## 7. Registry 写入边界修复

当前 `run_prediction_job()` 每次执行都会调用 `_sync_registry()`。这使长时间运行的旧进程能够在任意任务开始时用旧代码覆盖新进程写入的名称和 description。

修复后 Registry 自动同步只允许发生在：

- scheduler 启动构建任务表时；
- backend 启动时；
- Activation/Reconciliation 正式生命周期操作；
- 受保护的 admin sync。

单个 cron 预测任务不得回写 Registry。它仍可按 ID 读取当前方案配置并执行，但 Registry 元数据不是预测执行的副作用。

## 8. launchd 与配置

新增专用 V2 preflight launchd agent，配置 06:00、06:30、06:35、07:00 四个 `StartCalendarInterval`。它不使用 `KeepAlive`，每个阶段结束即退出；阶段选择必须 fail-closed，无法识别的触发时间不执行刷新或重启。

现有 scheduler 内部的日常 DataBridge cron 将移除，避免 06:00 双重刷新。scheduler 的 startup current 检查保留作为异常重启后的只读/恢复保护，但它不能生成当天 `ready` 凭证，也不能绕过 07:00 最终 Gate。

DataBridge 配置默认值同步调整为：

- `DATABRIDGE_REFRESH_START=06:00`
- `DATABRIDGE_REFRESH_DEADLINE=07:00`

## 9. 告警与审计

每次阶段运行输出一条 JSON 摘要到专用日志，并更新当天凭证。至少包含：阶段、计划时间、实际开始/结束时间、refresh attempt、generation、校验结果、旧/新 scheduler PID、重启结果和错误。

07:00 `blocked`、重启失败、PID 未切换、任务挂载缺失或 Registry 不一致均为 error。健康检查脚本必须读取当天凭证，把 V2 blocked 与 V1 运行状态分别报告，不能把 V2 DataBridge 故障扩大成全平台故障。

## 10. 测试与验收

采用测试先行，至少覆盖：

1. 06:00 必须发起首次刷新。
2. 06:30 校验通过和失败两条路径。
3. 06:35 只在未就绪且没有刷新占锁时重试。
4. 07:00 成功写 ready 并请求一次重启。
5. 07:00 失败写 blocked，绝不请求重启。
6. 07:00 后迟到触发不能转成 ready 或补跑。
7. ready 凭证 generation/digest 不一致时 V2 拒绝执行。
8. blocked/缺失凭证时 V2 cron 和 startup catchup 均不调用算法。
9. 同样状态下 V1 仍调用原执行路径。
10. 单个预测任务不再同步 Registry。
11. launchd plist 包含四个时间点且没有 KeepAlive。
12. 生产演练验证 V1 原任务数量和实际执行不减少，四个 V2 从下一交易日产生自然 `scheduled_live`。

## 11. 部署顺序

1. 在当前开发分支完成测试、代码、launchd 和 SOP 修改。
2. 当天所有既定预测任务完成后安装 preflight agent，并重启一次 scheduler 使 V2 Gate 和 Registry 写入边界生效。
3. 该部署重启不授权当天 V2 补跑；当天没有 ready 凭证时 V2 startup catchup 必须跳过。
4. 下一交易日按 06:00/06:30/06:35/07:00 自然运行。
5. 观察 07:03 起 V1 无回归，并核对四个 V2 的自然错峰时间、`scheduled_live`、API 短名称和前端展示。
6. 形成机器 evidence、日志摘要和生产状态文档。未经用户另行确认，不合并或推送 `master`。
