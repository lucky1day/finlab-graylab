#!/bin/bash
# =============================================================================
# backfill_live_predictions.sh
#
# One-shot script to backfill live predictions for the gap between backtest end
# (~May 31, 2026) and the first live scheduler run (~June 5, 2026).
#
# What it does:
#   Runs python -m scheduler.executor for t1_daily and t5_daily across every
#   calendar date from 2026-06-01 through 2026-06-09.
#
# Idempotency:
#   Safe to re-run. The scheduler executor inserts predictions with a
#   UNIQUE KEY constraint on (scheme_id, target_tenor, predict_date, run_id),
#   so duplicate runs on the same date will either succeed with no new rows
#   or log a skipped status.
#
# Non-trading days:
#   The scheduler's execute_scheme consults the trade calendar and auto-skips
#   non-trading days (status="skipped"). There's no need to trim the date
#   list — the script passes all dates and lets the scheduler decide.
#
# Requirements:
#   - conda environment "bond_factor_lab_service" must exist and be usable
#     by the current user
#   - Database credentials must be available via .env file (project root)
#     or BOND_DB_* environment variables
#
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Colour helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

PASS="${GREEN}OK${NC}"
FAIL="${RED}FAIL${NC}"
SKIP="${YELLOW}SKIP${NC}"
WARN="${YELLOW}WARN${NC}"

# ── Prerequisite checks ─────────────────────────────────────────────────────
echo -e "${CYAN}=== Bond Factor Lab — Live Prediction Backfill ===${NC}"
echo "Project root: $PROJECT_ROOT"
echo

cd "$PROJECT_ROOT"

# 1. Database credentials
if [ -f "$PROJECT_ROOT/.env" ]; then
    echo -e "  .env file        … $PASS  ($PROJECT_ROOT/.env)"
    # source it so conda run inherits the vars
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
elif [ -n "${BOND_DB_USER:-}" ] && [ -n "${BOND_DB_PASSWORD:-}" ] && [ -n "${BOND_DB_NAME:-}" ]; then
    echo -e "  BOND_DB_* env    … $PASS"
else
    echo -e "  Database config  … $FAIL  (no .env and no BOND_DB_* env vars)"
    exit 1
fi

# 2. conda env
if conda env list 2>/dev/null | grep -qw 'bond_factor_lab_service'; then
    echo -e "  conda env         … $PASS  (bond_factor_lab_service)"
else
    echo -e "  conda env         … $FAIL  (bond_factor_lab_service not found)"
    exit 1
fi

# ── Execution ────────────────────────────────────────────────────────────────
declare -a SCHEMES=("t1_daily" "t5_daily")
declare -a DATES=(
    "2026-06-01"
    "2026-06-02"
    "2026-06-03"
    "2026-06-04"
    "2026-06-05"
    "2026-06-06"
    "2026-06-07"
    "2026-06-08"
    "2026-06-09"
)

declare -a RESULTS=()   # "scheme_id|date|status|details"

TOTAL=$(( ${#SCHEMES[@]} * ${#DATES[@]} ))
CURRENT=0

echo
echo -e "${CYAN}--- Running predictions ---${NC}"
echo

for scheme_id in "${SCHEMES[@]}"; do
    for predict_date in "${DATES[@]}"; do
        CURRENT=$((CURRENT + 1))
        LABEL="[$CURRENT/$TOTAL] $scheme_id @ $predict_date"

        OUTPUT=""
        set +e
        OUTPUT=$(conda run -n bond_factor_lab_service \
            python -m scheduler.executor \
                "$predict_date" \
                --scheme-id "$scheme_id" \
            2>&1)
        RC=$?
        set -e

        # Parse SchemeRunResult from stdout (dataclass repr)
        # Example: SchemeRunResult(scheme_id='t1_daily', status='success', records_written=16, ...)
        if [ "$RC" -eq 0 ] && echo "$OUTPUT" | grep -q "status='success'"; then
            WRITTEN=$(echo "$OUTPUT" | grep -o "records_written=[0-9]*" | grep -o '[0-9]*')
            echo -e "  $LABEL  … ${PASS}  (${WRITTEN:-?} records written)"
            RESULTS+=("$scheme_id|$predict_date|success|${WRITTEN:-?} records")
        elif [ "$RC" -eq 0 ] && echo "$OUTPUT" | grep -q "status='skipped'"; then
            REASON=$(echo "$OUTPUT" | grep -o "error_msg='[^']*'" | head -1 | sed "s/error_msg='//;s/'//")
            echo -e "  $LABEL  … ${SKIP}  (${REASON:-non-trading or paused})"
            RESULTS+=("$scheme_id|$predict_date|skipped|${REASON:-non-trading or paused}")
        else
            ERR_MSG=$(echo "$OUTPUT" | tail -5 | tr '\n' ' ')
            echo -e "  $LABEL  … ${FAIL}  ${ERR_MSG}"
            RESULTS+=("$scheme_id|$predict_date|failed|${ERR_MSG}")
        fi
    done
done

# ── Summary ──────────────────────────────────────────────────────────────────
SUCCESS_COUNT=0
SKIPPED_COUNT=0
FAILED_COUNT=0

for row in "${RESULTS[@]}"; do
    IFS='|' read -r _ _ status _ <<< "$row"
    case "$status" in
        success) SUCCESS_COUNT=$((SUCCESS_COUNT + 1)) ;;
        skipped) SKIPPED_COUNT=$((SKIPPED_COUNT + 1)) ;;
        failed)  FAILED_COUNT=$((FAILED_COUNT + 1)) ;;
    esac
done

echo
echo -e "${CYAN}=== Summary ===${NC}"
echo -e "  Total attempts : $TOTAL"
echo -e "  Success        : ${GREEN}${SUCCESS_COUNT}${NC}"
echo -e "  Skipped        : ${YELLOW}${SKIPPED_COUNT}${NC}"
echo -e "  Failed         : ${RED}${FAILED_COUNT}${NC}"
echo

if [ "$FAILED_COUNT" -gt 0 ]; then
    echo -e "${RED}Some jobs failed. Review the output above for error details.${NC}"
    echo -e "${YELLOW}Tip: re-run this script after fixing issues — it is idempotent.${NC}"
    exit 1
fi

echo -e "${GREEN}Backfill complete.${NC}"
