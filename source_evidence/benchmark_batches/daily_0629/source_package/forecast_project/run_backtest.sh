#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd -P)" || {
  echo "run_backtest.sh: cannot resolve project root" >&2
  exit 1
}
cd "$PROJECT_ROOT"
export DRY_RUN="${DRY_RUN:-0}"
source "$PROJECT_ROOT/scripts/forecast_env.sh"

START_DATE="${1:-}"
END_DATE="${2:-}"
FREQUENCY="${3:-all}"
if [ -z "$START_DATE" ] || [ -z "$END_DATE" ]; then
  echo "Usage: $0 <START_DATE> <END_DATE> [all|daily|weekly|monthly]" >&2
  exit 2
fi

bash "$PROJECT_ROOT/run_backtest_test.sh" "$START_DATE" "$END_DATE" "$FREQUENCY"
