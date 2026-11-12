# Blackbox V2 平台入库 SOP（Contract 1.0）

**文档状态**：`CURRENT`

**适用运行时**：`blackbox_v2`

本文是平台操作者把一个 Blackbox 交付从两文件推进到生产自然运行的唯一操作流程。上游交付与自测见
[上游交付 SOP](BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)；身份、版本与生命周期合同见
[共享方案契约](../architecture/SCHEME_CONTRACT.md)；三日期、回测/实盘结果分区与 insert-only 见
[预测语义](../architecture/PREDICTION_SEMANTICS.md)；Writer、控制面与自然运行证据见
[调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。本文不重复这些规则。

## 1. 边界与授权

- 上游负责脚本可运行、算法确定性、逐 Request 截止隔离及 predict/backtest 等价；平台只验证 Intake、
  DataBridge 输入接入、批量 Result 合同、回测持久化与生命周期切换。
- 方案流程只消费已有 producer-ready 输入，不构建或修复 DataBridge，见[DataBridge 契约](../blackbox_v2/data_bridge_v1/README.md)。
- 回测、激活、补缺、生产名单和调度变更是不同副作用；执行一个命令不授权其它动作。
- 所有 `python -B -m harness ...` 命令在目标机候选/当前 release 目录执行，**不会自动加载目标环境**；
  每次执行前按[手工 Harness 的目标环境绑定](../../deploy/README.md#手工-harness-的目标环境绑定)核验进程。
  `gate backtest` 与 `activate` 可用 `--operator` 指定非机密审计身份，缺省取 `BFL_OPERATOR_ID` 或系统用户。
- Harness 命令退出码：`0` 通过（含正常跳过），`1` 失败，`2` 阻断（前置条件不满足，未执行副作用）。

### 1.1 config 中的 status 不代表是否上线

Blackbox 的 `schemes/{id}/config.yaml` 中 `status`/`version_status` 只是 Intake 写入的初始声明
（`paused`/`draft`）：

- 二者**不参与** exact version：config hash 只取
  [`canonical_platform_config`](../../shared/blackbox_v2/versioning.py) 列出的执行字段，改它们不会改变 exact；
- `activate` 不读取也不修改它们，只检查本次命令执行期间 canonical 未漂移；
- 运行期由 [`resolve_database_lifecycle`](../../scheduler/persistence/lifecycle.py) 用本机
  `t_scheme_versions` 中的 exact version 与全部 composite `t_scheme_registry` 覆盖为生效状态；调度与补缺都只认这个结果。

因此入库时**不修改** config 状态（保持 Intake 生成值），也**不得用 config 状态判断方案是否上线**：
大量在线方案的 config 仍是 `paused`，而 config 写着 `active` 也不代表本机已激活。是否上线只看本机数据库
exact + Registry 及 `activate` 读回。Native（W4）不适用本节，见 [W4 运行维护](NATIVE_V1_MAINTENANCE_SOP.md)。

## 2. 新方案流程总览

| # | 步骤 | 在哪里执行 | 产物 / 副作用 |
|---|---|---|---|
| 1 | Intake（可选 `--incremental-state`） | 开发工作区 | `schemes/{id}/` canonical |
| 2 | 定稿 canonical 与部署清单 | 开发工作区，提交 Git | 部署矩阵条目 |
| 3 | 候选 release 构建与安装 | 构建机 → 目标机 | 预安装的不可变 release |
| 4 | 持久化回测 | 目标机候选 release | immutable `t_backtest_*` run |
| 5 | 激活 | 目标机同一 release | exact version、composite Registry、历史产品事实 |
| 6 | 衔接补齐（回测末尾 → 首次自然运行，T+1 通常为 0 个点） | 目标机同一 release | 少量补跑 run 与预测 |
| 7 | 生产方案名单（仅运维确认时） | 目标机 runtime | `config/production_schemes.json` |
| 8 | 接管验收 | 目标机 | Dashboard / 一致性 Gate 证据 |
| 9 | 自然运行观察 | 目标机控制面 | 首个 `scheduled_live` 证据 |

ECS 与 Mac3 各自执行第 4—9 步并使用各自的输入与数据库；晋级规则见[部署手册](../../deploy/README.md#构建预安装和晋级)。

## 3. 新方案逐步操作

### 3.1 Intake

```bash
python -B -m harness intake-blackbox --delivery-dir <delivery-dir> --project-root . [--incremental-state]
```

- **前置条件**：交付目录恰好两个普通文件 `{scheme_id}.py`、`{scheme_id}.json`；`scheme_id` 在仓库中不存在；
  上游自测已按上游 SOP 完成。`--incremental-state` 仅用于已完成增量算法验收的交付。
- **通过标准**：命令成功，且只新增 `schemes/{id}/config.yaml` 与 `schemes/{id}/delivery/{id}.py|.json`；
  config 为 `runtime_type: blackbox_v2`、`status: paused`、`version_status: draft`，带
  `--incremental-state` 时另有 `incremental_state: true`（参与 exact，Metadata 与两文件字节不变）。
  Intake 校验两文件与文件名、Metadata 必填字段（含合法 `owner`）、固定 Runtime Profile 与 Data Schema、
  脚本语法及[静态安全策略](../../shared/blackbox_v2/_script_policy.py)，原子保存原始字节。
- **失败时**：修正交付后重新 Intake；目标目录不得残留部分写入；已有 base ID 一律拒绝覆盖（同 ID 走第 4 节）。
- **副作用与授权**：只写开发工作区文件；不读 DataBridge、不运行算法、不写数据库。

### 3.2 定稿 canonical 与部署清单

在同一开发分支完成以下改动并提交：

1. **canonical**：确认 config 的 `schedule`（cron/timezone/`timeout_sec`）等执行字段。这些字段参与 exact；
   提交后任何改动都会产生新 exact，须重新回测。不修改 `status`/`version_status`（见 1.1）。
2. **部署矩阵** [`deploy/scheme_deployment_matrix_v1.json`](../../deploy/scheme_deployment_matrix_v1.json)：
   新增 `"{id}": [<部署目标>...]`。**必需**：设置 `BFL_DEPLOYMENT_TARGET` 的生产 discovery 要求矩阵与全部
   `schemes/*/config.yaml` 一一对应，缺一项会使该主机所有方案的自然运行 fail-closed。

- **通过标准**：[入库相关验证](../onboarding/README.md#入库相关验证)全部通过，尤其
  `tests/test_deployment_scope.py`；exact 以 discovery 计算值为准。
- **失败时**：修正清单或 config 后重跑测试；不得通过修改交付两文件迁就清单。
- **副作用与授权**：仓库提交；不改变任何主机状态。

### 3.3 候选 release 构建与安装

- **命令与步骤**：按[部署手册：构建、预安装和晋级](../../deploy/README.md#构建预安装和晋级)从包含 3.2 提交的
  clean HEAD 构建，并在目标机预安装；不在本文重复。
- **前置条件**：候选提交通过部署手册要求的必选 CI 任务。
- **通过标准**：候选 release 预安装完成且校验通过；第 4—6 步全部在这个不可变 release 中执行。
- **失败时**：按部署手册隔离未激活目录，从核准 archive 重新预安装。
- **副作用与授权**：预安装不切换 current；切换 current、刷新服务属于独立授权的发布动作，可在第 6 步后执行。
  新方案在未激活前即使进入 current，也因数据库无 active exact 而不会被调度（见 1.1）。

### 3.4 持久化回测（一次覆盖到最新数据日）

```bash
python -B -m harness gate backtest \
  --scheme-id {scheme_id} \
  --predict-date <DataBridge current 的 refresh_date> \
  --persist \
  --backtest-start-date 2025-01-01 \
  --timeout-sec 7200   # 缺省值，可省略或调低
```

- **参数含义**：回测覆盖 `2025-01-01` 起、`target_date < --predict-date` 的全部 Request，即实际值已可验证的全部样本；
  `--predict-date` 取本机 DataBridge current 的 `refresh_date`（最新可用数据日）。结果分区不看来源，只按
  [`feature_date`](../architecture/PREDICTION_SEMANTICS.md#20-唯一结果分区公开口径) 判定，覆盖到 2026-06-01 之后是正常的。
  `--backtest-start-date` 缺省即 `2025-01-01`，周频方案必须使用该值。`--persist` 必填，缺省直接阻断。
  `gate backtest` 的 `--timeout-sec` 缺省 7200 秒（完整回测预算），可显式调低；实际上限还受 Runtime Profile 约束。
- **前置条件**：第 3 步 release；目标环境绑定已核验；本机有 producer-ready generation；取得本次回测写库授权。
- **执行内容**：复验 canonical 两文件 → 读取 ready generation → 构造完整 HistoricalCase → 每个交付只启动
  一次 backtest 入口 → 逐行校验 Result → 在一个事务内写入 success run、完整明细与非空 monthly metrics，并保存
  exact、code/config/manifest hash、脚本校验策略摘要、Runtime Profile、环境指纹、generation 与 snapshot。
  增量方案使用私有状态，不读取或推进生产状态。
- **通过标准**：退出码 0，`status: passed`；Evidence 中 `run_id`、`scheme_version`（等于当前 exact）、
  `generation_id`、`data_snapshot_id` 非空，`records`、`monthly_metrics` 大于 0，`target_date_before` 等于给定的 `--predict-date`。
- **失败时**：保留失败 run 与现场；修复后重新执行，产生新的 immutable run，不更新、删除或补写旧 run。
  无 ready generation 时停止，等待 producer 独立发布。
- **副作用与授权**：写 `t_backtest_*`（仅经 `backtests.repository`）；不写产品事实，不改变生命周期。

### 3.5 激活

```bash
python -B -m harness activate --scheme-id {scheme_id}
```

- **前置条件**：
  - 与 3.4 同一 release，canonical 自回测后未变；
  - 存在与当前 exact 完全匹配的成功持久化回测：exact、code/config/manifest hash、Runtime Profile、
    环境指纹（等于 release 冻结 manifest）、generation 与 snapshot；脚本校验策略摘要须等于当前
    [策略](../../shared/blackbox_v2/_script_policy.py)或经[受审等价映射](../../shared/blackbox_v2/_script_policy_compat.py)
    指向当前策略，否则按当前策略重新回测；
  - **owner 确认**：首次激活把 Metadata 的 `name/description/owner` 写入 Registry，此后同 ID 修订不再更改。
    激活前与业务确认 Metadata `owner` 正确；不一致时停止，由业务决定上游重新交付（新 exact、重新回测）
    或激活后另行授权更正 Registry，不得自行修改交付字节；
  - 取得该方案明确的激活授权。
- **执行内容**（一个事务）：锁定并复核回测证据、拒绝 base/composite 身份冲突；insert-only 发布缺失的
  `t_scheme_predictions` 历史产品事实（`backtest_run_id` 指向证据 run）；建立 active exact 与全部 active
  composite Registry、审批人与时间；锁内读回 Registry 三字段与 Metadata 严格相等后提交，任一步失败整体回滚。
- **通过标准**：退出码 0，Evidence `activation_mode: initial`、`identity_created: true`、
  `version_status`/`registry_status` 均为 `active`、`registry_scheme_ids` 覆盖全部 `{id}__h{horizon}__{tenor}`，
  `config_changed: false`。
- **失败时**：事务已整体回滚，按 Evidence/错误修复原因后重新执行；证据不匹配时重新回测当前 exact；
  身份冲突时停止，不覆盖、删除或手工改 Registry。
- **副作用与授权**：写 `t_scheme_versions`、`t_scheme_registry` 与历史产品事实；激活后下一个自然窗口起
  宿主调度会执行该方案（前提是 current 已包含该方案）。激活不修改 canonical，不安装或变更控制面。

### 3.6 衔接补齐（回测末尾 → 首次自然运行）

回测只含实际值已可验证的样本，多日期限方案（如 T+5）最后几个特征日的预测"已应发出、结果未出"，不在回测里；
自然调度又只从激活后的下一个窗口开始。中间这几个点用补跑入口补齐（T+1 通常为 0 个，T+5 约 4 个）。
这与修复漏跑使用同一工具，产出的事实同样按 `feature_date` 归入结果分区。

`weekly_point`(h1)、`T+1`、`T+5` 可用区间入口（规则见[第 6 节](#6-区间补跑)）：

```bash
python -B -m harness signal-gap-fill --scheme-id {scheme_id} \
  --target-date-from <3.4 的 --predict-date> --target-date-before <首个由自然调度负责的 target_date> \
  --timeout-sec <批次预算秒数>
```

其它任务类型按权威交易日历逐个 `predict_date` 使用单日入口（行为见[第 5 节](#5-可选后续动作)）：

```bash
python -B -m harness signal-gap-fill --predict-date YYYY-MM-DD --scheme-id {scheme_id}
```

- **前置条件**：3.5 已激活；同一 release 与环境绑定；本机 DataBridge authority 可用；取得补跑写库授权。
  增量方案需复用状态时按[部署手册](../../deploy/README.md#外置状态与启动环境)设置独立的 `BFL_BACKFILL_STATE_ROOT`，不得指向生产状态目录。
- **范围**：从回测覆盖终点到首个自然窗口之前；区间内任一业务键已存在即整组拒绝，单日入口对已有业务键返回 `SKIP_PRESENT`。
  应在激活后、首个自然窗口前完成。
- **通过标准**：区间入口退出码 0 且 `status: PASSED`；单日入口每日为 `PASSED`、`SKIP_NOT_DUE` 或 `SKIP_PRESENT`。
  按[入库完整性](../architecture/PREDICTION_SEMANTICS.md#52-入库完整性)核对：从 2025-01-01 到最新应发点，每个应有业务键恰好一条产品事实。
- **失败时**：`BLOCKED`（退出码 2）无写入，按 `failure_code` 处理；执行失败先读回已发生的 run 与预测，
  再按实际缺口重试，不删除旧结果、不 fallback 到旧版本或其它方案。
- **副作用与授权**：写补跑 run 与预测（insert-only）；不改变生命周期与调度。

### 3.7 生产方案名单（仅运维确认时）

`<BFL_RUNTIME_ROOT>/config/production_schemes.json` 只控制 Dashboard 的生产标记，由运维确认，
不从激活、部署矩阵或自然运行推断，也不影响执行资格。默认**不属于**入库步骤；需要加入时按
[部署手册：生产方案名单维护](../../deploy/README.md#生产方案名单维护)执行，规则见
[Dashboard 合同](../operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#生产方案标记)。

### 3.8 接管验收

```bash
python -B -m harness gate dashboard --scheme-id {scheme_id} \
  --api-base-url <本机 API> --api-prefix <路径前缀> --session-file <owner-only 会话文件>
python -B -m harness gate data-consistency --scheme-id {scheme_id} \
  --api-base-url <本机 API> --api-prefix <路径前缀> --session-file <owner-only 会话文件>
```

`--api-base-url` 省略时按 `BFL_DEPLOYMENT_TARGET` 推导（`aliyun-gray` 为 8100，`mac3-production` 为 8101）；
环境变量未设置且未传该参数时以退出码 2 拒绝，不再默认 8100。

- **前置条件**：current 已切到包含该方案的 release 且服务已按授权刷新；有效登录会话；数据库环境绑定到该 HTTP 服务所在主机。
- **通过标准**：两个 Gate 退出码 0；逐项满足[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)。
  `--scheme-id` 可重复以批量核对；会话与 HTTP 规则见[Dashboard 合同](../operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#认证响应与合同验收)。
- **失败时**：Dashboard Gate 失败先查 Registry 与 release；一致性 Gate 返回 `blocked` 时在稳定输入窗口重试；
  事实错误按[第 7 节](#7-失败处理)处理，不改写已发布事实。
- **副作用与授权**：只读 HTTP 与数据库。Dashboard 可见性不证明 exact 或自然运行。

### 3.9 自然运行观察

- **前置条件**：控制面 installed/loaded 已按[调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)读回；
  新方案复用现有 cadence 的 one-shot，不新增 timer/plist。
- **通过标准**：首个自然窗口由真实 launchd/systemd 时钟产生该方案成功的 `scheduled_live` run 与预测，
  并在 Dashboard 可见；增量方案日常推进成功。
- **失败时**：按调度治理处理漏跑；补缺使用 3.6 的入口并另行授权。模拟运行或补缺不能标为自然运行通过。
- **副作用与授权**：观察本身只读；控制面安装、重启、Writer 切换另行授权。

## 4. 同 ID 修订

与新方案的差异：

- 不执行 Intake；直接替换获准的 canonical 两文件及必要 config，部署矩阵通常不变，再从 3.3 开始。
- 任何交付字节或参与 exact 的 config 变化都会产生新 exact；回测后发生漂移必须重新回测，不能复用旧证据。
- `activate` 自动识别修订：原子替换唯一 active exact（Evidence `activation_mode: revision`），
  不重写历史产品事实，保留既有 Registry `name/description/owner`；边界见[共享契约](../architecture/SCHEME_CONTRACT.md#3-方案身份)。
- 修订不需要衔接补齐；切换期间的漏点按 3.6 的补跑入口单独授权处理。

## 5. 可选后续动作

以下动作不属于入库门禁，均需独立授权：

- 单日 live 缺口：`python -B -m harness signal-gap-fill --predict-date YYYY-MM-DD [--scheme-id {scheme_id}]`。
  省略 `--scheme-id` 时扫描当日全部应运行的 active 方案，仅在授权覆盖完整范围时使用。`SKIP_NOT_DUE`、
  `SKIP_PRESENT` 为无写入的正常结果，仅 `GAP_FILL` 执行；每个 base 独立提交、多 target 组内原子，
  一个方案失败不回滚其他方案。
- 产品读回、调度安装、timer/plist 变更、服务重启、Writer 切换：按 3.8、3.9 与调度治理执行。

单日与区间入口都在内部规划，不接收外部 plan、token 或 operator；任一 blocker 在执行前停止，不自动重试、
fallback 或切旧版本。Signal gap fill 按日期/区间分锁，并行规则见[部署手册](../../deploy/README.md#外置状态与启动环境)。

子进程组清理未确认时，正式 one-shot 的共享启动围栏阻止后续候选及自动重试，摘要标记
`process_cleanup_unconfirmed`；补缺以批次失败结算已创建且未结算的 run。继续前先确认该进程组已退出，并读回本机
run 与预测写入状态。

### 5.1 增量方案的显式预热/重建

仅适用于 `incremental_state: true`、已完成目标环境验证并取得本次状态维护授权的 exact version；
新增量方案在首个自然窗口前须完成一次，否则日常运行因缺状态明确失败。

执行前按[手工 Harness 环境绑定](../../deploy/README.md#手工-harness-的目标环境绑定)核验进程，并从 installed/loaded
控制面及环境文件读取真实 Runtime Profile allowlist（当前 `LANG/LC_ALL/TZ`）：真实入口缺少的变量须从维护进程移除，
不能设为空值或继承 SSH locale。已有状态与真实环境不匹配时，保留原证据后受控重建。

```bash
python -B -m harness rebuild-blackbox-state \
  --scheme-id {scheme_id} --predict-date YYYY-MM-DD \
  --expected-scheme-version <exact-version> --approved-by <operator> \
  --project-root <immutable-release>
```

该入口复用 ready 输入、标准 Request 和唯一 executor，从空状态计算并原子发布新状态；不创建
run/prediction/backtest/Actual，不激活或补发信号，但会写状态文件，不是只读验证。

| 核验项 | 通过条件 |
|---|---|
| 位置与独占 | `BFL_RUNTIME_ROOT/blackbox-state` 按 base/exact 隔离；可信根内无穿越或 symlink；同 base 重建与日常推进共用非阻塞锁，拒绝第二 Writer |
| 执行预算 | 冷重建与 stateful 回测默认 `min(Profile.backtest_timeout_sec, 7200)` 秒，日常增量 predict 不超过 `min(Profile.predict_timeout_sec, 120)` 秒；内存默认不超过 Profile 与 4 GiB 的较小值；单次覆盖按[部署手册](../../deploy/README.md#外置状态与启动环境) |
| 状态身份 | payload 不超过 16 MiB；封装绑定 exact、generation/snapshot、私有输入摘要、实际代码/Metadata/环境及完整性校验 |
| 发布与证据 | Result、输入和状态全部复验后同目录原子发布；记录 operator、版本、日期和状态摘要；用正常增量路径验收 |

缺状态、损坏、版本/环境不符或算法拒绝复用均明确失败，不自动 fallback、重建或重试。发布后目录 fsync 失败可能已留下
完整新状态：报告失败，按相同 Request 受控重试，不覆盖回旧状态。文件锁不替代 Writer 操作授权。

## 6. 区间补跑

区间入口只支持 exact active Blackbox 的以下组合（[`plan_signal_gap_target_range`](../../harness/signal_gap_plan.py)）：

| frequency | task_type | horizon |
|---|---|---:|
| `weekly` | `weekly_point` | 1 |
| `daily` | `T+1` | 1 |
| `daily` | `T+5` | 5 |

其它组合返回 `TARGET_RANGE_CONTRACT_UNSUPPORTED`，改用单日入口；`--target-date-from` 早于 2026-06-01 返回
`TARGET_RANGE_BEFORE_PLATFORM_LIVE_START`。代码支持不等于目标环境已验收，未闭环的现场验证见[TODO](../TODO.md)。

1. 按[预测语义 5.2](../architecture/PREDICTION_SEMANTICS.md#52-入库完整性)确认逐 Request 截止与 live-safe；
   无法证明时改用逐点单日入口。
2. 固定 exact、DataBridge authority、输入 lineage 与授权的 target 半开区间；日期由权威交易日历枚举。
3. 执行 3.6 的区间命令。一个区间只解析一次输入 authority、物化一次私有视图并启动一个算法 batch；任一业务键已有即整组拒绝。
4. 核验 repository 原子提交的全部日期 run 与预测：每个应有业务键恰好一条事实、零缺口，exact 与 Actual 关联正确。

## 7. 失败处理

| 场景 | 处理 |
|---|---|
| Intake 失败 | 修正交付后重新 Intake；目标目录不得有部分写入 |
| 部署矩阵测试失败 | 修正清单后重跑；不改交付字节 |
| 无 ready generation | 停止；由 DataBridge producer 独立发布，方案流程不代建 |
| 回测失败 | 保留失败现场；修复后创建新的完整回测 run |
| exact/环境/输入证据不匹配 | 禁止激活，对当前 exact 重新回测 |
| 身份冲突 | 禁止覆盖、删除或手工改 Registry |
| owner 与业务确认不一致 | 激活前停止，交业务决定（见 3.5） |
| activation 事务失败 | 已整体回滚；修复原因后重新 `activate` |
| 补缺 `BLOCKED` 或部分失败 | 读回已提交事实，按实际缺口重试；不删除旧结果 |
| backtest/live 重叠 | 保留旧 run，创建新的正确 canonical run |
| DataBridge 或日历失败 | 当前运行 fail-closed，不 fallback 到旧 generation |

## 8. 完成条件

新方案入库完成需同时具备：3.4 成功回测与 3.5 激活读回对应同一 exact；3.6 衔接补齐后应有业务键集完整；3.8 两个 Gate 通过；
3.9 首个自然窗口观察结果（未到窗口可记为待观察，不能以补缺或模拟代替）。同 ID 修订以第 4 节为准。

运行证据保留在本机控制面和外置目录（exact、输入、回测、激活与授权摘要）；不把内部 operation id、凭据或完整输入
提交到仓库。动态事实只记入[当前状态](../CURRENT_STATUS.md)。
