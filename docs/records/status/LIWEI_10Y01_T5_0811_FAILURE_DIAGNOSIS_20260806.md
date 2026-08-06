# Liwei 10Y_01 2026-08-11 T+5 单次失败诊断（2026-08-06）

**诊断状态：** `DONE_WITH_CONCERNS`（只读；未修复、未补数）

**唯一分类：** `CONTROL_PLANE` —— launchd one-shot 批次未先执行共享 Phase A cache 的 publisher，导致
`liwei_0616_10y01_cons_say_k3_div_k10`（consumer）在输入状态已前移时按契约拒绝旧 generation。

**置信度：** 高。launchd 摘要未输出子进程的原始异常文本，且本任务禁止手工 SQL，因此没有把推定的
run ID 或未读取的 `t_scheme_run_log.error_msg` 当作证据；但已对 2026-08-05 的输入 artifact 做了
同一等价口径的纯只读重算，证明它与当日随后由 publisher 生成的 generation 精确等价、与前一
generation 不等价。结合事故前 runner 的稳定排序，失败会确定性落入
`CACHE_PUBLISHER_REQUIRED: ... requires publisher refresh` 分支。

---

## 1. 范围与事故身份

本记录只诊断一个未成功的 scheduled-live 业务键，绝不把它并入 G3.1 的 T+1 历史补写：

| 字段 | 只读确认结果 |
|---|---|
| base scheme | `liwei_0616_10y01_cons_say_k3_div_k10` |
| runtime / 任务 | `native_adapter` / 日频 `T+5` |
| 当前精确 version | `481f79b25fae`；相对 `d440091^` 的 scheme 目录无差异，故与事故前 runner 所见的 scheme 内容相同 |
| Registry | 激活核验返回 `ok`；active target 集合精确为 `[("10Y", 5)]`，canonical composite identity 为 `liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y` |
| 预测语义 | `predict_date=2026-08-05` → `feature_date=2026-08-04` → `target_date=2026-08-11` |
| 输入 authority | Native `legacy_db`，经 `shared.input_artifacts`；不是 `data_bridge_current` Blackbox 路径 |

上述日期来自只读交易日历调用；方案代码也明确以 `previous_trading_day` 和
`nth_trading_day_after(..., 5)` 建立同一映射。

## 2. 直接运行、Harness 与输入证据

### 2.1 launchd batch receipt

主工作区的只读 `com.bond-factor-lab.daily-predictions.log` 第一条记录显示 2026-08-05 日频批次：

- `outcome="partial"`、`exit_code=1`；
- 唯一 `failed` item 是本 consumer，代码为 `execution_failed`；
- 同一批次的 publisher `liwei_0616_10y01_full_oos_k3_div_k10` 成功，`run_id=2141`。

这不是 Registry/lifecycle 的 `skipped`，也不是 Blackbox admission 的 `blocked` 或 `denied`。
执行器的 Native 路径会先核验 activation、创建 run，再运行方案；随后若子进程或校验抛错，则以
`fail_scheme_run_atomic` 写入 failed run 与 run log。按本任务的“不得手工 SQL”边界，未直接读取该
单条数据库 error message，也不臆造其 run ID。

没有可借用的 D1 Harness receipt：G3.1 状态记录明确将“8 月 5 日 Liwei consumer 对未来 target 的
单独失败”排除在其窗口外，且其 `signal-gap-fill` selection 明确不含 T+5、2026-08-11。本次没有
执行 Harness gate、signal-gap 写入、手动 runner 或任何生产控制面操作。

### 2.2 输入已生成且 cutoff 正确

目标 consumer 的现场 runtime input artifacts 均存在，且只读检查得到：

| artifact | 最后键 / cutoff |
|---|---|
| `daily_output_2026-08-05.csv` | `date=2026-08-04` |
| `weekly_output_2026-08-05.csv` | `week_id=202630` |
| `monthly_output_2026-08-05.csv` | `month_id=202608` |

因此没有“artifact 未生成”或 `feature_date` 误截止到 8 月 3 日的证据。只读健康检查确实把若干
监控用 source-watermark 标记为缺失；该检查是固定 tenor watermark 的告警规则，并不读取此方案的
artifact 内容，不能凌驾于已成功生成、且后续 publisher 可用同一输入状态完成刷新的事实之上。

## 3. 可复算的因果链

### 3.1 shared cache 契约

`liwei_0616_10y_v61` 的 authoritative publisher 是
`liwei_0616_10y01_full_oos_k3_div_k10`。本方案声明自己是 consumer，并将相同
`publisher_consumer_id` 传给 `prepare_phase_a_caches`。

非 publisher 不会训练或发布 cache。它会将当前 generation 与本次输入状态比较；若 spec、baseline
集合或有效输入不等价，代码唯一的拒绝为：

```text
CACHE_PUBLISHER_REQUIRED:
liwei_0616_10y01_cons_say_k3_div_k10 requires publisher refresh
```

### 3.2 事故日输入状态与 cache generation

对现场 artifact 以 cache 的 `_input_generation_state` 和
`_consumer_input_states_equivalent` 口径做纯只读重算，结果为：

| generation / 输入 | 创建时间（Asia/Shanghai） | daily bound | input content ID | 与 8/5 consumer artifact 等价 |
|---|---:|---|---|---|
| 前一 current `generation-f6cd…e11a5521` | 2026-08-04 07:11:52 | 2026-08-03 | `bf2c035d…6104ff` | 否 |
| 8/5 consumer artifact | — | 2026-08-04 | `ae942156…7061c3` | — |
| publisher 新 generation `generation-8575…8f148b7f` | 2026-08-05 07:50:40 | 2026-08-04 | `ae942156…7061c3` | 是 |

新 generation 的 parent 正是前一 `generation-f6cd…e11a5521`。这说明在 publisher 完成前，
consumer 所需的 8 月 4 日输入与 current 的 8 月 3 日输入严格不等价；而 publisher 随后确实产出了
consumer 所需的精确状态。

### 3.3 事故前执行顺序

事故后提交 `d440091`（2026-08-05 10:45:46 +08:00，
`fix(launchd): preserve cache ordering and gray exclusions`）首次增加
`_cache_publishers_first`，并新增了以本 consumer/publisher 为对象的
`test_native_cache_publishers_run_before_consumers`。

该提交的父版本没有重排 `admitted_candidates`，而是直接逐项执行。严格 discovery 对 config 路径排序；
只读重放当前未改动的 scheme 目录顺序得到：

```text
index 5: liwei_0616_10y01_cons_say_k3_div_k10
index 6: liwei_0616_10y01_full_oos_k3_div_k10
```

因此 2026-08-05 的 pre-fix runner 先运行 consumer、后运行 publisher。静态
`daily_scheduler_policy_v2.json` 也按这个顺序列出二者，并把 consumer 标为
`cache_prerequisite=false`、publisher 标为 `cache_prerequisite=true`；该文件不是本次
因果判断所依赖的实际排序器，只是相同依赖关系的旁证。

次日的 launchd receipt 进一步提供回归旁证：2026-08-06 publisher 的 `run_id=2167` 先于 consumer 的
`run_id=2179`，consumer 成功。这与 publisher-first 修复一致；该相关性不单独作为根因证明。

## 4. 排除的分类

| 分类 | 结论 | 依据 |
|---|---|---|
| 输入 / cutoff | 排除为主因 | 8/5 三类 artifact 均已生成，daily 精确截止 8/4；重算状态精确匹配随后 publisher 的 generation。 |
| 算法子进程 | 排除为主因 | 未改动的 scheme 在 publisher-first 后成功；失败发生在算法核心输出前的 cache consumer 保护分支。 |
| 业务契约 | 排除 | exact version activation 与 `10Y/h5` active Registry 均通过，日期语义正确。 |
| repository / 写入 | 排除 | 批次中 publisher 及同类任务可成功写入；证据链在 prediction 完成前即确定为 cache 拒绝，而非 DB completion 错误。 |
| 外部依赖 | 排除 | 输入 artifact、日历读取和 publisher 同批刷新均成功；没有对应的网络、DB 或数据服务失败证据。 |
| 控制面 | **确认** | pre-fix stable 顺序违反 publisher → consumer 依赖；这个顺序在输入状态前移时必触发 consumer 的 fail-closed 拒绝。 |

## 5. 影响、边界与后续授权

- 影响仅限一个 future T+5 key：本 base scheme / `10Y` / `h5` / feature
  `2026-08-04` / target `2026-08-11` 的 2026-08-05 scheduled-live 输出未成功。
- 本诊断没有写入预测、run、Harness receipt、Registry、缓存、配置或任何服务/launchd 状态。
- 若要决定是否补齐该 key，必须另起精确 scope：先只读冻结该业务键及现状，再由用户单独授权适用的
  recovery/写入路径。不得重用 G3.1 的 token、admission、provenance 或 `signal-gap-fill` receipt。
- 若未来需要审计原始 exception 文本，应在获准的受控只读 run-log 查询路径中读取；在此之前不应将
  本文的因果重建改写成未经读取的数据库 error string。

## 6. 验证结果

以下均在隔离 worktree 中执行；没有运行生产 runner：

| 命令 | 结果 |
|---|---|
| `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_onboarding_docs` | 47 tests，`OK` |
| `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_native_generation_liwei` | 1 test，`OK` |
| `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_launchd_prediction_runner` | 12 tests，`OK`；包含 publisher 先于 consumer 的相关测试 |
| `git diff --check` | exit 0，无输出 |
