# Blackbox V2 平台入库 SOP（Contract 1.0，Intake 至 Shadow）

**文档状态**：`CURRENT`
**适用运行时**：`blackbox_v2`
**目标读者**：平台入库、运行和审计人员
**最后核验日期**：2026-07-19

本文是平台操作人员接收、技术验收和登记 Blackbox V2 方案的唯一操作 SOP。上游交付契约见 [BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)；具体方案的版本、快照、运行结果和当前状态只追加到 [Blackbox V2 入库试验台账](../blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md)。文档分类和维护规则见 [Blackbox V2 文档管理](../blackbox_v2/README.md)。

Contract 1.0 当前只允许登记为 `shadow + paused`。不得执行 `activate` 或 `live`；这是当前操作政策，平台代码尚未设置专用 hard-stop。生产晋级前置条件见[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)，该清单尚未成为可执行 SOP。

## 1. Intake 与身份

### 1.1 接收前检查

交付目录必须恰好包含：

```text
{scheme_id}.py
{scheme_id}.json
```

执行 Intake 前确认：

- 两个条目均为普通文件，不是目录或符号链接；
- 文件名与 Metadata 中的 `scheme_id` 一致，Metadata 恰好八字段；
- `.py` 是唯一可执行内容，不存在模型、配置、依赖或辅助模块；
- trial 的 base `scheme_id` 和 composite Registry ID 均未占用；
- 同一算法已有原生实现时使用独立 trial ID，不覆盖原方案。

记录收到文件的原始摘要：

```bash
shasum -a 256 <delivery-dir>/{scheme_id}.py <delivery-dir>/{scheme_id}.json
```

### 1.2 执行 Intake

```bash
python -m harness intake-blackbox \
  --delivery-dir <incoming-two-file-directory> \
  --project-root /Users/macstudio0/bond-factor-lab \
  --runtime-profile blackbox-v2-v1 \
  --data-schema-version data-bridge-v1
```

Intake 应原字节保存交付文件，并生成：

```text
schemes/{scheme_id}/
├── config.yaml
└── delivery/
    ├── {scheme_id}.py
    └── {scheme_id}.json
```

检查平台配置至少包含：

```yaml
scheme_id: <scheme_id>
runtime_type: blackbox_v2
input_source: data_bridge_current
runtime_profile: blackbox-v2-v1
data_schema_version: data-bridge-v1
status: paused
version_status: draft
```

名称、算法版本、期限、任务类型、horizon 和 target rule 只能来自 Metadata。`blackbox-v2-v1` 是运行 profile；`forecast_env_blackbox_v1` 是该 profile 当前引用的 conda 环境，两者不得混称。

### 1.3 对照 Contract 1.0

机器契约 `shared.blackbox_v2.contracts` 是字段和组合的判定源：

- Metadata 恰好八字段：`schema_version`、`scheme_id`、`name`、`algorithm_version`、`target_tenor`、`task_type`、`horizon`、`target_rule`。
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

当前执行器仍从 `scheduler.blackbox_v2_runner.RuntimeProfile` 代码默认值构造实际限制，scheduled predict timeout 还可以由方案 schedule 覆盖。执行前必须同时核对 JSON profile、环境 manifest、代码默认值和方案 timeout；任一不一致时停止验收并登记整改，不能只凭 JSON 文件认定实际运行参数。

记录环境清单摘要和自检时间。环境不一致、资源基准漂移、sandbox 网络拒绝或数据目录写保护失效时，不得继续。CPU、内存、predict/backtest 超时、100 条批量上限、Output 和日志大小上限以核对一致后的实际执行值为准。

### 2.2 验证 DataBridge current

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/refresh_data_bridge_current.py --check-only \
    --date <selected-generation-refresh-date>
```

结果必须为 `status=ok`，并保存：

- `generation_id`、`refresh_date`、`refreshed_at`；
- `schema_version`、`business_digest`；
- 三份文件的行列数、起止键、SHA256 和业务摘要。

| 场景 | freshness 要求 |
|---|---|
| 技术 Onboarding | 选择最新一个通过完整性校验的 generation；将其 `refresh_date` 显式传给 `--date`，允许与执行日不同 |
| `scheduled_live` | 将运行日传给 `--date`；必须是当日成功发布且 state/文件摘要一致的 generation |

Input Gate 当前使用 `require_fresh=False`，只证明快照结构和内容可用，不独立证明它是当天 generation；scheduled-live 执行器才强制当天 freshness。两者不得混称。

DataBridge 校验失败时保留最后成功 current，阻断依赖 `data_bridge_current` 的运行，不用旧摘要冒充新 generation。

## 3. 快照与 Request

### 3.1 运行快照

平台通过 `shared.input_artifacts`：

1. 在共享锁内校验 `data/data_bridge/current` 与 state；
2. 复制 `daily_output.csv`、`weekly_output.csv`、`monthly_output.csv`；
3. 按冻结 Schema 生成内容寻址的 `data_snapshot_id`；
4. 将目录和文件设为只读，释放锁后启动算法。

Input 报告必须记录 snapshot ID、Schema、三份文件行列数与 SHA256，以及 Request 的三个日期和三个截止键。

Input 报告当前不包含 `generation_id`。操作人员必须将第二节保存的 current 状态与 Input 报告三份 SHA256 逐一比对；全部相同只能证明 Snapshot 内容与所选 generation 的三份文件一致，不能在两个 generation 内容完全相同时唯一证明 generation 身份。

正常 `onboard --stage all` 结束后临时快照会删除。`input_state.json` 中的绝对路径只在执行期间有效，不能用于回放；平台不永久保存该次完整输入文件。

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

### 4.2 Gate 证据边界

| Gate | 当前检查 | 当前没有证明 | 主要证据 |
|---|---|---|---|
| `static` | 两文件、Metadata、语法、禁止 import/调用，以及 `/Users/`、`/home/`、Windows 盘符形式的绝对路径字面量 | 其他绝对路径、算法效果、全局文件读取隔离 | runtime、版本、Metadata、违规列表 |
| `input` | 三频 Schema、快照、七字段 Request、三个截止键 | 当天 freshness、generation 映射 | snapshot ID、三 SHA、Request |
| `unit` | help 暴露两个模式；一个非法 Request 失败且无 Output | 所有非法组合均被覆盖 | help、非法输入、失败无 Output |
| `dry-run` | 单点 predict、Result 校验、内存 `PredictionRecord` | 已写预测表或已进入业务 API | PredictionRecord、结果路径 |
| `compare` | 重复、predict/backtest、分批、顺序、后续行隔离 | 准确率、历史修订回放 | 五类一致性证据 |
| `backtest` | 100 条全部返回、no-persist | 大于 100 条单进程能力、效果门槛 | 请求/结果数量、persist=false |
| `api-readiness` | composite 身份和结果结构兼容 | 真实 Registry、HTTP API 或 scheduler 探针 | registry ID、结构结果 |

报告中的 `business_tables_written: false` 是声明性证据，不是数据库前后计数。`api-readiness` 中的 scheduler/API 状态也是结构预期，不能单独证明生产不可见。

### 4.3 自动段副作用

`--stage all` 可以写：

- `reports/harness/{scheme_id}/...`；
- `t_harness_runs`、`t_harness_gate_results` 等控制面审计记录。

它不得写 `t_scheme_runs`、`t_scheme_predictions`、`t_backtest_*` 业务记录、active Registry 或前端可见状态。

Harness 控制面持久化当前是 best-effort。即使 `onboard_report.json` 为 `overall_passed=true`，也必须确认 exact `harness_run_id` 和七个 Gate 已存在于审计数据库，才能授权 shadow。

当前 Result 解析器会将 JSON 中的字符串 `"-1" / "0" / "1"` 转成整数，这与上游 SOP 的“JSON 必须输出整数”规范不一致。该兼容行为不改变上游契约；在解析器收紧前，报告只能写“业务值可解析”，不能宣称 JSON 类型已被机器严格拒绝。

临时原始 Request、Result 和 stderr 当前随运行目录清理，不承诺长期留存；持久审计以 Harness 报告、摘要和结构化记录为准。

## 5. Shadow 登记

### 5.1 授权前核验

从最新通过报告取得 `harness_run_id`、`scheme_version`、`predict_date`、七个 Gate 状态、snapshot ID 和三 SHA，并另外完成：

1. 重新运行环境自检；
2. 使用只读 SQL 确认 exact run 已写入审计 DB；`python -m harness report {scheme_id} --latest` 只读取本地最新报告，不能代替数据库核验；
3. 再次检查 base/composite Registry 冲突；
4. 保存业务表和 active Registry 的前置计数；
5. 确认本轮只允许 `shadow + paused`。

Shadow Gate 当前不会真正复验 conda 环境，也不会自动证明 API/scheduler 不可见，这些必须人工核验。

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

### 5.2 签发并使用授权

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

## 6. 失败恢复

| 场景 | 立即动作 | 允许继续的条件 |
|---|---|---|
| 环境自检失败 | 停止 Intake/Onboarding | 冻结环境恢复并重新自检 |
| DataBridge 失败 | 保留最后成功 current，阻断 V2 | `--check-only` 重新通过 |
| Intake 失败 | 不手工拼方案目录，不改交付文件 | 清理未完成 trial 后重新 Intake |
| 自动 Gate 失败 | 不签发 token，保留报告 | 问题修复后从 static 重跑全套 |
| 报告通过但审计 DB 缺失 | 不签发 token | exact run 和七个 Gate 完整持久化 |
| 临时快照残留 | 不交给下一次运行 | 无进程占用后清理并重建 |
| shadow 失败且 Registry/版本未变 | 核对 token、审计和前置状态 | 仍为 draft/paused 且身份未占用 |
| shadow 失败但 Registry/版本已变 | 禁止自动重试或删除记录 | 完成配置、Registry、版本三方 reconciliation |
| API/scheduler 意外出现 trial | 保持 Registry paused，不执行 live | 找到来源并移除生产入口 |

Shadow 当前由多个数据库事务和配置更新组成，不能宣称失败时自动原子回滚。命令失败后若发现任一 shadow 版本或 Registry 行：

1. 不再次签发 token；
2. 保存命令、报告和只读查询结果；
3. 检查配置、Registry、版本表是否构成完整 `shadow + paused`；
4. 三者完整且业务表零新增时，按登记后检查收口；
5. 三者不一致时保持 paused，登记整改，不手工删除历史版本或覆盖原生方案。

## 7. 最终检查

- [ ] 两文件和 Metadata 通过 Intake，摘要已记录
- [ ] base/composite 身份无冲突，配置为 `blackbox_v2 + paused + draft`
- [ ] 冻结环境和 sandbox 自检通过
- [ ] DataBridge generation 状态和三 SHA 已保存
- [ ] Input 报告三 SHA 与选定 generation 完全一致
- [ ] 七个 Gate 通过，并理解各 Gate 没有证明什么
- [ ] exact Harness run 和七个结果已进入审计 DB
- [ ] 自动段只产生控制面审计，没有业务表新增
- [ ] Shadow token 绑定 exact version/run 并设置短有效期
- [ ] 登记后配置、版本、Registry 为 `shadow + paused`
- [ ] 独立 DB、scheduler 和 API 检查证明 trial 未进入生产链路
- [ ] 失败按恢复矩阵处理，没有把部分状态当成成功
- [ ] 未执行 `activate` 或 `live`

具体方案的 generation、snapshot、Harness run、预测结果、数据库计数和当前状态只追加到平台入库规划文档，不回写本通用 SOP。
