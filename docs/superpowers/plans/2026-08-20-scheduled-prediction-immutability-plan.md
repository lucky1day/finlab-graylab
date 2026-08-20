# Scheduled Live Prediction Immutability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every live prediction publication insert-only so repeated runs cannot alter an existing `(scheme_id, target_tenor, horizon, target_date)` row.

**Architecture:** Keep the guarantee in `scheduler.repository`, the sole prediction writer. After existing lifecycle, Registry, run, and record validation has succeeded, classify the returned business-key set as none/all/partial inside the current transaction; publish with plain INSERT, complete as benign `skipped`, or complete as zero-write `failed`. Propagate the stable result through the existing executor and one-shot summary without adding a schema, service, ledger, or early scheduler preflight.

**Tech Stack:** Python 3.12, SQLAlchemy, MySQL 8.0 production semantics, SQLite/test repository fakes, unittest/pytest, launchd/systemd shared one-shot runner.

---

### Task 1: Lock the repository behavior with RED contract tests

**Files:**
- Modify: `tests/test_repository_registry.py:142-230`
- Modify: `tests/test_repository_registry.py:1358-1408`
- Modify: `tests/test_repository_registry.py:1758-1825`
- Test: `tests/test_repository_registry.py`

- [ ] **Step 1: Teach the transactional fake to read exact prediction business keys**

Add this branch to `_AtomicConnection.execute` before the INSERT branch so the tests exercise the same four-field identity as MySQL:

```python
        if (
            sql_text.lstrip().startswith("SELECT")
            and "FROM t_scheme_predictions" in sql_text
            and "scheme_id = :scheme_id" in sql_text
            and "target_tenor = :target_tenor" in sql_text
            and "horizon = :horizon" in sql_text
            and "target_date = :target_date" in sql_text
        ):
            self._store.setdefault("calls", []).append((sql_text, rows))
            matches = [
                row
                for row in self._store.get("prediction_rows", [])
                if row["scheme_id"] == rows["scheme_id"]
                and row["target_tenor"] == rows["target_tenor"]
                and int(row["horizon"]) == int(rows["horizon"])
                and str(row["target_date"]) == str(rows["target_date"])
            ]
            return _MappingResult(matches)
```

- [ ] **Step 2: Replace the obsolete Native UPSERT assertion with a plain-INSERT assertion**

Rename `test_active_native_completion_upserts_prediction_and_finishes_atomically` to `test_active_native_completion_inserts_prediction_and_finishes_atomically` and assert:

```python
        prediction_sql, rows = _call_for(
            engine.store,
            "INSERT INTO t_scheme_predictions",
        )
        self.assertNotIn("ON DUPLICATE KEY UPDATE", prediction_sql)
        self.assertNotIn("ON CONFLICT", prediction_sql)
        self.assertEqual((status, written, error_message), ("success", 1, None))
```

- [ ] **Step 3: Add full-duplicate immutability tests for Native and Blackbox**

For each runtime, preload `engine.store["prediction_rows"]` with an original row containing a different `run_id`, direction, confidence, version, phase, and `extra`; execute a new completion for the same four-field key and assert:

```python
        original_predictions = deepcopy(engine.store["prediction_rows"])
        status, written, error_message = complete_active_native_run(...)

        self.assertEqual(
            (status, written, error_message),
            ("skipped", 0, "prediction_keys_already_exist"),
        )
        self.assertEqual(engine.store["prediction_rows"], original_predictions)
        self.assertEqual(engine.store["run_row"]["status"], "skipped")
        self.assertEqual(engine.store["run_row"]["records_written"], 0)
        self.assertEqual(engine.store["run_log_rows"][0]["status"], "skipped")
```

Use the same assertions for `complete_approved_blackbox_run`; its expected return becomes the same three-element tuple.

- [ ] **Step 4: Add partial-conflict zero-write tests for Native and Blackbox**

Configure each test scheme with `tenors=["5Y", "10Y"]`, matching active Registry rows and `records_expected=2`. Preload only the `5Y` prediction, return both records, and assert:

```python
        original_predictions = deepcopy(engine.store["prediction_rows"])
        status, written, error_message = complete_active_native_run(...)

        self.assertEqual(status, "failed")
        self.assertEqual(written, 0)
        self.assertTrue(error_message.startswith("partial_prediction_key_conflict"))
        self.assertEqual(engine.store["prediction_rows"], original_predictions)
        self.assertEqual(engine.store["run_row"]["status"], "failed")
        self.assertEqual(engine.store["run_log_rows"][0]["status"], "failed")
```

Repeat for Blackbox and assert the missing `10Y` key was not inserted.

- [ ] **Step 5: Run the new tests and capture the expected RED result**

Run:

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q tests/test_repository_registry.py \
  -k 'completion and (duplicate or conflict or inserts_prediction)'
```

Expected: the new duplicate/partial tests fail because current completion still UPSERTs and Blackbox still returns only an integer. The existing first-publication test may also fail on the obsolete UPSERT assertion until Step 2 is applied.

---

### Task 2: Implement one insert-only repository decision for both runtimes

**Files:**
- Modify: `scheduler/repository.py:2217-2566`
- Modify: `scheduler/repository.py:2651-2661`
- Modify: `scheduler/repository.py:3349-3386`
- Modify: `scheduler/repository.py:3516-3600`
- Test: `tests/test_repository_registry.py`

- [ ] **Step 1: Add stable reason constants and the shared decision helper**

Add repository-level constants:

```python
PREDICTION_KEYS_ALREADY_EXIST = "prediction_keys_already_exist"
PARTIAL_PREDICTION_KEY_CONFLICT = "partial_prediction_key_conflict"
```

Add one private helper near the existing gray-gap business-key query:

```python
def _prediction_write_decision_conn(
    conn: Connection,
    records: Iterable[PredictionRecord],
) -> tuple[str, str | None]:
    keys = sorted(
        {
            (
                str(record.scheme_id),
                str(record.target_tenor),
                int(record.horizon),
                str(record.target_date),
            )
            for record in records
        }
    )
    existing: list[tuple[str, str, int, str]] = []
    for scheme_id, target_tenor, horizon, target_date in keys:
        row = _select_mapping_one_or_none(
            conn,
            """
            SELECT id, run_id
            FROM t_scheme_predictions
            WHERE scheme_id = :scheme_id
              AND target_tenor = :target_tenor
              AND horizon = :horizon
              AND target_date = :target_date
            """,
            {
                "scheme_id": scheme_id,
                "target_tenor": target_tenor,
                "horizon": horizon,
                "target_date": target_date,
            },
            for_update=True,
        )
        if row is not None:
            existing.append((scheme_id, target_tenor, horizon, target_date))

    if not existing:
        return "success", None
    if len(existing) == len(keys):
        return "skipped", PREDICTION_KEYS_ALREADY_EXIST
    missing = [key for key in keys if key not in set(existing)]
    return (
        "failed",
        f"{PARTIAL_PREDICTION_KEY_CONFLICT}: "
        f"existing={existing}, missing={missing}",
    )
```

Keep the helper private and tuple-based; do not introduce a dataclass or service object for this one decision.

- [ ] **Step 2: Make the prediction INSERT unconditionally insert-only**

Remove `insert_only: bool = False` from `_insert_run_predictions_conn`, delete both UPSERT suffixes, and retain one dialect-aware INSERT solely for the JSON expression:

```python
def _insert_run_predictions_conn(
    conn: Connection,
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None,
) -> int:
    """在调用方事务中 insert-only 写入预测。"""
    sqlite = _dialect_name(conn) == "sqlite"
    extra_expression = ":extra" if sqlite else "CAST(:extra AS JSON)"
    statement = """
        INSERT INTO t_scheme_predictions
            (run_id, scheme_version, scheme_id, target_tenor, horizon,
             predict_date, feature_date, target_date, prediction_phase,
             predicted_direction, confidence, model_version, extra)
        VALUES
            (:run_id, :scheme_version, :scheme_id, :target_tenor, :horizon,
             :predict_date, :feature_date, :target_date, :prediction_phase,
             :predicted_direction, :confidence, :model_version,
             {extra_expression})
    """.format(extra_expression=extra_expression)
```

Remove the obsolete `insert_only=True` argument from `complete_gray_gap_run`; its existing `_assert_gray_gap_business_keys_absent` remains unchanged.

- [ ] **Step 3: Apply the decision in Native completion**

After all current record validation and before INSERT:

```python
        status, error_message = _prediction_write_decision_conn(
            conn,
            record_list,
        )
        records_written = 0
        if status == "success":
            records_written = _insert_run_predictions_conn(
                conn,
                int(run_id),
                record_list,
                scheme_version=exact_scheme_version,
            )
            if records_written != len(expected_targets):
                raise RuntimeError(
                    "Native completion records_written mismatch: "
                    f"expected={len(expected_targets)}, "
                    f"written={records_written}"
                )
```

Use the resulting `status`, `records_written`, and `error_message` in the existing `_finish_scheme_run_conn`, `_write_run_log_conn`, and return tuple. Do not retain the old UPSERT-derived `partial` branch.

- [ ] **Step 4: Apply the same decision in Blackbox completion**

Change the return type to `tuple[str, int, str | None]`, remove `insert_only_predictions`, and use the same decision code. Only execute `precommit_validator` on the `success` branch after the plain INSERT and run/log updates:

```python
        status, error_message = _prediction_write_decision_conn(conn, record_list)
        records_written = 0
        if status == "success":
            records_written = _insert_run_predictions_conn(
                conn,
                int(run_id),
                record_list,
                scheme_version=exact_scheme_version,
            )
            if records_written != records_returned:
                raise RuntimeError(...)

        _finish_scheme_run_conn(..., status=status, ...)
        _write_run_log_conn(..., status, ..., error_message, run_id)
        if status == "success" and precommit_validator is not None:
            precommit_validator(conn)
        return status, records_written, error_message
```

- [ ] **Step 5: Run the repository suite**

Run:

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q tests/test_repository_registry.py
```

Expected: all repository tests pass; the first-publication SQL contains no prediction UPDATE clause; Native and Blackbox duplicate/partial contracts pass; gray-gap tests remain green.

- [ ] **Step 6: Commit the repository boundary**

```bash
git add scheduler/repository.py tests/test_repository_registry.py
git diff --cached --check
git commit -m "fix(repository): make live predictions immutable"
```

---

### Task 3: Propagate duplicate outcomes through executor and one-shot control planes

**Files:**
- Modify: `scheduler/executor.py:1157-1186`
- Modify: `scheduler/launchd_prediction_runner.py:21-33`
- Modify: `scheduler/launchd_prediction_runner.py:187-257`
- Modify: `tests/test_launchd_prediction_runner.py`
- Test: `tests/test_systemd_control_plane.py`

- [ ] **Step 1: Add RED one-shot summary tests**

Call `_execute_candidate` with a mocked `execute_scheme` result and then `_finalize`:

```python
    def test_duplicate_prediction_skip_is_benign_but_visible(self) -> None:
        from scheduler import launchd_prediction_runner as runner

        summary = runner.LaunchdPredictionSummary("daily", "2026-08-20")
        cfg = _blackbox_config("duplicate")
        result = SimpleNamespace(
            scheme_id=cfg.scheme_id,
            status="skipped",
            records_written=0,
            error_msg="prediction_keys_already_exist",
            run_id=101,
        )
        with patch.object(runner, "execute_scheme", return_value=result):
            runner._execute_candidate(
                summary,
                cfg,
                predict_date="2026-08-20",
                algo_env="forecast_env",
                scheduled_control_plane="launchd_one_shot",
                scheduled_execution_context=object(),
            )
        runner._finalize(summary, configuration_error=False)

        self.assertEqual(
            summary.skipped,
            [{"scheme_id": "duplicate", "code": "prediction_keys_already_exist"}],
        )
        self.assertEqual((summary.outcome, summary.exit_code), ("success", 0))
```

Add a companion test with `error_msg="activation_not_approved"` and assert `outcome="partial"`, `exit_code=1`, proving only the approved duplicate reason is benign.

- [ ] **Step 2: Propagate the Blackbox completion tuple in the executor**

Replace the assumed Blackbox success result with:

```python
            status, records_written, error_msg = complete_approved_blackbox_run(
                engine,
                cfg,
                run_id=run_id,
                records=records,
                scheme_version=scheme_version,
                records_returned=records_returned,
                run_date=predict_date,
                duration_sec=duration,
                precommit_validator=blackbox_precommit_validator,
            )
            return SchemeRunResult(
                cfg.scheme_id,
                status,
                records_written,
                duration,
                error_msg,
                run_id,
            )
```

Delete the historical-snapshot `insert_only_predictions` switch because every live path is now insert-only.

- [ ] **Step 3: Preserve the stable skip reason in the one-shot summary**

Import `PREDICTION_KEYS_ALREADY_EXIST` from `scheduler.repository`. In `_execute_candidate` use the returned reason instead of collapsing every skip to `execution_skipped`:

```python
    elif status == "skipped":
        code = str(getattr(result, "error_msg", "") or "execution_skipped")
        summary.skipped.append(_candidate_item(cfg, code))
```

In `_finalize`, only actionable skips contribute to a non-zero outcome:

```python
    actionable_skips = any(
        item.get("code") != PREDICTION_KEYS_ALREADY_EXIST
        for item in summary.skipped
    )
    if configuration_error:
        ...
    elif (
        summary.denied
        or summary.blocked
        or actionable_skips
        or summary.failed
    ):
        summary.outcome = "partial"
        summary.exit_code = 1
    else:
        summary.outcome = "success"
        summary.exit_code = 0
```

Do not make every `skipped` status benign.

- [ ] **Step 4: Correct the stale weekly comment**

Update `scheduler/launchd_prediction_runner.py:346-349` so it no longer claims a duplicate week overwrites a prediction. It should say that duplicate weekly business keys are now protected as benign skips, while the calendar applicability check remains necessary to avoid misleading run dates and wasted algorithm execution.

- [ ] **Step 5: Run shared launchd/systemd control-plane tests**

Run:

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q \
  tests/test_launchd_prediction_runner.py \
  tests/test_systemd_control_plane.py
```

Expected: all tests pass; a duplicate-only batch exits 0 on both control planes because systemd reuses the shared runner, while other skipped/failed results remain non-zero.

- [ ] **Step 6: Commit status propagation**

```bash
git add scheduler/executor.py scheduler/launchd_prediction_runner.py \
  tests/test_launchd_prediction_runner.py
git diff --cached --check
git commit -m "fix(scheduler): treat duplicate predictions as benign skips"
```

---

### Task 4: Make long-term documentation match the immutable contract

**Files:**
- Modify: `docs/architecture/PREDICTION_SEMANTICS.md:168`
- Modify: `docs/architecture/CODE_ARCHITECTURE.md:328`
- Modify: `AGENTS.md:147`
- Modify: `CLAUDE.md:147`

- [ ] **Step 1: Document the live publication state machine**

Add under the live date validation section in `PREDICTION_SEMANTICS.md`:

```markdown
`t_scheme_predictions` 的业务键为
`scheme_id + target_tenor + horizon + target_date`，所有 `gray_live` 与
`scheduled_live` 写入均为 insert-only。完整业务键集合已存在时，本次 run
以 `skipped / prediction_keys_already_exist / records_written=0` 收口；仅部分键
存在时整批 `failed` 且零写入。任何事后数据或代码修订都不得更新已发布预测。
```

- [ ] **Step 2: Remove the obsolete architecture claim that prediction UPSERT is idempotent**

Change the prediction write-safety row in `CODE_ARCHITECTURE.md` from UPSERT wording to:

```markdown
| **写库安全** | live prediction insert-only；四字段唯一键拒绝覆盖；完整重复记为 benign skipped，部分冲突整批失败 | repository 单事务 + harness 授权边界 |
```

- [ ] **Step 3: Add the invariant to both root instruction files**

Add the same sentence to the `t_scheme_predictions` database-table bullet in both files:

```markdown
  已发布 live 业务键永久 insert-only：完整重复记 `skipped`，部分重复整批失败，禁止因数据或代码修订覆盖历史预测。
```

- [ ] **Step 4: Verify root instructions remain byte-identical**

Run:

```bash
cmp -s AGENTS.md CLAUDE.md
```

Expected: exit code 0.

- [ ] **Step 5: Commit documentation truthfulness**

```bash
git add AGENTS.md CLAUDE.md \
  docs/architecture/PREDICTION_SEMANTICS.md \
  docs/architecture/CODE_ARCHITECTURE.md
git diff --cached --check
git commit -m "docs: define immutable live prediction writes"
```

---

### Task 5: Verify the complete change without touching production

**Files:**
- Verify only: `scheduler/repository.py`
- Verify only: `scheduler/executor.py`
- Verify only: `scheduler/launchd_prediction_runner.py`
- Verify only: `tests/`
- Verify only: `docs/`

- [ ] **Step 1: Run focused behavioral tests fresh**

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q \
  tests/test_repository_registry.py \
  tests/test_launchd_prediction_runner.py \
  tests/test_systemd_control_plane.py \
  tests/test_signal_gap_fill.py
```

Expected: all tests pass.

- [ ] **Step 2: Compile the changed production modules**

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m compileall -q scheduler shared scripts
```

Expected: exit code 0 with no output.

- [ ] **Step 3: Run the complete local suite**

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q
```

Expected: all tests and subtests pass. Existing release-temp cleanup warnings may remain, but no new warning category is accepted in this change.

- [ ] **Step 4: Audit the final diff and worktree**

```bash
git diff --check HEAD~3..HEAD
git status --short
git log -4 --oneline --decorate
```

Expected: no tracked changes remain. Only the pre-existing untracked `.superpowers/` and unrelated old `docs/superpowers/plans/2026-08-11-hide-missing-signal-prompt-plan.md` may remain outside the exact commits.

- [ ] **Step 5: Stop at the production boundary and report evidence**

Report exact commit SHAs, focused/full test counts, and the resulting state matrix. Do not build/promote a new immutable release, write a production prediction, change an installed plist/unit, restart Mac3, or reload ECS systemd without a separate user-approved production step.
