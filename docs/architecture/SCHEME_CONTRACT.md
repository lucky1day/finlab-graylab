# 双运行时共享方案契约

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：平台开发、入库和审计人员
本文定义两种运行时共享的身份、版本、任务、平台记录与可见性边界。运行时专属契约分别由 [Native V1 存量契约](../native_v1/SCHEME_CONTRACT.md)和 [Blackbox V2 Contract 1.0](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)定义。

> 后续新增与版本修订一律使用 `blackbox_v2`。Native V1 仅承载 Mac3 W4 九套现有版本运行，范围以[根规范](../../AGENTS.md#算法与数据不变量)为准。当前 canonical 和部署范围见[当前状态](../CURRENT_STATUS.md)。

## 1. 版本维度

以下名称不能混用：

| 名称 | 含义 |
|---|---|
| Native V1 / Blackbox V2 | 平台运行时代际 |
| `schema_version=1.0` | Blackbox 上游接口合同版本 |
| `data-bridge-v1` | 五文件 DataBridge Schema |
| `blackbox-v2-v1` | Blackbox 隔离执行 Runtime Profile |
| `policy_version=1.0` | 新旧运行时入库政策清单版本 |

## 2. 显式运行类型

每个方案必须解析为明确的 `runtime_type`，不得根据是否存在 `predict.py` 隐式猜测新方案类型。

| runtime_type | 目录形态 | 使用范围 | 执行入口 |
|---|---|---|---|
| `native_adapter` | `config.yaml + predict.py + core/` | W4 九套固定版本运行 | import `predict.run()` |
| `blackbox_v2` | `config.yaml + delivery/{scheme_id}.py/.json` | 所有后续新增和替代方案 | 隔离子进程 CLI |

统一入口和判断规则见[方案入库导航](../onboarding/README.md)。

## 3. 方案身份

身份分为两层：

- `base_scheme_id`：算法执行身份。Native 使用目录名/`config.scheme_id`；Blackbox 使用 Metadata `scheme_id`。
- Registry `scheme_id`：前端和业务身份，固定为 `{base_scheme_id}__h{horizon}__{target_tenor}`。

单标的和多标的均使用 composite Registry ID。预测、运行和回测底表继续保存 base `scheme_id`，并通过 `target_tenor` 区分目标。

运行时包装升级保持原 base/Registry ID，以新 exact 区分执行版本；迁移与临时身份历史保护见[源算法保真](SOURCE_ALGORITHM_FIDELITY.md)。

`t_scheme_registry.owner` 是方案来源的唯一运行和展示权威，必须为合法非空值。新 Blackbox 从两文件
Metadata 登记 owner；已有 Metadata 缺失 owner 的历史 Blackbox 与 Native 只保留数据库既有值，不改写
canonical 文件或算法版本。Dashboard 不读取仓库映射、配置兜底或占位值；Registry owner 缺失或非法时
整个产品读模型 fail-closed。

## 4. 任务类型与期限

平台任务格子只由 `target_tenor + task_type` 决定，不得由 `frequency/horizon` 猜测。

当前业务期限白名单为 `1Y/3Y/5Y/7Y/10Y`；`1Y` 与其他期限一样是可展示、可注册的正式目标。运行时仍须校验目标已在平台 target registry 中登记。

任务枚举及 `horizon/target_rule/frequency` 固定组合由
[shared.task_specs.TASK_COMBINATIONS](../../shared/task_specs.py)维护；平台配置由
[配置校验器](../../shared/scheme_config_schema.py)验证，上游可读组合表见
[Metadata 合同](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#2-metadata)。任务不是输入频率的别名，
也不能由 horizon 反推：周/月及周期均值 horizon=1 表示下一个同类业务桶，不是下一天。

存量周/月 6/30 只保留已有身份与事实兼容，不作新方案模板。运行时迁移的 Metadata horizon 与
原 Registry/事实 horizon 投影只按[迁移边界](SOURCE_ALGORITHM_FIDELITY.md#7-同算法-native--blackbox-迁移)执行，
日期生成按[预测语义](PREDICTION_SEMANTICS.md)。

## 5. 日期与输入

所有运行时使用 `predict_date`、`feature_date`、`target_date`；完整定义、任务日历、cutoff、分区与 Actual join 由[预测语义](PREDICTION_SEMANTICS.md)维护。算法不得自行推导平台 Request 的日期或周/月键。

Blackbox 读取平台传入的 DataBridge 与 Request；文件和字段合同见[上游交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)，因子版本封版与 legacy 输入兼容见[DataBridge](../blackbox_v2/data_bridge_v1/README.md)。平台输入入口见[代码架构](CODE_ARCHITECTURE.md)。

## 6. 标准结果

两种运行时最终都转换为 [shared.models.PredictionRecord](../../shared/models.py)，身份与业务结果必须包含：

- base `scheme_id`
- `predict_date`、`feature_date`、`target_date`
- `target_tenor`、`horizon`
- `predicted_direction`，取值 `-1/0/1`
- 可审计的模型、输入快照和运行上下文

Blackbox 上游结果文件本身只包含 Contract 1.0 的五个字段；平台校验成功后结合 Metadata 和运行上下文完成转换。异常或缺数不得伪装成成功信号；平台不得自行把低置信度转成方向 `0`。正常完成后的无信号补平原则与各运行时当前实现边界见[保真规则](SOURCE_ALGORITHM_FIDELITY.md#22-正常完成后的无信号补平)。

Blackbox `PredictionRecord.extra.data_snapshot_id` 直接使用包含本方案输入文件
的 generation Snapshot identity；来源由 DataBridge generation manifest
统一追溯，不再生成方案级组合身份或平台输入审计 manifest。

从 `PredictionRecord` 开始，Registry、actual join、指标、落库、API 和前端不再区分运行时。

## 7. 生命周期与分派

canonical 配置、数据库生命周期、部署和自然调度是不同状态，不能互相代替：

- canonical 是部署声明，编辑文件本身不建立或激活数据库身份；数据库生命周期仍由受控操作决定。
- 回测只产生 immutable 验收证据；首次激活在一个事务中建立并激活 exact 与 Registry，不以回测成功或中间 shadow 状态代替 active。
- `paused/archived` Registry 不进入 Dashboard，不允许 trigger 或写入该 target。数据库激活不证明 current 已切换、宿主控制面已挂载或自然运行已完成。
- W4 保留当前 Native 身份与版本，运行保障见[W4 运行手册](../sop/NATIVE_V1_MAINTENANCE_SOP.md)；后续修订不再使用 Native 准入或激活流程。

各命令按[平台 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)核验前置条件并取得对应授权。[生产准备](../blackbox_v2/PRODUCTION_READINESS.md)是接管前的完整证据清单，不是持久化回测的循环前置条件。历史与灰度的不可变性见[预测语义](PREDICTION_SEMANTICS.md)，区间支持范围和单日入口只在平台 SOP 维护。

## 8. 接口验证入口

身份、配置和 discovery 改动按[公共验证矩阵](../onboarding/README.md#可复用测试矩阵)选择配置与 active discovery 合同；
标准输出按对应 Native/Blackbox 合同验证。接口测试不能替代算法保真、当前 exact 入库证据或现场生产观察。
Gate 的实现与审计留证见[Harness 架构](HARNESS_ARCHITECTURE.md)，算法内部与包装迁移责任见
[源算法保真](SOURCE_ALGORITHM_FIDELITY.md)。
