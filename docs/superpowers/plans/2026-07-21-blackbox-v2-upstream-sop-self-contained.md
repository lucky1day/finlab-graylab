# Blackbox V2 Upstream SOP Self-Contained Rewrite Implementation Plan

> **2026-07-21 复审更正：** 本计划记录首版实施过程，其中“固定列数、完整表头、上游生产刷新时间表”要求已被 [DataBridge V1 增量列兼容实施计划](2026-07-21-databridge-v1-additive-columns.md) 取代，不得再按本计划中的旧步骤执行。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the Blackbox V2 upstream delivery SOP into the only human document an algorithm engineer needs, including direct DataBridge download and verification instructions for the three real CSV files.

**Architecture:** Keep Contract 1.0 and the machine schema unchanged. Lock the human-facing requirements in documentation tests first, then reorganize the existing SOP around the engineer's workflow: download real DataBridge data, verify it, implement the two-file contract, and run local validation. The ZIP package remains out of scope until the rewritten SOP is reviewed.

**Tech Stack:** Markdown, Python `unittest`, `curl`, pandas, JSON Schema asset

---

### Task 1: Lock the self-contained and download requirements with failing tests

**Files:**
- Modify: `tests/test_onboarding_docs.py:45-75`
- Modify: `tests/test_onboarding_docs.py:153-170`
- Test: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`

- [ ] **Step 1: Add a self-contained DataBridge download test**

Add this test to `OnboardingDocumentationTests`:

```python
def test_upstream_sop_is_self_contained_and_explains_databridge_download(self) -> None:
    text = UPSTREAM_SOP.read_text(encoding="utf-8")

    for marker in (
        "唯一需要阅读的人类文档",
        "DATABRIDGE_API_BASE_URL",
        "DATABRIDGE_API_USERNAME",
        "DATABRIDGE_API_PASSWORD",
        "export/csv/",
        "frequency=日",
        "frequency=周",
        "frequency=月",
        "sample_data/daily_output.csv",
        "sample_data/weekly_output.csv",
        "sample_data/monthly_output.csv",
        "data_bridge_v1_schema.json",
        "f959777b7f251937b6364843a81d8eb696072ca7671b1306c368aa0f3cf735dc",
        "真实 DataBridge 数据",
        "接口烟雾测试",
    ):
        self.assertIn(marker, text)

    self.assertNotRegex(
        text,
        r"\[[^\]]+\]\([^)]*\.md(?:#[^)]*)?\)",
    )
```

The exact `frequency=...` strings must appear in explanatory command comments immediately before the shell commands, because the shell commands themselves use `--data-urlencode "frequency=..."`.

- [ ] **Step 2: Require the daily refresh explanation in the upstream SOP**

In `test_platform_sop_defines_v2_preflight_timeline_and_isolation`, keep the platform-only gate assertion and replace the old upstream `06:35` absence assertion with:

```python
for marker in (
    "06:00",
    "06:30",
    "06:35",
    "07:00",
    "上一交易日",
    "连续两轮",
    "同一个 generation",
    "整体原子发布",
    "当天不运行、不自动补跑",
):
    self.assertIn(marker, upstream)
self.assertNotIn("v2-scheduler-gate-v1", upstream)
```

- [ ] **Step 3: Keep platform internals banned while allowing the user-facing download interface**

Extend `test_upstream_sop_contains_no_platform_internal_state` with these platform-only markers:

```python
"t_scheme_registry",
"gray_backfill_write",
"phase_ranges",
"launchctl",
```

Do not ban `export/csv/`, the three `DATABRIDGE_API_*` variables, or `generation`; those are now required user-facing concepts.

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest \
    tests.test_onboarding_docs.OnboardingDocumentationTests.test_upstream_sop_is_self_contained_and_explains_databridge_download \
    tests.test_onboarding_docs.OnboardingDocumentationTests.test_platform_sop_defines_v2_preflight_timeline_and_isolation \
    tests.test_onboarding_docs.OnboardingDocumentationTests.test_upstream_sop_contains_no_platform_internal_state -v
```

Expected: the first two tests fail because the existing SOP links another README and does not contain the download commands or refresh timeline; the platform-internal-state test still passes.

### Task 2: Rewrite the SOP around the algorithm engineer's workflow

**Files:**
- Modify: `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md:1-264`
- Test: `tests/test_onboarding_docs.py`

- [ ] **Step 1: Replace the opening with a single-document boundary**

Use this opening contract:

```markdown
本文是上游算法工程师唯一需要阅读的人类文档。完成开发只需要本文、随包的
`data_bridge_v1_schema.json` 和三份脱敏 sample；不需要再阅读仓库内其他文档。

本地开发和效果验证优先使用从统一 DataBridge 下载的真实三频 CSV；sample 只用于
DataBridge 暂时不可用时的读取、截断和接口烟雾测试。最终仍只交付同名 `.py + .json`。
```

Update the verification date to `2026-07-21`. Remove every Markdown link to another `.md` document.

- [ ] **Step 2: Add the unified DataBridge background and daily refresh timeline**

Before Metadata and CLI implementation, add a section that states:

```markdown
- 所有人使用同一个 DataBridge 导出接口和同一份 `data-bridge-v1` Schema；不得自行写 SQL、拼接其他数据源或手工改表。
- 开发阶段由工程师主动下载真实数据；生产阶段由平台准备只读 `--data-dir`，算法不主动下载。
```

Include this exact table:

```markdown
| 时间（Asia/Shanghai） | 动作 | 算法工程师需要知道的结果 |
|---|---|---|
| 06:00 | 第一次全量导出 | 构建日、周、月三份候选数据 |
| 06:30 | 第一次完整检查 | 通过则等待最终校验；未通过则等待重试 |
| 06:35 | 条件全量重导 | 仅在 06:30 未通过时执行 |
| 07:00 | 最终完整校验 | 通过才允许当天 V2 使用；失败则当天不运行、不自动补跑 |
```

Explain the seven validation guarantees from the approved design: exact three files and frozen schema; daily coverage through the previous trading day; unique ordered keys with no historical disappearance; finite numeric-or-empty values; two consecutive matching full-export digests; one generation atomically published; failed refresh keeps the previous complete data without calling it current.

- [ ] **Step 3: Add copyable DataBridge download commands**

Add an explicit note that these are CSV files that Excel can open, not `.xlsx` workbooks. Use this environment setup:

```bash
export DATABRIDGE_API_BASE_URL="<DataBridge 管理方提供的 /api/ 地址>"
export DATABRIDGE_API_USERNAME="<DataBridge 用户名>"
read -s DATABRIDGE_API_PASSWORD
export DATABRIDGE_API_PASSWORD
export DATABRIDGE_END_DATE="<YYYY-MM-DD 数据截止日>"
mkdir -p sample_data
```

Then include the three approved `curl --fail-with-body --location --retry 3` commands. Daily must pass `frequency=日`, `start_date=2010-01-01`, and `end_date=$DATABRIDGE_END_DATE`; weekly and monthly pass only `frequency=周` and `frequency=月`. Save to exactly:

```text
sample_data/daily_output.csv
sample_data/weekly_output.csv
sample_data/monthly_output.csv
```

State that credentials are supplied by the DataBridge manager and must never be written into `{scheme_id}.py`, `{scheme_id}.json`, shell history, logs, or the delivery package. Add `unset DATABRIDGE_API_PASSWORD` after download.

- [ ] **Step 4: Add a copyable schema verification command**

Provide a Python command that loads `data_bridge_v1_schema.json`, then for each real downloaded CSV checks exact header equality and key validity:

```python
import json
from pathlib import Path

import pandas as pd

root = Path("sample_data")
schema = json.loads(Path("data_bridge_v1_schema.json").read_text(encoding="utf-8"))
keys = {
    "daily_output.csv": "date",
    "weekly_output.csv": "week_id",
    "monthly_output.csv": "month_id",
}

for filename, key in keys.items():
    path = root / filename
    frame = pd.read_csv(path, dtype="string", keep_default_na=False)
    expected = schema["files"][filename]["columns"]
    if frame.empty:
        raise ValueError(f"{filename} is empty")
    if list(frame.columns) != expected:
        raise ValueError(f"{filename} header does not match data-bridge-v1")
    if frame[key].str.strip().eq("").any() or frame[key].duplicated().any():
        raise ValueError(f"{filename} {key} must be non-empty and unique")
    print(filename, "OK", f"rows={len(frame)}", f"columns={len(frame.columns)}")
```

Document expected column counts `774 / 575 / 123`, time keys `date / week_id / month_id`, and Schema SHA-256 `f959777b7f251937b6364843a81d8eb696072ca7671b1306c368aa0f3cf735dc`.

- [ ] **Step 5: Reorder and preserve the Contract 1.0 rules**

After the DataBridge sections, retain and consolidate the existing content in this order:

1. choose the fixed task combination;
2. fill Metadata, including concise `name` and optional recommended `description`;
3. implement `predict` and `backtest` in the same script;
4. read only the three `--data-dir` files actually consumed;
5. validate the seven-field Request and truncate each consumed frequency independently;
6. emit the exact five-field Result;
7. handle output, stderr/stdout, failure, determinism, batches, and future-row isolation;
8. run the complete upstream validation checklist;
9. deliver only `{scheme_id}.py + {scheme_id}.json`.

Do not weaken or remove the existing fixed task table, Metadata example, one-to-100 batch rule, current-snapshot as-of limitation, or direction mapping.

- [ ] **Step 6: Remove platform-only material and cross-document dependencies**

Confirm that the rewritten upstream SOP does not mention Registry, composite IDs, lifecycle status, activation or authorization tokens, gray backfill, frontend/API, database tables, scheduler restart implementation, launchd, platform runtime paths, or actual join. Do not include credentials or a concrete password.

- [ ] **Step 7: Run the focused tests and verify GREEN**

Run the Task 1 command again.

Expected: all three tests pass.

### Task 3: Verify the complete documentation contract

**Files:**
- Modify: `docs/superpowers/specs/2026-07-21-blackbox-v2-upstream-sop-self-contained-design.md`
- Test: `tests/test_onboarding_docs.py`

- [ ] **Step 1: Mark the approved design implemented**

Change the design status from `已确认，待实施` to `已实施（SOP 正文）`; keep the ZIP package explicitly deferred until user review.

- [ ] **Step 2: Run all onboarding documentation tests**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest tests.test_onboarding_docs -v
```

Expected: all onboarding documentation tests pass.

- [ ] **Step 3: Run static consistency checks**

Run:

```bash
rg -n '\]\([^)]*\.md' docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md
rg -n 'Registry|gray_backfill_write|phase_ranges|launchctl|t_scheme_registry' \
  docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md
git diff --check
```

Expected: both `rg` commands return no matches; `git diff --check` returns success.

- [ ] **Step 4: Commit the rewritten SOP and tests**

Before staging, check `git status --short` and `git branch --list`. Stage only:

```bash
git add docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md \
  docs/superpowers/specs/2026-07-21-blackbox-v2-upstream-sop-self-contained-design.md \
  tests/test_onboarding_docs.py
git commit -m "docs: make blackbox upstream SOP self-contained"
```

Do not stage the existing untracked plist or `reports/production-gray-20260720/`.

### Task 4: Full regression and handoff for SOP review

**Files:**
- Verify only; do not create the ZIP package yet.

- [ ] **Step 1: Run the full project test suite**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m unittest discover -s tests -p 'test_*.py' -q
```

Expected: zero failures.

- [ ] **Step 2: Verify the Git scope and final branch**

Run:

```bash
git diff --check
git status --short --branch
git log --oneline -5
```

Expected: no tracked uncommitted changes; only the pre-existing untracked plist and production report directory remain; the branch is `codex/audit-bugfixes-20260613`.

- [ ] **Step 3: Hand the rewritten SOP to the user for review**

Provide a clickable link to `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`, summarize the direct download and verification flow, and explicitly state that ZIP packaging has not started. Wait for user approval before building the final upstream kit.
