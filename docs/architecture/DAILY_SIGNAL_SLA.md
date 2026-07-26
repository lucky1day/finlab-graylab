# 日频信号 08:00 SLA 架构

**文档状态**：`CURRENT`

**目标读者**：平台开发、运维、架构评审和风险控制人员

**最后核验日期**：2026-07-26

本文定义单台 Mac Studio 上日频预测的目标架构、不可破坏的不变量和上线门禁。
它不证明当前生产已达到 08:00 SLA；动态结论以
[当前状态](../CURRENT_STATUS.md)为准。

## 1. 结论与边界

- APScheduler 只提供时间触发，不承担长任务排队。
- 一个交易日只有一个 `daily-signals + predict_date` occurrence。
- occurrence 创建时冻结 active daily Registry、代码/config 摘要、输入 generation
  和 target 验收全集；当前候选 policy 基线是 21 个 execution、25 个 target。
- 14 个 `generation_v1` Native 与 Blackbox V2 只读取当天已封存的不可变
  generation，不读取旧 `current`。三个固定 0629 Native 在 MVP 阶段使用受控
  `live_source_0629` 兼容桥；该例外不允许扩展，也不是 generation 失败回退。
- 单机 owner 使用 `launchd + flock`；完成权使用数据库
  `current_run_id + attempt_no` fence。不实现 lease、heartbeat claim、
  `SKIP LOCKED` 或通用分布式 worker。
- rollout 在迁移、输入适配、容量、故障注入和连续运行门禁全部通过前必须保持
  incumbent/legacy control path；真实配置值只记录在[当前状态](../CURRENT_STATUS.md)。
  新旧路径不得并行写库。
- mode 是跨进程控制面：backend、scheduler、健康巡检和写库 fence 必须读取同一
  部署状态。仓库 launchd 默认全部为 `legacy`，并保留 legacy V2 preflight
  作为该模式唯一的每日 DataBridge refresh owner；版本化 rollout bootstrap
  文件也保持 `mode=legacy`，只兼容缺环境变量的旧安装，不能单独承担 ledger
  切换。切换 ledger 时先把已安装的 preflight plist 改成 `ledger`，再 bootout
  当前 job，防止登录/重启从
  `~/Library/LaunchAgents` 复活 legacy owner；之后同步切换并重启
  backend/scheduler。仓库模板保持 `legacy`，不能覆盖 ledger 已安装副本。

未来如果需要多机、抢占式执行或复杂 DAG，应迁移到成熟工作流系统，不在本账本上
继续叠加分布式调度能力。

## 2. 日批时序

| 时间（Asia/Shanghai） | 不可变语义 |
|---|---|
| 06:30 | hard `not_before`；冻结 occurrence 账本并检查 T-1 日历、目标锚点和三频最小历史 |
| Native readiness | 数据齐备后立即构建 Native generation 并启动当天全新 DataBridge 更新；未齐备时不创建算法 attempt，由同日 recovery tick 重试 |
| 06:55 | DataBridge readiness 审计 guardrail；尚未 SEALED/绑定时投影为 `LATE` 并告警，但刷新继续使用当天新 generation，直至 08:30 recovery cutoff |
| DataBridge `sealed_at` | 四个 V2 分别在 `+0/+2/+4/+6` 分钟释放，互不依赖 |
| 07:00 | 进度 watchdog；只评估、恢复和告警，不重启 scheduler |
| 07:45 | V2 start guardrail；未启动者永久标记 item `sla_status=LATE`，告警后仍继续 |
| 07:55 | 25 个 target 的内部就绪目标 |
| 08:00 | hard SLA；少一个 target，occurrence 的 write-once `sla_outcome=BREACHED` |
| 08:30 | 禁止新 attempt 和自动重试；已运行 attempt 可在既定 timeout 内提交 late 结果 |

不跨日自动补跑，不使用昨日 generation，不把 08:00 后补齐回写为 `MET`。
06:55/07:45/08:00/08:30 的 Cron 是准时控制触发，不是唯一正确性来源；同日 startup、
两分钟 recovery tick、operator recovery 和 08:30 watchdog 都会幂等补写仍为
`PENDING` 的到期 guardrail/SLA。06:55 readiness 由冻结 policy 时点与可信
DataBridge DB `sealed_at` 确定，因此晚到 generation 在后续 catch-up 中仍为
`LATE`；该投影只降级健康状态和告警，不中止刷新。重复入口先补 visibility
receipt，再依靠数据库 write-once fence 求值；已经
ON_TIME/LATE/MET/BREACHED 的边界不重复告警。

## 3. 输入 generation

`t_input_generations` 同时管理 `native_source` 和 `databridge_v1`：

- `BUILDING -> SEALED` 是正常单向发布；内容或证据失效后只能转
  `INVALIDATED`。
- Native 导出必须在 MySQL `REPEATABLE READ` 的一致性快照只读事务中完成，
  source evidence 也必须在该事务内采集。06:30 是最早启动点，不是永久
  source seal；readiness 到位后可在 08:30 recovery cutoff 前开启快照。1Y、
  3Y、5Y、7Y、10Y 五个曲线锚点必须在冻结快照内再次通过同一 gate，再经
  `shared.input_artifacts` 产生内容寻址 manifest。
- DataBridge 必须在 06:30 后发起全新全量刷新，经稳定轮次和原子发布形成当天
  generation，并绑定用于日历的同日 Native generation。
- Native/DataBridge 都在 `.building-*` staging 中写完 payload 和最终
  manifest，逐文件重开、rehash、设为只读并 `fsync` 后，只执行一次 generation
  目录 rename。rename 后再 `fsync` 父目录；父目录同步失败必须向调用方报错，
  但完整 final 可由重启恢复，不能重复 live export/full refresh。
- DB `sealed_at` 由 register/seal/bind 的同一事务使用数据库时钟写入；V2 的
  `sealed_at + offset` 只从该提交后的可信时点起算，不相信文件 manifest 的
  本机墙钟作为调度权威。
- 每次打开 generation 都重新计算实际 SHA-256；manifest、文件、schema、
  business/feature date 或 Native 关联任一漂移即 fail-closed。
- generation 文件在落盘、manifest 和 current pointer 切换时使用
  temp file、逐文件/目录 `fsync` 和原子 replace。两类 generation storage
  root、DataBridge `data_root/runtime_root` 必须由服务 UID 拥有且为 `0700`
  （或更严）；新根按 `0700` 创建，既有非私有根必须在停服迁移中显式修复，
  runtime 不会静默 `chmod`。
- 旧式“调用方传入 bound/protected ID”清理入口永久禁用。协调器只能删除 DB
  resolver 返回的 `INVALIDATED`、无任何 schedule item 引用、且不是任一
  DataBridge generation 父代的精确 payload；删除前再次 rehash manifest 与
  全部文件。`SEALED` 以及任何历史/当前绑定 generation 永不由自动清理删除。
  `.building-*`、`.current-next-*` 和 GC tombstone 只允许 occurrence owner
  在私有根与跨进程锁内按闭合命名规则清理。
- generation 构建前按冻结 policy 对每类执行 64 代数量上限、50 GiB 总占用
  上限与 2 GiB 磁盘 free-space 低水位 preflight；失败时保持旧 generation、
  拒绝新构建并结构化告警。长期归档、内容去重和
  保留周期尚未形成可验证策略，仍是 ledger 切换门禁，不能用自动删除代替。
- 06:30 后的源表写入保留为诊断证据，但不阻断尚未冻结的晨间 generation；
  generation 一旦冻结，后续修正不重启 occurrence，也不改变其输入。

当前 factor 表只有 `create_time`、没有可靠的行级 `update_time` 或上游 seal
token。平台能够识别 cutoff 域内的晚插入，并由冻结快照隔离之后发生的修改；
但仅覆盖原值且不更新 `create_time` 的原位 UPSERT 无法单靠现有表证明。
MVP 不把上游 seal/CDC 作为启动前置；权威边界是 readiness 通过后创建的同一
Repeatable Read generation，不能把 `create_time` 水位误称为完整变更日志。

除五个曲线就绪锚点外，单个 factor 的业务空值可以保留为 NULL，不把“每列
非空”误当作数据 readiness。

MVP 仅允许 `daily_1y_xgb_1y13_0629`、`daily_5y_lgbm_5y10_0629` 和
`daily_10y_lgbm_10y04_0629` 使用 `live_source_0629`。它们仍由同一协调器
绑定当天 Native generation 作为 occurrence、日历和 completion fence，但算法
输入复用已通过灰度的 source runner。policy 冻结 source package SHA-256，子进程
只执行复制后重新验 hash 的私有副本；结果提交前再次比对该 hash。`data_watermark`
取冻结 source runner 实际输出的 `prediction_date`，平台 artifact 只作为运行前
readiness 观察，不宣称是算法消费的内容摘要。任务开始时间沿用 ledger
`started_at`；mode、水位口径、fence generation 任一漂移都在原子提交前拒绝。
兼容桥不使用旧 source cache，也不允许其它 Native 使用；generation 失败不回退。

该桥只解决 MVP 的有序触发与可审计落库，不提供不可变输入隔离。MVP 后必须逐个
替换为公共 generation adapter；若只能通过 L2 算法修改才能适配，则创建独立
Blackbox V2 replacement，不修改 source-backed Native 算法贴合平台。

## 4. 账本和验收权

### 4.1 `t_schedule_occurrences`

唯一键为 `schedule_key + predict_date`。保存 write-once `feature_date`、
policy、Registry digest、动态期望 item/target 数、SLA/cutoff 时间、
completion state，以及 write-once `sla_outcome=PENDING|MET|BREACHED`。
重启只能读取该 `feature_date`，不得再次查询交易日历推导。

### 4.2 `t_schedule_items`

唯一键为 `occurrence_id + base_scheme_id`。冻结 runtime、scheme/code/config
摘要、cache/resource 信息、generation、attempt 和 `current_run_id`。
一个 item 的全部 target 原子发布后才可进入 `SUCCESS`。

### 4.3 `t_schedule_item_targets`

保存 occurrence 创建时的业务 target 快照。唯一键为：

- `occurrence_id + registry_scheme_id`
- `item_id + target_tenor + horizon`

target 只承担业务验收和缺失定位，不参与 worker claim。

`scheduled_live` 必须绑定真实 `schedule_item_id` 和 attempt；manual、
background 或 gray 入口不得伪造 scheduled-live provenance。

当前迁移契约只提供独立外键，没有把
`target occurrence/item/base/runtime -> item -> current winning run ->
canonical prediction` 建成数据库级 composite FK。已发布迁移的内容和校验和
不可修改。018 仅修复 `t_scheme_runs.started_at` 的可空性，使 claim 到真实
子进程启动之间的账本状态能在 MySQL 上成立；该迁移不增加关系约束，也不得与
未来 composite FK 混称。能够安全处理 MySQL partial DDL/implicit commit
恢复的 composite FK 019 仍是 defense-in-depth 的明确 rollout blocker。
切换门禁通过前，由单一 ledger 写库入口与多层
严格关系验收共同兜底：任何 target/item/occurrence 身份漂移、旧 run、挂到其它
item 的 run、非 success 或身份不符的 run、prediction run/方案/标的/期限/
target date/phase 漂移、空 `accepted_at`，以及 `visible_at < accepted_at`，
都按缺失 target 处理；不得计入 accepted/visible/SLA，不得补 visibility receipt，
也不得维持 occurrence `SUCCESS`。该应用层兜底不能替代未来完成可恢复设计后的
composite FK。

## 5. 原子提交

唯一成功提交入口在一个数据库事务内完成：

1. 锁定 run、item 和该 item 的 target rows。
2. 验证 `item.current_run_id == run_id` 和 attempt fence。
3. 重新验证 generation 状态、文件 SHA、scheme/code/config 摘要。
4. 验证算法返回的 target multiset 与冻结 target rows 完全相等。
5. 一次发布全部 canonical predictions。
6. 同事务写 target acceptance、run success 和 item success。

任一检查失败整体回滚。旧 attempt、旧 generation、同日其它 generation 或
部分 target 结果都不能覆盖 winning attempt。

上述事务提交成功后，单独的短事务用数据库时钟写入 target `visible_at` 回执，
作为“提交已对其它连接可见”的保守 DB 证据。进程若在主事务提交后、回执写入前
崩溃，重启通过 run/item/target linkage 补写回执；回执缺失时 target 可以保持
业务 `ACCEPTED`，但 08:00 SLA 不计为可用。SLA 只统计 deadline 前的
`visible_at`，因此不能把提交返回窗口或假造的应用时间算成 `MET`。

DB `visible_at` 不冒充 HTTP/API 可见性。`/api/daily-schedule/visibility` 必须在
fresh transaction 中复核 target、canonical prediction linkage 和 DB receipt，
返回 `Cache-Control: no-store`；只有外部探针实际收到该响应，才能形成 API
观察证据。该观察不反向改写 write-once SLA。

## 6. 调度、资源和恢复

- Native pool 固定最大并发 2，禁止根据 ETA 自动扩到 3。
- V2 pool 固定最大并发 2，单 attempt 硬超时 120 秒。
- Native 顺序按绝对 deadline、cache prerequisite、同优先级 LPT 决定。
- 每个方案声明 resource class、内部 worker 数和 cache group；仅允许版本化
  policy 中经过同机驻留压测的组合。Registry 新增 active daily 方案必须先发布
  新的容量准入 policy。
- governor 的驻留快照同时包含 `native_export`、`databridge_refresh` 和
  `databridge_pack`，算法不能把输入管道占用当成“机器空闲”。当前 policy 中
  的输入/算法共存组合只是 forced-cold 测试候选；在真实同机驻留门禁通过并发布
  可追溯准入证据前，rollout mode 仍必须保持 `legacy`。
- runtime 只消费 ledger execution envelope；不得重新 discovery target、
  读取 DataBridge `current` 或调用 legacy `execute_scheme` 的直接落库路径。
- ledger 模式下 DataBridge 发布能力只在 coordinator 进程作用域内开放；独立
  CLI 只允许显式
  `BOND_DAILY_COORDINATOR_MODE=ledger ... --check-only`，
  `--run-once data-refresh` 也只能 check。未显式声明 mode 的独立命令不是获准的
  生产操作；`--publish` 和 `--dry-run` 都不得作为 ledger 旁路。跨进程独占锁
  覆盖 recover、下载、稳定轮次、publish 与 staging 清理的完整生命周期。

重启恢复复用同一 occurrence 和 generation：成功 item 跳过，遗留 running
attempt 标记 `ABANDONED`，相同 execution token 的孤儿进程组经核验后清理。
ABANDONED 二次启动只有在清理确认、全体首轮已覆盖、attempt 未耗尽且静态剩余
预算可在 08:30 前完成时才允许；否则保持等待或终态。08:30 前只重排 pending、
abandoned 和合格 retry；generation 缺失、失效或摘要不一致时拒绝恢复。

仅 `TRANSIENT_INFRA` 可自动重试一次，并且必须先覆盖全部 item 的首轮 attempt，
且静态预算仍可在 08:30 前结束。data、contract、algorithm、result structure
和 timeout 当日上午不自动重试。

## 7. Cache

每个 `cache_family + tenor` 只有一个 prewarmer。cache 使用不可变 generation，
完整构建和校验后才原子切换 current pointer：

- daily 输入逐 key 对比得到最早变化点；只有 spec 同时声明明确 lookback 和可审计
  dependency proof 时，才从该点向前展开并重算完整 suffix。
- weekly/monthly 到 daily 的安全影响映射目前无法证明，因此任意 revision 或
  append 都退回 full rebuild；不能把“只是新增行”当作安全增量的充分条件。
- 依赖窗口无法证明，或配置/内容身份漂移时，同样退回 full rebuild。
- 构建失败、进程被 kill、校验失败、磁盘余量不足时保留 previous generation。
- generation 数和磁盘占用有硬上限；每次读取重新验证内容摘要。

cache 自身 hash 只证明文件完整性；Phase A callback 只证明缓存中间结果与 cold
中间结果一致，两者都不能单独取得 SLA 容量资格。生产资格还必须有独立的
cold/no-cache callback 和完整输出 evidence，逐项覆盖 direction、`vote_score`、
baseline score/sign、probability/confidence、方案已有内部字段和 Phase A，
并绑定 input content、cache spec 与 cache generation hash。缺任一证据时 ledger
发布前 fail-closed。cache 改造只能属于 L0 I/O 优化。

## 8. 健康与告警

`/api/health` 和只读生产巡检以 occurrence ledger 为唯一日频口径，必须包含：

- scheduler heartbeat 和 occurrence 状态；
- Native/DataBridge generation ID、feature date 和 digest；
- item 的 expected/completed/late/failed；
- target 的 expected/committed/accepted/late/missing、receipt 缺失与 Registry ID；
- 四个 V2 的 release/start/accepted 时间；
- 08:00 `sla_outcome`。

Blackbox V2 不得从健康检查中过滤。DataBridge 已 SEALED 而任一 V2 未产出，
或 08:00 时 target 少于冻结全集，均为 error。

健康读取使用宽容的只读 envelope，以便呈现 generation 尚未绑定、BUILDING、
INVALIDATED、缺失或 source runner 不兼容等具体原因；它不得复用会在这些状态
直接抛错的严格执行 envelope。严格执行入口仍然 fail-closed。`accepted` 只统计
具备完整 prediction linkage 和 DB `visible_at` 回执的 target，事务已提交但回执
缺失的记录单列为 `committed/receipt_missing`，不能掩盖为可用。

08:00 前出现 24/25、07:00 ETA 超线或零进展也不得返回 `overall=ok`；以
`degraded/error` 暴露并保持控制结果，普通 30 秒 heartbeat 不能把它洗掉。
heartbeat 必须携带 `coordinator_mode=ledger`，backend 与 scheduler mode
不一致时返回 `MODE_MISMATCH`。四个 watchdog 在非交易日返回 `IDLE`，不得因
周一至周五 cron 在节假日误报 occurrence missing。

以下事件通过结构化 JSON、macOS Notification Center 和可配置 `AlertSink`
command hook 告警：generation 构建失败、07:00 ETA/进度异常、07:45 V2
未启动、terminal failure、hash/代码漂移、数据晚写、08:00 不完整和 08:30
仍未完成。告警 hook 必须有超时、参数边界和最小环境，失败不能改变业务账本。

## 9. 上线门禁

切换 `BOND_DAILY_COORDINATOR_MODE=ledger` 前必须同时完成：

1. 在生产同 minor、同 `sql_mode/time_zone/foreign_key_checks` 的脱敏 clone
   仅通过 `scripts/apply_migrations.py --apply` 完成 `001→018`、重复执行、中段断连和
   脏数据演练；禁止客户端直跑 017 SQL。迁移器必须在任何 DDL 前
   验证 MySQL `>=8.0.16`、strict/zero-date、UTC session 和 FK 开关。MySQL
   DDL 会 implicit commit，不能把 `engine.begin()` 当文件级回滚；应用 017 后
   必须用 definition fingerprint 复核 column、UNIQUE、FK、CHECK/ENFORCED 和
   ENUM/default 的完整定义；应用 018 后必须复核
   `t_scheme_runs.started_at=DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6)`，
   并完成 DDL 已提交但 history 未标记场景的 clone 演练。apply、inspect 和
   recover 在建立 DB engine 前都必须将工作树 `001..018` 的文件集合和逐文件 SHA-256 与受版本控制的
   `migrations/release_manifest.json` 完整比对；未知、缺失或任一字节漂移都
   fail-closed。若 history 留下 `017=APPLYING`，恢复采用严格的
   inspect-then-recover 协议：`--inspect-applying-017` 会连接 live DB，只执行
   `SELECT` 与 named advisory lock 的获取/释放，不执行 DDL 或 DML；它对连续
   history、固定 017 filename/checksum、MySQL server UUID/database、闭世界
   schema 和数据探针生成 canonical digest，并分类为 `COMPLETE`、
   `COMPATIBLE_PARTIAL` 或 `UNSAFE`。
   recovery 必须同时提供 `--recover-applying-017 --apply --state-digest`，在
   owner lock 内重读并 exact compare；状态漂移或 `UNSAFE` 一律拒绝。
   `COMPLETE` 只原子提交 history mark；`COMPATIBLE_PARTIAL` 才允许幂等重放，
   且定义 drift 只放行 SQL 明确产生的四个 nullable 过渡列。任何失败继续保留
   `APPLYING`，不得人工改 history 或复用旧 digest。018 使用相同的
   `--inspect-applying-018` / `--recover-applying-018 --apply
   --state-digest` 协议，但闭世界状态只有审核过的 legacy source 定义与
   `DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6)` target 定义；任何第三种
   定义均分类为 `UNSAFE`。
2. 三个 0629 Native 的 `live_source_0629` 兼容桥完成隔离回放、唯一触发、
   原子提交和同机容量验证；正式切换前再完成 L0/L1 generation 输入适配，或
   从候选日批移除并走替代方案审批。
3. DataBridge 当天 generation 最迟 06:55 SEALED；06:55 未就绪必须固化
   `LATE`/告警但仍只刷新当天新 generation 到 08:30。四个 V2 最迟 07:10 可见，
   25 个 target 最迟 07:55 可见。
4. 同一 Mac Studio 至少 20 次 forced-cold、20 次真实 T-1 revision/suffix；
   forced-cold 端到端 P95 不高于 80 分钟、最大值不高于 85 分钟。
5. 通过运行中 kill、返回后 kill、事务前/后 kill、timeout、磁盘满、
   DataBridge 延迟、Registry 漂移、generation 篡改和源表晚写注入。
6. 连续 10 个交易日 25/25，无 misfire、timeout、partial、孤儿进程和人工干预。
7. 容量样本必须来自可回溯 ledger/API/文件的受信采集器；collector evidence
   和独立 operator decision 分别使用 macOS detached CMS 签名，runtime trust
   只包含两套不同的 root-owned Keychain 与固定证书 DER SHA-256。decision
   必须有单调 sequence、active id 和短有效期，手工 JSON 和仅自报 SHA-256
   只能用于计算器单测，不能取得生产切换资格。
8. 签名 candidate 必须精确绑定当前 Registry/version 的 21 item/25 target、
   Mac identity/内存/macOS build、三套 conda explicit manifest 与 canonical
   全包清单（包含 pip）、scheduler/scheme 文件集、runtime profile、001..018、
   两类 exporter、MySQL server UUID、完整 ledger schema definition，以及
   candidate v2 control-plane identity（service UID、解析后的 machine-global
   runtime root、machine-global epoch-chain directory、固定 contract/genesis
   identity）。active epoch/digest 不进入 capacity candidate，而是冻结到当日
   occurrence policy 与 heartbeat，避免每次受控切换重做 20+20。ledger
   Native 环境固定为 `forecast_env`，不得由 CLI/admin 覆盖。scheduler 启动、
   coordinator 和 operator recovery 重新采集当前 candidate 并 exact compare；
   首次 occurrence 冻结后、任何输入构建前再复核一次，fingerprint 与 policy
   write-once 冻结。heartbeat 只验签，不在 30 秒路径反复扫描文件与环境。
9. 上线前必须发布可审计的 generation 长期归档/内容去重/保留策略并完成磁盘满
   演练；当前仅有 DB 引用安全的 `INVALIDATED` 精确回收，不足以解除长期容量
   风险。

真实 21/25 候选联跑使用独立的 Engine-bound replay epoch，不读取或修改
machine-global epoch chain。该能力只允许绑定到 `bfl_real_replay_*` 临时
MySQL 8.0.45：必须是 loopback 随机非 3306 端口、独立 UUID、UTC/InnoDB/
strict/REPEATABLE-READ、私有 socket/datadir/secure-file-priv，且关闭
binlog/local-infile。验证器先用短锁预留 Engine，锁外核验真实 DBAPI 连接并
安装 connection guard，再用短锁将 exact pending token 原子升级为 verified
capability；repository 与 executor 的每个 claim/process/commit fence 都传递
同一 Engine 或 Connection 并重验完整身份。occurrence 的 SLA/recovery cutoff
和全部 item deadline 统一冻结为 `opened_at` 之后的下一上海自然日 00:00；
该边界只用于停止新 attempt，不制造 replay SLA。该 occurrence 只证明隔离
功能，不得计入 07:55、20+20、签名 candidate、生产 SLA 或 rollout 证据。

真实 replay 的 `--check-only` 是瞬时、业务数据只读的人工判断，不是持久状态或
执行授权。它使用 machine-global operator/runtime 两把 `flock`，因此会在 BFL
私有 runtime root 创建并保留 `0600` fence 文件；除此之外不修改生产业务表、
服务状态、rollout 或 admission。检查在同一锁会话内首尾两次重开 Native/
DataBridge manifest，并复核候选 Git/policy、完整 active daily
Registry/version、migration history、source-readonly 九表与 T-1 readiness、
live/frozen calendar、start/end watermark、installed service definitions、
rollout-admission identity 边界，以及全日期 ledger/run/孤儿进程静默。输出固定为
`CHECK_PASSED + qualification=EXCLUDED` 或脱敏稳定错误码。

check-only 返回即释放锁，旧 `preflight_digest` 不带签名、TTL 或 capability
语义，未来 execute 不得接受它跨进程复用。execute 必须在同一进程重新完成全部
检查、持续持有两把锁并在每次 dispatch 前重验。增加 execute 前还须钉住独立
production audit-readonly endpoint/server UUID，完成 source/Blackbox 后代
进程和二进制/Conda 环境身份覆盖，并降低两次 source watermark 对生产源库的
扫描负载。

operator/runtime 双锁由一个同进程 session 持有，session 绑定创建 PID、固定锁名、
共同父目录和设备/inode；内部预检只能借用该 session，不得重新获取或释放锁。
成功预检在首尾验锁，PID 漂移、锁释放、路径替换、owner/权限漂移均 fail-closed。
外层 session 退出时固定先释放 runtime、再释放 operator，为后续 execute 的无缝
持锁和逆序资源清理提供唯一入口。replay runtime 必须复用同一 runtime 锁；
wrapper 与核心借用入口都在 DB、快照或线程池副作用之前验证 exact session 类型、
持有 PID、路径和 inode。借用路径只允许验证，不得 acquire/release；成功、
运行异常和线程池收口之后，锁所有权都必须仍属于外层 operator session。runtime
还必须把 exact session 绑定到本次运行态，在主线程 submit 前和 worker 进入
canonical claim 前分别复验；借用模式省略或替换 session 必须在 DB/claim 前拒绝。
锁/session、dispatch identity 和 replay-aware 进程 fence 都是 execute 的前置条件。

成功的二次预检还必须在同一 session 上一次性绑定不可序列化的 dispatch
identity；它只保存固定 manifest 路径和脱敏摘要，覆盖候选 Git/policy、
generation、冻结 item/target 定义、控制面、生产 migration/Registry/version、source
连接身份及预检起止水位。source 水位允许在预检期间前进，但起止两端都必须进入
身份，不能把两个数据状态压成同一个 capability。check-only report/digest 不得
替代该 capability；未绑定、重复绑定或 session 已释放均 fail-closed。runtime
必须在 dispatch lock 内、future submit 前重开 manifest 并重验候选、定义、
控制面、生产 migration/Registry/version 和 source endpoint/principal/table，
再在 worker 进入 canonical claim 前重复该检查；前者失败不得 submit，后者失败
不得接触隔离 DB/claim，两者都结构化为 `recovery_blocked` 并停止后续 dispatch。

replay 进程 allowlist 的 repository 查询契约只接受本 replay occurrence、runtime
当前 active future 和 current `operator_recovery + scheduled_live` running
attempt，并闭合 scheme/version/runtime/date/state/token 与 `PID=PGID>1`，只返回
已登记 leader PID/PGID。该查询只提供账本身份；OS 进程表、后代关系、UID 和
Popen-to-registration 窗口必须由专用进程 fence 独立验证。

真实 replay 的 MVP 顺序固定为：

1. 基于上述账本身份实现 replay 专用 OS 进程探针；
2. 在 runtime 每个 dispatch 接线，并以最小 start-window fence 闭合 `Popen`
   成功到 PID/PGID 登记之间的窗口；
3. 只通过 execute 入口完成冻结 candidate 的 Native/V2 与 target 全集的隔离
   MySQL 联跑。

隔离 replay MySQL 由专用 context manager 创建，固定使用本机新 datadir、新
server UUID、loopback 随机非 3306 端口和唯一 `bfl_real_replay_*` schema；
Engine 使用显式 URL 并安装 per-connection identity guard，不读取生产连接环境。
退出顺序固定为 dispose Engine、停止并确认 mysqld 退出、最后经 parent/root fd
清理原 inode；路径并发替换、进程仍存活或 owner/权限漂移均保留现场并
fail-closed。该层不应用 migration、不 seed Registry，也不执行算法。

当前 attestation/observation schema 有意只承认这组 21/25 与四个 V2，防止新增
任务偷用旧容量证据；它还不是最终的动态扩容接口。首次变更 active daily Registry
前，必须发布 schema v3，从签名 policy 与 candidate manifest 派生 item/target/V2
全集和 release offset，并对新全集重新完成 20+20、故障注入和稳定期。不得只改
Python 常量或沿用旧签名。

10 日是切换稳定期，不等于 95% 统计可靠性。若用零失败样本支持约 95% 的可靠性
表述，至少需要约 59 次有效日批。

切换必须一次完成：

1. 阻断 admin/operator；bootout legacy preflight、scheduler 和 backend，核验
   所有 scheduled-live、refresh/API background 子进程退出。
2. writer 全停后做一致性备份，只通过 pending-only migration runner 完成 schema
   history、preflight、017 和闭世界 postcondition；失败时保持服务停止。
3. 仓库 rollout 继续保持 `legacy`，只把三份**已安装** plist 的 mode 一起改为
   `ledger`；服务仍停止。复核 signed candidate v2 后，从仓库 canonical template
   通过 root operator 向固定 machine-global append-only chain 原子发布 genesis
   epoch。operator 必须在发布前再次验证当前 capacity admission/candidate。
4. 按 backend、单 scheduler 的顺序启动，preflight 保持 bootout 或仅允许
   ledger-disabled；核验实际进程环境、heartbeat、machine-global occurrence
   lock 和 refresh owner。

仓库三份 launchd 模板继续保持 `legacy`，生产切换只修改经备份的已安装副本。
仓库 rollout 也继续保持 `legacy`，只用于 epoch directory 完全不存在时的初始
bootstrap；epoch chain 存在后它不参与模式裁决，所有服务的显式 mode 必须与
最大完整 epoch 一致。任何时刻最多一个
路径拥有 scheduled-live 写权和一个 DataBridge refresh owner。

进入 epoch 控制后允许受控回滚，但只能在三服务 bootout、真实进程表无遗留
scheduler/backend/preflight/算法进程、全局（包括历史和未来业务日期）无非终态
occurrence/item、running run、无
`ABANDONED_FENCE_PENDING_CLEANUP`/未清孤儿时追加更高 `legacy` epoch。任何从
当前 ledger epoch 到更高 epoch（包括 `ledger → ledger`）都会使旧 occurrence
冻结身份失配，因此同样必须满足 quiescence。`legacy → ledger` 还必须拒绝旧
路径 running scheduled-live 和遗留进程，并重新通过当前 capacity admission。

epoch root/`records` 为 root-owned `0755`，record 为 `0644`，LaunchAgent 服务
UID 可读但所有非 root 不可写；`staging` 为 root-owned `0700`，与用户可写
runtime root 分离。发布先在同盘 staging 以 `O_EXCL` 完整写入、fsync、校验，
再 hard-link no-clobber 到 `records` 并 fsync 目录。staging 截断不参与 reader；
最终 record 截断必须 fail-closed，绝不忽略或覆盖。删除 chain、删改历史、
同/低 epoch replay、单独切 plist 均不是回滚。

root/operator 是信任根；本设计防普通服务、误配置、旧进程及正常运维 replay，
不声称抵御恶意 root 删除整条 chain。运行中 process-bound epoch 可检测删除；
整条 chain 被恶意 root 删除且全进程重启后，没有外部高水位锚就无法区分首次
bootstrap，必须作为未授权灾难恢复事件处理。
