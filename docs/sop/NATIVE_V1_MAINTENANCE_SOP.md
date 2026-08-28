# Native V1 存量维护 SOP

**文档状态**：`LEGACY_MAINTENANCE`
**适用运行时**：`native_adapter`
**目标读者**：平台维护人员
本 SOP 只维护已登记的 Native V1 方案，不接受新增方案。新算法和替代版本使用 [Blackbox V2 平台 SOP](BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)。

## 1. 准入、分级与基线

只有 `deploy/onboarding_policy_v1.json` 中已有的 `native_adapter` 身份可以进入本流程；
`scheme_id`、target、task type、Registry composite 身份和 `input_source=legacy_db` 必须保持不变。
不满足任一条件时停止维护，新增或替代算法改走 Blackbox V2。

| 级别 | 范围 | 处理 |
|---|---|---|
| L0 | 平台 I/O、日期字段、extra、缓存、日志、执行预算和审计适配 | 允许 |
| L1 | 原始 runner 已明确暴露的上下文参数恢复 | 有原始证据和逐项对比时允许 |
| L2 | 特征、窗口、模型、阈值、投票、selector、fallback 或 score 映射 | 禁止，创建 Blackbox V2 trial |

输入只能来自 `shared.input_artifacts`，实盘和回测只能经各自 repository 写库，Native
`core/` 不得连接数据库、写文件或跨方案 import。`feature_date` 是唯一数据截止日；存量周/月
`horizon=6/30` 只保留身份兼容，业务分列始终使用 `task_type`。

开始前记录：

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
static -> dry-run -> compare -> backtest
```

必须确认：

- StaticGate 同时通过 Native 白名单、配置、adapter、core 和 import 边界；
- DryRunGate 使用既有执行路径，通过仅在 Harness 子进程启用的 builder receipt 核验该次执行实际生成的 `shared.input_artifacts` 主/辅助输入来源、data version、截止、流式 SHA-256/行数、可信根内无 symlink 路径和必需列，并保证不写业务表；
- CompareGate 按 source role 比较正确证据；
- BacktestGate 默认 `no-persist`；
- 技术 `all` 不访问 Backend；激活后的 HTTP 验收另行使用 DashboardGate。

`all` 中的 Native source benchmark/CompareGate 是首次技术入库的必留证据。当前 exact version 完整通过 `all` 时，ActivationGate 走 `full_initial_onboarding_v1`，只核验当前四个 Gate（含 Compare），不要求 prior snapshot 或 maintenance。只有未走这条 full-`all` profile 的当前修订，且不同 prior Native active version 已通过 `all + compare`、该 prior `all` 的 `static.business_identity` 已持久化并与当前身份精确匹配时，才可以改走唯一的后续维护阶段。快照只允许包含 `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、target tenors 和 composite Registry IDs，绝不含代码、config 或 version hash：

```bash
python -m harness onboard {scheme_id} \
  --predict-date YYYY-MM-DD \
  --stage native-maintenance
```

固定顺序为：

```text
static -> native-maintenance-admission -> dry-run
```

`native-maintenance-admission` 只读复核 prior `all + compare`、匹配的 prior `static.business_identity` 与当前 Registry identity；current exact `t_scheme_versions` 必须为 `native_adapter` 的 `draft|active` 行，expected Registry 必须全 paused（预激活）或全 active（激活后），且 draft+active fail-closed。整个阶段持久化 Harness 审计证据但不写业务表；run-start 先 fail-early，全部 Gate 结果与 run 完成状态随后在一个事务中批量持久化，任一步失败都返回 `BLOCKED`，且该 run 不可作为 activation 依据。只有 ActivationGate 才能原子建立 active。prior snapshot 缺失、重复、损坏或不匹配时一律 fail-closed，不再读取方案级历史 receipt。它不执行当前 historical `compare/backtest`，不是全局关闭 CompareGate：新身份、业务身份漂移或任何前提不满足时都必须回到 `all`。任一所选阶段 Gate 失败时，从该阶段的 static 重跑，不跳过失败项。

## 5. 数据与结果核验

至少验证：

- `predict_date / feature_date / target_date` 符合对应频率语义；
- 输入严格截止到 `feature_date`；
- 方向值仅为 `-1/0/1`；
- 首次技术入库的方向和内部 score 与正确角色的 benchmark 一致；已入库同一身份修订的当前日期输出与 live-safe oracle 一致；
- 相同输入和日期重复执行结果一致；
- 回测不包含 gray/live target 区间；
- `predicted_direction=0` 不进入准确率分母。

## 6. 直接命令副作用

### 6.1 运行期输入与 Phase-A cache

scheduled one-shot 与单日 gap-fill 使用作业级临时输入根目录。同一作业内，只有 frequency、运行日、
起止日期/周、as-of、schema columns、data version 和数据库源类型全部一致的 builder 调用才复用同一只读
CSV；每个方案通过自己的只读硬链接路径读取。作业成功、失败或中断后都删除该目录，不向
`backtest_artifacts/runtime_inputs` 累积日常输入。DryRun 的输入和 builder receipt 同样只在该次 Gate 的
临时目录中存在，合同验证完成即清理。

单日补缺与自然 one-shot 的 Liwei Phase-A cache 继续使用调度环境的持久化根，不随临时输入切换到
private cache。单日 `signal-gap-fill` 只允许现有 generation `hit` 或安全追加一个尾部日期；`suffix/full`、
无 current 或多日缺失必须在训练前失败。自然 daily one-shot 的已批准 publisher 还可以处理 cache 已证明的
日频或有效辅助输入 suffix 修订，但去重后的重算范围最多为 32 个交易日期；未知原因、无法映射的周/月修订、
schema/spec/baseline/proof/lineage 漂移和 `full` 一律在训练前失败。DryRun 则显式使用 Gate 临时目录内的
私有 Phase-A cache 和 `private_build`，不读取或改写生产 cache。每个 cadence 先执行已批准 publisher、
再执行其余 Native，两阶段各最多两个 worker；consumer 不发布 cache。单方案成功即独立提交，失败不
回滚已完成方案；重试依赖 insert-only 业务键只规划剩余方案。中断时使用现有 process-control 终止已启动
进程组并关闭未完成 run，不新增任务表、报告或审计字段。

以上规则只优化平台 I/O 与编排，不允许改变训练窗口、模型、方向、confidence、三个业务日期、scheme
version 或算法必要 extra。

自动段通过后，任何 persist、单日 `signal-gap-fill` 或状态切换仍须使用精确的独立副作用命令；正式 `scheduled_live` 只由目标主机已安装的 one-shot 调度触发。命令本身是单维护者对本次操作的明确授权；不生成密钥、不签发 token、不复制 `--authorize`。操作前后独立查询：

Native 激活授权必须绑定刚通过 `all` 或 `native-maintenance` 的
`validation_scheme_version`，并记录非空 operator 身份。两条 profile 互斥：当前 exact version
有 passed `all` 及 CompareGate 时，ActivationGate 使用 `full_initial_onboarding_v1`，不检查
prior snapshot 或 maintenance；后续维护 profile 才须有 prior passed `all + compare`、匹配的 prior
`static.business_identity`、当前三个 Gate、native `draft|active` exact version 与统一 paused/active 的精确 Registry identity。draft+active 必须失败，且只有 ActivationGate 能原子建立 active。prior snapshot 缺失或不匹配时 ActivationGate 仍 fail-closed。用同一标准 discovery 入口只读
计算当前精确版本；该值必须与最近一次 passed `t_harness_runs.scheme_version` 一致，ActivationGate
会再次严格核验。随后显式执行：

```bash
python -m harness activate \
  --scheme-id "{scheme_id}"
```

CLI 从 canonical `config.yaml` 自动解析 current exact version，ActivationGate 自动选择并绑定
latest passed exact `all` 或 `native-maintenance` run。operator 默认取 `BFL_OPERATOR_ID` 或 OS
用户；只有需要固定审计名称时才传 `--operator "{operator-id}"`。仍不得用修改配置后的新版本号
替代已通过 Gate 的 `validation_scheme_version`；一旦 config 漂移就 fail-closed。paused 配置激活后因为只翻转
根级 `status`，`activated_scheme_version` 会变化；已 active 的 legacy 精确版本
重批准时版本保持不变。

日频 Native 激活不再要求维护 `daily_gray_launchd_policy_v1.json` 或任何 frozen
daily-gray 清单；这些已退役控制面不能作为新版本发布单元。激活只绑定刚通过
的精确 `validation_scheme_version`、Registry 状态和专项授权。只有匹配 prior
`static.business_identity` 的已入库同一身份修订，其历史 source-benchmark 输入 vintage
漂移才只作归档诊断，不能单独阻断 activation、gap repair、`gray_live`、`scheduled_live` 或
API；缺少标准 prior snapshot 时仍按本 SOP 的 full-`all` 路径 fail-closed。
输入截止、统一周历、日期语义、L0/L1/L2、live-safe oracle 与授权边界不变。

激活后，config、exact version 与 Registry target 均 active 的方案会按 cadence 自动进入
launchd one-shot 候选，不再另取 scheduler admission。这不证明现场 plist 已安装或自然运行
已经成功；宣称生产观察前仍须按
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核验 writer、输入新鲜度、
installed plist、loaded state、日志、run 与 prediction。任何
`bootout/bootstrap/kickstart`、installed plist 修改或服务重启同样必须另取明确生产
操作授权并保存现场证据；本 SOP 的算法维护授权不自动包含这些控制面操作。

- Registry 和版本状态；
- `t_scheme_runs`、预测表和 run log；
- `t_backtest_*`；
- 部署矩阵、仓库期望 plist、installed plist / loaded state 和任务日志；
- 激活后 `DashboardGate` 对 `/api/factor-lab/dashboard` 的读回。Dashboard 不携带 exact version，版本身份仍由生命周期、Registry 和数据库权威回读证明。

仅维护当前方案，不得改变其它方案记录。失败时保持或恢复原状态，保存审计证据，不手工删除历史版本。

## 7. 完成条件

- [ ] 身份仍在 Native 白名单且未改变。
- [ ] 改动保持 L0/L1，没有 L2 算法升级。
- [ ] 当前 exact version 的四个 `all` Gate 全部通过并使用 `full_initial_onboarding_v1`，或 maintenance profile 的三个 Gate、prior `all + compare`、匹配的 `static.business_identity` 与 Registry identity 全部通过；两者不得叠加要求。
- [ ] no-persist、重复和日期截止验证通过。
- [ ] 授权写入只影响允许的当前方案记录。
- [ ] Dashboard、scheduler 和 Registry 与预期一致；exact version 已由数据库权威证据独立确认。
- [ ] `CURRENT_STATUS` 记录修复事实和剩余风险。

不满足任一项时不得宣称维护完成。
