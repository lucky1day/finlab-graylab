# 文档索引（Bond Factor Lab Docs）

**更新日期**: 2026-06-12

> 当前文档入口以本文为准。历史评审报告保留原始语境，不作为最新状态来源；最新状态只看 [CURRENT_STATUS.md](CURRENT_STATUS.md)。

## 架构

| 文档 | 内容 |
|------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 系统架构：部署拓扑、DB schema、API 契约、数据流 |
| [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) | 代码架构主蓝图：分层模型、包依赖方向、运行时调用图 |
| [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) | 强约束 harness 边界总纲：分层边界、DB 安全边界 |
| [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) | 方案契约：config.yaml schema、predict.py 接口、core 约束 |
| [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) | 预测日期与实盘阶段强制语义：`predict_date` / `feature_date` / `target_date` / `prediction_phase` |

## 操作

| 文档 | 内容 |
|------|------|
| [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md) | 新增方案前必读 T0 强约束范式（daily / weekly 通用） |
| [sop/SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) | 新增方案入库 SOP（含 2026-06-10 修订的数据口径规则） |
| [sop/SCHEME_POST_ONBOARDING_TEST_SOP.md](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md) | 入库后测试验证 SOP |
| [sop/PITFALLS_2026-06-10.md](sop/PITFALLS_2026-06-10.md) | 2026-06-10 框架改造踩坑记录（新增方案前必读） |

## 状态与参考

| 文档 | 内容 |
|------|------|
| [CURRENT_STATUS.md](CURRENT_STATUS.md) | 项目当前状态（单一来源） |
| [bond_factor_lab_architecture_review.md](bond_factor_lab_architecture_review.md) | 架构评审报告（2026-06-09） |
| [bond_factor_lab_framework_review_2026-06-10.md](bond_factor_lab_framework_review_2026-06-10.md) | 框架评审报告（2026-06-10，历史审计语境） |

## 阅读路径

- **新人入门** → CODE_ARCHITECTURE → ARCHITECTURE → PREDICTION_SEMANTICS → SCHEME_CONTRACT → CURRENT_STATUS
- **新增方案** → 先读 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) + [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md) → 再读 [sop/PITFALLS_2026-06-10.md](sop/PITFALLS_2026-06-10.md) + SCHEME_CONTRACT + SCHEME_ONBOARDING_SOP，用 `python -m harness onboard {scheme_id} --stage all` 驱动
- **改 harness** → HARNESS_ARCHITECTURE；评审报告只作历史审计参考
- **了解当前状态** → CURRENT_STATUS
