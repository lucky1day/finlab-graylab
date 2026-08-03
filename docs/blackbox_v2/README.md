# Blackbox V2 文档管理

**文档状态**：`CURRENT`
**适用运行时**：`blackbox_v2`
**目标读者**：文档维护、平台入库和审计人员
**最后核验日期**：2026-08-03
**事实源**：本仓库 `docs/` 目录

本目录负责组织 Blackbox V2 的文档关系、试验记录和数据样例。对外发送的桌面文件或压缩包只是仓库文档的导出副本，不得在仓库外独立修改后再反向作为规范。

Blackbox V2 是所有后续新算法、新方案 ID、新目标、新任务和替代版本的唯一入库运行时。场景判断统一从[方案入库导航](../onboarding/README.md)进入；Native V1 只维护政策清单中的存量方案。

本目录不定义生产调度权。生产 writer、installed plist 证据、输入新鲜度及
`gray_live` / `scheduled_live` 边界以
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准；历史
台账名称中的 “ledger” 不表示可用于生产调度。

## 1. 文档分层

| 类型 | 文档 | 作用 | 是否可定义规则 |
|---|---|---|---|
| 上游契约 | [上游交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md) | 算法工程师交付、运行和自验标准 | 是 |
| 平台操作 | [平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md) | 平台收包、Gate、shadow、专项生产灰度、前端验收和失败恢复 | 是 |
| 生产准备 | [生产晋级条件](PRODUCTION_READINESS.md) | 管理从 shadow 到 active/live 的代码、真实交付覆盖和授权门槛 | 否，当前为阻塞草案 |
| 架构边界 | [平台架构说明](../architecture/BLACKBOX_V2_PLATFORM.md) | 双运行时、数据流和实现边界 | 是 |
| 试验记录 | [入库试验台账](records/ONBOARDING_TRIAL_LEDGER.md) | 记录具体 generation、snapshot、run 和整改项 | 否 |
| 稳定性审计 | [2026-07-19 全链路认证](records/FULL_PIPELINE_STABILITY_AUDIT_20260719.md) | 隔离验证入库、回测、live、actual、API 和前端，并给出时点结论 | 否 |
| 生产灰度记录 | [2026-07-20 专项激活](records/PRODUCTION_GRAY_ACTIVATION_20260720.md) | 记录真实方案的生产 Activation、回测、gray live 和下游证据 | 否 |
| 生产灰度记录 | [2026-07-20 1Y T+5 四方案](records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md) | 记录四个真实日频方案的 Intake、稳定性、Shadow 与分阶段上线 | 否 |
| 数据契约 | [DataBridge V1 数据说明](data_bridge_v1/README.md) | 指向机器 Schema，提供脱敏结构样例 | 否，机器 Schema 优先 |

本目录的分层入口：

- [DataBridge V1](data_bridge_v1/README.md)
- [试验记录](records/README.md)

规则冲突时，按以下优先级处理：

```text
已批准的机器契约
→ 现行 SOP
→ 架构说明
→ 试验记录
→ 数据样例
```

机器契约位于：

- [`shared/blackbox_v2/`](../../shared/blackbox_v2/)
- [`deploy/blackbox_v2/`](../../deploy/blackbox_v2/)
- [`data_bridge_v1_schema.json`](../../shared/blackbox_v2/data_bridge_v1_schema.json)

若运行代码、机器契约与现行 SOP 不一致，必须记录为实现缺口，并在平台 SOP 中如实说明当前可执行行为。代码偏差不会自动改写已批准的接口契约；不得在试验记录中偷偷改变通用规则。

## 2. 维护规则

### 2.1 修改通用契约

1. 先核对机器契约、运行代码和测试。
2. 修改受影响的上游 SOP 或平台 SOP。
3. 同步检查架构说明中的职责和实现边界。
4. 不在 SOP 中写入具体方案 ID、generation、snapshot、run 或当前状态。
5. 对外发送时从仓库现行文件生成副本，不维护第二份权威正文。

### 2.2 记录一次真实入库

1. 摘要只追加到[入库试验台账](records/ONBOARDING_TRIAL_LEDGER.md)；完整认证或生产灰度可增加独立 Markdown 和 JSON 证据，并由台账引用。
2. 每条记录必须包含执行时间和时区。
3. generation、snapshot、Harness run、方案版本和状态必须标明历史或当前结论时点。
4. 实测值不得提升为通用接口规则。
5. 新记录不得覆盖旧记录；结论被推翻时追加更正记录并引用原记录。

### 2.3 修改 DataBridge Schema

1. `data-bridge-v1` 机器 Schema 是最低兼容字段基线；当前导出增加业务列不修改该基线，也不创建新版本。
2. 新增列必须保留时间键首列和已有基线字段相对顺序，并由校验器纳入数值检查、摘要和 Snapshot identity。
3. 删除、改名或重排基线字段，改变时间键，或把新增字段提升为必需基线时，不覆盖已有版本；创建新的 `data_schema_version`。
4. 新版本同步更新机器 Schema、校验器、测试、数据说明、脱敏样例以及上下游 SOP 引用。
5. 全量业务 CSV 继续只存在于运行期目录，不进入 Git。

### 2.4 删除废弃文档

- 已被现行契约、SOP 或架构完整替代的废弃正文从当前工作树删除。
- 删除前必须确认现行入口覆盖仍有效的规则，并同步修正所有链接和测试。
- 原始正文和决策演进通过 Git 历史追溯，不在仓库内维护第二份 archive 副本。
- Git 历史中的 Excel、数据库、旧 horizon 或 Registry 规则不能作为当前验收依据。

## 3. 数据文件边界

| 内容 | 仓库位置 | Git 管理 |
|---|---|---|
| 最低兼容字段基线 | `shared/blackbox_v2/data_bridge_v1_schema.json` | 是 |
| 脱敏结构样例 | `docs/blackbox_v2/data_bridge_v1/samples/` | 是 |
| 当前全量三频数据 | `data/data_bridge/current/` | 否 |
| 刷新状态与 staging | `backtest_artifacts/data_bridge_refresh/` | 否 |
| 算法单次只读 Snapshot | 运行期临时目录 | 否 |

`data/data_bridge/current/` 的运行约定见 [`data/data_bridge/README.md`](../../data/data_bridge/README.md)。样例只用于理解文件名、制作时点的字段基线、时间键和读取方式，不能用于效果验证、数据水位检查、推断永久列数或生产运行。

## 4. 桌面目录迁移映射

2026-07-19 将桌面外发目录整合为以下仓库结构：

| 桌面内容 | 仓库处理 |
|---|---|
| `02-上游算法黑盒V2交付SOP-重构版.md` | 已收敛为现行上游 SOP |
| `01-Bond-Factor-Lab后续方案入库规划.md` | 迁入追加式试验台账 |
| `02-上游算法黑盒V2交付SOP.md` | 已由现行上游 SOP 替代，不保留工作树副本 |
| `数据桥/*.csv` | 不复制全量文件；以机器 Schema 和脱敏样例管理 |
| `.数据桥.previous.*` | 不迁入；历史数据修订由数据库和试验记录承担 |
| `.DS_Store` | 不迁入 |

桌面目录保留为历史外发快照。后续修改只发生在仓库，桌面文件不再做双向同步。

## 5. 命名与版本

- 现行 SOP 使用稳定文件名并在标题中标明接口版本。
- 数据契约目录按 `data_bridge_v{n}` 分开，禁止原地改写旧版本语义。
- 试验记录使用追加式单台账；规模明显增大后再按年份拆分。
- 文档日期使用 `YYYY-MM-DD`；真实执行时间同时写明 `Asia/Shanghai` 或 `UTC`。

## 6. 提交前检查

- [ ] 上游 SOP 只回答交付什么、怎么运行、怎么读取、怎么输出、怎么自验。
- [ ] 平台 SOP 只回答如何收包、Preflight、Gate、shadow、核验和恢复。
- [ ] 具体方案和运行 ID 只出现在试验台账。
- [ ] 已删除的废弃文档不再被现行入口、索引或测试引用。
- [ ] 数据样例不包含完整业务历史，只包含合成值。
- [ ] 全量 CSV、临时 Snapshot、状态文件和本机凭据未进入 Git。
- [ ] Markdown 相对链接能够从仓库内解析。
- [ ] 文档所述字段、任务组合和文件名与机器契约一致。
