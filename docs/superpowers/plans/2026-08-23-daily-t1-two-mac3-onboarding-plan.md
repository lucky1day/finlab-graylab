# 5Y / 10Y 日频 T+1 两方案 Mac3 入库实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 从 ECS-only 扩展为双目标资格，并在 Mac3 独立完成 Gate、Registry、回测、activation、gray-live 和 Dashboard 入库。

**Architecture:** 算法交付和 canonical `paused/draft` 保持不变，只扩展部署矩阵并增加精确回归。最终 clean HEAD 构建一份确定性 archive，先以 CAS 晋级 ECS 并复验既有 active 状态，再用同一 archive 晋级 Mac3；两端数据库、生命周期、运行记录和调度控制面完全独立。

**Tech Stack:** Git、Python 3.12、pytest、Blackbox V2 Harness、MySQL 8、immutable source release、systemd、launchd、FastAPI Dashboard

---

### Task 1: 用测试保护两个方案的双目标部署资格

**Files:**

- Modify: `tests/test_deployment_scope.py`
- Modify: `deploy/scheme_deployment_matrix_v1.json`
- Read: `schemes/five_y_factor_rule_online_v1/config.yaml`
- Read: `schemes/ten_y_factor_level_ensemble_v1/config.yaml`

- [ ] **Step 1: 写精确失败测试**

在 `tests/test_deployment_scope.py` 的 M0 常量后增加：

```python
DAILY_T1_TRIAL_SCHEME_IDS = (
    "five_y_factor_rule_online_v1",
    "ten_y_factor_level_ensemble_v1",
)
```

并在双目标测试后增加：

```python
def test_daily_t1_trial_schemes_are_deployed_to_both_targets() -> None:
    matrix = _matrix_schemes()
    for scheme_id in DAILY_T1_TRIAL_SCHEME_IDS:
        assert matrix[scheme_id] == [MAC3_TARGET, ALIYUN_TARGET], scheme_id
```

- [ ] **Step 2: 运行 RED**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest \
  -q tests/test_deployment_scope.py::test_daily_t1_trial_schemes_are_deployed_to_both_targets
```

Expected: FAIL；实际值仍为 `["aliyun-gray"]`。

- [ ] **Step 3: 最小修改部署矩阵**

把 `deploy/scheme_deployment_matrix_v1.json` 中两项精确改为：

```json
"five_y_factor_rule_online_v1": ["mac3-production", "aliyun-gray"],
"ten_y_factor_level_ensemble_v1": ["mac3-production", "aliyun-gray"]
```

不得修改两份 config、delivery、owner、schedule 或其它方案。

- [ ] **Step 4: 运行 GREEN 与范围回归**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_deployment_scope.py \
  tests/test_scheme_lifecycle_state.py \
  tests/test_blackbox_v2_discovery.py
git diff --check
```

Expected: 全部 passed；两个 exact version 仍为 `55f0e753b18e`、`997af2e57ad9`，canonical 仍为
`paused/draft`。

- [ ] **Step 5: 提交代码变更**

```bash
git status --short
git add deploy/scheme_deployment_matrix_v1.json tests/test_deployment_scope.py
git commit -m "deploy: admit two daily T1 schemes on mac3"
```

Expected: commit 只包含上述两个文件。

### Task 2: 复审、全量回归和远程锁定

**Files:**

- Review: Task 1 commit
- Test: `tests/`

- [ ] **Step 1: 规格复审**

独立复审 Task 1，确认两个方案均精确扩展为 `[mac3-production, aliyun-gray]`，且算法、canonical
lifecycle、owner、cron 和其它矩阵项不变。

- [ ] **Step 2: 质量复审**

规格 PASS 后再做质量复审，检查测试不依赖方案总数、命名与现有 M0 模式一致、JSON 顺序稳定。

- [ ] **Step 3: 完整测试**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q
```

Expected: 0 failures。

- [ ] **Step 4: 推送并锁定远程**

```bash
git status --short
git push origin codex/develop
git fetch origin codex/develop
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/codex/develop)"
```

Expected: clean；本地与远程 SHA 一致。

### Task 3: 构建确定性 release 并先晋级 ECS

**Files:**

- Execute: `scripts/build_source_release.py`
- Execute: `scripts/install_source_release.py`
- Read: ECS `/opt/bond-factor-lab/current`

- [ ] **Step 1: 双重构建**

```bash
release_sha=$(git rev-parse HEAD)
release_dir="/Users/macstudio0/bond-factor-lab-runtime/source-releases/$release_sha"
verify_dir=$(mktemp -d /tmp/bfl-two-daily-release.XXXXXX)
mkdir -p "$release_dir"
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -I -B \
  scripts/build_source_release.py --project-root . --output-dir "$release_dir"
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -I -B \
  scripts/build_source_release.py --project-root . --output-dir "$verify_dir"
cmp "$release_dir/$release_sha.source.tar.gz" "$verify_dir/$release_sha.source.tar.gz"
shasum -a 256 "$release_dir/$release_sha.source.tar.gz" "$verify_dir/$release_sha.source.tar.gz"
```

Expected: 两个 SHA-256 一致且 `cmp` 为 0。

- [ ] **Step 2: ECS 预安装和候选核验**

把 archive、manifest、installer 和 builder 复制到
`/var/lib/bond-factor-lab/source-releases/$release_sha/`；用 ECS service Python 执行不带 `--activate`
的安装。候选环境验证、严格 discovery 和 Dashboard 构建必须通过；两个方案必须保持 exact
`active/active`，pending journal 为 0。

- [ ] **Step 3: ECS CAS 与 Backend 重启**

以 `expected-current=990fd4b96fcd7cfb9fad533179c645bac723b817` 激活候选，只执行：

```bash
systemctl restart bond-factor-lab-backend.service
```

不得替换、reload 或 kickstart 任一 timer/writer。

- [ ] **Step 4: ECS 读回**

Expected: `current` 为最终 release、`previous` 为 `990fd4b...`；Backend 与 Dashboard HTTP 200；两个
exact version/Registry/overlay active，278 条回测和原 gray-live 不变；五个 timer 均 enabled/active，
daily 下次触发仍为 2026-08-24 07:03 Asia/Shanghai。

### Task 4: Mac3 候选 Gate、shadow identity 和历史回测

**Files:**

- Execute: same immutable archive
- Execute: `python -m harness onboard`
- Execute: `python -m harness gate shadow-register`
- Execute: `python -m harness gate backtest --persist`

- [ ] **Step 1: Mac3 预安装与候选环境**

不带 `--activate` 安装同一 archive 到 `/Users/macstudio0/bond-factor-lab-production/releases/`；通过
候选 osx-arm64 环境校验、DataBridge current/ready、strict discovery。两个候选必须是
`paused/draft`，Mac3 数据库仍无两个身份。

- [ ] **Step 2: 逐方案运行完整自动 Gate**

在候选 release 的可信运行环境中依次执行：

```bash
python -m harness onboard five_y_factor_rule_online_v1 --predict-date 2026-08-21 --stage all
python -m harness onboard ten_y_factor_level_ensemble_v1 --predict-date 2026-08-21 --stage all
```

Expected: 每方案 `static/input/unit/compare` passed 并持久化唯一 latest passed `all` run。

- [ ] **Step 3: 建立 shadow identity**

```bash
python -m harness gate shadow-register \
  --scheme-id five_y_factor_rule_online_v1 --predict-date 2026-08-21 \
  --operator codex-mac3-daily-t1-two-20260823
python -m harness gate shadow-register \
  --scheme-id ten_y_factor_level_ensemble_v1 --predict-date 2026-08-21 \
  --operator codex-mac3-daily-t1-two-20260823
```

Expected: exact version shadow、composite Registry paused、host overlay `paused/shadow`、pending 为 0。

- [ ] **Step 4: 持久化完整回测**

```bash
python -m harness gate backtest --persist \
  --scheme-id five_y_factor_rule_online_v1 --predict-date 2026-08-21 \
  --backtest-start-date 2025-07-01 --timeout-sec 3600 \
  --operator codex-mac3-daily-t1-two-20260823
python -m harness gate backtest --persist \
  --scheme-id ten_y_factor_level_ensemble_v1 --predict-date 2026-08-21 \
  --backtest-start-date 2025-07-01 --timeout-sec 3600 \
  --operator codex-mac3-daily-t1-two-20260823
```

Expected: 每方案 278 条预测、14 个月度指标，target `2025-07-02..2026-08-20`；不进入
`2026-08-21` gray-live business key。

### Task 5: Mac3 CAS、activation 和 gray-live 入库

**Files:**

- Execute: `scripts/install_source_release.py --activate`
- Execute: `python -m harness activate`
- Execute: `python -m harness signal-gap-*`

- [ ] **Step 1: writer idle 与 expected-current 复核**

确认 daily/weekly/monthly/Actuals/DataBridge one-shot 均不在运行，`current` 仍为
`59afc857c37eebb956adb200d9b1a05ed6a123f0`。

- [ ] **Step 2: Mac3 source CAS**

以该 SHA 作为 `--expected-current` 激活已预安装 release，只执行：

```bash
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.backend
```

Expected: Backend 恢复 healthy；不替换 plist，不触发 Writer。

- [ ] **Step 3: 逐方案 activation**

```bash
python -m harness activate --scheme-id five_y_factor_rule_online_v1 \
  --predict-date 2026-08-21 --operator codex-mac3-daily-t1-two-20260823
python -m harness activate --scheme-id ten_y_factor_level_ensemble_v1 \
  --predict-date 2026-08-21 --operator codex-mac3-daily-t1-two-20260823
```

Expected: exact version、host overlay 和 Registry 原子统一为 active。

- [ ] **Step 4: 单日 insert-only gray gap**

对每个方案先运行 `signal-gap-plan --predict-date 2026-08-21`，必须是唯一 actionable key；再执行：

```bash
python -m harness signal-gap-fill --scheme-id five_y_factor_rule_online_v1 \
  --predict-date 2026-08-21 --timeout-sec 3600
python -m harness signal-gap-fill --scheme-id ten_y_factor_level_ensemble_v1 \
  --predict-date 2026-08-21 --timeout-sec 3600
```

Expected: 每方案写 1 条 `gray_live`，日期为 `2026-08-21 / 2026-08-20 / 2026-08-21`；本机方向
分别为 `-1`、`0`。第二次 plan 必须为 `present=1/actionable=0`。

### Task 6: Dashboard、数据库、浏览器和控制面最终验收

**Files:**

- Execute: `python -m harness gate dashboard`
- Read: Mac3 MySQL/API/launchd
- Read: ECS MySQL/API/systemd

- [ ] **Step 1: 两个 DashboardGate**

对两个方案分别执行 dashboard Gate，API endpoint 使用 `http://127.0.0.1:8100`，prediction phase 为
`gray_live`。Expected: HTTP 200、composite identity matched、backtest identity matched。

- [ ] **Step 2: Mac3 数据库与 API 精确断言**

逐方案断言 exact version/Registry/overlay active、四 Gate passed、278 backtest、14 months、1 gray-live、
live run `records_expected/returned/written=1/1/1`；Dashboard API 显示 `T+1`、278 backtest、1 live。

- [ ] **Step 3: 真实浏览器与 launchd**

浏览器分别打开 5Y/10Y 的 T+1 候选排行并选择新方案详情；确认月度矩阵可见且控制台无
error/warning。运行 `scripts/audit_launchd_config_drift.py`，Expected: `ok=true`；daily plist 仍为工作日
07:03，当前不运行。

- [ ] **Step 4: 双端最终读回**

确认 ECS 和 Mac3 `current` 指向同一 release/archive SHA，ECS 五个 timer 不变，Mac3 七个 launchd
label 保持 installed/loaded；两端数据库各自独立包含两个 active identity，无 pending journal。

### Task 7: CURRENT 文档收口与工作计划清理

**Files:**

- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/TODO.md`
- Delete: `docs/superpowers/specs/2026-08-23-daily-t1-two-mac3-onboarding-design.md`
- Delete: `docs/superpowers/plans/2026-08-23-daily-t1-two-mac3-onboarding-plan.md`

- [ ] **Step 1: 更新当前事实**

把最终 release SHA/archive SHA、Mac3 Harness IDs、backtest run IDs、gray-live run IDs、Dashboard/API
与 launchd 验收写入 CURRENT；TODO 保留两端下一次自然 daily `scheduled_live` 的 Production Observed
观察，不把 timer waiting 写成已经观察。

- [ ] **Step 2: 删除闭环工作文档**

使用 `apply_patch` 删除本设计与本计划；全仓库检查无残留引用。

- [ ] **Step 3: 文档回归、提交与推送**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_onboarding_docs.py
git diff --check
git status --short
git commit -m "docs: record two daily T1 Mac3 onboardings"
git push origin codex/develop
```

Expected: 当前文档门禁通过；工作树 clean；本地与远程 SHA 一致。该 docs-only 收口提交不触发新的
source release 或服务重启。
