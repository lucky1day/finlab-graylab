# 当前状态

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台运维和审计人员

**最后核验日期**：2026-07-21

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
- 每方案已有 39 条连续 `gray_live`：`predict_date=2026-05-26..2026-07-20`、`feature_date=2026-05-25..2026-07-17`、`target_date=2026-06-01..2026-07-24`；历史/live target overlap 为 0，`scheduled_live=0`。
- 其中每方案 38 条历史缺口通过独立 `gray_backfill_write` token 和 insert-only `gray-backfill` Gate 补齐，统一绑定 generation `full-20260720-055026-00e12e3803a8` 与 snapshot `snapshot-fd8a1f8736d3a4d057fbd98e`；首条部署日 gray live 保留原记录。
- 前端在 `1Y国债活跃 × T+5` 格子内显示 4 个短名称；2026-05 是历史末月，实盘分隔线位于 2026-06 前，文案分别说明信号发出起点和“灰度实盘（目标期）”起点。
- 四方案已达到 `Onboarding Complete`；尚未达到 `Production Observed`，因为下一交易日自然 scheduler 尚未产生 `scheduled_live`。
- 2026-07-21 的人工重启未在 07:03 前执行，四方案当天没有自然 `scheduled_live`；旧 scheduler 同时在 07:03 用过期 discovery 回写 Registry 长名称。12:22 已安装 V2 独立日级 Gate 和自动重启控制并重载 scheduler：四个 V2 job 已挂载，今天以 blocked 凭证拒绝 startup catchup，17 个 Native V1 运行保持 17/17，Registry、本地及公网 API 已恢复四个短名称；下一交易日四阶段自然运行和 `scheduled_live` 仍待观察。
- 四个日频算法来自同一上游批次，证明了日频 Blackbox 运行路径，但不等于四个独立交付包，也不覆盖月频。

详细证据见[首个生产灰度激活记录](blackbox_v2/records/PRODUCTION_GRAY_ACTIVATION_20260720.md)、[1Y T+5 四方案分阶段记录](blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md)和[Blackbox V2 试验记录](blackbox_v2/records/README.md)。

## Native V1 当前摘要

- 版本化政策清单保留 29 个 Native V1 仓库身份；新增身份继续由机器门禁拒绝。
- 当前业务展示目标覆盖 `1Y/3Y/5Y/7Y/10Y`，具体 active 范围以 Registry 和 API 为准。
- 5Y/7Y 周点值方案已使用 `no_signal_to_flat_v1` 处理算法有效无信号；异常、缺数和执行失败仍然 fail-closed。
- 10Y 周点值方案的历史周历和输入冲突尚未通过数据治理修复，不能用补平或修改算法绕过。
- 两个 Full-OOS 灰度方案已经观察到真实 scheduler 触发，历史细节只保留在状态快照中。

## 当前观察项

1. 下一交易日观察已安装的 V2 preflight 自然执行 06:00 首刷、06:30 检查、06:35 条件重刷和 07:00 最终检查，并验收四个 1Y T+5 方案在 07:33/07:35/07:37/07:39 的自然 `scheduled_live`。
2. 随 target 到达持续复验 pending gray live 的 actual join、指标 API 和前端准确率展示；不得人工补 actual。
3. 使用更多独立真实交付继续覆盖周平均和月频任务；每个新方案继续执行独立生产准备检查，不复用已有方案授权。
4. DataBridge 当日刷新失败时继续阻断 `data_bridge_current` 方案，不影响 Native V1 的 `legacy_db` 路径。

## 权威入口

- 方案入库：[统一入库导航](onboarding/README.md)
- 上游交付：[Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台操作：[Blackbox V2 平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[Blackbox V2 生产准备清单](blackbox_v2/PRODUCTION_READINESS.md)
- 历史状态：[状态记录索引](records/status/README.md)
