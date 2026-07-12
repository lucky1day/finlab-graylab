# liwei_0616 baseline Phase A 增量缓存设计

日期：2026-07-12
状态：已确认，待实施计划

## 1. 背景与结论

当前 5 个 `liwei_0616` 日频 T+5 方案在每个新 `feature_date` 上都会重新执行完整 source PIT 窗口。昂贵部分是每个 baseline 的 Phase A LightGBM 网格：即使旧 test day 已训练并得到稳定的逐日 `preds/probs`，下一日运行仍会从历史起点重新训练这些旧日期。

原始 source `*_ctx.pkl` 保存的是 `test_dates`、`vs_full`、`ml_base`、`ens`、`final`、seasonal VT 上下文等逐日结果，不是可直接对下一日继续 `fit` 的 LightGBM 模型。因此，本设计不直接追加 source ctx pkl，而是持久化平台现有 `phase_a_cache` 契约中的逐 config、逐 test day `preds/probs`。

目标口径已经确认：

- 首次运行执行一次冷启动，生成 baseline Phase A 缓存。
- 后续仅为缓存中缺失的新交易日执行 Phase A 训练。
- 旧日期 Phase A 必须复用，不得再次从 2023/2024 或其他历史起点训练。
- Phase B/C、seasonal VT、共识投票和 streak fallback 仍按完整 source window 重算，保持 source 算法语义。
- 历史输入、baseline 配置或算法缓存版本变化时允许自动冷重建。

## 2. 范围

仅覆盖以下 5 个方案：

- `liwei_0616_cons_sda_k3_div_k10`
- `liwei_0616_7y01_cons_say_k3_div_k10`
- `liwei_0616_7y03_cons_all_k3_div_k8`
- `liwei_0616_10y01_cons_say_k3_div_k10`
- `liwei_0616_10y02_cons_say_k3_div_k5`

按 tenor 和 baseline 共享 11 个缓存：

| Tenor | Baseline | 使用方案 |
| --- | --- | --- |
| 5Y | `STD` / `DIV` / `ACCWT` | 5Y_01 |
| 7Y | `STD` / `DIV` / `ACCWT` / `CROSS_5Y` | 7Y_01、7Y_03 共享 |
| 10Y | `STD` / `DIV` / `ACCWT` / `V55_7Y` | 10Y_01、10Y_02 共享 |

不在本次范围内：

- 其他日频、周频、月频方案。
- daily/weekly/monthly 输入 artifact 的增量导出优化。
- 复用上一日 LightGBM 模型对象直接打分。
- 修改 IC screening、特征、模型参数、训练窗口、VT、投票或 fallback。
- 修改数据库表、registry、scheduler 日期语义或 API 契约。

## 3. 分层与组件

### 3.1 通用缓存存储层

在 `shared/` 增加通用 Phase A 缓存存储模块，职责限定为：

- 计算和校验缓存 identity、输入前缀指纹与缓存水位。
- 读取和写入内部可信 pkl。
- 合并旧 cache 与新增日期的 Phase A cache。
- 文件锁、损坏检测、原子替换和审计元数据。

该模块不 import `schemes.*`，不理解具体 baseline 算法，也不访问或写入数据库。

### 3.2 方案推理编排层

5 个方案的 `inference.py` 负责：

- 提供当前方案 core 的 `model_config`、`run_prediction` 和 required baselines。
- 构造原始 source PIT `test_ranges/current_start/current_end`。
- 调用通用缓存层找出每个 baseline 的缺失 test dates。
- 对缺失日期调用现有 `run_prediction(..., phase_a_only=True)`。
- 将完整 Phase A cache 传入现有 `run_*_for_feature_window(..., phase_a_caches=...)`。

### 3.3 core 纯净边界

`schemes/*/core/` 不读取或写入 pkl，不持有文件路径，不加锁，不访问 DB。现有纯函数契约继续使用：

- `phase_a_only=True` 产生 Phase A cache。
- `phase_a_cache=<cache>` 复用指定 test dates 的 Phase A 输出。
- `test_ranges=((day, day), ...)` 只选择缺失日期进行训练。

本设计不移动 source 固定锚点，也不改变任何算法内部计算。

## 4. 缓存路径与结构

运行期缓存根目录：

```text
backtest_artifacts/runtime_cache/liwei_0616/
├── 5y/{STD,DIV,ACCWT}.pkl
├── 7y/{STD,DIV,ACCWT,CROSS_5Y}.pkl
└── 10y/{STD,DIV,ACCWT,V55_7Y}.pkl
```

`backtest_artifacts/` 已是运行期 gitignore 区域，缓存不得进入提交。

每个 pkl 是带 schema 的内部 envelope：

```text
schema_version
cache_family
tenor
baseline
baseline_fingerprint
runtime_fingerprint
watermark
input_prefix_fingerprints
phase_a_cache:
  test_dates
  results[]:
    config
    preds
    probs
created_at
updated_at
```

`baseline_fingerprint` 至少覆盖：

- baseline 固定配置的 canonical JSON。
- `SOURCE_IC_SCREEN_START`、`HORIZON`、`PURGE_GAP`。
- 声明式 Phase A cache ABI/version。
- LightGBM、NumPy、pandas 和 Python 关键运行时版本。

7Y 两个方案或 10Y 两个方案只有在 canonical baseline fingerprint 完全相同时才允许共享。测试必须机器校验成对方案的 baseline 配置等价；不等价时生成不同 key，禁止误共享。

## 5. 增量算法

对每个 baseline 执行以下流程：

1. 根据原始 `test_ranges`、baseline close 列有效性和 `require_labels=False` 计算本次完整 `requested_dates`。
2. 获取该 tenor/baseline 的独占文件锁。
3. 读取缓存；在锁内再次校验 identity、输入前缀和水位。
4. 若缓存不存在或已失效，令 `missing_dates=requested_dates`，执行冷启动。
5. 若缓存有效，令 `missing_dates=requested_dates-cached_dates`。
6. 若 `missing_dates` 非空，将其转换为一组单日 `test_ranges=((d1,d1),(d2,d2),...)`，调用当前方案 core：

   ```python
   run_prediction(
       model_config(
           baseline,
           ...,
           test_ranges=missing_ranges,
           phase_a_only=True,
           require_labels=False,
       )
   )
   ```

7. 按 canonical config key 校验两份 cache 的 config 集合与顺序，按日期合并 `preds/probs`，禁止覆盖同日期的不同结果。
8. 更新水位与输入前缀指纹，通过临时文件、flush/fsync 和 `os.replace` 原子提交新 pkl。
9. 释放锁。
10. 使用原始完整 `test_ranges` 和合并后的 `phase_a_caches` 重跑 Phase B/C、seasonal VT、共识与 fallback，只选择当前 `feature_date` 输出。

当 `missing_dates` 为空时，Phase A 训练调用数必须为 0。

当一次补入多个新交易日时，只训练这些缺失日期；已缓存日期不得重训。

## 6. 为什么旧日期 Phase A 可以复用

liwei_0616 使用固定 `SOURCE_IC_SCREEN_START=2024-01-01`。每个 test day 的滚动模型只使用该日之前并满足 horizon/purge gap 的历史样本；在只追加未来数据且旧输入不修订时，未来新行不会改变旧 test day 的训练集合和 Phase A 输出。

seasonal VT、信号精度、共识和 streak fallback 具有路径依赖，因此不直接缓存最终方向作为新日真值。它们每次继续使用完整 source window 重算，以确保上下文和最终输出与无缓存路径一致。

## 7. 输入前缀校验与失效

缓存保存上一个 watermark 对应的规范化输入前缀指纹：

- daily：截至旧 watermark 的规范化日频行。
- weekly：截至旧 watermark 可见的周频输入行。
- monthly：截至旧 watermark 可见的月频输入行。

当前运行只追加新日期时，旧前缀指纹保持不变，缓存可扩展。

以下任一条件触发自动冷重建：

- 已缓存水位以前的 daily/weekly/monthly 值、日期或 schema 发生变化。
- baseline canonical 配置变化。
- Phase A cache schema/ABI 变化。
- 关键运行时版本变化。
- pkl 无法反序列化、字段缺失、数组长度不一致或 config 集合不一致。

失效缓存不得被当作命中使用。损坏或不兼容文件先移出 active path，再冷建新文件；新文件成功提交前保留上一份可审计副本。

对早于当前 cache watermark 的只读预测请求，若所需日期已缓存且输入前缀一致，可直接切片读取；不得降低 watermark 或删除较新日期。

## 8. 并发、失败与恢复

- 每个 tenor/baseline 使用独立锁文件，允许不同 baseline 并行，但同一 baseline 只能有一个扩展者。
- 7Y_01/7Y_03 或 10Y_01/10Y_02 并发时，后获得锁的进程必须重新读取缓存；若第一进程已补齐日期，第二进程不得再次训练。
- 训练、合并或校验失败时不写部分 cache，当前预测 fail-closed。
- 原子替换前完成完整 envelope 校验；替换后重新读取关键元数据确认水位。
- cache 命中不是跳过日期语义校验的理由。scheduler executor 的 `predict_date/feature_date/target_date` 校验继续生效。

## 9. 预测审计字段

5 个方案的预测 `extra` 增加：

- `phase_a_cache_status`: `cold_build`、`extended` 或 `hit`。
- `phase_a_cache_watermark`。
- `phase_a_cache_missing_dates`。
- `phase_a_cache_version`。
- `phase_a_cache_fingerprint`。

多个 baseline 状态不一致时，保存逐 baseline 明细，并给出方案级汇总状态：任一冷建为 `cold_build`，否则任一扩展为 `extended`，全部命中为 `hit`。

## 10. 预热与日常运行

提供受控预热入口，只读源数据库并写运行期 cache，不写预测表、run 表或 registry。预热一次执行三个代表方案即可覆盖 11 个 baseline：

- 5Y_01：生成 5Y 三个 baseline。
- 7Y_01：其 required baselines 包含 fallback `DIV`，生成 7Y 四个 baseline。
- 10Y_02：其完整 OOS 日期集合覆盖 10Y_01 的两段窗口，并生成 10Y 四个 baseline；用超集方案预热可保证两个 10Y 方案直接复用。

部署顺序：

1. 在非 scheduler 写库路径执行当前 feature date 的 cache 预热。
2. 验证缓存与无缓存结果一致。
3. 再让正常 scheduler 使用缓存。

预热不是发布到 `master` 的授权，也不写业务数据库。

## 11. 测试与验收

### 11.1 单元测试

- cold build 创建合法 envelope。
- 连续第二日只把新日期传给 `phase_a_only`。
- 已缓存日期训练调用数为 0。
- 一次缺多个日期时只训练缺失集合。
- 合并保持 config 顺序、日期排序、dtype 和数组长度。
- 7Y 与 10Y 成对方案 fingerprint 相同并共享缓存。
- 历史输入、配置、ABI 或运行时版本变化触发冷建。
- 损坏 pkl、并发竞争和训练异常不产生部分文件。
- 旧日期请求不降低 watermark。

### 11.2 结果保真测试

- 同一日期 cold cache 路径与完全禁用 cache 路径逐行一致。
- 连续两个 feature dates：第二日增量路径与第二日无缓存完整路径一致。
- 比较最终 direction、confidence、`vote_score`、所有 baseline `*_score/*_dir`。
- 采用现有 benchmark 样本和 live-safe 日期做 CompareGate；最大允许误差沿用当前 CompareGate 契约，不因缓存放宽。

### 11.3 平台 Gate

5 个方案均须通过：

- UnitGate。
- StaticGate。
- InputGate。
- Dry-run Gate。
- CompareGate。
- 针对性 live date alignment 测试。

Dry-run/Compare 不得写业务数据库；缓存文件写入属于已声明的 L0 运行期 cache 副作用。

### 11.4 性能验收

不以机器墙钟时间作为唯一判定。必须通过可观测调用计数证明：

- 首次冷建后，新增 1 个交易日时，每个 baseline 的 Phase A `test_dates` 仅包含该新日期。
- 旧日期 `run_config` 训练次数为 0。
- 同 tenor 第二个方案的 Phase A 训练次数为 0。

## 12. 算法改动分级

本设计属于 L0/L1 边界：

- L0：pkl、路径、锁、指纹、extra 和运行期缓存编排。
- L1：复用 source runner 已明确暴露的 `test_ranges`、`phase_a_only` 和 `phase_a_cache` 上下文参数，仅将缺失日期传入 Phase A。

不允许发生 L2 改动。若实施中发现仅凭现有上下文参数无法做到增量结果与无缓存路径一致，必须停止并报告，不得修改训练锚点、窗口、特征、selector、VT、投票、fallback 或 score 映射来贴合结果。

## 13. 完成定义

以下条件全部满足才算完成：

1. 5 个 liwei_0616 方案默认使用共享增量 Phase A cache。
2. 正常追加新日期只训练缺失新日期。
3. 旧日期、第二个同 tenor 方案不重复执行 Phase A 训练。
4. 缓存与无缓存路径的最终和内部结果通过保真比较。
5. 缓存失效、并发、损坏和失败恢复行为有自动化测试。
6. 预热入口可在不写业务数据库的情况下生成当前水位缓存。
7. 未修改其他方案、数据库 schema、registry 身份或日期语义。
