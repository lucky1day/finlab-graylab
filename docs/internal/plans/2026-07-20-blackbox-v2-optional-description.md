# Blackbox V2 Optional Description Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow Contract 1.0 Blackbox V2 Metadata to carry an optional, validated algorithm `description`, surface it in the frontend remark column, and recommend it without blocking Intake.

**Architecture:** Extend the strict Metadata parser with one optional field while retaining the existing eight required fields and rejecting every other key. Reuse `t_scheme_registry.description` and existing API payloads, add the missing backtest response mapping, and let the frontend treat `description` as the final remark source. Existing Metadata remains byte-for-byte unchanged and valid.

**Tech Stack:** Python 3.12 dataclasses and unittest, FastAPI/SQLAlchemy service layer, native JavaScript frontend, Markdown SOPs.

---

## File map

- Create `tests/test_blackbox_v2_metadata.py`: focused Contract 1.0 optional-field validation.
- Modify `shared/blackbox_v2/contracts.py`: required/optional field sets, `BlackboxMetadata.description`, validation.
- Modify `shared/blackbox_v2/intake.py`: stable non-blocking recommendation helper.
- Modify `harness/cli.py`: expose Intake `warnings` in machine JSON.
- Modify `tests/test_blackbox_v2_intake.py`: warning/no-warning behavior.
- Modify `scheduler/discovery.py`: map explicit description and use empty legacy fallback.
- Modify `tests/test_blackbox_v2_discovery.py`: mapping and version semantics.
- Modify `backend/services.py`: include description in backtest factor-lab scheme rows.
- Modify `tests/test_backtest_factor_lab_readonly.py`: verify the backtest API payload.
- Modify `frontend/aifin-shell.js`: use API description as a remark source.
- Modify `tests/test_frontend_factor_lab.py`: precedence, empty behavior, and HTML escaping.
- Modify `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`: recommend the optional field.
- Modify `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`: document warning, mapping, and legacy compatibility.
- Modify `tests/test_onboarding_docs.py`: lock the agreed SOP semantics.

### Task 1: Metadata contract and Intake recommendation

**Files:**
- Create: `tests/test_blackbox_v2_metadata.py`
- Modify: `tests/test_blackbox_v2_intake.py`
- Modify: `shared/blackbox_v2/contracts.py`
- Modify: `shared/blackbox_v2/intake.py`
- Modify: `harness/cli.py`

- [ ] **Step 1: Write focused failing Metadata tests**

Create tests that write JSON to a temporary path and assert these exact outcomes:

```python
def test_contract_1_0_accepts_missing_description(self) -> None:
    metadata = load_metadata(_write_metadata(self.root))
    self.assertIsNone(metadata.description)

def test_contract_1_0_accepts_and_strips_description(self) -> None:
    metadata = load_metadata(
        _write_metadata(self.root, description="  使用流动性指标判断未来方向。  ")
    )
    self.assertEqual(metadata.description, "使用流动性指标判断未来方向。")

def test_rejects_invalid_optional_description(self) -> None:
    for value in ("", [], "第一行\n第二行", "<b>模型</b>", "算" * 301):
        with self.subTest(value=value):
            with self.assertRaisesRegex(ValueError, "description"):
                load_metadata(_write_metadata(self.root, description=value))
```

Keep a separate assertion proving an undeclared key such as `platform_note` still raises `metadata fields mismatch`.

- [ ] **Step 2: Write failing Intake warning tests**

Extend the CLI test to assert:

```python
self.assertEqual(
    payload["warnings"],
    ["Blackbox V2 Metadata 未提供 description；建议上游补充简短算法逻辑说明。"],
)
```

Add a second delivery with a valid `description` and assert `payload["warnings"] == []` and exit code `0`.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_blackbox_v2_metadata tests.test_blackbox_v2_intake -v
```

Expected: failures because `description` is currently an extra field, `BlackboxMetadata` has no `description`, and Intake JSON has no `warnings`.

- [ ] **Step 4: Implement the minimal Contract 1.0 extension**

In `shared/blackbox_v2/contracts.py`, replace the single exact set with:

```python
REQUIRED_METADATA_FIELDS = {
    "schema_version", "scheme_id", "name", "algorithm_version",
    "target_tenor", "task_type", "horizon", "target_rule",
}
OPTIONAL_METADATA_FIELDS = {"description"}
MAX_DESCRIPTION_LENGTH = 300
```

Add `description: str | None = None` as the final `BlackboxMetadata` field so existing keyword constructors remain compatible. Validate missing required keys and keys outside the union. When present, call `_non_empty_string`, reject length over 300, newline/carriage return, `<`, or `>`, strip it, and pass it into the dataclass.

- [ ] **Step 5: Add the stable Intake recommendation**

In `shared/blackbox_v2/intake.py` add:

```python
DESCRIPTION_RECOMMENDATION = (
    "Blackbox V2 Metadata 未提供 description；建议上游补充简短算法逻辑说明。"
)

def intake_warnings(metadata: BlackboxMetadata) -> list[str]:
    return [] if metadata.description else [DESCRIPTION_RECOMMENDATION]
```

In `harness/cli.py`, load the preserved Metadata after `intake_delivery` and add `"warnings": intake_warnings(metadata)` to the existing JSON response. Do not change the success exit code or `intake_delivery` return type.

- [ ] **Step 6: Run tests and verify GREEN**

Run the Task 1 command again. Expected: all tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add shared/blackbox_v2/contracts.py shared/blackbox_v2/intake.py harness/cli.py \
  tests/test_blackbox_v2_metadata.py tests/test_blackbox_v2_intake.py
git commit -m "feat: accept optional blackbox descriptions"
```

### Task 2: Discovery, Registry, and API propagation

**Files:**
- Modify: `scheduler/discovery.py`
- Modify: `backend/services.py`
- Modify: `tests/test_blackbox_v2_discovery.py`
- Modify: `tests/test_backtest_factor_lab_readonly.py`

- [ ] **Step 1: Write failing discovery tests**

Update the legacy assertion to expect an empty description and add an explicit description case:

```python
self.assertEqual(legacy.description, "")
self.assertEqual(described.description, "使用期限利差和滚动分类模型形成方向信号。")
```

Load the same scheme before and after adding the optional Metadata field and assert that `manifest_hash` and `scheme_version` differ, proving the explanation is version-bound.

- [ ] **Step 2: Write a failing backtest API test**

In the existing SQLite factor-lab result test, store a non-empty Registry description and assert:

```python
self.assertEqual(result["schemes"][0]["description"], "使用期限利差形成方向信号。")
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_blackbox_v2_discovery \
  tests.test_backtest_factor_lab_readonly -v
```

Expected: discovery still generates `Blackbox V2: {name}` and the backtest response omits `description`.

- [ ] **Step 4: Implement propagation**

In `_load_blackbox_config`, set:

```python
description=metadata.description or "",
```

In `backtest_factor_lab_results`, include:

```python
"description": str(meta.get("description") or ""),
```

Registry synchronization and `/api/schemes` already persist and return `cfg.description`; do not add a database migration or a second remark column.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Task 2 command again. Expected: all tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add scheduler/discovery.py backend/services.py \
  tests/test_blackbox_v2_discovery.py tests/test_backtest_factor_lab_readonly.py
git commit -m "feat: propagate blackbox descriptions"
```

### Task 3: Frontend remark display

**Files:**
- Modify: `frontend/aifin-shell.js`
- Modify: `tests/test_frontend_factor_lab.py`

- [ ] **Step 1: Write failing frontend tests**

Extend the existing hook test with:

```javascript
descriptionRemark: hooks.getSchemeRemark({ description: "滚动模型方向信号" }),
explicitRemark: hooks.getSchemeRemark({
  remark: "人工备注",
  description: "算法说明"
})
```

Assert `descriptionRemark == "滚动模型方向信号"` and `explicitRemark == "人工备注"`. Render a ranking row with `description: "<script>alert(1)</script>"` and assert the HTML contains `&lt;script&gt;` but not a raw `<script>` tag.

- [ ] **Step 2: Run the frontend test and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_frontend_factor_lab -v
```

Expected: the description-only remark is empty.

- [ ] **Step 3: Implement the frontend fallback**

Change the helper to:

```javascript
function getSchemeRemark(scheme) {
  return String((scheme && (
    scheme.remark || scheme.note || scheme.notes || scheme.description
  )) || "").trim();
}
```

Keep `escapeHtml(getSchemeRemark(scheme))` in the table renderer; do not render Metadata as raw HTML.

- [ ] **Step 4: Run the frontend suite and verify GREEN**

Run the Task 3 command again. Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add frontend/aifin-shell.js tests/test_frontend_factor_lab.py
git commit -m "feat: show blackbox algorithm descriptions"
```

### Task 4: SOPs, regression guards, and final verification

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Modify: `tests/test_onboarding_docs.py`

- [ ] **Step 1: Write failing documentation guards**

Add assertions that both SOPs contain `description`, `schema_version=1.0`, “可选” and “不阻断”; require the upstream SOP to recommend inputs/windows/rules/model/direction content, and the platform SOP to mention Intake `warnings`, Registry/API mapping, and existing delivery exemption.

- [ ] **Step 2: Run documentation tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs -v
```

Expected: new semantic markers are absent.

- [ ] **Step 3: Update both SOPs**

Change the upstream wording from “只包含以下八个字段” to “包含以下八个必填字段，并可选提供 `description`”, add the example field and validation/recommendation text. Add a platform Intake subsection documenting successful missing-description warnings, no migration for existing files, the `Registry.description` data flow, and escaped frontend rendering.

- [ ] **Step 4: Run documentation tests and verify GREEN**

Run the Task 4 documentation command again. Expected: all tests pass.

- [ ] **Step 5: Run complete relevant regression**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest discover -s tests -p 'test_blackbox_v2_*.py' -q

conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_scheduler_main tests.test_backend_api \
  tests.test_backtest_factor_lab_readonly tests.test_frontend_factor_lab \
  tests.test_onboarding_docs -q

git diff --check
```

Expected: every unittest command exits `0` with `OK`, and `git diff --check` prints nothing.

- [ ] **Step 6: Confirm existing delivery immutability**

Record and compare:

```bash
git diff --exit-code -- schemes/*/delivery/*.json
```

Expected: no delivery Metadata diff. Confirm the four active 1Y T+5 scheme versions are unchanged by read-only discovery.

- [ ] **Step 7: Commit Task 4**

```bash
git add docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md tests/test_onboarding_docs.py
git commit -m "docs: recommend blackbox algorithm descriptions"
```

- [ ] **Step 8: Final repository check**

Run `git status --short` and confirm only the pre-existing untracked plist and production evidence directory remain. Do not merge or push `master`.
