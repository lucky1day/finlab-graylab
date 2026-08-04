# 已入库 Native 修订验证设计

## 决策

source benchmark 与 Native CompareGate 只承担首次技术入库的接收与保真证据职责。
一个 Native 方案完成首次技术入库后，只有其 prior `all` StaticGate 已持久化、且与当前
业务身份精确匹配的 `static.business_identity` 快照，历史 benchmark 因源数据 vintage
漂移而出现的差异才只保留为归档诊断，不再单独阻断该既有方案的修订激活、历史缺口修复、
`gray_live`、`scheduled_live` 或 API/前端读回。快照只包含 `scheme_id`、`runtime_type`、
`horizon`、`task_type`、`frequency`、target tenors 和 composite Registry IDs，不包含代码、
config 或 version hash。

2026-08-04 经专项授权后，允许一个更窄的历史补证路径：
`legacy-native-admission-identity-attestation`。它不是通用 waiver，也不改写历史
StaticGate。仅 `weekly_10y_d_overlay_0529` 可由专用、一次性、短期 authorization
显式声明其**被 maintenance 当前选择的** legacy prior `all` run 与当前 canonical
业务身份相同。该 receipt 只写 `t_harness_runs` 与 `t_harness_gate_results`，并且固定
绑定 prior version/run、canonical business identity（含全部 composite Registry IDs）、签发者、签发时间和 token hash；
不含也不锁定当前代码、config 或 version hash。它不能由 Gate 自动生成、不能从当前
Registry/config 反推，也不直接激活、回补或写业务表。

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

当前 exact candidate 的 `t_scheme_versions` 行必须存在，`runtime_type='native_adapter'` 且
`status in {'draft','active'}`。期望 composite Registry identity 在预激活时可统一为 `paused`，
激活后可统一为 `active`；若 current version 仍是 `draft` 却配有 `active` Registry，必须
fail-closed。只有 ActivationGate 能在严格 discovery、精确版本与一次性授权核验后原子建立
active 状态；maintenance 只读验证不得自行翻转 version 或 Registry。

ActivationGate 只在下列条件同时成立时承认这一替代阶段：

1. 当前 config 是 Native V1、`status='active'`，且仍在
   `deploy/onboarding_policy_v1.json` 的存量清单中；
2. DB 中存在 current exact `scheme_version` 的 `t_scheme_versions` 行，且其
   `runtime_type='native_adapter'`、`status in {'draft','active'}`；预激活 expected composite
   Registry 可统一为 `paused`，激活后可统一为 `active`，但 `draft` version 与 `active` Registry
   的组合一律 fail-closed；
3. DB 中存在该 `scheme_id` 一个不同于当前 version 的 active Native version，以及一次已通过的
   首次 `stage='all'` Native run。所选 prior run 必须恰有一条 `gate_name='compare'` 的结果，且其
   status 为 `passed`；零条、重复、`skipped` 或非通过结果一律 fail-closed。该 run 的 StaticGate 必须
   持久化唯一、规范化的 `static.business_identity`，并与当前业务字段精确匹配；仅当该 StaticGate
   已通过且其新字段明确缺失（不是 malformed、重复或不匹配）时，才可读取一次唯一、规范化、
   专项授权的 legacy attestation receipt；
4. 所述快照与当前 config/expected Registry 一起精确包含 `runtime_type`、`task_type`、
   `frequency`、`horizon`、target tenors 与全部 composite Registry IDs，故它不是新算法、
   新 target、新 task 或 runtime 迁移；不比较代码、config 或 version hash；
5. 当前 exact `scheme_version` 有一次已通过的 `stage='native-maintenance'` run，并通过
   该阶段全部六个 Gate；
6. 既有 activation token 仍精确绑定当前 `scheme_version`，并继续完成 strict discovery、
   文件 hash、config 生命周期翻转与 Registry/version 原子读回。

若任一条件不成立，maintenance profile 保持 fail-closed。尤其是 legacy prior admission 没有该
快照时，不得由当前 Registry/config 反推或自动补写。唯一已授权例外是上述固定 10Y scope 的
operator attestation：它要求最新被选择的 prior active Native `all+compare=passed` 证据、已通过
但仅缺 identity field 的 StaticGate、token 中精确 prior version/run、非空 issuer、短 TTL 和一次性
消费；receipt 已存在、缺失、重复、非 canonical 或与当前业务身份不等仍失败。写入命令先拒绝
过期或已消费 token；在无既有 receipt 时于唯一 DB receipt 事务前消费 token，若该事务失败则
返回 token 已消费但 receipt 未写入的失败结果，必须新签发 token 后重试。其他方案和其他 legacy
情形仍只能让 current exact version 重跑完整 `all`，然后使用互斥的
`full_initial_onboarding_v1`。attestation 本身不授予 activation、gap repair 或业务写入。maintenance
Activation result 才记录 validation profile、validation harness run、prior
admitted version、Registry target 集与 `benchmark_validation=not_run_post_admission`，避免把它误称为
benchmark pass；full-`all` profile 则记录 `benchmark_validation=passed_initial_admission`。Blackbox
不接受 maintenance stage，继续走既有 `all` lifecycle。

## G4 的正确性与写入边界

G4 不豁免 benchmark。当前 legacy prior admission 缺少可比较的持久化
`static.business_identity`，但满足上述专项 attestation 的唯一 scope：旧版 `63ffb52105ee` 的
最新 passed `all` run `hr_20260611T055610Z_8742d5bc99c9`。2026-08-04 已写入唯一 receipt
`lna_hr_20260611T055610Z_8742d5bc99c9` 并由 maintenance verifier 读回为
`legacy_operator_attestation_v1`；初次 maintenance run `hr_20260804T092226Z_5d84b9d45fd9` 因
预激活 API 状态混同失败，修正后 current exact version `e50ad79a6c2f` 的
`hr_20260804T102103Z_91fa9e7db871` 已通过完整六段 `native-maintenance`。仍须使用独立的
current-version activation token 并通过 ActivationGate；candidate 是 `native_adapter/draft`，expected
Registry 统一为 `paused`，这是正常预激活态，不是额外 blocker。full-`all` 仍是可用的互斥初始入库路径，但不会因
attestation 被伪称为已通过。只有 attestation、maintenance 和修订版本 activation 均完成后，才可
使用现有 signal-gap 路径对唯一业务键补数：

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
- 不把该固定 10Y receipt 扩展为任意 Native 的人工 Compare waiver，不修改历史 Gate JSON。
- 不新增 ledger、occurrence、epoch 或第二调度控制面。

## 验收

1. 首次/新 Native 身份不能请求或通过 post-admission stage。
2. current exact version 的 full-`all` 通过时可用 `full_initial_onboarding_v1` 激活，且不要求
   prior snapshot/maintenance；只有已有匹配 prior `static.business_identity` 的 Native revision 才可
   用 maintenance profile 激活。缺初次 full-onboard CompareGate、被选 prior CompareGate 非唯一或
   非 `passed`、legacy snapshot、Registry drift、runtime/task/frequency/horizon drift 或任一六个 Gate
   时 maintenance 拒绝；legacy snapshot 缺失时，只有固定 10Y 的唯一、专项授权、canonical
   attestation receipt 可作为 identity evidence，其余情形仍只能完整 `all`。
3. Blackbox `all` 与 CompareGate 行为不变。
4. 2026-08-01 G4 no-write 输出仍遵守统一周历和 exact date contract；在专项 receipt、六段
   maintenance 和 activation 均通过前不得实际写入。该六段 run 已通过，但实际写入仍只在用户已授权的单一
   `gray_live` business key 上发生，并可读回。
