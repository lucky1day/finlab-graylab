# 二次输入 Generation 控制面退役设计

## 1. 决策

退役旧 daily ledger 遗留的 Native/二次 DataBridge sealed generation
控制面。生产输入只保留两条真实路径：

```text
Native
当前权威 bond_db + predict_date 推导的 feature_date 截止
→ shared.input_artifacts
→ Native 方案

Blackbox
DataBridge current 三频业务文件
+ 权威数据库中的日历/方案声明平台注册输入
→ 每次运行的组合 snapshot/runtime view
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

- 普通 Native 与列入 `SOURCE_RUNTIME_SCHEME_IDS` 的 source-backed Native
  最终连接同一个 MySQL `server_uuid`、同一个 `bond_db`；两者都在输入构建时
  使用 `feature_date/as_of_date` 截断；
- 普通 Native 使用平台进程的数据库连接；source-backed Native 的原始加密
  runner 使用同一 `bond_db` 的独立 `SELECT`-only 身份。后者是权限隔离，
  不是第二份数据库或第二个数据权威；
- Blackbox 打开并校验 DataBridge current 三频文件，同时从权威数据库捕获
  日历和方案声明的平台注册输入，再生成单次运行组合快照；
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
+ 当前权威 bond_db
+ predict_date 推导的 feature_date 截止
→ 运行方案
```

自然调度继续使用现有持久 input artifact 路径；单日补缺继续使用已经实现的
私有临时 workspace。两者只在 artifact 存放位置不同，不再存在
`generation_v1` 或 `live_source_0629` 的输入执行模式差异。

所有 Native 共用同一个当前业务数据权威：

- 普通 Native 使用平台进程的 `bond_db` 连接；
- `SOURCE_RUNTIME_SCHEME_IDS` 中 9 个 source-backed 方案——daily 0629
  三个、monthly 0629 三个及 weekly average 0529 三个——继续通过专用
  `SELECT`-only 身份读取同一个 `bond_db`。

`SOURCE_RUNTIME_SCHEME_IDS` 只决定是否向原始 runner 注入受限凭据，不再被描述
为数据库选择或数据口径选择。该账号只允许读取算法所需源表，避免把平台进程可用
的业务写权限交给归档原始代码；它不产生数据副本、同步任务或另一套数据 vintage。

这里的“账号”是 MySQL 认证身份，不是另一个数据库。一个 `bond_db` 可以同时给
不同进程授予不同权限：平台进程需要通过 Repository 写入 run、prediction 和
Registry；原始 runner 只负责计算，只需要源表 `SELECT`。两者使用不同账号的目的
是让原始代码即使发生缺陷也不能修改业务表、Schema 或授权。该权限边界不改变
输入数据，且本模块不新增账号或权限配置。

这 9 个方案继续验证正式 source evidence、source package hash、模型身份和输出
日期。它们内部直接 SQL 是存量加密 runner 的冻结兼容例外，不是新增 Native
可以复用的模式。本模块只删除依赖 Native generation 的 compatibility fence，
不修改原始 runner 的输入接口，也不把 9 个方案迁移为 Blackbox。

### 3.2 Blackbox

Blackbox 保留：

- DataBridge current 的原子发布与严格只读校验；
- current state 中的 `generation_id`、refresh date、business digest 和文件摘要；
- 权威数据库日历与方案声明平台注册输入的捕获；
- 三频文件与平台输入合成的单次运行 snapshot/runtime view；
- gray replay 对 DataBridge authority 的严格绑定；
- Blackbox 子进程的 opaque schedule execution token。

不再构造 `DataBridgeGenerationContext`，也不要求它关联一份 Native
generation。DataBridge current 是三频业务文件的唯一权威，不是 Blackbox
全部输入的唯一权威；数据库日历和平台注册输入仍是独立权威，最终组合快照才是
本次 Blackbox 运行的完整输入身份。

## 4. 删除边界

删除当前运行代码中的：

- `shared/native_input_generation.py`；
- `shared/databridge_input_generation.py`；
- Executor 的 `native_generation`、`databridge_generation`、
  `calendar_generation`、`live_source_compatibility` 与相关环境变量分支；
- `shared.input_artifacts` 的 frozen Native engine、frozen frame builder、
  generation provenance 和 generation-scoped artifact 路径；
- `FrozenCalendarService` 与仅从 Native generation 捕获 Blackbox platform input
  的分支；保留从数据库连接捕获日历/平台注册输入的当前生产路径；
- 0629 adapter 的 generation compatibility 环境解析与额外 provenance；
- `shared/live_source_contract.py` 及其只服务旧 compatibility fence 的引用；
- Repository 中无调用者的 generation registration/readback 数据类、常量和
  SQL；
- 只验证上述退役路径的专项测试。

Signal Gap Plan 升级为 `active-signal-gap-plan-v7`：

- Native action 删除 `input_mode`，以 `runtime_type=native_adapter` 与
  `input_authority=null` 表达当前数据源重建；
- Blackbox action 继续以 `runtime_type=blackbox_v2` 和严格非空的
  DataBridge `input_authority` 表达 replay；
- v6 及更早计划只保留为历史 JSON，不允许执行；
- 删除 `source_package_sha256` 作为 gap plan 特殊控制面判别字段。

source-backed adapter 仍在算法启动时通过既有
`daily_0629_source_evidence`、`monthly_source_evidence` 和
`weekly_average_source_evidence` 校验 source package，并在结果中保留证据。
这已经满足提交前失败，不需要 gap plan 再维护第二套身份矩阵。

## 5. 保留边界

保留以下能力，不在本模块重构：

- `shared.data_bridge.refresh` 和 DataBridge current `generation_id`；
- Blackbox snapshot、runtime view、DataBridge authority、
  `source_generation_id` 与 gray replay；
- Blackbox `BOND_SCHEDULE_EXECUTION_TOKEN`；
- `shared.data_service` 的日期截止和三频构建逻辑；
- Native input artifact 的原子文件写入、读回校验和内容摘要；
- Signal Gap Fill 的私有临时 Native workspace；
- Liwei Phase-A publisher-first 排序、cache identity、cold/cached compare 和
  `private_build`；
- source runner 的 `bond_db` 最小权限凭据隔离与 source package hash 验证；
- 017 migration、`t_input_generations` 实体表及其 6 条历史行。

已核对当前 7 个 pointer 与 21 个 Phase-A manifest：所有
`native_generation` 均为 `null`、`native_generation_changed` 均为 `false`，
且没有现用 generation rebind。Phase-A 在本次遵循最小兼容边界：

- 删除 generation 参数、环境绑定和 `rebind` 分支；
- 保持现有 v3 manifest 结构与内容身份语义，不重建 pointer 或缓存；
- `native_generation` 字段继续存在且只接受 `null`，非空直接失败；
- `native_generation_changed` 字段继续存在且只接受 `false`；
- publisher-first、cache identity、cold/cached compare 与 `private_build`
  保持不变。

现有 manifest 的 `status=NON_PRODUCTION` 与自然生产消费语义是否匹配，是下一
独立模块“Phase-A acceptance/production semantics”的评审对象，不在本次顺带
修正。

## 6. 数据库与磁盘边界

Repository 不再把 `t_input_generations` 当作当前运行时控制面，但 017
migration 不改写，表和历史行不做 DDL。后续是否物理归档或删除该表，必须在
独立数据库清理模块中核对 migration history、外键和恢复边界。

现有 `backtest_artifacts/input_generations` 约 513MB 历史文件不参与当前
运行。本次代码退役不顺带删除它们；物理文件删除作为独立、可明确列出目标的
运维动作执行，不与代码提交混合。

## 7. 错误语义

不新增 fallback、自动重试或备用输入路径：

- Native 当前数据源缺少截止日前必要数据时直接失败；
- Blackbox DataBridge current 不新鲜、摘要不一致或缺失时直接阻断；
- Blackbox 权威日历或方案声明平台注册输入缺失/非法时直接阻断；
- source-backed Native 的 source evidence/package hash 不匹配时直接失败；
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
  和 strict-current 测试，并保留数据库日历/平台注册输入进入组合快照的测试；
- 将目前混在 `test_databridge_input_generation.py` 中的 current DataBridge
  契约测试迁入按真实职责命名的测试文件；
- 删除 Native/二次 DataBridge generation 的创建、封存、retention、GC、
  tamper、环境绑定和 DB fence 测试；
- 保留并增强 launchd Runner、Native 当前数据库输入、9 个 source-backed
  方案的同库只读权限隔离、Phase-A cache、Signal Gap Fill 和 Blackbox
  current 回归；
- Signal Gap Plan 测试证明 v7 可执行、v6 及更早版本 fail-closed，且 Native
  action 不再携带 `input_mode` 或特殊 source-package 判别字段；
- Phase-A 测试证明现有空绑定 manifest 仍可读取，非空
  `native_generation`、`native_generation_changed=true` 和 rebind 请求均失败；
- 静态检查只针对已退役符号，确保当前生产代码不再引用：
  `shared.native_input_generation`、`shared.databridge_input_generation`、
  `BOND_NATIVE_INPUT_MODE`、`BOND_NATIVE_GENERATION_*`、
  `BOND_NATIVE_LIVE_SOURCE_*`、`live_source_compatibility`、
  `NativeGenerationContext` 和 `DataBridgeGenerationContext`。

静态检查不得把通用 `generation_id`、`execution_token` 或
`t_input_generations` 作为零引用目标：前两者仍有 Blackbox 当前生产职责，
后者仍保留 migration、实体表和历史记录。若 Repository 的 bootstrap 空表清单
仍需包含该表，也不构成运行时 generation 控制面。

## 9. 非目标

本模块不做以下工作：

- 不修改任何 Native 算法逻辑、模型参数或 exact version hash；
- 不修改 DataBridge current 文件格式或发布时序；
- 不增加数据库一致性快照服务；
- 不做 DDL，不删除历史数据库行；
- 不删除磁盘历史 artifact；
- 不修改 plist、launchd、Backend 或前端；
- 不改变数据库账号、授权或连接配置；
- 不把 9 个 source-backed Native 转换为 Blackbox，也不新增 Native→Blackbox
  runtime 转换控制面；若上游未来提供标准输入交付，另行决定新旧 scheme ID、
  历史连续性和生产切换；
- 不升级 Phase-A manifest schema、重建缓存或处理其
  `NON_PRODUCTION` acceptance 语义；
- 不处理顶层 `inference.py` 是否纳入 exact version hash。

## 10. 验收条件

完成实现后必须证明：

1. active Native 自然调度继续读取同一个当前权威 `bond_db` 并按 feature
   cutoff 运行；9 个 source-backed Native 保持专用 `SELECT`-only 身份，
   不出现第二数据库或第二数据口径；
2. Native 单日补缺继续在私有临时 workspace 中运行，写入语义不变；
3. Blackbox 自然调度继续严格使用 DataBridge current 三频文件和数据库
   日历/平台注册输入，结果仍携带 current generation/组合 snapshot
   provenance；
4. source-backed Native 和 Liwei Phase-A 现有生产路径无行为回归；
5. `shared/native_input_generation.py`、
   `shared/databridge_input_generation.py` 以及相关运行时 API 已删除；
6. 当前生产代码不再引用本设计列出的退役符号；Blackbox current
   `generation_id`、source authority 和 schedule execution token 保持有效；
7. `t_input_generations` 表和历史数据未被修改；
8. Signal Gap Plan v7 成为唯一可执行版本，Native 不再有 mode/source-package
   控制面，Blackbox authority 仍严格；
9. Phase-A 现有 v3 空绑定缓存无需迁移，非空 generation/rebind fail-closed；
10. 聚焦测试、架构测试与全量回归全部通过；
11. current 文档只描述真实运行路径，不再承诺不存在的 generation fence。
