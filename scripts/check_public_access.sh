#!/usr/bin/env bash
# 公网入口验收。会话只通过私有文件或继承的文件描述符传入。
set -euo pipefail

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 -B "$SCRIPT_DIR/check_public_access.py" "$@"
