# Native V1 存量方案契约

**文档状态**：`LEGACY_MAINTENANCE`
**适用运行时**：`native_adapter`
**目标读者**：维护既有 Native V1 方案的平台工程师
**最后核验日期**：2026-07-19

> 本契约只适用于 `deploy/onboarding_policy_v1.json` 登记的存量方案，禁止用于新增方案。共享身份、日期和结果语义以[共享方案契约](../architecture/SCHEME_CONTRACT.md)为准。

## 1. 目录契约

既有方案保持原目录，不迁移、不重命名：

```text
schemes/{scheme_id}/
├── config.yaml
├── predict.py
└── core/
```

- `scheme_id` 必须等于目录名、`config.scheme_id`、`predict.py::SCHEME_ID` 和运行结果中的 base `scheme_id`。
- `runtime_type` 必须为 `native_adapter`；历史配置缺省时发现器可按兼容规则解释，但维护时不得改成 Blackbox 身份。
- 原有 Registry composite ID、数据库记录、scheduler 任务和历史结果保持不变。

## 2. config.yaml

维护时必须保留机器契约要求的字段：

| 字段 | 约束 |
|---|---|
| `scheme_id` | `^[a-z][a-z0-9_]*$`，且在 Native 存量白名单中 |
| `runtime_type` | `native_adapter` |
| `name`、`description` | 非空 |
| `task_type` | `T+1`、`T+5`、`weekly_point`、`weekly_average`、`monthly` |
| `horizon` | 保留既有方案口径；历史周/月方案可继续使用 `6/30` |
| `tenors` | 非空且只包含平台已登记目标 |
| `frequency` | `daily`、`weekly` 或 `monthly` |
| `input_source` | 既有方案缺省按 `legacy_db` 处理 |
| `schedule` | 保留既有 cron、时区和超时语义 |
| `status` | `active` 或 `paused`；维护不得擅自改变业务状态 |
| `input_spec` | 与 `shared.input_artifacts` 产出的 source、版本和必需列一致 |

具体字段由 `shared/scheme_config_schema.py` 判定，文档不得替代机器校验。

## 3. predict.py

`predict.py` 必须继续暴露：

```python
SCHEME_ID = "<scheme_id>"

def run(predict_date: str) -> list[PredictionRecord]:
    ...
```

Adapter 只负责平台输入、日期上下文、算法调用和结果映射：

- 输入只能来自 `shared.input_artifacts`。
- 不得直接访问数据库、写库或调用其他方案。
- 不得吞掉异常并生成方向信号。
- 返回 target 集合必须与当前有效 Registry target 集合一致。

## 4. core/

活跃 `core/` 必须保持：

- DataFrame 或明确数据对象输入，算法结果对象输出。
- 零数据库、零写库、零网络、零跨方案 import。
- source-backed 算法保持时间起点、窗口、特征顺序、模型参数、投票/fallback 和内部 score 映射。
- `legacy_*.py` 只可作为证据归档，活跃模块不得依赖。

改动分级遵循[源算法保真规范](../architecture/SOURCE_ALGORITHM_FIDELITY.md)：L0 可维护；L1 必须逐项举证；L2 默认禁止并应改走独立 Blackbox V2 trial。

## 5. 结果与副作用

`PredictionRecord`、三日期、`prediction_phase` 和 composite Registry 身份遵循[共享方案契约](../architecture/SCHEME_CONTRACT.md)与[预测语义](../architecture/PREDICTION_SEMANTICS.md)。

只有 `scheduler.repository`、`backtests.repository` 和 actual updater 可以写库。自动 Gate 不得写预测、回测等业务表；任何 persist、live 或状态变化仍需受控授权。

## 6. 机器门禁

- `StaticGate` 和 `ActivationGate` 均读取 `deploy/onboarding_policy_v1.json`。
- 未登记的 `native_adapter` ID 必须 fail-closed。
- 修改白名单不是普通入库步骤，不得用于绕过 Blackbox V2。
- 已登记方案仍需通过[Native V1 存量维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)规定的 Gate 和证据核验。
