# Phase-A 旧格式兼容层退役设计

## 目标

Phase-A cache 运行时只保留当前输入契约：所有调用者必须提供有效的
`AuxiliaryDependencyProjection`，输入状态只生成和接受 v3，generation
acceptance 只接受当前完整的 input-change 字段集合。

本次只删除已经不可达的旧格式兼容代码，不改变当前磁盘格式。现有
Phase-A cache 不得改写、迁移或重建。

## 现状证据

- 生产代码中 10 个 `prepare_phase_a_caches()` 调用者均显式传入
  `auxiliary_dependency_projection`。
- 当前磁盘上 7 个 current generation 与 14 个保留父代 generation，共
  21 份 manifest，input-state 全部为 v3。
- 21 份 manifest 的 input-change 全部使用当前完整字段集合。
- 7 个 current pointer 均指向现存 v3 generation。
- v2 input-state、旧 input-change 字段集合和无投影 build decision 只剩在
  兼容分支与旧式测试构造中，不再承载生产调用或当前 cache lineage。

## 唯一运行契约

清理后的数据流为：

```text
Phase-A 方案
→ 构建 AuxiliaryDependencyProjection
→ input-state v3
→ 当前完整 input-change
→ manifest v3
```

`prepare_phase_a_caches()` 的 `auxiliary_dependency_projection` 改为必填的
`AuxiliaryDependencyProjection`。缺少参数时由 Python 立即抛出 `TypeError`；
类型非法时在计算 input-state 前失败。平台不再生成无投影的 input-state
v2，也不再为旧格式选择另一套 build decision。

## 删除范围

删除以下旧格式语义：

- `LEGACY_INPUT_GENERATION_STATE_SCHEMA_VERSION`；
- input-state v2 的构建、验证和 consumer 等价比较分支；
- `_LEGACY_INPUT_CHANGE_FIELDS` 及旧字段集合双读；
- `_legacy_build_decision()`；
- `projection_status="absent"` 对应的生产决策路径；
- 只用于证明上述旧格式仍可运行的测试。

`_input_generation_state()` 只生成 v3；
`_validate_input_generation_state_record()` 只接受 v3；
`consumer_input_state_equivalence_sha256()` 只处理已完整验证的 v3 输入。

## 明确保留

为保证现存 cache 原样可读，本次保持以下磁盘契约和运行行为不变：

- generation manifest schema v3；
- input-state schema v3；
- `NON_PRODUCTION`；
- `native_generation=null`；
- `native_generation_changed=false`；
- generation、manifest、input-state、acceptance 和 baseline 的当前摘要；
- parent lineage、publisher/consumer、cold/cached compare 与 `private_build`；
- current pointer、retention 和容量边界；
- 10 个方案的算法文件及其辅助投影构造逻辑。

本次不删除或改写 `backtest_artifacts/` 中的任何文件。

## 失败语义

- 缺少 `auxiliary_dependency_projection`：入口立即 `TypeError`。
- 非 `AuxiliaryDependencyProjection`：输入状态计算前立即失败。
- input-state v2：报 `cache input generation state schema mismatch`。
- 旧字段集合的 acceptance record：报字段不匹配。
- 当前 v3 manifest、parent lineage 和 consumer hit 的验证顺序不变。
- current pointer、manifest 或 lineage 损坏时沿用现有失败语义。

不增加旧格式自动升级、双读、旧版本回退、缓存迁移、缓存重建或自动修复。
`private_build` 仍只在调用方提供的私有目录中工作，不读取或修改生产 cache。

## 测试设计

### 契约测试

- 未传 `auxiliary_dependency_projection` 必须 `TypeError`。
- input-state v2 必须拒绝。
- 旧 input-change 字段集合必须拒绝。
- 当前 v3 input-state 和完整 input-change 必须通过。
- 相同 v3 输入的 consumer 等价摘要保持稳定。

### 行为回归

- publisher cold build 正常；
- consumer 只读取 publisher 已发布的 cache；
- `private_build` 在私有目录正常构建；
- cached/cold 完整输出比较保持不变；
- 非法 current pointer、manifest 或 lineage 继续直接失败。

不新增缓存迁移或自动重建测试，因为这些行为不属于当前系统。

### 现场只读验证

只读取现存 JSON 元数据，验证 21 份 manifest 和 7 个 current pointer 仍符合
当前 v3 契约。不加载、反序列化、改写或重新发布生产 baseline cache。

### 静态验收

```bash
rg -n \
  "LEGACY_INPUT_GENERATION_STATE_SCHEMA_VERSION|_LEGACY_INPUT_CHANGE_FIELDS|_legacy_build_decision" \
  shared tests
```

预期零结果。同时使用静态调用检查确认 10 个方案调用者均显式传入
`auxiliary_dependency_projection`。

## 文档边界

当前权威文档只需明确：

```text
Phase-A 输入状态只接受 v3
所有调用必须提供有效辅助投影
当前磁盘 cache 无需迁移或重建
```

不新增迁移指南、兼容操作或恢复流程。设计和实施计划在模块完成后删除，
不作为长期权威文档。

## 完成标准

- 聚焦 Phase-A 测试通过；
- 架构与文档测试通过；
- 全量测试零失败；
- 旧格式静态标识在 `shared/` 与当前测试中为零；
- 21 份现存 manifest 和 7 个 current pointer 通过只读 v3 核验；
- `git diff --check` 通过；
- diff 不包含 cache 产物、方案算法改动或无关重构。

## 非目标

- 不升级 manifest 或 input-state schema；
- 不删除 `NON_PRODUCTION` 或空 generation 字段；
- 不修改算法、输入数据、缓存内容或生产调度；
- 不执行缓存迁移、重建、发布或清理；
- 不增加 fallback、双读或兼容转换；
- 不修改 plist、launchd、Backend、数据库或前端。
