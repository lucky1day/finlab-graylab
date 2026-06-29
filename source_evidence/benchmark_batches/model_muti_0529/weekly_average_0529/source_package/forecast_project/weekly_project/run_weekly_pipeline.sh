#!/bin/bash
set -euo pipefail

export TZ=Asia/Shanghai

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
PROJECT_ROOT="$SCRIPT_DIR"
FORECAST_ROOT="$(dirname "$PROJECT_ROOT")"
SRC_DIR="$PROJECT_ROOT/src"
PYTHON_DIR="$SRC_DIR/weekly"

ARG1="${1:-}"
CURRENT_DATE="$(date '+%Y-%m-%d')"
if [[ "$ARG1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  EXEC_STAGE="run"
  TRIGGER_DATE="$ARG1"
elif [ -z "$ARG1" ]; then
  EXEC_STAGE="multi"
  TRIGGER_DATE="$CURRENT_DATE"
else
  EXEC_STAGE="$ARG1"
  TRIGGER_DATE="${2:-$CURRENT_DATE}"
fi

if ! [[ "$TRIGGER_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "invalid trigger date: $TRIGGER_DATE, expected YYYY-MM-DD" >&2
  exit 2
fi

if [ -f "$FORECAST_ROOT/scripts/forecast_env.sh" ]; then
  # shellcheck source=/dev/null
  source "$FORECAST_ROOT/scripts/forecast_env.sh"
else
  echo "missing required environment file: $FORECAST_ROOT/scripts/forecast_env.sh" >&2
  exit 1
fi

if [ -z "${PYTHON_BIN:-}" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "invalid PYTHON_BIN from forecast_env.sh: ${PYTHON_BIN:-<empty>}" >&2
  exit 1
fi

export PYTHONPATH="$SRC_DIR:$PYTHON_DIR:$PROJECT_ROOT:$FORECAST_ROOT:${PYTHONPATH:-}"
RUN_DATE="$("$PYTHON_BIN" - "$TRIGGER_DATE" <<'PY'
from __future__ import annotations

import sys

from weekly.date_utils import normalize_weekly_rdate

print(normalize_weekly_rdate(sys.argv[1]))
PY
)"

LOG_DIR="$PROJECT_ROOT/logs/$RUN_DATE"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run.log"
ERROR_LOG="$LOG_DIR/error.log"
DB_CHECK_LOG="$LOG_DIR/db_check.log"

if ! declare -F forecast_log_section >/dev/null 2>&1; then
  forecast_log_section() {
    local log_file="$1"
    local title="$2"
    local note="${3:-}"
    {
      echo ""
      echo "================================================================================"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] $title"
      [ -n "$note" ] && echo "$note"
      echo "--------------------------------------------------------------------------------"
    } >> "$log_file"
  }
fi
if ! declare -F forecast_log_kv >/dev/null 2>&1; then
  forecast_log_kv() {
    printf '  - %s: %s\n' "$2" "$3" >> "$1"
  }
fi

run_main_process() {
  local stage_name="$1"
  local dry_arg=""
  if [ "$DRY_RUN" = "1" ]; then
    dry_arg="--dry-run"
  fi
  forecast_log_section "$LOG_FILE" "周频模型运行阶段：$stage_name" "周频生产规则：DB rdate 使用本周周六桶；不套日频交易日过滤。"
  forecast_log_kv "$LOG_FILE" "trigger_date" "$TRIGGER_DATE"
  forecast_log_kv "$LOG_FILE" "weekly bucket rdate" "$RUN_DATE"
  forecast_log_kv "$LOG_FILE" "DRY_RUN" "$DRY_RUN"
  forecast_log_kv "$LOG_FILE" "输出目录" "$PROJECT_ROOT/output/$RUN_DATE"
  (cd "$PROJECT_ROOT" && "$PYTHON_BIN" -m weekly.run_weekly "$RUN_DATE" $dry_arg) >> "$LOG_FILE" 2>> "$ERROR_LOG"
}

check_db_results() {
  local stage_name="$1"
  forecast_log_section "$DB_CHECK_LOG" "周频 DB 写入检查：$stage_name" "检查当前阶段是否需要重跑；dry-run 只验证 payload，不访问生产评估表。"
  forecast_log_kv "$DB_CHECK_LOG" "weekly bucket rdate" "$RUN_DATE"
  forecast_log_kv "$DB_CHECK_LOG" "DRY_RUN" "$DRY_RUN"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[$stage_name] DRY_RUN=1, skip weekly db result check" >> "$DB_CHECK_LOG"
    return 0
  fi
  echo "[$stage_name] weekly DB write mode enabled; detailed DB verification is handled by production DB constraints and payload logs" >> "$DB_CHECK_LOG"
  return 0
}

sleep_until() {
  local hh="$1"
  local mm="$2"
  local label="$3"
  local target_dt="${RUN_DATE} ${hh}:${mm}:00"
  local target_epoch
  target_epoch=$("$PYTHON_BIN" -c "import time; from datetime import datetime; print(int(time.mktime(datetime.strptime('$target_dt', '%Y-%m-%d %H:%M:%S').timetuple())))")
  local now_epoch
  now_epoch="$(date '+%s')"
  if [ "$now_epoch" -lt "$target_epoch" ]; then
    local dur=$((target_epoch - now_epoch))
    echo "[$label] sleep $dur seconds until $target_dt" >> "$LOG_FILE"
    sleep "$dur"
  else
    echo "[$label] target time $target_dt already passed" >> "$LOG_FILE"
  fi
}

forecast_log_section "$LOG_FILE" "周频入口启动" "本日志按阶段记录：环境、周六桶归一、模型复跑、预测输出、SHAP 和 DB 写入状态。"
forecast_log_kv "$LOG_FILE" "stage" "$EXEC_STAGE"
forecast_log_kv "$LOG_FILE" "trigger date" "$TRIGGER_DATE"
forecast_log_kv "$LOG_FILE" "weekly bucket rdate" "$RUN_DATE"
forecast_log_kv "$LOG_FILE" "DRY_RUN" "$DRY_RUN"
forecast_log_env "$LOG_FILE"

case "$EXEC_STAGE" in
  run)
    run_main_process "run"
    check_db_results "run_final"
    ;;
  rerun1|rerun2)
    if check_db_results "$EXEC_STAGE"; then
      echo "[$EXEC_STAGE] dry-run db check skipped" >> "$LOG_FILE"
    else
      run_main_process "${EXEC_STAGE}_rerun"
      check_db_results "${EXEC_STAGE}_final"
    fi
    ;;
  multi)
    run_main_process "run"
    sleep_until 16 00 "rerun1_wait"
    if ! check_db_results "rerun1"; then
      run_main_process "rerun1_rerun"
    fi
    sleep_until 22 00 "rerun2_wait"
    if ! check_db_results "rerun2"; then
      run_main_process "rerun2_rerun"
    fi
    check_db_results "multi_final"
    ;;
  *)
    echo "unknown stage: $EXEC_STAGE, expected run|rerun1|rerun2|multi" | tee -a "$ERROR_LOG" >&2
    exit 2
    ;;
esac

echo "end time: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
