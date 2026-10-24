# 方案内部分歧点映射：five_y_factor_rule_online_v1

> 状态：首分歧层已定位；A2 补丁已双端验证；待落补丁
> 日期：2026-10-20
> 基线：`codex/develop` @ `d682d06d`；worktree `bond-factor-lab-parity`
> 脚本 SHA：`bb714a60c3ab4159c9b89ac6777f96fdf55e66509c0033501923fea8fe79d408`
> exact：`55f0e753b18e`
> 元数据 SHA：`a18ff21e318acb4a046d46f8572ba1a231c43724dd6db498929e3e89b81a859d`

## 1. 结论先行

1. **这个方案没有 A1（同分排序）问题。** 唯一的排序是 `blocks.unique().sort_values()`（按月块排序，行唯一），不涉及评分并列。调查报告里的 O010 同分机制不适用于本方案。
2. **8 天实测：首分歧层是 XGB 列采样。** 特征值按列名逐列一致、训练输入一致、LGB 模型与预测一致；XGB 的模型与预测在 8/8 天不同。根因是 XGB 无权重路径的 `std::shuffle`（libstdc++ vs libc++ 排列不同），与 O045 同一机制。
3. **补丁已验证：均匀 `feature_weights=np.ones(n_features)`。** 两端同一训练/测试数组下，无权重预测哈希 Mac `322bc9ca…` / ECS `7684096a…`（不同）；加权路径两端都是 `ad8e83bd…`，模型哈希都是 `25d4f8b9…`（逐位一致），且两端重复训练一致。
4. **A3（历史控制器）是放大层。** 8 天里的 2 天属于“当前方向相同、历史后验 0.55 vs 0.50”，正是 `controller_decision` 的一票之差；上游 XGB 修好后这层应自动收敛。
5. **B1/B2/B3 不是这 8 天的原因。** 特征值按列名一致、训练输入一致；它们可能影响其他日期，但已知差异日不是它们造成的。
6. **A4（特征列顺序）是独立的卫生问题。** 6/8 天两端 `bond_feature_set` 列顺序不同（值按列名一致），来自 `windows` 的 set 推导；模型用固定 tuple 列，所以不是分数差异的原因，但应改为 `sorted(windows)`。
7. **现有 full observer 已经抓到了 L1 需要的标量。** `prediction_trace` 返回 `combined_score`、`controller_posterior`、`raw_factor`、`rule_score`；Mac 和 ECS 的 observation 目录都在，可直接做首分歧层定位，不需要新插桩。

## 2. 可复现输入

| 项 | 值 |
|---|---|
| 数据目录（legacy_v1） | `frozen/old_common_20261011/reference/legacy_v1/data` |
| `daily_output.csv` SHA | `7c5ac0fbfc78e69b6a2fd422cddc8a2b44feaf41b680b697632620d098f0ca73` |
| `api_wind_date.csv` SHA | `72f4b1146e0277bbc65d93d071f34317edb51c6ab28bd384d36d6c75c530087b` |
| 请求分片示例 | `baseline_engine/plans/full-native-v1/shards/static-five_y_factor_rule_online_v1-00000/requests.csv` |
| Python | `/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python3.13` |
| release | `1073d68aa589d4278320ff0d43661403f2a1c8bb` |

## 3. 处理链与行号

| 阶段 | 位置 | 说明 |
|---|---|---|
| 读取日频/日历 | `read_inputs` 136–174 | `pd.read_csv` daily 147–152，calendar 154；`to_numeric` 172–173 |
| 按请求截断 | `truncate_for_request` 177–198 | cutoff、feature_date、周键校验；行序保持 |
| 特征构造 | `bond_feature_set` 205–258 | `pct_change` 201–202；rolling 227/233/236–237；`astype("float32")` 258 |
| 模型 | `make_regressor` 261–274 | LGB 263–268；XGB 269–274 |
| 月度训练/预测 | `factor_forecast` 277–306 | 月度块 286；训练窗 289–291；fit 299；predict 301；双模型均值 303 |
| 因子/规则融合 | `base_predictions` 309–337 | `raw_factor` 323；rule 325–328；`score` 331；方向 332–335 |
| 历史控制器 | `controller_decision` 340–367 | 历史窗口 351–356；后验 359–366 |
| 单条预测 | `predict_trace` 393–424 | 月末回退 399–424 |
| 批量预测 | `batch_predict_traces` 450–498 | 最长前缀训练 + 逐请求回溯 |
| 批量自检 | `results_for_requests` 501–529 | 首/中/末单条路径对照，失败则回退 |

## 4. 分歧点映射

| 机制 | 行 | 当前未约定行为 | 风险 | 补丁候选 | 语义等价检查 |
|---|---|---|---|---|---|
| **A1 同分排序** | 286 | `blocks.unique().sort_values()` 只排月份块；无 `argsort` | 无 | 不需要 | — |
| **A2 XGB 列采样** | 269–273 | `colsample_bytree=0.80`、`subsample=0.85`、`random_state=SEED`；XGB 私有 RNG + `std::shuffle`，跨标准库排列不同 | **高** | 先验证 8 天是否在采样层分歧；再决定固定标准库 / 规范化采样器 | 抽样比例、分布、种子契约不变 |
| **A2 LGB 采样** | 263–268 | `colsample_bytree=0.80`、`subsample=0.85`、`random_state=SEED`、`force_col_wise=True`；LGB 自有 RNG | 中 | 先验证；必要时 `deterministic=True` | 同上 |
| **A3 历史控制器** | 340–367 | `traded_history` 351；`chosen[-10:]` 354；`posterior=(correct+5)/(len+10)` 359–366；`>0.5 / <0.5 / ==0.5` | **高（放大层）** | 上游修好后自动收敛；补丁只固定顺序/tie | 历史窗口、票数、后验公式不变 |
| **A4 初态/集合** | 23, 212, 220–225, 454, 509 | `SEED` 固定；无全局 RNG；`windows` 是 set 推导，只影响 frame 列序；模型用固定 tuple 列 | 低 | `windows=sorted(...)` 作为卫生补丁 | 模型输入列序不变 |
| **B1 解析** | 130, 147–152, 154, 172–173 | 默认 pandas C parser，无 `float_precision`；1 ULP 输入差 | 中高 | 显式 `float_precision`（**语义决策**） | 字段/行数/NA 语义不变 |
| **B2 滚动** | 201–202, 227, 233, 236–237, 326–327 | pandas rolling sum/std/mean；跨编译器 ULP | 中 | 先测量；必要时固定实现 | 窗口/min_periods/NaN 语义不变 |
| **B3 float32** | 258, 301, 315–316, 328 | `astype("float32")`；predict 转 float64 | 中 | 不改语义；在 trace 中记录 cast 前后 | 转换点不变 |
| **B4 学习器** | 263–274, 299–303 | LGB `force_col_wise=True`、`n_jobs=1`，无 `deterministic`；XGB `hist`、`n_jobs=1` | 中 | 评估 `deterministic=True` / XGB 等价控制 | 模型族、超参、特征不变 |
| **B5 阈值** | 332–335, 361–366 | `score` 严格 `> ±0.335762...`；posterior 严格 `> / < 0.5` | 低（需余量） | 不改阈值；计算 `M_min` | 阈值、比较符不变 |
| **B6 暖启动/缺失** | 24–25, 292, 352–356, 399, 471 | `SIM_START`、`TRAIN_WINDOW`、`<180`、lookback 10/min 5、月末回退 | 中 | 固定边界；验证同输入行数 | 暖启动/缺失规则不变 |

## 5. L1 标量

本方案的最终方向不是单一概率，而是两层：

1. `combined_score`（`score`，float64）= `0.4 * factor_z + 0.6 * rule_z`；
2. `controller_posterior`（float64，可为 `None`）= 历史窗口正确率的后验；
3. 最终方向 = `score` 阈值判定 + posterior 翻转/归零。

**L1 验收对象**：`combined_score` 与 `controller_posterior` 都要比；只比方向会漏掉“方向相同但数值已分叉”的情况。

## 6. 现有观测资产

- full observer 已记录 `prediction_trace`（`raw_factor`、`rule_score`、`combined_score`、`controller_observations`、`controller_posterior`）。
- `controller_decision` 的 `locals` 含 `directions`/`labels` 的数组哈希与形状。
- 每个模型 fit 保存 `X-frame.pickle`、`model.txt`/`model.json`、`arrays.npz`。
- Mac 与 ECS 的 `run/full-history-observation/` 目录都存在；8 天对应分片可直接比对，不需要新插桩。

## 7. 首分歧层实测（8 天）

对 8 天逐个比较，结果高度一致：

| 层 | 结果 |
|---|---|
| 特征值（按列名） | 8/8 天完全一致 |
| 特征列顺序 | 6/8 天不同（A4 set 推导）；值按名一致 |
| 训练输入（fit X/y） | 8/8 天完全一致 |
| LGB 模型/预测 | 8/8 天一致 |
| XGB 模型/预测 | 8/8 天不同 |
| `combined_score` | 8/8 天不同 |
| 历史控制器 | 2 天在 `combined_score` 相同情况下后验不同 |

**首分歧层：XGB 列采样。**

## 8. 已确认的补丁：均匀 feature_weights

### 根因

XGBoost 3.1.3 `ColumnSampler::ColSample`：

- 无 `feature_weights`：`std::shuffle` + 取前 n；`std::shuffle` 的排列映射跨标准库不保证一致。
- 有 `feature_weights`：`WeightedSamplingWithoutReplacement`（Efraimidis-Spirakis key），走 `uniform_real_distribution` + 排序。

### 双端最小探针

同一训练/测试数组（756×30 / 23×30），方案参数：

| 路径 | Mac arm64 | ECS x86_64 |
|---|---|---|
| 无权重预测哈希 | `322bc9ca3221eb3d` | `7684096a03b6a6a5`（不同） |
| 均匀 `feature_weights` 预测哈希 | `ad8e83bd77e07ce7` | `ad8e83bd77e07ce7`（相同） |
| 均匀 `feature_weights` 模型哈希 | `25d4f8b912d22214` | `25d4f8b912d22214`（相同） |
| 本机重复 | 一致 | 一致 |

结论：均匀 `feature_weights` 路径在两端逐位一致；采样比例仍为 80%，分布等价（均匀不放回），只替换了未约定的 shuffle 映射。

### 补丁

- `make_regressor(family, n_features)`：XGB 分支加 `feature_weights=np.ones(n_features, dtype=np.float32)`。
- `factor_forecast` 调用处传 `n_features=len(columns)`。
- LGB 分支不变。

### 语义等价

- `colsample_bytree=0.80` 不变；选中列数 `max(1, int(0.8*n))` 不变；
- 均匀权重下的加权不放回抽样与 shuffle 取前 n 分布相同；
- 模型族、超参、特征集合不变；
- 新的采样序列是 canonical 序列；与老 exact 的历史输出可能不同，这是预期。

### 方案级 8 天双端复跑（第 1 轮）

补丁后的 `predict_trace` 逐条跑 8 天：

| 日期 | 方向 Mac/ECS | `combined_score` Mac / ECS | \|Δ\| | posterior |
|---|---|---|---:|---|
| 2025-07-01 | 0/0 | 0.253004344826659 / 0.253004344826659 | 1.67e-16 | None/None |
| 2025-07-25 | 0/0 | -0.323597750201041 / -0.323597750201043 | 1.5e-15 | None/None |
| 2026-01-14 | -1/-1 | -0.342431875231682 / -0.342431875231683 | 5.55e-16 | 0.6/0.6 |
| 2026-04-01 | -1/-1 | -0.359140019109186 / -0.359140019109185 | 9.44e-16 | 0.55/0.55 |
| 2026-04-08 | 0/0 | 0.291218088799182 / 0.291218088799184 | 2.22e-15 | None/None |
| 2026-08-17 | -1/-1 | 0.409628648525654 / 0.409628648525659 | 4.66e-15 | 0.4/0.4 |
| 2026-09-29 | 0/0 | -0.364591494406523 / -0.364591494406522 | 8.88e-16 | 0.5/0.5 |
| 2026-10-08 | 0/0 | 0.550752970283739 / 0.550752970283745 | 5.33e-15 | 0.5/0.5 |

**结果：方向不一致 0/8；`max |Δcombined_score| = 5.33e-15`（上限 1e-9）；`max |Δraw_factor| = 0`；`max |Δrule_score| = 8.99e-15`。**

剩余残差来自 pandas rolling（B2），量级 1e-14，远低于任何阈值余量。第 1 轮通过。

### 待验证

- 8 天重复第 2/3 轮，确认稳定性；
- 307 天全量双端复跑；
- 9 个 T5 方案是否同样使用 XGB（是：同一模板）；
- 其他 XGB 方案（cgb/liwei/current55 等）是否也走同一路径。

## 9. 剩余待定

| # | 问题 | 状态 |
|---|---|---|
| Q1 | XGB 列采样 | ✅ 均匀 `feature_weights` 已验证 |
| Q2 | B1 parser | ⏳ 8 天未发现特征值差异；其他日期再验 |
| Q3 | B4 deterministic | ⏳ LGB 8 天一致，暂不需要 |
| Q4 | `controller_posterior` 容差 | ⏳ 上游修好后重新测 |
| Q5 | A4 列顺序 `sorted(windows)` | ⏳ 卫生补丁，单独提交 |
