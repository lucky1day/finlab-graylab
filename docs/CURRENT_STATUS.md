# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-09

本文只保留当前已验证事实。带日期的证据见[状态记录](records/status/README.md)，未完成工作的顺序见
[统一后续推进计划](TODO.md)，生产调度规则以
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。

## 当前政策

- `launchd + installed plist` 是唯一生产调度控制面；仓库代码或模板不单独证明生产挂载。
- 自然时钟写 `scheduled_live`；受控、insert-only 的历史修复写 `gray_live`，两者不可互相替代。
- ledger、occurrence、epoch、daily-gray、常驻 APScheduler 和旧预检不再是生产或过渡路径。
- 新的 installed plist、launchctl、服务、激活、业务写入、持久化回测和 DDL 均需独立授权。
- Blackbox Admission 与 Backend 手动预测入口已退役；active 生命周期是唯一自动调度资格。

## 当前生产验收

- 截至 2026-08-07 的 active live 信号只读报告为 `expected=863`、`present=863`、`missing=0`。
- 唯一 Native 日频缺口使用封存 `feature_date=2026-08-04` 输入完成受控 cache 预热，并由 run `2256`
  insert-only 写入一条 `gray_live`；没有重跑既有 Blackbox 缺口或触发 DataBridge/launchd 任务。
- Dashboard 每次直接读数据库；最新验收返回 `stale=false`、`snapshot_age_ms=0`，对应方案状态为 `present`。
- 公网 rollout 验收为 `PASS=60`、`FAIL=0`。旧 backtest JSON 本机生成约 0.25 秒；公网探针使用现有 gzip
  表示后传输约 239 KB，不再依赖扩大超时、重试或 fallback。
- 截至 2026-08-08，生产基线 `master` 已推送至 `49d77da`；后续开发提交不自动获得发布或生产操作授权。

## 已闭环的生产治理

- Blackbox Admission 已完整退役；历史补缺仅保留必须指定单日的 Harness 运维入口，且只写 `gray_live`。
- G1/G2 的 DataBridge 与 launchd-only single-writer、G3 的 8 月 3 日补写、G4 唯一键修复、G5/G6
  无写库验收均已闭环。
- launchd-only 清理已移除仓库中的 ledger/occurrence/epoch runtime、policy 与 replay 闭包；
  `t_input_generations` lineage、017 migration 与受控 recovery 测试仍保留。

## 已验证的 7Y 灰度闭环

- 两套 active 7Y v2 方案已完成 Gate、入库、历史 `gray_live`、served API 与前端读回。
- 它们在 config、exact version 与 Registry 均 active 且 cadence 匹配时进入 launchd one-shot 候选；这不证明 installed plist 已挂载。

## 已闭环的前端 UX

- 任务格高度为 `76px`，趋势图与空态为 `244px`，矩阵不再保留 `min-height: 560px`。
- 当前筛选后的最优方案仅在 `overall`、`upPrecision` 或 `downPrecision >= 60` 时显示红色准确率数字；
  samples、缺失值及低于阈值的结果不高亮。
- CSS 使用精确内容版本 URL；前端行为与静态缓存合同由测试维护，不再依赖一次性实施计划。

## 文档治理

- 当前架构、SOP、正式 onboarding/状态/审计记录继续保留。
- 13 份已完成且由现行资料、代码和测试替代的 `docs/superpowers` 过程文档已删除；Git 历史保留其过程。
- Weekly 10Y stable-order 设计与计划仍被 Native core、回归测试和用户工作区记录引用，作为源算法保真证据保留。

## 未完成的生产治理

- G7.1：Native 版本模型等待 G7.0 目标语义确认；确认前不改变算法、版本、Registry 或控制面。
- G8.1：物理 schema/archive 最终退役等待 G8.0 保留边界确认；installed plist、migration 与 DDL 仍需独立授权。
