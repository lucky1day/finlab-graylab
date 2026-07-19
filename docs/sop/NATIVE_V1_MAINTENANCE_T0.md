# Native V1 存量维护 T0

**文档状态**：`LEGACY_MAINTENANCE`
**适用运行时**：`native_adapter`
**目标读者**：负责现有 Native V1 修复的工程师
**最后核验日期**：2026-07-19

本文只判断一项工作能否作为 Native V1 存量维护执行。后续新增方案一律走 [Blackbox V2](BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)。

## 1. 身份门槛

开始前确认：

- `scheme_id` 已列入 `deploy/onboarding_policy_v1.json`；
- `runtime_type` 固定为 `native_adapter`；
- `input_source` 保持 `legacy_db`；
- 不新增 target、task type 或 Registry composite 身份；
- 不以维护名义替换算法核心。

StaticGate 和 ActivationGate 会拒绝白名单之外的 Native 身份。

## 2. 改动分级

| 级别 | 定义 | Native 维护处理 |
|---|---|---|
| L0 | 平台 I/O、日期字段、extra、缓存、日志、执行预算和审计适配 | 允许，完成自动 Gate |
| L1 | 原始 runner 明确暴露的上下文参数恢复 | 允许，但必须有原始证据和逐项对比 |
| L2 | 特征、窗口、模型、阈值、投票、selector、fallback 或 score 映射改变 | 禁止；创建 Blackbox V2 trial |

数据泄漏修复、错误分组键修复等即使会改变历史结果，也必须证明是在恢复已批准的点时口径，而不是调参贴结果。

## 3. 四条不变量

1. **输入单点**：adapter 和 backtest runner 只通过 `shared.input_artifacts` 获取数据。
2. **写库单点**：实盘只由 scheduler repository 写库，回测只由 backtests repository 写库。
3. **core 纯净**：`core/` 不连接数据库、不写文件、不跨方案 import。
4. **源算法保真**：平台适配不得改变原始算法逻辑和内部数值映射。

需要修改公共输入、日历、Harness、DB schema、API 或前端时，必须拆为独立平台能力改造，不与方案维护混合。

## 4. Legacy 日期和 horizon

- Native 日频保留既有 `horizon=1/5`。
- Native 周频保留既有登记值 `horizon=6`。
- Native 月频保留既有登记值 `horizon=30`。
- 上述 `6/30` 仅是存量 Registry 身份，不适用于 Blackbox V2。
- 业务分列始终使用 `task_type`，不得从 horizon 推断。
- `feature_date` 仍是唯一数据截止日；live 和 backtest 都不得读取其后的输入。

## 5. 开工前检查

- [ ] 方案在 Native 白名单中。
- [ ] 改动已分为 L0 或有充分证据的 L1。
- [ ] 没有新增算法、目标、任务或方案身份。
- [ ] 原始证据和当前基线已经保存。
- [ ] 已定义方向、内部字段、日期和副作用验收方法。
- [ ] 不需要改变公共平台能力。

全部满足后，按 [Native V1 存量维护 SOP](NATIVE_V1_MAINTENANCE_SOP.md) 执行。
