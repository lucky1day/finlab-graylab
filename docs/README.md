# 文档索引（Bond Factor Lab Docs）

**更新日期**: 2026-06-09

本目录是 Bond Factor Lab 的全部设计、规范、执行与运维文档。按用途分类如下。**新人从「① 架构与设计」入门，新增方案看「③ 规范与契约」，运维看「④ 运维与部署」。**

---

## ① 架构与设计（理解系统怎么搭的）

| 文档 | 内容 | 何时读 |
|------|------|--------|
| [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) | **代码架构主蓝图**：分层模型、包依赖方向规则、运行时调用图、扩展模型。所有设计文档的总索引 | 想懂代码怎么组织、谁能依赖谁 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | **系统架构**：部署拓扑、DB schema、API 契约、数据流 | 想懂部署/数据库/接口 |
| [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) | 强约束 harness **边界总纲**：分层边界、DB 安全边界、验收证据 | 想懂强约束的红线 |
| [HARNESS_DESIGN.md](HARNESS_DESIGN.md) | harness **实现级设计**：目录树、GateResult/Context 契约、各 Gate、StaticGate 判定规则、CLI、授权机制 | 想懂/改 harness 代码 |
| [DATA_LAYER_DESIGN.md](DATA_LAYER_DESIGN.md) | 统一公共层（数据接入）设计：周频去重、calendar_service、InputArtifact 强化 | 想懂/改 shared 数据层 |

## ② 规范与契约（新增方案必读）

| 文档 | 内容 |
|------|------|
| [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md) | **新增方案 SOP**：命名规范、目录/config/predict 模板、Gate 入库流程、回滚、FAQ |
| [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) | **方案契约形式化**：config.yaml schema、predict.py 接口、PredictionRecord/extra 必填、core 约束（机器可校验） |

## ③ 状态与记录（项目当前状态与历史结论）

| 文档 | 内容 |
|------|------|
| [CURRENT_STATUS.md](CURRENT_STATUS.md) | 各方案当前状态、run_id、是否 active、架构重构与 harness 落地进展（单一状态来源） |
| [HISTORICAL_REPRODUCTION.md](HISTORICAL_REPRODUCTION.md) | 历史回测复现口径与结论 |
| [TEST_PLAN.md](TEST_PLAN.md) | 测试计划与 Gate 验收矩阵 |
| [TEST_MACHINE_BASELINE.md](TEST_MACHINE_BASELINE.md) | 测试机环境基线 |

## ④ 运维与部署

| 文档 | 内容 |
|------|------|
| [DEPLOYMENT.md](DEPLOYMENT.md) | 部署与运行操作手册（launchd、端口、环境变量） |
| [PRD.md](PRD.md) | 产品需求基线（定位、目标、非目标） |
| [IFRAME_INTEGRATION.md](IFRAME_INTEGRATION.md) | panda_quantflow 外层 iframe 接入说明 |

---

## 归档区（历史产物，保留追溯，非当前有效）

- **[archive/](archive/)** — 已完成阶段的历史记录、执行计划与一次性产物
  - 执行计划（已完成）：`ARCH_EXECUTION_PLAN.md`（架构重构 S0–S8）、`WEEKLY_LIVE_ROLLOUT_PLAN.md`（周度 live 上线）
  - 历史记录：`IMPLEMENTATION_PLAN.md`（Phase 1–11 搭建历史）、`MIGRATION_PLAN.md`、`RESEARCH.md`、`CLEANUP_PLAN.md`
- **[legacy_sources/](legacy_sources/)** — 方案算法的**原始来源脚本**（审计留档，不被代码 import）
  - `legacy_weekly_5y_direct_production_0529.py`、`legacy_weekly_7y_cross_d_overlay_0529.py`
  - 注：`weekly_10y` 的 legacy 仍在 `schemes/weekly_10y_d_overlay/core/`，因 `predictors.py` **运行时复用**它，不可移动。

---

## 阅读路径建议

- **第一次接触项目** → ① CODE_ARCHITECTURE → ARCHITECTURE → ② SOP
- **要加一个新预测方案** → ② SCHEME_ONBOARDING_SOP + SCHEME_CONTRACT，用 `python -m harness onboard {scheme_id} --stage all` 驱动
- **要改数据层/harness** → ① DATA_LAYER_DESIGN / HARNESS_DESIGN；重构历史见 archive/ARCH_EXECUTION_PLAN
- **要部署/运维** → ④ DEPLOYMENT
