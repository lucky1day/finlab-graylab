# 7Y Platform Calendar Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the two pending 7Y Blackbox delivery scripts so weekly features
can only use the platform-provided `api_wind_date.csv` mapping.

**Architecture:** The delivery script owns its Contract 1.0 validation. It reads
the fourth platform input from `--data-dir`, validates it as an opaque
date-to-week lookup, validates each Request anchor, and feeds only that lookup
into weekly feature alignment. No new shared platform dependency is introduced.

**Tech Stack:** Python 3.12, pandas, `forecast_env_blackbox_v1`, unittest,
Blackbox V2 Contract 1.0.

---

### Task 1: Create a temporary real-delivery regression harness

**Files:**
- Create: `/private/tmp/bbl-7y-calendar.LmAALX/test_7y_delivery_calendar.py`
- Test: both files in `/Users/macstudio0/Downloads/BLACKBOX_V2_7Y_INCREMENTAL_PASS_DELIVERY_V1/`

- [ ] **Step 1: Write the failing test**

Create a `unittest` module that loads each delivery file with
`importlib.util.spec_from_file_location`, writes a two-date synthetic
DataBridge directory, and asserts these public behaviors:

```python
with self.assertRaises(module.ContractError):
    module.read_snapshot(data_dir_without_calendar)

snapshot = module.read_snapshot(data_dir_with_wrong_anchor)
with self.assertRaises(module.ContractError):
    module.clipped(snapshot, request_with_weekly_cutoff_202628)
```

Also assert rejection of an extra calendar column and an unmapped consumed daily
row, and assert a valid `rdate,week_id` map produces a non-null map and a
weekly feature frame. Finally read the source and assert that the symbols
`EARLY_WEEK_TRANSITIONS_B64`, `WEEK_TRANSITIONS_B64`, `_wind_week_key`, and
`_static_week_key` are absent.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
conda run --no-capture-output -n forecast_env_blackbox_v1 \
  python /private/tmp/bbl-7y-calendar.LmAALX/test_7y_delivery_calendar.py
```

Expected: failures showing that the current delivery accepts a missing calendar,
accepts an anchor mismatch, and retains embedded/derived calendar symbols.

### Task 2: Make calendar input mandatory and fail closed

**Files:**
- Modify: `/Users/macstudio0/Downloads/BLACKBOX_V2_7Y_INCREMENTAL_PASS_DELIVERY_V1/seven_y_current55_lgbm_001_v1.py`
- Modify: `/Users/macstudio0/Downloads/BLACKBOX_V2_7Y_INCREMENTAL_PASS_DELIVERY_V1/seven_y_current55_lgbm_002_v1.py`

- [ ] **Step 1: Replace optional map loading with strict platform-input loading**

Remove `base64`, `bisect_right`, `zlib`, the compressed transition constants,
`_wind_week_key`, and `_static_week_key`. Change the snapshot field to require
`week_map: pd.DataFrame`. Add a helper shaped as follows:

```python
def read_week_map(data_dir: Path) -> pd.DataFrame:
    path = data_dir / "api_wind_date.csv"
    if not path.is_file():
        raise ContractError("data-dir must contain required api_wind_date.csv")
    frame = pd.read_csv(
        path, dtype={"rdate": "string", "week_id": "string"},
        keep_default_na=False,
    )
    if list(frame.columns) != ["rdate", "week_id"] or frame.empty:
        raise ContractError("api_wind_date.csv must have exactly rdate,week_id")
    rdate_text = frame["rdate"].str.strip()
    rdate = pd.to_datetime(
        rdate_text, format="%Y-%m-%d", errors="coerce"
    )
    week_id = frame["week_id"].str.strip()
    if (
        rdate_text.eq("").any()
        or rdate.isna().any()
        or rdate.dt.strftime("%Y-%m-%d").ne(rdate_text).any()
        or rdate.duplicated().any()
        or not rdate.is_monotonic_increasing
        or not week_id.str.fullmatch(r"\d{6}").all()
    ):
        raise ContractError("api_wind_date.csv violates platform calendar contract")
    return pd.DataFrame({"rdate": rdate, "week_id": week_id})
```

Use `pd.to_datetime` with `format="%Y-%m-%d"` and `errors="coerce"` plus a
round-trip `strftime` equality test for dates. Keep `week_id` as strings.

- [ ] **Step 2: Validate every Request and feature-row mapping**

In `clipped()`, locate daily, weekly, and monthly cutoff rows by exact unique
position, slice through those rows, then perform:

```python
calendar_lookup = snapshot.week_map.set_index("rdate")["week_id"]
daily_cutoff = pd.Timestamp(request["daily_cutoff_key"])
if daily_cutoff not in calendar_lookup.index:
    raise ContractError("calendar does not cover daily cutoff key")
if calendar_lookup.loc[daily_cutoff] != request["weekly_cutoff_key"]:
    raise ContractError("daily cutoff week mapping does not match weekly cutoff key")
```

Pass the clipped required map into `weekly_to_daily()`. In that function, reject
any null daily mapping and any mapped week ID not present in the clipped weekly
frame before applying `ffill()`.

- [ ] **Step 3: Apply the same I/O patch to both file-named variants**

Preserve the file-name based `001`/`002` parameter selection exactly. Do not
modify model fit logic, feature selection, thresholds, cache behavior, CLI
arguments, metadata, or result format.

### Task 3: Verify both delivery files and review the patch

**Files:**
- Verify: temporary harness and both delivery files

- [ ] **Step 1: Run the regression harness**

Run the Task 1 command and require every test for both scripts to pass.

- [ ] **Step 2: Prove no forbidden fallback remains**

Run:

```bash
rg -n 'EARLY_WEEK_TRANSITIONS_B64|WEEK_TRANSITIONS_B64|_wind_week_key|_static_week_key|bisect_right|base64\.b64decode|zlib\.decompress' \
  /Users/macstudio0/Downloads/BLACKBOX_V2_7Y_INCREMENTAL_PASS_DELIVERY_V1/seven_y_current55_lgbm_001_v1.py \
  /Users/macstudio0/Downloads/BLACKBOX_V2_7Y_INCREMENTAL_PASS_DELIVERY_V1/seven_y_current55_lgbm_002_v1.py
```

Expected: exit status 1 with no matches.

- [ ] **Step 3: Check syntax, command interface, and hashes**

Run `python -B <script> --help` in `forecast_env_blackbox_v1` for both files,
then record fresh SHA-256 hashes. Do not run Intake, activation, registry writes,
gray-live, launchctl, or any production operation.

- [ ] **Step 4: Review**

Dispatch a specification reviewer, then a code-quality reviewer. Both must
confirm the delivery contains no calendar fallback and keeps the weekly keys
opaque. Do not stage or commit because the shared worktree contains unrelated
user changes and the delivery files are outside the Git repository.
