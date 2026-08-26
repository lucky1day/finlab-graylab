# 双运行时共享方案契约

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：平台开发、入库和审计人员
本文只定义两种运行时共享的身份、日期、结果、生命周期和分派边界。运行时专属契约分别由 [Native V1 存量契约](../native_v1/SCHEME_CONTRACT.md)和 [Blackbox V2 Contract 1.0](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)定义。

> 后续新增算法、新方案 ID、新目标、新任务和替代版本一律使用 `blackbox_v2`。Native V1 仅维护政策清单中的既有方案。

## 1. 版本维度

以下名称不能混用：

| 名称 | 含义 |
|---|---|
| Native V1 / Blackbox V2 | 平台运行时代际 |
| `schema_version=1.0` | Blackbox 上游接口合同版本 |
| `data-bridge-v1` | 四文件 DataBridge Schema |
| `blackbox-v2-v1` | Blackbox 隔离执行 Runtime Profile |
| `policy_version=1.0` | 新旧运行时入库政策清单版本 |

## 2. 显式运行类型

每个方案必须解析为明确的 `runtime_type`，不得根据是否存在 `predict.py` 隐式猜测新方案类型。

| runtime_type | 目录形态 | 使用范围 | 执行入口 |
|---|---|---|---|
| `native_adapter` | `config.yaml + predict.py + core/` | 版本化白名单内的存量方案维护 | import `predict.run()` |
| `blackbox_v2` | `config.yaml + delivery/{scheme_id}.py/.json` | 所有后续新增和替代方案 | 隔离子进程 CLI |

统一入口和判断规则见[方案入库导航](../onboarding/README.md)。

## 3. 方案身份

身份分为两层：

- `base_scheme_id`：算法执行身份。Native 使用目录名/`config.scheme_id`；Blackbox 使用 Metadata `scheme_id`。
- Registry `scheme_id`：前端和业务身份，固定为 `{base_scheme_id}__h{horizon}__{target_tenor}`。

单标的和多标的均使用 composite Registry ID。预测、运行和回测底表继续保存 base `scheme_id`，并通过 `target_tenor` 区分目标。

同一算法的 Native 与 Blackbox 实现必须使用不同 base ID；替代试验不得覆盖既有 Native 身份或历史结果。

## 4. 任务类型与期限

平台任务格子只由 `target_tenor + task_type` 决定，不得由 `frequency/horizon` 猜测。

当前业务期限白名单为 `1Y/3Y/5Y/7Y/10Y`；`1Y` 与其他期限一样是可展示、可注册的正式目标。运行时仍须校验目标已在平台 target registry 中登记。

`task_type` 固定为：

- `T+1`
- `T+5`
- `weekly_point`
- `weekly_average`
- `monthly`
- `monthly_average`
- `quarterly_average`
- `annual_average`

Blackbox Contract 1.0 的对应 horizon 固定为 `1/5/1/1/1/1/1/1`。周均、MID 月均、自然季均和春节年均的 `horizon=1` 都表示下一个同类业务桶；不得把周期均值改成 `30/90/365`，也不得通过 horizon 推断任务、桶边界或目标日期。Native V1 的历史周/月 `6/30` 仅用于存量兼容，不得作为新方案模板。

## 5. 日期语义

所有运行时统一使用：

- `predict_date`：信号发出日或回测站位日。
- `feature_date`：输入数据硬截止日。
- `target_date`：目标验证日和 actual join 日期。

Blackbox 还由平台提供与三频快照真实存在的 `daily_cutoff`、`weekly_cutoff`、`monthly_cutoff`。算法只校验、使用和逐 Request 截断，不得自行推导日期或周/月键。

完整规则以[预测日期与实盘语义](PREDICTION_SEMANTICS.md)为准。

### 5.1 Blackbox 输入

Blackbox 的输入契约是：

```text
DataBridge generation 四文件 + 平台 Request
```

每个 DataBridge generation 固定包含 `daily_output.csv`、
`weekly_output.csv`、`monthly_output.csv` 和 `api_wind_date.csv`。
平台在 generation 生成时一次性校验、摘要并封存四份文件；每个方案
看到相同的只读 `--data-dir` 结构，按需读取，不再声明或捕获方案级
输入。算法不得读取交付目录旁的同名文件或自行访问数据库。

## 6. 标准结果

两种运行时最终都转换为 `shared.models.PredictionRecord`，至少承载：

- base `scheme_id`
- `predict_date`、`feature_date`、`target_date`
- `target_tenor`、`horizon`
- `predicted_direction`，取值 `-1/0/1`
- 可审计的模型、输入快照和运行上下文

Blackbox 上游结果文件本身只包含 Contract 1.0 的五个字段；平台校验成功后结合 Metadata 和运行上下文完成转换。异常、缺数或低置信度不得伪装成方向 `0`。

Blackbox `PredictionRecord.extra.data_snapshot_id` 直接使用包含四份文件
的 generation Snapshot identity；来源由 DataBridge generation manifest
统一追溯，不再生成方案级组合身份或平台输入审计 manifest。

从 `PredictionRecord` 开始，Registry、actual join、指标、落库、API 和前端不再区分运行时。

## 7. 生命周期

Blackbox 不使用 `validated` 或 `shadow` 中间状态。Intake 生成 `paused/draft` canonical 身份；完整持久化回测只产生 immutable 证据，不改变生命周期；取得独立授权后，`activate` 在一个命令内建立 draft version/paused Registry 并原子切换为 `active/active`。

- 技术验证通过不等于业务激活、现场发布或生产调度授权。
- 具体 Blackbox 方案只有完成[生产晋级条件](../blackbox_v2/PRODUCTION_READINESS.md)核验并取得对应独立授权后，才可执行持久化回测、activation 或单日 `signal-gap-fill`；正式 `scheduled_live` 只由目标主机 one-shot 调度触发，任何授权不得外推到其他方案。
- Native V1 保持既有状态；维护操作不得借机改变 Registry、scheduler 或 API 可见性。

历史回测与灰度实盘使用同一冻结 exact version 和输入 lineage，但分别执行一次历史 batch 与一次 live-safe target 区间 batch。灰度区间不得逐日期重复启动算法；也不得复制数据库身份字段、覆盖 live 业务键，或把不满足逐 Request cutoff 的 source-original batch 伪装为实盘。完整条件见[预测日期语义](PREDICTION_SEMANTICS.md#52-历史批次与灰度区间批次)。

## 8. Harness 分派

入库命令按运行时分离：

```bash
# Native V1
python -m harness onboard {scheme_id} --predict-date YYYY-MM-DD --stage all

# Blackbox V2
python -m harness intake-blackbox --delivery-dir <two-file-dir>
python -m harness gate backtest --scheme-id {scheme_id} --predict-date YYYY-MM-DD --persist
python -m harness activate --scheme-id {scheme_id}
```

Native `all` 为 `static -> dry-run -> compare -> backtest`；DryRunGate 同时核验真实执行生成的输入 artifact 合同。Blackbox 不进入 `onboard`；Intake 定义静态平台边界，持久化回测在执行前复验脚本安全边界并负责真实批量执行和 Result 合同；activate 只严格加载 canonical 身份并匹配 exact-version 回测证据。

Native ActivationGate 的两条 profile 互斥：当前 exact version 已通过完整 `all` 时，采用 `full_initial_onboarding_v1`，只复核当前四个 Gate（含 Compare）和本次直接 activation 命令，不要求 prior snapshot 或 `native-maintenance`。只有未走该 full-`all` profile 的已有 Native V1 修订，在 prior `all` 的 `static.business_identity` 已持久化且与当前业务身份精确匹配时，才可改走 `native-maintenance`：`static -> native-maintenance-admission -> dry-run`。快照只含 `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、target tenors 和 composite Registry IDs，不含代码/config/version hash。maintenance profile 还要求 current exact `t_scheme_versions` 为 native `draft|active`、expected Registry 全 paused（预激活）或全 active（激活后）、draft+active fail-closed、prior Native version 的 passed `all + compare`、当前精确 version 的三段持久证据和独立 activation 命令；只有 ActivationGate 能原子建立 active。prior snapshot 缺失、重复、损坏或不匹配时一律 fail-closed。maintenance 不运行当前 historical `compare/backtest`，也不写业务表。满足任一标准 profile 的同一身份修订，其历史 source-benchmark 输入 vintage 漂移只归档，不单独阻断 activation、gap repair、`gray_live`、`scheduled_live` 或 Dashboard；新 Native 身份仍只能走 Native `all`，Blackbox V2 走 Intake、完整持久化回测和 activate。

- Native：校验 adapter/core、输入 artifact 和 source fidelity。
- Blackbox：Intake 和持久化 backtest 执行两文件安全校验；backtest 另校验 CLI、四文件快照和标准结果，并保存脚本校验策略摘要；activate 只匹配 canonical exact version 与当前校验策略的成功回测证据；确定性与截止隔离属上游义务。
- activate、lifecycle reconcile 与单日 `signal-gap-fill` 使用各自专用命令；不存在 Blackbox shadow 命令；`scheduled_live` 只由宿主 one-shot 触发。

## 9. 责任边界

| 事项 | Native V1 | Blackbox V2 |
|---|---|---|
| 算法内部保真 | full-`all` profile 检查 core 和内部 benchmark；maintenance profile 保留 prior admission 的 `static.business_identity` 快照与 live-safe 证据；缺快照只能走 full `all` | 上游负责；平台不反编译或改写脚本 |
| 输入 | `shared.input_artifacts` 注入 | DataBridge generation 四文件 + 平台 Request |
| 结果验收 | `PredictionRecord` 与 source evidence | Result 合同（确定性与截止隔离由上游保证） |
| 新身份 | 禁止 | 唯一允许路径 |
| 业务写入 | 受授权 repository | 默认禁止；生产准备通过并取得专项授权后由专用 Gate 执行 |

任何运行时都必须遵守输入单点、写库单点、失败不生成业务信号和授权 fail-closed 原则。
