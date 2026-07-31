# 3Y ADYN T+1 双方案生产灰度入库设计

## 目标

合入 PR #21 的两个 Blackbox V2 两文件交付：

- `three_y_adyn_lb2_k1_v1`
- `three_y_adyn_lb1_k3_v1`

在不修改算法内部逻辑的前提下，完成 Contract 1.0 技术验收、生产生命周期登记、
历史回测、连续 `gray_live`、API/前端验收，并复用现有
`com.bond-factor-lab.daily-gray` LaunchAgent 每天自动执行。

## 已核事实

- 当前开发分支和远程 `master` 均为 `af616bf`，工作区干净。
- PR #21 head 为 `a8b9ec4`，目标分支正确，无 CI 检查。
- PR 分支基于 `d42a668`；三方合并只需把两个 admission 追加与当前五个周均方案的
  追加合并，不得覆盖或删除当前开发分支内容。
- 当前生产库对两个新 base ID 的 Registry、版本、Harness、回测、预测和 run 均为
  零。PR 描述中的 337 条历史和 40 条灰度属于提交方测试环境，不能作为当前生产
  已入库证据。
- 当前生产模式为 `legacy`。现有 `com.bond-factor-lab.daily-gray` 每天 07:00
  执行 `python -m scheduler.daily_gray_runner`，统一扫描
  `status=active + frequency=daily` 的方案并写 `gray_live`。

## 生命周期设计

PR 中的 `active/active` 只是提交方环境状态。合并后先把两个 `config.yaml` 恢复为：

```yaml
status: paused
version_status: draft
```

交付 `.py + .json` 保持 PR 原字节。随后每个方案独立执行：

```text
environment/sandbox/DataBridge preflight
→ check-only 七 Gate
→ persisted 七 Gate
→ draft-register
→ shadow-register
→ backtest-persist
→ blackbox-activate
→ ordered gray-backfill
→ daily-gray --only canary
→ actual/API/frontend/launchd 验收
```

每个副作用动作使用绑定 exact scheme、version、Harness run 和日期的一次性短期
token。两个方案不能共享 token；任一方案在某阶段失败，只冻结该方案的后续动作，
不回滚另一个已完成且可审计的方案。

## 数据与日期设计

- `gray_target_start` 固定为 `2026-06-01`。
- 历史回测从 `2025-01-01` 开始，且必须满足
  `target_date < 2026-06-01`。
- T+1 灰度按平台交易日历枚举 `target_date >= 2026-06-01`：
  `feature_date` 为前一交易日，`predict_date=target_date`。
- 灰度写入为 insert-only `gray_live`；不伪造 `scheduled_live`、ledger
  occurrence 或 target receipt。
- 技术 Gate 使用最新通过完整性检查的 DataBridge generation；自然 launchd
  运行继续使用每日 preflight 刷新的 current。

## 调度设计

两个 scheme version 在 admission 中登记为：

```text
mode=gray
capabilities=[]
```

这会阻断 `legacy_automatic`、`daily_ledger` 和 `direct_scheduled`，避免长驻
APScheduler 把它们当成正式 `scheduled_live` 任务；独立 daily-gray runner 不读取
该 admission，仍会发现 active daily 方案并统一写 `gray_live`。

调度只保留一个 LaunchAgent：

```text
com.bond-factor-lab.daily-gray
StartCalendarInterval = 07:00
Program = python -m scheduler.daily_gray_runner
```

不新增每方案 plist，不修改 `deploy/daily_scheduler_policy_v2.json`，不扩大
25/29 正式 ledger 容量合同。上线时核对已安装 plist 与仓库模板一致且任务已
加载；若已加载则保留其日历触发，不在当日 DataBridge current 尚未就绪时用
`kickstart -k` 触发全部 active daily。受控 canary 使用一次 `--only` 同时选择
两个新方案。

## 验收和失败边界

完成状态必须同时满足：

- 两个方案七 Gate 全绿，持久审计可从数据库读回；
- config、版本和 composite Registry 均为 `active`，`deployed_at` 非空；
- 每方案一个 canonical 历史 success run，历史与 gray target 零重叠；
- 从 `2026-06-01` 到当前可生成目标的 gray 连续、无重复；
- `/api/schemes`、`/api/backtests/factor-lab`、
  `/api/metrics/{composite_id}` 和 dashboard 与数据库一致；
- daily-gray LaunchAgent 已加载，07:00 触发不变，active daily discovery 精确
  包含两个新方案；
- 当前批次 `scheduled_live=0`，自然 07:00 首跑前记录为
  `MOUNTED_NOT_OBSERVED`。

环境、DataBridge、Gate、授权、回测分区、gray 连续性或 API 任一失败时停止相应
方案后续生产动作。不得通过直接 SQL、修改算法、覆盖旧预测或前端特判绕过。
