# Blackbox V2 Historical Gray Backfill Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加只对 Blackbox V2 开放的逐日 `gray-backfill` Gate，以当前 DataBridge 快照按历史 `feature_date` 做 live-safe as-of 重放，并安全补齐四个 1Y T+5 方案从 `target_date=2026-06-01` 起的 `gray_live`。

**Architecture:** 复用现有 LiveGate 的 exact-version、latest all-stage、表增量和原子提交边界，只通过独立 action 与显式 executor snapshot mode 开放历史快照重放。普通 live/scheduler 始终保持 fresh-only；历史模式给结果追加 snapshot、generation、refresh date、replay mode 和实际补齐时间。

**Tech Stack:** Python 3.12、FastAPI/SQLAlchemy、MySQL 8、现有 Harness HMAC authorization、Blackbox V2 Contract 1.0、`unittest`/`pytest`。

---

### Task 1: CLI 与 Gate 路由

**Files:**
- Modify: `tests/test_blackbox_v2_harness_dispatch.py`
- Modify: `harness/cli.py`
- Modify: `harness/registry.py`
- Create: `harness/gates/gray_backfill_gate.py`

- [ ] **Step 1: 写 CLI 和路由失败测试**

在 `tests/test_blackbox_v2_harness_dispatch.py` 增加：

```python
def test_dispatches_blackbox_gray_backfill_as_explicit_gate(self) -> None:
    from harness.gates.gray_backfill_gate import GrayBackfillGate
    from harness.registry import gate_for_name

    ctx = GateContext(
        scheme_id="blackbox_trial",
        predict_date="2026-05-26",
        project_root=Path("/tmp/project"),
        report_dir=Path("/tmp/report"),
        config=SimpleNamespace(runtime_type="blackbox_v2"),
    )
    self.assertIsInstance(gate_for_name("gray-backfill", ctx=ctx), GrayBackfillGate)

def test_gray_backfill_cli_requires_predict_date(self) -> None:
    from harness.cli import _build_parser, _run_gate

    args = _build_parser().parse_args([
        "gate", "gray-backfill", "--scheme-id", "blackbox_trial",
        "--prediction-phase", "gray_live",
    ])
    with self.assertRaisesRegex(SystemExit, "requires --predict-date"):
        _run_gate(args)
```

- [ ] **Step 2: 运行测试并确认 RED**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_blackbox_v2_harness_dispatch -v
```

预期：因 `gray-backfill` 尚未注册或 `GrayBackfillGate` 不存在而失败。

- [ ] **Step 3: 实现最小 Gate 壳和 CLI 路由**

在 `harness/gates/gray_backfill_gate.py` 创建：

```python
from harness.gates.live_gate import LiveGate


class GrayBackfillGate(LiveGate):
    name = "gray-backfill"
    authorization_action = "gray_backfill_write"
    required_prediction_phase = "gray_live"
    blackbox_snapshot_mode = "historical_as_of_replay"
```

将 `gray-backfill` 加入 `harness.cli` 的显式 gate 列表和必填日期集合；在 `harness.registry` 只把它加入 Blackbox post-activation map，不加入 `AUTO_SEQUENCE`，Native 路由保持不支持。

- [ ] **Step 4: 运行测试并确认 GREEN**

运行 Step 2 命令，预期全部通过。

### Task 2: DataBridge snapshot provenance 与历史执行模式

**Files:**
- Modify: `tests/test_blackbox_v2_input_artifacts.py`
- Modify: `tests/test_blackbox_v2_runner.py`
- Modify: `tests/test_executor_run_id.py`
- Modify: `shared/blackbox_v2/snapshot.py`
- Modify: `shared/input_artifacts.py`
- Modify: `scheduler/executor.py`

- [ ] **Step 1: 写 snapshot provenance 失败测试**

在 input artifact 测试中让 `check_current_dataset` 返回：

```python
CurrentDataset(
    state={"generation_id": "full-20260720-test", "refresh_date": "2026-07-20"},
    dataset=validated,
)
```

并断言构建后的 `BlackboxSnapshot.generation_id` 和 `refresh_date` 等于上述值。

- [ ] **Step 2: 写 executor 模式隔离失败测试**

在 `tests/test_blackbox_v2_runner.py` 增加两个行为断言：

```python
result = run_blackbox_scheme_subprocess(
    cfg,
    "2026-05-26",
    engine="engine",
    algo_env="forecast_env_blackbox_v1",
    timeout_sec=600,
    snapshot_mode="historical_as_of_replay",
)
self.assertFalse(open_snapshot.call_args.kwargs["require_fresh"])
self.assertEqual(result[0].extra["replay_semantics"],
                 "current_snapshot_as_of_not_historical_vintage")
self.assertEqual(result[0].extra["backfill_mode"],
                 "post_deployment_live_safe_replay")
self.assertEqual(result[0].extra["data_generation_id"], "full-20260720-test")
self.assertEqual(result[0].extra["source_refresh_date"], "2026-07-20")
```

同时保留现有 scheduled live 测试，断言默认模式仍传 `require_fresh=True`。在 executor run 测试中断言历史模式能从 `execute_scheme` 传到 `run_configured_scheme`，Native 路径拒绝非默认 snapshot mode。

- [ ] **Step 3: 运行相关测试并确认 RED**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_blackbox_v2_input_artifacts \
  tests.test_blackbox_v2_runner \
  tests.test_executor_run_id -v
```

预期：snapshot 尚无 provenance 字段、executor 尚无 `snapshot_mode` 参数而失败。

- [ ] **Step 4: 实现 snapshot provenance**

给 `BlackboxSnapshot` 增加向后兼容的可空字段：

```python
generation_id: str | None = None
refresh_date: str | None = None
```

`build_blackbox_input_snapshot()` 从 `CurrentDataset.state` 读取两个非空字符串，并用 `dataclasses.replace()` 写回 snapshot；字段缺失或空值 fail-closed。

- [ ] **Step 5: 实现显式 snapshot mode**

在 `scheduler.executor` 定义：

```python
BLACKBOX_SNAPSHOT_MODE_FRESH = "fresh"
BLACKBOX_SNAPSHOT_MODE_HISTORICAL_AS_OF = "historical_as_of_replay"
```

将 `blackbox_snapshot_mode` 从 `execute_scheme()` 传到 `run_configured_scheme()`，再传到 `run_blackbox_scheme_subprocess()`。默认值必须是 `fresh`；历史模式使用 `require_fresh=False`，要求 `predict_date < snapshot.refresh_date`，并用 `dataclasses.replace()` 给返回记录增加：

```python
{
    "replay_semantics": "current_snapshot_as_of_not_historical_vintage",
    "backfill_mode": "post_deployment_live_safe_replay",
    "backfilled_at": datetime.now(timezone.utc).isoformat(),
    "data_generation_id": snapshot.generation_id,
    "source_refresh_date": snapshot.refresh_date,
}
```

未知模式、历史日期不早于 refresh date、缺少 provenance 或 Native 使用历史模式均 fail-closed。

- [ ] **Step 6: 运行测试并确认 GREEN**

运行 Step 3 命令，预期全部通过。

### Task 3: 授权、前置检查和原子写入边界

**Files:**
- Create: `tests/test_blackbox_v2_gray_backfill_gate.py`
- Modify: `tests/test_blackbox_v2_live_gate.py`
- Modify: `harness/gates/live_gate.py`
- Modify: `harness/gates/gray_backfill_gate.py`

- [ ] **Step 1: 写授权隔离和成功路径失败测试**

新测试构造 active Blackbox config 和 `gray_backfill_write` token，断言：

```python
result = GrayBackfillGate().run(ctx)
self.assertTrue(result.passed, result.errors)
self.assertEqual(execute.call_args.kwargs["prediction_phase"], "gray_live")
self.assertEqual(
    execute.call_args.kwargs["blackbox_snapshot_mode"],
    "historical_as_of_replay",
)
```

表快照 before/after 必须精确为 prediction/run/log 各 `+1`。另写测试证明 `live_write` token 被 GrayBackfillGate 拒绝，`gray_backfill_write` token 被 LiveGate 拒绝。

- [ ] **Step 2: 写前置日期和重复 target 失败测试**

模拟前置检查结果并分别断言：

- 当前 refresh date 为 `2026-07-20`，`predict_date >= refresh_date` 被拒绝；
- canonical latest current-snapshot backtest 最大 target 为 `2026-05-29`；候选 target 必须严格更晚；
- 候选 `scheme_id + tenor + horizon + target_date` 已存在时被拒绝且 token 未消费；
- 没有 canonical latest historical run 时被拒绝。

- [ ] **Step 3: 运行测试并确认 RED**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_blackbox_v2_gray_backfill_gate \
  tests.test_blackbox_v2_live_gate -v
```

预期：LiveGate 尚未读取子类 action/mode，GrayBackfill 前置检查尚未实现。

- [ ] **Step 4: 参数化 LiveGate 的最小差异点**

给 LiveGate 增加类属性：

```python
authorization_action = "live_write"
required_prediction_phase: str | None = None
blackbox_snapshot_mode = "fresh"
```

Blackbox token 校验使用 `self.authorization_action`；若 `required_prediction_phase` 非空则要求精确匹配；调用 executor 时传 `blackbox_snapshot_mode`。所有 blocked/failed 结果使用 `self.name`，evidence 增加 `authorization_action` 和 `blackbox_snapshot_mode`。普通 LiveGate 行为保持不变。

- [ ] **Step 5: 实现 GrayBackfill 前置检查**

在 `harness/gates/gray_backfill_gate.py` 增加只读 helper：

- 校验 current DataBridge 后读取 `generation_id/refresh_date`；
- 用平台 calendar 与 Metadata 推导候选 `feature_date/target_date`；
- 从 `v_latest_backtest_run` 选择该 base scheme 最新 `blackbox_v2_current_snapshot_as_of` 成功 run，再读取其最大 target；
- 查询候选业务唯一键是否已存在；
- 返回结构化 evidence，任一约束失败时在消费 token 前 blocked。

成功后调用共享 LiveGate 路径，仍由原子 precommit delta 校验阻止并发重复写入。

- [ ] **Step 6: 运行测试并确认 GREEN**

运行 Step 3 命令，预期全部通过。

### Task 4: 完整回归与代码提交

**Files:**
- Verify only: `harness/cli.py`
- Verify only: `harness/registry.py`
- Verify only: `harness/gates/live_gate.py`
- Verify only: `harness/gates/gray_backfill_gate.py`
- Verify only: `scheduler/executor.py`
- Verify only: `shared/blackbox_v2/snapshot.py`
- Verify only: `shared/input_artifacts.py`
- Verify only: the tests listed in Tasks 1–3

回归若暴露上述范围之外的问题，先记录失败证据并停止；不得用本任务顺手修改其它子系统。

- [ ] **Step 1: 运行 Blackbox、scheduler、backend 和 frontend 回归**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest discover \
  -s tests -p 'test_blackbox_v2_*.py' -v
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_scheduler_main tests.test_backend_api tests.test_frontend_factor_lab \
  tests.test_onboarding_docs -v
```

预期：全部通过；普通 scheduled/live freshness 测试仍为 GREEN。

- [ ] **Step 2: 检查工作区并提交代码**

先运行 `git status --short`、`git branch --show-current`、`git branch --list`。只暂存本计划涉及的代码和测试，排除 plist、`reports/production-gray-20260720/`、`outputs/`，提交：

```bash
git commit -m "feat: add authorized blackbox gray backfill"
```

### Task 5: 生产 Canary 与四方案补齐

**Files:**
- Runtime evidence only under ignored `reports/harness/` and `reports/production-gray-20260720/`.

- [ ] **Step 1: 只读核对生产前状态**

确认四个 exact version/latest all-stage、active Registry、DataBridge generation/snapshot、latest backtest run `178..181`、每方案当前 1 条 `gray_live`、`scheduled_live=0`，并确认 scheduler PID 未变化。

- [ ] **Step 2: 执行首日 Canary**

为 `one_y_t5_liq_excess_a_v1 + 2026-05-26` 签发 900 秒以内的独立 HMAC token：

```bash
python -m harness auth issue \
  --scheme-id one_y_t5_liq_excess_a_v1 \
  --action gray_backfill_write \
  --predict-date 2026-05-26 \
  --scheme-version 8d583560c9f1 \
  --harness-run-id hr_20260720T114504Z_cb6bf6eac22c \
  --expires-in 900 \
  --issued-by codex-gray-boundary-repair-20260720
```

随后执行 `harness gate gray-backfill`，核对 prediction/run/log 精确各 `+1`，日期为 `predict=2026-05-26, feature=2026-05-25, target=2026-06-01`，provenance 完整。

- [ ] **Step 3: 串行补齐剩余缺口**

对每个方案先用下列只读脚本动态输出缺失 signal date，并要求输出数等于该方案预期缺口：

```bash
scheme_id=one_y_t5_liq_excess_a_v1
missing_dates="$(conda run --no-capture-output -n bond_factor_lab_service python - "$scheme_id" <<'PY'
import sys
from sqlalchemy import text
from scheduler.repository import create_engine_from_env
from shared.calendar_service import get_calendar
from shared.prediction_context import build_daily_live_context

scheme_id = sys.argv[1]
engine = create_engine_from_env()
calendar = get_calendar(engine)
with engine.connect() as conn:
    trading_days = [
        str(row[0])[:10]
        for row in conn.execute(text("""
            SELECT rdate FROM t_trade_calendar
            WHERE trade_flag='1' AND rdate BETWEEN '2026-05-26' AND '2026-07-20'
            ORDER BY rdate
        """))
    ]
    existing = {
        str(row[0])[:10]
        for row in conn.execute(text("""
            SELECT target_date FROM t_scheme_predictions
            WHERE scheme_id=:scheme_id AND target_tenor='1Y' AND horizon=5
        """), {"scheme_id": scheme_id})
    }
for predict_date in trading_days:
    context = build_daily_live_context(calendar, predict_date, horizon=5)
    if context.target_date >= "2026-06-01" and context.target_date not in existing:
        print(predict_date)
engine.dispose()
PY
)"
test "$(wc -l <<< "$missing_dates" | tr -d ' ')" -le 38
```

然后对下列四组绑定逐日执行；Canary 成功后首个方案剩余应为 37 个，其它方案应各为 38 个：

```text
one_y_t5_liq_excess_a_v1|8d583560c9f1|hr_20260720T114504Z_cb6bf6eac22c
one_y_t5_liq_excess_a_w252_l7_v1|103c93bbc913|hr_20260720T114556Z_97bf5d06c341
one_y_t5_liq_excess_a_w350_l7_v1|86b458c568a5|hr_20260720T114746Z_fd4320122d72
one_y_t5_liq_excess_b_w252_l7_v1|ba00891cd179|hr_20260720T114824Z_4466dcbf4068
```

每个日期立即签发并消费独立 token：

```bash
auth_token="$(conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness auth issue \
  --scheme-id "$scheme_id" --action gray_backfill_write \
  --predict-date "$predict_date" --scheme-version "$scheme_version" \
  --harness-run-id "$harness_run_id" --expires-in 900 \
  --issued-by codex-gray-boundary-repair-20260720)"
conda run --no-capture-output -n bond_factor_lab_service \
  python -m harness gate gray-backfill \
  --scheme-id "$scheme_id" --predict-date "$predict_date" \
  --project-root /Users/macstudio0/bond-factor-lab \
  --algo-env forecast_env_blackbox_v1 --timeout-sec 600 \
  --prediction-phase gray_live --authorize "$auth_token"
```

任一失败立即停止，不重跑已有 target，不重启 scheduler。

- [ ] **Step 4: 核对最终数据库**

每个方案必须满足：latest backtest 333 条/17 月、`gray_live=39`、`scheduled_live=0`、gray predict range `2026-05-26..2026-07-20`、gray target range `2026-06-01..2026-07-24`、history/live target overlap=0。失败 run `960` 保留为审计证据，不计入成功 phase range。

### Task 6: API、前端、SOP 与证据收口

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Modify: `docs/blackbox_v2/PRODUCTION_READINESS.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md`
- Modify: `tests/test_onboarding_docs.py`
- Do not modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`

- [ ] **Step 1: 验证 API 和前端**

API 必须返回四个短名、run `178..181` 的 333 条历史样本，以及每方案完整 gray phase range。浏览器核对 `1Y国债活跃 × T+5` 四候选、2026-05 历史结束、2026-06 灰度开始、未来 actual pending、部署日期和补齐口径不混淆、控制台错误为 0。

- [ ] **Step 2: 先写文档守护失败测试，再更新平台文档**

新增断言要求平台 SOP 出现 `gray-backfill`、`gray_backfill_write`、`current_snapshot_as_of_not_historical_vintage`、普通 LiveGate 仍 fresh-only、重复 target fail-closed；同时锁定上游 SOP SHA-256 不变。先确认测试 RED，再更新指定平台文档到 GREEN。

- [ ] **Step 3: 运行最终验证**

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m unittest \
  tests.test_blackbox_v2_gray_backfill_gate \
  tests.test_blackbox_v2_live_gate \
  tests.test_blackbox_v2_runner \
  tests.test_scheduler_main \
  tests.test_backend_api \
  tests.test_frontend_factor_lab \
  tests.test_onboarding_docs -v
```

再执行生产只读 SQL/API 对账和浏览器截图；核对 scheduler PID 未变化、无意外 `scheduled_live`。

- [ ] **Step 4: 提交文档和证据索引**

按项目规范复核 status/branches，只提交平台 SOP、当前状态、readiness、生产记录、文档测试和选择性证据索引。提交信息：

```bash
git commit -m "docs: close blackbox gray backfill evidence"
```

不得合并或推送 `master`。
