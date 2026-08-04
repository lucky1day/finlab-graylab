# G3 All-Active Gap Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让现有 signal-gap planner 接受当前任意非空的 active Registry 集合，并继续按每个方案的 cadence 枚举缺口。

**Architecture:** 保留现有 `WHERE status='active'` 查询、日/周/月 context builder 和每个 target 的严格契约校验。仅把已过期的固定数量断言替换为“active 集合不能为空”的 fail-closed 断言；不修改版本/hash、CLI、DataBridge、fill gate、repository 或调度链路。

**Tech Stack:** Python 3.12、unittest、pytest、SQLAlchemy query fakes。

---

## File map

- Modify: `harness/signal_gap_plan.py:45-54,1307,3396-3417` — 删除固定 44/40/daily/weekly/monthly 数量常量，保留 `_validate_active_scope` 名称但只拒绝空集合。
- Create: `tests/test_signal_gap_plan.py` — 以假的 SQLAlchemy mapping result 覆盖非旧数量集合、空集合和既有 target 契约。
- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md` — 记录本地 G3 planner 修复和只读实际运行结果，明确不等于补数或生产恢复。
- Create: `docs/superpowers/plans/2026-08-04-g3-all-active-gap-planning.md` — 本执行计划。

### Task 1: 建立 planner 的 RED 回归测试

**Files:**
- Create: `tests/test_signal_gap_plan.py`

- [x] **Step 1: 写入最小 fake connection 与三个测试。**

实现使用仅覆盖两条 mapping query 的 fake connection，并生成与当前只读
快照相同形状的 53 target / 50 execution 集合：daily=32、weekly=13、
monthly=8。每个 base 都有匹配的 active version 与 discovery identity。
另有空 active Registry 和 T+1 horizon 漂移两个隔离回归；全程不连接真实
数据库。

- [x] **Step 2: 运行测试并确认 RED。**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_signal_gap_plan.py -q
```

Observed: 旧实现的 53/50 fixture 和空 Registry 均返回
`ACTIVE_REGISTRY_SCOPE_DRIFT`，任务契约测试通过。

### Task 2: 最小替换固定范围守卫

**Files:**
- Modify: `harness/signal_gap_plan.py:45-54,1307,3396-3417`
- Test: `tests/test_signal_gap_plan.py`

- [x] **Step 1: 删除旧数量常量。**

删除：

```python
EXPECTED_ACTIVE_TARGET_COUNT = 44
EXPECTED_ACTIVE_EXECUTION_COUNT = 40
EXPECTED_ACTIVE_FREQUENCY_COUNTS = {
    "daily": 29,
    "weekly": 7,
    "monthly": 8,
}
```

- [x] **Step 2: 将同名 guard 改为只拒绝空集合。**

```python
def _validate_active_scope(
    targets: Sequence[RegistryTarget],
) -> None:
    if not targets:
        raise SignalGapPlanError(
            "NO_ACTIVE_REGISTRY_TARGETS",
            "active Registry has no targets",
        )
```

保留 `_read_registry_versions()` 中的 `_validate_active_scope(targets)` 调用；不触碰 discovery/version/hash、task contract、input authority 或 action 构建。

- [x] **Step 3: 运行测试并确认 GREEN。**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_signal_gap_plan.py -q
```

Observed: `3 passed`。

### Task 3: 只读现场复核与状态记录

**Files:**
- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`
- Test: `tests/test_signal_gap_plan.py`

- [x] **Step 1: 运行相关回归。**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_signal_gap_plan.py tests/test_harness_persistence.py tests/test_onboarding_docs.py -q
```

Observed: `58 passed, 35 subtests passed`；不产生 DB 写入。

- [x] **Step 2: 运行既有 CLI 的只读现场复核。**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m harness signal-gap-plan --start 2026-08-03 --as-of 2026-08-03
```

Observed: 旧数量 blocker 已消失；当前输出为 `BLOCKED`，其 13 个 open gap
均为 `DATABRIDGE_CURRENT_REFRESH_REQUIRED`。未执行 publish、补数、activation、
`launchctl` 或服务重启。

- [x] **Step 3: 回写状态记录。**

在 G3 段落中记录：固定 active-count blocker 已删除、当前 plan 的实际只读结果和仍在的 DataBridge/版本/生产授权条件；不得宣称已补齐或已恢复生产。

### Task 4: 审查、提交与交接

**Files:**
- Modify: `harness/signal_gap_plan.py`
- Create: `tests/test_signal_gap_plan.py`
- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`
- Create: `docs/superpowers/plans/2026-08-04-g3-all-active-gap-planning.md`

- [x] **Step 1: 独立审查当前 diff。**

核对没有引入 hash/version lifecycle、scope/manifest、writer、scheduler、ledger、installed plist 或 launchctl 改动；确认空 Registry 仍 fail-closed。

- [x] **Step 2: 最终验证。**

Run:

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest tests/test_signal_gap_plan.py tests/test_harness_persistence.py tests/test_onboarding_docs.py -q
```

Observed: `git diff --check` 无输出；`58 passed, 35 subtests passed`。

- [ ] **Step 3: 仅提交本阶段归属文件。**

Run:

```bash
git status --short
git branch --show-current
git add harness/signal_gap_plan.py tests/test_signal_gap_plan.py \
  docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md \
  docs/superpowers/plans/2026-08-04-g3-all-active-gap-planning.md
git commit -m "fix(harness): plan all active signal gaps dynamically"
```

Expected: 只提交 G3 文件；不加入 `KNOWN_ISSUES_HANDOFF_20260802.md`、`scripts/_diag_weekly_10y_0801.py` 或 `scripts/_diag_weekly_10y_bench_drift.py`；不 merge/push。
