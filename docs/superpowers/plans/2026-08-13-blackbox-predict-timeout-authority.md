# Blackbox Predict Timeout Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `blackbox-v2-v1` Runtime Profile the only Blackbox prediction-timeout authority, preserving the current 600-second scheduled behavior while rejecting scheme-level timeout declarations.

**Architecture:** The Blackbox config contract, Intake, canonical versioning, and discovery will remove `schedule.timeout_sec`. The runtime profile will declare 600 seconds, while executor callers may pass an optional operation deadline that the runner can only narrow with `min(profile, deadline)`; Native and Blackbox backtest budgets remain unchanged. Existing Blackbox configs are migrated atomically, which intentionally produces new exact scheme versions and therefore requires the existing separately authorized Gate/activation workflow before any production deployment.

**Tech Stack:** Python 3.12, dataclasses, JSON/YAML configuration, `unittest`/pytest, existing Blackbox V2 runner and Harness contracts.

---

## File map

- `shared/scheme_config_schema.py`: runtime-aware config validation; Blackbox rejects `schedule.timeout_sec`, Native keeps it.
- `shared/blackbox_v2/intake.py`: generated Blackbox config no longer contains a timeout.
- `shared/blackbox_v2/versioning.py`: canonical Blackbox config no longer versions a scheme-level timeout.
- `scheduler/discovery.py`: Blackbox `SchemeSchedule.timeout_sec` is always `None` after validation.
- `deploy/blackbox_v2/runtime_profile_v1.json`: the sole Blackbox predict budget changes from the stale declaration 3600 to the already-effective 600 seconds.
- `scheduler/executor.py`: preserves optional operation deadlines without rewriting Runtime Profile limits or logging a truncation warning.
- `scheduler/blackbox_v2_runner.py`: threads an optional predict deadline into the existing `min(profile, deadline)` subprocess boundary.
- `tests/test_config_schema.py`, `tests/test_blackbox_v2_intake.py`, `tests/test_blackbox_v2_discovery.py`: fail-closed config, Intake, and canonical hash coverage.
- `tests/test_blackbox_timeout_drift.py`, `tests/test_blackbox_v2_runner.py`: Runtime Profile authority, narrowing-only deadline, Native isolation, and repository inventory coverage.
- `schemes/*/config.yaml` for the 39 Blackbox schemes listed in Task 3: remove the obsolete field only.
- `docs/architecture/ARCHITECTURE.md`, `docs/architecture/BLACKBOX_V2_PLATFORM.md`, `docs/architecture/CODE_ARCHITECTURE.md`, `docs/architecture/HARNESS_ARCHITECTURE.md`, `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`: describe the single authority and production version boundary.

### Task 1: Retire the Blackbox scheme-level timeout contract

**Files:**
- Modify: `tests/test_config_schema.py`
- Modify: `tests/test_blackbox_v2_intake.py`
- Modify: `tests/test_blackbox_v2_discovery.py`
- Modify: `shared/scheme_config_schema.py`
- Modify: `shared/blackbox_v2/intake.py`
- Modify: `shared/blackbox_v2/versioning.py`
- Modify: `scheduler/discovery.py`
- Modify: the 39 Blackbox `schemes/*/config.yaml` paths listed in Step 5

- [ ] **Step 1: Write the failing config and Intake tests**

Add a Blackbox fixture and rejection test to `tests/test_config_schema.py`, while retaining the existing Native positive-timeout test:

```python
def _base_blackbox_config() -> dict:
    return {
        "scheme_id": "demo_blackbox",
        "runtime_type": "blackbox_v2",
        "input_source": "data_bridge_current",
        "runtime_profile": "blackbox-v2-v1",
        "data_schema_version": "data-bridge-v1",
        "status": "paused",
        "version_status": "draft",
        "schedule": {
            "cron": "3 7 * * 1-5",
            "timezone": "Asia/Shanghai",
        },
        "delivery": {
            "script": "delivery/demo_blackbox.py",
            "metadata": "delivery/demo_blackbox.json",
        },
    }


def test_blackbox_schedule_timeout_sec_is_forbidden(self) -> None:
    config = _base_blackbox_config()
    config["schedule"]["timeout_sec"] = 600

    errors = validate_config(config, dirname="demo_blackbox")

    self.assertIn(
        "Blackbox V2 schedule.timeout_sec is forbidden; "
        "predict timeout is owned by runtime_profile",
        errors,
    )
```

In `tests/test_blackbox_v2_intake.py::test_intake_preserves_delivery_bytes_and_generates_paused_config`, add:

```python
self.assertNotIn("timeout_sec", config)
```

In `tests/test_blackbox_v2_discovery.py`:

```python
def test_blackbox_rejects_schedule_timeout(self) -> None:
    from scheduler.discovery import load_scheme_config

    with tempfile.TemporaryDirectory() as tmpdir:
        scheme_dir = _write_blackbox_scheme(Path(tmpdir))
        config_path = scheme_dir / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "  timezone: Asia/Shanghai\n",
                "  timezone: Asia/Shanghai\n  timeout_sec: 600\n",
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "schedule.timeout_sec is forbidden"):
            load_scheme_config(config_path)
```

Also assert the valid fixture resolves `config.schedule.timeout_sec is None`.

Add a repository-inventory test to the same class:

```python
def test_repository_blackbox_configs_do_not_declare_timeout(self) -> None:
    import yaml

    project_root = Path(__file__).resolve().parents[1]
    blackbox_paths = []
    for path in sorted((project_root / "schemes").glob("*/config.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw.get("runtime_type") == "blackbox_v2":
            blackbox_paths.append(path)
            self.assertNotIn("timeout_sec", raw["schedule"], str(path))
    self.assertEqual(len(blackbox_paths), 39)
```

- [ ] **Step 2: Run the contract tests and verify they fail for the intended reasons**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q \
  tests/test_config_schema.py::ConfigSchemaScheduleTests \
  tests/test_blackbox_v2_intake.py::BlackboxV2IntakeTests::test_intake_preserves_delivery_bytes_and_generates_paused_config \
  tests/test_blackbox_v2_discovery.py::BlackboxV2DiscoveryTests::test_blackbox_rejects_schedule_timeout \
  tests/test_blackbox_v2_discovery.py::BlackboxV2DiscoveryTests::test_repository_blackbox_configs_do_not_declare_timeout
```

Expected: FAIL because Blackbox validation still accepts `timeout_sec`, Intake still emits it, discovery does not reject it, and all 39 repository configs still declare it.

- [ ] **Step 3: Implement fail-closed Blackbox validation and stop Intake generation**

Add this check inside the Blackbox schedule branch in `shared/scheme_config_schema.py`:

```python
if "timeout_sec" in schedule:
    errors.append(
        "Blackbox V2 schedule.timeout_sec is forbidden; "
        "predict timeout is owned by runtime_profile"
    )
```

Delete only this line from `shared/blackbox_v2/intake.py::_config_text`:

```python
"  timeout_sec: 3600\n"
```

- [ ] **Step 4: Remove timeout from canonical Blackbox versioning and discovery**

Change `shared/blackbox_v2/versioning.py::canonical_platform_config` so its schedule payload is exactly:

```python
canonical["schedule"] = {
    "cron": str(_required_value(schedule, "cron", "schedule.cron")),
    "timezone": str(schedule.get("timezone", DEFAULT_TIMEZONE)),
}
```

Change the Blackbox schedule construction in `scheduler/discovery.py::_load_blackbox_config` to:

```python
schedule=SchemeSchedule(
    cron=str(schedule_raw["cron"]),
    timezone=str(schedule_raw.get("timezone", "Asia/Shanghai")),
    timeout_sec=None,
),
```

In `tests/test_blackbox_v2_discovery.py`, delete the old `test_blackbox_version_changes_when_schedule_timeout_changes`, remove `timeout_sec` from `_canonical_raw_config()` and the reordered canonical fixture, rename the frozen hash test to describe the single-authority canonical contract, and update its exact expected SHA-256 to:

```python
"7cced30aca764474c1771890d755d6bdfc868e90f1da28f2196b91edb5bbe6ff"
```

- [ ] **Step 5: Remove only the obsolete field from the 39 Blackbox configs**

Delete exactly the line `  timeout_sec: 3600` from each file below; do not change cron, timezone, lifecycle state, delivery paths, display names, platform inputs, or metadata:

```text
schemes/cgb_a4_fundseason_10y/config.yaml
schemes/cgb_a4_fundseason_1y/config.yaml
schemes/cgb_a4_fundseason_3y/config.yaml
schemes/cgb_a4_fundseason_5y/config.yaml
schemes/cgb_a4_fundseason_7y/config.yaml
schemes/cgb_causal_wk_1y/config.yaml
schemes/cgb_causal_wk_1y_v128/config.yaml
schemes/cgb_causal_wk_3y/config.yaml
schemes/five_y_t5_lgbm_3y_anti_lag252_b8_v1/config.yaml
schemes/five_y_t5_lgbm_3y_z_anti180_b12_v1/config.yaml
schemes/five_y_t5_xgb_spr_3y1y_b8_v1/config.yaml
schemes/one_y_t1_quote_state_hv_v1/config.yaml
schemes/one_y_t5_liq_excess_a_v1/config.yaml
schemes/one_y_t5_liq_excess_a_w252_l7_v1/config.yaml
schemes/one_y_t5_liq_excess_a_w350_l7_v1/config.yaml
schemes/one_y_t5_liq_excess_b_w252_l7_v1/config.yaml
schemes/one_y_t5_xgb_10y_streak_anti7_b8_v1/config.yaml
schemes/one_y_t5_xgb_7y_cond_rev20_b12_v1/config.yaml
schemes/one_y_t5_xgb_spr_zrev_10y5y_b12_v1/config.yaml
schemes/seven_y_current55_lgbm_001_v2/config.yaml
schemes/seven_y_current55_lgbm_002_v2/config.yaml
schemes/seven_y_t5_lgbm_bf_z_anti40_b8_v1/config.yaml
schemes/seven_y_t5_xgb_7y_rv_rev20_b0_v1/config.yaml
schemes/seven_y_t5_xgb_bf_z_anti40_b0_v1/config.yaml
schemes/ten_y_t5_maj3_k3_ic_static_v1/config.yaml
schemes/ten_y_t5_maj4_k3_ic_static_v1/config.yaml
schemes/ten_y_t5_maj4_k3_ic_yearly_v1/config.yaml
schemes/ten_y_t5_say_k5_sharpe_static_v1/config.yaml
schemes/three_y_adyn_lb1_k3_v1/config.yaml
schemes/three_y_adyn_lb2_k1_v1/config.yaml
schemes/three_y_t5_lgbm_7yanti_b12_v2/config.yaml
schemes/three_y_t5_xgb_fxlead_b8_v2/config.yaml
schemes/three_y_t5_xgb_tp_5y1y_b12_v2/config.yaml
schemes/wavg_10y_gapflip_v5/config.yaml
schemes/wavg_1y_gapflip_v5/config.yaml
schemes/wavg_3y_gapflip_v5/config.yaml
schemes/wavg_5y_gapflip_v5/config.yaml
schemes/wavg_7y_gapflip_v5/config.yaml
schemes/weekly_10y_lgbm_point_v1/config.yaml
```

- [ ] **Step 6: Run the focused contract tests and verify they pass**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q \
  tests/test_config_schema.py \
  tests/test_blackbox_v2_intake.py \
  tests/test_blackbox_v2_discovery.py \
  tests/test_active_blackbox_conformance.py
```

Expected: all selected tests PASS; Native positive `schedule.timeout_sec` remains valid.

- [ ] **Step 7: Commit the contract and repository migration atomically**

```bash
git add \
  shared/scheme_config_schema.py \
  shared/blackbox_v2/intake.py \
  shared/blackbox_v2/versioning.py \
  scheduler/discovery.py \
  tests/test_config_schema.py \
  tests/test_blackbox_v2_intake.py \
  tests/test_blackbox_v2_discovery.py \
  schemes/*/config.yaml
git diff --cached --check
git commit -m "fix(blackbox): retire scheme predict timeout"
```

Before committing, use `git diff --cached --name-only` and confirm every staged `schemes/*/config.yaml` is one of the 39 paths above. Each staged Blackbox config diff must contain exactly one deleted `timeout_sec` line and no addition.

### Task 2: Make Runtime Profile and operation deadline semantics executable

**Files:**
- Modify: `tests/test_blackbox_timeout_drift.py`
- Modify: `tests/test_blackbox_v2_runner.py`
- Modify: `deploy/blackbox_v2/runtime_profile_v1.json`
- Modify: `scheduler/executor.py`
- Modify: `scheduler/blackbox_v2_runner.py`

- [ ] **Step 1: Replace truncation-warning tests with authority tests**

Rewrite `tests/test_blackbox_timeout_drift.py` so its focused unit tests are:

```python
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _cfg(runtime_type: str, configured: int | None):
    return SimpleNamespace(
        scheme_id="demo",
        runtime_type=runtime_type,
        schedule=SimpleNamespace(timeout_sec=configured),
    )


class TimeoutAuthorityTests(unittest.TestCase):
    def _effective(self, cfg, deadline=None):
        from scheduler.executor import _effective_timeout_sec

        return _effective_timeout_sec(cfg, deadline)

    def test_runtime_profile_owns_600_second_predict_budget(self) -> None:
        from scheduler.blackbox_v2_runner import DEFAULT_RUNTIME_PROFILE

        self.assertEqual(DEFAULT_RUNTIME_PROFILE.predict_timeout_sec, 600)
        self.assertEqual(DEFAULT_RUNTIME_PROFILE.backtest_timeout_sec, 14400)

    def test_blackbox_without_operation_deadline_defers_to_profile(self) -> None:
        self.assertIsNone(self._effective(_cfg("blackbox_v2", None)))

    def test_blackbox_operation_deadline_remains_independent(self) -> None:
        self.assertEqual(self._effective(_cfg("blackbox_v2", None), 300), 300)
        self.assertEqual(self._effective(_cfg("blackbox_v2", None), 1800), 1800)

    def test_native_config_is_still_honoured(self) -> None:
        self.assertEqual(self._effective(_cfg("native_adapter", 3600)), 3600)

    def test_native_without_config_keeps_600_second_default(self) -> None:
        self.assertEqual(self._effective(_cfg("native_adapter", None)), 600)

    def test_runtime_profile_file_matches_loaded_contract(self) -> None:
        raw = json.loads(
            (PROJECT_ROOT / "deploy/blackbox_v2/runtime_profile_v1.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(raw["predict_timeout_sec"], 600)
        self.assertEqual(raw["backtest_timeout_sec"], 14400)
```

- [ ] **Step 2: Add runner tests for an independent optional deadline**

In `tests/test_blackbox_v2_runner.py`, extend the scheduled Blackbox mock assertion so a call with `timeout_sec=300` proves:

```python
self.assertEqual(predict.call_args.kwargs["profile"].predict_timeout_sec, 600)
self.assertEqual(predict.call_args.kwargs["timeout_sec"], 300)
```

Add a direct `run_blackbox_predict` mock test that verifies `timeout_sec=None` is omitted and `timeout_sec=1800` is passed independently to `execute_blackbox_cli`:

```python
def test_predict_forwards_optional_operation_deadline_independently(self) -> None:
    from scheduler.blackbox_v2_runner import RuntimeProfile, run_blackbox_predict

    for deadline in (None, 1800):
        with self.subTest(deadline=deadline), tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            kwargs = {
                "metadata": _metadata(),
                "script_path": root / "trial.py",
                "request": _request("001"),
                "data_dir": root / "data",
                "data_snapshot_id": "snapshot-test",
                "profile": RuntimeProfile.for_tests(predict_timeout_sec=600),
                "timeout_sec": deadline,
            }
            with (
                patch("scheduler.blackbox_v2_runner.execute_blackbox_cli") as execute,
                patch(
                    "scheduler.blackbox_v2_runner.load_prediction_result",
                    return_value="result",
                ),
                patch(
                    "scheduler.blackbox_v2_runner._to_prediction_record",
                    return_value="record",
                ),
            ):
                self.assertEqual(run_blackbox_predict(**kwargs), "record")

            if deadline is None:
                self.assertNotIn("timeout_sec", execute.call_args.kwargs)
            else:
                self.assertEqual(execute.call_args.kwargs["timeout_sec"], deadline)
```

Add this focused CLI boundary test using `_run_process` mocking so the actual subprocess wait value is observable without sleeping:

```python
def test_predict_operation_deadline_cannot_enlarge_profile(self) -> None:
    from scheduler.blackbox_v2_runner import RuntimeProfile, execute_blackbox_cli
    from shared.blackbox_v2.requests import write_request

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        script = _write_script(root / "trial.py", _SUCCESS_SCRIPT)
        data_dir = _write_data_dir(root)
        request = write_request(_request("001"), root / "request.json")
        output = root / "run" / "prediction.json"
        output.parent.mkdir()
        def completed(*_args, **_kwargs):
            output.write_text("{}\n", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "", "")

        with patch(
            "scheduler.blackbox_v2_runner._run_process",
            side_effect=completed,
        ) as run:
            execute_blackbox_cli(
                script_path=script,
                mode="predict",
                input_path=request,
                data_dir=data_dir,
                output_path=output,
                profile=RuntimeProfile.for_tests(predict_timeout_sec=600),
                timeout_sec=1800,
            )

    self.assertEqual(run.call_args.kwargs["timeout"], 600.0)
```

Repeat the call with a fresh temporary root and `timeout_sec=300`; assert `_run_process(..., timeout=300.0)`.

- [ ] **Step 3: Run the new authority tests and verify they fail**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q \
  tests/test_blackbox_timeout_drift.py \
  tests/test_blackbox_v2_runner.py -k 'timeout or scheduled_blackbox'
```

Expected: FAIL because the profile still declares 3600, executor still rewrites it, and `run_blackbox_predict` has no independent operation-deadline parameter.

- [ ] **Step 4: Correct the Runtime Profile declaration**

In `deploy/blackbox_v2/runtime_profile_v1.json`, change only:

```json
"predict_timeout_sec": 600
```

Keep `"backtest_timeout_sec": 14400` unchanged.

- [ ] **Step 5: Implement optional operation deadlines without Profile mutation**

Change `scheduler.executor.execute_scheme` to accept an optional deadline:

```python
timeout_sec: int | None = None,
```

Replace `_effective_timeout_sec` with:

```python
def _effective_timeout_sec(
    cfg: SchemeConfig,
    operation_timeout_sec: int | None,
) -> int | None:
    """解析本次执行 deadline；Blackbox 基础预算只来自 Runtime Profile。"""
    runtime_type = getattr(cfg, "runtime_type", "native_adapter")
    if runtime_type == "blackbox_v2":
        if operation_timeout_sec is None:
            return None
        timeout = int(operation_timeout_sec)
    else:
        schedule = getattr(cfg, "schedule", None)
        configured = getattr(schedule, "timeout_sec", None)
        if configured is None:
            configured = getattr(cfg, "execution_timeout_sec", None)
        fallback = 600 if operation_timeout_sec is None else int(operation_timeout_sec)
        timeout = int(configured) if configured is not None else fallback
    if timeout <= 0:
        raise ValueError(
            f"scheme {cfg.scheme_id} timeout_sec must be positive, got {timeout}"
        )
    return timeout
```

Delete the `blackbox_timeout_truncated` event and warning. Keep module-level `json`, `logging`, and `replace` imports if other code in `scheduler/executor.py` still uses them.

Update these signatures to accept `int | None` for the Blackbox branch only:

```python
def run_configured_scheme(..., timeout_sec: int | None, ...) -> list[PredictionRecord]:
def run_blackbox_scheme_subprocess(..., timeout_sec: int | None, ...) -> list[PredictionRecord]:
```

Before calling `run_scheme_subprocess` in the Native branch, fail if `timeout_sec is None`; the only legal caller is `execute_scheme`, which resolves Native `None` to 600:

```python
if timeout_sec is None:
    raise ValueError("Native execution timeout must be resolved before dispatch")
```

In `run_blackbox_scheme_subprocess`, keep only the conda environment override:

```python
profile = replace(DEFAULT_RUNTIME_PROFILE, conda_env=blackbox_env)
```

and pass the independent deadline in `predict_kwargs` only when present:

```python
if timeout_sec is not None:
    predict_kwargs["timeout_sec"] = timeout_sec
```

Add the optional parameter to `scheduler.blackbox_v2_runner.run_blackbox_predict`:

```python
timeout_sec: float | None = None,
```

and forward it to `execute_blackbox_cli` only when present:

```python
if timeout_sec is not None:
    execute_kwargs["timeout_sec"] = timeout_sec
```

Do not change `run_blackbox_backtest`, `BacktestExecutionBudget`, or `run_blackbox_gray_replay_batch`.

- [ ] **Step 6: Run focused runner and Native regression tests**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q \
  tests/test_blackbox_timeout_drift.py \
  tests/test_blackbox_v2_runner.py \
  tests/test_native_executor.py \
  tests/test_launchd_prediction_runner.py \
  tests/test_signal_gap_fill.py \
  tests/test_signal_gap_fill_cli.py
```

Expected: all selected tests PASS; Blackbox predict is capped by Profile, Native keeps its config timeout, and gray replay/backtest behavior remains unchanged.

- [ ] **Step 7: Commit the execution-authority change**

```bash
git add \
  deploy/blackbox_v2/runtime_profile_v1.json \
  scheduler/executor.py \
  scheduler/blackbox_v2_runner.py \
  tests/test_blackbox_timeout_drift.py \
  tests/test_blackbox_v2_runner.py
git diff --cached --check
git commit -m "fix(runtime): make profile own blackbox timeout"
```

### Task 3: Update the timeout contract documentation

**Files:**
- Modify: `docs/architecture/ARCHITECTURE.md`
- Modify: `docs/architecture/BLACKBOX_V2_PLATFORM.md`
- Modify: `docs/architecture/CODE_ARCHITECTURE.md`
- Modify: `docs/architecture/HARNESS_ARCHITECTURE.md`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`

- [ ] **Step 1: Verify the repository migration is complete and Native declarations remain**

Run:

```bash
test "$(rg -l '^runtime_type:[[:space:]]*blackbox_v2[[:space:]]*$' schemes/*/config.yaml | wc -l | tr -d ' ')" = "39"
test -z "$(for f in $(rg -l '^runtime_type:[[:space:]]*blackbox_v2[[:space:]]*$' schemes/*/config.yaml); do rg -l '^[[:space:]]*timeout_sec:' "$f"; done)"
rg -n '^[[:space:]]*timeout_sec:' schemes/*/config.yaml
```

Expected: first two commands exit 0; final output contains only Native configs that still own their execution budgets.

- [ ] **Step 2: Update architecture and platform SOP wording**

Make these statements explicit and consistent:

```text
Blackbox predict timeout is fixed by deploy/blackbox_v2/runtime_profile_v1.json.
Blackbox config.yaml must not declare schedule.timeout_sec.
An operation deadline can only narrow the profile budget and cannot enlarge it.
Native schedule.timeout_sec remains a scheme-level L3 execution budget.
Blackbox backtest_timeout_sec remains an independent Runtime Profile budget.
Changing the 39 canonical configs creates new exact versions; deployment requires the existing Gate and revision activation workflow and is not authorized by the code merge.
```

Apply that contract at the existing timeout/budget paragraphs in:

- `docs/architecture/ARCHITECTURE.md`: prediction flow reads Native scheme timeout or Blackbox Runtime Profile.
- `docs/architecture/BLACKBOX_V2_PLATFORM.md`: remove the three-source/preflight alignment description and state single authority.
- `docs/architecture/CODE_ARCHITECTURE.md`: split the Native scheme-level row from the Blackbox profile rule.
- `docs/architecture/HARNESS_ARCHITECTURE.md`: replace Blackbox-config budget evidence with Runtime Profile budget and actual duration evidence.
- `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`: add the fail-closed Intake/StaticGate rule and production exact-version boundary near the Runtime Profile section.

Do not add rollout automation, database commands, launchd commands, or compatibility instructions.

- [ ] **Step 3: Run the migrated-repository contract suite**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q \
  tests/test_blackbox_timeout_drift.py \
  tests/test_active_blackbox_conformance.py \
  tests/test_blackbox_v2_discovery.py \
  tests/test_blackbox_v2_intake.py \
  tests/test_blackbox_v2_harness_gates.py
```

Expected: all selected tests PASS, including discovery of all repository Blackbox configs.

- [ ] **Step 4: Commit the documentation update**

```bash
git add \
  docs/architecture/ARCHITECTURE.md \
  docs/architecture/BLACKBOX_V2_PLATFORM.md \
  docs/architecture/CODE_ARCHITECTURE.md \
  docs/architecture/HARNESS_ARCHITECTURE.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md
git diff --cached --check
git commit -m "docs: align blackbox timeout contract"
```

### Task 4: Fresh verification and production-version handoff gate

**Files:**
- No new implementation files
- Verify: all files changed in Tasks 1-3

- [ ] **Step 1: Run whitespace and compilation checks**

Run:

```bash
git diff --check 9fe8b73fee1d2f42d318e3f4cecd72c7cfc0b38d..HEAD
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m compileall -q shared/blackbox_v2 shared scheduler tests
```

Expected: both commands exit 0 with no output.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
conda run --no-capture-output -n bond_factor_lab_service \
python -m pytest -q
```

Expected: all tests and subtests PASS; record exact counts and warnings from this fresh run.

- [ ] **Step 3: Review the exact diff and production boundary**

Run:

```bash
git status --short
git diff --stat 9fe8b73fee1d2f42d318e3f4cecd72c7cfc0b38d..HEAD
git diff --name-status 9fe8b73fee1d2f42d318e3f4cecd72c7cfc0b38d..HEAD
rg -n 'blackbox_timeout_truncated|schedule\.timeout_sec' \
  scheduler shared tests docs/architecture docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md
```

Expected: worktree is clean; the old warning is absent; remaining `schedule.timeout_sec` references are explicitly Native-only or the Blackbox forbidden-field contract; no production operation script or migration was added.

- [ ] **Step 4: Prove the live control plane reads the shared development worktree**

Read the three installed one-shot plists without modifying or reloading them:

```bash
for f in \
  "$HOME/Library/LaunchAgents/com.bond-factor-lab.daily-predictions.plist" \
  "$HOME/Library/LaunchAgents/com.bond-factor-lab.weekly-predictions.plist" \
  "$HOME/Library/LaunchAgents/com.bond-factor-lab.monthly-predictions.plist"
do
  /usr/libexec/PlistBuddy -c 'Print :WorkingDirectory' "$f"
done
```

Expected: all three print `/Users/macstudio0/bond-factor-lab`. Record this as the reason a merge into the current development worktree is a production-visible configuration switch, not a harmless repository-only step.

- [ ] **Step 5: Stop at the production-version gate**

Leave `codex/issue-45-timeout-authority-20260813` fully committed and clean. Do not merge it into `codex/audit-bugfixes-20260613`, do not push it, and do not close #45 yet.

Handoff evidence must include:

```text
The 39 canonical config changes create 39 new exact scheme versions.
Installed prediction LaunchAgents execute from /Users/macstudio0/bond-factor-lab.
Merging before database preparation would expose natural scheduling to unregistered versions and fail closed.
No production DB, Gate side effect, activation, launchd, service, development-branch, remote, or master operation was performed.
```

The separate production migration design must establish an authorized, fail-closed sequence for registering/Gating/activating the new exact versions and switching the shared worktree without creating an execution window where repository configs and active database versions disagree. It may not solve ordering with a compatibility fallback.

- [ ] **Step 6: Record the feature-branch terminal state**

Run:

```bash
git branch --show-current
git status --short
git log --oneline --decorate 9fe8b73fee1d2f42d318e3f4cecd72c7cfc0b38d..HEAD
git rev-parse HEAD
git -C /Users/macstudio0/bond-factor-lab branch --show-current
git -C /Users/macstudio0/bond-factor-lab rev-parse HEAD
git -C /Users/macstudio0/bond-factor-lab status --short
```

Expected: the feature branch is clean and contains the design, plan, implementation, config, and documentation commits; the shared development branch remains exactly at its pre-task HEAD with only the two known unrelated untracked paths. #45 remains open until the separately authorized exact-version migration and development-branch push are actually complete.
