# W4 Native 固定版本运行契约

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`
**目标读者**：维护 W4 运行环境的平台工程师
> 本契约说明 Mac3 W4 九套现有 Native 版本的运行依赖，不是 Native 新版本入库规范。后续版本全部使用 Blackbox；W4 不自动迁移、不部署 ECS。固定版本运行维护见[W4 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)，共享身份、日期和结果语义见[共享方案契约](../architecture/SCHEME_CONTRACT.md)。

## 1. 目录契约

既有方案保持原目录，不迁移、不重命名：

```text
schemes/{scheme_id}/
├── config.yaml
├── predict.py
└── core/
```

- `scheme_id` 必须等于目录名、`config.scheme_id`、`predict.py::SCHEME_ID` 和运行结果中的 base `scheme_id`。
- `runtime_type` 必须为 `native_adapter`；历史配置缺省时发现器可按兼容规则解释，不能据此修改运行时或身份。
- 原有 Registry composite ID、数据库记录、scheduler 任务和历史结果保持不变。

## 2. config.yaml

完整字段类型与枚举由[配置 Schema](../../shared/scheme_config_schema.py)校验；运行发现与身份测试见[验证矩阵](../onboarding/README.md#可复用测试矩阵)。运行核验须保留以下兼容语义，不能因字段值合法就改变现有身份：

- `runtime_type=native_adapter`；历史 `input_source` 缺省解释为 `legacy_db`。
- task、horizon、tenors 和 frequency 保持既有口径；历史周/月 `6/30` 只供存量兼容，不作为新方案模板。
- schedule 保留既有 cron、时区和超时；status 的变化须另有业务授权。
- `input_spec` 与 `shared.input_artifacts` 实際产出的 source、版本和必需列一致。

`backtest.runner` 等历史声明只保留 canonical 字节与 exact 兼容；平台不再加载这些声明或提供 Native 历史重跑入口。

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
- 算法保持[源算法保真](../architecture/SOURCE_ALGORITHM_FIDELITY.md)要求的输入、计算及内部数值口径。
- `legacy_*.py` 只可作为证据归档，活跃模块不得依赖。

本节用于辨别运行依赖和故障边界，不授权修改 adapter/core 形成 Native 新版本。恢复环境或依赖按 W4 SOP 执行。

## 5. 结果与副作用

`PredictionRecord`、三日期、`prediction_phase` 和 composite Registry 身份遵循[共享方案契约](../architecture/SCHEME_CONTRACT.md)与[预测语义](../architecture/PREDICTION_SEMANTICS.md)。

写库单点以[根规范](../../AGENTS.md)为准。已有调度保持原版本与有效 Registry target；受控补缺、服务恢复和状态操作按[W4 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md#4-故障定位与恢复)执行，接口合法不授予操作权限。
