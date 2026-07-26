# Blackbox V2 平台架构与实现边界

**文档状态**：`CURRENT`
**适用运行时**：`blackbox_v2`（与共享平台衔接）
**目标读者**：平台开发和架构审计人员
**最后核验日期**：2026-07-26
**文档类型**：架构说明，不是入库操作 SOP，也不记录具体方案状态。
**操作入口**：[Blackbox V2 平台接入 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
**上游契约**：[Blackbox V2 上游交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
**文档管理**：[Blackbox V2 文档域](../blackbox_v2/README.md)

`Blackbox V2` 是运行时代际；`schema_version=1.0`、`data-bridge-v1` 和 `blackbox-v2-v1` 分别表示接口合同、数据 Schema 和 Runtime Profile，不是三个新的方案版本。

## 1. 统一管理模型

平台只维护一套方案管理体系。两种运行方式共用：

- `SchemeConfig`、composite Registry ID、版本和运行记录；
- `predict_date / feature_date / target_date` 日期语义；
- `PredictionRecord`、actual join、指标、API 和前端链路；
- Harness 编排、授权和生命周期审计。

`runtime_type` 只选择交付检查、输入准备和算法执行驱动，不允许根据目录内容隐式猜测：

| `runtime_type` | 上游形态 | 算法执行入口 |
|---|---|---|
| `native_adapter` | `config.yaml + predict.py + core/` | import `predict.run()` |
| `blackbox_v2` | 一个 `.py` 和一个 `.json` | sandbox CLI 子进程 |

从 `PredictionRecord` 开始，后续业务链路不再区分 runtime。

## 2. 目录与身份

Blackbox V2 上游交付只能包含：

```text
{scheme_id}.py
{scheme_id}.json
```

Intake 后保存为：

```text
schemes/{scheme_id}/
├── config.yaml
└── delivery/
    ├── {scheme_id}.py
    └── {scheme_id}.json
```

`delivery/` 保留上游原始字节并设为只读。Metadata 是名称、算法版本、期限、任务类型、horizon 和 target rule 的唯一来源。平台配置只保存运行信息：

```yaml
scheme_id: <scheme_id>
runtime_type: blackbox_v2
input_source: data_bridge_current
runtime_profile: blackbox-v2-v1
data_schema_version: data-bridge-v1
platform_inputs:
  - api-wind-date-v1
status: paused
version_status: draft
```

`platform_inputs` 是平台运行配置，不是上游 Metadata；未声明时旧
Blackbox 配置、版本和输入身份保持兼容。平台发现层将 Metadata 与
平台配置合并为统一 `SchemeConfig`。同一算法的原生版和 Blackbox
trial 使用不同 base ID，不允许两个实现以同一个 composite Registry
ID 同时 active。

## 3. 数据输入架构

### 3.1 DataBridge current

Blackbox V2 的业务输入来自：

```text
data/data_bridge/current/
├── daily_output.csv
├── weekly_output.csv
└── monthly_output.csv
```

DataBridge 刷新组件负责全量构建、连续两轮稳定性比较、Schema 校验、共享锁和整体发布。current 只保留最新成功版本，不按天保存历史文件；历史因子修订轨迹仍由数据库承担。

`data-bridge-v1` 的机器字段列表是最低兼容基线，不是永久完整表头。DataBridge 可以增加业务列；时间键必须保持第一列，基线字段必须继续存在且相对顺序稳定。校验、business digest 和 Snapshot identity 使用当次文件的全部实际列，因此新增列不会被丢弃，也不会因为总列数变化被拒绝。

`native_adapter` 未声明 `input_source` 时按 `legacy_db` 处理，不受 DataBridge current 失败影响。`blackbox_v2` 必须显式使用 `data_bridge_current`。

### 3.2 三文件父快照

算法不直接读取可变 current。`shared.input_artifacts` 在共享锁内校验 current/state，复制三份文件并生成内容寻址的只读快照：

```text
current generation
  -> 三文件完整性校验
  -> 临时只读 Snapshot
  -> data_snapshot_id
  -> Request + sandbox CLI
  -> 清理临时 Snapshot
```

这个 DataBridge 父快照严格保持三个文件：
`daily_output.csv / weekly_output.csv / monthly_output.csv`。日历不是
第四个 DataBridge 文件，也不改变 `SNAPSHOT_FILENAMES`、generation
或 business digest 的三频语义。Request 的日、周、月截止键由平台
权威 as-of 口径生成，算法不得自行推导周/月键。

Contract 1.0 回测使用“当前完整快照 + 每行 Request 截止键”，能够隔离快照中的后续行，但不能恢复历史时点的数据修订版本。

### 3.3 注册平台制品

Blackbox 正式运行输入定义为：

```text
三频父快照 + 显式声明的平台制品
```

封闭 provider registry 当前只注册：

```text
artifact_id: api-wind-date-v1
provider_version: api-wind-date-provider-v1
filename: api_wind_date.csv
columns: rdate,week_id
```

方案通过 `config.yaml.platform_inputs` 声明版本化 artifact ID，
Runner 再由 registry 推导精确文件名和列，不接受调用者提供任意
文件名。Harness/check-only 用只读数据库连接捕获
`api_wind_date`；scheduled 使用已和 DataBridge generation 核对
ID/manifest SHA 的 Native generation 冻结帧。两者共享规范化与
校验，但 `source provenance` 分别记录，不伪装为相同来源。

### 3.4 组合输入身份

平台将 DataBridge 父快照和平台注册制品组合成执行身份。组合对象
至少承载 `combined_snapshot_id`、`parent_snapshot_id`、
`base_snapshot`、已排序的 `platform_input_ids`、制品摘要、
`expected_filenames`、`identity_manifest` 和 `audit_manifest`。

没有 `platform_inputs` 时，组合 ID 沿用父快照身份。存在制品时，
组合内容身份只包含 `identity schema version`、父快照 ID，以及按
artifact ID 排序的 artifact ID、`provider version`、文件名、SHA256、
大小、行数和列。捕获时间、临时路径、source kind、generation ID
等 `source provenance` 只进入审计 manifest，不进入组合内容身份。
因此相同父快照与相同规范化制品跨 Harness/Native 来源得到相同
`combined_snapshot_id`。

### 3.5 私有临时运行视图

每次运行把父快照和声明制品物化成唯一私有目录。三频文件和平台
制品必须是独立普通文件，不使用 symlink 或 hardlink；写后复核
SHA256，再把文件权限设为 `0444`、目录权限设为 `0555`。无制品时
视图精确三文件；声明 `api-wind-date-v1` 时精确增加
`api_wind_date.csv`，任何缺失、第五个文件、symlink 或非常规文件
都 fail-closed。

正常结束后删除运行视图，不长期复制三份大型 CSV。如果子进程终止
状态不确定，视图转入受控 `debris cleanup`，由确认无进程占用后的
清理流程处理，不能立即删除仍可能被读取的目录。

### 3.6 Freshness 分层

- 技术 Onboarding 当前允许使用最新通过完整性校验的 generation，Input Gate 不强制执行日 freshness。
- `scheduled_live` 必须使用运行当日成功 generation，执行器在算法启动前 fail-closed。

Input Gate 报告记录父/组合 snapshot ID、三文件摘要、平台注册制品
及 identity/audit manifests。DataBridge generation 与平台注册制品
来源仍是分层 provenance；相同内容可能对应不同 generation，因此
仅比较 SHA 不能唯一证明 scheduled generation 身份。

## 4. 执行架构

统一分派：

```text
native_adapter
  -> scheduler.scheme_runner
  -> predict.run()
  -> PredictionRecord

blackbox_v2
  -> 三频父快照 + 注册平台制品 + Request
  -> 组合输入身份 + 私有运行视图
  -> scheduler.blackbox_v2_runner
  -> Result contract validation
  -> PredictionRecord
```

`deploy/blackbox_v2/runtime_profile_v1.json` 是 Blackbox runtime 的发布基准，并引用独立 conda 环境。当前执行器实际从代码中的 `RuntimeProfile` 默认值构造限制，scheduled predict timeout还可由方案配置覆盖；Preflight 必须核对发布基准、代码默认值和方案配置一致。平台使用子进程和 sandbox 施加网络、写路径、资源、超时、Output、stdout/stderr 和批量限制。

脚本协议固定为：

```bash
python {scheme_id}.py predict --request request.json --data-dir <data-dir> --output prediction.json
python {scheme_id}.py backtest --requests requests.csv --data-dir <data-dir> --output backtest.csv
```

Result 通过机器契约后转换为内存 `PredictionRecord`。
`PredictionRecord.extra.data_snapshot_id` 保存 `combined_snapshot_id`；
声明平台注册制品时还保存 `parent_data_snapshot_id`、
`platform_input_ids`、`platform_input_identity_manifest` 和
`platform_input_audit_manifest`（即调用链中的
`input_identity_manifest` / `input_audit_manifest`）。是否写库由
scheduler/backtest 的受控副作用层决定，算法脚本本身没有写库权限。

## 5. Harness 与生命周期

自动技术验收顺序为：

```text
static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness
```

自动段可以写 Harness 报告和控制面审计记录，不得写预测、回测等业务表。`api-readiness` 当前是结构兼容检查，不是实际 Registry/API/scheduler 探针。

`onboard --stage all --check-only` 使用相同七 Gate 和同一组合输入
身份，但禁止控制面持久化和所有业务副作用。它只生成本地
`harness_run_id`、Gate JSON 与统一报告，并明确记录
`check_only=true`、`control_plane_persisted=false`、
`business_tables_written=false`、`persist_backtest=false`。该报告
不能用于授权 shadow/activate/live；普通 all-stage 的控制面证据也
不能与 check-only 报告混用。

生命周期沿用统一状态：

```text
draft -> validated -> shadow -> active -> paused -> retired
```

状态转换必须通过独立授权。Contract 1.0 trial 的发布边界、授权前置检查和失败恢复以平台操作 SOP 为准；本架构文档不记录任何具体方案处于哪个状态。

## 6. 当前实现边界

生产路径控制已经完成以下收敛：

- Input Gate 和后续生产 Gate 绑定 DataBridge generation、freshness、父/组合 Snapshot、平台注册制品与业务摘要；
- JSON Result 严格要求整数方向，拒绝字符串、布尔和浮点方向；
- Runtime Profile 是 Blackbox 执行环境、资源和权限的唯一配置源；
- Harness 审计、版本批准、生命周期 journal 与 reconciliation 均 fail-closed；
- ActivationGate、持久化 BacktestGate 和 LiveGate 要求精确版本与专项授权；
- sandbox 使用文件读取 allowlist、最小环境变量和网络、写路径限制；
- 生产准备 Gate 可执行真实 Registry、API 和 scheduler 探针。

自动 `api-readiness` 仍只是无业务副作用的结构兼容检查，不能替代生产 Gate 的真实探针。临时 Snapshot、原始 Result 和 stderr 也不构成永久历史修订回放资产。

因此，“七个自动 Gate 通过”只表示技术契约验收通过，不等于 active、正式生产、已调度、API 可见或算法效果达标。具体方案必须完成生产准备核验并取得专项授权，才能进入受控生产路径。

## 7. 文档职责

| 信息 | 维护位置 |
|---|---|
| 上游交付、输入、Request、Result 和自验契约 | `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md` |
| 平台 Intake、Preflight、Gate、shadow 和恢复步骤 | `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md` |
| 双运行时架构和当前实现边界 | 本文 |
| 具体方案、generation、snapshot、run、版本和状态 | `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md` |
| DataBridge V1 Schema 入口和脱敏结构样例 | `docs/blackbox_v2/data_bridge_v1/` |
| 文档分类、迁移映射和维护规则 | `docs/blackbox_v2/README.md` |
| 废弃规范和决策演进 | `docs/blackbox_v2/archive/` |
| 机器契约 | `shared/blackbox_v2/` 与 `deploy/blackbox_v2/` |

仓库文档是唯一事实源，外发副本只能由仓库现行文件生成。通用文档不得写入具体方案的当前状态；试验记录不得反向改变通用契约；历史档案不得作为验收依据。
