from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus
from shared.monthly_source_evidence import MONTHLY_SOURCE_ROLE, PLATFORM_CURRENT_MONTHLY_ROLE
from shared.scheme_config_loader import load_yaml_mapping
from shared.weekly_average_source_evidence import (
    PLATFORM_CURRENT_SOURCE_ROLE,
    WEEKLY_AVERAGE_SOURCE_ROLE,
    is_point_backed_weekly_average_provenance,
)


# 容差（PINNED）
DIRECTION_MATCH_RATE_REQUIRED = 1.0
MAX_CONFIDENCE_ABS_DIFF = 1e-8
MAX_INTERNAL_ABS_DIFF = 1e-8
METRIC_ACCURACY_ABS_DIFF = 0.001
STRICT_PREDICTION_FIELDS = (
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "benchmark_role",
    "direction",
    "confidence",
    "label",
    "is_correct",
)
WEEKLY_STRICT_PREDICTION_FIELDS = (
    "feature_week_id",
    "feature_date",
    "target_week_id",
    "target_date",
    "target_tenor",
    "horizon",
    "benchmark_role",
    "direction",
    "confidence",
    "label",
    "is_correct",
)
WEEKLY_AVERAGE_STRICT_PREDICTION_FIELDS = WEEKLY_STRICT_PREDICTION_FIELDS + ("target_rule",)
MONTHLY_STRICT_PREDICTION_FIELDS = (
    "feature_month_id",
    "feature_date",
    "target_month_id",
    "target_date",
    "target_tenor",
    "horizon",
    "benchmark_role",
    "target_rule",
    "direction",
    "confidence",
    "label",
    "is_correct",
)
STRICT_KEY_FIELDS = ("feature_date", "target_date", "target_tenor", "horizon", "benchmark_role")
WEEKLY_STRICT_KEY_FIELDS = (
    "feature_week_id",
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "benchmark_role",
)
MONTHLY_STRICT_KEY_FIELDS = (
    "feature_month_id",
    "feature_date",
    "target_month_id",
    "target_date",
    "target_tenor",
    "horizon",
    "benchmark_role",
)
INTERNAL_SCORE_FIELDS = ("vote_score",)
INTERNAL_NUMERIC_FIELDS = ("rule_vote",)
INTERNAL_NUMERIC_SUFFIXES = ("_score", "_vs")
INTERNAL_DIRECTION_SUFFIXES = ("_dir", "_sign")


class CompareGate(Gate):
    """对比当前平台输出与方案 benchmark 原始输出（PINNED stage="compare"）。"""

    name = "compare"

    def run(self, ctx: GateContext) -> GateResult:
        return guarded_result(self.name, lambda started_at: self._run(ctx, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        bench_dir = ctx.project_root / "schemes" / ctx.scheme_id / "benchmarks"
        sample_path = bench_dir / "original_predictions_sample.csv"
        summary_path = bench_dir / "original_backtest_summary.json"

        # 读取配置判断 benchmark 是否为必需
        config = load_yaml_mapping(
            ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml"
        )
        backtest_config = config.get("backtest", {}) if isinstance(config.get("backtest"), dict) else {}
        benchmark_required = bool(backtest_config.get("benchmark_required", False))
        strict_fields = _strict_prediction_fields(config)
        key_fields = _strict_key_fields(config)
        exact_match_fields = _exact_match_fields(config)
        expected_exact_values = _expected_exact_values(config)
        required_internal_fields = _required_internal_fields(backtest_config)

        if not sample_path.exists() and not summary_path.exists():
            if benchmark_required:
                finished_at = utc_now()
                return GateResult(
                    gate_name=self.name,
                    status=GateStatus.FAILED,
                    passed=False,
                    evidence=[
                        Evidence("benchmark_required", True),
                        Evidence("reason", "benchmark_required=true but no benchmark files present"),
                        Evidence("benchmark_dir", str(bench_dir)),
                        Evidence("expected_files", ["original_predictions_sample.csv", "original_backtest_summary.json"]),
                    ],
                    errors=["benchmark_required=true but no benchmark files present; "
                            "place original predictions CSV and backtest summary JSON in schemes/{id}/benchmarks/"],
                    started_at=started_at,
                    finished_at=finished_at,
                )
            finished_at = utc_now()
            return GateResult(
                gate_name=self.name,
                status=GateStatus.SKIPPED,
                passed=True,
                evidence=[
                    Evidence("skipped", True),
                    Evidence("reason", "no benchmark files present"),
                    Evidence("benchmark_dir", str(bench_dir)),
                ],
                errors=[],
                started_at=started_at,
                finished_at=finished_at,
            )

        errors: list[str] = []
        evidence: list[Evidence] = [Evidence("benchmark_dir", str(bench_dir))]
        provenance_errors = _weekly_average_provenance_errors(config, bench_dir, ctx.scheme_id)
        provenance_errors.extend(_monthly_provenance_errors(config, bench_dir))
        if provenance_errors:
            errors.extend(provenance_errors)
            evidence.append(Evidence("benchmark_provenance_errors", provenance_errors))

        original_rows = _read_predictions_csv(sample_path) if sample_path.exists() else []
        current_rows = _current_platform_predictions(ctx)

        comparison: dict[str, Any] = {}

        if sample_path.exists():
            pred_diff = _compare_predictions(
                original_rows,
                current_rows,
                strict=benchmark_required,
                strict_fields=strict_fields,
                key_fields=key_fields,
                exact_match_fields=exact_match_fields,
                expected_exact_values=expected_exact_values,
                required_internal_fields=required_internal_fields,
            )
            comparison["predictions"] = pred_diff
            evidence.append(Evidence("total_record_count_original", pred_diff["total_original"]))
            evidence.append(Evidence("total_record_count_current", pred_diff["total_current"]))
            evidence.append(Evidence("direction_match_rate", pred_diff["direction_match_rate"]))
            evidence.append(Evidence("missing_keys", pred_diff["missing_keys"]))
            evidence.append(Evidence("extra_keys", pred_diff["extra_keys"]))
            evidence.append(Evidence("max_confidence_abs_diff", pred_diff["max_confidence_abs_diff"]))
            evidence.append(Evidence("mean_confidence_abs_diff", pred_diff["mean_confidence_abs_diff"]))
            evidence.append(Evidence("internal_fields", pred_diff["internal_fields"]))
            evidence.append(Evidence("required_internal_fields", required_internal_fields))
            evidence.append(Evidence("internal_mismatch_count", pred_diff["internal_mismatch_count"]))
            evidence.append(Evidence("max_internal_abs_diff", pred_diff["max_internal_abs_diff"]))
            evidence.append(Evidence("strict_prediction_key", benchmark_required))
            evidence.append(Evidence("validation_error_count", len(pred_diff["validation_errors"])))
            evidence.append(Evidence("strict_value_mismatch_count", len(pred_diff["strict_value_mismatches"])))
            evidence.append(Evidence("duplicate_key_error_count", len(pred_diff["duplicate_key_errors"])))
            evidence.append(Evidence("required_internal_error_count", len(pred_diff["required_internal_errors"])))

            if pred_diff["validation_errors"]:
                errors.append(
                    "missing required benchmark columns/values: "
                    f"{len(pred_diff['validation_errors'])}"
                )
            if pred_diff["strict_value_mismatches"]:
                errors.append(
                    "strict benchmark value mismatches: "
                    f"{len(pred_diff['strict_value_mismatches'])}"
                )
            if pred_diff["duplicate_key_errors"]:
                errors.append(
                    "duplicate strict benchmark keys: "
                    f"{len(pred_diff['duplicate_key_errors'])}"
                )
            if pred_diff["required_internal_errors"]:
                errors.append(
                    "missing required internal benchmark columns/values: "
                    f"{len(pred_diff['required_internal_errors'])}"
                )
            if pred_diff["total_current"] != pred_diff["total_original"]:
                errors.append(
                    f"record count mismatch: original={pred_diff['total_original']}, "
                    f"current={pred_diff['total_current']}"
                )
            if pred_diff["missing_count"] != 0:
                errors.append(f"missing dates/tenors: {pred_diff['missing_count']}")
            if pred_diff["extra_count"] != 0:
                errors.append(f"extra dates/tenors: {pred_diff['extra_count']}")
            if pred_diff["comparable_count"] > 0 and pred_diff["direction_match_rate"] < DIRECTION_MATCH_RATE_REQUIRED:
                errors.append(
                    f"direction_match_rate {pred_diff['direction_match_rate']:.6f} < required 1.0"
                )
            if pred_diff["max_confidence_abs_diff"] > MAX_CONFIDENCE_ABS_DIFF:
                errors.append(
                    f"max_confidence_abs_diff {pred_diff['max_confidence_abs_diff']:.3e} > "
                    f"{MAX_CONFIDENCE_ABS_DIFF:.0e}"
                )
            if pred_diff["internal_mismatch_count"] > 0:
                errors.append(
                    "internal benchmark field mismatch: "
                    f"{pred_diff['internal_mismatch_count']}"
                )
            if pred_diff["max_internal_abs_diff"] > MAX_INTERNAL_ABS_DIFF:
                errors.append(
                    f"max_internal_abs_diff {pred_diff['max_internal_abs_diff']:.3e} > "
                    f"{MAX_INTERNAL_ABS_DIFF:.0e}"
                )

        metric_diffs: list[dict[str, Any]] = []
        if summary_path.exists():
            original_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            current_summary = _current_backtest_summary(ctx)
            metric_diffs = _compare_metrics(original_summary, current_summary)
            comparison["metrics"] = metric_diffs
            evidence.append(Evidence("metric_diff_count", len(metric_diffs)))
            for item in metric_diffs:
                if item["abs_diff"] > METRIC_ACCURACY_ABS_DIFF:
                    errors.append(
                        f"metric {item['key']} abs_diff {item['abs_diff']:.6f} > {METRIC_ACCURACY_ABS_DIFF}"
                    )

        # 写出对比产物
        ctx.report_dir.mkdir(parents=True, exist_ok=True)
        summary_out = ctx.report_dir / "comparison_summary.json"
        summary_out.write_text(
            json.dumps(
                {"errors": errors, "comparison": comparison},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        diff_out = ctx.report_dir / "comparison_diff.csv"
        _write_diff_csv(diff_out, comparison)
        evidence.append(Evidence("comparison_summary_path", str(summary_out)))
        evidence.append(Evidence("comparison_diff_path", str(diff_out)))

        finished_at = utc_now()
        status = GateStatus.PASSED if not errors else GateStatus.FAILED
        return GateResult(
            gate_name=self.name,
            status=status,
            passed=status == GateStatus.PASSED,
            evidence=evidence,
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
            report_path=summary_out,
        )


def _read_predictions_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _strict_prediction_fields(config: dict[str, Any]) -> tuple[str, ...]:
    frequency = str(config.get("frequency") or "").strip()
    task_type = str(config.get("task_type") or "").strip()
    if task_type == "weekly_average":
        return WEEKLY_AVERAGE_STRICT_PREDICTION_FIELDS
    if frequency == "monthly" or task_type == "monthly":
        return MONTHLY_STRICT_PREDICTION_FIELDS
    if frequency == "weekly" or task_type.startswith("weekly_"):
        return WEEKLY_STRICT_PREDICTION_FIELDS
    return STRICT_PREDICTION_FIELDS


def _strict_key_fields(config: dict[str, Any]) -> tuple[str, ...]:
    frequency = str(config.get("frequency") or "").strip()
    task_type = str(config.get("task_type") or "").strip()
    if frequency == "monthly" or task_type == "monthly":
        return MONTHLY_STRICT_KEY_FIELDS
    if frequency == "weekly" or task_type.startswith("weekly_"):
        return WEEKLY_STRICT_KEY_FIELDS
    return STRICT_KEY_FIELDS


def _exact_match_fields(config: dict[str, Any]) -> tuple[str, ...]:
    task_type = str(config.get("task_type") or "").strip()
    if task_type in {"weekly_average", "monthly"}:
        return ("target_rule",)
    return ()


def _expected_exact_values(config: dict[str, Any]) -> dict[str, str]:
    task_type = str(config.get("task_type") or "").strip()
    if task_type not in {"weekly_average", "monthly"}:
        return {}
    target_rule = str(config.get("target_rule") or "").strip()
    return {"target_rule": target_rule} if target_rule else {}


def _weekly_average_provenance_errors(
    config: dict[str, Any],
    bench_dir: Path,
    scheme_id: str,
) -> list[str]:
    task_type = str(config.get("task_type") or "").strip()
    if task_type != "weekly_average":
        return []

    errors: list[str] = []
    expected_roles = {
        "original_backtest_summary.json": {WEEKLY_AVERAGE_SOURCE_ROLE},
        "current_backtest_summary.json": {PLATFORM_CURRENT_SOURCE_ROLE, "platform_current_weekly_average_runner"},
    }
    for summary_name, allowed_roles in expected_roles.items():
        summary_path = bench_dir / summary_name
        if not summary_path.exists():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"weekly average benchmark provenance unreadable: {summary_name}: {exc}")
            continue
        provenance = summary.get("benchmark_provenance")
        if not isinstance(provenance, dict):
            errors.append(f"weekly average benchmark provenance missing: {summary_name}")
            continue
        if is_point_backed_weekly_average_provenance(provenance, scheme_id=scheme_id):
            errors.append(f"weekly average benchmark provenance is point-backed: {summary_name}")
            continue
        source_role = str(provenance.get("source_role") or "").strip()
        if source_role not in allowed_roles:
            errors.append(
                "weekly average benchmark provenance source_role invalid: "
                f"{summary_name}: {source_role!r}"
            )
    return errors


def _monthly_provenance_errors(config: dict[str, Any], bench_dir: Path) -> list[str]:
    task_type = str(config.get("task_type") or "").strip()
    if task_type != "monthly":
        return []

    errors: list[str] = []
    expected_roles = {
        "original_backtest_summary.json": {MONTHLY_SOURCE_ROLE},
        "current_backtest_summary.json": {PLATFORM_CURRENT_MONTHLY_ROLE},
    }
    banned_tokens = ("point_runner", "point_scheme_id", "weekly_point", "weekly_*", "source_backed_point")
    for summary_name, allowed_roles in expected_roles.items():
        summary_path = bench_dir / summary_name
        if not summary_path.exists():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"monthly benchmark provenance unreadable: {summary_name}: {exc}")
            continue
        provenance = summary.get("benchmark_provenance")
        if not isinstance(provenance, dict):
            errors.append(f"monthly benchmark provenance missing: {summary_name}")
            continue
        source_role = str(provenance.get("source_role") or "").strip()
        if source_role not in allowed_roles:
            errors.append(
                "monthly benchmark provenance source_role invalid: "
                f"{summary_name}: {source_role!r}"
            )
        rendered = json.dumps(provenance, ensure_ascii=False).lower()
        for token in banned_tokens:
            if token in rendered:
                errors.append(f"monthly benchmark provenance contains banned point/weekly token: {summary_name}: {token}")
                break
    return errors


def _required_internal_fields(backtest_config: dict[str, Any]) -> list[str]:
    raw = backtest_config.get("required_internal_fields")
    if not isinstance(raw, list):
        return []
    return [str(field) for field in raw if str(field).strip()]


def _row_key(
    row: dict[str, Any],
    *,
    strict: bool = False,
    key_fields: tuple[str, ...] = STRICT_KEY_FIELDS,
) -> tuple[str, ...]:
    if strict:
        return tuple(str(row.get(key) or "").strip() for key in key_fields)
    date = str(row.get("predict_date") or row.get("date") or "").strip()
    tenor = str(row.get("tenor") or "").strip()
    return date, tenor


def _row_direction(row: dict[str, Any]) -> str:
    return str(row.get("direction") or row.get("pred_direction") or "").strip()


def _row_confidence(row: dict[str, Any]) -> float | None:
    raw = row.get("confidence")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _compare_predictions(
    original: list[dict[str, Any]],
    current: list[dict[str, Any]],
    *,
    strict: bool = False,
    strict_fields: tuple[str, ...] = STRICT_PREDICTION_FIELDS,
    key_fields: tuple[str, ...] = STRICT_KEY_FIELDS,
    exact_match_fields: tuple[str, ...] = (),
    expected_exact_values: dict[str, str] | None = None,
    required_internal_fields: list[str] | None = None,
) -> dict[str, Any]:
    required_internal_fields = required_internal_fields or []
    expected_exact_values = expected_exact_values or {}
    validation_errors = (
        _validate_strict_prediction_rows("original", original, strict_fields)
        + _validate_strict_prediction_rows("current", current, strict_fields)
        if strict
        else []
    )
    strict_value_mismatches = (
        _validate_expected_exact_fields("original", original, expected_exact_values)
        + _validate_expected_exact_fields("current", current, expected_exact_values)
        if strict
        else []
    )
    required_internal_errors = (
        _validate_required_internal_fields("original", original, required_internal_fields)
        + _validate_required_internal_fields("current", current, required_internal_fields)
        if strict
        else []
    )
    duplicate_key_errors = (
        _duplicate_key_errors("original", original, key_fields)
        + _duplicate_key_errors("current", current, key_fields)
        if strict
        else []
    )
    original_map = {_row_key(row, strict=strict, key_fields=key_fields): row for row in original}
    current_map = {_row_key(row, strict=strict, key_fields=key_fields): row for row in current}

    original_keys = set(original_map)
    current_keys = set(current_map)
    missing = sorted(original_keys - current_keys)
    extra = sorted(current_keys - original_keys)

    comparable = sorted(original_keys & current_keys)
    direction_matches = 0
    max_conf_diff = 0.0
    conf_diffs: list[float] = []
    internal_fields = _internal_fields(original, current, required_internal_fields)
    max_internal_diff = 0.0
    internal_mismatches: list[dict[str, Any]] = []
    per_key: list[dict[str, Any]] = []
    for key in comparable:
        o_dir = _row_direction(original_map[key])
        c_dir = _row_direction(current_map[key])
        dir_match = o_dir == c_dir
        if dir_match:
            direction_matches += 1
        o_conf = _row_confidence(original_map[key])
        c_conf = _row_confidence(current_map[key])
        conf_diff = None
        if o_conf is not None and c_conf is not None:
            conf_diff = abs(o_conf - c_conf)
            conf_diffs.append(conf_diff)
            max_conf_diff = max(max_conf_diff, conf_diff)
        key_internal_mismatches: list[dict[str, Any]] = []
        key_strict_value_mismatches: list[dict[str, Any]] = []
        if strict:
            for field in exact_match_fields:
                mismatch = _compare_exact_field(field, original_map[key], current_map[key])
                if mismatch is None:
                    continue
                key_mismatch = {"key": list(key), **mismatch}
                key_strict_value_mismatches.append(key_mismatch)
                strict_value_mismatches.append(key_mismatch)
        for field in internal_fields:
            mismatch = _compare_internal_field(field, original_map[key], current_map[key])
            if mismatch is None:
                continue
            if mismatch.get("abs_diff") is not None:
                max_internal_diff = max(max_internal_diff, float(mismatch["abs_diff"]))
            key_mismatch = {"key": list(key), **mismatch}
            key_internal_mismatches.append(key_mismatch)
            internal_mismatches.append(key_mismatch)
        per_key.append(
            {
                "key": list(key),
                "original_direction": o_dir,
                "current_direction": c_dir,
                "direction_match": dir_match,
                "confidence_abs_diff": conf_diff,
                "strict_value_mismatches": key_strict_value_mismatches,
                "internal_mismatches": key_internal_mismatches,
            }
        )

    rate = (direction_matches / len(comparable)) if comparable else 1.0
    mean_conf_diff = (sum(conf_diffs) / len(conf_diffs)) if conf_diffs else 0.0

    return {
        "total_original": len(original),
        "total_current": len(current),
        "comparable_count": len(comparable),
        "missing_keys": [list(k) for k in missing],
        "extra_keys": [list(k) for k in extra],
        "missing_count": len(missing),
        "extra_count": len(extra),
        "direction_matches": direction_matches,
        "direction_match_rate": rate,
        "max_confidence_abs_diff": max_conf_diff,
        "mean_confidence_abs_diff": mean_conf_diff,
        "internal_fields": internal_fields,
        "internal_mismatch_count": len(internal_mismatches),
        "internal_mismatches": internal_mismatches,
        "max_internal_abs_diff": max_internal_diff,
        "validation_errors": validation_errors,
        "strict_value_mismatches": strict_value_mismatches,
        "duplicate_key_errors": duplicate_key_errors,
        "required_internal_errors": required_internal_errors,
        "per_key": per_key,
    }


def _internal_fields(
    original: list[dict[str, Any]],
    current: list[dict[str, Any]],
    required_internal_fields: list[str] | None = None,
) -> list[str]:
    fields: set[str] = set(required_internal_fields or [])
    strict_prediction_fields = set(
        STRICT_PREDICTION_FIELDS
        + WEEKLY_STRICT_PREDICTION_FIELDS
        + WEEKLY_AVERAGE_STRICT_PREDICTION_FIELDS
        + MONTHLY_STRICT_PREDICTION_FIELDS
    )
    for row in [*original, *current]:
        for field in row:
            if field in strict_prediction_fields:
                continue
            if field in {"predict_date", "date", "tenor", "pred_direction", "direction", "confidence"}:
                continue
            if field in INTERNAL_SCORE_FIELDS or field.endswith(INTERNAL_NUMERIC_SUFFIXES + INTERNAL_DIRECTION_SUFFIXES):
                fields.add(field)
    return sorted(fields)


def _compare_internal_field(field: str, original: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
    o_raw = original.get(field)
    c_raw = current.get(field)
    if _is_blank(o_raw) and _is_blank(c_raw):
        return None
    if _is_blank(o_raw) or _is_blank(c_raw):
        return {
            "field": field,
            "original": o_raw,
            "current": c_raw,
            "abs_diff": None,
            "match": False,
            "reason": "missing_internal_value",
        }
    if _is_internal_numeric_field(field):
        o_val = _float_or_none(o_raw)
        c_val = _float_or_none(c_raw)
        if o_val is None or c_val is None:
            match = str(o_raw).strip() == str(c_raw).strip()
            return None if match else {
                "field": field,
                "original": o_raw,
                "current": c_raw,
                "abs_diff": None,
                "match": False,
                "reason": "non_numeric_internal_value",
            }
        abs_diff = abs(o_val - c_val)
        if abs_diff <= MAX_INTERNAL_ABS_DIFF:
            return None
        return {
            "field": field,
            "original": o_val,
            "current": c_val,
            "abs_diff": abs_diff,
            "match": False,
            "reason": "internal_numeric_diff",
        }
    match = str(o_raw).strip() == str(c_raw).strip()
    if match:
        return None
    return {
        "field": field,
        "original": str(o_raw).strip(),
        "current": str(c_raw).strip(),
        "abs_diff": None,
        "match": False,
        "reason": "internal_value_diff",
    }


def _compare_exact_field(field: str, original: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
    o_raw = original.get(field)
    c_raw = current.get(field)
    if _is_blank(o_raw) and _is_blank(c_raw):
        return None
    match = str(o_raw).strip() == str(c_raw).strip()
    if match:
        return None
    return {
        "field": field,
        "original": "" if o_raw is None else str(o_raw).strip(),
        "current": "" if c_raw is None else str(c_raw).strip(),
        "match": False,
        "reason": "strict_value_diff",
    }


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _is_internal_numeric_field(field: str) -> bool:
    return field in INTERNAL_SCORE_FIELDS or field in INTERNAL_NUMERIC_FIELDS or field.endswith(INTERNAL_NUMERIC_SUFFIXES)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_strict_prediction_rows(
    source: str,
    rows: list[dict[str, Any]],
    strict_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):
        missing = [field for field in strict_fields if _is_blank(row.get(field))]
        if missing:
            errors.append({"source": source, "row_number": index, "missing": missing})
    return errors


def _validate_required_internal_fields(
    source: str,
    rows: list[dict[str, Any]],
    required_internal_fields: list[str],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not required_internal_fields:
        return errors
    for index, row in enumerate(rows, start=2):
        missing = [field for field in required_internal_fields if _is_blank(row.get(field))]
        if missing:
            errors.append({"source": source, "row_number": index, "missing": missing})
    return errors


def _validate_expected_exact_fields(
    source: str,
    rows: list[dict[str, Any]],
    expected_values: dict[str, str],
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not expected_values:
        return errors
    for index, row in enumerate(rows, start=2):
        for field, expected in expected_values.items():
            actual = "" if row.get(field) is None else str(row.get(field)).strip()
            if actual != expected:
                errors.append(
                    {
                        "source": source,
                        "row_number": index,
                        "field": field,
                        "expected": expected,
                        "actual": actual,
                        "reason": "unexpected_strict_value",
                    }
                )
    return errors


def _duplicate_key_errors(
    source: str,
    rows: list[dict[str, Any]],
    key_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    seen: dict[tuple[str, ...], int] = {}
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=2):
        key = _row_key(row, strict=True, key_fields=key_fields)
        first = seen.get(key)
        if first is not None:
            errors.append(
                {
                    "source": source,
                    "row_number": index,
                    "first_row_number": first,
                    "key": list(key),
                }
            )
            continue
        seen[key] = index
    return errors


def _compare_metrics(original: Any, current: Any) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    original_flat = _flatten_numbers(original)
    current_flat = _flatten_numbers(current)
    for key in sorted(set(original_flat) | set(current_flat)):
        o_val = original_flat.get(key)
        c_val = current_flat.get(key)
        if o_val is None or c_val is None:
            diffs.append(
                {
                    "key": key,
                    "original": o_val,
                    "current": c_val,
                    "abs_diff": math.inf,
                }
            )
            continue
        diffs.append(
            {
                "key": key,
                "original": o_val,
                "current": c_val,
                "abs_diff": abs(o_val - c_val),
            }
        )
    return diffs


def _flatten_numbers(obj: Any, prefix: str = "") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten_numbers(value, child))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            child = f"{prefix}[{index}]"
            out.update(_flatten_numbers(value, child))
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        out[prefix] = float(obj)
    return out


def _write_diff_csv(path: Path, comparison: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    predictions = comparison.get("predictions")
    if isinstance(predictions, dict):
        for item in predictions.get("per_key", []):
            key = item.get("key", ["", ""])
            rows.append(
                {
                    "section": "prediction",
                    "key": "/".join(str(part) for part in key),
                    "original": item.get("original_direction"),
                    "current": item.get("current_direction"),
                    "abs_diff": item.get("confidence_abs_diff"),
                    "match": item.get("direction_match"),
                }
            )
            for mismatch in item.get("internal_mismatches", []):
                rows.append(
                    {
                        "section": "prediction_internal",
                        "key": "/".join(str(part) for part in key) + f"/{mismatch.get('field')}",
                        "original": mismatch.get("original"),
                        "current": mismatch.get("current"),
                        "abs_diff": mismatch.get("abs_diff"),
                        "match": mismatch.get("match"),
                    }
                )
    for item in comparison.get("metrics", []):
        rows.append(
            {
                "section": "metric",
                "key": item.get("key"),
                "original": item.get("original"),
                "current": item.get("current"),
                "abs_diff": item.get("abs_diff"),
                "match": item.get("abs_diff", 0) <= METRIC_ACCURACY_ABS_DIFF,
            }
        )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["section", "key", "original", "current", "abs_diff", "match"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _current_platform_predictions(ctx: GateContext) -> list[dict[str, Any]]:
    """读取当前平台预测输出。优先复用 backtest 产物，缺失时返回空。

    钩子点：未来可对接 input_artifacts / scheduler 输出。当前默认读取
    schemes/{scheme_id}/benchmarks/current_predictions_sample.csv（若由上游 gate 写出）。
    """
    candidate = ctx.project_root / "schemes" / ctx.scheme_id / "benchmarks" / "current_predictions_sample.csv"
    if candidate.exists():
        return _read_predictions_csv(candidate)
    return []


def _current_backtest_summary(ctx: GateContext) -> dict[str, Any]:
    candidate = ctx.project_root / "schemes" / ctx.scheme_id / "benchmarks" / "current_backtest_summary.json"
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return {}
