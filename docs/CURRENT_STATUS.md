# 当前状态

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台运维和审计人员

**最后核验日期**：2026-07-25

本文只保留当前有效结论。较早的逐日状态、数据库快照和整改过程已冻结到[历史状态记录](records/status/README.md)。

## 当前政策

- Native V1 只维护版本化政策清单中的既有身份，不接受新方案或算法升级。
- 新算法、新方案 ID、新目标、新任务和替代版本一律通过 Blackbox V2 两文件交付。
- Blackbox V2 自动 Gate 通过不等于生产授权；每个方案仍需独立完成生产准备核验和专项授权。
- 具体方案的授权不得外推为后续新方案的默认权限。

## 平台状态

| 项目 | 当前结论 |
|---|---|
| Native V1 | 现有方案保持原 Registry、scheduler、数据库和历史结果，只做存量维护 |
| Blackbox V2 技术入库 | Intake、统一 DataBridge 输入、七个 Gate、预测和 no-persist 回测已形成稳定路径 |
| Blackbox V2 生产路径 | 已完成一个真实周频方案的专项生产灰度激活 |
| 日频 08:00 保障 | `FUNCTIONAL_MVP_VERIFIED`；21/25 ledger 功能 MVP 已在隔离 MySQL 通过，但 rollout 关闭，容量和生产门禁未通过，不能宣称 SLA 稳定 |
| 平台总体评级 | `PRODUCTION_PATH_READY`，尚未取得覆盖所有任务和依赖的 `PRODUCTION_READY` |
| 新方案默认终点 | 先进入受控技术验收；生产运行必须逐方案专项授权 |

## 当前生产灰度方案

- `weekly_10y_lgbm_point_v1` 已通过专项授权进入生产灰度。
- 当前配置、方案版本和 composite Registry 状态为 `active`。
- 已完成一次持久化回测和一次 `gray_live` 预测，API、前端和 scheduler 已识别该方案。
- 首条灰度预测目标日为 2026-07-24；实际方向和对应准确率必须在目标数据产生后复验。
- 当前只有一个真实 `10Y + weekly_point + LightGBM` 交付样本，不能代表所有任务类型和依赖组合稳定。

1Y T+5 四方案批次已按用户明确授权全部进入生产灰度：

- `LIQ_EXCESS_A`、`LIQ_EXCESS_A_W252_L7`、`LIQ_EXCESS_A_W350_L7`、`LIQ_EXCESS_B_W252_L7` 的配置、方案版本和 composite Registry 均为 `active`。
- canonical latest backtest 已切换到 run `178..181`：每方案 333 条、17 个月，`predict_date=feature_date=2025-01-02..2026-05-22`，`target_date=2025-01-09..2026-05-29`；旧 run 全部 immutable 保留审计。
- 每方案已有 40 条连续 `gray_live`：`predict_date=2026-05-26..2026-07-21`、`feature_date=2026-05-25..2026-07-20`、`target_date=2026-06-01..2026-07-27`；历史/live target overlap 为 0，`scheduled_live=0`。
- 其中每方案 38 条历史缺口通过独立 `gray_backfill_write` token 和 insert-only `gray-backfill` Gate 补齐，统一绑定 generation `full-20260720-055026-00e12e3803a8` 与 snapshot `snapshot-fd8a1f8736d3a4d057fbd98e`；首条部署日 gray live 保留原记录。
- 前端在 `1Y国债活跃 × T+5` 格子内显示 4 个短名称；2026-05 是历史末月，实盘分隔线位于 2026-06 前。所有方案的详情分隔文案统一只显示 `实盘预测目标区间`；这四个方案尚无 `scheduled_live`，因此当前显示“待产生”。
- 四方案已达到 `Onboarding Complete`；尚未达到 `Production Observed`，因为下一交易日自然 scheduler 尚未产生 `scheduled_live`。
- 2026-07-21 的人工重启未在 07:03 前执行，四方案当天没有自然 `scheduled_live`；旧 scheduler 同时在 07:03 用过期 discovery 回写 Registry 长名称。12:22 已安装 V2 独立日级 Gate 和自动重启控制并重载 scheduler：四个 V2 job 已挂载，今天以 blocked 凭证拒绝 startup catchup，17 个 Native V1 运行保持 17/17，Registry、本地及公网 API 已恢复四个短名称。随后按用户专项授权以 fresh LiveGate 补齐四条当日 `gray_live`，没有伪造 `scheduled_live`；下一交易日四阶段自然运行和 `scheduled_live` 仍待观察。
- 四个日频算法来自同一上游批次，证明了日频 Blackbox 运行路径，但不等于四个独立交付包，也不覆盖月频。

### 日频 08:00 整改状态

- 2026-07-24 旧 APScheduler 路径仅生成 16/21 个 run（13 success、3 failed、
  5 未运行），因此当前生产不能认定为稳定。
- 步骤 6 已完成：隔离 MySQL 精确展开 21 item/25 target，其中 Native
  17 item/21 target、输入模式 14/3；双 lane、失败隔离、重入幂等和 claim 单
  winner 均通过。
- 步骤 7 已完成：四个 V2 同代并按 `+0/+2/+4/+6` 独立释放，最大并发 2；
  四个真实 delivery 的冻结输入、确定性、120 秒超时、父 generation fence、
  late 后继续执行和失败隔离均通过。
- 功能 MVP 已完成：真实 coordinator/repository/executor 配合受控 recorder
  走过 21 次 claim、子进程回调、原子提交和 25 次 target acceptance；重入不
  增加 run/prediction。24/25 时真实 08:00 watchdog 永久写入 `BREACHED`，
  08:01 补齐不回写；正常 25/25 为 `MET`，缺 visibility receipt 仍算 missing。
- 候选已实现 06:30 readiness 后单次冻结、Native/V2 双池、三层账本、
  attempt fence、原子提交和动态 21/25 健康投影；07:45/08:00 可幂等补写，
  后续源数据修正不重启当前 occurrence。
- 生产仍为 `legacy`，`bond_db` 保持 migration 017 且 ledger 表为空；三个
  0629 仅处于受控兼容桥，其他 Native 禁止 fallback，该桥尚无生产资格。
- 本轮只证明分层功能 MVP：真实 21 算法同轮、07:55 容量、生产 clone 迁移、
  generation 长期归档/磁盘上限、20+20 样本、故障注入和连续 10 日均未通过；
  admission 继续 `BLOCKED`。
- cache 候选虽具备单 prewarmer、不可变 generation、原子 pointer 和硬配额，
  但完整 cached-vs-cold 内部字段证据不足；DataBridge 与 Native 同机
  forced-cold 也未准入。
- migration runner 已在隔离 MySQL 覆盖 017 clean/legacy/APPLYING 恢复；
  生产同构 clone 的 018 partial-DDL 演练仍未完成，未授权当前生产执行。

权威设计与门禁见[日频信号 08:00 SLA 架构](architecture/DAILY_SIGNAL_SLA.md)。

详细证据见[首个生产灰度激活记录](blackbox_v2/records/PRODUCTION_GRAY_ACTIVATION_20260720.md)、[1Y T+5 四方案分阶段记录](blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md)和[Blackbox V2 试验记录](blackbox_v2/records/README.md)。

## Native V1 当前摘要

- 版本化政策清单保留 29 个 Native V1 仓库身份；新增身份继续由机器门禁拒绝。
- 当前业务展示目标覆盖 `1Y/3Y/5Y/7Y/10Y`，具体 active 范围以 Registry 和 API 为准。
- 5Y/7Y 周点值方案已使用 `no_signal_to_flat_v1` 处理算法有效无信号；异常、缺数和执行失败仍然 fail-closed。
- 10Y 周点值方案的历史周历和输入冲突尚未通过数据治理修复，不能用补平或修改算法绕过。
- 两个 Full-OOS 灰度方案已经观察到真实 scheduler 触发，历史细节只保留在状态快照中。

## 当前观察项

1. 下一步先在生产同构脱敏 clone 演练 migration 018 的 apply、重复执行、
   断连和 `APPLYING` 恢复；当前生产仍停留在 migration 017。
2. 随后三个 0629 方案逐个改为公共 generation adapter，每个方案独立执行
   CompareGate 和 commit；若触及 L2 算法语义则停止并改走 Blackbox V2 replacement。
3. 完成功能候选上的一次真实 21 算法全量联跑后，才进入单 Mac cache/I/O
   容量优化、20 次 forced-cold、20 次 revision/suffix、故障注入和 07:55 门禁。
4. generation 长期归档/去重/磁盘上限、019 composite FK 与连续 10 个交易日
   25/25 仍是生产切换前置条件；在此之前 rollout 保持 `legacy`、admission
   保持 `BLOCKED`。
5. 随 target 到达持续复验 pending gray live 的 actual join、指标 API 和前端准确率展示；不得人工补 actual。
6. 使用更多独立真实交付继续覆盖周平均和月频任务；每个新方案继续执行独立生产准备检查，不复用已有方案授权。

## 权威入口

- 方案入库：[统一入库导航](onboarding/README.md)
- 上游交付：[Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台操作：[Blackbox V2 平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[Blackbox V2 生产准备清单](blackbox_v2/PRODUCTION_READINESS.md)
- 历史状态：[状态记录索引](records/status/README.md)
