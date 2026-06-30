#!/bin/bash
set -euo pipefail

export TZ=Asia/Shanghai

SRC_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)"
DAILY_PROJECT_ROOT="$(dirname "$SRC_DIR")"
PROJECT_ROOT="$(dirname "$DAILY_PROJECT_ROOT")"

EXEC_STAGE="${1:-run}"
RUN_DATE="${2:-$(date '+%Y-%m-%d')}"

if ! [[ "$RUN_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "invalid run date: $RUN_DATE, expected YYYY-MM-DD" >&2
  exit 2
fi

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
export DRY_RUN="${DRY_RUN:-0}"
export PYTHONPATH="$SRC_DIR:$PROJECT_ROOT:${PYTHONPATH:-}"

LOG_DIR="$DAILY_PROJECT_ROOT/logs/$RUN_DATE"
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

check_trade_calendar() {
  forecast_log_section "$LOG_FILE" "日频交易日历检查" "生产规则：日频只允许在 t_trade_calendar.trade_flag=1 的 rdate 写入数据库。"
  forecast_log_kv "$LOG_FILE" "run_date" "$RUN_DATE"
  forecast_log_kv "$LOG_FILE" "skip_calendar_check" "${FORECAST_SKIP_TRADE_CALENDAR_CHECK:-0}"
  if [ "${FORECAST_SKIP_TRADE_CALENDAR_CHECK:-0}" = "1" ]; then
    echo "[calendar] skip t_trade_calendar check because FORECAST_SKIP_TRADE_CALENDAR_CHECK=1" >> "$LOG_FILE"
    return 0
  fi

  local status
  if ! status=$(
    (
      cd "$PROJECT_ROOT"
      "$PYTHON_BIN" - "$RUN_DATE" <<'PY'
from __future__ import annotations

import sys

from db_writer import _load_db_config


def main() -> int:
    rdate = sys.argv[1]
    try:
        import pymysql
    except ImportError as exc:
        print(f"ERROR:pymysql import failed: {exc}", file=sys.stderr)
        return 1

    cfg = _load_db_config()
    conn = pymysql.connect(
        host=str(cfg.get("host", "localhost")),
        user=str(cfg.get("user", "root")),
        password=str(cfg.get("password", "")),
        database=str(cfg.get("database", "bond_db")),
        port=int(cfg.get("port", 3306)),
        charset=str(cfg.get("charset", "utf8mb4")),
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute("select trade_flag from t_trade_calendar where rdate = %s limit 1", (rdate,))
            row = cursor.fetchone()
    finally:
        conn.close()
    if row is None:
        print("MISSING")
        return 0
    flag = str(row[0]).strip()
    print("TRADE" if flag == "1" else "NON_TRADE")
    return 0


raise SystemExit(main())
PY
    ) 2>> "$ERROR_LOG"
  ); then
    echo "[calendar] t_trade_calendar check failed for $RUN_DATE" >> "$ERROR_LOG"
    exit 1
  fi

  case "$status" in
    TRADE)
      echo "检查结果：$RUN_DATE 是交易日，继续运行日频模型。" >> "$LOG_FILE"
      ;;
    NON_TRADE)
      echo "检查结果：$RUN_DATE 是非交易日，正常退出 0；不会运行模型，也不会写 t_pre_market_forecast。" >> "$LOG_FILE"
      echo "[calendar] non-trading day, no t_pre_market_forecast write for $RUN_DATE" >> "$DB_CHECK_LOG"
      exit 0
      ;;
    MISSING)
      echo "[calendar] missing t_trade_calendar row for $RUN_DATE; abort to avoid DB date mismatch" >> "$ERROR_LOG"
      exit 1
      ;;
    *)
      echo "[calendar] unexpected t_trade_calendar status for $RUN_DATE: $status" >> "$ERROR_LOG"
      exit 1
      ;;
  esac
}

run_main_process() {
  local stage_name="$1"
  forecast_log_section "$LOG_FILE" "日频模型运行阶段：$stage_name" "本阶段将依次复跑 D1Y/D5Y/D10Y 最终筛选模型，候选模型输入会在 Python 内按 date < rdate 截断。"
  forecast_log_kv "$LOG_FILE" "PYTHON_BIN" "$PYTHON_BIN"
  forecast_log_kv "$LOG_FILE" "DRY_RUN" "$DRY_RUN"
  forecast_log_kv "$LOG_FILE" "input_source" "database"
  forecast_log_kv "$LOG_FILE" "输出目录" "$DAILY_PROJECT_ROOT/output/$RUN_DATE"
  local dry_arg=""
  if [ "$DRY_RUN" = "1" ]; then
    dry_arg="--dry-run"
  fi
  (
    cd "$DAILY_PROJECT_ROOT"
    "$PYTHON_BIN" -m daily.run_daily "$RUN_DATE" \
      --project-root "$PROJECT_ROOT" \
      --n-jobs "${DAILY_N_JOBS:-3}" \
      $dry_arg
  ) >> "$LOG_FILE" 2>> "$ERROR_LOG"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[$stage_name] DRY_RUN=1, skip daily db result check" >> "$DB_CHECK_LOG"
  else
    echo "[$stage_name] daily DB write mode enabled; see output/$RUN_DATE/db_payload/daily_db_write_summary.json" >> "$DB_CHECK_LOG"
  fi
}

forecast_log_section "$LOG_FILE" "日频入口启动" "本日志按阶段记录：环境、交易日历、模型复跑、预测输出、SHAP 和 DB 写入状态。"
forecast_log_kv "$LOG_FILE" "stage" "$EXEC_STAGE"
forecast_log_kv "$LOG_FILE" "run date / DB rdate" "$RUN_DATE"
forecast_log_kv "$LOG_FILE" "project root" "$PROJECT_ROOT"
forecast_log_kv "$LOG_FILE" "python path" "$PYTHONPATH"
if declare -F forecast_log_env >/dev/null 2>&1; then
forecast_log_env "$LOG_FILE"
fi
check_trade_calendar

case "$EXEC_STAGE" in
  run)
    run_main_process "run"
    ;;
  rerun1|rerun2)
    run_main_process "$EXEC_STAGE"
    ;;
  multi)
    run_main_process "run"
    sleep "${RERUN_WAIT_SECONDS:-0}"
    run_main_process "rerun1"
    sleep "${RERUN_WAIT_SECONDS:-0}"
    run_main_process "rerun2"
    ;;
  *)
    echo "unknown stage: $EXEC_STAGE, expected run|rerun1|rerun2|multi" | tee -a "$LOG_FILE" >&2
    exit 2
    ;;
esac

echo "end time: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
