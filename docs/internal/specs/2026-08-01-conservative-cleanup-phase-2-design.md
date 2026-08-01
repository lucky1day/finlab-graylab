# 第二阶段保守清理设计

**文档状态**：`HISTORICAL`

**目标**：只删除能够证明已完成、无当前调用者且不承担生产或审计复现职责的文件。

## 审计结论

本批删除 `scripts/backfill_live_predictions.sh`。该脚本明确是一次性工具，只处理
`t1_daily`、`t5_daily` 在 2026-06-01 至 2026-06-09 的历史灰度缺口；当前代码、
launchd、测试和现行 SOP 均不调用它，只有历史审计以过去时记录其曾经存在。

`docs/internal/plans/README.md` 已没有任何计划条目，只剩标题与元数据。删除该空索引，
并同步移除 `docs/internal/README.md` 中的导航项。历史审计原文保持不变。

## 明确保留

- `scripts/h2_incremental_cache_operator.py`：仍对应尚未完成的历史缺口处理。
- benchmark rebuild/build 工具：仍服务 active 方案的源算法保真和复现。
- coordinator epoch、cache、DataBridge 工具：仍有当前运维或待切换职责。
- CURRENT 文档、生产验收 evidence、状态与审计记录：不属于本批范围。

## 验证

删除后检查全仓引用、文档门禁、Shell/Python 编译、完整 pytest，以及相对基线的生产
目录零差异。验证完成后删除本设计和对应实施计划，使最终工作树不保留中间产物。
