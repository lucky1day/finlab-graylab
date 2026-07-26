# Blackbox V2 Platform Inputs and FengRL Monthly Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在独立工作树中增加可扩展、可审计的 Blackbox V2
`platform_inputs`，提供 `api-wind-date-v1`，实现真正零数据库写入的
`onboard --check-only`，再按 `1Y → 3Y → 5Y → 7Y → 10Y` 串行完成五个
月度方案的技术入库。

**Architecture:** DataBridge 继续只冻结三个业务 CSV，并保持原有
`BlackboxSnapshot` 身份。平台输入由封闭 provider registry 解析、规范化
和冻结，再与父快照组成稳定的执行输入身份。Harness 从只读数据库捕获
日历；scheduled 从同批、已核验的 Native generation 取得冻结日历；二者
共享 provider 校验但分别记录 provenance。每次算法运行只获得精确、
只读、私有的临时文件视图。`--check-only` 运行七个自动 Gate，但完全
绕过 Harness 控制面和业务数据持久化。

**Tech Stack:** Python 3.12、pandas、PyYAML、`unittest`、现有 Harness
CLI/Orchestrator、Blackbox subprocess sandbox、Git worktree。

---

## 0. 固定边界与执行组织

- [x] 仅使用工作树
  `/Users/macstudio0/.config/superpowers/worktrees/bond-factor-lab/blackbox-v2-monthly-fengrl-review-20260726`
  和分支 `codex/blackbox-v2-monthly-fengrl-review-20260726`。
- [x] 设计文档明确每日 `00:01` 日历插入、DataBridge 三文件不变、
  Harness/Native 两种冻结来源及 provenance/identity 分离。
- [ ] 每个共享平台任务由一个 fresh implementer 按 TDD 实现；主代理
  独占 Git index 和 commit。
- [ ] 每个实现任务依次通过规格 reviewer、质量 reviewer；发现问题只由
  原 implementer 修复。
- [ ] 平台完成后并行运行三个只读测试组；真实方案之间绝不并行。
- [ ] 全程不激活 Registry、不运行 persistent backtest、不写
  gray/scheduled live 或生产业务表、不改 rollout/admission、
  BondProjectPro、日频 coordinator，不 merge/push/deploy。

## 1. Registry、配置、Intake 与版本兼容

**目标文件：**

- 新增 `shared/blackbox_v2/platform_input_registry.py`
- 修改 `shared/blackbox_v2/intake.py`
- 修改 `shared/blackbox_v2/versioning.py`
- 修改 `scheduler/discovery.py`
- 修改 `harness/cli.py`
- 新增 `tests/test_blackbox_v2_platform_inputs.py`
- 修改 `tests/test_blackbox_v2_intake.py`
- 修改 `tests/test_blackbox_v2_discovery.py`
- 修改 `tests/test_blackbox_v2_metadata.py`

### TDD 步骤

- [ ] 先写失败测试，覆盖：
  - `platform_inputs` 仅接受非空、唯一、已注册的版本化 ID；
  - 手写配置允许乱序，但 discovery/versioning 统一排序；
  - 显式 `[]`、未知 ID、重复 ID、Native 配置声明该字段均拒绝；
  - Metadata 出现 `platform_inputs` 仍按额外字段拒绝；
  - Intake 的可重复 `--platform-input` 拒绝重复并按 ID 排序写入；
  - 两文件交付规则不变；
  - 未声明字段时不进入 canonical payload；
  - 既有 golden config hash 固定为
    `268f80abf7391ead1ee31c775a7d303aa3500b4ad081663d45e476b8b928f669`。
- [ ] 运行新测试并确认失败原因是接口尚未实现：

```bash
python -m unittest \
  tests.test_blackbox_v2_platform_inputs \
  tests.test_blackbox_v2_intake \
  tests.test_blackbox_v2_discovery \
  tests.test_blackbox_v2_metadata
```

- [ ] 建立纯配置 registry；每个 spec 至少声明 artifact ID、provider
  version、唯一文件名和精确列契约。
- [ ] 给 Intake CLI 增加
  `--platform-input api-wind-date-v1`（可重复、重复值 fail-closed）。
- [ ] `SchemeConfig` 以规范化 tuple 暴露字段；Native 路径主动拒绝。
- [ ] canonical config 只有在原配置显式声明时才加入非空排序列表。
- [ ] 跑目标测试至绿并检查旧方案 hash/version 逐位不变。
- [ ] 规格评审、质量评审、必要修复和复评。
- [ ] 主代理提交：`feat: define Blackbox platform input registry`。

## 2. `api-wind-date-v1` Provider

**目标文件：**

- 新增 `shared/blackbox_v2/platform_inputs.py`
- 修改 `shared/input_artifacts.py`
- 扩展 `tests/test_blackbox_v2_platform_inputs.py`
- 修改 `tests/test_blackbox_v2_input_artifacts.py`

### Provider 契约

```text
artifact_id: api-wind-date-v1
provider_version: api-wind-date-provider-v1
filename: api_wind_date.csv
columns: rdate,week_id
```

### TDD 步骤

- [ ] 先写失败测试，覆盖精确列、空表、非法日期、空周键、重复/乱序
  日期、cutoff 未覆盖、增列/缺列。
- [ ] 覆盖 `week_id` 接受整数或尾随 `.0`，输出六位平台键。
- [ ] 覆盖输出 UTF-8、LF、固定列顺序，且不按 `feature_date` 截断。
- [ ] 覆盖 DB capture 与 Native frame 相同内容时规范 bytes/SHA 完全
  相同。
- [ ] Harness capture 复用
  `read_calendar_snapshot_from_connection()`；scheduled capture 复用
  `NativeGenerationContext.frame("api_wind_date")`。
- [ ] source kind、generation ID、manifest SHA 和捕获时间只写审计
  provenance。
- [ ] 跑目标测试至绿。
- [ ] 规格评审、质量评审、必要修复和复评。
- [ ] 主代理提交：`feat: add api wind date platform provider`。

## 3. 组合输入身份与私有临时运行视图

**目标文件：**

- 修改 `shared/blackbox_v2/snapshot.py`
- 修改 `shared/input_artifacts.py`
- 修改 `tests/test_blackbox_v2_snapshot.py`
- 修改 `tests/test_blackbox_v2_input_artifacts.py`

### 数据模型

组合对象至少携带：

```text
combined_snapshot_id
parent_snapshot_id
base_snapshot
platform_input_ids
platform_input_artifacts
expected_filenames
identity_manifest
audit_manifest
```

### TDD 步骤

- [ ] 断言 `SNAPSHOT_FILENAMES` 始终恰好三个 DataBridge 文件。
- [ ] 先写组合身份测试：
  - 无制品直接沿用父 snapshot ID；
  - 有制品只哈希 identity schema version、父 snapshot ID、排序后的
    artifact ID/provider version/filename/SHA/size/row count/columns；
  - timestamps、temp paths、source kind/generation 不影响 ID；
  - Harness/Native 来源对相同内容生成相同组合 ID。
- [ ] 先写运行视图测试：
  - 每次创建唯一私有临时目录；
  - 精确物化预期普通文件，不使用 symlink/hardlink；
  - 写入后复核 SHA；
  - 文件 `0444`、目录 `0555`；
  - 正常退出清理；
  - 终止状态不确定时保留到受控 debris cleanup；
  - 不长期复制三份大型 CSV。
- [ ] 实现 immutable artifact/bundle/view 对象与稳定 canonical JSON
  identity。
- [ ] 跑目标测试至绿。
- [ ] 规格评审、质量评审、必要修复和复评。
- [ ] 主代理提交：`feat: compose Blackbox runtime input bundles`。

## 4. Runner、Sandbox 与 Executor 集成

**目标文件：**

- 修改 `scheduler/blackbox_v2_runner.py`
- 修改 `scheduler/executor.py`
- 修改 `tests/test_blackbox_v2_runner.py`
- 修改 `tests/test_databridge_generation_executor.py`
- 修改 `tests/test_scheduled_executor.py`

### TDD 步骤

- [ ] 先写失败测试，证明 Runner 只接受规范化 artifact ID，不接受
  调用方自由文件名。
- [ ] 无制品只允许三文件；日历制品只允许三文件 +
  `api_wind_date.csv`。
- [ ] 缺文件、未声明第四文件、第五个文件、symlink、非常规文件均
  fail-closed。
- [ ] Sandbox 只增加声明制品的 literal read 权限；网络、数据库、
  data-dir 写入仍拒绝；delivery 旁日历不可读。
- [ ] scheduled 路径只从已与 DataBridge 核对 generation ID/manifest
  SHA 的 Native generation 取得日历，不新增 mutable current 或 DB
  路径。
- [ ] Harness 路径通过只读 Engine 捕获。
- [ ] `PredictionRecord.extra.data_snapshot_id` 改为组合 ID，并记录父
  ID、artifact manifest 和来源 provenance。
- [ ] 证明 DataBridge generation、Native generation、scheduled
  coordinator 的生成语义未改变。
- [ ] 跑目标测试至绿。
- [ ] 规格评审、质量评审、必要修复和复评。
- [ ] 主代理提交：`feat: enforce declared Blackbox runtime inputs`。

## 5. 正式零写入 `onboard --check-only`

**目标文件：**

- 修改 `harness/cli.py`
- 修改 `harness/context.py`
- 修改 `harness/orchestrator.py`
- 修改 `harness/blackbox_v2/gates.py`
- 必要时修改 Harness report/result 数据模型
- 修改 `tests/test_blackbox_v2_harness_gates.py`
- 修改 `tests/test_blackbox_v2_harness_dispatch.py`
- 修改 `tests/test_blackbox_v2_backtest_persistence.py`
- 修改 `tests/test_blackbox_v2_api_gate.py`
- 修改 `tests/test_harness_persistence.py`

### TDD 步骤

- [ ] 先写失败测试，patch
  `persist_harness_run_start/persist_gate_result/persist_harness_run_finish`
  并断言 check-only 下调用次数均为零。
- [ ] 证明 check-only 仍可使用只读 Engine 构造日历、cutoff、Request。
- [ ] 证明七 Gate 按
  static→input→unit→dry-run→compare→backtest→api-readiness fail-fast。
- [ ] 生成本地 `harness_run_id`、统一报告和 Gate JSON。
- [ ] 报告固定包含：

```text
check_only=true
control_plane_persisted=false
business_tables_written=false
persist_backtest=false
```

- [ ] `--check-only` 与授权 token、持久化、shadow/live/activate 或 API
  等副作用阶段不兼容并 fail-closed。
- [ ] 所有 Gate 共享同一组合输入：
  - Static 记录声明 provider；
  - Input 分组记录三频父快照与平台制品；
  - Unit/Dry-run/Compare/Backtest/API readiness 记录相同组合 ID；
  - Compare 只变异 `SNAPSHOT_FILENAMES` 三个业务文件并证明日历 SHA
    未变；
  - Backtest 强制 `persist=false`；
  - API readiness 只做结构验证。
- [ ] 非 check-only 现有行为保持兼容。
- [ ] 跑目标测试至绿。
- [ ] 规格评审、质量评审、必要修复和复评。
- [ ] 主代理提交：`feat: add no-write Blackbox check-only onboarding`。

## 6. SOP、架构、Contract 与文档契约

**目标文件：**

- `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- 相关 `docs/architecture/*.md`
- `docs/architecture/SCHEME_CONTRACT.md`
- `tests/test_onboarding_docs.py`

### TDD 步骤

- [ ] 先扩展文档契约测试并确认红灯。
- [ ] 上游 SOP 明确正式交付仍只有 `.py + .json`，随包日历只作自验。
- [ ] 平台 SOP 记录 `--platform-input`、组合身份、运行视图、
  `--check-only` 与零写入边界。
- [ ] 架构文档明确 DataBridge 三文件父快照和平台制品组合身份。
- [ ] Contract 明确 Blackbox 输入为“三频父快照 + 显式声明的平台
  制品”。
- [ ] 文档测试和 `git diff --check` 通过。
- [ ] 规格评审、质量评审、必要修复和复评。
- [ ] 主代理提交文档变更。

## 7. 平台联合验收

- [ ] 三个只读 subagent 并行运行：

```text
A: test_blackbox_v2_platform_inputs / intake / discovery / metadata
B: snapshot / input_artifacts / runner / databridge_generation_executor /
   scheduled_executor
C: harness_gates / harness_dispatch / backtest_persistence / api_gate /
   harness_persistence / onboarding_docs
```

- [ ] 主代理重新运行 A+B+C 联合测试。
- [ ] 主代理运行完整：

```bash
python -m unittest discover -s tests
git diff --check
python -m harness gate static <representative_scheme_id>
```

- [ ] 任何失败只交给一个 implementer 修复，再重复规格/质量评审与
  全部验证。
- [ ] 平台全绿、工作树 clean 后才允许 Intake 第一个真实方案。

## 8. 五个真实方案严格串行入库

固定次序：

1. `cgb_a4_fundseason_1y`
2. `cgb_a4_fundseason_3y`
3. `cgb_a4_fundseason_5y`
4. `cgb_a4_fundseason_7y`
5. `cgb_a4_fundseason_10y`

对每个方案重复以下 checklist；前一方案 commit 且工作树 clean 后才
开始下一方案：

- [ ] 核对当前分支、clean status、前一方案提交。
- [ ] 核对 Metadata、必填 description、scheme ID、期限和上游
  `.py/.json` SHA。
- [ ] 只把精确 `.py + .json` 复制到私有临时 Intake 目录。
- [ ] 明确排除上游 `api_wind_date.csv`、`sample_data/`、`examples/`、
  `dev_checks/` 和交接文档。
- [ ] 运行 Intake：

```bash
python -m harness intake-blackbox \
  --delivery-dir <exact-two-file-temp-dir> \
  --project-root <worktree> \
  --runtime-profile blackbox-v2-v1 \
  --data-schema-version data-bridge-v1 \
  --platform-input api-wind-date-v1
```

- [ ] 使用月度自然触发日运行：

```bash
python -m harness onboard <scheme_id> \
  --predict-date 2026-07-15 \
  --stage all \
  --check-only \
  --algo-env forecast_env_blackbox_v1 \
  --timeout-sec 1800
```

- [ ] 验证七 Gate 7/7 passed、check-only、控制面零持久化、
  backtest 100/100 且 `persist=false`。
- [ ] 验证 DataBridge 父快照仍为三文件，组合快照含
  `api-wind-date-v1`，cutoff 被日历覆盖，所有 Gate 中日历 SHA 一致。
- [ ] 验证重复/分批/逆序/未来业务行隔离，且 Registry、prediction、
  run、backtest 表没有写入。
- [ ] 两个只读 reviewer 并行核查：
  1. Gate/evidence/零写库边界；
  2. Git diff、交付 bytes、状态文档和提交范围。
- [ ] 第一方案创建并索引
  `docs/blackbox_v2/records/MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md`
  与 `.evidence.json`；后续方案只更新自身记录。
- [ ] 每个方案提交只包含 config、两个 delivery 文件和对应状态/evidence
  更新。

若任一 Gate 失败：

- [ ] 立即停止当前方案和整个后续批次。
- [ ] 状态记录为准确的 `BLOCKED_<GATE>`，不得写“技术通过”。
- [ ] 保留失败报告和现场，不 Intake 下一方案。
- [ ] 不修改上游算法来贴合平台结果。

## 9. 最终验收与交接

- [ ] 五方案后重新运行平台联合回归、完整 `unittest discover`、文档
  测试、StaticGate 和 `git diff --check`。
- [ ] 每个方案恰好一个独立入库提交。
- [ ] 所有 config 均满足：

```yaml
status: paused
version_status: draft
runtime_type: blackbox_v2
platform_inputs:
  - api-wind-date-v1
```

- [ ] 更新 `docs/CURRENT_STATUS.md`。
- [ ] 复核无 Registry 激活、gray/scheduled live、persistent
  backtest、rollout/admission、BondProjectPro、coordinator、master、
  push 或 deploy 变更。
- [ ] 最终规格 reviewer 与质量 reviewer 对整条分支只读审计。
- [ ] 输出提交清单、测试/证据路径、已知上游文档问题和主会话可按顺序
  合并的交接说明；分支保持未推送、未合并。
