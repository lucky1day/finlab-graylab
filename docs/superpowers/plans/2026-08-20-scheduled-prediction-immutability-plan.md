# Scheduled Live Prediction Immutability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every live prediction publication insert-only so repeated runs cannot alter an existing `(scheme_id, target_tenor, horizon, target_date)` row. In this key, `t_scheme_predictions.scheme_id` is the base scheme / algorithm execution identity, not the Registry composite `scheme_id`.

**Architecture:** Keep the guarantee in `scheduler.repository`, the sole prediction writer. For ordinary Native/Blackbox active completion, after existing lifecycle, Registry, run, and record validation has succeeded, classify the returned business-key set as none/all/partial inside the current transaction; publish with plain INSERT, complete as benign `skipped`, or complete as zero-write `failed`. Authorized gray-gap retains its stricter rule: any existing authorized key rejects the entire group with zero writes and never becomes benign `skipped`. Propagate the stable ordinary-completion result through the existing executor and one-shot summary without adding a schema, service, ledger, or early scheduler preflight.

**Tech Stack:** Python 3.12, SQLAlchemy, MySQL 8.0 production semantics, SQLite/test repository fakes, unittest/pytest, launchd/systemd shared one-shot runner.

**Execution Mode:** Use subagent-driven implementation with review between tasks under the user's standing preference. Do not pause for an inline-versus-subagent choice; stop only at an independent production or destructive-action authority boundary.

---

### Task 1: Lock the repository behavior with RED contract tests

**Files:**
- Modify: `tests/test_repository_registry.py:142-230`
- Modify: `tests/test_repository_registry.py:1358-1408`
- Modify: `tests/test_repository_registry.py:1758-1825`
- Test: `tests/test_repository_registry.py`

- [x] **Step 1: Teach the transactional fake to read exact prediction business keys**

Add this branch to `_AtomicConnection.execute` before the INSERT branch so the tests exercise the same four-field identity as MySQL:

```python
        compact_sql = " ".join(sql_text.split())
        if (
            compact_sql.startswith(
                "SELECT scheme_id, target_tenor, horizon, target_date "
                "FROM t_scheme_predictions"
            )
            and "WHERE scheme_id = :scheme_id" in compact_sql
            and "AND target_tenor = :target_tenor" in compact_sql
            and "AND horizon = :horizon" in compact_sql
            and "AND target_date = :target_date" in compact_sql
        ):
            self._store.setdefault("calls", []).append((sql_text, rows))
            prediction_rows = [
                {
                    key: row[key]
                    for key in (
                        "scheme_id",
                        "target_tenor",
                        "horizon",
                        "target_date",
                    )
                }
                for row in self._store.get("prediction_rows", [])
                if row["scheme_id"] == rows["scheme_id"]
                and row["target_tenor"] == rows["target_tenor"]
                and int(row["horizon"]) == int(rows["horizon"])
                and str(row["target_date"]) == str(rows["target_date"])
            ]
            return _MappingResult(prediction_rows)
```

- [x] **Step 2: Replace the obsolete Native UPSERT assertion with a plain-INSERT assertion**

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

- [x] **Step 3: Add full-duplicate immutability tests for Native and Blackbox**

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

- [x] **Step 4: Add partial-conflict zero-write tests for Native and Blackbox**

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

- [x] **Step 5: Run the new tests and capture the expected RED result**

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

- [x] **Step 1: Add stable reason constants and the shared decision helper**

Add repository-level constants:

```python
PREDICTION_KEYS_ALREADY_EXIST = "prediction_keys_already_exist"
PARTIAL_PREDICTION_KEY_CONFLICT = "partial_prediction_key_conflict"
```

Add one private helper near the existing gray-gap business-key query:

```python
def _prediction_write_decision_conn(
    conn: Connection,
    prediction_rows: Iterable[Mapping[str, object]],
) -> tuple[str, int, str | None]:
    keys = sorted(
        (
            str(row["scheme_id"]),
            str(row["target_tenor"]),
            int(row["horizon"]),
            str(row["target_date"]),
        )
        for row in prediction_rows
    )
    existing = []
    missing = []
    for key in keys:
        row = _select_mapping_one_or_none(
            conn,
            """
            SELECT scheme_id, target_tenor, horizon, target_date
            FROM t_scheme_predictions
            WHERE scheme_id = :scheme_id
              AND target_tenor = :target_tenor
              AND horizon = :horizon
              AND target_date = :target_date
            """,
            {
                "scheme_id": key[0],
                "target_tenor": key[1],
                "horizon": key[2],
                "target_date": key[3],
            },
            for_update=True,
        )
        (existing if row is not None else missing).append(key)

    if not existing:
        return "success", len(keys), None
    if not missing:
        return "skipped", 0, PREDICTION_KEYS_ALREADY_EXIST

    def format_keys(values: list[tuple[str, str, int, str]]) -> str:
        return "[" + ", ".join(
            f"{scheme_id}/{target_tenor}/h{horizon}/{target_date}"
            for scheme_id, target_tenor, horizon, target_date in values
        ) + "]"

    return (
        "failed",
        0,
        f"{PARTIAL_PREDICTION_KEY_CONFLICT}: "
        f"existing={format_keys(existing)}; missing={format_keys(missing)}",
    )
```

Keep the helper private and tuple-based; do not introduce a dataclass or service object for this one decision.

- [x] **Step 2: Prepare final SQL rows, then make their INSERT unconditionally insert-only**

Normalize and validate records once before conflict classification. The implemented preparation boundary is:

```python
def _prepare_run_prediction_rows(
    run_id: int,
    records: Iterable[PredictionRecord],
    *,
    scheme_version: str | None,
) -> list[dict[str, object]]:
    """将 prediction records 规范化并验证为最终 SQL rows。"""
    rows: list[dict[str, object]] = []
    for record in records:
        row = asdict(record)
        extra = dict(record.extra or {})
        feature_date = record.feature_date or extra.get("feature_date")
        if not feature_date:
            raise ValueError(
                "feature_date is required for prediction record "
                f"{record.scheme_id}/{record.target_tenor}"
            )
        anchor_date = extra.get("anchor_date")
        if anchor_date and str(anchor_date) != str(feature_date):
            raise ValueError(
                "anchor_date must equal feature_date for prediction record "
                f"{record.scheme_id}/{record.target_tenor}"
            )
        phase = record.prediction_phase or extra.get("prediction_phase")
        if phase not in VALID_PREDICTION_PHASES:
            raise ValueError(
                f"prediction_phase must be one of {sorted(VALID_PREDICTION_PHASES)} "
                f"for prediction record {record.scheme_id}/{record.target_tenor}"
            )
        extra["feature_date"] = str(feature_date)
        extra["prediction_phase"] = str(phase)
        row["run_id"] = record.run_id if record.run_id is not None else run_id
        if int(row["run_id"]) != int(run_id):
            raise RuntimeError(
                "prediction record run_id must match the committing run: "
                f"{row['run_id']} != {run_id}"
            )
        row["scheme_version"] = (
            record.scheme_version
            if record.scheme_version is not None
            else scheme_version
        )
        row["feature_date"] = str(feature_date)
        row["prediction_phase"] = str(phase)
        row["extra"] = json.dumps(extra, ensure_ascii=False)
        rows.append(row)
    return rows
```

Remove `insert_only: bool = False` from `_insert_run_predictions_conn`, delete both UPSERT suffixes, and make the insert helper accept only those prepared mappings:

```python
def _insert_run_predictions_conn(
    conn: Connection,
    prediction_rows: Iterable[Mapping[str, object]],
) -> int:
    """在调用方事务中以 plain INSERT 写入已验证的 prediction rows。"""
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
    rows = list(prediction_rows)
    if not rows:
        return 0
    conn.execute(text(statement), rows)
    return len(rows)
```

Remove the obsolete `insert_only=True` argument from `complete_gray_gap_run`; its existing `_assert_gray_gap_business_keys_absent` remains unchanged.

- [x] **Step 3: Apply the decision in Native completion**

After all current record validation and before INSERT:

```python
        prediction_rows = _prepare_run_prediction_rows(
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
        )
        status, records_written, error_message = (
            _prediction_write_decision_conn(conn, prediction_rows)
        )
        if status == "success":
            records_written = _insert_run_predictions_conn(
                conn,
                prediction_rows,
            )
            if records_written != len(expected_targets):
                raise RuntimeError(
                    "Native completion records_written mismatch: "
                    f"expected={len(expected_targets)}, "
                    f"written={records_written}"
                )
```

Use the resulting `status`, `records_written`, and `error_message` in the existing `_finish_scheme_run_conn`, `_write_run_log_conn`, and return tuple. Do not retain the old UPSERT-derived `partial` branch.

- [x] **Step 4: Apply the same decision in Blackbox completion**

Change the return type to `tuple[str, int, str | None]`, remove `insert_only_predictions`, and use the same decision code. Only execute `precommit_validator` on the `success` branch after the plain INSERT and run/log updates:

```python
        prediction_rows = _prepare_run_prediction_rows(
            int(run_id),
            record_list,
            scheme_version=exact_scheme_version,
        )
        status, records_written, error_message = (
            _prediction_write_decision_conn(conn, prediction_rows)
        )
        if status == "success":
            records_written = _insert_run_predictions_conn(
                conn,
                prediction_rows,
            )
            if records_written != records_returned:
                raise RuntimeError(
                    "Blackbox completion records_written mismatch: "
                    f"returned={records_returned}, written={records_written}"
                )
```

The existing run-finish and run-log calls consume that exact three-element result;
`precommit_validator` remains limited to the `success` branch, and the completion
returns `status, records_written, error_message`.

- [x] **Step 5: Run the repository suite**

Run:

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q tests/test_repository_registry.py
```

Expected: all repository tests pass; the first-publication SQL contains no prediction UPDATE clause; Native and Blackbox duplicate/partial contracts pass; gray-gap tests remain green.

- [x] **Step 6: Commit the repository boundary**

```bash
git add scheduler/repository.py tests/test_repository_registry.py
git diff --cached --check
git commit -m "fix(repository): make live predictions immutable"
```

#### Implementation Evidence

This is a concise record from the implementation session, not an independently
persisted command log in the repository. The repository contract tests first moved
from 7 failed to 7 passed. Review of invalid duplicate inputs then produced 8 failed
subtests before the correction. Final session evidence was 65 passed / 73 subtests
for the repository suite and 13 passed for the gap suite. The resulting repository
commit is `62e54de`.

---

### Task 3: Propagate duplicate outcomes through executor and one-shot control planes

**Files:**
- Modify: `scheduler/executor.py:1157-1186`
- Modify: `scheduler/launchd_prediction_runner.py:21-33`
- Modify: `scheduler/launchd_prediction_runner.py:187-257`
- Modify: `tests/test_launchd_prediction_runner.py`
- Test: `tests/test_systemd_control_plane.py`

- [x] **Step 1: Add RED one-shot summary tests**

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

Add companion cases with `error_msg=None` and a sensitive-looking arbitrary detail. Both must emit only `code="execution_skipped"` and assert `outcome="partial"`, `exit_code=1`, proving that only the exact approved duplicate constant is visible and benign; arbitrary `error_msg` values must never be copied into the one-shot summary.

- [x] **Step 2: Propagate the Blackbox completion tuple in the executor**

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

- [x] **Step 3: Allowlist only the stable duplicate reason in the one-shot summary**

Import `PREDICTION_KEYS_ALREADY_EXIST` from `scheduler.repository`. In `_execute_candidate`, expose only that exact constant; collapse every other skipped detail to the generic `execution_skipped` code:

```python
    elif status == "skipped":
        error_msg = getattr(result, "error_msg", None)
        code = (
            PREDICTION_KEYS_ALREADY_EXIST
            if error_msg == PREDICTION_KEYS_ALREADY_EXIST
            else "execution_skipped"
        )
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

- [x] **Step 4: Correct the stale weekly comment**

Update `scheduler/launchd_prediction_runner.py:346-349` so it no longer claims a duplicate week overwrites a prediction. It should say that duplicate weekly business keys are now protected as benign skips, while the calendar applicability check remains necessary to avoid misleading run dates and wasted algorithm execution.

- [x] **Step 5: Run shared launchd/systemd control-plane tests**

Run:

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q \
  tests/test_launchd_prediction_runner.py \
  tests/test_systemd_control_plane.py
```

Expected: all tests pass; a duplicate-only batch exits 0 on both control planes because systemd reuses the shared runner, while other skipped/failed results remain non-zero.

- [x] **Step 6: Commit status propagation**

```bash
git add scheduler/executor.py scheduler/launchd_prediction_runner.py \
  tests/test_launchd_prediction_runner.py
git diff --cached --check
git commit -m "fix(scheduler): treat duplicate predictions as benign skips"
```

#### Implementation Evidence

This is a concise record from the implementation session, not an independently
persisted command log in the repository. The control-plane tests initially had 4
failures; the security allowlist review then exposed 1 failed subtest. Final session
evidence for the shared launchd/systemd control plane was 17 passed / 13 subtests.
The resulting scheduler commit is `bc92501`.

---

### Task 4: Make long-term documentation match the immutable contract

**Files:**
- Modify: `docs/architecture/PREDICTION_SEMANTICS.md:168`
- Modify: `docs/architecture/CODE_ARCHITECTURE.md:328`
- Modify: `AGENTS.md:147`
- Modify: `CLAUDE.md:147`

- [x] **Step 1: Document the live publication state machine**

Add under the live date validation section in `PREDICTION_SEMANTICS.md`:

```markdown
`t_scheme_predictions` 的业务键为
`scheme_id + target_tenor + horizon + target_date`；其中表内 `scheme_id` 是
base scheme / 算法执行身份，不是 Registry composite `scheme_id`。所有
`gray_live` 与 `scheduled_live` 写入均为 insert-only。仅普通 Native/Blackbox
active completion 使用 none/all/partial 三态：完整重复以
`skipped / prediction_keys_already_exist / records_written=0` 收口，部分重复整批
`failed` 且零写入。该 benign `skipped` 是算法执行、records 返回并验证后的
per-scheme publication outcome；one-shot exit `0` 仅表示无 actionable failure，
可同时包含首次发布 success 与 benign duplicate，不能解释为候选未执行。
Authorized gray-gap 任一授权键已存在即整组拒绝并零写入，不得转 benign skipped。
任何事后数据或代码修订都不得更新已发布预测。
```

- [x] **Step 2: Remove the obsolete architecture claim that prediction UPSERT is idempotent**

Change the prediction write-safety row in `CODE_ARCHITECTURE.md` from UPSERT wording to:

```markdown
| **写库安全** | 所有 live prediction insert-only，四字段唯一键拒绝覆盖；仅普通 Native/Blackbox active completion 的完整重复记 benign skipped、部分冲突整批失败 | repository 单事务 + harness 授权边界 |

Authorized gray-gap 任一授权业务键已存在即整组拒绝、零写入，不转 benign skipped。
```

- [x] **Step 3: Add the invariant to both root instruction files**

Add the same sentence to the `t_scheme_predictions` database-table bullet in both files:

```markdown
  所有已发布 live 业务键永久 insert-only、禁止覆盖。仅普通 Native/Blackbox active completion 使用三态：完整重复记 benign `skipped`，部分重复整批失败；authorized gray-gap 任一键已存在即整组拒绝、`records_written=0`，不得转为 benign `skipped`。数据或代码修订不得覆盖历史预测。
```

- [x] **Step 4: Verify root instructions remain byte-identical**

Run:

```bash
cmp -s AGENTS.md CLAUDE.md
```

Expected: exit code 0.

- [x] **Step 5: Commit documentation truthfulness**

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

- [x] **Step 1: Run focused behavioral tests fresh**

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q \
  tests/test_repository_registry.py \
  tests/test_launchd_prediction_runner.py \
  tests/test_systemd_control_plane.py \
  tests/test_signal_gap_fill.py
```

Expected: all tests pass.

- [x] **Step 2: Compile the changed production modules**

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m compileall -q scheduler shared scripts
```

Expected: exit code 0 with no output.

- [x] **Step 3: Run the complete local suite**

```bash
/Users/macstudio0/miniconda3/bin/conda run -n bond_factor_lab_service \
  python -m pytest -q
```

Expected: all tests and subtests pass. Existing release-temp cleanup warnings may remain, but no new warning category is accepted in this change.

- [x] **Step 4: Audit the final diff and worktree**

```bash
git diff --check HEAD~3..HEAD
git status --short
git log -4 --oneline --decorate
```

Expected: no tracked changes remain. Only the pre-existing untracked `.superpowers/` and unrelated old `docs/superpowers/plans/2026-08-11-hide-missing-signal-prompt-plan.md` may remain outside the exact commits.

- [x] **Step 5: Stop at the production boundary and report evidence**

Report exact commit SHAs, focused/full test counts, and the resulting state matrix. Do not build/promote a new immutable release, write a production prediction, change an installed plist/unit, restart Mac3, or reload ECS systemd without a separate user-approved production step.

#### Final Verification Evidence

The following is fresh independent verification evidence for exact HEAD
`9539c2a5f81bbc22a79ee98dcbd821a98ada637d`, not a test rerun performed while
editing this plan:

- Focused scope (`test_repository_registry`, `test_launchd_prediction_runner`,
  `test_systemd_control_plane`, and `test_signal_gap_fill`): exit `0`, 95 passed
  plus 86 subtests, 30 warnings, 0.67s.
- Compile scope (`scheduler`, `shared`, and `scripts`): `compileall` exit `0`.
- Full local suite: exit `0`, 968 passed plus 486 subtests, 76 warnings, 55.49s.
- Warning classification: 3 existing invalid-escape `SyntaxWarning`, 43 Python
  3.12 sqlite/SQLAlchemy `DeprecationWarning`, and 30 pytest temporary-directory
  `rm_rf` warnings.
- Documentation/worktree checks: root `AGENTS.md` / `CLAUDE.md` comparison and
  diff check both exited `0`; status contained only the pre-existing untracked
  `.superpowers/` and
  `docs/superpowers/plans/2026-08-11-hide-missing-signal-prompt-plan.md`.
- Production boundary: verification did not deploy or promote a release, write a
  production prediction, modify or reload an installed plist/unit, restart Mac3,
  reload ECS systemd, or otherwise touch production.
