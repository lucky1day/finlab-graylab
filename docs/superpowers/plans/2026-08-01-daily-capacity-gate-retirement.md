# Daily Capacity Gate Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除无生产调用方的离线 capacity gate/CLI 及其专属测试，同时让保留的 attestation 合同从现行日度 policy v2 派生身份常量。

**Architecture:** 生产 direct authority、candidate runtime 和 attestation 验证逻辑保持不变；先用两个失败测试锁定退役路径与 policy 常量来源，再迁移常量、删除离线模块并收敛测试文件。历史文档保留事实记录，现行入口继续不得引用旧 CLI。

**Tech Stack:** Python 3.12、`unittest`、Markdown、Git

---

### Task 1: 用失败测试锁定模块退役和常量来源

**Files:**
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `tests/test_capacity_gate.py`
- Test: `tests/test_architecture_boundaries.py`
- Test: `tests/test_capacity_gate.py`

- [ ] **Step 1: 增加 gate/CLI 不得回流门禁**

在 `tests/test_architecture_boundaries.py` 的 admission 退役门禁之后加入：

```python
def test_retired_daily_capacity_gate_and_cli_are_absent(self) -> None:
    project_root = Path(__file__).resolve().parents[1]
    retired = (
        project_root / "scheduler" / "capacity_gate.py",
        project_root / "scripts" / "evaluate_daily_capacity_gate.py",
    )

    self.assertEqual(
        [],
        [
            path.relative_to(project_root).as_posix()
            for path in retired
            if path.exists()
        ],
        "retired offline daily capacity gate and CLI must not return",
    )
```

- [ ] **Step 2: 增加 attestation 常量来源合同**

在 `tests/test_capacity_gate.py` 的 `CapacityAttestationContractTests` 开头加入：

```python
def test_daily_policy_v2_constants_are_authoritative(self) -> None:
    from scheduler import capacity_attestation, daily_policy

    self.assertTrue(
        hasattr(daily_policy, "DAILY_POLICY_V2_VERSION"),
        "daily policy must expose one authoritative v2 identity",
    )
    self.assertEqual(
        capacity_attestation.SUPPORTED_POLICY_VERSION,
        daily_policy.DAILY_POLICY_V2_VERSION,
    )
    self.assertEqual(
        capacity_attestation.EXPECTED_TARGET_COUNT,
        daily_policy.EXPECTED_V2_TARGET_COUNT,
    )
    self.assertEqual(
        capacity_attestation.EXPECTED_V2_SCHEME_IDS,
        frozenset(
            daily_policy.EXPECTED_POLICY_V2_RELEASE_OFFSETS_BY_SCHEME
        ),
    )
```

- [ ] **Step 3: 运行两个测试并确认预期红灯**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_architecture_boundaries.RepositoryArchitectureBoundaryTests.test_retired_daily_capacity_gate_and_cli_are_absent \
  tests.test_capacity_gate.CapacityAttestationContractTests.test_daily_policy_v2_constants_are_authoritative
```

Expected: 两项均为 `FAIL`。第一项精确列出
`scheduler/capacity_gate.py` 和
`scripts/evaluate_daily_capacity_gate.py`；第二项只因
`DAILY_POLICY_V2_VERSION` 尚不存在而失败，不得出现 import 或语法错误。

### Task 2: 迁移 attestation 常量并删除离线模块

**Files:**
- Modify: `scheduler/daily_policy.py:72-81`
- Modify: `scheduler/capacity_attestation.py:18-39`
- Delete: `scheduler/capacity_gate.py`
- Delete: `scripts/evaluate_daily_capacity_gate.py`
- Test: `tests/test_capacity_gate.py`

- [ ] **Step 1: 在日度策略公开唯一 policy v2 身份**

将 `scheduler/daily_policy.py` 的版本集合改为：

```python
DAILY_POLICY_V2_VERSION = "daily-scheduler-policy-v2"
SUPPORTED_POLICY_VERSIONS = frozenset(
    {
        "daily-scheduler-policy-v1",
        DAILY_POLICY_V2_VERSION,
    }
)
```

不替换该文件其他 v1/v2 分支，不改变 policy JSON 或解析逻辑。

- [ ] **Step 2: 让 attestation 从日度策略派生常量**

删除 `scheduler/capacity_attestation.py` 对
`scheduler.capacity_gate` 的 import，改为：

```python
from scheduler.daily_policy import (
    DAILY_POLICY_V2_VERSION,
    EXPECTED_POLICY_V2_RELEASE_OFFSETS_BY_SCHEME,
    EXPECTED_V2_TARGET_COUNT,
)
```

在现有 attestation schema 常量之前定义：

```python
OBSERVATION_SCHEMA_VERSION = "daily-capacity-observations-v2"
SUPPORTED_POLICY_VERSION = DAILY_POLICY_V2_VERSION
EXPECTED_TARGET_COUNT = EXPECTED_V2_TARGET_COUNT
EXPECTED_V2_SCHEME_IDS = frozenset(
    EXPECTED_POLICY_V2_RELEASE_OFFSETS_BY_SCHEME
)
```

保留 `ATTESTED_EVIDENCE_SCHEMA_VERSION`、`CANDIDATE_SCHEMA_VERSION`、
`ARTIFACT_SET_SCHEMA_VERSION` 和所有验证函数原样。

- [ ] **Step 3: 运行常量来源测试并确认绿灯**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_capacity_gate.CapacityAttestationContractTests.test_daily_policy_v2_constants_are_authoritative
```

Expected: `Ran 1 test`，`OK`。

- [ ] **Step 4: 删除离线 gate 和 CLI**

使用 `apply_patch` 精确删除：

```text
*** Delete File: scheduler/capacity_gate.py
*** Delete File: scripts/evaluate_daily_capacity_gate.py
```

不得创建兼容 wrapper、redirect 或 deprecated alias。

### Task 3: 收敛并改名 attestation 测试

**Files:**
- Delete/Rename: `tests/test_capacity_gate.py`
- Create/Rename: `tests/test_capacity_attestation.py`
- Test: `tests/test_capacity_attestation.py`
- Test: `tests/test_architecture_boundaries.py`

- [ ] **Step 1: 删除 gate 和 CLI 专属测试块**

对 `tests/test_capacity_gate.py` 做一次机械收敛：

```bash
perl -0pi -e '
  s/\nclass CapacityGateTests\(unittest\.TestCase\):.*?(?=\nclass CapacityAttestationContractTests)/\n/s;
  s/\n    def test_offline_evaluation_never_claims_runtime_admission\(.*?(?=\n\nif __name__ == "__main__":)/\n/s;
' tests/test_capacity_gate.py
```

Expected: 文件只保留 helper、`CapacityAttestationContractTests` 和
`unittest.main()`；`CapacityGateTests`、两个 CLI 专属 attestation 测试和
`CapacityGateCliTests` 均消失。

- [ ] **Step 2: 将测试文件改为 attestation 职责名**

使用 `apply_patch` 的 move 操作：

```text
*** Update File: tests/test_capacity_gate.py
*** Move to: tests/test_capacity_attestation.py
@@
-import json
-import tempfile
 import unittest
-from pathlib import Path
```

同时删除三个无用 import；不得留下旧文件或同内容副本。

- [ ] **Step 3: 运行两个红灯测试的新路径并确认绿灯**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_architecture_boundaries.RepositoryArchitectureBoundaryTests.test_retired_daily_capacity_gate_and_cli_are_absent \
  tests.test_capacity_attestation.CapacityAttestationContractTests.test_daily_policy_v2_constants_are_authoritative
```

Expected: `Ran 2 tests`，`OK`。

- [ ] **Step 4: 运行 policy/attestation/candidate 定向回归**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_architecture_boundaries \
  tests.test_daily_policy \
  tests.test_daily_policy_v2 \
  tests.test_capacity_attestation \
  tests.test_capacity_candidate_runtime
```

Expected: 全部通过；不得出现已删除 gate/CLI 的 import error。

- [ ] **Step 5: 提交模块闭环清理**

提交前执行：

```bash
git status --short
git branch --show-current
git branch -vv
git diff --check
```

Expected: 当前分支为 `codex/audit-bugfixes-20260613`，变更只包含本计划列出的
两个生产文件修改、两个离线文件删除、测试文件收敛/改名和架构门禁；不包含
`outputs/` 或其他草稿。

然后执行：

```bash
git add \
  scheduler/daily_policy.py \
  scheduler/capacity_attestation.py \
  scheduler/capacity_gate.py \
  scripts/evaluate_daily_capacity_gate.py \
  tests/test_capacity_gate.py \
  tests/test_capacity_attestation.py \
  tests/test_architecture_boundaries.py
git commit -m "refactor: retire offline daily capacity gate"
```

Expected: 形成一个包含常量迁移、模块删除和测试收敛的原子提交。

### Task 4: 验证生产 direct authority 与全仓边界

**Files:**
- Verify only: repository-wide Python and Markdown files

- [ ] **Step 1: 运行 direct authority 与 scheduler 回归**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_daily_direct_cache_runtime \
  tests.test_daily_runtime \
  tests.test_ledger_018_started_at_preflight \
  tests.test_scheduler_direct_authority \
  tests.test_scheduler_main
```

Expected: 全部通过；生产 direct authority 的入口、返回结构和错误边界不变。

- [ ] **Step 2: 验证现行文档没有旧 CLI 入口**

Run:

```bash
rg -n \
  "evaluate_daily_capacity_gate|scheduler[./]capacity_gate" \
  docs/sop docs/architecture deploy/README.md README.md
```

Expected: 退出码为 1 且无输出。历史 specs、plans 和 records 不在该检查范围。

- [ ] **Step 3: 验证可执行源码没有旧模块引用**

Run:

```bash
rg -n \
  "evaluate_daily_capacity_gate|scheduler[./]capacity_gate|from scheduler\.capacity_gate" \
  scheduler scripts tests harness backend backtests shared \
  --glob '*.py' \
  --glob '!tests/test_architecture_boundaries.py'
```

Expected: 退出码为 1 且无输出。

- [ ] **Step 4: 运行文档门禁和完整测试**

冻结服务环境不安装 pytest。为完整 `unittest discover` 的 import 阶段在 `/tmp` 创建
一次性 pytest support 目录，同时保持解释器与 `sys.prefix` 为原始服务环境：

```bash
capacity_test_support=$(mktemp -d /tmp/bfl-capacity-gate-pytest.XXXXXX)
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pip install -q --target "$capacity_test_support" pytest==9.1.1
PYTHONPATH="$capacity_test_support" \
  /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m unittest discover -s tests
```

Expected: 全仓测试零失败、零错误；允许既有条件 skip。临时 support 目录位于 `/tmp`，
不写入仓库或冻结 Conda 环境。

- [ ] **Step 5: 运行编译、格式和越界门禁**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m compileall -q \
  backend backtests harness migrations scheduler scripts shared tests
git diff --check
git diff 81854eb^..HEAD -- \
  deploy \
  scheduler/main.py \
  scheduler/daily_direct_authority.py \
  scheduler/capacity_candidate_runtime.py \
  scheduler/daily_runtime.py \
  migrations \
  schemes \
  shared
```

Expected: 编译与 diff 门禁返回 0；列出的生产边界没有修改。

- [ ] **Step 6: 核对最终分支状态**

Run:

```bash
git status --short --branch
git log -3 --oneline --decorate
git rev-parse master origin/master
```

Expected: 工作树干净；开发分支新增设计、计划和实现提交；本地/远程 `master` 仍停在
`f11c28d`，开发分支未推送，未同步任何远程引用。
