# Blackbox V2 平台入库 SOP（Contract 1.0，Intake、生产灰度与前端验收）

**文档状态**：`CURRENT`
**适用运行时**：`blackbox_v2`
**目标读者**：平台入库、运行和审计人员
**最后核验日期**：2026-08-13

本文是平台操作人员接收、技术验收和登记 Blackbox V2 方案的唯一操作 SOP。上游交付契约见 [BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)。精确版本、快照和运行结果由 Harness 控制面与本机 ignored reports 保存；当前状态和未闭环工作分别进入 [CURRENT_STATUS](../CURRENT_STATUS.md) 与 [TODO](../TODO.md)。文档分类和维护规则见 [Blackbox V2 文档管理](../blackbox_v2/README.md)。

本文的通用入库流程止于 `shadow + paused`，不自动授予生产运行权限。仓库由一名维护者独立管理，因此 `activate`、回测落库和 `live` 使用直接副作用命令：命令本身表达该次明确操作意图，系统不生成密钥、token、nonce 或 replay store。这些命令仍只能在完成[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)核验并决定具体方案范围后执行；不得把某个试验方案的操作外推为所有新方案的默认权限。具体生产灰度记录只写入平台试验台账。

自然 `scheduled_live` 还受
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)约束：只有
launchd + installed plist 可以成为生产控制面，`ledger`、`occurrence`、`epoch`、daily-gray
和常驻 APScheduler 不能作为新的或过渡调度路径。本文中保留的旧生产时序仅用于解释历史
证据，不能据此安装、迁移或启动服务。

> **方案 A 机器合同已生效（2026-08-13）**：正式新交付必须在 Metadata
> 中同时提供 `name`、`owner` 和 `description`。Contract、Intake 原子登记、
> StaticGate owner readback 和新方案 DashboardGate 展示身份核对均已实现；任何
> 缺项、非法 owner、owner 冲突或三字段读回偏移均 fail-closed。正式新交付的
> `name` 只来自 Metadata，Intake 不生成 `config.yaml.display_name`。
> 已入库且正在使用 `display_name` 的历史方案继续兼容，不得为迁移新规则原地改写。

## 1. Intake 与身份

### 1.1 接收前检查

交付目录必须恰好包含：

```text
{scheme_id}.py
{scheme_id}.json
```

执行 Intake 前确认：

- 两个条目均为普通文件，不是目录或符号链接；
- 文件名与 Metadata 中的 `scheme_id` 一致；Metadata 有包含 `name` 的历史八字段，并另外提供正式新交付必填的 `owner` 和 `description`；
- Metadata 不含 `platform_inputs`；该字段属于 Intake 生成的平台配置，不属于上游合同；
- `.py` 是唯一可执行内容，不存在模型、配置、依赖或辅助模块；
- trial 的 base `scheme_id` 和 composite Registry ID 均未占用；
- 同一算法已有原生实现时使用独立 trial ID，不覆盖原方案。

上游工作目录可以含从 DataBridge 下载的自验
`api_wind_date.csv`、sample、自测输入摘要或交接材料，但不得把这些
内容交给 Intake。平台必须先复制精确 `.py + .json` 到私有临时目录，
并确认该目录恰好两个普通文件；上游自验日历只用于摘要对齐，永不成为
正式运行输入。

记录收到文件的原始摘要：

```bash
shasum -a 256 <delivery-dir>/{scheme_id}.py <delivery-dir>/{scheme_id}.json
```

### 1.2 执行 Intake

先确认当前代码包含方案 A 机器合同；若 Contract 仍把 `owner` 报为额外字段，
立即停止并保留原始包，核对分支和版本，不得删除字段后重试。

以下示例为需要平台周历的方案；不需要任何平台注册制品时省略最后一行：

```bash
python -m harness intake-blackbox \
  --delivery-dir <incoming-two-file-directory> \
  --project-root /Users/macstudio0/bond-factor-lab \
  --runtime-profile blackbox-v2-v1 \
  --data-schema-version data-bridge-v1 \
  --platform-input api-wind-date-v1
```

Intake 应原字节保存交付文件，并生成：

```text
schemes/{scheme_id}/
├── config.yaml
└── delivery/
    ├── {scheme_id}.py
    └── {scheme_id}.json
```

检查平台配置至少包含基础字段；使用上述参数时还必须包含
`platform_inputs`：

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

对方案 A 生效后的正式新交付，名称、交付来源、算法说明、算法版本、期限、任务类型、horizon 和 target rule 只能来自 Metadata；Intake 生成的配置不得新增 `display_name` 覆盖。已有历史配置中的 `display_name` 仍按原有 runtime 兼容，它不是新交付可使用的第二个名称入口。`blackbox-v2-v1` 是运行 profile；`forecast_env_blackbox_v1` 是该 profile 当前引用的 conda 环境，两者不得混称。

Intake 必须在任何方案文件写入前，对 Metadata 和
`deploy/scheme_owner_v1.json` 完成 preflight，并将 `owner` 按
`{scheme_id}__h{horizon}__{target_tenor}` 登记。相同 composite ID 与相同
owner 可幂等接受；相同 composite ID 与不同 owner 必须 fail-closed，不能覆盖。
交付保存和 owner 登记必须形成单一成功或单一失败结果，不能留下半写状态。

`owner` 是前端“来源”列的方案交付归属，可填写交付同事姓名缩写、姓名或稳定团队代码；它不是 DataBridge 数据源、`input_source`、算法依赖来源或审批人。去除首尾空白后必须仍非空，且不得包含换行、`<`、`>`、HTML 或其他标记文本，也不得使用 `--`、`unknown`、`待定` 等占位值。

`description` 是正式新交付的必填算法逻辑摘要。通用 Metadata 解析仍可读取历史不可变八字段包，但 `intake-blackbox` 会在写入前 fail-closed，拒绝任何缺少 `name`、`owner` 或 `description` 的正式新交付。说明必须是单段非空纯文本、最多 300 个字符，换行、`<`、`>`、空字符串或错误类型均拒绝。

已有不可变方案缺少 `owner` 或 `description` 时不修改只读 Metadata、不推测交付来源或算法逻辑，也不制造新版本。历史兼容只适用于 `deploy/blackbox_v2_legacy_metadata_v1.json` 中锁定的精确 base ID + Metadata SHA-256；同一 ID 的任何新 revision 只要 Metadata 字节变化，就必须提供方案 A 三字段。历史来源继续由平台在 `deploy/scheme_owner_v1.json` 中补录；历史缺失说明仍按现有发现与运行兼容边界映射为空字符串，但不能据此把技术 Gate 通过解释为说明已经补齐。正式新交付没有 owner 或 description waiver。

### 1.3 对照 Contract 1.0

机器契约 `shared.blackbox_v2.contracts` 是字段和组合的判定源；平台注册输入则由 Intake 参数和 `config.yaml` 表达，不写入 Metadata：

- Metadata 的历史机器基线包含八字段：`schema_version`、`scheme_id`、`name`、`algorithm_version`、`target_tenor`、`task_type`、`horizon`、`target_rule`；当前机器合同兼容历史不可变包，并由正式新 Intake 强制 `owner` 和 `description`。除这两个正式字段外，其他额外字段（包括 `platform_inputs`）继续 fail-closed。
- Request 恰好七字段：`request_id`、`predict_date`、`feature_date`、`target_date`、`daily_cutoff_key`、`weekly_cutoff_key`、`monthly_cutoff_key`。
- Result 恰好五字段：`request_id`、`predict_date`、`feature_date`、`target_date`、`predicted_direction`。

| `task_type` | `horizon` | `target_rule` |
|---|---:|---|
| `T+1` | 1 | `target_date_yield_vs_feature_date_yield` |
| `T+5` | 5 | `target_date_yield_vs_feature_date_yield` |
| `weekly_point` | 1 | `target_week_end_yield_vs_feature_week_end_yield` |
| `weekly_average` | 1 | `target_week_average_yield_vs_feature_week_average_yield` |
| `monthly` | 1 | `target_month_observation_yield_vs_feature_month_observation_yield` |
| `monthly_average` | 1 | `target_month_average_yield_vs_feature_month_average_yield` |
| `quarterly_average` | 1 | `target_quarter_average_yield_vs_feature_quarter_average_yield` |
| `annual_average` | 1 | `target_year_average_yield_vs_feature_year_average_yield` |

字段数、名称或固定组合不一致时 Intake 必须失败，不得在平台配置中纠正上游 Metadata。

### 1.4 方案名称、交付来源与算法说明的展示链路

方案 A 配套机器实现上线后的正式新交付，三个展示字段必须保持独立、按各自单向链路传播：

```text
{scheme_id}.json.name
→ SchemeConfig.name
→ t_scheme_registry.name
→ Dashboard.name
→ 灰度实验室前端“方案”列

{scheme_id}.json.owner
→ Intake 校验并按 composite Registry ID 原子登记
→ deploy/scheme_owner_v1.json
→ Dashboard.owner
→ 灰度实验室前端“来源”列

{scheme_id}.json.description
→ SchemeConfig.description
→ t_scheme_registry.description
→ Dashboard.description
→ 灰度实验室前端“备注”详情
```

平台复用现有 `t_scheme_registry.name`、`t_scheme_registry.description` 和版本化 owner registry，不新增备注表或算法 Request 字段。前端只展示经过文本转义的名称、来源和说明；不得把它们当作 HTML，也不得用方案名、任务格子、部署状态或平台运营意见填充空来源或空说明。正式新交付的 Metadata 中任一展示字段发生变化时，其文件摘要和 canonical `scheme_version` 必须随之变化；历史方案的来源补录不得反向改写不可变 Metadata，历史 `display_name` override 也不得被误称为方案 A 的新交付链路。

## 2. 环境与数据 Preflight

### 2.1 验证冻结环境

以下机器资产是对外发布和 Preflight 的冻结基准，不在 SOP 中硬编码 Python 版本：

```text
deploy/blackbox_v2/runtime_profile_v1.json
deploy/blackbox_v2/environment_manifest.json             # Linux x86_64
deploy/blackbox_v2/environment_manifest.osx-arm64.json   # Mac arm64
```

`data-bridge-v1` 的机器 Schema 和脱敏结构样例入口见 [DataBridge V1 数据契约与样例](../blackbox_v2/data_bridge_v1/README.md)。

执行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/verify_blackbox_v2_environment.py

```

执行器从版本化 JSON 加载 Runtime Profile，并在启动时严格校验字段、类型和安全边界。环境验证按当前平台只选择上述唯一匹配 manifest；未知平台 fail-closed。环境 manifest 用于核验实际环境指纹；方案配置不得覆盖 Profile 的算法执行资源、读路径、环境变量或网络权限。Intake 为 Blackbox 生成正整数 `schedule.timeout_sec` 作为 predict 预算申请，Profile 的 `predict_timeout_sec` 是平台上限，显式 operation deadline 是可选的第三层收紧约束；最终取三者最小值。backtest timeout 仍是独立 Profile 预算。Profile、manifest 或实际环境任一漂移时停止验收。

记录环境清单摘要和自检时间。环境不一致、资源基准漂移、入库 StaticGate 违规、精确版本不匹配或运行后输入目录指纹变化时，不得继续。CPU、内存、predict/backtest 超时、100 条批量上限、Output 和日志大小上限以核对一致后的实际执行值为准。当前清理不修改既有 Blackbox 配置或 canonical config hash，因此不产生新 exact scheme version，也不触发 Gate、revision activation 或 Registry 切换。

### 2.2 验证 DataBridge current

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/refresh_data_bridge_current.py --check-only \
    --date <selected-generation-refresh-date>
```

这只是技术 Onboarding 的只读检查，不发布 artifact，也不授予自然调度权。G1 完成前，
不得把这个兼容检查入口、常驻 scheduler 或任何环境开关当作 DataBridge 的生产 refresh
writer；生产 refresh 的唯一控制面与授权边界以
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准。

结果必须为 `status=ok`，并保存：

- `generation_id`、`refresh_date`、`refreshed_at`；
- `schema_version`、`business_digest`；
- 三份文件的行列数、起止键、SHA256 和业务摘要。

`data-bridge-v1` 机器 Schema 的字段列表是最低兼容字段基线，不是完整固定表头。实际三频文件允许随指标接入增加业务列，不设置总列数常量；校验器必须确保时间键位于第一列、基线字段全部存在且相对顺序不变，并对全部实际业务列执行有限数值或空值检查。新增业务列必须进入 business digest、文件 SHA256 和后续 Snapshot identity。报告中的实际列数只记录本次 generation 的事实，不能作为下一次刷新或其它 generation 的固定门槛。

| 场景 | freshness 要求 |
|---|---|
| 技术 Onboarding | 选择最新一个通过完整性校验的 generation；将其 `refresh_date` 显式传给 `--date`，允许与执行日不同 |
| `scheduled_live` | 将运行日传给 `--date`；必须是当日成功发布且 state/文件摘要一致的 generation |

Input Gate 当前使用 `require_fresh=False`，只证明快照结构和内容可用，不独立证明它是当天 generation；scheduled-live 执行器才强制当天 freshness。两者不得混称。

DataBridge 校验失败时保留最后成功 current，阻断依赖 `data_bridge_current` 的运行，不用旧摘要冒充新 generation。

### 2.3 对齐上游自测与平台验收输入

需要逐行比较上游自测结果时，不能只核对算法两文件。完整可比较输入
身份固定为：

```text
generation_id
refresh_date
daily_output.csv SHA256
weekly_output.csv SHA256
monthly_output.csv SHA256
api_wind_date.csv canonical SHA256
combined_snapshot_id
```

上游通过 DataBridge 专用接口下载 `api_wind_date.csv`，但普通 CSV
响应不携带平台内部 `generation_id`。因此平台必须：

1. 接收上游自测记录中的下载时间、三频/日历 SHA256、行数、起止键和
   Request 七字段；
2. 用三频 SHA256 对应到本次选定的 DataBridge generation，禁止按文件
   名、日期范围或口头说明猜测；
3. 用 `api-wind-date-v1` provider 对平台权威日历规范化，核对其 SHA256；
4. 生成并记录 `combined_snapshot_id`；
5. 核对每条 Request 的
   `daily_cutoff_key -> weekly_cutoff_key` 与同一日历精确一致；
6. 只有完整身份相同后，才允许把输出差异归类为算法或平台适配差异。

三频或日历任一摘要不一致时，本次比较必须标记
`data_vintage_mismatch` 并停止算法归因。平台应指定已选 generation
及其只读输入，让上游在同代数据上重跑；原自测报告保留为旧数据版本
证据，不能冒充当前部署验收。

同代约束只适用于一次可重复验收，不把生产永久冻结在 Onboarding
generation。正式 `scheduled_live` 仍使用当天最新、完整校验且
`SEALED` 的 generation，并在每次运行记录
`generation_id + combined_snapshot_id`。不同 generation 的结果只能
用于稳定性观察，不能宣称逐行复现。

### 2.4 确认回测成本

Intake 前必须核对 delivery 目录外的 `{scheme_id}.performance.json`，验收字段和资源上限以
[上游强制性能自测](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#64-强制性能自测与交接证据)为唯一规范。
`predict 120秒 / 100条 600秒 / 完整区间 1800秒 / 峰值RSS 4GiB` 任一不满足即退回上游，
平台不得通过改写算法或逐条重验上游性质来补救。

walk-forward 交付必须一次构建完整区间、按 Request cutoff 取值，并用首/中/末独立复算自证；
非 walk-forward 交付必须显式声明但仍遵守同一资源上限。平台只核对性能报告、实际批次数和总
Request 数，不在 Harness 中再次实现算法级性能测试。

### 2.5 生产时序与自然候选

生产时点、installed/loaded 控制面和 freshness 规则只由
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)定义，本 SOP 不维护副本。
平台入库只执行以下检查：

- 自然运行使用当天最新且 `SEALED` 的 DataBridge generation，任一身份或截止漂移均 fail-closed；
- active config 与 active exact version 才进入对应 cadence 的 one-shot 候选，任务选择使用
  `task_type`，不由 `frequency/horizon` 猜测；
- Activation 不安装 plist/unit、不重启服务，也不能证明已经产生 `scheduled_live`；
- 历史缺口只使用单日 `signal-gap-fill`，保持 insert-only，禁止 fallback、覆盖和自动重试；唯一命令格式和
  零写终态见[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md#2-自然信号历史修复与输入新鲜度)。

## 3. 快照与 Request

### 3.1 运行快照

平台输入分两层。DataBridge 父快照仍然严格只有三份业务文件：

1. 在共享锁内校验 `data/data_bridge/current` 与 state；
2. 复制 `daily_output.csv`、`weekly_output.csv`、`monthly_output.csv`；
3. 按最低兼容字段基线校验实际表头，并用三份实际文件的完整列集合和内容生成父 `snapshot_id`；
4. 不把日历或其它平台注册制品加入 DataBridge generation、父
   Snapshot 或 `SNAPSHOT_FILENAMES`。

声明了 `platform_inputs: [api-wind-date-v1]` 的方案在父快照上组合
`api_wind_date.csv`。首个 provider 的固定契约是：

```text
artifact_id: api-wind-date-v1
provider_version: api-wind-date-provider-v1
filename: api_wind_date.csv
columns: rdate,week_id
```

Provider 将 `rdate` 规范化为非空、唯一、严格升序的 `YYYY-MM-DD`，
将整数或尾随 `.0` 形式的 `week_id` 规范化为六位平台键，并要求覆盖
本次 Request 的 `weekly_cutoff_key`。日历按完整权威范围冻结，不按
`feature_date` 截断；三频业务文件仍按各自 cutoff 使用。

Harness、自然调度和历史 replay 均通过调用方只读 DB
连接捕获权威 `api_wind_date`，并使用同一个 provider 规范化内容。
来源类型和捕获时间只进入 `audit_manifest`，不参与内容身份；
DataBridge generation 只绑定三频父快照，不再承载第二份 Native
平台输入 generation。

组合对象同时记录 `combined_snapshot_id`、`parent_snapshot_id`、
三频父快照、已排序 `platform_inputs`、制品摘要、`identity_manifest`
和 `audit_manifest`。没有平台注册制品时，组合 ID 直接沿用父
snapshot ID；有制品时只对身份 schema 版本、父 ID，以及按 ID 排序
的 artifact ID、provider version、文件名、SHA256、大小、行数和列
计算组合 ID。因此相同父快照和相同规范化日历在重复 DB capture 后
具有相同 `combined_snapshot_id`。

每次子进程运行前，平台把三频父快照和声明的制品物化成独立、私有
的临时运行视图。文件必须是普通文件而非 symlink/hardlink，写入后
复核 SHA256，再将文件设为 `0444`、目录设为 `0555`；Runner 只允许
精确三文件或声明后的精确四文件。正常退出后清理视图；无法确认
子进程终止时移入受控 `debris cleanup`，不得立即删除仍可能被读取
的目录。平台不长期重复保存三份大型 CSV。

Input 报告必须分组记录三频父快照与平台注册制品，并记录父/组合
snapshot ID、Schema、各文件行列数与 SHA256、两个 manifest，以及
Request 的三个日期和三个截止键。

若存在上游自测报告，Input 报告还必须增加
`self_test_alignment`：

```text
status: matched | data_vintage_mismatch | not_provided
matched_generation_id
matched_refresh_date
business_file_hashes_match
api_wind_date_hash_match
request_calendar_mapping_match
upstream_downloaded_at
```

`matched` 只有在三频摘要、规范化日历摘要和 Request 周键映射全部一致
时成立；不能因为最终方向相同而反推输入已对齐。

`blackbox_v2/input_state.json` 必须记录 `generation_id`、`refresh_date`、business digest、环境指纹、父/组合 Snapshot 身份和制品 provenance；Input Gate 同时记录三份业务文件、平台注册制品摘要和 Request。授权段必须绑定该 input state，不能只凭三份 SHA256 推断 generation。

正常 `onboard --stage all` 结束后临时父快照和运行视图会删除。`input_state.json` 中的绝对路径只在执行期间有效，不能用于回放；Harness 报告保留组合 manifest，但平台不永久保存该次完整输入文件。

### 3.2 Request

Request 固定为 `request_id`、三个标准日期和三个频率 cutoff 共七个字段；批量上限 100、
`request_id` 唯一、cutoff 必须能在同一运行快照中唯一定位。字段类型、日期关系、as-of 计算和
批次合并顺序以 [Blackbox Contract](../architecture/SCHEME_CONTRACT.md) 与
[预测日期语义](../architecture/PREDICTION_SEMANTICS.md)为唯一规范，本 SOP 不复制字段表。

## 4. 自动 Harness

### 4.1 执行全部 Gate

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness onboard {scheme_id} \
    --predict-date YYYY-MM-DD \
    --stage all \
    --algo-env forecast_env_blackbox_v1
```

固定顺序：

```text
static -> input -> unit -> compare
```

任一 Gate 失败时 fail-fast，不进入后续 Gate，也不能执行 shadow 登记。

**自动 Gate 的验证边界**：平台只验证平台自己新增或修改的部分——数据接入、写出与平台侧
逻辑。交付代码自身的性质由上游按 [上游交付契约](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
保证，平台不重验：

| 性质 | 契约条款 | 平台是否重验 |
|---|---|---|
| 重复执行一致性 | [上游 SOP 第 8–9 节](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#8-output日志失败和确定性) | 否 |
| `predict` 与 `backtest` 结果一致 | [上游 SOP 第 5 节](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#5-实现同一个脚本的两个命令) | 否 |
| 不同批次大小、分区和顺序结果一致 | [上游 SOP 第 5 节](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#5-实现同一个脚本的两个命令) | 否 |
| 按截止键隔离未来数据 | [上游 SOP 第 6.3 节](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#63-对每个-request-独立截断) | 否 |

新增 Gate 只覆盖平台代码责任；交付脚本自身性质由上游契约和自验负责，不进入平台重复回归。

### 4.2 Gate 证据边界

| Gate | 当前检查 | 当前没有证明 | 主要证据 |
|---|---|---|---|
| `static` | 两文件、Metadata、新交付三字段、无新 `display_name`、owner composite readback、已声明 provider、语法、禁止 import/调用，以及 `/Users/`、`/home/`、Windows 盘符形式的绝对路径字面量 | 其他绝对路径、算法效果、全局文件读取隔离 | runtime、版本、Metadata、owner registry ID、`platform_inputs`、违规列表 |
| `input` | 三频 Schema、父/组合快照、平台注册制品、七字段 Request、三个截止键；如有上游自测则核对同代输入身份 | 当天 freshness、跨 generation 结果可比性 | 两类文件摘要、父/组合 ID、两个 manifest、Request、`self_test_alignment` |
| `unit` | help 暴露两个模式；一个非法 Request 失败且无 Output | 所有非法组合均被覆盖 | help、非法输入、失败无 Output |
| `compare` | 平台输入逐字节等于声明值；一次冒烟 predict 证明交付在平台喂进去的输入下产出合法 Result（原 dry-run 即此次调用） | 准确率、历史修订回放、跨 generation 逐行复现，以及**交付自身的性质**（重复执行确定性、predict/backtest 一致、截止隔离、跨请求无状态）——那些属上游义务 | PredictionRecord、组合 ID、结果路径 |
报告中的 `business_tables_written: false` 是声明性证据，不是数据库前后计数。激活后的
`DashboardGate` 只验证 `/api/factor-lab/dashboard` 当前业务读模型；它不属于 `all`，且
Dashboard payload 不含 exact version，因此不能替代生命周期、Registry 和数据库版本证据。

当前 `static` 对带 owner 的正式新交付验证 `name/owner/description`、禁止新
`display_name` override，并精确读回 owner registry；证据通过后才允许继续后续 Gate。
历史已入库且 Metadata 缺 owner 的不可变包只按显式 ID + Metadata SHA-256 兼容范围发现，StaticGate
不会要求为它们原地改写 Metadata，也不能把这种历史兼容当成新交付 waiver。

### 4.3 自动段副作用

`--stage all` 可以写：

- Gate 执行必需的临时输入和专项诊断产物；
- `t_harness_runs`、`t_harness_gate_results` 控制面审计记录。

它不得写 `t_scheme_runs`、`t_scheme_predictions`、`t_backtest_*` 业务记录、active Registry 或前端可见状态。

Harness 控制面持久化采用 fail-closed，数据库是 run/Gate 审计的唯一耐久来源；不再写 `{gate}.json` 或 `onboard_report.json` 本地镜像。run-start、任一 Gate 或 run-finish 持久化失败都会阻断本次 Harness。执行 shadow 前必须确认 exact `harness_run_id` 和 Blackbox 四个 Gate 已存在于审计数据库。

Result 解析器严格要求 JSON 的 `predicted_direction` 为整数 `-1/0/1`，拒绝字符串、布尔值和浮点数；CSV 继续按合同接受文本 token `-1/0/1`。

临时原始 Request、Result 和 stderr 当前随运行目录清理，不承诺长期留存；持久审计只以 `t_harness_runs` 和 `t_harness_gate_results.summary_json` 为准。Blackbox lineage 仍可通过 run 的目录型 `report_uri` 定位必要输入状态，该路径不是第二份 run/Gate 报告。

## 5. Shadow 登记

### 5.1 Shadow 前核验

从最新通过的数据库审计记录取得 `harness_run_id`、`scheme_version`、`predict_date`、四个 Gate 状态、snapshot ID 和三 SHA，并另外完成：

1. 重新运行环境自检；
2. 使用只读 SQL 确认 exact run 已写入审计 DB；
3. 再次检查 base/composite Registry 冲突；
4. 保存业务表和 active Registry 的前置计数；
5. 确认本轮通用入库只允许 `shadow + paused`；生产灰度必须另有具体方案专项授权。

Shadow Gate 绑定 all-stage 的环境指纹、generation、Snapshot 和 exact version/run；API/scheduler 不可见性仍需独立探针验证。

审计 DB 至少核对：

```sql
SELECT harness_run_id, scheme_id, scheme_version, stage, status
FROM t_harness_runs
WHERE harness_run_id = '<passed_harness_run_id>';

SELECT gate_name, status
FROM t_harness_gate_results
WHERE harness_run_id = '<passed_harness_run_id>'
ORDER BY id;
```

第一条必须恰好一行且为 exact scheme/version、`stage=all`、`status=passed`；第二条必须恰好覆盖 `static/input/unit/compare` 四个固定 Gate，并且全部为 `passed`。日常操作不需要手工查询这些字段：ShadowGate 会从数据库自动选择 current exact version 的 latest passed run 并逐项复核；上述 SQL 只用于诊断或独立审计。

### 5.2 Shadow 登记（首次身份自动创建）

一条命令完成登记。当方案的 base/composite 身份在生产 Schema 中**完全不存在**时，
`shadow-register` 先在 scheme-scoped MySQL advisory lock 下重检 exact
base/composite/version/Registry 全部不存在，再在单事务中 insert-only 写入
`t_scheme_versions` draft 与 composite `t_scheme_registry` paused 并精确 readback，
随后走既有的 shadow 迁移。身份已存在时（revision 路径）行为完全不变，不触碰创建路径。

任何冲突或 readback 不一致均回滚，禁止覆盖或 upsert。环境指纹与 snapshot ID 只取自
latest passed all-stage。创建前重读 canonical config 并逐字段比对身份，拒绝交付在
Gate 运行期间发生漂移。

本 Gate 不改业务表、不写 run/prediction/backtest、不激活，也不产生 scheduler 可执行身份。

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate shadow-register \
    --scheme-id {scheme_id} \
    --predict-date {latest_all_stage_predict_date}
```

该命令本身就是一次 shadow 操作授权。CLI 从 `config.yaml` 自动解析 exact version，Gate 自动选择
current exact version 的 latest passed `all` run，并把 scheme、action、predict date、version、run 与
operator 写入审计；不再存在密钥、token、nonce、有效期、replay store、`--scheme-version` 或
`--harness-run-id` 的人工传递。operator 默认取 `BFL_OPERATOR_ID` 或 OS 用户，需要稳定展示名时增加
`--operator {operator}`。

证据里的 `identity_created` 表明本次是否执行了首次创建：新方案为 `true`，
revision 路径为 `false`。

### 5.3 登记后独立检查

| 检查面 | 通过条件 |
|---|---|
| 配置 | `status=paused`，`version_status=shadow` |
| 版本表 | exact 交付摘要可追溯，存在 shadow 记录 |
| Registry | composite 行为 `paused + blackbox_v2` |
| 业务表 | trial 的正式 run、prediction、backtest run 和明细均未新增 |
| scheduler | 没有该 trial 的可执行 job |
| API/前端 | active 方案接口和前端矩阵不含 trial |
| 既有方案 | active 集合和原生方案状态未变化 |

全部通过后才记录为“完成 shadow 技术入库”。不得写成 active、正式上线、进入生产调度或已产生正式预测。

## 6. 专项授权后的完整回测持久化

本节不属于默认 Shadow 入库流程。只有具体方案已经完成生产准备核验并取得 `backtest_persist` 专项授权时才可执行；授权不得跨方案、版本、Harness run、预测截止日或回测起点复用。

### 6.1 日期范围和分批语义

完整持久化回测由日期区间定义，不由单批样本数定义：

- `--backtest-start-date` 默认 `2025-01-01`；周度方案的持久化回测必须
  精确使用该正式起点，其他频率只有在专项授权明确绑定时才可传入其它
  规范 ISO 日期；
- 生产授权前必须先在该方案的生命周期证据中登记 `gray_target_start`；`target_date >= gray_target_start` 全部属于实盘观察区。该起点必须来自方案级专项授权和证据，不得由算法 Metadata、部署日、历史批次默认值或操作当天自动推导；
- Backtest Gate 的 `predict_date` 参数承担 target 日期 exclusive cutoff，生产持久化时必须传已批准的 `gray_target_start`，只选择 `target_date < gray_target_start` 的历史样本；不得把激活日、`deployed_at` 或操作当天直接当作回测 cutoff；
- 最早样本是 `predict_date >= backtest_start_date` 的第一个合格站位日，起点本身不要求是交易日；
- Blackbox Backtest Gate 只接受明确的 `--persist`，不存在抽样认证或 `--sample-size`；
- 平台先生成完整 HistoricalCase 序列，再按 Runtime Profile 拆成每批最多 100 条；单批上限不是完整回测总量上限；
- 全部批次使用同一 scheme version、DataBridge generation、snapshot 和环境指纹，并共享一个总执行超时预算；
- 当前历史运行是 `current snapshot as-of replay`，不提供历史 vintage PIT，不得写成历史时点原貌复现。

### 6.2 直接命令的范围绑定

不再从报告抄写 `scheme_version + harness_run_id` 或签发 token。持久化命令必须显式写入实际
`gray_target_start` 和回测起点；CLI 自动绑定 canonical exact version，Gate 自动选择 latest passed
exact `all` run。`predict_date` 必须等于已登记的 `gray_target_start`，不是部署日期；任一日期非法、
current config 已漂移、passed run 缺失或 scope 不一致都必须 fail-closed。

### 6.3 执行完整持久化回测

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

平台必须在全部批次成功后，完成全区间数量、Request/Result 顺序、echo、日期唯一性和非空月度指标校验，再进入操作审计和写库。任一批次失败、超时或结果不合同时，不进入提交段，也不写 `t_backtest_*` 业务表。

提交段通过单一事务写入一个 immutable run、完整 prediction 明细和完整 monthly metrics，并把该 run 更新为 success。任一写入数量不匹配时整个事务回滚；数据库失败后必须重新执行完整命令，由 CLI 生成新的内部 operation id，不能复用半完成状态。

### 6.4 持久化后验收

验收不得再写死“100 条”，而应逐项比较动态预期：

| 检查 | 通过条件 |
|---|---|
| run 增量 | exact benchmark scope `+1` |
| prediction 增量 | `+完整 HistoricalCase 数`，且可以大于 100 |
| monthly metric 增量 | `+完整输出月数` 且非空 |
| 日期范围 | 最早站位日不早于起点；所有 `target_date < gray_target_start`，不得存在 `target_date >= gray_target_start` 的历史行 |
| 分批证据 | 每批不超过 100，总批次数、各批行数、总 deadline、最大/实际子进程数完整记录 |
| 版本与数据 | exact scheme version、Harness run、generation、snapshot 和环境指纹一致 |
| durable summary | 数据库 run summary 含授权起点/cutoff、实际 predict/target 边界、Request 总数、分批/预算和 replay semantics |
| API | canonical latest success 指向新完整 run |
| 历史 | 旧 run 保持不可变并可审计，不删除、不覆盖、不原地扩充 |

同一 all-stage run 可以通过新的明确命令追加新 run；默认 API 通过 canonical latest-success 规则选择最后成功记录。不得直接更新旧 run 或手工删除 100 条历史记录来伪造完整回测。

### 6.5 一次性批量结果复用快路径

一次性 batch 的资格、`gray_target_start` 分区和日期重建规则只由
[预测日期语义 5.2](../architecture/PREDICTION_SEMANTICS.md#52-一次性批量结果的分区与复用)定义。
平台执行时只保留四条不变量：

1. 先冻结 exact version、输入 identity/lineage、完整 Request 集和 Output 摘要；
2. `target_date < gray_target_start` 进入新的 immutable canonical backtest，其余合格缺口才可
   insert-only 物化为 `gray_live`；
3. live `predict_date` 必须按任务日历重建，禁止复制数据库主键、run、Actual、指标或 Harness 历史；
4. 已有任一业务键即整组拒绝，禁止 update/upsert、先删后写或第二套 SQL。

## 7. 生产激活、灰度补齐与前端验收

本阶段需要针对具体方案分别取得 activation、backtest/gray materialization 或单日补缺授权。
日期分区、gray/scheduled 阶段和 actual join 以
[预测日期语义](../architecture/PREDICTION_SEMANTICS.md)为准；自然调度证据以
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)为准；展示口径以
[灰度实验室说明手册](../product/GRAY_LAB_USER_MANUAL.md)为准。

平台最小闭环：

1. Activation 后读回 active exact version、全部 active composite Registry 和非空 `deployed_at`；
2. canonical backtest 只能包含 `target_date < gray_target_start`，旧 run 保持不可变；
3. 应补观察点按时间顺序经一次性 batch 快路径或单日 `signal-gap-fill` 写为 `gray_live`，不得直接 SQL；
4. 运行 `DashboardGate`，核对 active identity、backtest/live 零重叠、三日期、phase ranges、pending
   actual 和 Metadata/owner/description；前端不得用常量、覆盖或隐藏修饰错误数据；
5. `Onboarding Complete` 要求 Activation、完整 backtest、gray 补齐和 Dashboard/前端均通过；
   `Production Observed` 还必须由真实 one-shot 时钟产生至少一条成功 `scheduled_live`，并可从
   installed/loaded state、日志、run 和 prediction 共同追溯。

Activation 只建立业务生命周期，不授予调度安装、服务重启或生产 Writer 权限。

## 8. 失败恢复

| 场景 | 立即动作 | 允许继续的条件 |
|---|---|---|
| 环境自检失败 | 停止 Intake/Onboarding | 冻结环境恢复并重新自检 |
| scheduled-live DataBridge 失败 | 保留旧 generation 供审计，但本次自然运行禁止 fallback，阻断 V2 并告警 | 当天全新 generation 已 SEALED，仍满足 feature cutoff 且有执行预算 |
| 上游自测与平台输入摘要不同 | 标记 `data_vintage_mismatch`，停止算法结果归因 | 上游在平台选定的同一 generation 与规范化日历上重跑，完整输入身份一致 |
| 当前环境仍拒绝 Metadata `owner` | 原字节保管并停止 Intake；不得删字段代收 | 核对到包含方案 A 机器合同的精确开发版本并重新执行完整 Intake |
| owner composite 已登记为不同值 | Intake fail-closed，不覆盖 owner registry，不写方案文件 | 交付方确认正确归属并提交一致的新包，或按独立受控 owner correction 流程处理历史登记 |
| 新交付缺 `name`、`owner` 或 `description` | 拒绝 Intake/Gate，不推测、不硬编码、不使用占位值 | 上游重新提交三个展示字段均合法的完整两文件包 |
| Intake 失败 | 不手工拼方案目录，不改交付文件 | 清理未完成 trial 后重新 Intake |
| 自动 Gate 失败 | 保留报告，不执行副作用命令 | 问题修复后从 static 重跑全套 |
| 报告通过但审计 DB 缺失 | 不执行副作用命令 | exact run 和四个 Gate 完整持久化 |
| 临时快照残留 | 不交给下一次运行 | 无进程占用后清理并重建 |
| shadow 失败且 Registry/版本未变 | 核对操作审计和前置状态 | 仍为 draft/paused 且身份未占用 |
| shadow 失败但 Registry/版本已变 | 禁止自动重试或删除记录，pending 直接阻断 | 唯一 pending journal 已通过独立 `lifecycle-reconcile` 命令回退 previous safe state并完成 readback |
| API/scheduler 意外出现 trial | 保持 Registry paused，不执行 live | 找到来源并移除生产入口 |
| canonical backtest 含 gray target | 保留旧 run 审计，停止前端验收 | 用绑定 `gray_target_start` 的新直接命令生成 immutable run；不得靠前端裁剪收口 |
| 激活后 gray live 不连续 | 冻结该方案的完成状态，不伪造 `scheduled_live` | 按 target 日历逐日执行单日 `signal-gap-fill`；普通 `live` Gate 保持 fresh-only |
| active Registry 缺 `deployed_at` | API/前端 fail-closed | 通过正式 Registry reconciliation 恢复真实部署日期 |
| backtest/live 同一 target 重叠 | 阻断上线，保留冲突清单 | 新回测 run 或正式 correction 使 target 分区互斥后重验 |

Blackbox lifecycle journal 一旦存在 pending，新的 shadow、activate 或 revision activate 都必须
直接阻断，不得在其它命令前隐式恢复。只有独立 `gate lifecycle-reconcile` 命令可以处理唯一 pending
journal：它只回退到 previous safe state，保留原 journal，并新增 linked reconciliation journal；
多个 pending 或恢复失败继续阻断并转人工核查。数据库与配置文件不能组成单一事务，因此命令
异常后仍必须执行三方只读对账：

1. 不重试原 lifecycle 动作，保存命令、报告和只读查询结果；
2. 确认只有一个 pending journal，并核对其 previous safe state；
3. 显式运行 `gate lifecycle-reconcile --scheme-id ...`；CLI/Gate 自动绑定 exact scheme/version/latest passed run 与 operator；
4. 回读 safe state、原 journal 与新 linked journal；
5. 多个 pending、恢复失败或 readback 不一致时继续阻断并转人工核查，不手工删除历史版本。

## 9. 最终检查

这里只核对不能由其它证据替代的终态；字段、输入、Gate、事务和恢复细项以本 SOP 对应章节的机器输出为准：

- [ ] 两文件通过原子 Intake，正式 Metadata、owner、平台输入声明合法，候选保持 `blackbox_v2 + paused + draft`。
- [ ] exact generation、combined snapshot、Request cutoff 与环境指纹一致；四段自动 Gate 已持久化且未写业务表。
- [ ] 如执行 shadow，直接命令绑定 current exact version/latest passed run，读回为 `shadow + paused` 且未进入自然调度。
- [ ] 如执行持久化回测或一次性 batch，授权、`gray_target_start`、canonical 分区、live 日期重建和零重叠均满足第 6 节。
- [ ] Activation 使用独立专项授权；exact version、Registry、配置和 lifecycle journal 读回一致，不存在未处理 pending。
- [ ] `gray_live`、`scheduled_live`、installed 控制面和自然观察证据没有互相冒充；repo 状态不作为部署或运行证据。
- [ ] DashboardGate、数据库/API/前端口径通过；exact version 另由 lifecycle 与数据库权威回读证明。

具体方案的 generation、snapshot、Harness run、预测结果和数据库计数保留在现有控制面；稳定摘要进入
`CURRENT_STATUS.md`，未闭环事项进入 `TODO.md`，不为单次入库另建规划或交接文档。
