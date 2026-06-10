from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from harness.context import GateContext
from harness.gates.base import Gate, guarded_result, utc_now
from harness.result import Evidence, GateResult, GateStatus


# 容差（PINNED）
DIRECTION_MATCH_RATE_REQUIRED = 1.0
MAX_CONFIDENCE_ABS_DIFF = 1e-8
METRIC_ACCURACY_ABS_DIFF = 0.001


class CompareGate(Gate):
    """对比当前平台输出与方案 benchmark 原始输出（PINNED stage="compare"）。"""

    name = "compare"

    def __init__(self, context: GateContext | None = None) -> None:
        self._context = context

    def run(self, ctx: GateContext | None = None) -> GateResult:
        context = ctx if ctx is not None else self._context
        if context is None:
            raise ValueError("CompareGate.run requires a GateContext")
        return guarded_result(self.name, lambda started_at: self._run(context, started_at))

    def _run(self, ctx: GateContext, started_at: str) -> GateResult:
        bench_dir = ctx.project_root / "schemes" / ctx.scheme_id / "benchmarks"
        sample_path = bench_dir / "original_predictions_sample.csv"
        summary_path = bench_dir / "original_backtest_summary.json"

        # 读取配置判断 benchmark 是否为必需
        config = _load_config(ctx.project_root / "schemes" / ctx.scheme_id / "config.yaml")
        benchmark_required = bool(config.get("backtest", {}).get("benchmark_required", False))

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

        original_rows = _read_predictions_csv(sample_path) if sample_path.exists() else []
        current_rows = _current_platform_predictions(ctx)

        comparison: dict[str, Any] = {}

        if sample_path.exists():
            pred_diff = _compare_predictions(original_rows, current_rows)
            comparison["predictions"] = pred_diff
            evidence.append(Evidence("total_record_count_original", pred_diff["total_original"]))
            evidence.append(Evidence("total_record_count_current", pred_diff["total_current"]))
            evidence.append(Evidence("direction_match_rate", pred_diff["direction_match_rate"]))
            evidence.append(Evidence("missing_keys", pred_diff["missing_keys"]))
            evidence.append(Evidence("extra_keys", pred_diff["extra_keys"]))
            evidence.append(Evidence("max_confidence_abs_diff", pred_diff["max_confidence_abs_diff"]))
            evidence.append(Evidence("mean_confidence_abs_diff", pred_diff["mean_confidence_abs_diff"]))

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


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
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
) -> dict[str, Any]:
    original_map = {_row_key(row): row for row in original}
    current_map = {_row_key(row): row for row in current}

    original_keys = set(original_map)
    current_keys = set(current_map)
    missing = sorted(original_keys - current_keys)
    extra = sorted(current_keys - original_keys)

    comparable = sorted(original_keys & current_keys)
    direction_matches = 0
    max_conf_diff = 0.0
    conf_diffs: list[float] = []
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
        per_key.append(
            {
                "key": list(key),
                "original_direction": o_dir,
                "current_direction": c_dir,
                "direction_match": dir_match,
                "confidence_abs_diff": conf_diff,
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
        "per_key": per_key,
    }


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


def _load_config(config_path: Path) -> dict[str, Any]:
    """读取 config.yaml 返回字典；失败时返回空 dict 不阻断 gate。"""
    try:
        import yaml
        if config_path.exists():
            return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}
