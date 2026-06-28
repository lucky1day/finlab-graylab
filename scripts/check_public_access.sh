#!/usr/bin/env bash
# =============================================================================
# Bond Factor Lab — 公网只读访问验收脚本（200/403 矩阵）
# -----------------------------------------------------------------------------
# 落地 PRD §8 验收标准，逐项断言 HTTP 状态码；任一不符即 exit 1。
#
# 用法：
#   bash scripts/check_public_access.sh
#   bash scripts/check_public_access.sh https://bond.finailab.cn/bond-factor-lab
#   BASE_URL=https://bond.finailab.cn/bond-factor-lab bash scripts/check_public_access.sh
#
# 注意：必须在“能访问公网入口”的机器上运行（开发沙箱网络受限，无法外联）。
#       脚本只读，不写库、不触发，安全可重复执行。
# =============================================================================
set -uo pipefail

BASE_URL="${1:-${BASE_URL:-https://bond.finailab.cn/bond-factor-lab}}"
BASE_URL="${BASE_URL%/}"   # 去掉结尾斜杠
# 由 https://host/bond-factor-lab 推导出 http://host/bond-factor-lab 用于跳转用例
HTTP_BASE_URL="$(printf '%s' "$BASE_URL" | sed -e 's#^https://#http://#')"

PASS=0
FAIL=0

# curl 取 HTTP 状态码（-o /dev/null 丢弃 body，-s 静默，最多 10s）
http_code() {
  local method="$1" url="$2"
  shift 2
  curl -k -s -o /dev/null -w '%{http_code}' -X "$method" \
       --max-time 10 "$@" "$url" 2>/dev/null
}

# 断言：method url 期望码
assert_code() {
  local method="$1" url="$2" want="$3"
  shift 3
  local got
  got="$(http_code "$method" "$url" "$@")"
  if [[ "$got" == "$want" ]]; then
    printf '  [PASS] %-4s %-55s -> %s\n' "$method" "${url#"$BASE_URL"}" "$got"
    PASS=$((PASS + 1))
  else
    printf '  [FAIL] %-4s %-55s -> %s (期望 %s)\n' "$method" "${url#"$BASE_URL"}" "$got" "$want"
    FAIL=$((FAIL + 1))
  fi
}

echo "==> 验收目标: $BASE_URL"
echo

# --- 取一个真实 scheme_id 喂给 metrics 用例（无 jq 用 grep/sed 兜底）---
SCHEMES_JSON="$(curl -k -s --max-time 10 "$BASE_URL/api/schemes" 2>/dev/null || true)"
SCHEME_ID=""
if command -v jq >/dev/null 2>&1; then
  SCHEME_ID="$(printf '%s' "$SCHEMES_JSON" | jq -r 'if type=="array" then .[0].scheme_id else (.schemes[0].scheme_id // .data[0].scheme_id) end' 2>/dev/null)"
fi
if [[ -z "$SCHEME_ID" || "$SCHEME_ID" == "null" ]]; then
  # 兜底：抓第一个 "scheme_id":"..." 值
  SCHEME_ID="$(printf '%s' "$SCHEMES_JSON" | grep -o '"scheme_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -e 's/.*"scheme_id"[[:space:]]*:[[:space:]]*"//' -e 's/"$//')"
fi

echo "--- 展示白名单（期望 200）---"
assert_code GET "$BASE_URL/"                          200
assert_code GET "$BASE_URL/api/health"               200
assert_code GET "$BASE_URL/api/schemes"              200
if [[ -n "$SCHEME_ID" ]]; then
  # scheme_id 可能含 / 之外的特殊字符，做最小 URL 编码（空格->%20）
  ENC_ID="${SCHEME_ID// /%20}"
  assert_code GET "$BASE_URL/api/metrics/$ENC_ID"    200
else
  printf '  [SKIP] GET  /api/metrics/{id}（未能从 /api/schemes 解析 scheme_id）\n'
fi
assert_code GET "$BASE_URL/api/backtests/factor-lab" 200

echo
echo "--- health body 校验 ---"
HEALTH_BODY="$(curl -k -s --max-time 10 "$BASE_URL/api/health" 2>/dev/null || true)"
if printf '%s' "$HEALTH_BODY" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
  printf '  [PASS] /api/health body 含 "status":"ok"\n'
  PASS=$((PASS + 1))
else
  printf '  [FAIL] /api/health body 不含 "status":"ok" (实际: %s)\n' "$HEALTH_BODY"
  FAIL=$((FAIL + 1))
fi

echo
echo "--- 原始数据导出（期望 403）---"
assert_code GET "$BASE_URL/api/predictions"            403
assert_code GET "$BASE_URL/api/actuals"                403
assert_code GET "$BASE_URL/api/targets"                403
assert_code GET "$BASE_URL/api/backtests/runs"         403
assert_code GET "$BASE_URL/api/backtests/data-checks"  403

echo
echo "--- 写入/触发（期望 403）---"
TRIGGER_ID="${SCHEME_ID:-any_scheme}"
assert_code POST "$BASE_URL/api/schemes/${TRIGGER_ID// /%20}/trigger" 403 -H 'Content-Type: application/json' -d '{}'
assert_code POST "$BASE_URL/api/admin/registry/sync"                  403 -H 'Content-Type: application/json' -d '{}'

echo
echo "--- HTTP -> HTTPS 跳转（期望 301）---"
# 不跟随重定向，断言 301
assert_code GET "$HTTP_BASE_URL/api/health" 301

echo
echo "============================================================"
printf '结果: PASS=%d  FAIL=%d\n' "$PASS" "$FAIL"
echo "============================================================"
[[ "$FAIL" -eq 0 ]] || exit 1
