# SOP 文档索引

**文档状态**：`CURRENT`

**适用运行时**：`common`、`blackbox_v2`、`native_adapter`

**目标读者**：上游算法工程师、平台入库人员、Native V1 维护人员

**最后核验日期**：2026-07-20

本目录只保存可执行 SOP 和旧路径兼容页。所有方案先从[统一入库导航](../onboarding/README.md)判断场景，再按本页选择唯一操作文档。

## 1. 按读者选择

| 你是谁 | 你要做什么 | 唯一入口 |
|---|---|---|
| 上游算法工程师 | 交付后续新增算法方案 | [Blackbox V2 上游交付 SOP](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)；算法侧只需要阅读这一份 |
| 平台入库、运行和审计人员 | 接收并验收 Blackbox V2 方案 | [Blackbox V2 平台入库 SOP](BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md) |
| Native V1 维护工程师 | 判断既有 Native 方案能否修改 | [Native V1 存量维护 T0](NATIVE_V1_MAINTENANCE_T0.md) |
| Native V1 维护工程师 | 实施已经通过 T0 的维护 | [Native V1 存量维护 SOP](NATIVE_V1_MAINTENANCE_SOP.md) |
| Native V1 验证工程师 | 验证既有 Native 方案修改 | [Native V1 修改后验证 SOP](NATIVE_V1_POST_CHANGE_TEST_SOP.md) |
| 从旧报告或旧链接进入的读者 | 找到现行流程 | 只使用历史兼容页中的跳转，不执行历史流程 |

新算法、新方案 ID、新目标、新任务类型和替代版本一律使用 Blackbox V2。Native V1 文档只用于政策清单内既有方案的维护。

## 2. 完整文档清单

| 文档 | 状态 | 运行时 | 用途 | 是否可直接执行 |
|---|---|---|---|---|
| [BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) | `CURRENT` | `blackbox_v2` | 上游算法交付、运行和自验契约 | 是，上游算法唯一手册 |
| [BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md](BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md) | `CURRENT` | `blackbox_v2` | 平台收包、Gate、登记和专项授权操作 | 是，按文档权限边界执行 |
| [NATIVE_V1_MAINTENANCE_T0.md](NATIVE_V1_MAINTENANCE_T0.md) | `LEGACY_MAINTENANCE` | `native_adapter` | Native 存量维护的身份与改动分级判断 | 仅限既有 Native 方案 |
| [NATIVE_V1_MAINTENANCE_SOP.md](NATIVE_V1_MAINTENANCE_SOP.md) | `LEGACY_MAINTENANCE` | `native_adapter` | Native 存量方案维护流程 | 仅限既有 Native 方案 |
| [NATIVE_V1_POST_CHANGE_TEST_SOP.md](NATIVE_V1_POST_CHANGE_TEST_SOP.md) | `LEGACY_MAINTENANCE` | `native_adapter` | Native 修改后的 Gate 与回归验证 | 仅限既有 Native 方案 |
| [SCHEME_ONBOARDING_T0.md](SCHEME_ONBOARDING_T0.md) | `HISTORICAL` | `native_adapter` | 保留旧 T0 链接并跳转现行文档 | 否 |
| [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md) | `HISTORICAL` | `native_adapter` | 保留旧入库 SOP 链接并跳转现行文档 | 否 |
| [SCHEME_POST_ONBOARDING_TEST_SOP.md](SCHEME_POST_ONBOARDING_TEST_SOP.md) | `HISTORICAL` | `native_adapter` | 保留旧测试 SOP 链接并跳转现行文档 | 否 |

## 3. 文档状态

| 状态 | 含义 |
|---|---|
| `CURRENT` | 当前权威操作文档，适用于其声明的运行时和读者 |
| `LEGACY_MAINTENANCE` | 只维护既有 Native V1 身份，禁止用于新增方案 |
| `HISTORICAL` | 仅保留旧链接兼容和审计入口，不得作为操作依据 |

## 4. 场景决策

1. 新算法、新 ID、新 target、新 task type 或替代版本：上游读 Blackbox V2 上游交付 SOP，平台读 Blackbox V2 平台入库 SOP。
2. 既有 Native V1 故障、数据口径或复现性修复：先执行 Native V1 存量维护 T0，通过后再进入维护和修改后验证 SOP。
3. 从旧报告进入 `SCHEME_*` 文档：只跟随页面中的现行入口，不执行归档正文。
4. 查询具体方案版本、运行结果或当前状态：查看状态文档或试验台账，不在通用 SOP 中查找。

## 5. 维护规则

- `docs/sop/` 新增、重命名或删除 Markdown 文件时，必须同步更新本索引的完整文档清单。
- 每份现行 SOP 必须声明文档状态、适用运行时、目标读者和最后核验日期。
- 通用 SOP 不记录具体方案状态、运行 ID、快照 ID 或一次性测试结论。
- 上游外发材料只提供 Blackbox V2 上游交付 SOP，不附带平台、Native 或历史文档。
- 历史兼容页只负责跳转，保持简短，不恢复成可执行流程。
- 文档中的机器字段、任务组合和命令必须与代码契约及自动测试一致。
