# Phase-A `secure_runtime` 假控制面退役设计

**文档状态**：`APPROVED_DESIGN`

**确认日期**：2026-08-11

## 目标

删除 Phase-A 缓存内部已经无法启用的 `secure_runtime` 参数、仅写路径中永远为 false 的 `secure` 分支，以及上次退役 v1 缓存迁移后遗留的无用 `root` 参数传递，使代码直接表达当前唯一行为，同时保持生产缓存、算法结果和失败语义不变。

## 已验证现象

- `prepare_phase_a_caches()` 的 publisher 与 consumer 调用都固定传入 `secure_runtime=False`，没有外部参数、环境变量或其它调用者可以启用它。
- `_validated_consumer_hit()` 接收 `secure_runtime`，但函数体不读取该值。
- `_prepare_under_family_lock()` 的 `root` 参数已无使用者；它原先只服务已经删除的 v1 松散缓存迁移。
- `_create_generation()`、`_validate_staged_generation()`、`_cleanup_staging_directories()`、`_prune_generations()`、`_switch_current_generation()` 和 `_discard_unpublished_generation()` 都只有当前模块内的唯一调用路径，且其 `secure` 实参永远为 false。
- 当前 `is_publisher` 同时表示正式 publisher 和使用私有根目录的 `private_build` consumer；该名称没有准确表达“允许修改当前 cache root”的真实语义。
- consumer 仍通过 `secure=True` 读取 current generation，并无条件验证 generation acceptance lineage；底层安全读取能力仍有真实用途。
- parent generation 的 lineage 回读仍显式使用 `secure=True`，因此共享读取函数的 `secure` 参数不是死代码。

## 设计决策

### 唯一数据流

```text
可修改 cache 的执行
（正式 publisher 或 private_build）
→ family 独占锁
→ 普通方式读取 current
→ 构建并校验 immutable generation
→ 原子切换 current

consumer
→ 安全方式读取 current
→ 校验输入等价、日期覆盖和 acceptance lineage
→ 只读返回 current cache
```

不再保留一个永远为 false 的运行模式参数来表达这两条固定路径。内部布尔量使用 `can_mutate_cache`，不再把 private-build consumer 错称为 publisher。

### 最小代码边界

- 将当前内部 `is_publisher` 改名为 `can_mutate_cache`；判定规则仍为“正式 publisher 或 `private_build`”。
- 从 `_prepare_under_family_lock()` 删除 `secure_runtime` 和未使用的 `root` 参数，并同步使用 `can_mutate_cache`。
- 从 `_validated_consumer_hit()` 删除未使用的 `secure_runtime` 参数。
- 将 current generation 的读取规则直接表达为 `secure=not can_mutate_cache`。
- 删除可写路径截断输入分支中永远不可达的 `if secure_runtime` lineage 分支。
- 从仅服务可写路径的 create、staged validation、staging cleanup、prune、current switch 和未发布 candidate 清理函数删除 `secure` 参数及其不可达分支；它们继续执行当前普通文件路径。
- 保留 `_load_current_generation()` 和 `_load_generation_directory()` 的 `secure` 参数及全部文件、目录、owner、mode、symlink 和 lineage 校验，因为普通 consumer 与 parent lineage 仍实际使用 `secure=True`。

## 不做事项

- 不改变 publisher 或 consumer 身份规则。
- 不将 private-build consumer 提升为正式 publisher；`can_mutate_cache` 只描述其私有根目录中的当前执行能力。
- 不改变 manifest v3、current pointer、generation acceptance 或 cache pickle 格式。
- 不修改 `NON_PRODUCTION`、`native_generation=null`、`native_generation_changed=false` 兼容字段。
- 不修改缓存 hash、content identity、spec fingerprint、CompareGate 证据或 retention 策略。
- 不读取、重写、迁移或删除现有生产缓存文件。
- 不新增配置、环境变量、fallback、兼容分支或安全抽象。
- 不修改 Native 算法、Harness、数据库、调度、plist、Backend 或前端。

## 错误语义

错误继续直接暴露：

- consumer 无 current、current 非法、输入不等价、覆盖不足或 lineage 校验失败时，继续返回 `CACHE_PUBLISHER_REQUIRED`。
- 可写路径的构建、容量校验、generation 发布或清理失败时，继续按现有异常路径退出。
- 本次不增加重试、旧缓存回退或隐式重建路径。

## 验收设计

聚焦验证必须证明：

- 正式 publisher 的 cold build、current hit 和增量构建行为不变；private build 继续只能写其显式绝对私有根目录。
- 增加一个聚焦 consumer 回归测试：普通 consumer 通过安全 current reader 命中合法 current，current 非法时继续返回 `CACHE_PUBLISHER_REQUIRED`。
- `secure_runtime` 在当前代码和测试中归零。
- `_prepare_under_family_lock()` 不再接收无用 `root`。
- mutation-only helpers 不再暴露不可启用的 `secure` 参数。
- `_load_current_generation()`、`_load_generation_directory()` 的 consumer 安全读取和 parent lineage `secure=True` 调用仍然存在。
- Phase-A 聚焦测试、架构测试和全量回归全部通过。

## 文档生命周期

本文只服务尚未实施的设计确认。当前权威文档没有定义 `secure_runtime`，因此实施不需要修改 `CURRENT_STATUS.md` 或新增 ADR。实施完成并通过验证后删除本文；历史决策通过 Git 提交追溯，不把已完成设计保留为当前操作入口。
