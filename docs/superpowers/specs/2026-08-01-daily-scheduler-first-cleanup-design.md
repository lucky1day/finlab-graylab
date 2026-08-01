# 日度调度冗余清理第一批设计

**文档状态**：`CURRENT`

**核验日期**：2026-08-01

## 背景与目标

日度调度的目标架构已经确定：以一次性 `launchd/plist` 日批入口作为最终生产
调度权威，再逐步退役 ledger coordinator 和常驻 APScheduler 的日度/Actuals
职责。但第一批不改变生产调度行为，只清理已经退出运行调用图的容量 admission
旧实现，以及一份与现状冲突的日度实施草案。

本批采用保守的依赖剥离方式：只有同时满足“无生产调用方、现行文档已宣告退役、
删除后可用结构门禁防止回流”的代码才进入删除范围。launchd 稳定性增强、重复执行、
覆盖写、授权和静默缺失等行为问题留在后续独立批次处理。

## 现状诊断

### 已退役但仍留在仓库的代码

- `scheduler/capacity_runtime_admission.py` 没有生产调用方，仅被对应测试引用。
- `scheduler/capacity_admission.py` 的唯一运行时代码调用方是上述已孤立模块；其余引用
  来自自身测试、旧说明和 scheduler release 必备文件清单。
- 现行日度路径不再读取 `deploy/daily_capacity_admission_v2.json`，现有门禁也已验证
  该控制文件不存在，因此继续保留 admission 实现只会形成错误的可用性暗示。
- `scheduler/capacity_candidate_runtime.py` 会扫描整个 `scheduler/` Python 源码树；
  删除旧模块后 release digest 会自然反映新的源码集合。必须同步从
  `SCHEDULER_RELEASE_REQUIRED_FILES` 删除旧文件名，避免候选重算因要求已退役文件而失败。

### 已失真的实施文档

`docs/sop/DAILY_GRAY_RUNNER_IMPLEMENTATION_PLAN_20260730.md` 仍标记为待确认草案，
却描述已经落地的独立 launchd 路径，同时保留 07:10、绕过 ledger 和同步 master 等
与当前事实或项目边界不一致的操作说明。该文件不再能安全指导实施，也不是需要保留在
现行 SOP 索引中的审计记录；Git 历史足以承担追溯用途。

## 设计决策

第一批只做“确定死亡代码 + 确定失真文档”的闭环删除，不顺手重构仍在运行的容量
candidate、attestation、gate 或 ledger 代码。

### 删除代码与对应测试

精确删除：

- `scheduler/capacity_runtime_admission.py`
- `scheduler/capacity_admission.py`
- `tests/test_capacity_runtime_admission.py`
- `tests/test_capacity_admission.py`

删除旧实现的专属测试是同一个原子变更。它们验证的是已退役接口，不应继续让测试套件
把该接口表达为受支持能力。

### 修正仍在使用的代码说明与清单

精确修改：

- `scheduler/capacity_candidate_runtime.py`：从
  `SCHEDULER_RELEASE_REQUIRED_FILES` 删除
  `scheduler/capacity_admission.py`；不改变 release 扫描范围、digest 算法或其他必备文件。
- `scheduler/capacity_attestation.py`：删除“runtime admission 由
  `scheduler.capacity_admission` 完成”的过期说明，保留本模块对结构验证和候选绑定的
  实际职责描述。
- 在 `tests/test_architecture_boundaries.py` 增加退役路径不存在门禁，明确拒绝上述两个
  scheduler 模块重新出现。

### 删除失真文档并修正索引

精确删除和修改：

- 删除 `docs/sop/DAILY_GRAY_RUNNER_IMPLEMENTATION_PLAN_20260730.md`。
- 修改 `docs/sop/README.md`，移除该草案的索引行。
- 文档链接门禁继续负责发现任何遗漏引用；不创建内容相同的替代页或 archive 副本。

本批预计直接删除 2,564 行旧代码、专属测试和失真草案，并增加一条小型结构门禁。

## 行为不变量

本批完成后必须保持：

1. `deploy/` 下所有 launchd plist、启动脚本和安装脚本字节级不变。
2. 07:00 日批的触发时间、入口、方案集合和写入阶段不变。
3. `scheduler.main` 的常驻任务、Actuals 任务和 weekly/monthly 职责不变。
4. ledger runtime、数据库 schema、数据库内容和迁移清单不变。
5. DataBridge、Native/Blackbox 算法、registry 和输入 artifact 行为不变。
6. 容量 candidate 的字段契约、校验规则和 digest 算法不变；源码集合因删除旧文件而
   产生的新 release digest 是预期结果，不通过兼容常量伪装成旧 release。

## 验证策略

实现时按以下顺序验证：

1. 先增加退役模块不存在门禁，并确认它在旧文件仍存在时失败。
2. 删除旧模块、专属测试和旧文档，完成两处最小同步修改。
3. 运行容量 candidate、attestation、gate 和架构边界的定向回归测试。
4. 运行 scheduler 相关测试及文档索引、相对链接检查。
5. 运行完整 `unittest` 测试套件、Python `compileall` 和 `git diff --check`。
6. 用 `rg` 确认已退役模块名和旧文档路径只允许出现在本设计记录或明确的
   “禁止回流”门禁中，不再存在可执行 import 或现行 SOP 链接。

项目服务环境以 `unittest` 为当前测试入口；若该环境未安装 pytest，不把缺少 pytest
解释为代码失败，也不为本批引入新的测试依赖。

## 明确不处理

本批不修改或执行：

- launchd/plist 内容、安装、重载、启停或生产首跑；
- 七个日度方案的 `scheduled_live` 提升和历史 `gray_live` 数据；
- 日度 ledger coordinator、occurrence、epoch 或 migration 017/018；
- APScheduler 日度/Actuals 职责退役；
- 覆盖写、重复运行、隐式授权、静默缺失的行为修复；
- `capacity_gate.py`、容量 attestation/candidate 的职责拆分；
- 数据库写入、生产服务状态、`master` 合并或远程发布。

## 后续批次边界

第一批通过后再分别设计和审批：

1. 容量 gate、attestation、candidate 的职责收敛与命名清理。
2. 以观测证据确认单一 launchd 日批稳定后，退役 ledger coordinator 的日度职责。
3. 再将常驻 APScheduler 的日度/Actuals 职责迁出，最终只保留仍需常驻调度的
   weekly/monthly 范围。

后续批次不得回填到本批提交，以便单独验证、审查和回滚。

## Git 与回滚边界

- 设计和实现都只发生在 `codex/audit-bugfixes-20260613`。
- 实现使用独立提交，不夹带 `outputs/`、生产产物或其他草稿。
- 不合并、覆盖或推送 `master`，除非用户后续明确授权。
- 若验证发现仍有生产调用方，停止删除并保留失败证据；若合入后发现遗漏，可整体
  revert 实现提交，恢复旧文件与 release 必备清单，不需要数据库回滚。
