# Native V1 存量方案文档管理

**文档状态**：`LEGACY_MAINTENANCE`
**适用运行时**：`native_adapter`
**目标读者**：维护现有原生方案的平台工程师
Native V1 只维护 `deploy/onboarding_policy_v1.json` 登记的存量方案。本文档域不再定义任何新增方案流程；具体数量只在当前状态页维护。

## 1. 允许的工作

- 修复平台 I/O、日期字段、审计字段或运行适配问题。
- 修复已经确认的数据口径、历史复现或未来数据泄漏问题。
- 恢复原始算法明确要求但在平台迁移中丢失的行为。
- 调整不影响算法语义的执行预算、日志和故障恢复配置。
- 对现有结果、数据库记录和 API 可见性做只读核验。

所有修复必须保留 `scheme_id`、`runtime_type=native_adapter`、目标组合、Registry 身份和历史记录。

## 2. 禁止的工作

- 新增 Native V1 方案 ID。
- 在现有 ID 下替换为新算法、扩展 target 或新增 task type。
- 为单个方案修改公共输入、日历、Harness、数据库或前端规则。
- 把 Blackbox 两文件交付改造成 `predict.py + core/`。
- 修改白名单以绕过 Blackbox V2 Intake。

需要上述能力时，创建独立 Blackbox V2 trial；原 Native 方案保持原状态，直到替代方案完成单独授权。

## 3. 当前文档

| 文档 | 用途 |
|---|---|
| [存量维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md) | 准入、改动分级、实施、验证和副作用核验的唯一流程 |
| [Native V1 契约](SCHEME_CONTRACT.md) | `config + predict + core` 字段和代码边界 |

冻结前新增方案流程已从工作树删除，可通过 Git 历史追溯；当前维护只使用上表入口。

## 4. 维护决策

进入维护前必须回答：

1. `scheme_id` 是否在版本化 Native 白名单中？
2. 改动是否只恢复或修正已批准算法，而不是形成新算法？
3. 是否保持输入单点、写库单点、core 纯净和源算法保真？
4. 是否能够用原始证据或 live-safe oracle 验证改动？
5. 是否不需要改变公共平台契约？

任一答案为“否”时停止 Native 维护，转 Blackbox V2 或单独的平台能力改造。
