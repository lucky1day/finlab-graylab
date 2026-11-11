#!/usr/bin/env bash
# 公网入口验收。会话只通过私有文件或继承的文件描述符传入。
set -euo pipefail

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# Dashboard Gate 需要服务环境依赖；可用 BFL_SERVICE_PYTHON 覆盖（见 docs/operations/DEPLOYMENT_ACCESS.md）。
readonly SERVICE_PYTHON="${BFL_SERVICE_PYTHON:-/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python}"
if [[ ! -x "$SERVICE_PYTHON" ]]; then
  echo "service python not found: $SERVICE_PYTHON (set BFL_SERVICE_PYTHON)" >&2
  exit 2
fi
exec "$SERVICE_PYTHON" -B "$SCRIPT_DIR/check_public_access.py" "$@"
