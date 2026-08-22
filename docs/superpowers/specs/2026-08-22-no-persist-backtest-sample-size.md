# no-persist 回测样本量设计

## 背景

`BlackboxBacktestGate` 的 no-persist 段（`all` 六段中的 `backtest`）不是历史回测。它取
`_comparison_requests` 生成的**两个**模板——当前 Request，以及各频截止均退一格的对照——然后
复制成 `sample_size` 份，用两种分区各跑一遍，验证 `batch_split_invariant`：结果不得随平台分批
方式改变。

现场实测（`weekly_1y_causal_v1_31_0_standalone`，2026-08-22）：

```
requests 100 → 全部是同样 2 个输入各 50 份
  feature=2026-08-20  predict=2026-08-22  target=2026-08-28   ×50
  feature=2026-08-21  predict=2026-08-22  target=2026-08-28   ×50
```

对照模板取「上一个交易日」。当 `feature_date` 落在周二至周五时，上一个交易日与它**同属一周**，
这使依赖「截止日之后不得再有本周交易日」的交付端批处理优化按构造必然失效，100 份请求全部退化
为独立全量拟合；两种分区各一遍即约 200 次拟合。实测单次约 98 秒 → **约 5.4 小时**，默认
`--timeout-sec 600` 因此必然超时。

若 `feature_date` 恰好落在周一，上一个交易日属上一周，批处理生效，同一 Gate 只需数分钟。
**即该 Gate 的耗时取决于入库当天是星期几**，这不是可接受的性质。

## 第一性判断

该 Gate 要证明的命题是「**平台如何分批不影响结果**」。这是一个结构性不变量：只要存在一次真实
的分批差异（一种分区为单批，另一种拆成多批），命题即被检验。把 2 个不同输入复制成 100 份，
增加的是**重复次数**，不是**分区形态**。

而重复次数所能覆盖的「重复执行确定性」已由 CompareGate 独立覆盖：其 `repeat_deterministic`
证据重复执行并比对方向，同一 Gate 还检验请求顺序不变性与 cutoff 之外行的未来数据隔离。因此
本 Gate 的 100 份复制对**不变量本身没有增量**，只按线性放大成本。

## 目标

在不削弱 `batch_split_invariant` 的前提下，把该 Gate 的默认成本降到与其命题相称的量级。

不改变：Gate 判定语义、`batch_split_invariant` 的定义与失败条件、`--sample-size` 参数与其
1–1000 取值范围、持久化回测路径、CompareGate 的任何检查。

## 最小设计

两处改动：

1. **默认样本量 100 → 4**，提取为具名常量 `DEFAULT_NO_PERSIST_SAMPLE_SIZE`。
   4 份使两个模板在两种分区中都出现，且交替分布（`templates[index % 2]`）。

2. **alternate 分区大小改为随实际样本量派生**：

   ```python
   alternate_batch_size = _alternate_batch_size(min(profile.max_batch_requests, sample_size))
   ```

   `_alternate_batch_size` 本身不改。效果：
   - `sample_size=4` → `_alternate_batch_size(4)` = 3 → 分区为 `[3,1]` 对 `[4]`，真实分批差异成立；
   - `sample_size≥100` 且 profile 为 100 → 仍为 80，既有行为与既有测试断言 `[100, 80]` 不变。

改动后该 Gate 的拟合次数从 `2 × sample_size` = 200 降到 8，预计约 13 分钟；批处理优化不适用时
仍能完成，适用时更快。

## 为什么不采用其它方案

- **在 Gate 内对相同请求去重**：会取消重复执行这一维度，且与 `sample_size` 的语义冲突；
  真正的确定性检查应留在 CompareGate，不应在此处以副作用形式存在。
- **只调大 `--timeout-sec`**：把成本原样保留，且 `onboard` 不透传 `--sample-size`，每个方案
  每次入库都要付一遍 5 小时。
- **改交付端 `_can_optimize_batch`**：属算法内部，平台不得改写；且问题不在交付。

## 测试与验收

1. 不传 `--sample-size` 时，`sample_size` 证据为 4，`run_blackbox_backtest` 被调用两次，
   两次 `profile.max_batch_requests` 分别为 `profile` 原值与 3。
2. `sample_size=4` 时 alternate 分区确实拆成多批（`max_subprocesses` ≥ 3）。
3. 既有 `sample_size ∈ {100, 101, 500, 1000}` 的用例断言不变，两次 `max_batch_requests`
   仍为 `[100, 80]`。
4. `batch_split_invariant` 的失败路径不变：构造两种分区结果不同时仍报
   `no-persist backtest results changed with platform batch partitioning`。
5. 持久化回测路径不受影响，仍拒绝 `--sample-size`。
6. 全量回归对照纯净 HEAD 无新增失败。

## 停止条件

- 需要改变 `batch_split_invariant` 的定义或其失败条件；
- 需要修改交付脚本或 `_comparison_requests` 的模板构造；
- 降低样本量后无法再构造出真实的分批差异。
