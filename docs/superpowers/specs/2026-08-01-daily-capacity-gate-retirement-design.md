# 日度离线 Capacity Gate 退役设计

**文档状态**：`CURRENT`

**核验日期**：2026-08-01

## 背景与目标

第一批已经删除不再被生产调用的 capacity admission runtime。第二批继续采用保守
清理，只退役同样没有生产调用方、也没有现行 SOP 入口的离线容量观测 gate 和 CLI。

生产日度链仍为 `scheduler.main` / `scheduler.daily_runtime` →
`scheduler.daily_direct_authority` → `scheduler.capacity_candidate_runtime` 的只读 direct
authority 校验。本批不改变该调用链，不拆分 candidate/attestation，也不调整任何
调度、写库或方案执行行为。

## 调用图结论

- `scheduler/capacity_gate.py` 的唯一非测试调用方是
  `scripts/evaluate_daily_capacity_gate.py`。
- `scripts/evaluate_daily_capacity_gate.py` 没有生产入口、launchd/plist 引用或现行 SOP
  入口，只提供读取历史容量观测 JSON 的离线判断。
- `scheduler/capacity_attestation.py` 仍被
  `scheduler.capacity_candidate_runtime` 使用，但它当前从 gate 模块导入四个常量；因此
  删除 gate 前必须先把其中三个日度策略常量改为从现行 policy v2 契约派生。
- `OBSERVATION_SCHEMA_VERSION` 属于 attested evidence 自身的输入 schema，不属于日度
  调度策略；它继续定义在 `capacity_attestation.py`，不创建新的兼容模块。
- `tests/test_capacity_gate.py` 同时混有 gate、CLI 和 attestation 三类测试。删除整个
  文件会误删仍在使用的 candidate/attestation 合同测试，必须先按职责收敛。

## 方案比较

### 方案 A：模块闭环删除（采用）

删除 gate 与 CLI，保留 attestation/candidate；日度策略常量从
`scheduler.daily_policy` 派生，测试文件收敛并改名。该方案能完整移除无调用方的
离线执行链，同时不移动生产 direct authority 的实现边界。

### 方案 B：只删除 CLI

改动最小，但会留下 1,253 行只有测试调用的 gate 死代码，无法实现本批清理目标。

### 方案 C：同时删除 attested evidence 与 candidate 框架

删除量最大，但会开始重构生产 direct authority 依赖的 candidate/attestation 边界，
需要独立设计和更强回归，不纳入本批。

## 精确改动范围

### 删除离线实现

精确删除：

- `scheduler/capacity_gate.py`
- `scripts/evaluate_daily_capacity_gate.py`

不保留 redirect、兼容 wrapper、空模块或 deprecated alias。Git 历史承担旧离线工具的
追溯。

### 让 attestation 从现行日度策略派生常量

修改 `scheduler/daily_policy.py`：

- 新增单一 `DAILY_POLICY_V2_VERSION = "daily-scheduler-policy-v2"` 常量；
- `SUPPORTED_POLICY_VERSIONS` 使用该常量，不改变允许的 v1/v2 集合。

修改 `scheduler/capacity_attestation.py`：

- `SUPPORTED_POLICY_VERSION` 绑定 `DAILY_POLICY_V2_VERSION`；
- `EXPECTED_TARGET_COUNT` 绑定 `EXPECTED_V2_TARGET_COUNT`；
- `EXPECTED_V2_SCHEME_IDS` 从
  `EXPECTED_POLICY_V2_RELEASE_OFFSETS_BY_SCHEME` 的键派生；
- `OBSERVATION_SCHEMA_VERSION = "daily-capacity-observations-v2"` 留在 attestation
  模块，因为保留的 attested evidence validator 仍需识别该 schema；
- candidate 构造、candidate fingerprint、attested evidence 校验和所有错误语义不变。

该修改只消除对待删除模块的 import，不新增可配置项，也不改变 policy JSON。

### 收敛测试职责

将 `tests/test_capacity_gate.py` 改名为
`tests/test_capacity_attestation.py`，保留：

- candidate/target builder 与 canonical hash helpers；
- `CapacityAttestationContractTests` 中不依赖离线 CLI 的全部合同测试；
- attested evidence 的候选绑定、fault trial、cache qualification 和结构校验测试。

删除：

- 整个 `CapacityGateTests`；
- `CapacityAttestationContractTests` 中两个直接调用离线 CLI 的测试；
- 整个 `CapacityGateCliTests`；
- 清理后不再使用的 `json`、`tempfile` 和 `Path` import。

在 `tests/test_architecture_boundaries.py` 新增独立门禁，要求以下路径不存在：

- `scheduler/capacity_gate.py`
- `scripts/evaluate_daily_capacity_gate.py`

该门禁先在旧文件仍存在时观察预期失败，再实施删除。

## 文档边界

现行 `docs/sop/`、`docs/architecture/`、`deploy/README.md` 和根入口没有指向离线
capacity gate CLI，因此本批没有需要删除的现行业务文档。

以下内容继续保留：

- 历史设计、实施计划和 onboarding records 中对旧 capacity gate 的事实记录；
- 第一批清理的设计与实施计划；
- 当前状态中关于旧 admission 已退役、direct authority 仍为生产目标的说明。

历史事实不得因为代码退役而改写；文档门禁负责确保现行入口没有新增旧 CLI 链接。

## 行为不变量

本批完成后必须保持：

1. `scheduler.daily_direct_authority` 的 import、公开接口和返回结构不变。
2. `scheduler.capacity_candidate_runtime` 的代码和测试行为不变。
3. candidate/attestation 的 schema、fingerprint、目标集合和错误消息不变。
4. `deploy/`、launchd/plist、`scheduler.main`、ledger runtime 和 migration 不变。
5. 数据库、registry、方案配置、DataBridge、Native/Blackbox 算法和生产服务状态不变。
6. 不增加新的依赖、配置文件、兼容层或运行时分支。

## 验证策略

实现时按以下顺序执行：

1. 增加 gate/CLI 退役路径门禁，并确认旧文件存在时测试精确失败。
2. 先让 attestation 常量改从 `daily_policy` 派生，再删除 gate/CLI。
3. 收敛并改名测试文件，确认没有误删 attestation/candidate 合同覆盖。
4. 运行 architecture、daily policy、attestation、candidate、direct authority 和
   scheduler 定向回归。
5. 运行文档索引/链接门禁和全仓测试。
6. 运行 `compileall`、`git diff --check` 和退役引用扫描。
7. 对照本设计确认 `deploy/`、`scheduler.main`、daily direct authority、candidate
   runtime、ledger、migration、schemes 和 shared 均无越界修改。

## 分支与发布边界

- 本批只提交到 `codex/audit-bugfixes-20260613`。
- 不在本批完成后单独同步 `master`，也不推送任何远程分支。
- 后续继续逐批清理；只有形成用户认可的完整阶段并统一验收后，才再次请求同步
  `master` 和远程仓库的明确授权。
- 本批实现使用独立提交，不夹带 `outputs/`、生产产物或其他草稿。
