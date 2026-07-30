# cgb_causal_wk_1y 周度方案生产入库设计

## 1. 目标

将上游两文件交付 `cgb_causal_wk_1y.py + cgb_causal_wk_1y.json`
按 Blackbox V2 Contract 1.0 接入平台，完成历史回测、灰度实盘补齐、
生产激活、周度 scheduler 挂载和下游可见性验收。

本次以最快形成可运行生产链路为目标。平台只验证输入、接口、确定性、
截止隔离、标准输出和写库链路，不验证、改写或调参算法内部逻辑。

## 2. 身份与范围

- base scheme ID：`cgb_causal_wk_1y`
- Registry scheme ID：`cgb_causal_wk_1y__h1__1Y`
- runtime：`blackbox_v2`
- runtime profile：`blackbox-v2-v1`
- data schema：`data-bridge-v1`
- target tenor：`1Y`
- task type：`weekly_point`
- horizon：`1`
- target rule：`target_week_end_yield_vs_feature_week_end_yield`
- algorithm version：`1.0.0`
- 交付脚本 SHA256：
  `90f3abcc1501eb7173c706fc2ad5d76fda89bf9004c88ee976b98378bbb6d450`
- Metadata SHA256：
  `efc8e03c5db98c33f0b830d62cc4465f7f890b5a783de68fee58e4ae19d4162b`

交付文件按原字节 Intake。仓库不接收上游 `sample_data/`、`reference/`、
`dev_checks/`、`sop/`、`requirements.txt` 或其它辅助文件。

## 3. 输入设计

方案从平台统一 `shared.input_artifacts` 链路消费同一代 DataBridge
父快照：

- `daily_output.csv`
- `weekly_output.csv`
- `monthly_output.csv`

Intake 同时声明 `platform_inputs: [api-wind-date-v1]`。平台将权威业务周历
作为组合输入中的 `api_wind_date.csv` 提供给算法，避免把交付脚本内嵌日历
当作生产日历真值。父 DataBridge Snapshot 仍严格只有三份业务文件，周历只
进入组合 Snapshot。

每个 Request 由平台生成 Contract 1.0 的七个字段，三个 cutoff 均由平台权威
日历和同一快照计算。算法只能读取本次私有只读运行视图，不接触数据库、网络
或平台写库接口。

## 4. 日期和数据分区

所有历史、灰度和正式信号按 `target_date` 分区：

| 阶段 | 范围 |
|---|---|
| 历史回测 | `predict_date >= 2025-01-01`，且 `target_date < 2026-06-01` |
| 灰度实盘 | `2026-06-01 <= target_date <= 2026-07-31` |
| 首条正式调度 | `predict_date=2026-08-01`、`feature_date=2026-07-31`、`target_date=2026-08-07` |

生命周期证据登记 `gray_target_start=2026-06-01`。完整持久化回测以
`2025-01-01` 为起点，以 `2026-06-01` 为 exclusive target cutoff，
因此 canonical latest backtest 不得含任何六月及以后的 target。

灰度区先枚举平台日历中的应有目标周，再反推每条 `feature_date` 和
`predict_date`。每条缺口经独立 `gray_backfill_write` 授权、以
`prediction_phase=gray_live` insert-only 写入。灰度回放使用当前同代快照，
但每条 Request 仍硬截止在对应 `feature_date`，并明确记录
`current_snapshot_as_of_not_historical_vintage`。

`scheduled_live` 只能由 2026-08-01 的真实 scheduler 时钟自然执行产生，
不得用手工回放或激活时间伪造。

## 5. 生命周期和调度

方案先完成两文件 Intake 和七个自动 Gate，再按专项授权依次执行：

1. shadow 登记；
2. ActivationGate 将 config、exact version 和 composite Registry 一致激活；
3. 持久化完整历史回测；
4. 按时间顺序补齐六月至七月全部 `gray_live`；
5. 加入 Blackbox scheduler 精确版本准入；
6. 验证周度 recurring 和 direct scheduled 能力；
7. 验证 Registry、API、前端和 actual join。

配置使用周六 `11:30 Asia/Shanghai` 的周度 cron。与现有周度任务同组时由平台
按既有规则错峰，预期本方案有效时间为 `11:32`；最终以 scheduler 实际登记
结果为证据。

在开发分支完成验证后，不自行合并或覆盖 `master`。生产切换前必须再次取得
用户对已验证分支合并到 `master` 的明确确认。

## 6. 写库边界

- Harness 自动 Gate 只写报告和控制面审计，不写预测或回测业务表。
- 完整历史只经 `backtests.repository` 单事务写入一个 immutable backtest run、
  全部 prediction 和非空 monthly metrics。
- 灰度和正式预测只经 `scheduler.repository` 写入
  `t_scheme_runs`、`t_scheme_predictions` 和 `t_scheme_run_log`。
- Registry 只经生命周期 Gate 变更。
- 不直接执行 SQL，不绕过授权 Gate，不覆盖已存在 target。

## 7. 验收与失败处理

上线前至少满足：

- 冻结运行环境与 sandbox probe 通过；
- Intake 原字节摘要与收到的两文件一致；
- Static、Input、Unit、DryRun、Compare、Backtest、API Readiness 七 Gate
  全部通过；
- 技术 Gate 只证明 Contract、确定性、截止隔离和结果结构，不把它表述为算法
  逻辑或效果验证；
- canonical backtest 全部 `target_date < 2026-06-01`；
- gray live 覆盖平台日历枚举出的全部六月、七月目标点且与 backtest 零重叠；
- config、exact version、Registry、scheduler admission 和代码摘要一致；
- `/api/schemes`、`/api/metrics/{registry_id}` 和
  `/api/backtests/factor-lab` 与数据库逐行一致；
- 前端 `1Y国债活跃 × 周度` 格子可见，控制台零错误；
- 8 月 1 日自然调度成功后，run、log、prediction、API 均能追溯首条
  `scheduled_live`。

任何 Gate、批次、授权、写库计数、日期分区或输入 generation 不一致时立即
停止后续步骤。历史回测事务整体回滚；灰度补齐在首个失败点冻结，不跳过缺口；
已激活但未完成灰度连续性和 scheduler 挂载时不得标记
`Onboarding Complete`。
