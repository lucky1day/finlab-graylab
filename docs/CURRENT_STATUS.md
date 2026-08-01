# 当前状态

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台运维和审计人员

**最后核验日期**：2026-08-01

本文只保留当前已验证结论。较早的逐日状态、数据库快照、旧规模与整改过程已冻结到[历史状态记录](records/status/README.md)；未完成工作的排序查看[TODO](TODO.md)。

## 当前政策

- Native V1 只维护版本化政策清单中的既有身份；新增算法、方案 ID、目标、任务和
  替代版本一律通过 Blackbox V2 两文件交付。
- Blackbox V2 自动 Gate、active Registry 或 gray/formal 标签都不自动授予生产
  权限；每个 identity/version/runtime 仍须独立生产准备核验和专项授权。
- [日频信号 SLA](architecture/DAILY_SIGNAL_SLA.md)是已经批准、待切换的目标
  合同，不是当前已安装生产状态的证明。历史记录中的旧规模和实验不再定义目标
  门禁，但其中冻结的生产现场事实仍须保留到新证据取代。
- 后续以 launchd + plist 作为真实生产调度控制面。任务是否已生产挂载只能由 installed plist、`launchctl` loaded state 和对应日志共同证明；`scheduler.main`/APScheduler 只是部分 plist 启动的子进程实现，新增或迁移生产任务必须先定义并验收对应 LaunchAgent。

## 已批准的日频目标合同

| 项目 | 切换后的目标 |
|---|---|
| owner | 1 个 coordinator；每交易日 1 个 `daily-signals` occurrence |
| 冻结全集 | 25 base execution：17 Native + 8 Blackbox V2 |
| 资源上限 | Native 最大并发 2；V2 最大并发 2 |
| 完成口径 | 29/29 signal target receipt；少一个即不完整 |
| cache | warm-cache 日常生产；Liwei 使用 7 个 schema 3 family |
| phase | 历史漏跑只写 `gray_live`；只有未来真实 ledger occurrence 可写 `scheduled_live` |
| 控制面 | machine-global epoch、单实例锁、run fence；ledger 下 v2-preflight 保持未加载 |

`deploy/daily_scheduler_policy_v2.json` 是待切换 25/29 精确身份和并发上限的目标
机器合同。它存在于仓库不等于 production rollout、migration、cache bootstrap、
epoch cutover 或真实 occurrence 已完成。目标合同直接校验冻结 policy、输入、cache
和执行结果的确定性；开发分支已删除旧 capacity admission JSON、签名 qualification
和 epoch admission probe，改由 direct authority 校验。该代码状态不等于生产已切换。

## 当前生产事实

| 项目 | 最后核验事实 |
|---|---|
| schema | production 仍为 migration 017；migration 018 尚未应用 |
| rollout | `rollout=legacy`；ledger 尚未启用 |
| launchd 现场 | backend 与 scheduler 已加载且有运行中 PID；actuals 与 daily-gray 已加载为按时启动的一次性任务；v2-preflight 已加载但无运行中 PID，最近退出状态为 1；installed plist 只读比对显示 actuals、daily-gray 与仓库模板一致，backend、scheduler、v2-preflight 存在漂移 |
| cache | 7 个 production Liwei family 已完成 schema 3 bootstrap；同 authority 二次运行 7/7 `hit`、零训练，cache-local direct authority 通过 |
| DataBridge | `full-20260730-081804-9794ce962c1a` 已于 08:18 晚到发布并通过 `--check-only`；daily cutoff 为 2026-07-29 |
| 控制面 | machine-global epoch 与 ledger cutover 尚未执行；没有 production run fence 生效证据 |
| 开发代码 | runtime 已改为 direct authority，旧 admission 文件已删除；PR #16 已合并 Native gray-gap artifact 入口并与正式 ledger 隔离 |
| 日频现场 | 2026-07-28 为 12/29，2026-07-29 为 0/29 |
| 历史补缺 | 50 条缺口尚未写入 |
| 2026-07-30 | 尚无真实 ledger occurrence；06:30 未创建，08:00 为 0/29，无 receipt 或 `scheduled_live` provenance |

因此只能确认 production cache 本地合同和 7 月 30 日 DataBridge current 已就绪，
不能声称 ledger、migration 018、epoch 或 run fence 已上线。完整 direct authority
仍被旧 `started_at` 定义阻断。Native snapshot 的受控代码入口已合并，但两份
production artifact 尚未生成或登记，因此当前仍没有可执行的 Native authority。
旧 admission 依赖虽已从开发代码移除，但任一前置未闭合时 cutover 必须 fail-closed。
只有未来真实 coordinator 产生并验收的 occurrence 才能成为 `scheduled_live` 起点；
日期标签本身 不构成证据。

## 历史日频缺口

历史缺口共 50 条，按 `predict_date` 为：

| 日期 | 缺口 |
|---|---:|
| 2026-07-23 | 1 |
| 2026-07-24 | 1 |
| 2026-07-27 | 2 |
| 2026-07-28 | 17 |
| 2026-07-29 | 29 |

按任务类型为 T+1 共 5、T+5 共 45，当前全部尚未写入。DataBridge 已使 20 个 V2
target 具备 freshness；30 个 Native target 仍缺已生成并 SEALED 的 snapshot
authority。代码入口已合并不等于 production authority 已存在。它们
只能通过受控 insert-only 补为 `gray_live`，不得倒签 `scheduled_live` 或混用 receipt。

## Liwei schema 3 目标 cache 合同

七个 current 均绑定 `native-8ad794ac7bfc9505f9a498f1`，manifest/input-state
schema 3 且 acceptance `ACCEPTED`；二次运行 7 个 `hit`、27 reuse、0 training，
未改 current。这里只表示 cache-local direct-ready；ledger 仍须 migration 018。

- 10 个 Liwei execution 归入 7 个精确 `cache_family + tenor`，每个 family 只有
  SLA 表中指定的唯一 publisher；共享 consumer 只读同一 generation。
- effective input projection 绑定实际 weekly/monthly 有效列、原 core alignment、
  `date_to_week`、daily grid、feature cutoff、proof files 和内容 SHA-256。
- generation 保留 parent lineage，覆盖范围单调；正常路径只允许
  `hit/append/suffix`，无法证明的变化在对应 family fail-closed full rebuild。
- consumer 零训练、零 staging、零 pointer 切换、零清理；缺 cache 或覆盖不足时
  返回 `CACHE_PUBLISHER_REQUIRED` 等待 publisher。
- root、generation、manifest、payload 和 pointer 每次重开验证 owner/mode/inode、
  非 symlink、canonical path 与 hash；失败不切换 current。

## 平台与迁移

- production schema history 当前仍停在 017；migration 018、production-shaped
  演练和生产 apply 都是待办。
- `migrations.runner` 是 caller-supplied `Engine` 的唯一迁移实现，
  `scripts/apply_migrations.py` 是唯一受控 operator CLI。017/018 inspect 保持只读，
  所有 apply/recovery 写路径都要求 expected database/server UUID。
- migration 018 的当前契约仅修正 `t_scheme_runs.started_at` 可空性；不把它混称为
  composite FK。019/020 和长期 generation 归档仍是 defense-in-depth 后续项。
- canonical migration runner 的实现提交为 `f3a5720`、`1f1019b`、`8ee916f`、
  `3c96f58`；隔离 MySQL 验证未应用生产 migration，也不等于 production-shaped
  sanitized clone 演练。durable signed operator report 仍未形成。

## Blackbox V2 与灰度批次

- PR #21 两个 `three_y_adyn_*` 3Y/T+1 方案均为 active，每方案为 `337 backtest + 17 monthly + 43 gray`，历史/灰度零重叠、交易日灰度零缺口，并已进入现有 `com.bond-factor-lab.daily-gray` 每天 07:00 的 active-daily 扫描；双方案同路径 canary 为 2/2，自然首跑尚未观察，`scheduled_live=0`，29/29 policy 未修改。专项证据见[双方案生产入库记录](blackbox_v2/records/THREE_Y_ADYN_T1_2SCHEMES_ONBOARDING_20260731.md)。PR #20 五个 `wavg_*_gapflip_v5` 均为 active，每方案为 `72 backtest + 17 monthly + 9 gray`；五个周平均格子均已前端可见，scheduler 按用户要求未挂载，`scheduled_live=0`。专项证据见[五方案生产入库记录](blackbox_v2/records/WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.md)。
- `cgb_causal_wk_1y@cba824c27f0e` 已 active；旧版已 retired 且业务数据清零。
  新历史 run `193` 为 `72 + 17` 条，新灰度 run `1674..1682` 共 9 条；scheduler
  admission 为 gray/零能力，详见[技术入库记录](blackbox_v2/records/CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md)。
- 10Y T+5 四方案均为 active；每方案 333 条 canonical backtest、17 个月度指标、
  39 条手工 `gray_live` 和 372 条前端展示记录，本批 gray 共 156 条。精确摘要见
  [手工入库记录](blackbox_v2/records/GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)。
- FengRL 五个月度方案均为 active；每方案 16 条历史和 3 条手工 `gray_live`，
  本批为 `80 + 15 = 95`。专项记录见
  [月度入库记录](blackbox_v2/records/MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md)。
- 上述手工灰度不伪造 scheduled provenance；历史 `schedule_cron` 交付元数据也不
  绕过 ledger、epoch 或专项授权。

## 非日频剩余状态

- 月度 updater 按 active Registry 的 `1Y/3Y/5Y/7Y/10Y` 幂等运行，8 个 active
  月度方案保持 `19 signal / 18 valid`。
- `weekly_10y_d_overlay_0529` 的生产 `202625` 冲突、四个真实信号缺口和历史 `200951` 双表日历边界均已闭合，当前为 `81/80`；当前输入与旧 benchmark 仍有独立 vintage 漂移，CompareGate 保持 fail-closed。
- 6 个 active `weekly_point` 方案均为 `81/80`；另有 8 个 active `weekly_average` 身份，按独立 `task_type` 统计，不混入 point 候选计数。
- Python 后端仍需在独立授权维护窗口重启，以使已合并的 Registry Actual 范围和
  统一展示起点由服务端进程生效。

## 权威入口

- 未完成工作：[TODO](TODO.md)
- 方案入库：[统一入库导航](onboarding/README.md)
- 平台操作：[Blackbox V2 平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[Blackbox V2 生产准备清单](blackbox_v2/PRODUCTION_READINESS.md)
- 历史状态：[状态记录索引](records/status/README.md)
