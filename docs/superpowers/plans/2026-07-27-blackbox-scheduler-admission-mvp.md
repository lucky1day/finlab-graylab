# Blackbox Scheduler Admission MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate Blackbox gray-lab visibility/manual execution from legacy scheduler permission.

**Architecture:** A strict version-controlled exact-identity admission file is loaded by a small scheduler helper. Automatic registration, startup catch-up, and scheduled execution all require `formal`; manual execution and Registry/API visibility do not.

**Tech Stack:** Python 3.12, APScheduler, JSON, unittest/pytest.

---

### Task 1: Exact Blackbox scheduler admission

**Files:**
- Create: `deploy/blackbox_scheduler_admission_v1.json`
- Create: `scheduler/blackbox_scheduler_admission.py`
- Create: `tests/test_blackbox_scheduler_admission.py`

- [ ] **Step 1: Write failing parser and policy tests**

```python
def test_gray_and_unknown_blackbox_are_not_scheduled():
    policy = load_scheduler_admission(path)
    assert policy.mode(gray_cfg) == "gray"
    assert policy.is_scheduled(gray_cfg) is False
    assert policy.is_scheduled(unknown_cfg) is False

def test_exact_formal_identity_is_scheduled_but_version_drift_is_not():
    policy = load_scheduler_admission(path)
    assert policy.is_scheduled(formal_cfg) is True
    assert policy.is_scheduled(replace(formal_cfg, scheme_version="drift")) is False
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q tests/test_blackbox_scheduler_admission.py
```

Expected: collection/import failure because the helper does not exist.

- [ ] **Step 3: Implement the strict helper and control file**

The helper must validate:

```python
SCHEMA_VERSION = "blackbox-scheduler-admission-v1"
VALID_MODES = frozenset({"formal", "gray"})
```

It must reject duplicate identities, empty fields, unsupported modes, and
malformed JSON. Native schemes return admitted; Blackbox identities must match
both scheme ID and version, and only `formal` returns admitted.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Task 1 command and require zero failures.

- [ ] **Step 5: Commit**

```bash
git add deploy/blackbox_scheduler_admission_v1.json scheduler/blackbox_scheduler_admission.py tests/test_blackbox_scheduler_admission.py
git commit -m "feat: add blackbox scheduler admission fence"
```

### Task 2: Enforce automatic-only admission

**Files:**
- Modify: `scheduler/main.py`
- Modify: `tests/test_scheduler_main.py`
- Test: `tests/test_blackbox_scheduler_admission.py`

- [ ] **Step 1: Write failing scheduler-boundary tests**

```python
def test_build_scheduler_excludes_gray_blackbox_but_keeps_formal():
    scheduler = build_scheduler()
    assert scheduler.get_job("predict:formal_v2") is not None
    assert scheduler.get_job("predict:gray_v2") is None

def test_scheduled_wrapper_rejects_gray_before_execution():
    with pytest.raises(SchedulerAdmissionError):
        run_scheduled_prediction_job("gray_v2")
    execute_scheme.assert_not_called()
```

Also prove startup catch-up excludes gray schemes while direct manual execution
continues to use the existing path.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q tests/test_blackbox_scheduler_admission.py tests/test_scheduler_main.py
```

Expected: gray Blackbox is still registered or reaches execution.

- [ ] **Step 3: Add minimal enforcement**

Load the policy once when building scheduler jobs, filter startup catch-up with
the same helper, and reload the policy in `run_scheduled_prediction_job()` as a
revocation fence. Do not change `run_prediction_job()`.

- [ ] **Step 4: Run targeted and related regression**

```bash
/tmp/bfl-test-conda-20260726/bin/python -m pytest -q \
  tests/test_blackbox_scheduler_admission.py \
  tests/test_scheduler_main.py \
  tests/test_scheduler_capacity_admission.py \
  tests/test_scheduler_mode_config.py
```

Expected: zero failures.

- [ ] **Step 5: Commit**

```bash
git add scheduler/main.py tests/test_scheduler_main.py tests/test_blackbox_scheduler_admission.py
git commit -m "fix: deny gray blackbox automatic scheduling"
```
