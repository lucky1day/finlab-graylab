# Actuals Launchd Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留成熟的一次性 Actuals 执行链，并从常驻 APScheduler 删除重复的 Actuals 时钟职责，使独立 launchd plist 成为唯一生产调度权威。

**Architecture:** `com.bond-factor-lab.actuals` 继续在 `08:30/19:00/23:45` 调用 `scheduler.main --run-once actuals`；`run_actuals_job()` 和三个 updater 原样保留。只删除 `build_scheduler()` 内第二套 Actuals cron/executor，并同步收敛测试和当前架构文档，历史记录不改写。

**Tech Stack:** Python 3.12、APScheduler、launchd plist、`unittest`、Markdown、Git

---

### Task 1: 用失败测试锁定 launchd 单一调度权

**Files:**
- Modify: `tests/test_scheduler_main.py:2809-2830`
- Test: `tests/test_scheduler_main.py`

- [ ] **Step 1: 增加 APScheduler 不得注册 Actuals 的合同测试**

在现有 `test_actuals_refresh_registers_morning_evening_and_late_jobs` 之前加入：

```python
def test_build_scheduler_never_registers_actuals_jobs(self) -> None:
    from scheduler import main as scheduler_main

    for coordinator_mode in ("legacy", "ledger"):
        with self.subTest(coordinator_mode=coordinator_mode):
            engine = SimpleNamespace(dispose=Mock())
            with (
                patch.dict(
                    os.environ,
                    {"BOND_SCHEDULER_STARTUP_CATCHUP": "false"},
                ),
                patch.object(
                    scheduler_main,
                    "_daily_coordinator_mode",
                    return_value=coordinator_mode,
                ),
                patch.object(
                    scheduler_main,
                    "discover_schemes",
                    return_value=[],
                ),
                patch.object(
                    scheduler_main,
                    "_preflight_source_runtime_database",
                    return_value=None,
                ),
                patch.object(
                    scheduler_main,
                    "_sync_registry",
                    return_value=None,
                ),
                patch.object(
                    scheduler_main,
                    "create_engine_from_env",
                    return_value=engine,
                ),
                patch.object(
                    scheduler_main,
                    "build_daily_direct_cache_authorities",
                    return_value={},
                ),
            ):
                scheduler = scheduler_main.build_scheduler()

            try:
                actuals_job_ids = sorted(
                    job.id
                    for job in scheduler.get_jobs()
                    if job.id.startswith("actuals:")
                )
            finally:
                if scheduler.running:
                    scheduler.shutdown(wait=False)

            self.assertEqual([], actuals_job_ids)
```

- [ ] **Step 2: 运行新测试并确认预期红灯**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_scheduler_main.SchedulerMainTests.test_build_scheduler_never_registers_actuals_jobs
```

Expected: 两个 subtest 均为 `FAIL`；实际值精确包含
`actuals:0830`、`actuals:1900`、`actuals:2345`，不得出现 import、配置或语法错误。

### Task 2: 收敛旧 scheduler 测试并补齐一次性 CLI 合同

**Files:**
- Modify: `tests/test_scheduler_main.py:580-584`
- Modify: `tests/test_scheduler_main.py:1062-1210`
- Modify: `tests/test_scheduler_main.py:2809-2935`
- Test: `tests/test_scheduler_main.py`
- Test: `tests/test_actuals_launchd.py`

- [ ] **Step 1: 删除 ledger scheduler 对 Actuals executor 的旧正向断言**

从 ledger scheduler job 测试中删除：

```python
for job_id in ("actuals:0830", "actuals:1900", "actuals:2345"):
    self.assertEqual(jobs[job_id].executor, "actuals")
```

其他 daily coordinator、heartbeat、watchdog、recovery 和非日频 prediction 断言原样
保留。

- [ ] **Step 2: 收敛 admission 失败测试的职责名称和 job 集合**

把两个测试名改为：

```python
def test_invalid_blackbox_policy_keeps_native_jobs(self) -> None:
```

```python
def test_non_utf8_blackbox_policy_keeps_native_jobs(self) -> None:
```

并从两项测试中删除以下旧断言：

```python
self.assertTrue(
    {
        "actuals:0830",
        "actuals:1900",
        "actuals:2345",
    }.issubset(job_ids)
)
```

Native prediction 保留、Blackbox 拒绝和 registry sync 断言全部保留。

- [ ] **Step 3: 删除与新合同矛盾的旧 Actuals 注册测试**

完整删除从
`def test_actuals_refresh_registers_morning_evening_and_late_jobs(self) -> None:`
开始、到下一个
`def test_wavg_gapflip_v5_gray_identities_never_mount_automatic_jobs(`
之前的整个 method block。

保留 Task 1 新增的 `test_build_scheduler_never_registers_actuals_jobs`。

- [ ] **Step 4: 增加一次性 Actuals CLI 分派 characterization test**

在两个 `run_actuals_job()` 业务测试之前加入：

```python
def test_run_once_actuals_dispatches_existing_one_shot(self) -> None:
    from scheduler import main as scheduler_main

    with (
        patch.object(
            scheduler_main,
            "_daily_coordinator_mode",
            return_value="legacy",
        ),
        patch.object(
            scheduler_main,
            "run_actuals_job",
        ) as run_actuals_job,
    ):
        exit_code = scheduler_main.main(
            [
                "--run-once",
                "actuals",
                "--date",
                "2026-08-01",
                "--force",
            ]
        )

    self.assertEqual(exit_code, 0)
    run_actuals_job.assert_called_once_with(
        run_date="2026-08-01",
        force=True,
    )
```

- [ ] **Step 5: 验证红灯仍只来自待删除生产注册，CLI 合同已通过**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_scheduler_main.SchedulerMainTests.test_build_scheduler_never_registers_actuals_jobs
```

Expected: 仍是两个精确 `FAIL`，原因仍为三个 `actuals:*` job 存在。

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_scheduler_main.SchedulerMainTests.test_run_once_actuals_dispatches_existing_one_shot \
  tests.test_actuals_launchd
```

Expected: `Ran 2 tests`，`OK`。

### Task 3: 删除 APScheduler Actuals 时钟职责

**Files:**
- Modify: `scheduler/main.py:75`
- Modify: `scheduler/main.py:180-181`
- Modify: `scheduler/main.py:1235-1302`
- Test: `tests/test_scheduler_main.py`
- Test: `tests/test_actuals_launchd.py`

- [ ] **Step 1: 删除仅服务于 APScheduler Actuals 的常量和 formatter**

从 `scheduler/main.py` 删除：

```python
ACTUALS_REFRESH_TIMES = ((8, 30), (19, 0), (23, 45))
```

以及：

```python
def _format_actuals_refresh_times() -> str:
    return ", ".join(
        f"{hour:02d}:{minute:02d}"
        for hour, minute in ACTUALS_REFRESH_TIMES
    )
```

- [ ] **Step 2: 删除 Actuals executor 与三个 cron job**

从 `BlockingScheduler` 的 `executors` 映射删除：

```python
"actuals": APSchedulerThreadPoolExecutor(max_workers=1),
```

从 `build_scheduler()` 删除：

```python
for hour, minute in ACTUALS_REFRESH_TIMES:
    scheduler.add_job(
        run_actuals_job,
        trigger=CronTrigger(
            hour=hour,
            minute=minute,
            timezone=ASIA_SHANGHAI,
        ),
        id=f"actuals:{hour:02d}{minute:02d}",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        executor="actuals",
    )
logger.info(
    "Scheduled actuals refresh at %s Asia/Shanghai",
    _format_actuals_refresh_times(),
)
```

不得修改同函数中的 prediction、ledger control、startup catch-up job。

- [ ] **Step 3: 运行 Actuals/scheduler 定向回归并确认绿灯**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_actuals_launchd \
  tests.test_scheduler_main
```

Expected: 全部通过；`build_scheduler()` 在 legacy/ledger 下均无 `actuals:*` job，
`--run-once actuals` 和两个日期语义测试仍通过。

- [ ] **Step 4: 验证旧 scheduler Actuals 符号完全消失**

Run:

```bash
if rg -n \
  "ACTUALS_REFRESH_TIMES|_format_actuals_refresh_times|actuals:(0830|1900|2345)|Scheduled actuals refresh" \
  scheduler/main.py; then
  exit 1
fi
rg -n "run_actuals_job|--run-once" scheduler/main.py
```

Expected: 第一项无输出且返回 0；第二项仍列出 `run_actuals_job` 定义、CLI choice 和
`--run-once actuals` 分派。

- [ ] **Step 5: 提交代码与测试收敛**

提交前执行：

```bash
git status --short
git branch --show-current
git branch -vv
git diff --check
```

Expected: 当前分支为 `codex/audit-bugfixes-20260613`；变更只有
`scheduler/main.py` 和 `tests/test_scheduler_main.py`，不包含 `outputs/`。

然后执行：

```bash
git add scheduler/main.py tests/test_scheduler_main.py
git commit -m "refactor: retire apscheduler actuals jobs"
```

### Task 4: 将当前文档收敛到 launchd Actuals 权威

**Files:**
- Modify: `docs/architecture/ARCHITECTURE.md:18-31`
- Modify: `docs/architecture/ARCHITECTURE.md:106-116`
- Modify: `docs/architecture/ARCHITECTURE.md:573-580`
- Modify: `docs/architecture/CODE_ARCHITECTURE.md:193`
- Modify: `deploy/README.md:245-256`
- Test: `tests/test_onboarding_docs.py`

- [ ] **Step 1: 修正系统图和 Actuals 数据流**

在 `docs/architecture/ARCHITECTURE.md` 的 Scheduler 图中把：

```text
│  │  actuals jobs   │
```

改为：

```text
│  │ prediction jobs │
```

并在图后加入：

```markdown
Actuals 不挂载在常驻 APScheduler 中。独立
`com.bond-factor-lab.actuals` LaunchAgent 在 `08:30/19:00/23:45` 启动一次性
`scheduler.main --run-once actuals` 进程；三个时点和进程退出状态均由 launchd 管理。
```

把 §2.2 第一行改为：

```text
launchd 在每日 08:30、19:00 和 23:45 启动一次性 Actuals 任务；交易日 daily/weekly 刷新到当日，非交易日 daily/weekly 刷新到上一交易日，monthly 仍刷新到自然 run date
```

- [ ] **Step 2: 修正 launchd 进程清单**

把 §7.1 改为：

```markdown
### 7.1 进程管理（launchd）

当前核心 launchd plist：

- `com.bond-factor-lab.scheduler.plist` — 常驻预测调度器，不注册 Actuals job；
- `com.bond-factor-lab.actuals.plist` — `08:30/19:00/23:45` 一次性 Actuals 任务；
- `com.bond-factor-lab.backend.plist` — FastAPI 后端。
```

不在本批重写同文件的日度预测或 ledger 章节。

- [ ] **Step 3: 修正代码架构主蓝图**

把 `docs/architecture/CODE_ARCHITECTURE.md` 的 Actuals 段落替换为：

```markdown
日频、周频、月频 actuals 由独立
`com.bond-factor-lab.actuals` LaunchAgent 启动
`scheduler.main --run-once actuals` 一次性刷新。当前生产节奏为
`08:30/19:00/23:45`，其中夜间 `23:45` 用于承接上游 Wind 日频晚间导入；非交易日
daily/weekly actuals 刷新到上一交易日，monthly actuals 仍刷新到自然 run date，以同时
覆盖周末补刷和自然 15 号月度规则。常驻 APScheduler 不得再注册 `actuals:*` job。
```

- [ ] **Step 4: 在部署入口记录现行 Actuals LaunchAgent**

在 `deploy/README.md` 的后端 admin token 章节之后加入：

````markdown
### 3a) 本地 Mac：Actuals 一次性 LaunchAgent

`deploy/launchd/com.bond-factor-lab.actuals.plist` 是 Actuals 的唯一生产调度权威，
在每日 `08:30/19:00/23:45` 执行：

```text
python -m scheduler.main --run-once actuals
```

该任务 `RunAtLoad=false`、不设置 `KeepAlive`；常驻
`com.bond-factor-lab.scheduler` 不得再注册 `actuals:0830`、`actuals:1900` 或
`actuals:2345`。本节只记录现行入口，本批不重载 installed plist 或 scheduler。
````

- [ ] **Step 5: 运行文档门禁与当前入口扫描**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs tests.test_actuals_launchd
rg -n \
  "com\.bond-factor-lab\.actuals|run-once actuals" \
  docs/architecture deploy/README.md
```

Expected: 文档与 plist 测试全部通过；扫描结果只表达 launchd 一次性权威和
APScheduler 禁止边界，不再把 Actuals 描述成常驻 scheduler 注册的任务。

- [ ] **Step 6: 提交当前文档收敛**

提交前执行：

```bash
git status --short
git branch --show-current
git branch -vv
git diff --check
```

然后执行：

```bash
git add \
  docs/architecture/ARCHITECTURE.md \
  docs/architecture/CODE_ARCHITECTURE.md \
  deploy/README.md
git commit -m "docs: make launchd authoritative for actuals"
```

Expected: 形成独立文档提交；不改写 `docs/records/`、历史 specs/plans 或 plist。

### Task 5: 验证 Actuals 行为与全仓边界

**Files:**
- Verify only: repository-wide Python, plist and Markdown files

- [ ] **Step 1: 运行 Actuals updater 与 scheduler 定向回归**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
  tests.test_actuals_launchd \
  tests.test_daily_actuals \
  tests.test_weekly_actuals \
  tests.test_monthly_actuals \
  tests.test_scheduler_main \
  tests.test_scheduler_direct_authority \
  tests.test_daily_runtime \
  tests.test_daily_direct_cache_runtime
```

Expected: 全部通过；一次性 Actuals 行为、三类 updater、预测调度和日度 direct
authority 均无回归。

- [ ] **Step 2: 运行完整测试**

冻结服务环境不安装 pytest。为完整 `unittest discover` 的 import 阶段在 `/tmp` 创建
一次性 pytest support 目录，同时保持解释器与 `sys.prefix` 为原始服务环境：

```bash
actuals_test_support=$(mktemp -d /tmp/bfl-actuals-authority-pytest.XXXXXX)
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m pip install -q --target "$actuals_test_support" pytest==9.1.1
PYTHONPATH="$actuals_test_support" \
  /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
  -m unittest discover -s tests
```

Expected: 全仓测试零失败、零错误；允许既有条件 skip。临时 support 目录只位于
`/tmp`，不修改仓库或冻结 Conda 环境。

- [ ] **Step 3: 运行编译、格式、引用和越界门禁**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m compileall -q \
  backend backtests harness migrations scheduler scripts shared tests
git diff --check
if rg -n \
  "ACTUALS_REFRESH_TIMES|_format_actuals_refresh_times|actuals:(0830|1900|2345)|Scheduled actuals refresh" \
  scheduler/main.py; then
  exit 1
fi
git diff --exit-code d3f39ed..HEAD -- \
  deploy/launchd/com.bond-factor-lab.actuals.plist \
  scheduler/daily_actuals_updater.py \
  scheduler/weekly_actuals_updater.py \
  scheduler/monthly_actuals_updater.py \
  scheduler/daily_runtime.py \
  scheduler/daily_direct_authority.py \
  scheduler/capacity_candidate_runtime.py \
  migrations schemes shared
```

Expected: 全部返回 0；Actuals plist、三个 updater、日度/ledger、migration、算法和共享层
没有越界修改。

- [ ] **Step 4: 核对最终分支和发布边界**

Run:

```bash
git status --short --branch
git log -5 --oneline --decorate
git rev-parse master origin/master
git rev-list --left-right --count \
  origin/codex/audit-bugfixes-20260613...HEAD
```

Expected: 工作树干净；开发分支新增设计、计划、代码和文档提交；`master` 与
`origin/master` 仍为 `f11c28d`；开发分支未推送，installed launchd 和当前运行进程未被
修改。
