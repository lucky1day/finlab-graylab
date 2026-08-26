# Blackbox V2 平台入库 SOP（Contract 1.0）

**文档状态**：`CURRENT`

**适用运行时**：`blackbox_v2`

**目标读者**：平台入库、运行和审计人员

本文只描述平台操作者必须执行的步骤。字段合同以
[共享方案契约](../architecture/SCHEME_CONTRACT.md)为准，日期和批量复用以
[预测日期语义](../architecture/PREDICTION_SEMANTICS.md)为准，生产操作授权以
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。

技术入库默认止于 `shadow + paused`。`activate`、回测落库、灰度信号写入和调度变更是彼此独立的
操作；直接执行对应命令只表达该命令的本次操作意图，不授权其它方案、其它副作用或现场部署。

## 1. Intake 与身份

### 1.1 接收前检查

交付目录必须恰好包含两个普通文件，且文件名、目录名和 Metadata `scheme_id` 一致：

```text
{scheme_id}.py
{scheme_id}.json
```

执行 Intake 前确认：

- 没有符号链接、目录、模型、辅助模块或额外配置；
- Metadata 提供合法的 `name` 和 `description`，且不含平台输入或控制字段；
- trial 的 base ID 和 composite Registry ID 未占用；
- 同一算法已有 Native 实现时使用独立 trial ID，不覆盖既有身份；
- 上游自测数据、性能报告和交接材料没有混入两文件目录。

先记录交付原始摘要：

```bash
shasum -a 256 <delivery-dir>/{scheme_id}.py <delivery-dir>/{scheme_id}.json
```

### 1.2 执行 Intake

所有方案统一执行：

```bash
python -m harness intake-blackbox \
  --delivery-dir <delivery-dir> \
  --project-root . \
  --runtime-profile blackbox-v2-v1 \
  --data-schema-version data-bridge-v1
```

Intake 不再接收方案级输入声明。成功后只应生成：

```text
schemes/{scheme_id}/
├── config.yaml
└── delivery/
    ├── {scheme_id}.py
    └── {scheme_id}.json
```

核对 `config.yaml`：

```yaml
scheme_id: <scheme_id>
runtime_type: blackbox_v2
input_source: data_bridge_current
runtime_profile: blackbox-v2-v1
data_schema_version: data-bridge-v1
status: paused
version_status: draft
```

正式新交付的名称、说明、任务和期限只能来自 Metadata，生成配置不得新增 `display_name` 覆盖。Intake 必须原子保存交付并按
base `scheme_id` 拒绝覆盖既有目录，禁止手工拼目录。

Metadata 字段、任务组合和文本约束不在本文维护副本，见
[Blackbox Contract](../architecture/SCHEME_CONTRACT.md)和
[上游交付 SOP](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)。既有 Metadata 中的可选 `owner` 只作兼容解析，
不进入平台状态、门禁或 Dashboard。

### 1.3 Intake 验收

Intake 后只核对四件事：

1. 交付文件字节摘要与接收摘要一致；
2. canonical config 为 `blackbox_v2 + paused + draft`；
3. `name/description` 沿 Metadata 和 canonical config 单向传播；
4. 没有数据库业务记录、active Registry 或调度副作用。

## 2. 环境与数据 Preflight

### 2.1 验证冻结环境

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/verify_blackbox_v2_environment.py
```

结果必须证明 Runtime Profile、当前平台 manifest、包指纹和关键 import 一致。Profile、manifest 或实际
环境任一漂移时停止验收。资源和超时上限以版本化 Runtime Profile 为准，不在 SOP 中复制数值。

### 2.2 验证 DataBridge current

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/refresh_data_bridge_current.py --check-only \
    --date <generation-refresh-date>
```

结果必须为 `status=ok`，并记录 generation、refresh date、business digest、四份文件摘要和范围。
该命令只读，不发布 artifact，也不授予自然调度权。

- 技术 Onboarding 使用显式选定且完整校验通过的 generation；
- `scheduled_live` 必须使用运行日成功发布且 state、文件和截止一致的 generation；
- DataBridge 失败时保留最后成功 current 供审计，但本次运行禁止 fallback。

Schema、允许新增列和 business digest 规则见
[DataBridge V1](../blackbox_v2/data_bridge_v1/README.md)。

### 2.3 对齐上游自测输入

需要逐行比较时，必须同时匹配：

```text
generation_id + refresh_date
四份标准文件 SHA-256
data_snapshot_id
Request 七字段与 cutoff 映射
```

任一身份不同即标记 `data_vintage_mismatch` 并停止算法归因；不得按文件名、日期范围、最终方向或口头
说明推断同代。平台应给出选定的只读 generation，让上游在同一输入上重跑。生产不会永久冻结在
Onboarding generation，后续自然运行仍使用当天合格 generation。

### 2.4 核对上游性能交接

Intake 目录外的 `{scheme_id}.performance.json` 必须满足
[上游强制性能自测](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#64-强制性能自测与交接证据)。平台核对报告、
批次数和 Request 总数，不在 Harness 中重复实现算法级性能、确定性或截止隔离测试。

## 3. 快照与 Request

DataBridge producer 发布 generation 时，直接使用本次已经验证的内存数据构建一次 ready snapshot；不得重新读取或再次校验 current CSV。方案入库和调度只读取该 ready receipt，在 Compare 证据中记录 Snapshot ID、generation、business digest、环境指纹和 Request；缺失即阻断，不代建、不修复、不触发 DataBridge 验证。

新版 receipt 不兼容沿用旧 receipt。release 切换后必须先由同一 release 完成一次 DataBridge publish 和 ready gate，再允许 Harness、回测或自然调度消费；不得在方案流程中补建或升级 receipt。

运行视图仅把 producer 封存文件稳定复制为本次子进程私有的只读普通文件；不重复哈希、解析或扫描 CSV。运行结束前后仍校验私有文件未被替换或修改。
正常结束后清理；无法确认子进程终止时只允许进入受控 debris recovery，不得立即删除可能仍在读取的目录。
报告中的临时绝对路径不能用于回放。

Request 恰好包含 `request_id`、三个标准日期和三个 cutoff。字段关系、as-of 计算、100 条批量上限和
合并顺序以[共享方案契约](../architecture/SCHEME_CONTRACT.md)和
[预测日期语义](../architecture/PREDICTION_SEMANTICS.md)为准。

## 4. 自动 Harness

### 4.1 执行技术 Gate

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness onboard {scheme_id} \
    --predict-date YYYY-MM-DD \
    --stage all \
    --algo-env forecast_env_blackbox_v1
```

固定顺序为 `static → compare`。Compare 只绑定 producer-ready snapshot、Request 并执行一次有效冒烟预测；非法 Request、退出码和失败无 Output 由上游交付契约负责，平台不重复认证。任一 Gate 失败即 fail-fast，不得进入 shadow。

平台只验证自身边界：交付和 Metadata、输入捕获、Request/Result 合同、平台喂入内容及一次冒烟执行。
重复执行确定性、predict/backtest 等价、跨批顺序一致和逐 Request 截止隔离由上游契约负责，平台不
重复回归，也不得宣称已经验证这些算法性质。

### 4.2 Gate 证据与副作用

Gate 的精确职责见[Harness 架构](../architecture/HARNESS_ARCHITECTURE.md)。继续前必须确认：

- current exact version 的 latest `all` run 为 `passed`；
- `static/compare` 两项均已持久化；
- generation、combined snapshot、环境指纹和 Request 身份一致；
- 没有写入 `t_scheme_runs`、prediction、backtest、active Registry 或前端状态。

`all` 只允许写 Harness 审计表和短生命周期运行输入。数据库是 run/Gate 的耐久权威：开始时先写
`running` 以 fail-early，Gate 结束后在一个事务中批量写结果并完成 run；commit ACK 不确定时用新连接精确读回 run 状态、Gate multiset 和 summary，任一无法确认的持久化失败都阻断后续
副作用。Dashboard payload 不含 exact version，不能替代生命周期和数据库版本证据。

两段 evidence profile 不兼容读取旧流程报告。升级前已有旧 `all`、但尚需执行后续副作用的
exact version，先用当前两段流程重跑一次；不得用旧 `report_uri` 或本地 `input_state.json` 补证。

## 5. Shadow 登记

执行前重新核对 exact run、两个 Gate、身份冲突和业务表前置状态，然后运行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate shadow-register \
    --scheme-id {scheme_id} \
    --predict-date {all_stage_predict_date}
```

CLI 自动绑定 canonical exact version 和 latest passed `all` run。新身份只能 insert-only 创建 draft
version 与 paused composite Registry；既有 revision 走现有生命周期事务。冲突、漂移或 readback
不一致必须回滚，禁止 upsert、覆盖或手工修状态。

Shadow 后必须满足：配置为 `paused + shadow`、Registry 全 paused、业务表零增量、scheduler 和
active API 不可见、既有 active 集合不变。此状态只能称为“完成 shadow 技术入库”。

## 6. 专项授权后的回测与批量复用

本节不属于默认技术入库。§6.1–6.4 只有在具体方案取得独立 `backtest_persist` 授权后才能执行；
该授权不包含 §6.5 的 gray materialization。

### 6.1 日期范围

- 先以方案级证据确定 `gray_target_start`；不得从 Metadata、部署日或操作日推导；
- `predict_date` 必须等于该起点，并作为 `target_date` 的 exclusive cutoff；
- canonical backtest 只能包含 `target_date < gray_target_start`；
- `--backtest-start-date` 默认 `2025-01-01`，其它起点必须由专项范围明确绑定；
- 当前 Blackbox 回测只接受 `--persist`，不存在抽样认证路径。

### 6.2 执行完整持久化回测

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate backtest \
    --scheme-id {scheme_id} \
    --predict-date {gray_target_start} \
    --persist \
    --backtest-start-date 2025-01-01 \
    --timeout-sec 1800 \
    --algo-env forecast_env_blackbox_v1
```

平台先生成完整 HistoricalCase，再按 Profile 分批；单批上限不是完整回测总量上限。全部批次必须绑定
同一 version、generation、snapshot 和环境指纹并共享总 deadline。任一批失败、超时、顺序/echo 或
数量不匹配时不得进入写库事务。

### 6.3 持久化后验收

一次事务只能产生一个 immutable success run、完整 prediction 明细和非空 monthly metrics。核对动态
预期数量、日期边界、批次证据、版本/输入身份和 durable summary。旧 run 保持不可变；禁止更新、删除
或扩充旧记录来制造 canonical 结果。

### 6.4 失败重试

事务失败后重新执行完整命令并产生新的 operation ID。不得复用半完成状态、直接 SQL 修补或把前端裁剪
当成 backtest/live 零重叠证明。

### 6.5 一次性批量结果复用快路径

资格和日期重建只由
[预测日期语义 5.2](../architecture/PREDICTION_SEMANTICS.md#52-一次性批量结果的分区与复用)定义：

gray 分区物化还必须取得独立授权，并精确绑定 scheme、version、输入 lineage 和本次授权组；不得从
`backtest_persist`、activation 或其它方案的授权外推。

1. 计算前冻结 exact version、输入 identity/lineage、完整 Request 集和 Output 摘要；
2. `target_date < gray_target_start` 写入新 canonical backtest，其余合格缺口才可 insert-only 物化为
   `gray_live`；
3. live `predict_date` 按任务日历重建，不复制数据库主键、run、Actual、指标或 Harness 历史；
4. 已有任一业务键即整组拒绝，禁止 update/upsert、先删后写或第二套 SQL。

不能证明逐 Request cutoff、batch 等价或 live-safe 时必须重新计算，不得复用。

## 7. 激活、灰度与前端验收

每个动作分别取得范围明确的授权。激活命令为：

```bash
python -m harness activate --scheme-id {scheme_id}
```

激活后读回 active exact version、全部 active composite Registry 和非空 `deployed_at`。历史缺口只用
单日入口，按日期顺序执行：

```bash
python -m harness signal-gap-fill --predict-date YYYY-MM-DD \
  --scheme-id {scheme_id}
```

已有键整组拒绝，禁止直接 SQL、日期范围补写或把 `gray_live` 冒充 `scheduled_live`。随后执行：

```bash
python -m harness gate dashboard --scheme-id {scheme_id}
```

DashboardGate 核对当前业务读模型；exact version 仍由生命周期和数据库证明。`Onboarding Complete`
要求 activation、canonical backtest、gray 补齐和 Dashboard 均通过；`Production Observed` 还必须由真实
one-shot 时钟产生成功 `scheduled_live`，并有 installed/loaded state、日志、run 和 prediction 证据。

这些命令均不安装 plist/unit、不重启服务，也不授予生产 Writer 或域名切换权限。

## 8. 失败恢复

| 场景 | 处理原则 |
|---|---|
| Intake、环境、输入或自动 Gate 失败 | 停止后续动作；修复后从 static 重跑完整 `all` |
| 上游与平台输入身份不同 | 标记 `data_vintage_mismatch`，让上游在平台选定 generation 重跑 |
| shadow/activation 冲突或状态漂移 | 禁止覆盖、删除或自动重试；保存审计并核对 lifecycle journal |
| 回测含 gray target 或 backtest/live 重叠 | 保留旧 run，生成新的 immutable canonical run 后重验 |
| gray live 缺口 | 逐日执行 `signal-gap-fill`；任一 blocker 时零写入 |
| API/scheduler 意外出现 paused trial | 保持 paused，定位可见性来源，不执行 live |
| DataBridge 或日历覆盖失败 | 当前运行 fail-closed，不 fallback 到旧 generation 或伪装为节假日 |
| 临时运行目录残留 | 确认无进程占用后走受控 debris recovery，不交给下一次运行 |

任一 pending lifecycle journal 都必须阻断新的 shadow、activate 或 revision 动作。唯一恢复入口是：

```bash
python -m harness gate lifecycle-reconcile --scheme-id {scheme_id}
```

该命令只恢复 journal 记录的 previous safe state，并新增 linked reconciliation journal。多个 pending、
恢复失败或 readback 不一致时继续阻断并转人工核查；不得手工删除历史版本或 journal。

## 9. 完成条件

- [ ] 两文件 Intake、Metadata 和 canonical paused/draft 配置一致；
- [ ] exact generation、combined snapshot、Request cutoff、环境指纹和两个 Gate 证据一致；
- [ ] Shadow 后 Registry paused、业务表零增量且 active API/scheduler 不可见；
- [ ] 如执行 backtest/批量复用，授权、`gray_target_start`、日期重建和零重叠均通过；
- [ ] 如执行 activation/gray 写入，exact version、Registry、journal 和 insert-only readback 一致；
- [ ] Dashboard 通过，且 `scheduled_live` 只由真实 one-shot 时钟证据认定。

生产准备的最终判定见[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)。具体方案的运行事实保留在
现有控制面；稳定摘要进入 `CURRENT_STATUS.md`，未闭环事项进入 `TODO.md`，不另建单次交接文档。
