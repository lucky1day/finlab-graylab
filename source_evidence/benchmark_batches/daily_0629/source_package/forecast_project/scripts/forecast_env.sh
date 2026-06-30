#!/bin/bash

_FORECAST_ENV_SCRIPT="${BASH_SOURCE[0]}"
FORECAST_PROJECT_ROOT="$(cd -P "$(dirname "$_FORECAST_ENV_SCRIPT")/.." >/dev/null 2>&1 && pwd -P)" || {
  echo "forecast_env.sh: cannot resolve FORECAST_PROJECT_ROOT from $_FORECAST_ENV_SCRIPT" >&2
  exit 1
}
CONDA_ENV_NAME="${CONDA_ENV_NAME:-forecast_env}"

_forecast_error() {
  echo "forecast_env.sh: $*" >&2
}

_forecast_set_venv_from_python() {
  local py="$1"
  VENV_PATH="$(cd -P "$(dirname "$py")/.." >/dev/null 2>&1 && pwd -P)"
}

if [ -n "${PYTHON_BIN:-}" ]; then
  if [ ! -x "$PYTHON_BIN" ]; then
    _forecast_error "PYTHON_BIN is set but not executable: $PYTHON_BIN"
    exit 1
  fi
  VENV_PATH="${VENV_PATH:-}"
  if [ -z "$VENV_PATH" ]; then
    _forecast_set_venv_from_python "$PYTHON_BIN"
  fi
elif [ -n "${VENV_PATH:-}" ]; then
  PYTHON_BIN="$VENV_PATH/bin/python"
  if [ ! -x "$PYTHON_BIN" ]; then
    _forecast_error "VENV_PATH is set but python is not executable: $PYTHON_BIN"
    exit 1
  fi
else
  _FORECAST_PYTHON_CANDIDATES=()
  if command -v conda >/dev/null 2>&1; then
    CONDA_BASE="$(conda info --base 2>/dev/null || true)"
    if [ -n "$CONDA_BASE" ]; then
      _FORECAST_PYTHON_CANDIDATES+=("$CONDA_BASE/envs/$CONDA_ENV_NAME/bin/python")
    fi
  fi
  _FORECAST_PYTHON_CANDIDATES+=("/Users/macmini02/miniconda3/envs/$CONDA_ENV_NAME/bin/python")
  _FORECAST_PYTHON_CANDIDATES+=("/Users/fengrl/miniconda3/envs/$CONDA_ENV_NAME/bin/python")
  if [ -n "${HOME:-}" ]; then
    _FORECAST_PYTHON_CANDIDATES+=("$HOME/miniconda3/envs/$CONDA_ENV_NAME/bin/python")
    _FORECAST_PYTHON_CANDIDATES+=("$HOME/anaconda3/envs/$CONDA_ENV_NAME/bin/python")
  fi

  PYTHON_BIN=""
  for _candidate in "${_FORECAST_PYTHON_CANDIDATES[@]}"; do
    if [ -x "$_candidate" ]; then
      PYTHON_BIN="$_candidate"
      _forecast_set_venv_from_python "$PYTHON_BIN"
      break
    fi
  done

  if [ -z "$PYTHON_BIN" ]; then
    _forecast_error "forecast conda python not found; refusing to fall back to system Python."
    _forecast_error "searched env name: $CONDA_ENV_NAME"
    _forecast_error "set PYTHON_BIN=/path/to/$CONDA_ENV_NAME/bin/python or VENV_PATH=/path/to/$CONDA_ENV_NAME"
    for _candidate in "${_FORECAST_PYTHON_CANDIDATES[@]}"; do
      _forecast_error "candidate: $_candidate"
    done
    exit 1
  fi
fi

if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import pandas  # noqa: F401
PY
then
  _forecast_error "selected Python cannot import pandas: $PYTHON_BIN"
  _forecast_error "this usually means the script is not using the forecast conda environment."
  exit 1
fi

export FORECAST_PROJECT_ROOT CONDA_ENV_NAME VENV_PATH PYTHON_BIN
export DRY_RUN="${DRY_RUN:-0}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
export PYTHONPATH="$FORECAST_PROJECT_ROOT:$FORECAST_PROJECT_ROOT/daily_project/src:$FORECAST_PROJECT_ROOT/weekly_project/src:$FORECAST_PROJECT_ROOT/monthly_project/src:${PYTHONPATH:-}"
export LD_LIBRARY_PATH="$VENV_PATH/lib:${LD_LIBRARY_PATH:-}"
export PATH="$VENV_PATH/bin:$PATH"

forecast_log_env() {
  local log_file="${1:-}"
  if [ -z "$log_file" ]; then
    return 0
  fi
  {
    echo "conda env name: $CONDA_ENV_NAME"
    echo "venv path: $VENV_PATH"
    echo "python: $PYTHON_BIN"
    echo "forecast project root: $FORECAST_PROJECT_ROOT"
    echo "dry run: $DRY_RUN"
    echo "pythonpath: ${PYTHONPATH:-}"
  } >> "$log_file"
}

forecast_log_section() {
  local log_file="${1:-}"
  local title="${2:-}"
  local note="${3:-}"
  if [ -z "$log_file" ]; then
    return 0
  fi
  {
    echo ""
    echo "================================================================================"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $title"
    if [ -n "$note" ]; then
      echo "$note"
    fi
    echo "--------------------------------------------------------------------------------"
  } >> "$log_file"
}

forecast_log_kv() {
  local log_file="${1:-}"
  local key="${2:-}"
  local value="${3:-}"
  if [ -z "$log_file" ]; then
    return 0
  fi
  printf '  - %s: %s\n' "$key" "$value" >> "$log_file"
}

forecast_log_end_section() {
  local log_file="${1:-}"
  if [ -z "$log_file" ]; then
    return 0
  fi
  echo "================================================================================" >> "$log_file"
}
