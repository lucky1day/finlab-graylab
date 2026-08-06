# Blackbox 灰度信号单快照批量回补与 CGB 周度截止隔离设计

## 决策

`cgb_causal_wk_1y_v128` 的批量适配层必须把一次预测的身份定义为完整的
输入截止状态，而不是仅定义为 `week_id`。同一 `week_id` 在不同的
`daily_cutoff_key` 下会得到不同的日度聚合特征和预测，这是正确的
point-in-time 语义，批量执行不得将它们合并为同一个结果。

采用“可证明的一次性 walk-forward + 截止状态去重的独立回退”方案：

1. 只有一个批次内所有唯一截止状态都能由同一份最宽截断输入精确代表时，才保留
   一次性 walk-forward 快路径；
2. 否则按完整截止状态去重，每个不同状态独立截断、独立运行一次，再把结果分发给
   原始 Request；
3. 不修改 20 个冻结的源算法组件、特征公式、模型参数或预测语义，只修改 Contract
   1.0 的批量适配包装层。
4. `gray_live` 是结果的生命周期/落库语义，不是回补时必须逐周获取 DataBridge、
   逐周重跑模型的计算拓扑。连续灰度区间可以参与同一次批量计算。

## 所有频率共用的输入不变量

灰度信号回补的最小输入单位必须是一次冻结的 DataBridge base snapshot，而不是一个
`predict_date`、一个周点或一个月点。一次回补作业先确定最大 as-of 边界，物理生成并
保存一份只含该边界及以前日/周/月数据的快照与 manifest；同一 DataBridge lineage、
同一 schema、同一最大 as-of 边界的日频、周频、月频 Blackbox 请求都从这个只读父快照
派生。

```text
一次 DataBridge 拉取 / 一份 base snapshot + manifest
  ├─ 日频 Request 批次（每条有自己的 T+1 feature/target/cutoff）
  ├─ 周频 Request 批次（每条有自己的 feature week/target week/cutoff）
  └─ 月频 Request 批次（每条有自己的自然 15 日 feature/target/cutoff）
```

不同方案、runtime profile 或平台附加输入可以在同一父快照上各自创建只读 runtime view
并各自调用模型；它们不应重新从数据库拉取、导出或保存另一份 DataBridge 数据。只有
source lineage、schema 或最大 as-of 边界不同，才允许创建第二份 base snapshot。

这里的“物理截断”必须同时覆盖三张输入表：日表不晚于 session 的最大日度 cutoff，
周表和月表不晚于该最大 feature date 对应的权威 period cutoff。快照保存后再写一份
不可变的 session manifest，绑定 `parent_snapshot_id`、DataBridge generation、refresh
date、source manifest、schema、最大 as-of 和三频上界；后续每条 Request 只引用其中
自己的已冻结 cutoff signature，不能再从可变的当前库重新推导截止键。

现有 `open_blackbox_input_snapshot(snapshot_date=predict_date)` 不能充当这个 session：它为
每条 Request 复制临时快照，而 `snapshot_date` 在当前实现中只是 current DataBridge 的
新鲜度校验参数，并不把三张表物理截到该日期。新的 session 必须在整个作业开始时读取和
校验一次 DataBridge，再永久化一次上述受最大 as-of 约束的 base snapshot；它绝不能在
每个预测点再次打开当前 DataBridge。

“单快照”不等价于所有算法必须只运行一次：能返回整条因果序列的方案（如本 CGB 周度
方案）应一次 walk-forward 取全表；其他日频/月频方案至少应接收同一 snapshot 下的
batch Request。若其交付契约无法证明一次模型运行等价于逐点独立截断，则保持逐
signature 的模型计算，但绝不重复 DataBridge 拉取、制品生成或快照保存。

对所有频率，单快照中的每条 Request 仍须保留自己的 point-in-time 语义：

| 频率 | 灰度实盘 Request 语义 | 同一快照中的计算规则 |
|---|---|---|
| 日频 | `predict_date=T+1`、`feature_date=T`、`target_date=T+horizon` | 由同一父快照按每条 `feature_date` 截断；可批量调用，输出必须等于独立截断。 |
| 周频 | `predict_date` 前一交易日为 feature，映射 feature week，目标为下一实际周 | CGB 等 walk-forward 交付件一次返回逐周表；同周多 cutoff 时按 signature 隔离。 |
| 月频 | 自然月 15 日触发，feature/target 分别取该日及目标月同日以前最近交易日 | 由同一父快照按每月 cutoff 截断；是否一次模型运行取决于交付件的等价证明。 |

快照一经冻结，任何 Request 需要晚于其最大 as-of 边界的数据都必须 fail-closed，不能
在同一作业中临时再拉一份“更近”的 DataBridge 来补齐。

## 入库补齐的主路径

本设计的性能主场景是**方案入库时补齐一段连续的历史信号**，不是为每个历史周重新
生成输入并重启模型。操作员先选定一个权威数据截止边界，平台只使用不晚于该边界的
同一份 DataBridge base snapshot，并在该快照上构造每个预测点的输入视图：

```text
一份固定 as-of 输入
  -> 一次完整 walk-forward
  -> 一张逐周预测表
  -> 依 Request / target_date 范围筛选所需周
  -> 按阶段持久化，并由现有 API 读回前端
```

一次完整 walk-forward 在进程内部仍会逐周训练和预测；优化消除的是外层的重复前缀
计算。若需补齐 72 个周点，逐周独立模式会重复计算 W01、W02 等历史前缀，而一次运行
只计算 W01…W72 一次并返回所有行。

截至边界必须按日期语义解释，不能只写一个自然日文本。这里的“补齐到 2026-08-01
的数据”定义为**输入数据 as-of 边界**，不是 `target_date` 上界。2026-08-01 是非交易日，
因此必须由权威交易日历解析为 2026-07-31 的最后交易日。以该日为最大 feature cutoff
的一次计算会同时返回此前所有周的结果，并包含以 2026-07-31 为 `feature_date`、下一
实际周为 `target_date` 的最新信号。

无论选择哪一种边界，数据库中已经存在更晚日期的数据也不得被读入这次运行。输入
artifact 和 `daily_cutoff_key` 必须物理截断到选定 as-of 状态，保证补齐结果不吸收未来
数据。

同一批输出在**计算完成后**才按预测语义分流：本方案 `target_date <= 2026-05-29` 的结果
属于 historical backtest；`target_date >= 2026-06-01` 的结果属于 `gray_live`/实盘信号，
不能因为同样由一次回放产生就写入 `t_backtest_*`。对于 2026-06 至 2026-07 的灰度周，
仍然为每条记录保留其原本的 `predict_date=T+1`、`feature_date=T`、`target_date`、各自
cutoff 和 `prediction_phase=gray_live`；但它们共享一次输入读取和一次 batch delivery。
前端展示由现有 Registry/API 读取已持久化的正确阶段结果，不需要为每个周点单独刷新
或改写前端算法。

具体执行顺序为：

```text
一次拉取、生成并保存截至 2026-07-31 的 DataBridge base snapshot
  -> 由权威周历枚举历史 + gray_live 的每个预测点和各自 cutoff
  -> 按交付件分别发起 Blackbox `backtest` batch（当前 profile 单批上限 100 条）
  -> delivery 一次 walk-forward 返回逐周结果
  -> 按原始预测点回填日期、阶段和 provenance
  -> 仅在持久化阶段拆回各自的 historical / gray_live 业务键与审计 run
```

这里“为每个预测点构建 cutoff”只是在内存中生成 Request 元数据和对同一父快照的
前缀视图；不是为每个日/周/月点重新查询数据库、生成或保存 DataBridge 制品、复制
runtime 文件或启动算法进程。

## 问题边界

日频输入先通过 `api_wind_date.csv` 映射为 `date -> week_id`，然后对每周已纳入的
日度观察做 `last`、`mean`、`sum`、`change`、`range`、`volatility`、`slope` 和
`valid_days` 等聚合，并经过滚动 z-score、利差和交互特征后合并到周度特征表。

因此 W30 截至 2026-08-04 与截至 2026-08-05 是两个合法但不同的特征快照：后者多了
一个 W30 日度观察，`last`、`mean`、`change`、`range` 等可能变化，预测不同是正确的。
问题不在独立预测结果不同；问题在现有 `_one_pass_rows()` 使用 `week_id -> row` 映射，
把两个不同的截止状态压缩成一个结果。`max(... weekly_cutoff_key)` 在同周并列时还会受
Request 输入顺序影响。

## 方案比较

1. **推荐：状态感知的混合批量路径。** 对可证明等价的完整周序列保留一次性运行；对
   冲突状态按完整截止状态去重后独立运行。历史周度回测仍享受一次性计算，而同周多
   截止状态保持严格正确。
2. 所有 Request 永远逐条独立运行。正确但会把正常的 72 条历史周回测重新放大为
   72 次完整 walk-forward，不采用。
3. 在最大截止状态运行一次，再按 `week_id` 强制复用结果。速度快但会向较早的同周
   Request 泄漏后续日度观察，正是当前缺陷，不采用。

## 输入身份与数据流

每个 Request 的缓存/结果身份为有序三元组：

```text
(daily_cutoff_key, weekly_cutoff_key, monthly_cutoff_key)
```

`request_id` 只用于请求关联和日志，不参与算法输入身份。完全相同的三元组可安全地
共享一次计算；三元组任一字段不同，都必须先被当作不同的输入状态处理。

```text
原始 Requests（保持调用者顺序）
  -> 按完整 cutoff signature 分组
  -> 若整组可一次性等价运行：一次 walk-forward + 三点独立复算
     否则：每个唯一 signature 独立截断并运行一次
  -> signature -> 结果
  -> 按原始 Requests 顺序返回 Contract 输出
```

## 一次性运行的资格判定

一次性运行只在以下条件全部成立时使用：

- 批内每个 `weekly_cutoff_key` 最多对应一个完整 cutoff signature；相同周但不同签名
  直接使整批走状态去重的独立路径。
- 选择 `daily_cutoff_key` 最晚的 Request 作为最宽输入，而不是按
  `weekly_cutoff_key` 做不稳定的并列选择。
- 在这份最宽日频截断中，每个 Request 的 `daily_cutoff_key` 都是其
  `weekly_cutoff_key` 对应周内最后一条已纳入日度记录。这样一次性构造出的该周聚合
  特征，与该 Request 的独立截断完全相同。
- 既有的确定性首/中/末三点逐字段独立复算继续执行；任一字段不一致时整批回退到
  按完整 signature 去重的独立路径。

这允许正常的历史周度序列继续只跑一次。若最新一周是当前输入的最宽截止状态，它
可以是未收周的 as-of 快照；它不能与同周较早的 as-of 快照共同走一次性路径。

## 实现范围

实现分为两个边界清晰的层：

1. `schemes/cgb_causal_wk_1y_v128/delivery/cgb_causal_wk_1y_v128.py` 的非冻结 Contract
   包装区负责正确消费一个批次；
2. 方案入库/gray gap 的 Blackbox 协调层负责把同一方案、同一精确版本、同一
   DataBridge snapshot lineage（`generation_id`、manifest、`refresh_date`、replay mode
   相同）的连续周点合并为一个输入快照和一个 batch 调用。每条 Request 自己的
   `cutoff_date` 仍然不同且必须保留。协调层在计算完成后再把记录交回既有逐
   `predict_date` 的审计和原子写入边界。

交付包装层的职责：

- 新增一个纯函数生成完整 cutoff signature，并据此稳定分组与回填结果；
- 将 `_one_pass_rows()` 的结果从 `week_id -> row` 改为 `signature -> row`，且只在
  资格判定成功后调用；
- 将核验、fallback、日志和返回顺序都改为使用 signature；
- fallback 对重复 signature 只运行一次，避免 Harness 的 100 条交替请求退化为 100
  次完整重算；
- 保留 `truncate_for_request()` 的逐 Request 截断与日历一致性检查，不放宽任何截止
  键校验。

协调层的职责：

- 在整个 gray 回补作业范围内建立一个 `DataBridge base snapshot session`：一次读取、
  一次保存不可变 manifest、一次冻结最大 as-of 边界并物理裁剪三频表；所有兼容的
  日/周/月 Blackbox batch runner 只读复用该 session。每条 Action 已冻结的 cutoff
  signature 必须存在于 session，且不得晚于其三频上界；
- 新增一个只读的 Blackbox batch runner 接口：接收一个 config、同一 snapshot lineage
  下的有序预测点和一个最大 as-of 边界；它使用冻结 Action 的原始 live 日期和独立
  cutoff，而不是再查询当前库；每个 config 只打开一次输入快照/runtime view，并调用
  `run_blackbox_backtest()`。超过 Contract batch 上限时只分割模型 CLI Request，不分割
  DataBridge session；
- `signal-gap-fill` 对满足上述兼容条件的 Blackbox 日/周/月 gray gap 使用该接口，而不再按
  `(base_scheme_id, predict_date)` 分别调用 `run_configured_scheme()`；
- 继续保留逐 `predict_date` 的授权、run 创建、insert-only 业务键和完成/失败审计；批量
  只合并计算，不扩大任何 token 或写入权限；
- 若 DataBridge snapshot lineage、精确版本、运行时、频率、输入身份不同，或批内出现
  同周多个 cutoff signature，则不合并该集合，走已有的隔离路径；仅 `cutoff_date` 不同
  是连续历史/灰度周点的正常情形，不能据此拆分 DataBridge 或算法运行。

不修改以下内容：

- 20 个 `_build_component_*` 冻结组件及其日频聚合、模型、阈值、训练窗口、投票和
  fallback 逻辑；
- `predict` 单点路径、日历、数据库 schema、Registry、scheduler 调度语义或 Harness
  的授权范围；
- 任何生产数据库、Registry、launchd plist 或 scheduler admission 的状态。

修改交付脚本会生成新的精确 `scheme_version`。现有
`ee921f65476c` 的历史入库记录和无能力 gray admission 保持为历史事实，不能被新
脚本复用；新版本在后续获得明确授权后必须重新走相应的 Blackbox Gate/登记流程。

## 错误处理与可观测性

- 若 Request 自身截止键与权威日历不一致，继续由现有 `truncate_for_request()`
  fail-closed。
- 若批次不满足一次性资格，日志明确写入“同周多截止状态/在最宽输入中不是该周最后
  日度记录”，随后走 signature 去重的独立路径；这不是错误，也不应把不同预测强行
  对齐。
- 若一次性路径的三点独立复算有任一字段不一致，日志记录 signature、字段和双方值，
  然后整批回退到独立路径。
- 返回记录数、Request 顺序和 `request_id` 必须与输入一一对应；批次分割或输入顺序
  改变不得改变同一 Request 的结果。

## 验收与测试

新增一个专用的 CGB delivery 回归测试，使用轻量确定性替身替代昂贵的冻结模型管线，
但仍调用真实批量包装函数和真实截止/日历检查。测试覆盖：

1. 同一 W30、不同 `daily_cutoff_key` 的两个 Request：批量结果逐条等于各自独立结果，
   且两个 as-of 预测允许不同；不再按 `week_id` 串用结果。
2. 同一完整 signature 的重复 Request：只计算一次并按原始顺序 fan-out；不因
   `request_id` 不同而改变结果。
3. 正常的不同周、完整周历史序列：保留一次性路径，结果与逐 Request 独立截断逐字段
   一致。
4. 含同周多截止状态的 100 条交替批次，以及不同 batch-size/反向输入：结果按
   `request_id` 映射一致，且独立计算次数按唯一 signature 而非原始条数计。
5. 现有 Blackbox Contract/Harness 单元测试和静态检查仍通过；执行实际零写 CGB
   no-persist BacktestGate 以验证批次大小不变性和截止隔离。
6. 入库补齐回归用一个固定 as-of 输入覆盖连续历史周和 2026-06 起的 gray_live 周，断言
   DataBridge 快照/runtime view/Blackbox batch 各只打开或调用一次，delivery 只启动一次
   完整 walk-forward、返回所需全部周行、不会读取截止日之后的数据；持久化测试分别
   断言 historical 与 `gray_live` 结果不会跨阶段落表，且每条 gray 记录仍带原始 live
   日期、cutoff、phase、exact version 和 authority。
7. 日/周/月混合的 gray 回补计划断言整个作业只生成一个 DataBridge base snapshot 和一个
   immutable manifest；各频率 Request 都引用同一父 snapshot ID，并各自满足日频 T+1、
   周频 feature-week、月频自然 15 日语义。日频或月频 delivery 若不能证明全序列一次
   运行等价，允许多次模型运行，但 snapshot/export 计数仍必须为一；快照后的源数据
   变动或任何晚于 session 上界的行都不能影响结果。

验收不以“同周不同截止日结果相同”为条件；验收条件是每条批量结果与同一条 Request
的独立截断结果相同。

## 已批准的实施计划

详细的 TDD 实施计划见
[2026-08-06-blackbox-gray-replay-shared-snapshot.md](../plans/2026-08-06-blackbox-gray-replay-shared-snapshot.md)。
当前状态为**核心实现已在本分支完成、生产/业务入库尚未执行**。已完成的独立提交为：

1. `345170c feat: add blackbox gray replay snapshot session`：一次 current DataBridge 读取、
   三频物理截断与不可变 session manifest；
2. `c51f658 feat: batch blackbox gray replay execution`：同一 session 的 Blackbox Contract
   batch 执行；
3. `ac21c23 feat: share snapshots across blackbox gray gaps`：冻结 authority 的 preflight/
   postflight 回放与同源 Blackbox 批量 fan-out；
4. `b1ad1b1 fix: isolate CGB batch cutoff signatures`：完整 signature 去重、同周冲突隔离、
   真实日度周末资格判定、signature 行映射与稳定抽样核验。

最后一项修复还解决了旧 Harness 诊断中的性能退化：100 条交替 Request 虽只含两个完整
截止状态，旧代码在 one-pass 核验失败后会重跑 100 次；现在会对两个 signature 分别独立
运行一次，再按原始 Request 顺序回填。连续且每周完整的历史序列仍使用一次
walk-forward 加至多三条独立核验。

本地代码回归已通过：服务环境的共享快照/执行器/信号补齐/CGB/Contract 聚焦套件为
`181 passed, 94 subtests passed`，DataBridge executor 套件为 `5 passed`；CGB 交付测试也在
`blackbox-v2-v1` 实际环境中以 `unittest` 通过。上述均未写业务表、未激活、未改 Registry、
未触发 scheduler/launchd，且不构成生产入库。

`b1ad1b1` 后重新计算的精确 CGB Blackbox version 为 `59415aa789c5`。旧
`ee921f65476c` 的证据不可复用；真实零写 BacktestGate 和该精确版本完整七段 Gate 仍待在
明确授权下执行。其提交边界与后续验收顺序固定如下：

1. `shared.input_artifacts` 创建一次读取、一次物理三频截断、一次持久化 manifest 的
   `BlackboxGrayReplaySession`；所有请求的 cutoff 必须存在于该父快照。
2. `scheduler.executor` 对同一 Blackbox config 从该 session 调用 Contract `backtest`；超过
   批量上限只切分模型 CLI batch，不新建 DataBridge 快照。
3. `signal-gap-fill` 将同源的日/周/月 Blackbox groups 合并为一个 session；本次 fill 的
   preflight/postflight 复用该 session 已验证的冻结 authority，不再次读取 mutable
   DataBridge。结果回填和 `complete_gray_gap_run()` 仍保持逐 `predict_date` 的授权、run 和
   原子写入边界。
4. CGB delivery 将 `week_id` 缓存键改为完整 cutoff signature，并对不满足 one-pass 条件的
   请求按唯一 signature 独立回退；冻结算法组件零修改。
5. 所有测试和零写 BacktestGate 通过后，因 delivery hash 改变而重新取得该精确 Blackbox
   version 的 Gate 证据；不自动执行 activation、持久化回测、gray 写入、Registry 或
   launchd 操作。
