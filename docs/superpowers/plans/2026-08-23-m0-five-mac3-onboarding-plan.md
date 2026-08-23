# M0 五方案 Mac3 完整入库实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 ECS 已完整入库的五个 `m0_weekly_avg_*_v1` Blackbox V2 方案扩展到 `mac3-production`，并在 Mac3 独立完成 Gate、回测、激活、灰度信号和 Dashboard 验收。

**Architecture:** 五份源码和 canonical `paused/draft` 不变，只把部署矩阵从 ECS-only 扩展为双目标。新 Git 精确提交构建一份确定性 archive，先在 ECS 晋级并复验其既有 exact active overlay，再用同一 archive 晋级 Mac3；Mac3 不复制 ECS 数据库或 receipt，而是逐方案重新建立本机状态。

**Tech Stack:** Git、Python 3.12、pytest、Harness CLI、SQLAlchemy/MySQL、immutable source release、systemd、launchd、FastAPI Dashboard。

---

### Task 1: 固化五方案双环境部署资格

**Files:**
- Modify: `deploy/scheme_deployment_matrix_v1.json`
- Modify: `tests/test_deployment_scope.py`

- [ ] **Step 1: 增加失败回归测试**

  新增 `M0_WEEKLY_AVERAGE_SCHEME_IDS`，逐个断言矩阵精确为：

  ```python
  ["mac3-production", "aliyun-gray"]
  ```

- [ ] **Step 2: 验证测试先失败**

  Run: `pytest tests/test_deployment_scope.py -q`

  Expected: 新增断言因五个条目仍为 `["aliyun-gray"]` 而失败。

- [ ] **Step 3: 最小修改部署矩阵**

  仅把以下五项改成双目标，不修改 config、算法、owner 或调度模板：

  ```text
  m0_weekly_avg_1y_v1
  m0_weekly_avg_3y_v1
  m0_weekly_avg_5y_v1
  m0_weekly_avg_7y_v1
  m0_weekly_avg_10y_v1
  ```

- [ ] **Step 4: 验证部署资格与 exact version 不变**

  Run: `pytest tests/test_deployment_scope.py tests/test_scheme_lifecycle_state.py -q`

  Expected: PASS；五份 config 仍为 `paused/draft`，exact version 仍分别为 `246cc5b71238`、`0726b172d237`、`6208c1fc671d`、`8c0eeae6ec6e`、`d56548e8f295`。

### Task 2: 独立规格与质量复审

**Files:**
- Review: `deploy/scheme_deployment_matrix_v1.json`
- Review: `tests/test_deployment_scope.py`

- [ ] **Step 1: 规格复审**

  检查只有部署 scope 扩大，未改变 canonical lifecycle、算法、owner、plist、systemd unit 或 exact version。

- [ ] **Step 2: 质量复审**

  检查测试以集合常量覆盖全部五项，无重复硬编码或依赖方案数量的脆弱断言。

- [ ] **Step 3: 提交代码**

  Run: `git status --short && git diff --check && git add deploy/scheme_deployment_matrix_v1.json tests/test_deployment_scope.py docs/superpowers/plans/2026-08-23-m0-five-mac3-onboarding-plan.md && git commit -m "deploy: admit five M0 weekly-average schemes on mac3"`

  Expected: 单一、可审计提交；`master` 不移动。

### Task 3: 构建并复验确定性 source release

**Files:**
- Read: `scripts/build_source_release.py`
- Output: `/Users/macstudio0/bond-factor-lab-runtime/source-releases/${release_commit}/`

- [ ] **Step 1: 在 clean HEAD 构建两次**

  Run:

  ```bash
  release_commit="$(git rev-parse HEAD)"
  python scripts/build_source_release.py --project-root . \
    --output-dir "/Users/macstudio0/bond-factor-lab-runtime/source-releases/${release_commit}"
  second_build_dir="$(mktemp -d)"
  python scripts/build_source_release.py --project-root . \
    --output-dir "${second_build_dir}"
  ```

  Expected: 两份 archive SHA-256 相同，manifest commit 等于精确 HEAD。

- [ ] **Step 2: 校验 archive 闭包**

  Run: 对 `${release_commit}.source.tar.gz` 执行 `tar -tzf`、`shasum -a 256` 和 manifest JSON 读回。

  Expected: 只含 Git source tree，不含 `.git`、secret、`outputs/`、reports 或 runtime artifact。

### Task 4: ECS 先行晋级与无回归读回

**Files:**
- Execute: `scripts/install_source_release.py`
- Read: `/var/lib/bond-factor-lab/state/lifecycle/m0_weekly_avg_*_v1.json`

- [ ] **Step 1: 预安装同一 archive**

  将 manifest/archive 复制到 ECS 候选目录，使用 `/opt/bond-factor-lab/current/scripts/install_source_release.py` 做非激活预安装。

- [ ] **Step 2: 激活前只读检查**

  精确断言五个 overlay 与 exact version 一致且为 `active/active`，数据库 version/Registry active，无 pending lifecycle journal，五个 timer 仍 enabled/active/waiting。

- [ ] **Step 3: expected-current CAS 激活并只重启 Backend**

  先以 `readlink -f /opt/bond-factor-lab/current` 取得 fresh expected-current，再使用 `--activate --expected-current "${ecs_current##*/}"`；执行 `systemctl restart bond-factor-lab-backend.service`，不替换 unit/timer。

- [ ] **Step 4: ECS 后验**

  验证 current/install record/archive SHA、health、strict discovery、五个 active Registry、既有回测和 gray_live 均未改变。

### Task 5: Mac3 候选预安装与五个 all Gate

**Files:**
- Execute: `scripts/install_source_release.py`
- Execute: `scripts/verify_blackbox_v2_environment.py`
- Execute: `python -m harness onboard`

- [ ] **Step 1: 使用同一 archive 非激活预安装**

  `--deploy-root /Users/macstudio0/bond-factor-lab-production --runtime-root /Users/macstudio0/bond-factor-lab-runtime`，不改变 current。

- [ ] **Step 2: 候选环境和 discovery**

  从候选 release 运行 `verify_blackbox_v2_environment.py`，期望 osx-arm64 fingerprint `720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd`；严格 discovery 应发现五方案但保持 `paused/draft`。

- [ ] **Step 3: 逐方案运行 all Gate**

  对五个 scheme 运行：

  ```bash
  for scheme_id in \
    m0_weekly_avg_1y_v1 m0_weekly_avg_3y_v1 m0_weekly_avg_5y_v1 \
    m0_weekly_avg_7y_v1 m0_weekly_avg_10y_v1; do
    python -m harness onboard "${scheme_id}" \
      --predict-date 2026-08-22 --stage all \
      --algo-env forecast_env_blackbox_v1 --timeout-sec 1800 || break
  done
  ```

  Expected: 每个 run 的 `static/input/unit/compare` 四 Gate 全部 passed，并持久化新的 Mac3 `harness_run_id`。

### Task 6: Mac3 影子登记与完整回测

**Files:**
- Execute: `python -m harness gate shadow-register`
- Execute: `python -m harness gate backtest`

- [ ] **Step 1: 逐方案 shadow register**

  ```bash
  python -m harness gate shadow-register \
    --scheme-id "${scheme_id}" --predict-date 2026-08-22 \
    --operator codex-mac3-m0-five-onboarding-20260823
  ```

  Expected: exact version 为 shadow、composite Registry 为 paused，identity 首次创建。

- [ ] **Step 2: 逐方案持久化完整回测**

  ```bash
  python -m harness gate backtest \
    --scheme-id "${scheme_id}" --predict-date 2026-08-28 \
    --persist --backtest-start-date 2025-01-01 \
    --algo-env forecast_env_blackbox_v1 --timeout-sec 1800 \
    --operator codex-mac3-m0-five-onboarding-20260823
  ```

  Expected: 每方案 84 条预测、20 个月指标，最大 target_date 早于 `2026-08-22`；五次事务各自成功。

### Task 7: Mac3 source CAS、激活与 gray_live

**Files:**
- Execute: `scripts/install_source_release.py`
- Execute: `python -m harness activate`
- Execute: `python -m harness signal-gap-fill`

- [ ] **Step 1: expected-current CAS 和 Backend 重启**

  先从 `readlink -f /Users/macstudio0/bond-factor-lab-production/current` 取得 fresh expected-current，激活已预安装 release；只执行 `launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.backend`，不替换 plist，不触发 Writer。

- [ ] **Step 2: 逐方案 activation**

  ```bash
  python -m harness activate --scheme-id "${scheme_id}" \
    --predict-date activate \
    --operator codex-mac3-m0-five-onboarding-20260823
  ```

  Expected: exact version、host lifecycle overlay、composite Registry 一致为 active。

- [ ] **Step 3: 逐方案 insert-only gray gap fill**

  ```bash
  python -m harness signal-gap-plan \
    --scheme-id "${scheme_id}" --predict-date 2026-08-22
  python -m harness signal-gap-fill \
    --scheme-id "${scheme_id}" --predict-date 2026-08-22 \
    --timeout-sec 1800
  ```

  Expected: 每方案只写一条 `gray_live`，日期为 predict `2026-08-22` / feature `2026-08-21` / target `2026-08-28`；方向与同口径本机算法输出一致。

### Task 8: Dashboard、数据库和控制面最终验收

**Files:**
- Execute: `python -m harness gate dashboard`
- Read: Mac3 MySQL、API、installed plist 和 launchctl state

- [ ] **Step 1: DashboardGate 与 API**

  对五方案运行 DashboardGate，验证五个 active composite 均出现在 `/api/factor-lab/dashboard`，task type 为 `weekly_average`。

- [ ] **Step 2: 数据库精确断言**

  逐方案断言 exact version/Registry active、四 Gate passed、84 条回测、20 个月指标、一条 insert-only gray_live；历史与 live target 零重叠。

- [ ] **Step 3: 前端和 launchd 验收**

  浏览器验证五个目标格子的候选行与详情，控制台零错误；launchd drift audit 仍通过，现有周频触发保持周六 11:30。

- [ ] **Step 4: 完整回归**

  Run: `pytest -q`

  Expected: 全部通过；只把未来首次自然周频触发保留为 Production Observed 证据，不把 timer waiting 宣称为已自然运行。
