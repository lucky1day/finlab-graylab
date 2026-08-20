# Phase-A Cache Prune 可观察性设计

**状态**：待实施

**日期**：2026-08-21

## 目标

保持“publication 成功后，历史 generation 清理失败不能让预测失败”的现有语义，同时让持续清理
失败立即出现在现有 cache audit 和 warning 日志中。

## 最小设计

- `_prune_generations(...) -> tuple[str, ...]` 返回未能完成清理的 generation ID。
- 删除单个 generation 失败时记录该 ID，并继续清理后续 ID。
- generation 目录 fsync 失败时，保守地返回本次计划清理的全部 ID。
- `_generation_audit()` 默认包含 `prune_deferred: []`；publisher 在 publication 后以实际结果覆盖。
- `prune_deferred` 非空时只输出一条 warning，包含 cache family、tenor 和 deferred generation IDs。
- `_prune_generations` 出现未预期普通异常时，外层仍保持 publication 成功，并把全部计划 ID 记为
  deferred 后输出同一条 warning。

## 非目标

- 不增加 daemon、重试、队列、数据库表、调度任务或新的 audit 文件。
- 不改变 retention/capacity 规划、current pointer、publication commit point 或预测成功语义。
- 不捕获 `KeyboardInterrupt` / `SystemExit`。
- 不修改 Native core、算法结果、cache manifest 或 generation 身份。

## 验收

- 单个删除失败：后续 generation 继续删除；audit 精确列出失败 ID；warning 只发一次。
- 整体 prune 普通异常：candidate 保持 published；所有计划 ID 出现在 audit；warning 只发一次。
- generation-root fsync 失败：candidate 保持 published；计划 ID 进入 audit；warning 只发一次。
- 正常 prune：`prune_deferred == []`，不输出 prune warning。
- 原有 publication、suffix、private-cache 与全量测试全部通过。
