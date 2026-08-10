# Phase-A `secure_runtime` 假控制面退役设计

**文档状态**：`APPROVED_DESIGN`

**确认日期**：2026-08-11

## 目标

删除 Phase-A 缓存内部已经无法启用的 `secure_runtime` 参数及上次退役 v1 缓存迁移后遗留的无用 `root` 参数传递，使代码直接表达当前唯一行为，同时保持生产缓存、算法结果和失败语义不变。

## 已验证现象

- `prepare_phase_a_caches()` 的 publisher 与 consumer 调用都固定传入 `secure_runtime=False`，没有外部参数、环境变量或其它调用者可以启用它。
- `_validated_consumer_hit()` 接收 `secure_runtime`，但函数体不读取该值。
- `_prepare_under_family_lock()` 的 `root` 参数已无使用者；它原先只服务已经删除的 v1 松散缓存迁移。
- consumer 仍通过 `secure=True` 读取 current generation，并无条件验证 generation acceptance lineage；底层安全读取能力仍有真实用途。
- parent generation 的 lineage 回读仍显式使用 `secure=True`，因此底层函数的 `secure` 参数不是死代码。

## 设计决策

### 唯一数据流

```text
publisher
→ family 独占锁
→ 普通方式读取 current
→ 构建并校验 immutable generation
→ 原子切换 current

consumer
→ 安全方式读取 current
→ 校验输入等价、日期覆盖和 acceptance lineage
→ 只读返回 current cache
```

不再保留一个永远为 false 的运行模式参数来表达这两条固定路径。

### 最小代码边界

- 从 `_prepare_under_family_lock()` 删除 `secure_runtime` 和未使用的 `root` 参数。
- 从 `_validated_consumer_hit()` 删除未使用的 `secure_runtime` 参数。
- 将 current generation 的读取规则直接表达为 `secure=not is_publisher`。
- 删除 publisher 截断输入路径中永远不可达的 `if secure_runtime` lineage 分支。
- publisher 的 create、prune、current switch 和未发布 candidate 清理继续使用当前普通文件路径，不提升为 secure 模式。
- 保留 `_load_current_generation()`、`_load_generation_directory()` 等底层函数的 `secure` 参数及全部文件、目录、owner、mode、symlink 和 lineage 校验。

## 不做事项

- 不改变 publisher 或 consumer 身份规则。
- 不改变 manifest v3、current pointer、generation acceptance 或 cache pickle 格式。
- 不修改 `NON_PRODUCTION`、`native_generation=null`、`native_generation_changed=false` 兼容字段。
- 不修改缓存 hash、content identity、spec fingerprint、CompareGate 证据或 retention 策略。
- 不读取、重写、迁移或删除现有生产缓存文件。
- 不新增配置、环境变量、fallback、兼容分支或安全抽象。
- 不修改 Native 算法、Harness、数据库、调度、plist、Backend 或前端。

## 错误语义

错误继续直接暴露：

- consumer 无 current、current 非法、输入不等价、覆盖不足或 lineage 校验失败时，继续返回 `CACHE_PUBLISHER_REQUIRED`。
- publisher 构建、容量校验、generation 发布或清理失败时，继续按现有异常路径退出。
- 本次不增加重试、旧缓存回退或隐式重建路径。

## 验收设计

聚焦验证必须证明：

- publisher cold build、current hit、增量构建和 private build 行为不变。
- consumer 合法 current 可以命中；无 current、输入不一致、覆盖不足或 lineage 异常继续失败。
- `secure_runtime` 在当前代码和测试中归零。
- `_prepare_under_family_lock()` 不再接收无用 `root`。
- consumer 安全读取和 parent lineage 的 `secure=True` 调用仍然存在。
- Phase-A 聚焦测试、架构测试和全量回归全部通过。

## 文档生命周期

本文只服务尚未实施的设计确认。实施完成、当前状态文档更新并通过验证后删除本文；历史决策通过 Git 提交追溯，不把已完成设计保留为当前操作入口。
