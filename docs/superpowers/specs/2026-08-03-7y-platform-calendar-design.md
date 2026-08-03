# 7Y Blackbox Platform Calendar Design

**Status:** User-approved design — 2026-08-03

**Goal:** Make both pending 7Y T+1 Blackbox delivery scripts use only the
platform-provided `api_wind_date.csv` for date-to-week mapping.

## Scope

The two delivery files are:

- `/Users/macstudio0/Downloads/BLACKBOX_V2_7Y_INCREMENTAL_PASS_DELIVERY_V1/seven_y_current55_lgbm_001_v1.py`
- `/Users/macstudio0/Downloads/BLACKBOX_V2_7Y_INCREMENTAL_PASS_DELIVERY_V1/seven_y_current55_lgbm_002_v1.py`

They must require `<data-dir>/api_wind_date.csv`, with exactly ordered
`rdate,week_id` columns. `rdate` must be non-empty, valid `YYYY-MM-DD`, unique,
and strictly ascending. `week_id` must be a non-empty six-digit opaque string.

For every Request, the script must fail closed unless:

1. `daily_cutoff_key` occurs exactly once in the daily input and calendar;
2. its calendar `week_id` equals `weekly_cutoff_key` exactly as a string; and
3. that weekly cutoff occurs exactly once in the consumed weekly input; and
4. every daily row whose weekly feature is consumed maps to a week key present
   in the independently truncated weekly input.

The feature builder maps every consumed daily row through that same calendar and
fails if any mapped date is absent. It never derives, increments, decrements, or
interprets `week_id`.

## Root Cause

The current delivery treats `api_wind_date.csv` as optional. When absent, it
uses an embedded compressed transition table and `_wind_week_key()` derivation.
When present, it still does not verify the Request's daily-to-week anchor. This
permits a platform/Request calendar mismatch to silently influence weekly
features.

## Design

`read_snapshot()` reads and validates the required calendar together with the
three frequency files. `Snapshot.week_map` becomes required. `clipped()`
locates each cutoff by its unique row position, checks the daily anchor before
returning the per-Request snapshot, and preserves rows through that exact
position. `weekly_to_daily()` uses the required map only and rejects incomplete
calendar or weekly coverage.

The embedded calendar payload, its decoding imports/constants, `_wind_week_key`,
and `_static_week_key` are removed. Both scripts receive the same calendar I/O
patch; their existing stem-selected training window/threshold parameters remain
unchanged.

## Non-goals

- No change to model fitting, feature selection, score mapping, thresholds,
  windows, cache behavior, metadata, or command-line interface.
- No Intake, registry change, activation, business-table write, plist change,
  launchctl operation, or production execution.
- The independently discovered T+1 `feature_idx + 1` causality/index issue and
  persistent cache Contract issue remain outside this calendar-only change and
  must be separately decided before full Contract acceptance.

## Verification

Use a temporary, synthetic `--data-dir` through the real delivery CLI/module to
prove: missing calendar fails; malformed calendar fails; a mismatched
`daily_cutoff_key -> week_id` fails; and a valid mapped snapshot reaches the
post-calendar model-data validation path. Verify both delivery files have the
same intended source changes, retain their distinct file names, and contain no
embedded/derived weekly-calendar symbols.
