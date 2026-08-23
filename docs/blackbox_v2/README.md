# Blackbox V2 文档入口

**文档状态**：`CURRENT`

**目标读者**：平台入库、开发和审计人员

**最后核验日期**：2026-08-23

Blackbox V2 是所有新算法、新方案 ID、新目标、新任务和替代版本的唯一入库运行时。场景判断从[方案入库导航](../onboarding/README.md)进入。

## 当前入口

| 内容 | 权威文档 |
|---|---|
| 上游两文件交付与自验 | [上游交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) |
| 平台 Intake、Gate、激活与恢复 | [平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md) |
| 执行、输入和结果边界 | [平台架构](../architecture/BLACKBOX_V2_PLATFORM.md) |
| 通用生产条件 | [生产晋级条件](PRODUCTION_READINESS.md) |
| 一次性 batch 的历史/gray 分区复用 | [平台入库 SOP 6.5](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#65-一次性批量结果复用快路径) |
| DataBridge 结构说明 | [DataBridge V1](data_bridge_v1/README.md) |
| 当前平台状态 | [当前状态](../CURRENT_STATUS.md) |
| 未关闭方案问题 | [全方案问题台账](../records/SCHEME_ISSUE_LEDGER.md) |

## 证据边界

- 精确 version、generation、snapshot、Harness run、Gate 结果和授权证据由控制面数据库及本机 ignored reports 保存，不提交到 `docs/`。
- 已完成的单方案入库过程、截图、JSON 和时点审计不在工作树维护副本；需要追溯时使用 Git 历史和控制面记录。
- `docs/CURRENT_STATUS.md` 只保存当前有效摘要，`docs/records/SCHEME_ISSUE_LEDGER.md` 只保存未关闭问题。
- 试验或历史记录不能改变机器契约、现行 SOP 或生产调度治理。

## 数据文件边界

| 内容 | 仓库位置 | Git 管理 |
|---|---|---|
| 最低兼容字段基线 | `shared/blackbox_v2/data_bridge_v1_schema.json` | 是 |
| 脱敏结构样例 | `docs/blackbox_v2/data_bridge_v1/samples/` | 是 |
| 当前全量三频数据 | `data/data_bridge/current/` | 否 |
| 刷新、快照和 Harness 运行产物 | `backtest_artifacts/`、`reports/` | 否 |

删除、改名或重排 DataBridge 基线字段时必须创建新的 `data_schema_version`；新增未消费业务列保持兼容，不冻结当前全量列数。

## 维护规则

1. 通用 SOP 不记录具体方案 ID、run、generation、snapshot 或当前状态。
2. 已被现行规则覆盖的历史正文从工作树删除，通过 Git 历史追溯。
3. 全量 CSV、临时 Snapshot、截图、报告和本机凭据不进入 Git；直接操作授权只在运行期审计中保存 operation hash，不把原始内部 operation id 写入仓库。
4. 修改文档时必须保持相对链接可解析，并运行 `tests/test_onboarding_docs.py`。
