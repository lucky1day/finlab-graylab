# ECS Natural systemd Manual Environment Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从四个 ECS 自然 systemd service 仓库模板移除一次性 `manual-run.env`，并用长期合同测试和当前文档固定第一阶段边界。

**Architecture:** 不新增抽象、不改 runner。测试把“所有自然 service 均不得加载共享手工环境文件”设为模板合同；实现只删除四行 EnvironmentFile。文档明确仓库期望配置已治理、ECS installed unit 尚未替换，避免把候选状态误称为现场生效。

**Tech Stack:** systemd unit、Python `unittest`/pytest、Markdown。

---

### Task 1: 用合同测试移除四个模板的手工环境文件

**Files:**
- Modify: `tests/test_systemd_control_plane.py`
- Modify: `deploy/systemd/bond-factor-lab-data-bridge.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-daily.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-weekly.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-monthly.service`

- [ ] **Step 1: 写失败合同**

在 `test_systemd_templates_are_disabled_first_one_shots()` 的 service 通用断言中加入：

```python
self.assertNotIn(
    "EnvironmentFile=-/run/bond-factor-lab/manual-run.env",
    content,
    name,
)
```

删除现有允许该文件存在并只检查顺序的 `manual_environment` 条件块。不要改变其它环境文件、timer、
时限或 ExecStart 断言。

- [ ] **Step 2: 运行 RED**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest \
  tests/test_systemd_control_plane.py::SystemdControlPlaneTests::test_systemd_templates_are_disabled_first_one_shots \
  -q
```

Expected: FAIL，并精确指出四个旧模板中的 `manual-run.env` 引用。

- [ ] **Step 3: 作出最小模板修改**

从以下四个文件各删除唯一一行：

```text
EnvironmentFile=-/run/bond-factor-lab/manual-run.env
```

不要改变其它字节或重新格式化 unit。

- [ ] **Step 4: 运行 GREEN 与模板回归**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_systemd_control_plane.py -q
```

Expected: PASS。

Run:

```bash
! rg -n "manual-run\.env" deploy/systemd
git diff --check
```

Expected: `rg` 无结果，diff check 通过。

- [ ] **Step 5: 提交代码与长期测试**

```bash
git add \
  tests/test_systemd_control_plane.py \
  deploy/systemd/bond-factor-lab-data-bridge.service \
  deploy/systemd/bond-factor-lab-prediction-daily.service \
  deploy/systemd/bond-factor-lab-prediction-weekly.service \
  deploy/systemd/bond-factor-lab-prediction-monthly.service
git commit -m "fix(systemd): remove sticky manual environment"
```

Expected: 只提交上述五个文件。

### Task 2: 更新长期文档并清理完成计划

**Files:**
- Modify: `deploy/README.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/TODO.md`
- Modify: `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md`
- Delete: `docs/superpowers/specs/2026-08-20-natural-systemd-manual-env-removal-design.md`
- Delete: `docs/superpowers/plans/2026-08-20-natural-systemd-manual-env-removal-plan.md`

- [ ] **Step 1: 更新事实边界**

四份长期文档必须一致表达：

- 仓库四个自然 ECS service 模板不再加载共享 `manual-run.env`；
- 手工历史日期继续只走 `signal-gap-fill`；
- 第一阶段没有修改 runner 的 CLI 日期入口；
- ECS installed unit 尚未替换，仍是下一阶段独立生产操作；
- 本阶段未执行 daemon-reload、启动、停止、触发任务或数据库写入。

不要记录一次性测试日志、临时文件、逐命令 hash 或新的兼容层。

- [ ] **Step 2: 删除完成的设计和实施计划**

用 `apply_patch` 删除本设计与本计划；确认 `docs/superpowers/plans` 和
`docs/superpowers/specs` 不含其它文件后，只对这些精确空目录逐层执行 `rmdir`。长期规则已经进入
CURRENT 文档，历史由 Git 保留。

- [ ] **Step 3: 运行文档和范围验证**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_onboarding_docs.py -q
cmp AGENTS.md CLAUDE.md
git diff --check
! rg -n "manual-run\.env" deploy/systemd
test ! -e docs/superpowers
```

Expected: 文档测试通过、根规范一致、无模板残留、完成计划目录不存在。

- [ ] **Step 4: 提交文档治理**

```bash
git add \
  deploy/README.md \
  docs/CURRENT_STATUS.md \
  docs/TODO.md \
  docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md \
  docs/superpowers/specs/2026-08-20-natural-systemd-manual-env-removal-design.md \
  docs/superpowers/plans/2026-08-20-natural-systemd-manual-env-removal-plan.md
git commit -m "docs: record natural systemd template cleanup"
```

Expected: 只提交四份长期文档和两份已完成计划的删除。

### Task 3: 最终验证与现场边界读回

**Files:**
- No repository changes.

- [ ] **Step 1: 全量本地验证**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -m compileall -q scheduler scripts tests
```

Expected: 全量测试及 compileall 通过。

- [ ] **Step 2: ECS 只读复核**

严格连接当前灰度 authority，只读确认：

- `/run/bond-factor-lab/manual-run.env` 仍不存在；
- installed 四个 service 仍含旧 EnvironmentFile，证明本阶段没有静默替换现场；
- prediction timers 仍 enabled/active/waiting，one-shot services idle。

不得执行 daemon-reload、unit 替换、服务启停、任务触发或数据库访问。

- [ ] **Step 3: 最终报告**

报告两个代码提交、RED/GREEN、全量测试、ECS 只读结果，并明确下一阶段才处理 runner 与 installed unit。
