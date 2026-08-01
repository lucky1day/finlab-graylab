# 低风险仓库卫生清理设计

> **文档状态：TEMPORARY。** 本文只用于本轮实施；完成并验证后从工作树删除，Git 历史保留审计轨迹。

日期：2026-08-01

## 目标

删除已经完成使命、没有现行生产入口或外部仓库引用的定点修复脚本，以及只承担已完成实施过程记录的内部文档。本批次只降低仓库体积和维护表面，不修改任何生产行为。

## 删除范围

### 已完成的一次性脚本

- `scripts/backfill_prediction_semantics.py`：只处理早期固定 run 的 prediction semantics 回填。
- `scripts/repair_live_prediction_semantics.py`：只处理早期固定 live run 的语义修复。
- `scripts/delete_bad_live_predictions.py`：只删除已经审计确认的固定坏 run。
- `scripts/delete_retired_weekly_average_schemes.py`：只清退已经退役的三个 point-backed 周平均身份。

四个脚本共 1,021 行。删除前再次使用文件名和入口扫描确认没有外部代码、文档、plist 或测试引用。`repair_live_prediction_semantics.py` 对 `delete_bad_live_predictions.py` 的内部提示不构成外部消费者，两者必须同批删除。

### 已完成的内部过程文档

- `docs/internal/plans/2026-07-01-daily-0629-sop-closure.md`
- `docs/internal/specs/2026-07-12-liwei-0616-incremental-phase-a-cache-design.md`
- `docs/internal/specs/2026-07-12-liwei-full-oos-gray-models-design.md`

三份正文共 456 行，只由内部索引引用，且均声明为历史、已完成或已实施。现行规则由架构、SOP、方案配置和 Git 历史承担。删除正文时同步移除 `docs/internal/plans/README.md` 与 `docs/internal/specs/README.md` 对应索引项。

## 明确保留

- `docs/internal/specs/2026-07-21-t5-no-foreign-lgbm-ablation-design.md`：仍是待实施设计，尚未明确取消。
- `docs/operations/CLOUD_ENVIRONMENT.md`：历史但与环境依赖说明相关，留待独立决策。
- 所有 benchmark 构建、源算法认证和通用 Blackbox onboarding 脚本。
- `scripts/apply_migrations.py`、`scripts/daily_coordinator_epoch_operator.py` 和当前生产健康检查脚本。
- `scheduler/`、`harness/`、`backend/`、`shared/`、`backtests/`、`schemes/`、`deploy/`、数据库和 launchd 状态。
- `daily-real-replay` 与 ledger coordinator 闭环；它们属于后续独立模块清理，不与本批混合。

## 实施顺序

1. 记录当前工作树、分支、候选文件行数和外部引用扫描结果。
2. 删除四个一次性脚本，运行静态边界和相关语义测试，独立提交。
3. 删除三份内部过程文档并清理索引，运行文档守卫，独立提交。
4. 运行完整 pytest、compileall、残留引用扫描和生产目录零改动检查。
5. 删除本设计及对应实施计划和索引项，提交临时记录清理。

## 验收标准

- 精确删除 7 个目标文件，正文净减少至少 1,477 行。
- 四个脚本名、三份文档名在当前工作树中没有残留引用。
- `tests/test_prediction_semantics.py`、`tests/test_onboarding_docs.py`、`tests/test_architecture_boundaries.py` 通过。
- 完整 pytest 通过，收集用例数不因本批下降。
- `python -m compileall` 通过。
- `scheduler/`、`harness/`、`backend/`、`shared/`、`backtests/`、`schemes/`、`deploy/`、`migrations/` 无改动。
- 当前分支保持 `codex/audit-bugfixes-20260613`；`master`、远程分支和生产状态不变。

## 回滚

本批不执行数据库写入、不操作 launchctl、不删除运行产物。若发现遗漏消费者，直接从本批提交的父提交恢复对应文件；不需要数据回滚。
