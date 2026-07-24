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
| 日频 08:00 保障 | `REMEDIATION_OBSERVATION`；ledger 协调器代码已进入候选分支，但 rollout 关闭，不能宣称 SLA 稳定 |
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

- 2026-07-24 的旧 APScheduler 路径只生成 16/21 个 scheduled run：13 success、
  3 failed、5 个未运行；该事实否定了“当前日频已稳定”的结论。
- 候选已实现单一 06:30 coordinator、两类不可变 generation、三层账本、
  attempt fence、原子提交、隔离 Native/V2 pool、动态 21/25 健康投影和容量门禁。
- 07:45/08:00 边界不再依赖一次性 Cron；同日恢复入口会幂等补写 PENDING
  guardrail/SLA。ABANDONED 只有清理确认、首轮覆盖、attempt 和剩余预算均通过
  才能二次启动。
- 两类 generation 均在 staging 完成 manifest、重开校验、`chmod/fsync`，再以
  一次目录 rename 发布；DB `sealed_at` 是权威时点。ledger 拒绝 standalone
  DataBridge publisher，刷新锁覆盖下载、发布与清理。
- 健康投影区分 committed、DB 可见回执和外部 no-store API 观察；24/25、ETA
  超线、零进展、generation 异常或 mode 漂移均不得为 `ok`。节假日 watchdog
  返回 `IDLE`，严格执行 envelope 仍保持 fail-closed。
- Native 在 06:30 后检查 T-1 日历、五个曲线锚点和三频最小历史；未就绪不创建 attempt。
  就绪后 14 个 Native 冻结 RR 输入；三个 0629 兼容桥绑定同日 generation fence 和水位。
- 06:55 是版本化 DataBridge readiness guardrail：未 SEALED/绑定即 `LATE`/告警，但仍只刷新当天新 generation 到 08:30。
- generation/DataBridge current roots 要求服务 UID + `0700`；旧 caller-supplied retention 已禁用，只回收 DB 证明无引用的 `INVALIDATED` payload。
- 2026-07-24 输入域在 06:30 后仍有写入，最晚 `create_time=07:10:12`；因此
  06:30 只作 hard not-before，readiness 后只冻结一次，后续修正不重启 occurrence。
- ledger 候选已退休旧 V2 精确分钟触发与 scheduler restart；仓库 launchd 仍
  全部显式为 `legacy`，保留切换前唯一 refresh owner。2026-07-24 13:04 本机
  `bond_db` 已建立 001..017 history 并应用 017 schema；五张 ledger 表为空，
  既有 1187 条 run 的新关联字段均为 NULL。服务未重启，新协调器没有写权。
- 仅三个固定 0629 ID 可用 `live_source_0629`；协调器校验 mode、冻结 source hash、source 输出水位和 generation fence。
  其它 Native 不可使用且 generation 失败不回退；三方案 CompareGate 337/337 行零差异，该桥尚未取得生产资格。
- forced-cold、revision/suffix、故障注入、20+20 次样本和连续 10 个交易日 25/25 均未完成。
- cache 候选具备单 prewarmer、不可变 generation、原子 pointer、硬配额和受证明
  的 daily suffix；周/月或依赖不明自动 full rebuild。生产 caller 尚无绑定
  input/spec/cache hash 的完整 cached-vs-cold 内部字段证据，故继续拒绝。
- 容量准入使用独立 collector/operator macOS CMS、root-owned trust、时效/序列
  和当前 candidate 精确重算；三套环境同时绑定 conda explicit 与含 pip 的全包
  清单，非 `forecast_env` Native 执行被拒绝，occurrence 冻结后还会二次复核。
  当前 evidence schema 仍固定 21/25 与四个 V2，扩容前须发布参数化 v3 并重新
  认证；缺 017 的预迁移探针会拒绝，默认 admission 继续 `BLOCKED`。
- pending-only runner 已在隔离 MySQL 8.0.45 通过 clean/legacy v16/零执行重跑；
  CLI 写路径必须显式 `--apply`。`017=APPLYING` 现有只读三态 inspect 和
  digest-fenced recovery，已隔离覆盖 DDL 前、四列 nullable 过渡和完整未 mark，
  失败保持 `APPLYING`。生产精确 clone 的 partial-DDL 演练仍未完成，也未授权在
  当前 `bond_db` 执行恢复。
- DataBridge refresh/pack 与 Native 的同机 forced-cold 尚未准入；晚写诊断不是
  CDC，但 MVP 已在 readiness 后冻结一次 generation，不再依赖上游永久 seal。
- generation 长期归档、去重、保留周期和磁盘满验收未完成；当前安全清理只防误删，不能解除上线门禁。

权威设计与门禁见[日频信号 08:00 SLA 架构](architecture/DAILY_SIGNAL_SLA.md)。

详细证据见[首个生产灰度激活记录](blackbox_v2/records/PRODUCTION_GRAY_ACTIVATION_20260720.md)、[1Y T+5 四方案分阶段记录](blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md)和[Blackbox V2 试验记录](blackbox_v2/records/README.md)。

## Native V1 当前摘要

- 版本化政策清单保留 29 个 Native V1 仓库身份；新增身份继续由机器门禁拒绝。
- 当前业务展示目标覆盖 `1Y/3Y/5Y/7Y/10Y`，具体 active 范围以 Registry 和 API 为准。
- 5Y/7Y 周点值方案已使用 `no_signal_to_flat_v1` 处理算法有效无信号；异常、缺数和执行失败仍然 fail-closed。
- 10Y 周点值方案的历史周历和输入冲突尚未通过数据治理修复，不能用补平或修改算法绕过。
- 两个 Full-OOS 灰度方案已经观察到真实 scheduler 触发，历史细节只保留在状态快照中。

## 当前观察项

1. 下一步在隔离环境验证 17 个 Native 由协调器唯一触发；之后逐项完成 0629
   generation 替换、迁移及容量/故障演练，门禁前不得打开 ledger。
2. 使用历史交易日 `--no-persist` 驻留回放验证 21 item/25 target、四个 V2
   同代 generation 和 `+0/+2/+4/+6` 独立释放。
3. 随 target 到达持续复验 pending gray live 的 actual join、指标 API 和前端准确率展示；不得人工补 actual。
4. 使用更多独立真实交付继续覆盖周平均和月频任务；每个新方案继续执行独立生产准备检查，不复用已有方案授权。

## 权威入口

- 方案入库：[统一入库导航](onboarding/README.md)
- 上游交付：[Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台操作：[Blackbox V2 平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[Blackbox V2 生产准备清单](blackbox_v2/PRODUCTION_READINESS.md)
- 历史状态：[状态记录索引](records/status/README.md)
