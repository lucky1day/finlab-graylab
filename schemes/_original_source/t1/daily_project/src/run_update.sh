#!/bin/bash
set -euo pipefail

export TZ=Asia/Shanghai

SRC_DIR="$(cd "$(dirname "$0")"; pwd)"
PROJECT_ROOT="$(dirname "$SRC_DIR")"
FORECAST_ROOT="$(dirname "$PROJECT_ROOT")"
PYTHON_DIR="$SRC_DIR/daily"
LOG_DIR="$PROJECT_ROOT/logs/daily"

RUN_DATE="${1:-$(date '+%Y-%m-%d')}"
if [ "$RUN_DATE" = "run" ]; then
  RUN_DATE="${2:-$(date '+%Y-%m-%d')}"
fi

if ! [[ "$RUN_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "invalid run date: $RUN_DATE, expected YYYY-MM-DD" >&2
  exit 2
fi

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run_update_${RUN_DATE//-/}.log"

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

echo "========================================" >> "$LOG_FILE"
echo "start time: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
echo "run date: $RUN_DATE" >> "$LOG_FILE"
echo "python: $PYTHON_BIN" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

if [ "${WRITE_DB:-0}" = "1" ]; then
  "$PYTHON_BIN" -m daily.run_update "$RUN_DATE" --write-db >> "$LOG_FILE" 2>&1
else
  "$PYTHON_BIN" -m daily.run_update "$RUN_DATE" >> "$LOG_FILE" 2>&1
fi

echo "end time: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
