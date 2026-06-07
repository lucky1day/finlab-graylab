#!/bin/bash
set -euo pipefail

export TZ=Asia/Shanghai

SRC_DIR="$(cd "$(dirname "$0")"; pwd)"
PROJECT_ROOT="$(dirname "$SRC_DIR")"
FORECAST_ROOT="$(dirname "$PROJECT_ROOT")"
PYTHON_DIR="$SRC_DIR/daily"
LOG_DIR="$PROJECT_ROOT/logs/daily"

EXEC_STAGE="${1:-run}"
RUN_DATE="${2:-$(date '+%Y-%m-%d')}"

if ! [[ "$RUN_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "invalid run date: $RUN_DATE, expected YYYY-MM-DD" >&2
  exit 2
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_daily_${RUN_DATE//-/}.log"

VENV_PATH="${VENV_PATH:-/Users/macstudio0/miniconda3/envs/forecast_env}"
PYTHON_BIN="${PYTHON_BIN:-$VENV_PATH/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "python executable not found" | tee -a "$LOG_FILE" >&2
  exit 1
fi
export VENV_PATH PYTHON_BIN
export LD_LIBRARY_PATH="$VENV_PATH/lib:${LD_LIBRARY_PATH:-}"
export PATH="$VENV_PATH/bin:$PATH"

export PYTHONPATH="$FORECAST_ROOT:$SRC_DIR:$PYTHON_DIR:${PYTHONPATH:-}"

is_trading_day() {
  "$PYTHON_BIN" - "$RUN_DATE" <<'PY'
import sys
from datetime import datetime

run_date = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
try:
    import chinese_calendar
    ok = (not chinese_calendar.is_holiday(run_date)) and run_date.weekday() < 5
except Exception:
    ok = run_date.weekday() < 5
print("1" if ok else "0")
PY
}

run_main_process() {
  local stage_name="$1"
  local dry_arg=""
  if [ "${DRY_RUN:-0}" = "1" ]; then
    dry_arg="--dry-run"
  fi
  echo "[$stage_name] start daily LightGBM process for $RUN_DATE at $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
  "$PYTHON_BIN" -m daily.run_daily "$RUN_DATE" $dry_arg >> "$LOG_FILE" 2>&1
}

check_db_results() {
  local stage_name="$1"
  if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "[$stage_name] DRY_RUN=1, skip db result check" >> "$LOG_FILE"
    return 0
  fi
  echo "[$stage_name] check database results for $RUN_DATE" >> "$LOG_FILE"
  "$PYTHON_BIN" -m daily.check_db_result "$RUN_DATE" >> "$LOG_FILE" 2>&1
}

echo "========================================" >> "$LOG_FILE"
echo "start time: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
echo "stage: $EXEC_STAGE" >> "$LOG_FILE"
echo "run date: $RUN_DATE" >> "$LOG_FILE"
echo "python: $PYTHON_BIN" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

if [ "$(is_trading_day)" != "1" ]; then
  echo "$RUN_DATE is not a China trading day, skip daily forecast" | tee -a "$LOG_FILE"
  exit 0
fi

case "$EXEC_STAGE" in
  run)
    run_main_process "run"
    ;;
  rerun1|rerun2)
    if check_db_results "$EXEC_STAGE"; then
      echo "[$EXEC_STAGE] database already has all daily results" >> "$LOG_FILE"
    else
      run_main_process "${EXEC_STAGE}_rerun"
    fi
    ;;
  multi)
    run_main_process "run"
    sleep "${RERUN_WAIT_SECONDS:-900}"
    if ! check_db_results "rerun1"; then
      run_main_process "rerun1_rerun"
    fi
    sleep "${RERUN_WAIT_SECONDS:-900}"
    if ! check_db_results "rerun2"; then
      run_main_process "rerun2_rerun"
    fi
    ;;
  *)
    echo "unknown stage: $EXEC_STAGE, expected run|rerun1|rerun2|multi" | tee -a "$LOG_FILE" >&2
    exit 2
    ;;
esac

echo "end time: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
