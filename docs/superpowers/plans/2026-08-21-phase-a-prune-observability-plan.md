# Phase-A Cache Prune Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Phase-A publication 后的 best-effort prune 失败进入既有 audit 与 warning，同时保持发布成功。

**Architecture:** `_prune_generations` 返回 deferred generation IDs，publisher 把结果写入 `prune_deferred` 并在非空时记录一条 warning。删除和 fsync 仍然是同步 best-effort，不引入任何新控制面。

**Tech Stack:** Python logging、pytest、现有 Phase-A private-cache 测试。

---

### Task 1: 锁定 prune 可观察性合同并实现最小修复

**Files:**
- Modify: `shared/liwei_0616_phase_a_cache.py`
- Modify: `tests/test_liwei_0616_private_cache.py`

- [ ] **Step 1: 写 RED 测试**

更新现有 postcommit prune failure、单 generation removal failure、successful prune 测试，并新增
generation-root fsync failure 测试，要求：

```python
assert audit["prune_deferred"] == [expected_generation_id]
assert len(prune_warning_records) == 1
```

正常路径要求：

```python
assert audit["prune_deferred"] == []
assert prune_warning_records == []
```

使用 `caplog` 捕获 `shared.liwei_0616_phase_a_cache` logger 的 WARNING，不匹配其它日志。

- [ ] **Step 2: 运行聚焦 RED**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_liwei_0616_private_cache.py \
  -k "postcommit_prune or successful_publication_prunes" -q
```

Expected: 新 audit/warning 断言失败，现有 publication 保持成功。

- [ ] **Step 3: 实现最小生产代码**

- 导入 `logging` 并创建模块 logger。
- `_generation_audit` 增加 `"prune_deferred": []`。
- `_prune_generations` 返回 deferred ID 元组；单项失败继续；无计划时直接返回空元组；fsync 失败时
  返回全部计划 ID。
- publication 后调用捕获普通 `Exception`，未预期异常时使用全部计划 ID；将列表写入 audit；非空时
  输出一条 warning。
- 不捕获 `BaseException`，不改变 publication commit point。

- [ ] **Step 4: 运行 GREEN**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_liwei_0616_private_cache.py \
  tests/test_liwei_0616_revision_suffix_contract.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

只提交生产模块与 private-cache 测试：

```bash
git add shared/liwei_0616_phase_a_cache.py tests/test_liwei_0616_private_cache.py
git commit -m "fix(cache): report deferred generation pruning"
```

### Task 2: 更新长期文档并清理完成计划

**Files:**
- Modify: `docs/architecture/CODE_ARCHITECTURE.md`
- Modify: `docs/CURRENT_STATUS.md`
- Delete: `docs/superpowers/specs/2026-08-21-phase-a-prune-observability-design.md`
- Delete: `docs/superpowers/plans/2026-08-21-phase-a-prune-observability-plan.md`

- [ ] **Step 1: 更新长期规则**

架构文档只记录：publication 后 prune 仍为 best-effort；失败不回滚 current 或预测；deferred IDs 进入
cache audit 并输出 warning；不增加后台控制面。CURRENT 只记录该能力已进入候选代码，不误称已部署。

- [ ] **Step 2: 删除完成计划并提交**

用 `apply_patch` 删除 spec/plan，确认 `docs/superpowers` 无其它文件后精确 `rmdir` 空目录。运行文档测试、
链接、diff-check 后提交：

```bash
git add docs/architecture/CODE_ARCHITECTURE.md docs/CURRENT_STATUS.md \
  docs/superpowers/specs/2026-08-21-phase-a-prune-observability-design.md \
  docs/superpowers/plans/2026-08-21-phase-a-prune-observability-plan.md
git commit -m "docs: record Phase-A prune observability"
```

### Task 3: 最终验证

- [ ] 运行 private-cache、suffix、架构/文档聚焦测试。
- [ ] 运行全量 pytest 与 compileall。
- [ ] 独立规格、质量和全局审查。
- [ ] 确认工作树干净；不部署、不触碰 Mac3/ECS/数据库。
