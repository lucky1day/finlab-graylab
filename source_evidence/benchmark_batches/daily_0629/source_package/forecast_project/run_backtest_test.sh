#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)" || {
  echo "run_backtest_test.sh: cannot resolve project root" >&2
  exit 1
}
cd "$PROJECT_ROOT"
if [ -z "${DRY_RUN+x}" ]; then
  export DRY_RUN=1
fi
source "$PROJECT_ROOT/scripts/forecast_env.sh"
export DRY_RUN="${DRY_RUN:-1}"

START_DATE="${1:-}"
END_DATE="${2:-}"
FREQUENCY="${3:-all}"
if [ -z "$START_DATE" ] || [ -z "$END_DATE" ]; then
  echo "Usage: $0 <START_DATE> <END_DATE> [all|daily|weekly|monthly]" >&2
  exit 2
fi

RUN_DATE="$(date +%F)"
LOG_DIR="$PROJECT_ROOT/logs/backtest_test/$RUN_DATE"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_backtest_test_${START_DATE}_${END_DATE}_${FREQUENCY}.log"
forecast_log_env "$LOG_FILE"

run_daily() {
  local dry_arg=""
  if [ "${DRY_RUN:-0}" = "1" ]; then
    dry_arg="--dry-run"
  fi
  (
    cd "$PROJECT_ROOT/daily_project"
    PYTHONPATH="$PROJECT_ROOT/daily_project/src:$PROJECT_ROOT:${PYTHONPATH:-}" \
      "$PYTHON_BIN" -m daily.run_backtest --start-date "$START_DATE" --end-date "$END_DATE" --workers "${DAILY_BACKTEST_WORKERS:-1}" $dry_arg
  ) 2>&1 | tee -a "$LOG_FILE"
}

run_weekly() {
  local dry_arg=""
  if [ "${DRY_RUN:-0}" = "1" ]; then
    dry_arg="--dry-run"
  fi
  (
    cd "$PROJECT_ROOT/weekly_project"
    PYTHONPATH="$PROJECT_ROOT/weekly_project/src:$PROJECT_ROOT/weekly_project:$PROJECT_ROOT:${PYTHONPATH:-}" \
      "$PYTHON_BIN" -m weekly.run_backtest "$START_DATE" "$END_DATE" --workers "${WEEKLY_BACKTEST_WORKERS:-1}" $dry_arg
  ) 2>&1 | tee -a "$LOG_FILE"
}

run_monthly() {
  local dry_arg=""
  if [ "${DRY_RUN:-0}" = "1" ]; then
    dry_arg="--dry-run"
  fi
  (
    cd "$PROJECT_ROOT/monthly_project"
    PYTHONPATH="$PROJECT_ROOT/monthly_project/src:$PROJECT_ROOT/monthly_project:$PROJECT_ROOT:${PYTHONPATH:-}" \
      "$PYTHON_BIN" -m monthly.run_backtest --start-date "$START_DATE" --end-date "$END_DATE" --workers "${MONTHLY_BACKTEST_WORKERS:-1}" $dry_arg
  ) 2>&1 | tee -a "$LOG_FILE"
}

case "$FREQUENCY" in
  daily) run_daily ;;
  weekly) run_weekly ;;
  monthly) run_monthly ;;
  all)
    run_daily
    run_weekly
    run_monthly
    ;;
  *)
    echo "unknown frequency: $FREQUENCY" >&2
    exit 2
    ;;
esac

echo "backtest test completed: $FREQUENCY $START_DATE..$END_DATE" | tee -a "$LOG_FILE"
