#!/usr/bin/env bash
# Bond Factor Lab 公网只读入口验收（无写操作）
#
# 用法：
#   scripts/check_public_access.sh https://bond.finailab.cn
#
# 任一 TLS、curl、HTTP、header 或 JSON 校验失败均非零退出。
set -euo pipefail

readonly USER_AGENT="bond-factor-lab-access-check/1.0"

usage() {
  printf 'Usage: %s https://bond.finailab.cn\n' "$0" >&2
}

if [[ "$#" -ne 1 ]]; then
  usage
  exit 2
fi

BASE_ORIGIN="${1%/}"
if [[ "$BASE_ORIGIN" != "https://bond.finailab.cn" ]]; then
  printf 'BASE_URL must be exactly https://bond.finailab.cn\n' >&2
  exit 2
fi

readonly BASE_ORIGIN
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
LAST_REQUEST_SUCCEEDED=0

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
  touch -- "$LAST_HEADERS" "$LAST_BODY" "$error_file"
  LAST_REQUEST_SUCCEEDED=0
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
    LAST_REQUEST_SUCCEEDED=1
    record_pass "$label"
  else
    record_failure "$label" " expected=$expected_code"
  fi
}

header_query() {
  local name="$1" mode="$2" token="${3:-}"
  python3 - "$LAST_HEADERS" "$name" "$mode" "$token" <<'PY'
# FINAL_HEADER_PARSER_BEGIN
import re
import sys
from pathlib import Path

header_path, wanted_name, mode, wanted_token = sys.argv[1:]
final_headers = []
seen_status = False
for raw_line in Path(header_path).read_text(
    encoding="iso-8859-1", errors="strict"
).splitlines():
    if re.match(r"^HTTP/\S+\s+\d{3}(?:\s|$)", raw_line):
        final_headers = []
        seen_status = True
        continue
    if not seen_status or not raw_line or ":" not in raw_line:
        continue
    name, value = raw_line.split(":", 1)
    final_headers.append((name.strip(), value.strip()))

values = [
    value
    for name, value in final_headers
    if name.casefold() == wanted_name.casefold()
]
if mode == "values":
    print("\n".join(values))
elif mode == "token":
    tokens = {
        token.strip().casefold()
        for value in values
        for token in value.split(",")
        if token.strip()
    }
    if wanted_token.casefold() not in tokens:
        raise SystemExit(1)
elif mode == "single-token":
    if len(values) != 1 or values[0].casefold() != wanted_token.casefold():
        raise SystemExit(1)
elif mode == "optional-token":
    if len(values) > 1:
        raise SystemExit(1)
    if values and values[0].casefold() != wanted_token.casefold():
        raise SystemExit(1)
else:
    raise SystemExit(1)
# FINAL_HEADER_PARSER_END
PY
}

header_value() {
  header_query "$1" values
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

assert_last_vary_token() {
  local label="$1" expected_token="$2"
  if header_query Vary token "$expected_token"; then
    record_pass "$label"
  else
    record_failure "$label" " Vary=missing_exact_token"
  fi
}

assert_last_content_encoding() {
  local label="$1" expected="$2"
  if header_query Content-Encoding single-token "$expected"; then
    record_pass "$label"
  else
    record_failure "$label" " Content-Encoding=unexpected_or_repeated"
  fi
}

assert_last_not_gzip() {
  local label="$1"
  if header_query Content-Encoding optional-token identity; then
    record_pass "$label"
  else
    record_failure "$label" " Content-Encoding=unexpected_or_repeated"
  fi
}

extract_versioned_asset_url() {
  local body_path="$1" asset_name="$2"
  python3 - "$body_path" "$asset_name" <<'PY'
# VERSIONED_ASSET_EXTRACTOR_BEGIN
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
import sys


body_path, asset_name = sys.argv[1:]


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = set()

    def handle_starttag(self, tag, attrs):
        if tag.casefold() not in {"link", "script"}:
            return
        attribute = "href" if tag.casefold() == "link" else "src"
        raw_url = dict(attrs).get(attribute)
        if not raw_url:
            return
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or parsed.path != asset_name
            or "%" in parsed.query
        ):
            return
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if len(query) != 1 or query[0][0] not in {"v", "version"} or not query[0][1]:
            return
        self.values.add(raw_url)


parser = AssetParser()
parser.feed(Path(body_path).read_text(encoding="utf-8"))
if len(parser.values) != 1:
    raise SystemExit(1)
print(next(iter(parser.values)))
# VERSIONED_ASSET_EXTRACTOR_END
PY
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

body_is_valid() {
  local kind="$1" encoding="$2" body_path="$3" expected_token="${4:-}"
  python3 - "$kind" "$encoding" "$body_path" "$expected_token" <<'PY'
# BODY_VALIDATOR_BEGIN
import gzip
import json
import sys

DASHBOARD_TOP_FIELDS = {
    "schema_version", "snapshot_id", "generated_at", "display_until",
    "row_fields", "target_labels", "schemes",
}
DASHBOARD_SCHEME_FIELDS = {
    "scheme_id", "base_scheme_id", "name", "description", "horizon",
    "task_type", "frequency", "target_tenor", "target_label", "status",
    "deployed_at", "live_rows",
    "backtest",
}
DASHBOARD_BACKTEST_FIELDS = {
    "benchmark_id", "benchmark_label", "data_source", "data_source_label",
    "latest_run_date", "rows",
}
from pathlib import Path

kind, encoding, body_path, expected_token = sys.argv[1:]
wire = Path(body_path).read_bytes()
if encoding == "gzip":
    try:
        decoded = gzip.decompress(wire)
    except (EOFError, OSError) as exc:
        raise SystemExit(1) from exc
elif encoding == "identity":
    decoded = wire
else:
    raise SystemExit(1)

# 一次解压后必须直接是应用内容；第二层 gzip 明确拒绝。
if decoded.startswith(b"\x1f\x8b"):
    raise SystemExit(1)
try:
    text = decoded.decode("utf-8")
except UnicodeDecodeError as exc:
    raise SystemExit(1) from exc

if kind == "dashboard":
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(1) from exc
    if not isinstance(payload, dict):
        raise SystemExit(1)
    if payload.get("schema_version") != "factor-lab-dashboard-v3":
        raise SystemExit(1)
    if set(payload) != DASHBOARD_TOP_FIELDS:
        raise SystemExit(1)
    if payload.get("row_fields") != [
        "predict_date",
        "feature_date",
        "target_date",
        "prediction_phase",
        "predicted_direction",
        "actual_direction",
    ]:
        raise SystemExit(1)
    schemes = payload.get("schemes")
    if not isinstance(schemes, list):
        raise SystemExit(1)
    if any(
        not isinstance(scheme, dict)
        or set(scheme) != DASHBOARD_SCHEME_FIELDS
        or not isinstance(scheme.get("scheme_id"), str)
        or not scheme["scheme_id"]
        or (
            scheme.get("backtest") is not None
            and (
                not isinstance(scheme["backtest"], dict)
                or set(scheme["backtest"]) != DASHBOARD_BACKTEST_FIELDS
            )
        )
        for scheme in schemes
    ):
        raise SystemExit(1)
elif kind == "text":
    if not expected_token or expected_token not in text:
        raise SystemExit(1)
else:
    raise SystemExit(1)
# BODY_VALIDATOR_END
PY
}

assert_body_valid() {
  local label="$1"
  shift
  if body_is_valid "$@"; then
    record_pass "$label"
  else
    record_failure "$label" " body_validation=failed"
  fi
}

printf 'Bond Factor Lab public access check: assets=page-advertised\n'

# 固定域名 redirect；不跟随 Location。
run_request http-redirect GET "$HTTP_ORIGIN/bond-factor-lab/" 301
assert_last_header_exact \
  http-redirect-location Location "$EXPECTED_REDIRECT_ROOT"
run_request slash-redirect GET "$APP_URL" 301
assert_last_header_exact \
  slash-redirect-location Location "$EXPECTED_REDIRECT_ROOT"

# 页面和精确版本资源。
run_request page GET "$APP_URL/" 200
CSS_ASSET_URL="invalid-asset"
if [[ "$LAST_REQUEST_SUCCEEDED" -eq 1 ]] \
    && CSS_ASSET_URL="$(extract_versioned_asset_url "$LAST_BODY" "aifin-shell.css")"; then
  record_pass page-css-version
else
  record_failure page-css-version " current_asset_reference=invalid"
fi
JS_ASSET_URL="invalid-asset"
if [[ "$LAST_REQUEST_SUCCEEDED" -eq 1 ]] \
    && JS_ASSET_URL="$(extract_versioned_asset_url "$LAST_BODY" "aifin-shell.js")"; then
  record_pass page-js-version
else
  record_failure page-js-version " current_asset_reference=invalid"
fi
run_request versioned-css GET \
  "$APP_URL/$CSS_ASSET_URL" 200 \
  --header 'Accept-Encoding: gzip'
assert_last_content_encoding versioned-css-gzip gzip
assert_last_vary_token versioned-css-vary Accept-Encoding
assert_body_valid versioned-css-body text gzip "$LAST_BODY" ':root'
run_request versioned-js GET \
  "$APP_URL/$JS_ASSET_URL" 200 \
  --header 'Accept-Encoding: gzip'
assert_last_content_encoding versioned-js-gzip gzip
assert_last_vary_token versioned-js-vary Accept-Encoding
assert_body_valid \
  versioned-js-body text gzip "$LAST_BODY" 'factor-lab-dashboard-v3'
run_request asset-icon GET "$APP_URL/assets/aifin-lab-icon.svg" 200
run_request asset-logo GET "$APP_URL/assets/aifin-lab-logo.svg" 200

# Dashboard gzip/HEAD/identity/q=0/Vary 合同。
run_request dashboard-get-gzip GET \
  "$APP_URL/api/factor-lab/dashboard" 200 \
  --header 'Accept-Encoding: gzip'
assert_last_content_encoding dashboard-get-gzip-encoding gzip
assert_last_vary_token dashboard-get-gzip-vary Accept-Encoding
GZIP_CONTENT_LENGTH="$(header_value Content-Length)"
if [[ "$GZIP_CONTENT_LENGTH" =~ ^[1-9][0-9]*$ \
    && "$GZIP_CONTENT_LENGTH" == "$LAST_SIZE" ]]; then
  record_pass dashboard-get-gzip-length
else
  record_failure dashboard-get-gzip-length " content_length=unexpected"
fi
assert_body_valid dashboard-get-gzip-json dashboard gzip "$LAST_BODY"

run_request dashboard-head-gzip HEAD \
  "$APP_URL/api/factor-lab/dashboard" 200 \
  --header 'Accept-Encoding: gzip'
assert_last_content_encoding dashboard-head-gzip-encoding gzip
assert_last_vary_token dashboard-head-gzip-vary Accept-Encoding
assert_last_head_contract dashboard-head-gzip-length

run_request dashboard-get-identity GET \
  "$APP_URL/api/factor-lab/dashboard" 200 \
  --header 'Accept-Encoding: identity'
assert_last_not_gzip dashboard-get-identity-encoding
assert_last_vary_token dashboard-get-identity-vary Accept-Encoding
if [[ "$LAST_REQUEST_SUCCEEDED" -eq 1 ]] \
    && body_is_valid dashboard identity "$LAST_BODY"; then
  record_pass dashboard-get-identity-json
  cp "$LAST_BODY" "$CHECK_TMP_DIR/dashboard.json"
else
  record_failure dashboard-get-identity-json " body_validation=failed"
fi

run_request dashboard-get-gzip-q0 GET \
  "$APP_URL/api/factor-lab/dashboard" 200 \
  --header 'Accept-Encoding: gzip;q=0, identity;q=1'
assert_last_not_gzip dashboard-get-gzip-q0-encoding
assert_last_vary_token dashboard-get-gzip-q0-vary Accept-Encoding
assert_body_valid dashboard-get-gzip-q0-json dashboard identity "$LAST_BODY"

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
run_request deny-encoded-health GET "$APP_URL/api/he%61lth" 403
run_request deny-encoded-prefix GET "$BASE_ORIGIN/%62ond-factor-lab/" 403
run_request deny-encoded-dashboard GET \
  "$APP_URL/api/factor-lab/dashbo%61rd" 403
run_request deny-encoded-static GET "$APP_URL/%61ifin-shell.js" 403
run_request deny-single-dot GET "$APP_URL/./" 403
run_request deny-api-single-dot GET "$APP_URL/api/./health" 403
run_request deny-double-encoded-slash GET "$APP_URL/api%252fhealth" 403
run_request deny-trigger POST "$APP_URL/api/schemes/probe/trigger" 403
run_request deny-admin POST "$APP_URL/api/admin/registry/sync" 403

printf 'Summary: PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
if [[ "$FAIL" -ne 0 ]]; then
  exit 1
fi
