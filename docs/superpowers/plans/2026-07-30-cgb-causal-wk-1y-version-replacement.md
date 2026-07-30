# cgb_causal_wk_1y Version Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用上游 2026-07-30 更新脚本替换已激活的 `cgb_causal_wk_1y`，
重建历史与灰度数据，清除旧版本业务数据并更新前端。

**Architecture:** 保持 scheme/Registry 身份不变；增加一个 repository 单事务版本
切换原语和一个精确目标的运维包装器。新历史先持久化、旧 run 后删除；灰度按目标
原子替换。旧 version row 退役而不物理删除。

**Tech Stack:** Python 3.12、SQLAlchemy、MySQL 8.0、Blackbox V2 Harness、
FastAPI、pytest。

## Global Constraints

- 保留现有未提交的
  `docs/blackbox_v2/records/CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md`
  用户改动，不混入无关提交。
- 只替换 `.py`；`.json`、config、Registry ID 和 scheduler 配置不变。
- 生产周历只来自 `api-wind-date-v1`。
- 不重新验证算法内部逻辑，不改模型以贴结果。
- 所有删除先 dry-run，且以 exact scheme/version/run/target 为围栏。
- 周度 scheduler 继续保持未加载。

### Task 1: 冻结新交付身份

**Files:**
- Modify: `tests/test_cgb_causal_wk_1y_onboarding.py`
- Modify: `schemes/cgb_causal_wk_1y/delivery/cgb_causal_wk_1y.py`

- [ ] 在测试中将预期脚本 SHA 和 scheme version 更新为新交付身份。
- [ ] 先运行测试确认旧脚本导致红灯。
- [ ] 仅用上游新 `.py` 替换仓库交付脚本。
- [ ] 运行 identity、discovery、Contract 测试并确认绿灯。

### Task 2: 增加已激活版本替换事务

**Files:**
- Modify: `scheduler/repository.py`
- Create: `scripts/replace_active_blackbox_version.py`
- Create: `tests/test_replace_active_blackbox_version.py`

- [ ] 编写红灯测试：正常切换、旧版本非唯一 active、新版本身份漂移、
  Registry 非 active、dry-run 零写入。
- [ ] 在 `scheduler.repository` 实现单事务 exact-version 切换：
  new active、old retired、Registry active、读回唯一 active。
- [ ] 运维脚本必须显式传入 scheme、old version、new version、harness run、
  `--apply`；默认 dry-run。
- [ ] 运行目标测试和 repository/lifecycle 回归。

### Task 3: active recertification 与逐周对比

**Files:**
- Modify: onboarding/production evidence records as needed.

- [ ] 在平台同一代 DataBridge 和 canonical calendar 下运行新旧历史 72 周、
  灰度 9 周对比。
- [ ] 运行 `harness onboard ... --stage all --check-only`，确认 7/7 PASS。
- [ ] 记录 exact generation、combined snapshot、calendar hash、harness run 和
  新 scheme version。
- [ ] 任何方向、日期或 cutoff 不一致时停止生产切换。

### Task 4: 生产版本切换与新历史回测

**Files:**
- Modify: production evidence records.

- [ ] 对版本替换脚本执行 dry-run，核对旧 active/new candidate/Registry。
- [ ] 用 exact all-stage run 和 operator 身份执行 `--apply`。
- [ ] 持久化 `2025-01-01` 起、`target_date < 2026-06-01` 的新历史回测。
- [ ] 核对新 run 为 canonical latest、72 条预测、月度指标非空。
- [ ] 调用 `scripts/delete_backtest_runs.py` dry-run 后精确删除旧 run `192`。

### Task 5: 原子替换灰度数据

**Files:**
- Modify: `scheduler/repository.py`
- Create: `scripts/replace_blackbox_gray_history.py`
- Create: `tests/test_replace_blackbox_gray_history.py`

- [ ] 红灯测试覆盖 exact old version、9 个目标、唯一键、事务回滚和 dry-run。
- [ ] 复用 Blackbox Request、当前 input bundle 和执行器在事务外计算 9 条结果。
- [ ] repository 在每个目标的事务中替换 prediction/run/log，并校验写后版本。
- [ ] 执行 dry-run 后 `--apply`，确认 9 条全部归属新 version。
- [ ] 核对旧 version 在 prediction/run/log 中计数为零。

### Task 6: 前端、文档和 Git 收尾

**Files:**
- Modify: `docs/blackbox_v2/records/CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.md`
- Modify: `docs/blackbox_v2/records/CGB_CAUSAL_WK_1Y_PRODUCTION_ACCEPTANCE_20260730.evidence.json`
- Modify: scheduler admission exact version files/tests if version-pinned.

- [ ] 核对 `/api/schemes`、`/api/metrics/{registry_id}`、
  `/api/backtests/factor-lab` 和前端验证脚本。
- [ ] 核对 scheduler 未加载该方案。
- [ ] 运行目标测试、相关回归、compileall、`git diff --check`。
- [ ] 分批提交，合并/同步到本地 `master`，推送开发分支和 `master`；
  不创建 PR。

