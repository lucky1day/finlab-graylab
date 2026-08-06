# Liwei 10Y_01 2026-08-11 T+5 单次失败诊断（2026-08-06）

**诊断状态：** `DONE_WITH_CONCERNS`（只读；未修复、未补数）

**当前最强分类（高置信因果推断，非已读取 root cause）：** `CONTROL_PLANE` —— repository 中事故前的
launchd one-shot 排序会让共享 Phase A cache 的 consumer 先于 publisher 执行；在本文已重算的
8 月 5 日输入状态下，这一顺序满足 `CACHE_PUBLISHER_REQUIRED` 拒绝分支的充分条件。

**证据边界：** launchd 摘要只输出 `execution_failed`，没有子进程原始异常文本；本任务也禁止手工 SQL，
故没有读取该 failed run 的 `t_scheme_run_log.error_msg` 或推定其 run ID。现场 artifact 的等价性重算、
manifest 谱系和事故前 runner 源码共同支持控制面排序是**最强解释**，但不能证明该失败执行实际走到了
该 cache 分支，或绝对排除算法子进程、repository/写入、外部依赖的另一错误。读取原始 error message
需要独立、受控的只读授权。

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

## 3. 可复算的条件因果链（非已读取错误日志的 root cause）

### 3.1 shared cache 契约

`liwei_0616_10y_v61` 的 authoritative publisher 是
`liwei_0616_10y01_full_oos_k3_div_k10`。本方案声明自己是 consumer，并将相同
`publisher_consumer_id` 传给 `prepare_phase_a_caches`。

非 publisher 不会训练或发布 cache。它会将当前 generation 与本次输入状态比较；若 spec、baseline
集合或有效输入不等价，源码会进入以下拒绝分支：

```text
CACHE_PUBLISHER_REQUIRED:
liwei_0616_10y01_cons_say_k3_div_k10 requires publisher refresh
```

这证明的是条件命题：**若** failed 子进程以重算后的 8/5 consumer 输入面对前一 current generation
执行该分支，**则**它必定得到上述 cache-publisher 错误。因为未读取事故行的原始 error message，
本文不把这个条件命题表述为已观测到的异常文本。

### 3.2 事故日输入状态与 cache generation

对现场 artifact 以 cache 的 `_input_generation_state` 和
`_consumer_input_states_equivalent` 口径做纯只读重算，结果为：

| generation / 输入 | 创建时间（Asia/Shanghai） | daily bound | input content ID | 与 8/5 consumer artifact 等价 |
|---|---:|---|---|---|
| 前一 current `generation-f6cd37e29ff1303f9cd0aedf-e11a5521` | 2026-08-04 07:11:52 | 2026-08-03 | `bf2c035de7d517bc8c92ef0161a2ccd9b9bcb3348a847efe7228e743763104ff` | 否 |
| 8/5 consumer artifact | — | 2026-08-04 | `ae94215664732de875d5eb45466940f6527ffb348337134bc7e715a3da7061c3` | — |
| publisher 新 generation `generation-8575dd3cd3c902915473f61e-8f148b7f` | 2026-08-05 07:50:40 | 2026-08-04 | `ae94215664732de875d5eb45466940f6527ffb348337134bc7e715a3da7061c3` | 是 |

新 generation 的 manifest parent 是前一 `generation-f6cd37e29ff1303f9cd0aedf-e11a5521`。这建立了
cache 状态转移和输入等价性的强证据；它不能单独替代事故行原始异常的读取。

### 3.3 无敏感、可复现的现场 artifact 等价性重算

该重算读取的路径类别只有：

- `backtest_artifacts/runtime_inputs/<scheme_id>/{daily,weekly,monthly}_output_2026-08-05.csv`；
- `backtest_artifacts/runtime_cache/liwei_0616/liwei_0616_10y_v61/10y/generations/<generation-id>/manifest.json`。

它调用的 repo 函数是 `predict._date_to_week_map`、`build_auxiliary_dependency_projection`、
`_input_generation_state`、`_consumer_input_states_equivalent` 和
`consumer_input_state_equivalence_sha256`。唯一 DB 接触是既有 `get_calendar(engine)` 的只读日历读取；
不使用 raw SQL、不调用 cache prepare/publish、算法 subprocess 或任何 repository 写入，也不输出
DSN、token、凭据、原始日志或行情行。

在保留上述 operational artifacts 的受控 checkout 根目录中，以服务环境执行下列 recipe。`Path.cwd()`
就是该 operational root，不需要将其绝对路径或环境变量内容写入输出：

```text
conda run --no-capture-output -n bond_factor_lab_service python -
```

将下列脚本通过标准输入传给该命令即可；它只向 stdout 输出最后的无敏感 JSON。

```python
import json
from pathlib import Path

import pandas as pd

from scheduler.repository import create_engine_from_env
from shared.calendar_service import get_calendar
from shared.liwei_0616_cache_projection import build_auxiliary_dependency_projection
from shared.liwei_0616_phase_a_cache import (
    _consumer_input_states_equivalent,
    _input_generation_state,
    consumer_input_state_equivalence_sha256,
)
from schemes.liwei_0616_10y01_cons_say_k3_div_k10 import predict
from schemes.liwei_0616_10y01_cons_say_k3_div_k10.core import (
    data_alignment,
    v31_common,
)

scheme_id = "liwei_0616_10y01_cons_say_k3_div_k10"
operational_root = Path.cwd()
inputs = operational_root / "backtest_artifacts" / "runtime_inputs" / scheme_id
generations = (
    operational_root / "backtest_artifacts" / "runtime_cache" / "liwei_0616"
    / "liwei_0616_10y_v61" / "10y" / "generations"
)
daily = pd.read_csv(inputs / "daily_output_2026-08-05.csv", encoding="utf-8-sig")
weekly = pd.read_csv(inputs / "weekly_output_2026-08-05.csv", encoding="utf-8-sig")
monthly = pd.read_csv(inputs / "monthly_output_2026-08-05.csv", encoding="utf-8-sig")
engine = create_engine_from_env()
try:
    date_to_week = predict._date_to_week_map(daily, get_calendar(engine))
finally:
    engine.dispose()
projection = build_auxiliary_dependency_projection(
    daily_df=daily,
    weekly_df=weekly,
    monthly_df=monthly,
    date_to_week=date_to_week,
    prepare_model_frames=v31_common.prepare_model_frames,
    build_wkmo_features=v31_common.build_wkmo_features,
    proof_files=(Path(v31_common.__file__), Path(data_alignment.__file__)),
)
artifact = _input_generation_state(
    daily_df=daily,
    weekly_df=weekly,
    monthly_df=monthly,
    auxiliary_dependency_projection=projection,
    native_generation_binding=None,
)
def state(generation_id):
    return json.loads(
        (generations / generation_id / "manifest.json").read_text(encoding="utf-8")
    )["input_state"]
prior = state("generation-f6cd37e29ff1303f9cd0aedf-e11a5521")
publisher = state("generation-8575dd3cd3c902915473f61e-8f148b7f")
result = {
    "artifact_content_id": artifact["content_id"],
    "publisher_20260805_content_id": publisher["content_id"],
    "prior_content_id": prior["content_id"],
    "artifact_equals_publisher_20260805": _consumer_input_states_equivalent(
        publisher, artifact
    ),
    "artifact_equals_prior": _consumer_input_states_equivalent(prior, artifact),
    "artifact_equivalence_sha256": consumer_input_state_equivalence_sha256(artifact),
    "publisher_20260805_equivalence_sha256": (
        consumer_input_state_equivalence_sha256(publisher)
    ),
    "prior_equivalence_sha256": consumer_input_state_equivalence_sha256(prior),
}
print(json.dumps(result, sort_keys=True))
```

2026-08-06 的无敏感结构化结果如下；完整 SHA-256 是可复核的等价摘要，而非凭据：

```json
{
  "artifact_content_id": "ae94215664732de875d5eb45466940f6527ffb348337134bc7e715a3da7061c3",
  "artifact_equals_prior": false,
  "artifact_equals_publisher_20260805": true,
  "artifact_equivalence_sha256": "9fcdaf01c13ed4cb9c33414e0f14386b811cc4e83f92cc01d3fd68329c63ee35",
  "prior_content_id": "bf2c035de7d517bc8c92ef0161a2ccd9b9bcb3348a847efe7228e743763104ff",
  "prior_equivalence_sha256": "1220ddf943aadf4fb5cf98e9058c3f4270fe1cbb798cd5f0fff099397b753013",
  "publisher_20260805_content_id": "ae94215664732de875d5eb45466940f6527ffb348337134bc7e715a3da7061c3",
  "publisher_20260805_equivalence_sha256": "9fcdaf01c13ed4cb9c33414e0f14386b811cc4e83f92cc01d3fd68329c63ee35"
}
```

### 3.4 事故前执行顺序

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

这重建了 repository 中 2026-08-05 pre-fix runner 的 consumer→publisher 顺序。静态
`daily_scheduler_policy_v2.json` 也按这个顺序列出二者，并把 consumer 标为
`cache_prerequisite=false`、publisher 标为 `cache_prerequisite=true`；该文件不是本次
因果判断所依赖的实际排序器，只是相同依赖关系的旁证。

次日的 launchd receipt 进一步提供回归旁证：2026-08-06 publisher 的 `run_id=2167` 先于 consumer 的
`run_id=2179`，consumer 成功。这与 publisher-first 修复一致；该相关性不单独作为根因证明。
本任务没有检查 installed plist 或进程源码路径，因此 repository 排序重建不能替代对事故进程二进制/
源码 provenance 的现场读取。

## 4. 分类比较与证据边界

| 分类 | 结论 | 依据 |
|---|---|---|
| 输入 / cutoff | 非首选解释；未绝对排除 | artifact 已生成且 daily cutoff 为 8/4；重算状态匹配随后 publisher generation。原始失败日志未读，不能据此排除单个数据列/子进程错误。 |
| 算法子进程 | 未见支持证据；未绝对排除 | scheme 内容未变且 publisher-first 后成功，但没有读取该次子进程 stderr 或 error message。 |
| 业务契约 | 不像首因；未绝对排除 | 当前 activation、active `10Y/h5` Registry 和日期语义均正确；未读取事故时的完整 run receipt。 |
| repository / 写入 | 未见支持证据；未绝对排除 | publisher 与同批任务可成功写入，但没有读取 failed run 的 completion/error 记录。 |
| 外部依赖 | 未见支持证据；未绝对排除 | 输入 artifact、日历读取和 publisher 刷新成功；未读取原始异常，无法绝对排除瞬态依赖错误。 |
| 控制面 | **最高置信推断，尚未确证** | pre-fix stable 顺序满足 consumer 先于 publisher，且现场 artifact 状态会令 cache guard 拒绝旧 generation；原始 error message 是确证所需的缺口。 |

## 5. 影响、边界与后续授权

- 已观察到的直接影响是一个 future T+5 key：本 base scheme / `10Y` / `h5` / feature
  `2026-08-04` / target `2026-08-11` 的 2026-08-05 scheduled-live 输出未成功；未做原始 run-log
  查询前，不将“仅限”写成对所有连带影响的绝对断言。
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
| `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_native_generation_liwei` | 1 test，`OK`；验证 mocked/frozen generation 合同，不验证现场 artifact 重算。 |
| `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_launchd_prediction_runner` | 12 tests，`OK`；包含 publisher 先于 consumer 的 mock runner 测试，不验证历史 failed run 的原始异常。 |
| `git diff --check` | exit 0，无输出 |
