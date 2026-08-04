# 已入库 Native 修订验证设计

## 决策

source benchmark 与 Native CompareGate 只承担首次技术入库的接收与保真证据职责。
一个 Native 方案完成首次技术入库后，只有其 prior `all` StaticGate 已持久化、且与当前
业务身份精确匹配的 `static.business_identity` 快照，历史 benchmark 因源数据 vintage
漂移而出现的差异才只保留为归档诊断，不再单独阻断该既有方案的修订激活、历史缺口修复、
`gray_live`、`scheduled_live` 或 API/前端读回。快照只包含 `scheme_id`、`runtime_type`、
`horizon`、`task_type`、`frequency`、target tenors 和 composite Registry IDs，不包含代码、
config 或 version hash。

ActivationGate 的 Native profile 必须互斥：current exact version 已通过完整七段 `all` 时，采用
`full_initial_onboarding_v1`，只核验该 current `all` 的七个 Gate（含 Compare）和一次性授权，
不要求 prior snapshot 或 `native-maintenance`。只有未走 full-`all` profile 的既有修订，才可能
采用下述 `native_post_admission_revision_v1` maintenance 路径。

这不是全局关闭 CompareGate：Blackbox V2 的 CompareGate 仍验证确定性、分批/顺序与
截止隔离；新方案、新算法身份、新 target、新 task 或新 runtime 的首次 Native 入库仍须
完整执行 source benchmark CompareGate。

## 问题

`weekly_10y_d_overlay_0529` 的跨年稳定排序修订形成了新的精确 Native version。现有
activation 只接受当前 version 的 `stage='all'` 成功记录，而 `all` 固定执行 CompareGate 与
BacktestGate；后二者会比较已变化的历史 benchmark。因此即使 2026-08-01 的 live-safe
日期、周历和算法输出正确，新 version 仍无法激活，进而无法受控补齐该日 `gray_live` 信号。

## 最小架构

新增唯一的 Native 自动验证阶段：`native-maintenance`。

它只适用于已有正式首次入库证据、且 prior StaticGate 留有匹配业务快照的 Native V1 既有身份，执行顺序固定为：

```text
static -> native-maintenance-admission -> input -> unit -> dry-run -> api-readiness
```

`native-maintenance-admission` 是只读 Gate，显式记录 prior admitted version、prior
`static.business_identity` 与当前 Registry target 集合的精确匹配。该阶段不执行 `compare` 或 `backtest`：两者在当前 Native runner 中
都会触发 historical source benchmark 对比，不是验证后续 live-safe 修订所必需的条件。它
不包含 live 或任何业务表写入；harness run/gate evidence 仍持久化，以供后续
ActivationGate 审计。因此它不是 `--check-only`，但也不产生业务表副作用。

ActivationGate 只在下列条件同时成立时承认这一替代阶段：

1. 当前 config 是 Native V1、`status='active'`，且仍在
   `deploy/onboarding_policy_v1.json` 的存量清单中；
2. DB 中存在该 `scheme_id` 一个不同于当前 version 的 active Native version，以及一次已通过的
   首次 `stage='all'` Native run。所选 prior run 必须恰有一条 `gate_name='compare'` 的结果，且其
   status 为 `passed`；零条、重复、`skipped` 或非通过结果一律 fail-closed。该 run 的 StaticGate 必须
   持久化唯一、规范化的 `static.business_identity`，并与当前业务字段精确匹配；
3. 所述快照与当前 config/active Registry 一起精确包含 `runtime_type`、`task_type`、
   `frequency`、`horizon`、target tenors 与全部 composite Registry IDs，故它不是新算法、
   新 target、新 task 或 runtime 迁移；不比较代码、config 或 version hash；
4. 当前 exact `scheme_version` 有一次已通过的 `stage='native-maintenance'` run，并通过
   该阶段全部六个 Gate；
5. 既有 activation token 仍精确绑定当前 `scheme_version`，并继续完成 strict discovery、
   文件 hash、config 生命周期翻转与 Registry/version 原子读回。

若任一条件不成立，maintenance profile 保持 fail-closed。尤其是 legacy prior admission 没有该
快照时，不得由当前 Registry/config 反推或自动补写；当前唯一已实现 fallback 是 current exact
version 重跑完整 `all`，然后使用互斥的 `full_initial_onboarding_v1`。`legacy admission identity
attestation` 尚未设计或实现；未来即使建设也必须独立设计、实现并取得明确专项授权，当前不是可
执行路径。本设计不创建、不执行 attestation，且 attestation 本身也不授予 activation、gap repair
或业务写入。maintenance Activation result 才记录 validation profile、validation harness run、prior
admitted version、Registry target 集与 `benchmark_validation=not_run_post_admission`，避免把它误称为
benchmark pass；full-`all` profile 则记录 `benchmark_validation=passed_initial_admission`。Blackbox
不接受 maintenance stage，继续走既有 `all` lifecycle。

## G4 的正确性与写入边界

G4 不豁免 benchmark。当前 legacy prior admission 缺少可比较的持久化
`static.business_identity`，所以它不能使用 `native-maintenance`、activation 或任何 signal-gap
写入。当前唯一已实现恢复路径是 current exact version 重跑完整 `all`（包含当前 Compare），并在
通过后使用 `full_initial_onboarding_v1` 激活；该 full-`all` 尚未通过。`legacy admission identity
attestation` 尚未设计或实现，不是当前替代路径。只有已实现的 full-`all` 前提和修订版本激活均
完成后，才可使用现有 signal-gap 路径对唯一业务键补数：

```text
weekly_10y_d_overlay_0529 / 10Y / h6
predict_date=2026-08-01
feature_date=2026-07-31
target_date=2026-08-07
prediction_phase=gray_live
```

写入前仍必须有冻结且重算一致的 gap plan、正确的统一周历、Native 输入 artifact/source
authority、单方案 HMAC 授权以及输出日期/target contract。写入后必须由 repository 原子
提交并从 DB/API 读回。历史 benchmark 不会成为该链路的输入或 blocker；live-safe oracle
与 source/input provenance 仍是当前日期正确性的依据。

## 非目标

- 不删除 benchmark 文件、既有 benchmark 记录或首次入库 CompareGate。
- 不修改 Native core、算法参数、数据源或历史 benchmark 数据以贴合结果。
- 不放宽 Blackbox CompareGate、输入截止、统一日历、版本身份、写库授权或 scheduler。
- 不新增 ledger、occurrence、epoch 或第二调度控制面。

## 验收

1. 首次/新 Native 身份不能请求或通过 post-admission stage。
2. current exact version 的 full-`all` 通过时可用 `full_initial_onboarding_v1` 激活，且不要求
   prior snapshot/maintenance；只有已有匹配 prior `static.business_identity` 的 Native revision 才可
   用 maintenance profile 激活。缺初次 full-onboard CompareGate、被选 prior CompareGate 非唯一或
   非 `passed`、legacy snapshot、Registry drift、runtime/task/frequency/horizon drift 或任一六个 Gate
   时 maintenance 拒绝；legacy snapshot 缺失时当前只能完整 `all`，未实现 attestation 不构成例外。
3. Blackbox `all` 与 CompareGate 行为不变。
4. 2026-08-01 G4 no-write 输出仍遵守统一周历和 exact date contract；在 current exact version
   完整 `all` 通过和 full-profile activation 前不得实际写入。之后实际写入只在用户已授权的单一
   `gray_live` business key 上发生，并可读回。
