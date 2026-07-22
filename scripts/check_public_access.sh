#!/usr/bin/env bash
# Bond Factor Lab 公网只读入口验收（无写操作）
#
# 用法：
#   scripts/check_public_access.sh --mode rollout|final https://bond.finailab.cn
#
# rollout 要求旧 schemes/metrics/backtest 与 dashboard 同时可达；final 要求旧
# 展示 API 已收口。任一 TLS、curl、HTTP、header 或 JSON 校验失败均非零退出。
set -euo pipefail

readonly USER_AGENT="bond-factor-lab-access-check/1.0"
readonly POLICY_VERSION="20260722a"

usage() {
  printf 'Usage: %s --mode rollout|final https://bond.finailab.cn\n' "$0" >&2
}

if [[ "$#" -ne 3 || "$1" != "--mode" ]]; then
  usage
  exit 2
fi

MODE="$2"
BASE_ORIGIN="${3%/}"
if [[ "$MODE" != "rollout" && "$MODE" != "final" ]]; then
  usage
  exit 2
fi
if [[ "$BASE_ORIGIN" != "https://bond.finailab.cn" ]]; then
  printf 'BASE_URL must be exactly https://bond.finailab.cn\n' >&2
  exit 2
fi

readonly MODE BASE_ORIGIN
readonly HTTP_ORIGIN="http://bond.finailab.cn"
readonly APP_URL="$BASE_ORIGIN/bond-factor-lab"
readonly EXPECTED_REDIRECT_ROOT="$BASE_ORIGIN/bond-factor-lab/"

CHECK_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bond-factor-access-check.XXXXXX")"
trap 'rm -rf -- "$CHECK_TMP_DIR"' EXIT

PASS=0
FAIL=0
REQUEST_NUMBER=0
LAST_CODE="000"
LAST_SIZE="0"
LAST_TIME="0"
LAST_HEADERS=""
LAST_BODY=""

print_result() {
  local outcome="$1" label="$2" detail="${3:-}"
  printf '[%s] %-34s code=%s size=%s time=%ss%s\n' \
    "$outcome" "$label" "$LAST_CODE" "$LAST_SIZE" "$LAST_TIME" "$detail"
}

record_pass() {
  local label="$1" detail="${2:-}"
  PASS=$((PASS + 1))
  print_result PASS "$label" "$detail"
}

record_failure() {
  local label="$1" detail="${2:-}"
  FAIL=$((FAIL + 1))
  print_result FAIL "$label" "$detail"
}

run_request() {
  local label="$1" method="$2" url="$3" expected_code="$4"
  shift 4

  REQUEST_NUMBER=$((REQUEST_NUMBER + 1))
  LAST_HEADERS="$CHECK_TMP_DIR/${REQUEST_NUMBER}-${label}.headers"
  LAST_BODY="$CHECK_TMP_DIR/${REQUEST_NUMBER}-${label}.body"
  local error_file="$CHECK_TMP_DIR/${REQUEST_NUMBER}-${label}.error"
  local metrics curl_status
  local -a method_args
  if [[ "$method" == "HEAD" ]]; then
    method_args=(--head)
  else
    method_args=(--request "$method")
  fi

  # 不使用 -k；--path-as-is 确保绕过样本不会先被 curl 规范化。
  if metrics="$(
    curl --silent --show-error --path-as-is \
      --connect-timeout 3 --max-time 10 \
      --user-agent "$USER_AGENT" \
      --dump-header "$LAST_HEADERS" --output "$LAST_BODY" \
      --write-out $'%{http_code}\t%{size_download}\t%{time_total}' \
      "${method_args[@]}" "$@" "$url" 2>"$error_file"
  )"; then
    curl_status=0
  else
    curl_status=$?
  fi

  if [[ "$curl_status" -ne 0 ]]; then
    LAST_CODE="000"
    LAST_SIZE="0"
    LAST_TIME="0"
    record_failure "$label" " curl_exit=$curl_status"
    return 0
  fi

  IFS=$'\t' read -r LAST_CODE LAST_SIZE LAST_TIME <<<"$metrics"
  if [[ ! "$LAST_CODE" =~ ^[0-9]{3}$ || ! "$LAST_SIZE" =~ ^[0-9]+$ \
      || ! "$LAST_TIME" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    record_failure "$label" " malformed_curl_metrics"
  elif [[ "$LAST_CODE" == "$expected_code" ]]; then
    record_pass "$label"
  else
    record_failure "$label" " expected=$expected_code"
  fi
}

header_value() {
  local wanted="$1"
  awk -v wanted="$wanted" '
    {
      sub(/\r$/, "", $0)
      separator = index($0, ":")
      if (separator > 0) {
        name = substr($0, 1, separator - 1)
        if (tolower(name) == tolower(wanted)) {
          value = substr($0, separator + 1)
          sub(/^[[:space:]]+/, "", value)
          found = value
        }
      }
    }
    END { print found }
  ' "$LAST_HEADERS"
}

to_lower() {
  LC_ALL=C tr '[:upper:]' '[:lower:]' <<<"$1"
}

assert_last_header_exact() {
  local label="$1" name="$2" expected="$3" actual
  actual="$(header_value "$name")"
  if [[ "$actual" == "$expected" ]]; then
    record_pass "$label"
  else
    record_failure "$label" " ${name}=unexpected"
  fi
}

assert_last_header_contains() {
  local label="$1" name="$2" expected_token="$3" actual actual_lower token_lower
  actual="$(header_value "$name")"
  actual_lower="$(to_lower "$actual")"
  token_lower="$(to_lower "$expected_token")"
  if [[ "$actual_lower" == *"$token_lower"* ]]; then
    record_pass "$label"
  else
    record_failure "$label" " ${name}=missing_token"
  fi
}

assert_last_not_gzip() {
  local label="$1" actual actual_lower
  actual="$(header_value Content-Encoding)"
  actual_lower="$(to_lower "$actual")"
  if [[ -z "$actual" || "$actual_lower" == "identity" ]]; then
    record_pass "$label"
  else
    record_failure "$label" " content_encoding=unexpected"
  fi
}

assert_last_body_contains() {
  local label="$1" expected="$2"
  if grep -Fq -- "$expected" "$LAST_BODY"; then
    record_pass "$label"
  else
    record_failure "$label" " body_token=missing"
  fi
}

assert_last_head_contract() {
  local label="$1" actual_length
  actual_length="$(header_value Content-Length)"
  if [[ "$LAST_SIZE" == "0" && "$actual_length" =~ ^[1-9][0-9]*$ ]]; then
    record_pass "$label"
  else
    record_failure "$label" " head_content_length=unexpected"
  fi
}

printf 'Bond Factor Lab public access check: mode=%s policy=%s\n' \
  "$MODE" "$POLICY_VERSION"

# 固定域名 redirect；不跟随 Location。
run_request http-redirect GET "$HTTP_ORIGIN/bond-factor-lab/" 301
assert_last_header_exact \
  http-redirect-location Location "$EXPECTED_REDIRECT_ROOT"
run_request slash-redirect GET "$APP_URL" 301
assert_last_header_exact \
  slash-redirect-location Location "$EXPECTED_REDIRECT_ROOT"

# 页面和精确版本资源。
run_request page GET "$APP_URL/" 200
assert_last_body_contains page-css-version "aifin-shell.css?v=$POLICY_VERSION"
assert_last_body_contains page-js-version "aifin-shell.js?v=$POLICY_VERSION"
run_request versioned-css GET \
  "$APP_URL/aifin-shell.css?v=$POLICY_VERSION" 200 \
  --header 'Accept-Encoding: gzip'
assert_last_header_exact versioned-css-gzip Content-Encoding gzip
assert_last_header_contains versioned-css-vary Vary Accept-Encoding
run_request versioned-js GET \
  "$APP_URL/aifin-shell.js?v=$POLICY_VERSION" 200 \
  --header 'Accept-Encoding: gzip'
assert_last_header_exact versioned-js-gzip Content-Encoding gzip
assert_last_header_contains versioned-js-vary Vary Accept-Encoding
run_request asset-icon GET "$APP_URL/assets/aifin-lab-icon.svg" 200
run_request asset-logo GET "$APP_URL/assets/aifin-lab-logo.svg" 200

# Dashboard gzip/HEAD/identity/q=0/Vary 合同。
run_request dashboard-get-gzip GET \
  "$APP_URL/api/factor-lab/dashboard" 200 \
  --header 'Accept-Encoding: gzip'
assert_last_header_exact dashboard-get-gzip-encoding Content-Encoding gzip
assert_last_header_contains dashboard-get-gzip-vary Vary Accept-Encoding
GZIP_CONTENT_LENGTH="$(header_value Content-Length)"
if [[ "$GZIP_CONTENT_LENGTH" =~ ^[1-9][0-9]*$ \
    && "$GZIP_CONTENT_LENGTH" == "$LAST_SIZE" ]]; then
  record_pass dashboard-get-gzip-length
else
  record_failure dashboard-get-gzip-length " content_length=unexpected"
fi

run_request dashboard-head-gzip HEAD \
  "$APP_URL/api/factor-lab/dashboard" 200 \
  --header 'Accept-Encoding: gzip'
assert_last_header_exact dashboard-head-gzip-encoding Content-Encoding gzip
assert_last_header_contains dashboard-head-gzip-vary Vary Accept-Encoding
assert_last_head_contract dashboard-head-gzip-length

run_request dashboard-get-identity GET \
  "$APP_URL/api/factor-lab/dashboard" 200 \
  --header 'Accept-Encoding: identity'
assert_last_not_gzip dashboard-get-identity-encoding
assert_last_header_contains dashboard-get-identity-vary Vary Accept-Encoding
cp "$LAST_BODY" "$CHECK_TMP_DIR/dashboard.json"

run_request dashboard-get-gzip-q0 GET \
  "$APP_URL/api/factor-lab/dashboard" 200 \
  --header 'Accept-Encoding: gzip;q=0, identity;q=1'
assert_last_not_gzip dashboard-get-gzip-q0-encoding
assert_last_header_contains dashboard-get-gzip-q0-vary Vary Accept-Encoding

# Health 必须明确报告已预热 ready；用 JSON parser，不用易误判的 grep。
run_request health-ready GET "$APP_URL/api/health" 200 \
  --header 'Accept-Encoding: identity'
if python3 - "$LAST_BODY" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("dashboard_snapshot", {}).get("status") != "ready":
    raise SystemExit(1)
PY
then
  record_pass health-ready-json
else
  record_failure health-ready-json " dashboard_snapshot.status!=ready"
fi

# 从 dashboard 严格取一个真实 composite scheme ID，供 rollout/final metrics 验收。
SCHEME_ID_ENCODED=""
if SCHEME_ID_ENCODED="$(python3 - "$CHECK_TMP_DIR/dashboard.json" <<'PY'
import json
import sys
from urllib.parse import quote

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("schema") != "factor-lab-dashboard-v1":
    raise SystemExit(1)
schemes = payload.get("schemes")
if not isinstance(schemes, list) or not schemes:
    raise SystemExit(1)
scheme_id = schemes[0].get("id")
if not isinstance(scheme_id, str) or not scheme_id:
    raise SystemExit(1)
print(quote(scheme_id, safe=""))
PY
)"; then
  record_pass dashboard-scheme-id
else
  SCHEME_ID_ENCODED="invalid-probe-id"
  record_failure dashboard-scheme-id " missing_scheme_id"
fi

# Default-deny、安全路径和编码绕过矩阵。
run_request deny-docs GET "$APP_URL/docs" 403
run_request deny-redoc GET "$APP_URL/redoc" 403
run_request deny-openapi GET "$APP_URL/openapi.json" 403
run_request deny-unknown-api GET "$APP_URL/api/not-allowed" 403
run_request deny-unknown-page GET "$APP_URL/not-allowed" 403
run_request deny-case-variant GET "$BASE_ORIGIN/Bond-Factor-Lab/" 403
run_request deny-dashboard-trailing-slash GET \
  "$APP_URL/api/factor-lab/dashboard/" 403
run_request deny-double-slash GET \
  "$BASE_ORIGIN/bond-factor-lab//api/health" 403
run_request deny-dot-segment GET \
  "$APP_URL/not-allowed/../api/health" 403
run_request deny-encoded-slash GET "$APP_URL/api%2fhealth" 403
run_request deny-encoded-backslash GET "$APP_URL/api%5chealth" 403
run_request deny-encoded-dot GET "$APP_URL/%2e%2e/api/health" 403
run_request deny-double-encoded-slash GET "$APP_URL/api%252fhealth" 403
run_request deny-trigger POST "$APP_URL/api/schemes/probe/trigger" 403
run_request deny-admin POST "$APP_URL/api/admin/registry/sync" 403

# rollout 与 final 的同一版本化模板发布矩阵。
if [[ "$MODE" == "rollout" ]]; then
  LEGACY_CODE=200
else
  LEGACY_CODE=403
fi
run_request legacy-schemes GET "$APP_URL/api/schemes" "$LEGACY_CODE"
run_request legacy-metrics GET \
  "$APP_URL/api/metrics/$SCHEME_ID_ENCODED" "$LEGACY_CODE"
run_request legacy-backtest GET \
  "$APP_URL/api/backtests/factor-lab" "$LEGACY_CODE"

printf 'Summary: PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
if [[ "$FAIL" -ne 0 ]]; then
  exit 1
fi
