# Blackbox V2 Short Display Names and Four-Scheme Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display the four 1Y T+5 Blackbox schemes by concise business names, update the upstream naming contract, and bring all four schemes to active with certified backtest and gray-live evidence.

**Architecture:** Keep delivered `.py/.json` bytes immutable. Resolve an optional platform-only `display_name` from `config.yaml` into Registry `name` without changing the canonical algorithm version, and make the task-scoped frontend prefer `scheme_name` over the API-wide name that includes a target suffix. Production mutations continue through the existing signed Blackbox gates.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, unittest, native JavaScript, Blackbox V2 Harness, MySQL 8.0, launchd.

---

### Task 1: Add a platform-only Blackbox display-name override

**Files:**
- Modify: `tests/test_blackbox_v2_discovery.py`
- Modify: `harness/contracts/config_schema.py`
- Modify: `scheduler/discovery.py`

- [ ] **Step 1: Write failing discovery and version tests**

Add these tests to `BlackboxV2DiscoveryTests`:

```python
def test_blackbox_display_name_overrides_metadata_without_changing_version(self) -> None:
    from scheduler.discovery import load_scheme_config

    with tempfile.TemporaryDirectory() as tmpdir:
        scheme_dir = _write_blackbox_scheme(Path(tmpdir))
        config_path = scheme_dir / "config.yaml"
        original = load_scheme_config(config_path)
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "scheme_id: trial_10y\n",
                "scheme_id: trial_10y\ndisplay_name: LIQ_EXCESS_A\n",
            ),
            encoding="utf-8",
        )
        displayed = load_scheme_config(config_path)

    self.assertEqual(original.name, "10Y Trial")
    self.assertEqual(displayed.name, "LIQ_EXCESS_A")
    self.assertEqual(displayed.description, "Blackbox V2: 10Y Trial")
    self.assertEqual(original.config_hash, displayed.config_hash)
    self.assertEqual(original.scheme_version, displayed.scheme_version)

def test_blackbox_display_name_must_be_non_empty_when_present(self) -> None:
    from scheduler.discovery import load_scheme_config

    with tempfile.TemporaryDirectory() as tmpdir:
        scheme_dir = _write_blackbox_scheme(Path(tmpdir))
        config_path = scheme_dir / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "scheme_id: trial_10y\n",
                "scheme_id: trial_10y\ndisplay_name: '   '\n",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "display_name"):
            load_scheme_config(config_path)
```

- [ ] **Step 2: Run the tests and verify red**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest \
  tests.test_blackbox_v2_discovery.BlackboxV2DiscoveryTests.test_blackbox_display_name_overrides_metadata_without_changing_version \
  tests.test_blackbox_v2_discovery.BlackboxV2DiscoveryTests.test_blackbox_display_name_must_be_non_empty_when_present -v
```

Expected: the override assertion fails and the blank value is not rejected.

- [ ] **Step 3: Implement validation and resolution**

In `_validate_blackbox_config` add:

```python
display_name = raw.get("display_name")
if display_name is not None and (
    not isinstance(display_name, str) or not display_name.strip()
):
    errors.append("Blackbox V2 display_name must be a non-empty string when present")
```

In `_load_blackbox_config`, resolve the Registry-facing name while retaining the upstream name in the description:

```python
display_name = raw.get("display_name")
resolved_name = (
    str(display_name).strip()
    if isinstance(display_name, str) and display_name.strip()
    else metadata.name
)

return SchemeConfig(
    scheme_id=metadata.scheme_id,
    name=resolved_name,
    description=f"Blackbox V2: {metadata.name}",
    # existing fields unchanged
)
```

Do not add `display_name` to `canonical_platform_config`; it is intentionally excluded from algorithm versioning.

- [ ] **Step 4: Run discovery tests**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest tests.test_blackbox_v2_discovery -v
```

Expected: all discovery tests pass and the two versions remain equal.

- [ ] **Step 5: Commit the platform override**

```bash
git add harness/contracts/config_schema.py scheduler/discovery.py tests/test_blackbox_v2_discovery.py
git commit -m "feat: support blackbox display names"
```

### Task 2: Use the concise name inside task-grid UI

**Files:**
- Modify: `tests/test_frontend_factor_lab.py`
- Modify: `frontend/aifin-shell.js`

- [ ] **Step 1: Write a failing task-grid name test**

Add a frontend hook test whose backtest payload deliberately contains both a short `scheme_name` and a redundant general `display_name`:

```python
def test_task_grid_prefers_scheme_name_without_target_suffix(self) -> None:
    result = _run_factor_lab_hook(
        """
        const payload = {
          target_labels: { "1Y": "1Y国债活跃" },
          schemes: [{
            id: "bb:one_y_t5_liq_excess_a_v1__h5__1Y:source",
            scheme_id: "one_y_t5_liq_excess_a_v1__h5__1Y",
            base_scheme_id: "one_y_t5_liq_excess_a_v1",
            scheme_name: "LIQ_EXCESS_A",
            display_name: "LIQ_EXCESS_A · 1Y国债活跃",
            name: "LIQ_EXCESS_A · 1Y国债活跃",
            target_tenor: "1Y",
            target_label: "1Y国债活跃",
            horizon: 5,
            task_type: "T+5",
            frequency: "daily",
            status: "complete",
            deployed_at: "2026-07-20",
            benchmark_label: "bb",
            data_source_label: "source",
            monthly_metrics: [],
            daily_rows: [{
              predict_date: "2026-07-10", feature_date: "2026-07-10",
              target_date: "2026-07-17", target_tenor: "1Y", horizon: 5,
              predicted_direction: 1, actual_direction: 1, is_correct: true
            }]
          }]
        };
        var tasks = hooks.buildBacktestTaskSchemes(payload);
        return { name: tasks["1Y:T+5"][0].name };
        """
    )
    self.assertEqual(result["name"], "LIQ_EXCESS_A")
```

- [ ] **Step 2: Run the test and verify red**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest \
  tests.test_frontend_factor_lab.FactorLabRankingTests.test_task_grid_prefers_scheme_name_without_target_suffix -v
```

Expected: actual name is `LIQ_EXCESS_A · 1Y国债活跃`.

- [ ] **Step 3: Implement the task-scoped name choice**

In `buildBacktestTaskSchemes`, replace the task object name assignment with:

```javascript
name: String(scheme.scheme_name || getSchemeDisplayName(scheme)),
```

Leave the API-wide `display_name` construction unchanged.

- [ ] **Step 4: Run frontend regression**

Run:

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest tests.test_frontend_factor_lab -v
```

Expected: all frontend factor-lab tests pass.

- [ ] **Step 5: Commit the frontend behavior**

```bash
git add frontend/aifin-shell.js tests/test_frontend_factor_lab.py
git commit -m "fix: shorten task-grid scheme names"
```

### Task 3: Update the upstream name contract and current platform configs

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Modify: `tests/test_onboarding_docs.py`
- Modify: `schemes/one_y_t5_liq_excess_a_v1/config.yaml`
- Modify: `schemes/one_y_t5_liq_excess_a_w252_l7_v1/config.yaml`
- Modify: `schemes/one_y_t5_liq_excess_a_w350_l7_v1/config.yaml`
- Modify: `schemes/one_y_t5_liq_excess_b_w252_l7_v1/config.yaml`
- Modify: `docs/internal/specs/README.md`
- Modify: `docs/internal/plans/README.md`

- [ ] **Step 1: Add a failing documentation contract test**

Add to `OnboardingDocumentationTests`:

```python
def test_upstream_metadata_name_is_task_scoped_and_concise(self) -> None:
    text = UPSTREAM_SOP.read_text(encoding="utf-8")
    self.assertIn('"name": "LIQ_EXCESS_A_W252_L7"', text)
    self.assertIn("不得重复 `target_tenor`", text)
    self.assertIn("不得重复 `task_type`", text)
    self.assertIn("不得追加“方向预测”", text)
    self.assertNotIn('"name": "10年国债收益率周频点位方向预测"', text)
```

- [ ] **Step 2: Run the test and verify red**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest \
  tests.test_onboarding_docs.OnboardingDocumentationTests.test_upstream_metadata_name_is_task_scoped_and_concise -v
```

Expected: the short-name example and prohibitions are missing.

- [ ] **Step 3: Update the SOP example and rules**

Change the Metadata example to:

```json
{
  "schema_version": "1.0",
  "scheme_id": "one_y_t5_liq_excess_a_w252_l7_v1",
  "name": "LIQ_EXCESS_A_W252_L7",
  "algorithm_version": "1.0.0",
  "target_tenor": "1Y",
  "task_type": "T+5",
  "horizon": 5,
  "target_rule": "target_date_yield_vs_feature_date_yield"
}
```

State that `name` is the concise business candidate name within one task grid and must not repeat tenor, task type, horizon, or the phrase “方向预测”.

- [ ] **Step 4: Add exact display names to the four configs**

Immediately after `scheme_id`, add the corresponding line:

```yaml
display_name: LIQ_EXCESS_A
display_name: LIQ_EXCESS_A_W252_L7
display_name: LIQ_EXCESS_A_W350_L7
display_name: LIQ_EXCESS_B_W252_L7
```

Use one line per matching file; do not edit the delivery `.json` files.

- [ ] **Step 5: Register the design and plan in existing document indexes**

Add these links:

```markdown
- [2026-07-20-blackbox-display-name-and-four-scheme-activation-design.md](../specs/2026-07-20-blackbox-display-name-and-four-scheme-activation-design.md)
- [2026-07-20-blackbox-display-name-and-four-scheme-activation.md](2026-07-20-blackbox-display-name-and-four-scheme-activation.md)
```

- [ ] **Step 6: Verify delivery hashes and versions are unchanged**

Run a Python check that loads each config and asserts versions remain:

```python
expected = {
    "one_y_t5_liq_excess_a_v1": "8d583560c9f1",
    "one_y_t5_liq_excess_a_w252_l7_v1": "103c93bbc913",
    "one_y_t5_liq_excess_a_w350_l7_v1": "86b458c568a5",
    "one_y_t5_liq_excess_b_w252_l7_v1": "ba00891cd179",
}
```

Expected: all four match and all script hashes remain `689fe3734e3cdae5524a0e6cf8b22d83e96a40686baba77dd2bbd2eb6e08c6c0`.

- [ ] **Step 7: Run documentation and discovery tests**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest \
  tests.test_onboarding_docs tests.test_blackbox_v2_discovery -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit SOP and current scheme display configuration**

```bash
git add docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md tests/test_onboarding_docs.py \
  schemes/one_y_t5_liq_excess_a_v1/config.yaml \
  schemes/one_y_t5_liq_excess_a_w252_l7_v1/config.yaml \
  schemes/one_y_t5_liq_excess_a_w350_l7_v1/config.yaml \
  schemes/one_y_t5_liq_excess_b_w252_l7_v1/config.yaml \
  docs/internal/specs docs/internal/plans
git commit -m "docs: require concise blackbox names"
```

### Task 4: Run platform regression and fresh certification

**Files:**
- No source files changed.
- Runtime reports remain under ignored `reports/harness/`.

- [ ] **Step 1: Run the complete repository test suite**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Expected: all tests pass; the frozen service environment is not modified.

- [ ] **Step 2: Verify current DataBridge generation**

Confirm `refresh_date=2026-07-20`, daily maximum key `2026-07-17`, environment fingerprint `720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd`, and file/state digests match.

- [ ] **Step 3: Run fresh all-stage for the three paused schemes**

Run separately:

```bash
conda run --no-capture-output -n bond_factor_lab_service python -m harness onboard \
  one_y_t5_liq_excess_a_v1 --predict-date 2026-07-20 --stage all \
  --algo-env forecast_env_blackbox_v1
conda run --no-capture-output -n bond_factor_lab_service python -m harness onboard \
  one_y_t5_liq_excess_a_w350_l7_v1 --predict-date 2026-07-20 --stage all \
  --algo-env forecast_env_blackbox_v1
conda run --no-capture-output -n bond_factor_lab_service python -m harness onboard \
  one_y_t5_liq_excess_b_w252_l7_v1 --predict-date 2026-07-20 --stage all \
  --algo-env forecast_env_blackbox_v1
```

Expected: each run reports seven passed gates and 100 no-persist backtest records. Record each new `harness_run_id`.

### Task 5: Activate the remaining three schemes

**Files:**
- Lifecycle gates update the three `config.yaml` files from `paused + shadow` to `active + active`.

- [ ] **Step 1: Establish zero-write baselines**

Query per-scheme counts in `t_scheme_runs`, `t_scheme_predictions`, `t_scheme_run_log`, `t_backtest_runs`, and `t_backtest_predictions`.

Expected: all five counts are zero for each paused scheme.

- [ ] **Step 2: Issue and consume independent activation tokens**

Use this exact loop. It reads the latest passed all-stage run for the current scheme, issues one token, consumes it once, and then overwrites the shell variables on the next iteration:

```bash
set -euo pipefail
bfl_service_py=/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python
for bfl_sid in \
  one_y_t5_liq_excess_a_v1 \
  one_y_t5_liq_excess_a_w350_l7_v1 \
  one_y_t5_liq_excess_b_w252_l7_v1; do
  case "$bfl_sid" in
    one_y_t5_liq_excess_a_v1) bfl_version=8d583560c9f1 ;;
    one_y_t5_liq_excess_a_w350_l7_v1) bfl_version=86b458c568a5 ;;
    one_y_t5_liq_excess_b_w252_l7_v1) bfl_version=ba00891cd179 ;;
  esac
  bfl_run_id=$("$bfl_service_py" - "$bfl_sid" <<'PY'
import sys
from sqlalchemy import text
from scheduler.repository import create_engine_from_env
with create_engine_from_env().connect() as conn:
    value = conn.execute(text("""
        SELECT harness_run_id FROM t_harness_runs
        WHERE scheme_id=:scheme_id AND stage='all' AND status='passed'
        ORDER BY started_at DESC LIMIT 1
    """), {"scheme_id": sys.argv[1]}).scalar_one()
print(value)
PY
  )
  bfl_token=$("$bfl_service_py" -m harness auth issue \
    --scheme-id "$bfl_sid" --action blackbox_activate \
    --predict-date 2026-07-20 --scheme-version "$bfl_version" \
    --harness-run-id "$bfl_run_id" \
    --issued-by codex-four-scheme-activation-20260720 --expires-in 900)
  "$bfl_service_py" -m harness activate --scheme-id "$bfl_sid" \
    --predict-date 2026-07-20 --project-root /Users/macstudio0/bond-factor-lab \
    --report-dir "/tmp/bfl-four-active-$bfl_sid/activate" \
    --authorize "$bfl_token"
done
```

- [ ] **Step 3: Verify lifecycle state after each activation**

Expected per scheme: config `active + active`, exact version `active` with approval fields, composite Registry `active`, and business table counts still zero.

### Task 6: Persist backtests and gray-live predictions

**Files:**
- No source files changed.
- MySQL allowed writes only through BacktestGate and LiveGate.

- [ ] **Step 1: Persist exactly 100 backtest predictions per newly active scheme**

Issue and consume a new `backtest_persist` token inside this complete per-scheme loop:

```bash
set -euo pipefail
bfl_service_py=/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python
for bfl_sid in \
  one_y_t5_liq_excess_a_v1 \
  one_y_t5_liq_excess_a_w350_l7_v1 \
  one_y_t5_liq_excess_b_w252_l7_v1; do
  case "$bfl_sid" in
    one_y_t5_liq_excess_a_v1) bfl_version=8d583560c9f1 ;;
    one_y_t5_liq_excess_a_w350_l7_v1) bfl_version=86b458c568a5 ;;
    one_y_t5_liq_excess_b_w252_l7_v1) bfl_version=ba00891cd179 ;;
  esac
  bfl_run_id=$("$bfl_service_py" - "$bfl_sid" <<'PY'
import sys
from sqlalchemy import text
from scheduler.repository import create_engine_from_env
with create_engine_from_env().connect() as conn:
    value = conn.execute(text("""
        SELECT harness_run_id FROM t_harness_runs
        WHERE scheme_id=:scheme_id AND stage='all' AND status='passed'
        ORDER BY started_at DESC LIMIT 1
    """), {"scheme_id": sys.argv[1]}).scalar_one()
print(value)
PY
  )
  bfl_token=$("$bfl_service_py" -m harness auth issue \
    --scheme-id "$bfl_sid" --action backtest_persist \
    --predict-date 2026-07-20 --scheme-version "$bfl_version" \
    --harness-run-id "$bfl_run_id" \
    --issued-by codex-four-scheme-backtest-20260720 --expires-in 900)
  "$bfl_service_py" -m harness gate backtest --scheme-id "$bfl_sid" \
    --predict-date 2026-07-20 --project-root /Users/macstudio0/bond-factor-lab \
    --report-dir "/tmp/bfl-four-active-$bfl_sid/persist" \
    --algo-env forecast_env_blackbox_v1 --persist --sample-size 100 \
    --authorize "$bfl_token"
done
```

Expected per scheme: delta `1 backtest run + 100 predictions + nonzero monthly metrics`, with current-snapshot as-of replay semantics.

- [ ] **Step 2: Write one gray-live prediction per newly active scheme**

Issue a separate `live_write` token and consume it once inside this complete per-scheme loop:

```bash
set -euo pipefail
bfl_service_py=/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python
for bfl_sid in \
  one_y_t5_liq_excess_a_v1 \
  one_y_t5_liq_excess_a_w350_l7_v1 \
  one_y_t5_liq_excess_b_w252_l7_v1; do
  case "$bfl_sid" in
    one_y_t5_liq_excess_a_v1) bfl_version=8d583560c9f1 ;;
    one_y_t5_liq_excess_a_w350_l7_v1) bfl_version=86b458c568a5 ;;
    one_y_t5_liq_excess_b_w252_l7_v1) bfl_version=ba00891cd179 ;;
  esac
  bfl_run_id=$("$bfl_service_py" - "$bfl_sid" <<'PY'
import sys
from sqlalchemy import text
from scheduler.repository import create_engine_from_env
with create_engine_from_env().connect() as conn:
    value = conn.execute(text("""
        SELECT harness_run_id FROM t_harness_runs
        WHERE scheme_id=:scheme_id AND stage='all' AND status='passed'
        ORDER BY started_at DESC LIMIT 1
    """), {"scheme_id": sys.argv[1]}).scalar_one()
print(value)
PY
  )
  bfl_token=$("$bfl_service_py" -m harness auth issue \
    --scheme-id "$bfl_sid" --action live_write \
    --predict-date 2026-07-20 --scheme-version "$bfl_version" \
    --harness-run-id "$bfl_run_id" \
    --issued-by codex-four-scheme-gray-live-20260720 --expires-in 900)
  "$bfl_service_py" -m harness gate live --scheme-id "$bfl_sid" \
    --predict-date 2026-07-20 --project-root /Users/macstudio0/bond-factor-lab \
    --report-dir "/tmp/bfl-four-active-$bfl_sid/gray" \
    --algo-env forecast_env_blackbox_v1 --prediction-phase gray_live \
    --authorize "$bfl_token"
done
```

Expected per scheme: exactly one run, one prediction, and one run log; all protected non-business tables have zero delta.

- [ ] **Step 3: Synchronize the Canary short name through the official Registry path**

Load all four configs and call `sync_scheme_registry(engine, configs)`. Expected: lifecycle states remain active and only Registry names/descriptions change; no prediction or backtest counts change.

### Task 7: Verify four candidates and close evidence

**Files:**
- Modify: `docs/blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md`
- Modify: `docs/blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json`
- Modify: `docs/blackbox_v2/records/ONBOARDING_TRIAL_LEDGER.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/blackbox_v2/PRODUCTION_READINESS.md`
- Create: `docs/blackbox_v2/records/screenshots/production-gray-1y-t5-20260720-four-active.png`

- [ ] **Step 1: Restart backend only**

```bash
launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.backend
```

Expected: backend PID changes, `/api/health` returns 200, scheduler PID remains unchanged, and no startup catchup runs.

- [ ] **Step 2: Verify database and APIs**

Expected:

- four config/version/Registry rows are active;
- each scheme has 100 persisted backtest predictions and at least one gray-live row;
- `/api/schemes` includes exactly the selected four and excludes the rejected four;
- each metrics endpoint reports `1Y`, `T+5`, horizon 5 and pending actual;
- each factor-lab backtest entry has 100 daily rows and nonempty monthly metrics.

- [ ] **Step 3: Verify frontend and public access**

Open the factor lab, select `1Y国债活跃 × T+5`, and verify the candidate names are exactly:

```text
LIQ_EXCESS_A
LIQ_EXCESS_A_W252_L7
LIQ_EXCESS_A_W350_L7
LIQ_EXCESS_B_W252_L7
```

Capture the full candidate-list screenshot, verify console errors are zero, and run:

```bash
bash scripts/check_public_access.sh https://bond.finailab.cn/bond-factor-lab
```

Expected: public matrix 14/14 passes.

- [ ] **Step 4: Update evidence and status documents**

Record exact versions, all-stage runs, authorization token hashes, backtest/live run IDs, database deltas, short-name behavior, screenshot digest, backend/scheduler PIDs, API results, and the remaining scheduled-live/actual time gates.

- [ ] **Step 5: Run final verification**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m json.tool \
  docs/blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.evidence.json >/dev/null
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

Expected: JSON valid, all tests pass, and no whitespace errors.

- [ ] **Step 6: Commit the activation closure**

Stage only the three lifecycle config changes, evidence/status documents, and final screenshot. Do not stage the existing unrelated plist or `reports/production-gray-20260720/`.

```bash
git commit -m "feat: activate four 1y t5 schemes"
```

Do not merge or push `master`.
