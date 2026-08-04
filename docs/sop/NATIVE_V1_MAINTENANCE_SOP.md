# Native V1 存量维护 SOP

**文档状态**：`LEGACY_MAINTENANCE`
**适用运行时**：`native_adapter`
**目标读者**：平台维护人员
**最后核验日期**：2026-08-04

本 SOP 只维护已登记的 Native V1 方案，不接受新增方案。新算法和替代版本使用 [Blackbox V2 平台 SOP](BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)。

## 1. 接收维护任务

记录：

- `scheme_id`、当前 `scheme_version`、Registry composite ID 和状态；
- 问题现象、影响日期、目标期限和任务类型；
- 修改分级 L0/L1 及证据来源；
- 当前代码、配置、benchmark 和关键数据库只读快照；
- prior `all` 的 `static.business_identity` 证据是否存在，以及它是否可与当前业务身份逐字段比对；
- 明确禁止变化的算法锚点。

先确认方案位于 `deploy/onboarding_policy_v1.json`。不在白名单时立即停止，不能补写白名单后继续。

## 2. 限定修改范围

允许修改现有方案目录、对应回测 runner、方案专属测试和状态记录。除非任务已拆为独立平台改造，不得修改：

- `shared` 公共输入、日历和模型契约；
- scheduler、backend、frontend、Harness 公共 Gate；
- migrations 和数据库 Schema；
- 其他方案目录；
- Registry 身份、target 或 task type。

维护期间不得把外部文件、历史 benchmark 或 source evidence 变成生产运行输入。

## 3. 实现和自验

1. 保持 `config.yaml`、目录名和 `predict.py::SCHEME_ID` 一致。
2. 保持 `runtime_type=native_adapter` 和 `input_source=legacy_db`。
3. adapter 只负责输入、日期、调用 core 和构造 `PredictionRecord`。
4. core 不接触数据库、平台写库和其它方案。
5. backtest 与 live 使用同一输入口径和算法核心。
6. source-backed 方案同时比较方向和可导出的内部字段。
7. future `source_end` benchmark 只能验证 source-original backtest，不能作为 live 真值。

## 4. 自动 Gate

首次技术入库先执行单项 Gate 定位问题，再执行完整自动段：

```bash
python -m harness onboard {scheme_id} \
  --predict-date YYYY-MM-DD \
  --stage all
```

固定顺序：

```text
static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness
```

必须确认：

- StaticGate 同时通过 Native 白名单、配置、adapter、core 和 import 边界；
- InputGate 使用 `shared.input_artifacts`；
- DryRunGate 不写业务表；
- CompareGate 按 source role 比较正确证据；
- BacktestGate 默认 `no-persist`；
- ApiReadiness 只表示激活前结构准备，不是 active API 验收。

`all` 中的 Native source benchmark/CompareGate 是首次技术入库的必留证据。当前 exact version 完整通过 `all` 时，ActivationGate 走 `full_initial_onboarding_v1`，只核验当前七个 Gate（含 Compare），不要求 prior snapshot 或 maintenance。只有未走这条 full-`all` profile 的当前修订，且不同 prior Native active version 已通过 `all + compare`、该 prior `all` 的 `static.business_identity` 已持久化并与当前身份精确匹配时，才可以改走唯一的后续维护阶段。快照只允许包含 `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、target tenors 和 composite Registry IDs，绝不含代码、config 或 version hash：

```bash
python -m harness onboard {scheme_id} \
  --predict-date YYYY-MM-DD \
  --stage native-maintenance
```

固定顺序为：

```text
static -> native-maintenance-admission -> input -> unit -> dry-run -> api-readiness
```

`native-maintenance-admission` 只读复核 prior `all + compare`、匹配的 prior `static.business_identity` 与当前 Registry identity；current exact `t_scheme_versions` 必须为 `native_adapter` 的 `draft|active` 行，expected Registry 必须全 paused（预激活）或全 active（激活后），且 draft+active fail-closed。整个阶段持久化 Harness 审计证据但不写业务表，故不得用 `--check-only`；任一 run-start、gate-result 或 run-finish 持久化失败时必须留下本地 `BLOCKED` 证据，且该 run 不可作为 activation 依据。只有 ActivationGate 才能原子建立 active。legacy admission 缺快照时一律 fail-closed，唯一已实现补证仅限 `weekly_10y_d_overlay_0529`：`native-legacy-admission-attest` 重新读取 maintenance 选定的 prior，要求唯一 passed `all + compare`、已通过但仅缺 identity 的 StaticGate，以及绑定 issuer/exact prior version/run、TTL ≤900 秒的一次性 token。其 canonical receipt 只写 `t_harness_runs`/`t_harness_gate_results`，不含当前 hash、不改历史、已存在即阻断；它只让 Gate 记录 `legacy_operator_attestation_v1`，但不替代完整六段 maintenance；activation 仍要求一条已通过且完整持久化的六 Gate run 与独立 token。它不执行当前 historical `compare/backtest`，不是全局关闭 CompareGate：新身份、业务身份漂移或任何前提不满足时都必须回到 `all`。任一所选阶段 Gate 失败时，从该阶段的 static 重跑，不跳过失败项。

## 5. 数据与结果核验

至少验证：

- `predict_date / feature_date / target_date` 符合对应频率语义；
- 输入严格截止到 `feature_date`；
- 方向值仅为 `-1/0/1`；
- 首次技术入库的方向和内部 score 与正确角色的 benchmark 一致；已入库同一身份修订的当前日期输出与 live-safe oracle 一致；
- 相同输入和日期重复执行结果一致；
- 回测不包含 gray/live target 区间；
- `predicted_direction=0` 不进入准确率分母。

## 6. 授权副作用

自动段通过后，任何 persist、live 写库或状态切换仍使用既有一次性授权流程。操作前后独立查询：

Native 激活授权必须绑定刚通过 `all` 或 `native-maintenance` 的
`validation_scheme_version`，并记录非空 operator 身份。两条 profile 互斥：当前 exact version
有 passed `all` 及 CompareGate 时，ActivationGate 使用 `full_initial_onboarding_v1`，不检查
prior snapshot 或 maintenance；后续维护 profile 才须有 prior passed `all + compare`、匹配的 prior
`static.business_identity`、当前六个 Gate、native `draft|active` exact version 与统一 paused/active 的精确 Registry identity。draft+active 必须失败，且只有 ActivationGate 能原子建立 active。缺 legacy snapshot 时 ActivationGate 仍 fail-closed；唯一固定 receipt 不构成激活 token 或写库授权，它只让 `weekly_10y_d_overlay_0529` 的已通过、identity 缺字段 prior 作为 `legacy_operator_attestation_v1` 通过 maintenance admission。receipt 后仍要六段 Gate 与独立 activation token。用同一标准 discovery 入口只读
计算当前精确版本；该值必须与最近一次 passed `t_harness_runs.scheme_version` 一致，ActivationGate
会再次严格核验。随后显式签发和消费：

```bash
NATIVE_SCHEME_ID="{scheme_id}"
VALIDATION_SCHEME_VERSION="$(
  python -c 'import sys; from pathlib import Path; from scheduler.discovery import load_scheme_config; print(load_scheme_config(Path("schemes") / sys.argv[1] / "config.yaml").scheme_version)' \
    "${NATIVE_SCHEME_ID}"
)"
NATIVE_RELEASE_OPERATOR="<operator-id>"

python -m harness auth issue \
  --scheme-id "${NATIVE_SCHEME_ID}" \
  --action activate \
  --scheme-version "${VALIDATION_SCHEME_VERSION}" \
  --issued-by "${NATIVE_RELEASE_OPERATOR}"

python -m harness activate \
  --scheme-id "${NATIVE_SCHEME_ID}" \
  --authorize "<raw-one-time-token>"
```

不得省略 `--scheme-version` 或 `--issued-by`，也不得用修改配置后的新版本号
替代已通过 Gate 的 `validation_scheme_version`。paused 配置激活后因为只翻转
根级 `status`，`activated_scheme_version` 会变化；已 active 的 legacy 精确版本
重批准时版本保持不变。

日频 Native 激活不再要求维护 `daily_gray_launchd_policy_v1.json` 或任何 frozen
daily-gray 清单；这些都是待退役兼容控制面，不能作为新版本发布单元。激活只绑定刚通过
的精确 `validation_scheme_version`、Registry 状态和专项授权。只有匹配 prior
`static.business_identity` 的已入库同一身份修订，其历史 source-benchmark 输入 vintage
漂移才只作归档诊断，不能单独阻断 activation、gap repair、`gray_live`、`scheduled_live` 或
API；缺 legacy snapshot 时仍按本 SOP 的 full-`all` 路径 fail-closed，除非固定 10Y scope 的专项 receipt 已成功写入并经 maintenance admission 读取。receipt 不授予写业务表或调度权。
输入截止、统一周历、日期语义、L0/L1/L2、live-safe oracle 与授权边界不变。

激活本身也不授予自然调度权。若某个方案随后需要 scheduler admission，必须在
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)规定的 G1/G2
前置完成后，重新评估 writer、输入新鲜度和 installed plist，并另取授权。任何
`bootout/bootstrap/kickstart`、installed plist 修改或服务重启同样必须另取明确生产
操作授权并保存现场证据；本 SOP 的算法维护授权不自动包含这些控制面操作。

- Registry 和版本状态；
- `t_scheme_runs`、预测表和 run log；
- `t_backtest_*`；
- 对应 launchd policy、installed plist / loaded state 和任务日志；
- `/api/schemes`、metrics 和 factor-lab backtest。

仅维护当前方案，不得改变其它方案记录。失败时保持或恢复原状态，保存审计证据，不手工删除历史版本。

## 7. 完成条件

- [ ] 身份仍在 Native 白名单且未改变。
- [ ] 改动保持 L0/L1，没有 L2 算法升级。
- [ ] 当前 exact version 的七个 `all` Gate 全部通过并使用 `full_initial_onboarding_v1`，或 maintenance profile 的六个 Gate、prior `all + compare`、匹配的 `static.business_identity`（仅固定 10Y 可由 canonical receipt 补证）与 Registry identity 全部通过；两者不得叠加要求。receipt 本身不构成 activation 或业务写入完成条件。
- [ ] no-persist、重复和日期截止验证通过。
- [ ] 授权写入只影响允许的当前方案记录。
- [ ] API、scheduler 和 Registry 与预期一致。
- [ ] `CURRENT_STATUS` 记录修复事实和剩余风险。

不满足任一项时不得宣称维护完成。
