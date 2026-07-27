# Blackbox V2 平台入库 SOP（Contract 1.0，Intake、生产灰度与前端验收）

**文档状态**：`CURRENT`
**适用运行时**：`blackbox_v2`
**目标读者**：平台入库、运行和审计人员
**最后核验日期**：2026-07-26

本文是平台操作人员接收、技术验收和登记 Blackbox V2 方案的唯一操作 SOP。上游交付契约见 [BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)；具体方案的版本、快照、运行结果和当前状态只追加到 [Blackbox V2 入库试验台账](../blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md)。文档分类和维护规则见 [Blackbox V2 文档管理](../blackbox_v2/README.md)。

本文的通用入库流程止于 `shadow + paused`，不自动授予生产运行权限。`activate`、回测落库和 `live` 已有独立签名门禁，但只能在完成[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)核验并取得具体方案专项授权后执行；不得把某个试验方案的授权外推为所有新方案的默认权限。具体生产灰度记录只写入平台试验台账。

## 1. Intake 与身份

### 1.1 接收前检查

交付目录必须恰好包含：

```text
{scheme_id}.py
{scheme_id}.json
```

执行 Intake 前确认：

- 两个条目均为普通文件，不是目录或符号链接；
- 文件名与 Metadata 中的 `scheme_id` 一致；Metadata 有历史八字段，并提供正式新交付必填的 `description`；
- Metadata 不含 `platform_inputs`；该字段属于 Intake 生成的平台配置，不属于上游合同；
- `.py` 是唯一可执行内容，不存在模型、配置、依赖或辅助模块；
- trial 的 base `scheme_id` 和 composite Registry ID 均未占用；
- 同一算法已有原生实现时使用独立 trial ID，不覆盖原方案。

上游工作目录可以含自验用 `api_wind_date.csv`、sample 或交接材料，但不得把这些内容交给 Intake。平台必须先复制精确 `.py + .json` 到私有临时目录，并确认该目录恰好两个普通文件；上游随包日历永不成为正式运行输入。

记录收到文件的原始摘要：

```bash
shasum -a 256 <delivery-dir>/{scheme_id}.py <delivery-dir>/{scheme_id}.json
```

### 1.2 执行 Intake

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

名称、算法版本、期限、任务类型、horizon 和 target rule 只能来自 Metadata。`blackbox-v2-v1` 是运行 profile；`forecast_env_blackbox_v1` 是该 profile 当前引用的 conda 环境，两者不得混称。

`description` 是正式新交付的必填算法逻辑摘要。为兼容已有不可变交付，机器兼容 Intake 对历史八字段包仍可能以退出码 `0` 返回，并在机器 JSON 的 `warnings` 数组提示缺少说明；机器兼容 Intake 不等于正式收包通过。平台必须在 Gate 前 fail-closed，拒绝任何缺少 `description` 的正式新交付。说明必须是单段非空纯文本、最多 300 个字符，换行、`<`、`>`、空字符串或错误类型均拒绝。

已有方案缺少 `description` 时不修改只读 Metadata、不推测算法逻辑，也不制造新版本；只有在专项批次记录中写明 `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` 的既有交付，才可按其原有授权范围继续发现、Gate 和运行。缺失说明在平台配置中映射为空字符串。

### 1.3 对照 Contract 1.0

机器契约 `shared.blackbox_v2.contracts` 是字段和组合的判定源；平台注册输入则由 Intake 参数和 `config.yaml` 表达，不写入 Metadata：

- Metadata 的历史机器基线包含八字段：`schema_version`、`scheme_id`、`name`、`algorithm_version`、`target_tenor`、`task_type`、`horizon`、`target_rule`；正式新交付还必须有 `description`，其他额外字段（包括 `platform_inputs`）继续 fail-closed。
- Request 恰好七字段：`request_id`、`predict_date`、`feature_date`、`target_date`、`daily_cutoff_key`、`weekly_cutoff_key`、`monthly_cutoff_key`。
- Result 恰好五字段：`request_id`、`predict_date`、`feature_date`、`target_date`、`predicted_direction`。

| `task_type` | `horizon` | `target_rule` |
|---|---:|---|
| `T+1` | 1 | `target_date_yield_vs_feature_date_yield` |
| `T+5` | 5 | `target_date_yield_vs_feature_date_yield` |
| `weekly_point` | 1 | `target_week_end_yield_vs_feature_week_end_yield` |
| `weekly_average` | 1 | `target_week_average_yield_vs_feature_week_average_yield` |
| `monthly` | 1 | `target_month_observation_yield_vs_feature_month_observation_yield` |

字段数、名称或固定组合不一致时 Intake 必须失败，不得在平台配置中纠正上游 Metadata。

### 1.4 算法说明的展示链路

显式 `description` 必须按以下单向链路传播：

```text
{scheme_id}.json.description
→ SchemeConfig.description
→ t_scheme_registry.description
→ /api/schemes 与 /api/backtests/factor-lab
→ 灰度实验室前端备注
```

平台复用现有 `t_scheme_registry.description`，不新增备注表或算法 Request 字段。前端只展示经过文本转义的说明；不得把它当作 HTML，也不得用方案名、任务格子、部署状态或平台运营意见填充空说明。Metadata 中的说明发生变化时，其文件摘要和 canonical `scheme_version` 必须随之变化。

## 2. 环境与数据 Preflight

### 2.1 验证冻结环境

以下机器资产是对外发布和 Preflight 的冻结基准，不在 SOP 中硬编码 Python 版本：

```text
deploy/blackbox_v2/runtime_profile_v1.json
deploy/blackbox_v2/environment_manifest.json
```

`data-bridge-v1` 的机器 Schema 和脱敏结构样例入口见 [DataBridge V1 数据契约与样例](../blackbox_v2/data_bridge_v1/README.md)。

执行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/verify_blackbox_v2_environment.py

conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/probe_blackbox_v2_sandbox.py
```

执行器从版本化 JSON 加载 Runtime Profile，并在启动时严格校验字段、类型和安全边界。环境 manifest 用于核验实际环境指纹；方案配置不得覆盖 profile 的算法执行资源、读路径、环境变量或网络权限。Profile、manifest 或实际环境任一漂移时停止验收。

记录环境清单摘要和自检时间。环境不一致、资源基准漂移、sandbox 网络拒绝或数据目录写保护失效时，不得继续。CPU、内存、predict/backtest 超时、100 条批量上限、Output 和日志大小上限以核对一致后的实际执行值为准。

### 2.2 验证 DataBridge current

```bash
BOND_DAILY_COORDINATOR_MODE=legacy \
  conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/refresh_data_bridge_current.py --check-only \
    --date <selected-generation-refresh-date>
```

mode 必须按当前实际部署显式给出；示例使用尚未切换生产时的 `legacy`。ledger
部署只能以 `BOND_DAILY_COORDINATOR_MODE=ledger` 调用同一个 `--check-only`
入口；独立 `--publish`、`--dry-run` 和 scheduler `--run-once data-refresh`
都不得成为 ledger 刷新旁路。

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

### 2.3 每日 generation、occurrence 与 V2 释放时序

DataBridge 只服务 `runtime_type=blackbox_v2` 日频执行，Native 使用独立的
`native_source` generation。两者共享同一 occurrence 和冻结日历证据，但互不以
前一个算法任务的成功作为释放条件。

原 `com.bond-factor-lab.v2-preflight` 的精确分钟触发和 scheduler restart
只保留为 legacy 运行/回滚代码；仓库默认仍为 legacy 并保留 calendar trigger，
以保证切换前每日只有一个 DataBridge refresh owner。正式切到 ledger 前必须
先 unload 该 job，同时将 backend 与 scheduler 的 mode 一并改为 `ledger`；
ledger 模式下该入口即使被误调用也返回 `disabled`。ledger 时序统一由单一
coordinator 管理，时区为 `Asia/Shanghai`：

| 时间 | 平台动作 | 通过条件与后续动作 |
|---|---|---|
| `06:30` | 创建 occurrence，冻结 active daily Registry；并行构建 Native generation、发起当天全新 DataBridge 全量刷新 | 两条输入链分别封存，不读取昨日或旧 `current` |
| DataBridge `sealed_at` | 计算四个 V2 release | 分别在 `+0/+2/+4/+6` 分钟释放；前一个失败不阻断后一个 |
| `07:00` | 进度 watchdog | 只检查 ETA、无进展和恢复资格，不重启 scheduler |
| `07:45` | V2 start guardrail | 未启动 item 永久记 `sla_status=LATE` 并告警，仍可使用当天 generation 继续执行 |
| `08:00` | target SLA | 冻结 target 少一个即 write-once `BREACHED`；后来补齐不改回 `MET` |
| `08:30` | recovery cutoff | 不再启动新 attempt 或自动重试；不跨日自动补跑 |

每个 V2 item 必须绑定 occurrence 中同一个当天 DataBridge generation，以及该
generation 关联的同日 Native calendar generation。generation ID、manifest
SHA、business/feature date 或关联摘要任一漂移时 fail-closed。旧 generation
即使结构完整也不能作为 scheduled-live fallback。

四个 V2 使用独立执行池，最大并发 2，单 attempt 硬超时 120 秒。执行结果只能
通过 occurrence item 的 fenced 原子提交入口发布；manual/background/gray
运行不得伪造 `scheduled_live`。

该候选路径的 rollout 在迁移、Native 输入适配、同机容量、故障注入和连续运行
门禁通过前保持关闭。完整不变量见
[日频信号 08:00 SLA 架构](../architecture/DAILY_SIGNAL_SLA.md)。

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

Harness/check-only 通过只读 DB capture 读取权威 `api_wind_date`；
scheduled 路径只使用已与 DataBridge generation 核对 ID 和 manifest
SHA 的 Native generation 冻结帧。两条路径使用同一个 provider
规范化内容，但在 `source provenance` 中分别记录来源。来源类型、
generation ID、捕获时间和临时路径仅进入 `audit_manifest`，不参与
内容身份。

组合对象同时记录 `combined_snapshot_id`、`parent_snapshot_id`、
三频父快照、已排序 `platform_inputs`、制品摘要、`identity_manifest`
和 `audit_manifest`。没有平台注册制品时，组合 ID 直接沿用父
snapshot ID；有制品时只对身份 schema 版本、父 ID，以及按 ID 排序
的 artifact ID、provider version、文件名、SHA256、大小、行数和列
计算组合 ID。因此相同父快照和相同规范化日历跨 DB capture/Native
generation 具有相同 `combined_snapshot_id`。

每次子进程运行前，平台把三频父快照和声明的制品物化成独立、私有
的临时运行视图。文件必须是普通文件而非 symlink/hardlink，写入后
复核 SHA256，再将文件设为 `0444`、目录设为 `0555`；Runner 只允许
精确三文件或声明后的精确四文件。正常退出后清理视图；无法确认
子进程终止时移入受控 `debris cleanup`，不得立即删除仍可能被读取
的目录。平台不长期重复保存三份大型 CSV。

Input 报告必须分组记录三频父快照与平台注册制品，并记录父/组合
snapshot ID、Schema、各文件行列数与 SHA256、两个 manifest，以及
Request 的三个日期和三个截止键。

`blackbox_v2/input_state.json` 必须记录 `generation_id`、`refresh_date`、business digest、环境指纹、父/组合 Snapshot 身份和制品 provenance；Input Gate 同时记录三份业务文件、平台注册制品摘要和 Request。授权段必须绑定该 input state，不能只凭三份 SHA256 推断 generation。

正常 `onboard --stage all` 结束后临时父快照和运行视图会删除。`input_state.json` 中的绝对路径只在执行期间有效，不能用于回放；Harness 报告保留组合 manifest，但平台不永久保存该次完整输入文件。

### 3.2 Request

平台根据 Metadata、统一日历和日期语义生成恰好七字段：

```text
request_id
predict_date
feature_date
target_date
daily_cutoff_key
weekly_cutoff_key
monthly_cutoff_key
```

必须满足：

- 日期关系和字段类型通过机器契约；
- 批量一至 100 行，全批次 `request_id` 唯一；
- 日截止键是快照中不晚于 `feature_date` 的最后一个日频键；
- 周/月截止键来自平台权威 as-of 口径，不按 ISO 周或年月直接推导；
- 三个截止键都能在同一个运行快照中唯一定位。

更大回测由平台切分成不超过 100 条的批次，合并时恢复原 Request 顺序，切分方式不得改变结果。

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
static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness
```

任一 Gate 失败时 fail-fast，不进入后续 Gate，不签发 shadow 授权。

仅做技术入库准备、要求生产数据库零写入时，必须使用正式
`--check-only` 编排：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness onboard {scheme_id} \
    --predict-date YYYY-MM-DD \
    --stage all \
    --check-only \
    --algo-env forecast_env_blackbox_v1 \
    --timeout-sec 1800
```

`--check-only` 仍使用只读 Engine 构造平台日历、三个 cutoff 和
Request，仍按固定七 Gate 顺序 fail-fast，并生成本地
`harness_run_id`、逐 Gate JSON 与统一报告；但完全不调用 Harness
控制面 run/gate 持久化，也不写任何业务表。统一报告必须同时写明：

```text
check_only=true
control_plane_persisted=false
business_tables_written=false
persist_backtest=false
```

该模式只允许 `--stage all`，且与授权 token、持久化 backtest、
shadow、activate、gray/live 和真实 API 等任何副作用阶段不兼容；
出现组合参数时必须 fail-closed。Backtest Gate 固定执行 100 条
no-persist 验收，必须得到 `100/100` 且 `persist=false`；
`api-readiness` 仍只做结构验证，不访问真实 Registry、HTTP API 或
scheduler。

### 4.2 Gate 证据边界

| Gate | 当前检查 | 当前没有证明 | 主要证据 |
|---|---|---|---|
| `static` | 两文件、Metadata、已声明 provider、语法、禁止 import/调用，以及 `/Users/`、`/home/`、Windows 盘符形式的绝对路径字面量 | 其他绝对路径、算法效果、全局文件读取隔离 | runtime、版本、Metadata、`platform_inputs`、违规列表 |
| `input` | 三频 Schema、父/组合快照、平台注册制品、七字段 Request、三个截止键 | 当天 freshness、scheduled generation 映射 | 两类文件摘要、父/组合 ID、两个 manifest、Request |
| `unit` | help 暴露两个模式；一个非法 Request 失败且无 Output | 所有非法组合均被覆盖 | help、非法输入、失败无 Output |
| `dry-run` | 单点 predict、Result 校验、内存 `PredictionRecord` | 已写预测表或已进入业务 API | PredictionRecord、组合 ID、结果路径 |
| `compare` | 重复、predict/backtest、分批、顺序、后续业务行隔离；平台制品哈希不变 | 准确率、历史修订回放 | 五类一致性证据、`platform_input_hashes_unchanged=true` |
| `backtest` | 100 条全部返回、no-persist、组合输入一致 | 大于 100 条单进程能力、效果门槛 | 请求/结果数量、组合 ID、persist=false |
| `api-readiness` | composite 身份和结果结构兼容 | 真实 Registry、HTTP API 或 scheduler 探针 | registry ID、结构结果 |

报告中的 `business_tables_written: false` 是声明性证据，不是数据库前后计数。`api-readiness` 中的 scheduler/API 状态也是结构预期，不能单独证明生产不可见。

`api-readiness` 按生命周期区分两种结构模式：首次入库的 `paused` 方案使用 `pre_shadow`，预期 scheduler/API 不可见；已经专项激活的方案重新执行 all-stage 时使用 `active_recertification`，要求 `status=active + version_status=active`，并把 scheduler/API 可见性记录为 active 预期。两种模式都只做内存结构验证，不在自动段写业务表，也不能替代正式 HTTP、Registry 或 scheduler 探针。

### 4.3 自动段副作用

普通 `--stage all`（没有 `--check-only`）可以写：

- `reports/harness/{scheme_id}/...`；
- `t_harness_runs`、`t_harness_gate_results` 等控制面审计记录。

它不得写 `t_scheme_runs`、`t_scheme_predictions`、`t_backtest_*` 业务记录、active Registry 或前端可见状态。

Harness 控制面持久化采用 fail-closed。即使 `onboard_report.json` 为 `overall_passed=true`，仍必须确认 exact `harness_run_id` 和七个 Gate 已存在于审计数据库，才能授权 shadow。

`--check-only` 恰好相反：它不得尝试写
`t_harness_runs`/`t_harness_gate_results`，本地通过报告也不能用于
签发 shadow 或任何生产授权。两种模式的报告不得混称。

Result 解析器严格要求 JSON 的 `predicted_direction` 为整数 `-1/0/1`，拒绝字符串、布尔值和浮点数；CSV 继续按合同接受文本 token `-1/0/1`。

临时原始 Request、Result 和 stderr 当前随运行目录清理，不承诺长期留存；持久审计以 Harness 报告、摘要和结构化记录为准。

## 5. Shadow 登记

### 5.1 授权前核验

从最新通过报告取得 `harness_run_id`、`scheme_version`、`predict_date`、七个 Gate 状态、snapshot ID 和三 SHA，并另外完成：

1. 重新运行环境自检；
2. 使用只读 SQL 确认 exact run 已写入审计 DB；`python -m harness report {scheme_id} --latest` 只读取本地最新报告，不能代替数据库核验；
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

第一条必须恰好一行且为 exact scheme/version、`stage=all`、`status=passed`；第二条必须恰好覆盖七个固定 Gate，并且全部为 `passed`。

### 5.2 首次 Draft 登记

生产 Schema 已包含其它方案、但当前 Blackbox 的 base/composite 身份完全不存在
时，先使用独立的 insert-only Gate 登记 `draft + paused`。必须启用
`HARNESS_AUTH_SECRET`，并使用非空 operator、最长 900 秒且绑定 latest persisted all-stage exact scheme/version/run/predict date 的一次性 token：

```bash
TOKEN=$(conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness auth issue \
    --scheme-id {scheme_id} \
    --action draft_register \
    --predict-date {latest_all_stage_predict_date} \
    --scheme-version {passed_scheme_version} \
    --harness-run-id {passed_harness_run_id} \
    --issued-by {operator} \
    --expires-in 900)

conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate draft-register \
    --scheme-id {scheme_id} \
    --predict-date {latest_all_stage_predict_date} \
    --authorize "$TOKEN"
```

`draft-register` 只允许 `blackbox_v2` 的 `paused + draft` config。它在
scheme-scoped MySQL advisory lock 下重检 exact base/composite/version/Registry
全部不存在，再在单事务中 insert-only 写入 `t_scheme_versions` draft 与
composite `t_scheme_registry` paused，并精确 readback；任何冲突或 readback
不一致均回滚，禁止覆盖或 upsert。环境指纹和 snapshot ID 只取自该 latest
passed all-stage。此 Gate 不改 config，不写 run/prediction/backtest，不激活，
也不产生 scheduler 可执行身份。

### 5.3 签发并使用 Shadow 授权

```bash
TOKEN=$(conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness auth issue \
    --scheme-id {scheme_id} \
    --action shadow_register \
    --predict-date YYYY-MM-DD \
    --scheme-version {passed_scheme_version} \
    --harness-run-id {passed_harness_run_id} \
    --expires-in 900)

conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate shadow-register \
    --scheme-id {scheme_id} \
    --predict-date YYYY-MM-DD \
    --authorize "$TOKEN"
```

Token 必须绑定 exact scheme、action、predict date、version 和 Harness run，且使用短有效期，不得跨方案或跨 run 复用。

### 5.4 登记后独立检查

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

- `--backtest-start-date` 默认 `2025-01-01`，也可以显式传入其它规范 ISO 日期；
- 生产授权前必须先在该方案的生命周期证据中登记 `gray_target_start`；当前生产灰度观察基线为 `2026-06-01`，即 `target_date >= gray_target_start` 全部属于实盘观察区；后续如使用不同起点，必须有方案级专项授权和证据，不得由算法 Metadata、部署日或操作当天自动推导；
- Backtest Gate 的 `predict_date` 参数承担 target 日期 exclusive cutoff，生产持久化时必须传已批准的 `gray_target_start`，只选择 `target_date < gray_target_start` 的历史样本；不得把激活日、`deployed_at` 或操作当天直接当作回测 cutoff；
- 最早样本是 `predict_date >= backtest_start_date` 的第一个合格站位日，起点本身不要求是交易日；
- `--sample-size` 只用于自动段的 no-persist 稳定性测试，和 `--persist` 同时使用时拒绝执行；
- 平台先生成完整 HistoricalCase 序列，再按 Runtime Profile 拆成每批最多 100 条；单批上限不是完整回测总量上限；
- 全部批次使用同一 scheme version、DataBridge generation、snapshot 和环境指纹，并共享一个总执行超时预算；
- 当前历史运行是 `current snapshot as-of replay`，不提供历史 vintage PIT，不得写成历史时点原貌复现。

### 6.2 签发范围绑定授权

从最新通过的 all-stage 记录取得 exact `scheme_version + harness_run_id`。签发 token 时必须写入实际回测起点：

```bash
TOKEN=$(conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness auth issue \
    --scheme-id {scheme_id} \
    --action backtest_persist \
    --predict-date {gray_target_start} \
    --backtest-start-date 2025-01-01 \
    --scheme-version {passed_scheme_version} \
    --harness-run-id {passed_harness_run_id} \
    --expires-in 900 \
    --issued-by {operator})
```

`backtest_persist` token 必须同时包含规范且非空的 `predict_date` 与 `backtest_start_date`。此处 token 的 `predict_date` 必须等于已登记的 `gray_target_start`，不是部署日期。Gate 参数和 token 中的 exclusive cutoff、起点都必须完全一致；CLI 缺少 `--predict-date` 时拒绝签发，旧 token、任一日期缺失或不匹配、过期、已消费或签名不正确都必须 fail-closed。

### 6.3 执行完整持久化回测

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate backtest \
    --scheme-id {scheme_id} \
    --predict-date {gray_target_start} \
    --persist \
    --backtest-start-date 2025-01-01 \
    --timeout-sec 1800 \
    --algo-env forecast_env_blackbox_v1 \
    --authorize "$TOKEN"
```

平台必须在全部批次成功后，完成全区间数量、Request/Result 顺序、echo、日期唯一性和非空月度指标校验，再进入授权审计和写库。任一批次失败、超时或结果不合同时，不消费尚未进入提交段的 token，不写 `t_backtest_*` 业务表。

提交段通过单一事务写入一个 immutable run、完整 prediction 明细和完整 monthly metrics，并把该 run 更新为 success。任一写入数量不匹配时整个事务回滚；token 已进入提交段后即视为已消费，数据库失败重试必须重新签发。

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

同一 all-stage run 可以使用新 token 重新执行并追加新 run；默认 API 通过 canonical latest-success 规则选择最后成功记录。不得直接更新旧 run 或手工删除 100 条历史记录来伪造完整回测。

## 7. 生产激活、灰度补齐与前端验收

本节适用于取得具体方案 `blackbox_activate`、`live_write`、`gray_backfill_write` 等专项授权后的生产动作。Shadow 完成不等于实盘；Activation 成功、Registry 变为 active 且方案挂载生产任务时，才表示方案部署进入实盘链路。部署上去的那一刻即属于实盘运行状态，不能继续把方案描述为仅有历史回测，也不能等待下一次 scheduler 后才补前端实盘段。

### 7.1 灰度起点、部署时间和正式调度起点

三个边界必须分开记录：

| 边界 | 判定源 | 业务用途 |
|---|---|---|
| `gray_target_start` | 方案生命周期专项授权；当前生产灰度基线为 `2026-06-01` | 按 `target_date` 切分历史回测与实盘观察区 |
| `deployed_at` | Activation 后 active composite Registry 的真实部署日期 | 前端“部署时间”和生产挂载审计 |
| 正式调度起点 | scheduler 自然成功写入的第一条 `prediction_phase=scheduled_live` 的 `predict_date` | 区分灰度实盘和正式 scheduler 实盘 |

强制语义：

- 灰度实盘也属于实盘，使用 `prediction_phase=gray_live`；正式 scheduler 自然发出的实盘使用 `prediction_phase=scheduled_live`；
- 历史回测只允许 `target_date < gray_target_start`，所有 `target_date >= gray_target_start` 的应有预测必须进入 `t_scheme_predictions`，不得进入 canonical latest backtest；
- `deployed_at` 表示方案真正激活并挂载生产任务的日期，active Registry 必须非空；它不参与回测截断、灰度补齐范围、月份归属、actual join 或预测唯一键计算；
- 方案可以在 `gray_target_start` 之后才部署，因此 gray live 的 `predict_date` 可以早于 `deployed_at`；这是按历史应发时点补齐观察序列，不是伪造部署时间；
- 正式调度起点只能由自然 scheduler 成功记录证明，不能用 Activation 时间、`deployed_at` 或第一条手工 gray live 代替。

### 7.2 激活后强制补齐 gray live

Activation 完成后、前端验收前，必须按时间顺序补齐从 `gray_target_start` 到当前所有应有的实盘目标点。当前生产灰度基线下，从 `target_date=2026-06-01` 起就属于灰度实盘。

补齐必须先枚举 `target_date >= gray_target_start` 的目标点，再按平台日历反推 `feature_date` 和应发 `predict_date`，不能从部署日向后枚举：

- 日频 T+N：`feature_date` 是 `target_date` 前第 N 个交易日，`predict_date` 是 `feature_date` 的下一交易日；例如 T+5 的首个灰度目标 `2026-06-01` 对应 `feature_date=2026-05-25`、`predict_date=2026-05-26`；
- 周频：先枚举应有目标周末，再反推上一轮调度日；`predict_date` 可能位于 5 月；
- 月频：按目标月观察点反推自然触发日；若合同规定自然 15 号，不能顺延为交易日。

普通 `live` Gate 仍是 fresh-only：它只接受 `live_write` token，并要求 DataBridge `refresh_date` 等于本次运行日。历史 gray 缺口只能使用显式 `gray-backfill` Gate；不得放宽普通 LiveGate、伪造 DataBridge freshness 或直接调用 repository 绕过授权。

每个补齐点必须使用独立、范围匹配的一次性 `gray_backfill_write` token，并显式指定 `prediction_phase=gray_live`。token 必须绑定 canonical exact `predict_date`、scheme、version 和最新通过的 all-stage run，TTL 不超过 900 秒；缺失日期、日期不匹配、跨 Gate 使用、过期或重放全部拒绝。

```bash
TOKEN=$(conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness auth issue \
    --scheme-id {scheme_id} \
    --action gray_backfill_write \
    --predict-date {historical_signal_date} \
    --scheme-version {passed_scheme_version} \
    --harness-run-id {passed_harness_run_id} \
    --expires-in 900 \
    --issued-by {operator})

conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate gray-backfill \
    --scheme-id {scheme_id} \
    --predict-date {historical_signal_date} \
    --prediction-phase gray_live \
    --algo-env forecast_env_blackbox_v1 \
    --authorize "$TOKEN"
```

`gray-backfill` 使用 `historical_as_of_replay` 输入模式：完整校验当前 DataBridge 后，允许历史 `predict_date` 读取当前同代快照，但 Request 的三个 cutoff key 仍硬截止在该点的 `feature_date`。该结果必须标记 `current_snapshot_as_of_not_historical_vintage`，只能解释为当前快照上的 live-safe as-of replay，不能宣称历史 vintage PIT。Gate 在预检与实际快照之间 pin `generation_id + refresh_date`；切代即失败。prediction `extra` 必须持久化 snapshot、generation、refresh、三频 cutoff、replay semantics 和 backfill 时间。

历史 gray 写入采用 insert-only，并依赖 `uk_scheme_tenor_target` 原子拒绝重复 target；不得进入 `ON DUPLICATE KEY UPDATE`。预检已存在、竞争事务冲突、算法失败、provenance 缺失或 Gate 表增量不是 run/prediction/log 精确各 `+1` 时，事务失败且不能覆盖首条预测。补齐产生的 run、prediction 和 run log 必须一一对应；任一点失败时冻结当前方案的后续补齐，不得把缺口留给前端隐藏。

Activation 当天还必须为当前可运行点执行至少一次受控 `gray_live`，证明部署时刻已经进入实盘链路。后续只有 scheduler 在真实时钟自然触发的成功预测才能标为 `scheduled_live`。

### 7.3 API 与前端展示契约

前端要求属于平台入库验收，不属于上游算法交付契约。平台必须同时核验 `/api/schemes`、`/api/backtests/factor-lab` 和 `/api/metrics/{registry_scheme_id}`：

1. `/api/schemes` 的 active composite row 必须包含真实、非空的 `deployed_at`；前端候选排行的“部署时间”只能来自该字段，不得使用 hardcoded 日期、默认值或 scheme ID 特判。
2. `/api/backtests/factor-lab` 的 canonical latest-success 明细必须全部满足 `target_date < gray_target_start`；前端不得靠裁剪或覆盖历史行来掩盖错误的回测落库。
3. `/api/metrics/{registry_scheme_id}` 必须返回全部 gray/scheduled live 明细、标准三日期、`prediction_phase` 和 `phase_ranges`；`phase_ranges` 至少能分别表达灰度实盘区间和正式调度起点。
4. 前端详情的阶段分隔文案统一只表达正式实盘的目标日期起点。存在 `scheduled_live` 时显示 `▼ 实盘预测目标区间：{scheduled_live.start_target_date}开始`，日期取 `phase_ranges` 中 scheduled 行的 `start_target_date`；尚不存在 `scheduled_live` 时显示 `▼ 实盘预测目标区间：待产生`。不得使用 `predict_date`、`feature_date`、gray 端点或 `deployed_at` 替代该目标起点；“部署时间”继续单独显示 Registry 的 `deployed_at`。
5. 回测和 live 统一按 `target_date` 归属月份。“全部”口径必须在第一条 live target 月前插入实盘分隔线；分隔线之前不得包含 `target_date >= gray_target_start` 的回测，之后不得遗漏应有的 gray live。
6. 同一方案、同一 `target_date` 同时出现在 backtest 与 live 是数据分区失败，必须阻断上线；不得通过前端同月追加、覆盖、去重或隐藏其中一侧宣称验收通过。
7. actual 尚未到达的 live target 显示“待验证”，计入展示样本数，但不进入准确率分母；不得人工补 actual，也不得把 pending 显示成预测错误。
8. 任务格子只由 Registry 的 `target_tenor + task_type` 决定；候选名称使用 Metadata 的简短 `name`，不得重复任务说明、目标名称或公开内部 scheme version。
9. “仅回测”“仅实盘”“全部”三个口径必须与 DB/API 明细逐行一致；候选样本数、月度指标、每日明细、phase 标签和最新运行日期均可追溯。
10. 浏览器强制刷新后候选数和名称正确，控制台错误为 0；如静态资源有变更，必须同步资源版本，不能把缓存页面当成通过证据。

前端验收失败时先查 Registry、canonical backtest 和 live 明细的真实分区。禁止在前端增加日期常量或方案特判来修饰结果；平台数据修正必须走新的授权 run、正式 reconciliation 或受控 correction 流程。

### 7.4 生产完成状态

- **Onboarding Complete**：Activation、完整历史回测、从 `gray_target_start` 起的 gray live 补齐、API 和前端验收、scheduler 挂载均通过；不要求已经观察到自然调度。
- **Production Observed**：在 Onboarding Complete 基础上，scheduler 真实时钟自然产生至少一条成功 `prediction_phase=scheduled_live`，并能从 run、log、prediction、API 和前端追溯。

仅 active 但未补齐灰度实盘、前端仍混入灰度 target 的回测、缺 `deployed_at` 或尚未挂载 scheduler，都不得标记为 Onboarding Complete。

## 8. 失败恢复

| 场景 | 立即动作 | 允许继续的条件 |
|---|---|---|
| 环境自检失败 | 停止 Intake/Onboarding | 冻结环境恢复并重新自检 |
| scheduled-live DataBridge 失败 | 保留旧 generation 供审计，但本 occurrence 禁止 fallback，阻断 V2 并告警 | 当天全新 generation 在 08:30 前 SEALED 且仍有执行预算 |
| Intake 失败 | 不手工拼方案目录，不改交付文件 | 清理未完成 trial 后重新 Intake |
| 自动 Gate 失败 | 不签发 token，保留报告 | 问题修复后从 static 重跑全套 |
| 报告通过但审计 DB 缺失 | 不签发 token | exact run 和七个 Gate 完整持久化 |
| 临时快照残留 | 不交给下一次运行 | 无进程占用后清理并重建 |
| shadow 失败且 Registry/版本未变 | 核对 token、审计和前置状态 | 仍为 draft/paused 且身份未占用 |
| shadow 失败但 Registry/版本已变 | 禁止自动重试或删除记录 | 完成配置、Registry、版本三方 reconciliation |
| API/scheduler 意外出现 trial | 保持 Registry paused，不执行 live | 找到来源并移除生产入口 |
| canonical backtest 含 gray target | 保留旧 run 审计，停止前端验收 | 用绑定 `gray_target_start` 的新 token 生成新的 immutable run；不得靠前端裁剪收口 |
| 激活后 gray live 不连续 | 冻结该方案的完成状态，不伪造 `scheduled_live` | 按 target 日历补齐缺口并逐条通过 `gray-backfill`；普通 `live` Gate 保持 fresh-only |
| active Registry 缺 `deployed_at` | API/前端 fail-closed | 通过正式 Registry reconciliation 恢复真实部署日期 |
| backtest/live 同一 target 重叠 | 阻断上线，保留冲突清单 | 新回测 run 或正式 correction 使 target 分区互斥后重验 |

Shadow 生命周期操作通过 journal、补偿和 reconciliation 收口；数据库与配置文件不能组成单一事务，因此命令异常后仍必须执行三方对账。若发现任一 shadow 版本或 Registry 行：

1. 不再次签发 token；
2. 保存命令、报告和只读查询结果；
3. 检查配置、Registry、版本表是否构成完整 `shadow + paused`；
4. 三者完整且业务表零新增时，按登记后检查收口；
5. 三者不一致时保持 paused，登记整改，不手工删除历史版本或覆盖原生方案。

## 9. 最终检查

- [ ] 两文件和 Metadata 通过 Intake，摘要已记录
- [ ] 正式新交付含合法 `description`；自验日历未进入两文件目录，Metadata 未声明 `platform_inputs`
- [ ] base/composite 身份无冲突，配置为 `blackbox_v2 + paused + draft`
- [ ] 需要平台周历的方案以 `--platform-input api-wind-date-v1` Intake，父快照仍严格三文件
- [ ] 冻结环境和 sandbox 自检通过
- [ ] DataBridge generation 状态和三 SHA 已保存
- [ ] 当天 occurrence 已冻结 Registry、代码/config 摘要、21/25 动态期望数
- [ ] Native 和 DataBridge generation 均在 06:30 后建立并 SEALED；V2 绑定同一 DataBridge generation
- [ ] 四个 V2 按 DataBridge `sealed_at +0/+2/+4/+6` 独立释放，未通过旧 preflight 或 scheduler restart 绕行
- [ ] 08:00 target SLA 已从冻结 ledger 评估；late completion 没有回写 `BREACHED`
- [ ] Input 报告三 SHA 与选定 generation 完全一致
- [ ] 七个 Gate 通过，并理解各 Gate 没有证明什么
- [ ] 技术零写入批次使用 `--check-only`，报告四个零写字段正确，Backtest 为 `100/100 + persist=false`，API readiness 仅为结构验证
- [ ] 如准备 shadow（非 check-only），exact Harness run 和七个结果已进入审计 DB
- [ ] 普通自动段只产生控制面审计，没有业务表新增；check-only 连控制面也未持久化
- [ ] Shadow token 绑定 exact version/run 并设置短有效期
- [ ] 登记后配置、版本、Registry 为 `shadow + paused`
- [ ] 独立 DB、scheduler 和 API 检查证明 trial 未进入生产链路
- [ ] 失败按恢复矩阵处理，没有把部分状态当成成功
- [ ] 默认入库流程未执行 `activate` 或 `live`；如有专项授权，已转入独立生产灰度记录
- [ ] 如执行持久化回测，授权已绑定实际起点和 `gray_target_start`，完整区间已分批计算并在单一事务中写入一个 immutable run
- [ ] canonical backtest 全部满足 `target_date < gray_target_start`，与 live target 零重叠
- [ ] 激活即登记真实 `deployed_at`，并已补齐 `target_date >= gray_target_start` 的连续 `gray_live`
- [ ] `/api/metrics/{registry_scheme_id}` 返回三日期、`prediction_phase` 和 `phase_ranges`
- [ ] 前端单独展示部署时间；详情分隔文案为 `实盘预测目标区间`，有 scheduled target 时显示 `{scheduled_live.start_target_date}开始`，否则显示“待产生”；actual pending 继续显示“待验证”
- [ ] 前端三个数据口径与 DB/API 一致，任务格子、短名称、样本数、分隔线和控制台均通过

具体方案的 generation、snapshot、Harness run、预测结果、数据库计数和当前状态只追加到平台入库规划文档，不回写本通用 SOP。
