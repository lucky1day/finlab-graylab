# Aliyun ECS Manual-Write Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy an exact Bond Factor Lab C56 release to the Aliyun ECS, run every due scheduling path manually against the ECS candidate database, and leave every systemd timer disabled and inactive.

**Architecture:** Preserve the existing launchd one-shot path and add one parallel, capability-fenced `systemd_one_shot` path. Linux systemd services call the existing DataBridge, prediction, actuals, and backend entry points; prediction writes continue exclusively through `scheduler.repository`. Deployment uses an immutable release directory plus an atomic `current` symlink, then performs read-before/write/read-after acceptance against the ECS clone.

**Tech Stack:** Python 3.12/3.13, pytest, FastAPI/Uvicorn, SQLAlchemy, MySQL 8.0, conda-forge, systemd, SSH, zstd.

---

## File map

- Create `shared/one_shot_control_plane.py`: canonical launchd/systemd control-plane and DataBridge producer values.
- Modify `scheduler/executor.py`: accept only capability-matched launchd or systemd scheduled execution.
- Modify `scheduler/repository.py`: accept only the two canonical scheduled control-plane values before run creation.
- Modify `scheduler/launchd_prediction_runner.py`: extract a private common one-shot execution function while preserving the public launchd behavior.
- Create `scheduler/systemd_prediction_runner.py`: Linux CLI that supplies the systemd-only in-process capability and an optional manual date.
- Modify `scripts/refresh_data_bridge_current.py`: admit exactly the launchd and systemd producer markers.
- Modify `backend/main.py`: report the validated installed control plane in health output.
- Create `deploy/systemd/*.service` and `deploy/systemd/*.timer`: Linux control-plane templates with `Persistent=false`.
- Modify `deploy/README.md`: document the dual-platform one-shot model and disabled-first Linux installation boundary.
- Create `tests/test_systemd_control_plane.py`: executor, repository, runner, DataBridge, health, and unit-template contracts.
- Modify `tests/test_launchd_prediction_runner.py`, `tests/test_repository_registry.py`, and `tests/test_backend_api.py`: focused compatibility and exact-enum coverage.
- Create `docs/operations/ALIYUN_ECS_MANUAL_WRITE_ACCEPTANCE_20260817.md` after execution: sanitized immutable release, DB readback, run IDs, and timer state evidence.

### Task 1: Canonical two-platform control-plane contract

**Files:**
- Create: `shared/one_shot_control_plane.py`
- Modify: `tests/test_repository_registry.py`
- Modify: `scheduler/repository.py`

- [ ] **Step 1: Write repository tests that accept systemd and reject every other value**

Add tests beside the existing scheduled-live run-creation tests:

```python
def test_create_scheme_run_accepts_systemd_one_shot_for_scheduled_live(self) -> None:
    engine = self._run_engine()

    run_id = create_scheme_run(
        engine,
        scheme_id="systemd_scheme",
        predict_date="2026-08-17",
        prediction_phase="scheduled_live",
        scheduled_control_plane="systemd_one_shot",
    )

    self.assertIsInstance(run_id, int)


def test_create_scheme_run_rejects_unknown_scheduled_control_plane(self) -> None:
    with self.assertRaisesRegex(ValueError, "scheduled_control_plane"):
        create_scheme_run(
            self._run_engine(),
            scheme_id="unknown",
            predict_date="2026-08-17",
            prediction_phase="scheduled_live",
            scheduled_control_plane="cron",
        )
```

- [ ] **Step 2: Run the focused tests and confirm the systemd case fails**

Run:

```bash
PYTHONNOUSERSITE=1 pytest -q tests/test_repository_registry.py -k 'systemd_one_shot or unknown_scheduled_control_plane'
```

Expected: the systemd case fails because `create_scheme_run` currently accepts only `launchd_one_shot`; the unknown case passes.

- [ ] **Step 3: Add canonical constants and update repository validation**

Create:

```python
"""Canonical one-shot scheduler identities shared by platform boundaries."""

LAUNCHD_ONE_SHOT_CONTROL_PLANE = "launchd_one_shot"
SYSTEMD_ONE_SHOT_CONTROL_PLANE = "systemd_one_shot"
SCHEDULED_ONE_SHOT_CONTROL_PLANES = frozenset(
    {
        LAUNCHD_ONE_SHOT_CONTROL_PLANE,
        SYSTEMD_ONE_SHOT_CONTROL_PLANE,
    }
)

DATABRIDGE_LAUNCHD_PRODUCER = "launchd-one-shot"
DATABRIDGE_SYSTEMD_PRODUCER = "systemd-one-shot"
DATABRIDGE_ONE_SHOT_PRODUCERS = frozenset(
    {
        DATABRIDGE_LAUNCHD_PRODUCER,
        DATABRIDGE_SYSTEMD_PRODUCER,
    }
)


def require_scheduled_one_shot_control_plane(value: object) -> str:
    """Return a canonical installed control plane or fail closed."""
    normalized = str(value or "").strip()
    if normalized not in SCHEDULED_ONE_SHOT_CONTROL_PLANES:
        raise ValueError("unsupported scheduled one-shot control plane")
    return normalized
```

Import `SCHEDULED_ONE_SHOT_CONTROL_PLANES` in `scheduler/repository.py`, replace the private one-value constant and validate `scheduled_control_plane` against `{None, *SCHEDULED_ONE_SHOT_CONTROL_PLANES}`. A `scheduled_live` run must carry one of the two canonical values; gray-live behavior remains unchanged.

- [ ] **Step 4: Run repository tests**

Run:

```bash
PYTHONNOUSERSITE=1 pytest -q tests/test_repository_registry.py
```

Expected: all repository tests pass.

- [ ] **Step 5: Commit the contract**

```bash
git add shared/one_shot_control_plane.py scheduler/repository.py tests/test_repository_registry.py
git commit -m "feat(scheduler): admit systemd one-shot run identity"
```

### Task 2: Capability-fenced Linux prediction runner

**Files:**
- Modify: `scheduler/executor.py`
- Modify: `scheduler/launchd_prediction_runner.py`
- Create: `scheduler/systemd_prediction_runner.py`
- Create: `tests/test_systemd_control_plane.py`
- Modify: `tests/test_launchd_prediction_runner.py`

- [ ] **Step 1: Write failing executor and systemd-runner tests**

Cover these exact properties:

```python
def test_executor_accepts_only_matching_systemd_context() -> None:
    cfg = _blackbox_config("systemd_candidate")
    context = executor._systemd_scheduled_execution_context()
    with patch.object(executor, "discover_schemes", return_value=[cfg]):
        assert executor.scheduled_live_execution_configuration_error(
            cfg,
            scheduled_control_plane="systemd_one_shot",
            scheduled_execution_context=context,
        ) is None
        assert "requires one-shot execution context" in executor.scheduled_live_execution_configuration_error(
            cfg,
            scheduled_control_plane="systemd_one_shot",
            scheduled_execution_context=executor._launchd_scheduled_execution_context(),
        )


def test_systemd_runner_passes_truthful_control_plane() -> None:
    cfg = _blackbox_config("systemd_candidate")
    engine = Mock()
    with (
        patch.object(systemd_runner.DataBridgeRefreshConfig, "from_env", return_value=object()),
        patch.object(systemd_runner, "_runner_lock", return_value=nullcontext()),
        patch.object(systemd_runner, "discover_schemes", return_value=[cfg]),
        patch.object(systemd_runner, "create_engine_from_env", return_value=engine),
        patch.object(systemd_runner, "get_calendar", return_value=_WeeklyCalendar()),
        patch.object(
            systemd_runner,
            "execute_scheme",
            return_value=SimpleNamespace(
                scheme_id=cfg.scheme_id,
                status="success",
                records_written=1,
                run_id=100,
            ),
        ) as execute_one,
    ):
        summary = systemd_runner.run(
            "weekly",
            predict_date="2026-08-15",
            algo_env="forecast_env",
        )
    assert execute_one.call_args.kwargs["scheduled_control_plane"] == "systemd_one_shot"
    assert summary.to_payload()["event"] == "systemd_prediction_run"
```

Also retain the existing launchd assertion that its public `run()` still passes `launchd_one_shot` and emits `launchd_prediction_run`.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```bash
PYTHONNOUSERSITE=1 pytest -q tests/test_systemd_control_plane.py tests/test_launchd_prediction_runner.py
```

Expected: new systemd tests fail because the capability and module do not yet exist; launchd tests pass.

- [ ] **Step 3: Implement paired opaque capabilities in the executor**

Use the shared constants and two distinct sentinel objects:

```python
_SCHEDULED_EXECUTION_CONTEXTS = {
    LAUNCHD_ONE_SHOT_CONTROL_PLANE: object(),
    SYSTEMD_ONE_SHOT_CONTROL_PLANE: object(),
}


def _launchd_scheduled_execution_context() -> object:
    return _SCHEDULED_EXECUTION_CONTEXTS[LAUNCHD_ONE_SHOT_CONTROL_PLANE]


def _systemd_scheduled_execution_context() -> object:
    return _SCHEDULED_EXECUTION_CONTEXTS[SYSTEMD_ONE_SHOT_CONTROL_PLANE]


def _is_scheduled_execution_context(control_plane: object, context: object) -> bool:
    return (
        control_plane in SCHEDULED_ONE_SHOT_CONTROL_PLANES
        and context is _SCHEDULED_EXECUTION_CONTEXTS[control_plane]
    )
```

Use `_is_scheduled_execution_context` for configuration validation, preflight-failure admission, and the `create_scheme_run` fence. A systemd marker with the launchd sentinel, or vice versa, must fail before DB access.

- [ ] **Step 4: Extract the common runner without changing launchd behavior**

In `scheduler/launchd_prediction_runner.py`:

- Add `event: str = "launchd_prediction_run"` to `LaunchdPredictionSummary` and emit `self.event`.
- Extend `_execute_candidate` with required `scheduled_control_plane` and `scheduled_execution_context` arguments.
- Move the current `run` body into `_run_one_shot`, with those arguments plus `event`.
- Keep public `run` as a thin wrapper passing launchd identity and the launchd sentinel.
- Keep lock path, cadence/date validation, DataBridge gate, candidate ordering, and fail-fast summary semantics unchanged.

Create `scheduler/systemd_prediction_runner.py` as a thin CLI:

```python
"""由 systemd one-shot 启动的单批预测入口。"""

from __future__ import annotations

import argparse
import json
import os
from typing import Sequence

from scheduler.executor import (
    DEFAULT_ALGO_ENV,
    _systemd_scheduled_execution_context,
)
from scheduler.launchd_prediction_runner import (
    VALID_CADENCES,
    LaunchdPredictionConfigurationError,
    LaunchdPredictionSummary,
    _configuration_summary,
    _run_one_shot,
    _today,
)
from shared.one_shot_control_plane import SYSTEMD_ONE_SHOT_CONTROL_PLANE

MANUAL_PREDICT_DATE_ENV = "BFL_SYSTEMD_PREDICT_DATE"


def run(cadence: str, *, predict_date: str, algo_env: str = DEFAULT_ALGO_ENV) -> LaunchdPredictionSummary:
    return _run_one_shot(
        cadence,
        predict_date=predict_date,
        algo_env=algo_env,
        scheduled_control_plane=SYSTEMD_ONE_SHOT_CONTROL_PLANE,
        scheduled_execution_context=_systemd_scheduled_execution_context(),
        event="systemd_prediction_run",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cadence", required=True, choices=sorted(VALID_CADENCES))
    parser.add_argument(
        "--predict-date",
        default=os.getenv(MANUAL_PREDICT_DATE_ENV) or _today(),
    )
    parser.add_argument("--algo-env", default=DEFAULT_ALGO_ENV)
    args = parser.parse_args(argv)
    try:
        summary = run(args.cadence, predict_date=args.predict_date, algo_env=args.algo_env)
    except LaunchdPredictionConfigurationError:
        summary = _configuration_summary(
            args.cadence,
            args.predict_date,
            event="systemd_prediction_run",
        )
    except Exception:
        summary = _configuration_summary(
            args.cadence,
            args.predict_date,
            event="systemd_prediction_run",
        )
    print(json.dumps(summary.to_payload(), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
```

During implementation, avoid the broad tuple shown above by retaining the launchd CLI's two existing ordered exception branches; the code block defines output behavior, not an exception-swallowing expansion.

- [ ] **Step 5: Run focused and executor tests**

Run:

```bash
PYTHONNOUSERSITE=1 pytest -q \
  tests/test_systemd_control_plane.py \
  tests/test_launchd_prediction_runner.py \
  tests/test_native_executor.py \
  tests/test_calendar_coverage_fail_closed.py \
  tests/test_weekly_signal_date.py
```

Expected: all tests pass; launchd output remains unchanged and systemd uses only its own sentinel.

- [ ] **Step 6: Commit the runner**

```bash
git add scheduler/executor.py scheduler/launchd_prediction_runner.py \
  scheduler/systemd_prediction_runner.py tests/test_systemd_control_plane.py \
  tests/test_launchd_prediction_runner.py
git commit -m "feat(scheduler): add systemd prediction one-shot"
```

### Task 3: DataBridge and backend health identity

**Files:**
- Modify: `scripts/refresh_data_bridge_current.py`
- Modify: `backend/main.py`
- Modify: `tests/test_systemd_control_plane.py`
- Modify: `tests/test_data_bridge_current.py`
- Modify: `tests/test_backend_api.py`

- [ ] **Step 1: Add failing exact-enum tests**

Tests must prove:

```python
@pytest.mark.parametrize("producer", ["launchd-one-shot", "systemd-one-shot"])
def test_databridge_publish_accepts_installed_one_shot_producers(producer):
    with patch.dict(os.environ, {entry.DATABRIDGE_PRODUCER_ENV: producer}):
        assert entry.run_command("publish", refresh_date="2026-08-17")[0] != 2


@pytest.mark.parametrize("producer", ["", "cron", "systemd_one_shot", "systemd-one-shot-extra"])
def test_databridge_publish_rejects_every_other_producer(producer):
    with patch.dict(os.environ, {entry.DATABRIDGE_PRODUCER_ENV: producer}):
        code, payload = entry.run_command("publish", refresh_date="2026-08-17")
    assert code == 2
    assert payload["status"] == "configuration_error"
```

Backend health tests set `BOND_FACTOR_LAB_CONTROL_PLANE=systemd_one_shot` and expect that exact mode; an invalid value must fail closed.

- [ ] **Step 2: Run focused tests and confirm systemd fails**

Run:

```bash
PYTHONNOUSERSITE=1 pytest -q tests/test_data_bridge_current.py tests/test_backend_api.py tests/test_systemd_control_plane.py
```

Expected: systemd producer and systemd health-mode tests fail before implementation.

- [ ] **Step 3: Admit exactly two DataBridge producer values**

Keep the existing public launchd constant aliases for compatibility, but validate the environment value against `DATABRIDGE_ONE_SHOT_PRODUCERS`. Both admitted values enable the same controlled continuity bootstrap; unknown or underscore-spelled values return the existing sanitized configuration error before constructing an engine.

- [ ] **Step 4: Validate backend health control plane**

Use:

```python
control_plane = require_scheduled_one_shot_control_plane(
    os.getenv(
        "BOND_FACTOR_LAB_CONTROL_PLANE",
        LAUNCHD_ONE_SHOT_CONTROL_PLANE,
    )
)
```

Return this value in `daily_schedule.mode`. Preserve the existing default so Mac launchd health output and tests remain compatible.

- [ ] **Step 5: Run DataBridge and backend tests**

Run:

```bash
PYTHONNOUSERSITE=1 pytest -q \
  tests/test_data_bridge_current.py \
  tests/test_data_bridge_cross_week_bootstrap.py \
  tests/test_data_bridge_failure_taxonomy.py \
  tests/test_backend_api.py \
  tests/test_systemd_control_plane.py
```

Expected: all tests pass and no output exposes environment values or credentials.

- [ ] **Step 6: Commit DataBridge and health support**

```bash
git add scripts/refresh_data_bridge_current.py backend/main.py \
  tests/test_data_bridge_current.py tests/test_backend_api.py \
  tests/test_systemd_control_plane.py
git commit -m "feat(deploy): report systemd one-shot identity"
```

### Task 4: Disabled-first systemd templates

**Files:**
- Create: `deploy/systemd/bond-factor-lab-backend.service`
- Create: `deploy/systemd/bond-factor-lab-data-bridge.service`
- Create: `deploy/systemd/bond-factor-lab-data-bridge.timer`
- Create: `deploy/systemd/bond-factor-lab-prediction-daily.service`
- Create: `deploy/systemd/bond-factor-lab-prediction-daily.timer`
- Create: `deploy/systemd/bond-factor-lab-prediction-weekly.service`
- Create: `deploy/systemd/bond-factor-lab-prediction-weekly.timer`
- Create: `deploy/systemd/bond-factor-lab-prediction-monthly.service`
- Create: `deploy/systemd/bond-factor-lab-prediction-monthly.timer`
- Create: `deploy/systemd/bond-factor-lab-actuals.service`
- Create: `deploy/systemd/bond-factor-lab-actuals.timer`
- Modify: `deploy/README.md`
- Modify: `tests/test_systemd_control_plane.py`

- [ ] **Step 1: Write a failing template contract test**

The test reads every expected file and asserts:

- all services use `/opt/bond-factor-lab/current` and the absolute service-env Python;
- prediction services call `scheduler.systemd_prediction_runner` with the exact cadence;
- DataBridge sets `BFL_DATABRIDGE_PRODUCER=systemd-one-shot`;
- backend binds only `127.0.0.1:8100` and sets `BOND_FACTOR_LAB_CONTROL_PLANE=systemd_one_shot`;
- every timer contains `Persistent=false`, `RandomizedDelaySec=0`, and its exact `Unit=`;
- schedules are `06:30`, `Mon..Fri 07:03`, `Sat 11:30`, day 15 at `18:00`, and actuals at `08:30/19:00/23:45`, all in `Asia/Shanghai`;
- no service runs `scheduler.main`, APScheduler, ledger, backtest, `systemctl`, or `enable`.

- [ ] **Step 2: Run the contract test and confirm missing-file failure**

Run:

```bash
PYTHONNOUSERSITE=1 pytest -q tests/test_systemd_control_plane.py -k systemd_template
```

Expected: failure naming the first missing unit.

- [ ] **Step 3: Create service templates**

Use this service shape, changing only description and `ExecStart`:

```ini
[Unit]
Description=Bond Factor Lab daily prediction one-shot
After=network-online.target mysql.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/bond-factor-lab/current
EnvironmentFile=/etc/bond-factor-lab/bond-factor-lab.env
EnvironmentFile=-/run/bond-factor-lab/manual-run.env
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=BOND_FACTOR_LAB_CONTROL_PLANE=systemd_one_shot
ExecStart=/opt/miniconda3/envs/bond_factor_lab_service/bin/python -m scheduler.systemd_prediction_runner --cadence daily
TimeoutStartSec=infinity
KillMode=control-group
UMask=0077
```

DataBridge additionally sets `BFL_DATABRIDGE_PRODUCER=systemd-one-shot`. Backend is `Type=simple`, executes the absolute Uvicorn binary with `--host 127.0.0.1 --port 8100`, and uses `Restart=on-failure`. The optional `/run` file is used only for manual date/deadline overrides and is removed after each manual run.

- [ ] **Step 4: Create timer templates**

Use this exact disabled-on-install template shape:

```ini
[Unit]
Description=Bond Factor Lab daily prediction timer

[Timer]
OnCalendar=Mon..Fri *-*-* 07:03:00 Asia/Shanghai
Persistent=false
RandomizedDelaySec=0
Unit=bond-factor-lab-prediction-daily.service

[Install]
WantedBy=timers.target
```

The files declare installability but do not enable themselves. Deployment is responsible for `disable --now` before and after copying them.

- [ ] **Step 5: Document Linux state boundaries**

Update `deploy/README.md` to state that launchd remains the Mac control plane, systemd is the ECS control plane, repository templates are desired state only, installation does not imply enablement, and this candidate stage requires every Linux timer to remain disabled/inactive.

- [ ] **Step 6: Run tests and syntax checks**

Run locally:

```bash
PYTHONNOUSERSITE=1 pytest -q tests/test_systemd_control_plane.py tests/test_launchd_config_drift_audit.py
git diff --check
```

Run later on ECS before installation:

```bash
systemd-analyze verify /opt/bond-factor-lab/current/deploy/systemd/*.service \
  /opt/bond-factor-lab/current/deploy/systemd/*.timer
```

Expected: local tests pass; ECS verifier exits 0.

- [ ] **Step 7: Commit templates**

```bash
git add deploy/systemd deploy/README.md tests/test_systemd_control_plane.py
git commit -m "feat(deploy): add disabled-first systemd units"
```

### Task 5: Full local verification and immutable release

**Files:**
- Verify: all implementation files above

- [ ] **Step 1: Run the C56 and scheduling regression suites**

```bash
PYTHONNOUSERSITE=1 pytest -q \
  tests/test_aliyun_c56_candidate.py \
  tests/test_systemd_control_plane.py \
  tests/test_launchd_prediction_runner.py \
  tests/test_repository_registry.py \
  tests/test_native_executor.py \
  tests/test_data_bridge_current.py \
  tests/test_data_bridge_cross_week_bootstrap.py \
  tests/test_data_bridge_failure_taxonomy.py \
  tests/test_backend_api.py \
  tests/test_calendar_coverage_fail_closed.py \
  tests/test_weekly_signal_date.py \
  tests/test_actuals_runner.py
```

Expected: all selected tests pass. Do not run CompareGate.

- [ ] **Step 2: Run the complete test suite**

```bash
PYTHONNOUSERSITE=1 pytest -q
```

Expected: exit 0. If an unrelated environment-only test cannot run, record the exact test and reason; do not hide it or declare a full pass.

- [ ] **Step 3: Verify the tree and commit any test-only corrections**

```bash
git diff --check
git status --short
git log -5 --oneline
```

Expected: clean worktree after committed corrections; no `outputs/`, reports, caches, secrets, or runtime artifacts tracked.

- [ ] **Step 4: Build the exact archive**

```bash
release_commit=$(git rev-parse HEAD)
git archive --format=tar --prefix=bond-factor-lab/ "$release_commit" \
  | zstd -19 -T0 -o "/tmp/bond-factor-lab-${release_commit}.tar.zst"
shasum -a 256 "/tmp/bond-factor-lab-${release_commit}.tar.zst"
```

Expected: one archive whose filename and recorded SHA-256 identify the exact committed release.

### Task 6: ECS installation and read-only preflight

**Files:**
- Install: `/opt/bond-factor-lab/releases/$release_commit/`, where `release_commit=$(git rev-parse HEAD)` is captured before upload
- Install: `/etc/bond-factor-lab/bond-factor-lab.env`
- Install: `/etc/systemd/system/bond-factor-lab-*`

- [ ] **Step 1: Recreate strict SSH host verification**

Create a private temporary known-hosts file, obtain the current host key, and require its SHA-256 fingerprint to equal `SHA256:ue5OMfnTmotUaVbS/f5cqQ3aGcdDxF5VPfF3rYLpDCs` before any SSH/SCP command. Use `StrictHostKeyChecking=yes`, the explicit key `/Users/macstudio0/.ssh/finlab-key.pem`, and remove the temporary file at the end.

- [ ] **Step 2: Snapshot ECS state without secrets**

Record hostname, OS, CPU, RAM, swap, disk, MySQL service/identity, failed units, current Bond Factor Lab symlink, installed unit hashes, timer enable/active states, listening ports, and existing BondPrediction cron count. Never print DSNs, passwords, private keys, or environment-file content.

- [ ] **Step 3: Upload and install the immutable release**

Upload the exact archive and SHA-256, verify them remotely, extract into a new directory named from the resolved commit, and verify expected files. Do not overwrite an existing release path. Atomically point `/opt/bond-factor-lab/current` to the new directory only after preflight checks pass.

- [ ] **Step 4: Create the root-only environment file in place**

Use a remote root Python process to read the already installed BondPrediction DB configuration in memory and write only the required `BOND_DB_*` keys plus non-secret runtime settings to `/etc/bond-factor-lab/bond-factor-lab.env`. Force host `127.0.0.1`, port `3306`, database `bond_db`, control plane `systemd_one_shot`, absolute conda `PATH`, and DataBridge/cache paths. Read back only owner, mode `0600`, key names, and a password-present boolean.

- [ ] **Step 5: Reverify the three conda environments**

Run `python --version`, `python -m pip check`, and key imports in:

```text
/opt/miniconda3/envs/bond_factor_lab_service
/opt/miniconda3/envs/forecast_env
/opt/miniconda3/envs/forecast_env_blackbox_v1
```

Expected: Python 3.12.13 for service, Python 3.13.12 for both algorithm envs, all `pip check` calls exit 0, and cryptography/SQLAlchemy/PyMySQL plus algorithm imports succeed.

- [ ] **Step 6: Install units while forcing timers off**

Before copying, run `systemctl disable --now` for every Bond Factor Lab timer and tolerate only the normal “unit not installed” result. Verify unit syntax from the release, install exact files into `/etc/systemd/system`, run `systemctl daemon-reload`, and run `systemctl disable --now` again. Read back every timer as `disabled` and `inactive`. Do not call `systemctl enable` or start a timer.

- [ ] **Step 7: Run application and DB read-only preflight**

With the root environment loaded inside a non-echoing root process:

- create a fresh SQLAlchemy/PyMySQL/cryptography connection;
- assert `DATABASE()='bond_db'` and host is local;
- inspect migration manifest without applying migrations;
- strict-discover 65 schemes and assert 56 active / 9 paused, with 60 active targets;
- read current Registry and version/approval state;
- inspect source watermarks and valid daily/weekly/monthly manual dates;
- confirm no Bond Factor Lab runner process exists and no timer is active.

If schema, approval, exact version, input watermark, or timer state fails, stop before any candidate DB write.

### Task 7: Registry sync, manual writes, and acceptance report

**Files:**
- Create after evidence collection: `docs/operations/ALIYUN_ECS_MANUAL_WRITE_ACCEPTANCE_20260817.md`

- [ ] **Step 1: Capture the candidate DB before-image**

Write a sanitized JSON snapshot on ECS containing counts and identities for Registry status, versions/approvals, selected business dates, existing run/prediction keys, actual table watermarks, DataBridge generation, and maximum run IDs. Store no credentials or row-level factor data.

- [ ] **Step 2: Synchronize C56 Registry through repository code**

Run a controlled root Python entry that calls:

```python
engine = create_engine_from_env()
configs = discover_schemes(strict=True)
sync_scheme_registry(engine, configs)
```

Then read back exactly 69 candidate composite identities with 60 active and the exact 9 deferred composites paused. Assert 56 active base IDs and that all active Registry target sets match active configs. On mismatch, stop before predictions.

- [ ] **Step 3: Publish DataBridge manually**

Create `/run/bond-factor-lab/manual-run.env` as root mode `0600` with a same-day manual deadline later than the current time. Start only `bond-factor-lab-data-bridge.service`; wait for completion; read `systemctl show`, journal output, current generation, publication manifest, business digest, refresh date, feature date, and lineage. Remove the manual env file immediately. Do not start its timer.

- [ ] **Step 4: Execute daily predictions and read back every due run**

Set `daily_predict_date` from the read-only preflight's latest covered trading-day result. Write only `BFL_SYSTEMD_PREDICT_DATE=$daily_predict_date` into the temporary root-only manual env file, start `bond-factor-lab-prediction-daily.service`, and monitor without starting the timer. Read the structured summary, every fresh run ID, run log, exact version, active target count, records returned/written, prediction business key, and run linkage. Assert no deferred scheme ran. Remove the manual env file.

- [ ] **Step 5: Execute weekly predictions and read back every due run**

Repeat Step 4 with the selected valid Saturday signal date and `bond-factor-lab-prediction-weekly.service`. Verify all due weekly active schemes and targets, including idempotent upsert linkage when a business key existed.

- [ ] **Step 6: Execute monthly predictions and read back every due run**

Repeat Step 4 with the selected valid natural day 15 and `bond-factor-lab-prediction-monthly.service`. Verify all due monthly active schemes and targets.

- [ ] **Step 7: Execute actuals once and read back updater effects**

Start `bond-factor-lab-actuals.service` for the selected covered date. Read daily, weekly, and monthly actual watermarks and changed business keys. If there was no eligible gap, record a successful idempotent result and prove no out-of-range change; never fabricate source data.

- [ ] **Step 8: Start and health-check the private backend**

Start `bond-factor-lab-backend.service` without enabling it. Verify only `127.0.0.1:8100` listens and `/api/health` reports `status=ok` with `daily_schedule.mode=systemd_one_shot`. Do not configure Nginx, DNS, security groups, or public traffic.

- [ ] **Step 9: Perform terminal safety checks**

Assert:

- all five timers are `disabled` and `inactive`;
- no prediction/DataBridge/actuals one-shot remains active;
- no conda, scheme runner, delivery, or orphan child process remains;
- no new OOM event or unexplained failed unit exists;
- no failed fresh scheme run exists after the captured maximum run ID;
- the exact 9 deferred base schemes have zero fresh runs;
- BondPrediction cron count is unchanged;
- Mac Studio was not contacted or modified by deployment commands.

- [ ] **Step 10: Write and commit the sanitized acceptance report**

The report records exact release commit/archive SHA, three env fingerprints/checks, selected dates, DataBridge generation, 60/9 Registry result, per-cadence due/success/target totals, fresh run-ID ranges, actuals outcome, backend bind/health, timer states, rollback target, and any idempotent overwritten keys. It contains no DSN, credentials, server UUID, private host-key material, full factor rows, or CompareGate claim.

Run:

```bash
git diff --check
git status --short
```

Then commit only the report:

```bash
git add docs/operations/ALIYUN_ECS_MANUAL_WRITE_ACCEPTANCE_20260817.md
git commit -m "docs(ops): record ECS manual-write acceptance"
```

Expected: report evidence supports every completion condition in the approved design while all timers remain off.
