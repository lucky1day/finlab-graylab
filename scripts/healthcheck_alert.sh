#!/usr/bin/env bash
# =============================================================================
# Bond Factor Lab — 公网健康检查告警模板（可选，部署在入口机或任意监控点）
# -----------------------------------------------------------------------------
# 周期性探测 /api/health，失败时发告警。建议用 cron 或 systemd timer 调度。
# 这是“模板”：把 notify() 换成你的告警通道（邮件 / 钉钉 / 飞书 / Slack webhook）。
#
# cron 示例（每 5 分钟，避开整点）：
#   3,8,13,18,23,28,33,38,43,48,53,58 * * * * \
#     /Users/.../scripts/healthcheck_alert.sh >> /var/log/bond-health.log 2>&1
# =============================================================================
set -uo pipefail

URL="${1:-${HEALTH_URL:-https://bond.finailab.cn/bond-factor-lab/api/health}}"
TIMEOUT="${HEALTH_TIMEOUT:-10}"

notify() {
  # TODO: 替换为真实告警通道。默认仅打印到 stderr。
  echo "[ALERT] $(date '+%F %T') $*" >&2
}

code="$(curl -s -o /tmp/bond_health_body -w '%{http_code}' --max-time "$TIMEOUT" "$URL" 2>/dev/null || echo 000)"
body="$(cat /tmp/bond_health_body 2>/dev/null || true)"

if [[ "$code" == "200" ]] && printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
  echo "$(date '+%F %T') OK $URL -> 200"
  exit 0
fi

notify "health 异常: $URL -> HTTP $code body=$body"
exit 1
