# CGB 周度批量回测截止状态隔离设计

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

仅修改
`schemes/cgb_causal_wk_1y_v128/delivery/cgb_causal_wk_1y_v128.py` 的非冻结 Contract
包装区：

- 新增一个纯函数生成完整 cutoff signature，并据此稳定分组与回填结果；
- 将 `_one_pass_rows()` 的结果从 `week_id -> row` 改为 `signature -> row`，且只在
  资格判定成功后调用；
- 将核验、fallback、日志和返回顺序都改为使用 signature；
- fallback 对重复 signature 只运行一次，避免 Harness 的 100 条交替请求退化为 100
  次完整重算；
- 保留 `truncate_for_request()` 的逐 Request 截断与日历一致性检查，不放宽任何截止
  键校验。

不修改以下内容：

- 20 个 `_build_component_*` 冻结组件及其日频聚合、模型、阈值、训练窗口、投票和
  fallback 逻辑；
- `predict` 单点路径、输入制品、日历、数据库、Harness Gate 通用实现或 scheduler；
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

验收不以“同周不同截止日结果相同”为条件；验收条件是每条批量结果与同一条 Request
的独立截断结果相同。
