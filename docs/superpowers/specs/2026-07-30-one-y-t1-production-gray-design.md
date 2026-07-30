# 1Y T+1 Blackbox 生产灰度入库设计

## 目标

在当前开发基线合入 PR #19 的 CompareGate 修复和
`one_y_t1_quote_state_hv_v1` 两文件交付，按 Blackbox V2 SOP 完成该方案的
专项生产灰度授权、完整历史回测、连续灰度写入及 API/前端验收。

## 范围

本批包含：

1. 验证并合入 CompareGate 的 prior request 日期自洽修复；
2. 保留上游 `.py + .json` 原始字节，只验证平台接口和标准输出；
3. 将方案从首次 `draft + paused` 依次推进到
   `shadow + paused`、`active + active`；
4. 使用 `gray_target_start=2026-06-01`，将
   `target_date < 2026-06-01` 写入独立历史回测，将其后所有当前可生成目标写为
   `gray_live`；
5. 更新实际值、验证 Registry、数据库、API 和前端读取，并保存逐方案机器证据。

本批不包含：

- 修改算法内部逻辑或评估算法效果；
- 独立 per-scheme cron；
- 伪造 `scheduled_live`、occurrence、target receipt 或 08:00 SLA；
- 把当前日频容量合同从 25/29 静默修改为 26/30。

## CompareGate 修复

CompareGate 构造 prior request 时，已经同时回退 daily、weekly、monthly 三个
cutoff。`feature_date` 必须同步回退为 prior `daily_cutoff_key`，否则会构造出
平台真实 Request 不会产生的矛盾组合。

验收采用测试先行：

1. 在未合入修复的当前基线上，增加断言
   `prior.feature_date == prior.daily_cutoff_key`；
2. 确认测试因 prior `feature_date` 仍为原日期而失败；
3. 合入 PR 修复后确认同一测试通过，并运行完整 Blackbox Gate 测试。

该修复不改变当前 request、三个 cutoff 的来源或周历映射，只修复 prior
comparison request 的内部自洽性。

## 生命周期和写库

生产数据库当前不存在本方案的 Registry、版本、Harness、预测和回测记录，不能把
PR 中的 `version_status=shadow` 当成生产 shadow 已完成。合入后先将平台配置恢复为
`draft + paused`，使用当前 DataBridge generation 重跑七 Gate，并按顺序执行：

```text
persisted all-stage
→ draft-register
→ shadow-register
→ controlled activate
→ persistent backtest
→ ordered gray-backfill
→ actual updater
→ API/frontend acceptance
```

每个副作用阶段使用绑定 exact scheme/version/Harness run/date 的一次性授权。
历史 run 保持 immutable；gray 使用 insert-only，任一点失败即停止后续写入。

## 调度边界

当前日频目标架构只有一个 coordinator 和一个 daily occurrence。生产仍处于
legacy rollout，已批准候选策略固定为 25 个 base execution、29 个 target。

本方案在 `blackbox_scheduler_admission_v1.json` 中登记为 `mode=gray` 且
`capabilities=[]`，明确阻断 `legacy_automatic`、`daily_ledger` 和
`direct_scheduled`。配置中的 cron 只表达自然运行周期，不产生独立任务。

后续自动调度必须作为独立批次，把统一日频策略升级为经过容量验证的 26/30：
更新 admission、版本化 daily policy、固定 release offset、容量证据和相关测试，
再由未来真实 occurrence 产生首条 `scheduled_live`。不得额外创建第二个 cron
入口。

## 验收条件

- PR 修复有先失败、后通过的针对性回归测试；
- 七个自动 Gate 为 7/7，no-persist backtest 数量完整；
- exact persisted Harness run 和七 Gate 可从审计库读回；
- Registry、version 和配置最终均为 active，`deployed_at` 非空；
- 历史与 gray 按 `target_date=2026-06-01` 严格互斥；
- gray 从首个应有目标到当前可生成目标连续，无重复 target；
- actual 为 0 时保留，只有预测为 0 的样本按指标规则剔除；
- `/api/schemes`、`/api/backtests/factor-lab`、
  `/api/metrics/one_y_t1_quote_state_hv_v1__h1__1Y` 与数据库逐条一致；
- admission 明确拒绝三个自动调度能力，`scheduled_live=0`；
- 方案代码、配置、输入 generation、snapshot、Harness run、数据库 run 和证据
  JSON 可闭环追溯。

## 失败处理

- PR 修复导致既有周频/日频 Gate 回归时不合并；
- DataBridge 或环境检查失败时停止所有授权；
- 生命周期三方状态不一致时保持 paused 并执行 reconciliation；
- 历史/gray target 重叠、gray 缺口或重复写入时阻断前端验收；
- API 服务未加载最新代码时只重启 Bond Factor Lab 自身后端，不操作
  BondProjectPro；
- 自动调度保持 fail-closed，不能为了“上线”绕过现有容量合同。
