# 双运行时共享方案契约

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：平台开发、入库和审计人员
**最后核验日期**：2026-07-19

本文只定义两种运行时共享的身份、日期、结果、生命周期和分派边界。运行时专属契约分别由 [Native V1 存量契约](native_v1/SCHEME_CONTRACT.md)和 [Blackbox V2 Contract 1.0](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)定义。

> 后续新增算法、新方案 ID、新目标、新任务和替代版本一律使用 `blackbox_v2`。Native V1 仅维护政策清单中的既有方案。

## 1. 版本维度

以下名称不能混用：

| 名称 | 含义 |
|---|---|
| Native V1 / Blackbox V2 | 平台运行时代际 |
| `schema_version=1.0` | Blackbox 上游接口合同版本 |
| `data-bridge-v1` | 三频 CSV 数据 Schema |
| `blackbox-v2-v1` | Blackbox 隔离执行 Runtime Profile |
| `policy_version=1.0` | 新旧运行时入库政策清单版本 |

## 2. 显式运行类型

每个方案必须解析为明确的 `runtime_type`，不得根据是否存在 `predict.py` 隐式猜测新方案类型。

| runtime_type | 目录形态 | 使用范围 | 执行入口 |
|---|---|---|---|
| `native_adapter` | `config.yaml + predict.py + core/` | 白名单内 29 个存量方案维护 | import `predict.run()` |
| `blackbox_v2` | `config.yaml + delivery/{scheme_id}.py/.json` | 所有后续新增和替代方案 | 隔离子进程 CLI |

统一入口和判断规则见[方案入库导航](onboarding/README.md)。

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

Blackbox Contract 1.0 的对应 horizon 固定为 `1/5/1/1/1`。Native V1 的历史周/月 `6/30` 仅用于存量兼容，不得作为新方案模板。

## 5. 日期语义

所有运行时统一使用：

- `predict_date`：信号发出日或回测站位日。
- `feature_date`：输入数据硬截止日。
- `target_date`：目标验证日和 actual join 日期。

Blackbox 还由平台提供与三频快照真实存在的 `daily_cutoff`、`weekly_cutoff`、`monthly_cutoff`。算法只校验、使用和逐 Request 截断，不得自行推导日期或周/月键。

完整规则以[预测日期与实盘语义](PREDICTION_SEMANTICS.md)为准。

## 6. 标准结果

两种运行时最终都转换为 `shared.models.PredictionRecord`，至少承载：

- base `scheme_id`
- `predict_date`、`feature_date`、`target_date`
- `target_tenor`、`horizon`
- `predicted_direction`，取值 `-1/0/1`
- 可审计的模型、输入快照和运行上下文

Blackbox 上游结果文件本身只包含 Contract 1.0 的五个字段；平台校验成功后结合 Metadata 和运行上下文完成转换。异常、缺数或低置信度不得伪装成方向 `0`。

从 `PredictionRecord` 开始，Registry、actual join、指标、落库、API 和前端不再区分运行时。

## 7. 生命周期

统一生命周期为：

```text
draft -> validated -> shadow -> active -> paused -> retired
```

- 自动 Gate 通过只代表技术验证，不等于业务激活。
- 当前 Blackbox V2 正式能力止于 `shadow + paused`。
- 在[生产晋级条件](blackbox_v2/PRODUCTION_READINESS.md)完成并形成独立 CURRENT SOP 前，不得执行或宣称 Blackbox active/live。
- Native V1 保持既有状态；维护操作不得借机改变 Registry、scheduler 或 API 可见性。

## 8. Harness 分派

统一命令由 `runtime_type` 选择 Gate 实现：

```bash
python -m harness onboard {scheme_id} --predict-date YYYY-MM-DD --stage all
```

自动阶段保持 `static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness`。Gate 证据的含义和副作用边界以[Harness 架构](HARNESS_ARCHITECTURE.md)为准。

- Native：校验 adapter/core、输入 artifact 和 source fidelity。
- Blackbox：校验两文件、CLI、三频快照、确定性、截止隔离和标准结果。
- persist、shadow、activate 或 live 都不包含在无授权自动段中。

## 9. 责任边界

| 事项 | Native V1 | Blackbox V2 |
|---|---|---|
| 算法内部保真 | 平台可检查 core 和内部 benchmark | 上游负责；平台不反编译或改写脚本 |
| 输入 | `shared.input_artifacts` 注入 | 同代三频只读快照加平台 Request |
| 结果验收 | `PredictionRecord` 与 source evidence | Result 合同、确定性和截止隔离 |
| 新身份 | 禁止 | 唯一允许路径 |
| 业务写入 | 受授权 repository | 当前禁止，最多 shadow + paused |

任何运行时都必须遵守输入单点、写库单点、失败不生成业务信号和授权 fail-closed 原则。
