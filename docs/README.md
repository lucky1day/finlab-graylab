# 文档索引（Bond Factor Lab Docs）

**更新日期**: 2026-06-10

> 2026-06-10 文档清理：已实现功能的计划/设计文档已删除，仅保留 8 个参考文档 + 2 个 SOP。

## 架构

| 文档 | 内容 |
|------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统架构：部署拓扑、DB schema、API 契约、数据流 |
| [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) | 代码架构主蓝图：分层模型、包依赖方向、运行时调用图 |
| [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) | 强约束 harness 边界总纲：分层边界、DB 安全边界 |
| [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) | 方案契约：config.yaml schema、predict.py 接口、core 约束 |

## 操作

| 文档 | 内容 |
|------|------|
| [sop/SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) | 新增方案入库 SOP |
| [sop/SCHEME_POST_ONBOARDING_TEST_SOP.md](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md) | 入库后测试验证 SOP |

## 状态与参考

| 文档 | 内容 |
|------|------|
| [CURRENT_STATUS.md](CURRENT_STATUS.md) | 项目当前状态（单一来源） |
| [bond_factor_lab_architecture_review.md](bond_factor_lab_architecture_review.md) | 架构评审报告（2026-06-09） |

## 阅读路径

- **新人入门** → CODE_ARCHITECTURE → ARCHITECTURE → SCHEME_CONTRACT
- **新增方案** → SCHEME_CONTRACT + SCHEME_ONBOARDING_SOP，用 `python -m harness onboard {scheme_id} --stage all` 驱动
- **改 harness** → HARNESS_ARCHITECTURE + 评审报告
- **了解当前状态** → CURRENT_STATUS
