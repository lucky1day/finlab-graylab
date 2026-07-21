# DataBridge V1 Additive Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `data-bridge-v1` 在保持 Contract 1.0 和 Schema 版本不变的前提下接受每日新增业务列，并把同一规则同步到 Blackbox V2 上游 SOP、数据样例说明和自动化测试。

**Architecture:** `shared/blackbox_v2/data_bridge_v1_schema.json` 从“完整固定表头”重新解释为“最低兼容字段基线”。三份 CSV 的时间键必须位于第一列；基线字段必须全部存在且相对顺序不变；额外业务列允许出现，仍需通过有限数值或空值校验，并进入 DataBridge business digest、文件 SHA256 和 Blackbox snapshot identity。同一轮日频分块仍要求实际表头完全一致，避免一个 generation 混用不同导出结构。

**Tech Stack:** Python 3.12/3.13、pandas、unittest、Markdown、JSON

---

### Task 1: Lock the additive schema policy with failing tests

**Files:**
- Modify: `tests/test_data_bridge_validation.py`
- Modify: `tests/test_blackbox_v2_snapshot.py`
- Modify: `tests/test_blackbox_v2_input_artifacts.py`

- [ ] **Step 1: Add a DataBridge validation test for additive columns**

Add a test that keeps the schema baseline at `date/week_id/month_id + factor`, appends a numeric `new_factor` to the three actual frames, and asserts validation succeeds, the reported column count reflects the actual CSV, and changing `new_factor` changes `business_digest`.

- [ ] **Step 2: Add fail-closed compatibility tests**

Add tests proving that a missing baseline field, an empty or wrong-key-first baseline, a changed baseline relative order, a duplicate raw CSV header, a duplicate actual DataFrame column name, a time key that is not first, or a nonnumeric value in an added column is rejected.

- [ ] **Step 3: Add a snapshot identity test**

Add a test that passes baseline columns separately from actual frames, appends `new_factor`, and asserts the snapshot manifest includes it and the snapshot ID differs from the baseline-only snapshot.

- [ ] **Step 4: Remove fixed-count assertions from the default-schema test**

Replace `774/575/123` assertions with checks that each baseline is non-empty, starts with the correct time key, and contains no duplicate field names.

- [ ] **Step 5: Run focused tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_data_bridge_validation \
    tests.test_blackbox_v2_snapshot \
    tests.test_blackbox_v2_input_artifacts -v
```

Expected: additive-column tests fail because both validators still require exact full-header equality.

### Task 2: Implement baseline-compatible validation

**Files:**
- Modify: `shared/data_bridge/validation.py`
- Modify: `shared/blackbox_v2/snapshot.py`

- [ ] **Step 1: Add a shared compatibility validator**

Implement a helper that receives `filename`, actual columns, and baseline columns. It must reject duplicate actual columns, require the declared time key to be first, require every baseline column, and require baseline positions to be strictly increasing. It must not restrict the number or names of added business columns.

Before each platform `pandas.read_csv`, parse the raw CSV header and reject duplicate names so pandas cannot silently rewrite a duplicate as an apparent additive column.

- [ ] **Step 2: Use the helper in DataBridge validation**

Replace exact list equality in `validate_dataset`. Continue validating every actual non-key column as finite numeric or empty. Keep every actual column in the dataset digest and file profile.

Keep exact actual-header equality across chunks within one daily export round, but use a collision-free temporary merge key so a legitimate added business column such as `__normalized_date` cannot be overwritten or dropped.

- [ ] **Step 3: Use the helper in snapshot validation**

Replace exact list equality in `_validate_frame_set`. Keep the actual columns in the manifest and therefore in the content-addressed snapshot identity.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Task 1 command again. Expected: all focused tests pass.

### Task 3: Rewrite the upstream-facing data contract

**Files:**
- Modify: `tests/test_onboarding_docs.py`
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Modify: `docs/blackbox_v2/data_bridge_v1/README.md`
- Modify: `docs/blackbox_v2/data_bridge_v1/manifest.json`
- Modify: `docs/blackbox_v2/README.md`
- Modify: `docs/superpowers/specs/2026-07-21-blackbox-v2-upstream-sop-self-contained-design.md`

- [ ] **Step 1: Write failing documentation assertions**

Require the upstream SOP to state that columns can increase, algorithms select fields by name, unused added columns are ignored, and the machine Schema is a minimum compatibility baseline. Require Python `3.13.12`, key package versions and resource limits. Reject Runtime Profile、Conda 环境名、操作系统平台、环境指纹、平台环境自检命令、the old fixed-count table heading, the three historical counts, the four production refresh times, and `完整检查保证` in the upstream SOP; keep the explicit rule that total column count is not fixed.

- [ ] **Step 2: Simplify the DataBridge update explanation**

Keep only how data is updated: the unified DataBridge performs a full three-frequency export and atomically publishes one generation after platform validation. Delete the production clock table and the internal validation checklist from the upstream SOP.

- [ ] **Step 3: Replace exact-header sample validation**

Update the copyable Python command to check file existence, non-empty data, first time key, unique columns, non-empty unique keys, baseline-field presence, and baseline relative order. Do not check a total column count. State that algorithms must explicitly verify their consumed fields and ignore unused added fields.

- [ ] **Step 4: Publish Python package and resource facts directly**

List Python and key package versions from `deploy/blackbox_v2/environment_manifest.json`, plus the resource limits from `deploy/blackbox_v2/runtime_profile_v1.json`. Do not publish Runtime Profile、Conda 环境名、操作系统平台、环境指纹 or a platform environment self-check command in the upstream SOP.

- [ ] **Step 5: Update sample documentation semantics**

Remove fixed column counts from the README and manifest. Describe samples as a point-in-time synthetic field example, not a permanent complete header. Keep the sample files unchanged.

- [ ] **Step 6: Run onboarding documentation tests**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs -v
```

Expected: all documentation tests pass.

### Task 4: Regression, scope audit, and commit

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run the Blackbox/DataBridge regression set**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest discover -s tests -p 'test_data_bridge_*.py' -q

conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest discover -s tests -p 'test_blackbox_v2_*.py' -q

conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs -q
```

- [ ] **Step 2: Run the full project test suite**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest discover -s tests -p 'test_*.py' -q
```

- [ ] **Step 3: Check policy wording and diff quality**

```bash
rg -n '`data-bridge-v1` 固定列数|columns=774|columns=575|columns=123|完整检查保证|06:00|06:30|06:35|07:00' \
  docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/blackbox_v2/data_bridge_v1/README.md \
  docs/blackbox_v2/data_bridge_v1/manifest.json
git diff --check
```

Expected: the search returns no matches and `git diff --check` succeeds.

- [ ] **Step 4: Commit only the reviewed scope**

Check `git status --short` and branch lists first. Do not stage the existing untracked launchd plist or `reports/production-gray-20260720/`. Commit the code, tests, SOP, data-contract docs, design revision, and this plan on `codex/audit-bugfixes-20260613`.
