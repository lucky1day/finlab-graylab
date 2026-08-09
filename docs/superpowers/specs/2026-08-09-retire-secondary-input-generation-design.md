# 二次输入 Generation 控制面退役设计

## 1. 决策

退役旧 daily ledger 遗留的 Native/二次 DataBridge sealed generation
控制面。生产输入只保留两条真实路径：

```text
Native
当前数据库 + predict_date 推导的 feature_date 截止
→ shared.input_artifacts
→ Native 方案

Blackbox
DataBridge current 原子发布目录 + current state generation_id
→ 每次运行的私有 snapshot/runtime view
→ Blackbox 方案
```

本设计不删除 Blackbox 当前使用的 DataBridge current `generation_id`。
需要退役的是在 current 之上再次构造
`NativeGenerationContext/DataBridgeGenerationContext`、再登记
`t_input_generations` 的第二层封装。

## 2. 事实基础

当前 installed launchd 的 daily/weekly/monthly 任务均调用通用
`scheduler.launchd_prediction_runner`，没有设置 Native generation 环境变量。
通用 Runner 调用 `execute_scheme()` 时只传递
`scheduled_live + launchd_one_shot`，Executor 随后调用
`run_configured_scheme()`，没有传入 `native_generation`、
`databridge_generation` 或 `calendar_generation`。

因此当前生产行为是：

- Native 通过 `create_input_engine()` 打开当前数据库，并在输入构建时使用
  `feature_date/as_of_date` 截断；
- Blackbox 打开并校验 DataBridge current，再复制为单次运行私有快照；
- 26 个 active Native 的最新生产结果没有 Native generation provenance；
- `t_input_generations` 当前仅有 6 条历史 `native_source/SEALED` 行，
  没有 `databridge_v1` 行，也没有 `t_schedule_items` 引用；
- `create_native_generation()`、`create_databridge_generation()` 以及
  Repository 的 generation 登记/读取 API 没有当前生产调用者。

现有二次 generation 代码并未保护自然调度，继续保留只会形成错误的
安全承诺和第二条潜在输入路径。

## 3. 目标状态

### 3.1 Native

Native 自然调度与单日补缺复用同一输入语义：

```text
当前 active exact version
+ 当前数据库
+ predict_date 推导的 feature_date 截止
→ 运行方案
```

自然调度继续使用现有持久 input artifact 路径；单日补缺继续使用已经实现的
私有临时 workspace。两者只在 artifact 存放位置不同，不再存在
`generation_v1` 或 `live_source_0629` 的输入执行模式差异。

0629 source-backed 方案继续验证正式 source evidence、source package hash、
模型身份和输出日期；只删除依赖 Native generation 的 compatibility fence。

### 3.2 Blackbox

Blackbox 保留：

- DataBridge current 的原子发布与严格只读校验；
- current state 中的 `generation_id`、refresh date、business digest 和文件摘要；
- 单次运行私有 snapshot/runtime view；
- gray replay 对 DataBridge authority 的严格绑定；
- 日历与平台注册输入从权威数据库读取并进入组合输入身份。

不再构造 `DataBridgeGenerationContext`，也不要求它关联一份 Native
generation。DataBridge current 本身就是 Blackbox 的唯一数据权威。

## 4. 删除边界

删除当前运行代码中的：

- `shared/native_input_generation.py`；
- `shared/databridge_input_generation.py`；
- Executor 的 `native_generation`、`databridge_generation`、
  `calendar_generation`、`live_source_compatibility` 与相关环境变量分支；
- `shared.input_artifacts` 的 frozen Native engine、frozen frame builder、
  generation provenance 和 generation-scoped artifact 路径；
- `FrozenCalendarService` 与从 Native generation 捕获 Blackbox platform input
  的分支；
- 0629 adapter 的 generation compatibility 环境解析与额外 provenance；
- Repository 中无调用者的 generation registration/readback 数据类、常量和
  SQL；
- 只验证上述退役路径的专项测试。

Signal Gap Plan 中所有 Native action 使用同一个当前数据库输入语义。
`source_package_sha256` 仍属于 0629 算法身份，不再决定一种独立执行模式。

## 5. 保留边界

保留以下能力，不在本模块重构：

- `shared.data_bridge.refresh` 和 DataBridge current `generation_id`；
- Blackbox snapshot、runtime view、DataBridge authority 与 gray replay；
- `shared.data_service` 的日期截止和三频构建逻辑；
- Native input artifact 的原子文件写入、读回校验和内容摘要；
- Signal Gap Fill 的私有临时 Native workspace；
- Liwei Phase-A publisher-first 排序、cache identity、cold/cached compare 和
  `private_build`；
- 0629 source database isolation 与 source package hash 验证；
- 017 migration、`t_input_generations` 实体表及其 6 条历史行。

Phase-A cache 的现有持久 manifest 结构不在本次升级。其
`native_generation` 字段在当前生产中恒为 `null`；本次只移除动态绑定能力，
暂时保留这个 `null` 字段，避免无业务收益的 cache identity 换代。Phase-A
cache 自身将在独立模块中评审。

## 6. 数据库与磁盘边界

Repository 不再把 `t_input_generations` 当作当前运行时控制面，但 017
migration 不改写，表和历史行不做 DDL。后续是否物理归档或删除该表，必须在
独立数据库清理模块中核对 migration history、外键和恢复边界。

现有 `backtest_artifacts/input_generations` 约 513MB 历史文件不参与当前
运行。本次代码退役不顺带删除它们；物理文件删除作为独立、可明确列出目标的
运维动作执行，不与代码提交混合。

## 7. 错误语义

不新增 fallback、自动重试或备用输入路径：

- Native 当前数据库缺少截止日前必要数据时直接失败；
- Blackbox DataBridge current 不新鲜、摘要不一致或缺失时直接阻断；
- 0629 source evidence/package hash 不匹配时直接失败；
- Phase-A publisher/cache 不满足现有契约时直接失败；
- 不回退到旧 generation、旧 snapshot 或旧 exact version。

Native 当前数据库构建包含多次查询，理论上存在查询之间源表变化的可能性。
旧 generation 在当前生产中没有启用，因此删除它不会降低现有保障。本次不为
该低概率场景新增长事务或快照服务；只有出现可复现的数据漂移证据时，才另行
评审最小一致性事务边界。

## 8. 文档与测试迁移

当前文档中“scheduled Native 使用 `generation_v1`”“scheduled Blackbox
必须绑定二次 `SEALED generation`”均与生产事实不符，必须改为本设计的两条
唯一数据流。

测试迁移遵循以下规则：

- 保留 DataBridge current、三项月频追加列、cutoff、snapshot、runtime view
  和 strict-current 测试；
- 将目前混在 `test_databridge_input_generation.py` 中的 current DataBridge
  契约测试迁入按真实职责命名的测试文件；
- 删除 Native/二次 DataBridge generation 的创建、封存、retention、GC、
  tamper、环境绑定和 DB fence 测试；
- 保留并增强 launchd Runner、Native 当前数据库输入、0629 source isolation、
  Phase-A cache、Signal Gap Fill 和 Blackbox current 回归；
- 静态检查确保生产代码不再引用退役模块或环境变量。

## 9. 非目标

本模块不做以下工作：

- 不修改任何 Native 算法逻辑、模型参数或 exact version hash；
- 不修改 DataBridge current 文件格式或发布时序；
- 不增加数据库一致性快照服务；
- 不做 DDL，不删除历史数据库行；
- 不删除磁盘历史 artifact；
- 不修改 plist、launchd、Backend 或前端；
- 不重构 Phase-A cache；
- 不处理顶层 `inference.py` 是否纳入 exact version hash。

## 10. 验收条件

完成实现后必须证明：

1. active Native 自然调度继续直接读取当前数据库并按 feature cutoff 运行；
2. Native 单日补缺继续在私有临时 workspace 中运行，写入语义不变；
3. Blackbox 自然调度继续严格使用 DataBridge current，结果仍携带 current
   generation/snapshot provenance；
4. 0629 source-backed 和 Liwei Phase-A 现有生产路径无行为回归；
5. `shared/native_input_generation.py`、
   `shared/databridge_input_generation.py` 以及相关运行时 API 已删除；
6. 当前生产代码不再引用 `BOND_NATIVE_INPUT_MODE`、
   `native_generation_v1`、`live_source_compatibility` 或二次 DataBridge
   generation；
7. `t_input_generations` 表和历史数据未被修改；
8. 聚焦测试、架构测试与全量回归全部通过；
9. current 文档只描述真实运行路径，不再承诺不存在的 generation fence。
