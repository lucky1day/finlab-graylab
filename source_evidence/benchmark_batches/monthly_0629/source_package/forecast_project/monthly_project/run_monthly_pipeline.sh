#!/bin/bash
set -euo pipefail

MONTHLY_PROJECT_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
PROJECT_ROOT="$(dirname "$MONTHLY_PROJECT_ROOT")"
if [ -f "$PROJECT_ROOT/scripts/forecast_env.sh" ]; then
  # shellcheck source=/dev/null
  source "$PROJECT_ROOT/scripts/forecast_env.sh"
else
  echo "missing required environment file: $PROJECT_ROOT/scripts/forecast_env.sh" >&2
  exit 1
fi
if [ -z "${PYTHON_BIN:-}" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "invalid PYTHON_BIN from forecast_env.sh: ${PYTHON_BIN:-<empty>}" >&2
  exit 1
fi
DRY_RUN="${DRY_RUN:-0}"
MONTHLY_RERUN_SLEEP_SECONDS="${MONTHLY_RERUN_SLEEP_SECONDS:-1800}"
export PYTHONPATH="$MONTHLY_PROJECT_ROOT:$PROJECT_ROOT:$MONTHLY_PROJECT_ROOT/src:${PYTHONPATH:-}"
export DRY_RUN

STAGE="${1:-multi}"
TRIGGER_DATE="${2:-$(date +%F)}"
if ! [[ "$TRIGGER_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "invalid trigger date: $TRIGGER_DATE, expected YYYY-MM-DD" >&2
  exit 2
fi
SCHEDULE_LINE="$("$PYTHON_BIN" - "$TRIGGER_DATE" <<'PY'
from __future__ import annotations

import sys

import pandas as pd
from data_service import read_trade_calendar_from_db
from monthly.monthly_calendar import resolve_monthly_schedule_from_trading_days

trade_calendar = read_trade_calendar_from_db()
trading_days = pd.to_datetime(
    trade_calendar.loc[trade_calendar["trade_flag"].astype(str).eq("1"), "rdate"],
    errors="coerce",
)
schedule = resolve_monthly_schedule_from_trading_days(
    sys.argv[1],
    trading_days=trading_days,
    trading_calendar_source="db:t_trade_calendar",
)
print(
    schedule.db_rdate,
    schedule.scheduled_trigger_date,
    schedule.feature_month,
    schedule.target_month,
)
PY
)"
read -r DB_RDATE SCHEDULED_TRIGGER_DATE FEATURE_MONTH TARGET_MONTH <<< "$SCHEDULE_LINE"
LOG_DIR="$MONTHLY_PROJECT_ROOT/logs/$DB_RDATE"
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
if ! declare -F forecast_log_env >/dev/null 2>&1; then
  forecast_log_env() {
    {
      echo "PYTHON_BIN=$PYTHON_BIN"
      "$PYTHON_BIN" - <<'PY'
import os, sys
print("sys.executable=" + sys.executable)
print("sys.path[:5]=" + repr(sys.path[:5]))
print("DRY_RUN=" + os.environ.get("DRY_RUN", ""))
PY
    } >> "$1" 2>&1
  }
fi
forecast_log_section "$LOG_FILE" "月频入口启动" "本日志按阶段记录：触发日、DB rdate、目标月、模型复跑、伪 SHAP、payload 和 DB 写入状态。"
forecast_log_kv "$LOG_FILE" "stage" "$STAGE"
forecast_log_kv "$LOG_FILE" "trigger date" "$TRIGGER_DATE"
forecast_log_kv "$LOG_FILE" "scheduled trigger date" "$SCHEDULED_TRIGGER_DATE"
forecast_log_kv "$LOG_FILE" "feature month" "$FEATURE_MONTH"
forecast_log_kv "$LOG_FILE" "target month" "$TARGET_MONTH"
forecast_log_kv "$LOG_FILE" "monthly db rdate" "$DB_RDATE"
forecast_log_kv "$LOG_FILE" "project root" "$PROJECT_ROOT"
forecast_log_kv "$LOG_FILE" "monthly project root" "$MONTHLY_PROJECT_ROOT"
forecast_log_kv "$LOG_FILE" "python path" "$PYTHONPATH"
forecast_log_env "$LOG_FILE"

run_main() {
  forecast_log_section "$LOG_FILE" "月频模型运行阶段" "月频生产规则：每月自然日15日固定触发；输入数据截止到15日及之前最近交易日。"
  forecast_log_kv "$LOG_FILE" "trigger_date" "$TRIGGER_DATE"
  forecast_log_kv "$LOG_FILE" "scheduled_trigger_date" "$SCHEDULED_TRIGGER_DATE"
  forecast_log_kv "$LOG_FILE" "feature_month" "$FEATURE_MONTH"
  forecast_log_kv "$LOG_FILE" "target_month" "$TARGET_MONTH"
  forecast_log_kv "$LOG_FILE" "monthly db rdate" "$DB_RDATE"
  forecast_log_kv "$LOG_FILE" "DRY_RUN" "$DRY_RUN"
  forecast_log_kv "$LOG_FILE" "输出目录" "$MONTHLY_PROJECT_ROOT/output/$DB_RDATE"
  local cmd=("$PYTHON_BIN" -m src.monthly.run_monthly_pipeline "$TRIGGER_DATE")
  if [ "$DRY_RUN" = "1" ]; then
    cmd+=(--dry-run)
  fi
  (cd "$MONTHLY_PROJECT_ROOT" && "${cmd[@]}") >> "$LOG_FILE" 2>> "$ERROR_LOG"
}

check_payload() {
  forecast_log_section "$DB_CHECK_LOG" "月频 payload / DB 检查" "检查 DB rdate 下 t_pre_market_forecast payload、伪 SHAP 清洗结果和写库摘要。"
  forecast_log_kv "$DB_CHECK_LOG" "trigger_date" "$TRIGGER_DATE"
  forecast_log_kv "$DB_CHECK_LOG" "monthly db rdate" "$DB_RDATE"
  forecast_log_kv "$DB_CHECK_LOG" "DRY_RUN" "$DRY_RUN"
  local dry_arg=""
  if [ "$DRY_RUN" = "1" ]; then
    dry_arg="--dry-run"
  fi
  (cd "$MONTHLY_PROJECT_ROOT" && "$PYTHON_BIN" -m src.monthly.check_db_result "$DB_RDATE" $dry_arg) >> "$DB_CHECK_LOG" 2>> "$ERROR_LOG"
}

case "$STAGE" in
  run)
    run_main
    check_payload
    ;;
  rerun1|rerun2)
    if ! check_payload; then
      run_main
    fi
    ;;
  multi)
    run_main
    if [ "$MONTHLY_RERUN_SLEEP_SECONDS" -gt 0 ]; then
      sleep "$MONTHLY_RERUN_SLEEP_SECONDS"
    fi
    if ! check_payload; then
      run_main
    fi
    if [ "$MONTHLY_RERUN_SLEEP_SECONDS" -gt 0 ]; then
      sleep "$MONTHLY_RERUN_SLEEP_SECONDS"
    fi
    if ! check_payload; then
      run_main
    fi
    ;;
  *)
    echo "unknown stage: $STAGE, expected run|rerun1|rerun2|multi" >&2
    exit 2
    ;;
esac

echo "end time: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
