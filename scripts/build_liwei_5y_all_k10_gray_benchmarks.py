#!/usr/bin/env python3
"""从 prod_screen clean cache 构建三套 5Y ALL_K10 严格 benchmark。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.calendar_service import get_calendar
from shared.input_artifacts import (
    build_daily_input_artifact,
    build_monthly_input_artifact,
    build_weekly_input_artifact,
    create_input_engine,
)


BASELINES = ("STD", "DIV", "ACCWT", "CROSS_7Y")
BENCHMARK_ROLE = "historical/source-configured-experiment"
MODEL_SCOPE = "experimental/platform_live_pit_variant"
HORIZON = 5
TARGET_TENOR = "5Y"
DEFAULT_FEATURE_START = "2026-05-06"
DEFAULT_FEATURE_END = "2026-05-21"
DEFAULT_TARGET_CUTOFF = "2026-06-01"
DEFAULT_INPUT_START = "2010-07-27"
EXACT_TOLERANCE = 1e-8

BENCHMARK_COLUMNS = (
    "feature_date",
    "target_date",
    "target_tenor",
    "horizon",
    "benchmark_role",
    "direction",
    "confidence",
    "label",
    "is_correct",
    "vote_score",
    "STD_score",
    "STD_dir",
    "DIV_score",
    "DIV_dir",
    "ACCWT_score",
    "ACCWT_dir",
    "CROSS_7Y_score",
    "CROSS_7Y_dir",
)


@dataclass(frozen=True)
class VariantSpec:
    """一套 5Y ALL_K10 特征筛选实验身份。"""

    scheme_id: str
    metric: str
    rebal: str

    @property
    def source_group(self) -> str:
        return f"{self.metric}_{self.rebal}"


@dataclass(frozen=True)
class ArtifactBundle:
    """只由 shared.input_artifacts 产出的平台模型输入。"""

    daily: Any
    weekly: Any
    monthly: Any
    date_to_week: Mapping[str, int | str]


SPECS = (
    VariantSpec(
        "liwei_0616_5y_auc_static_all_k3_div_k10",
        "auc",
        "static",
    ),
    VariantSpec(
        "liwei_0616_5y_auc_yearly_all_k3_div_k10",
        "auc",
        "yearly",
    ),
    VariantSpec(
        "liwei_0616_5y_ic_yearly_all_k3_div_k10",
        "ic",
        "yearly",
    ),
)


def build_all_k10_prediction(signs: Mapping[str, np.ndarray]) -> np.ndarray:
    """执行 ALL k=3 共识，再执行 DIV 连续同向 K=10 打断。"""
    missing = [name for name in BASELINES if name not in signs]
    if missing:
        raise RuntimeError(f"missing ALL_K10 baseline signs: {missing}")
    arrays = {name: np.asarray(signs[name], dtype=np.int32) for name in BASELINES}
    sizes = {len(values) for values in arrays.values()}
    if len(sizes) != 1:
        raise RuntimeError(f"ALL_K10 baseline length mismatch: {sorted(sizes)}")
    votes = np.column_stack([arrays[name] for name in BASELINES])
    prediction = np.zeros(len(votes), dtype=np.int32)
    prediction[(votes > 0).sum(axis=1) >= 3] = 1
    prediction[(votes < 0).sum(axis=1) >= 3] = -1

    output = prediction.copy()
    streak = 0
    last_direction = 0
    fallback = arrays["DIV"]
    for index, current in enumerate(prediction):
        if current != 0 and current == last_direction:
            streak += 1
        elif current != 0:
            streak = 1
            last_direction = int(current)
        if streak > 10 and fallback[index] != 0:
            output[index] = int(fallback[index])
            if fallback[index] != last_direction:
                streak = 0
    return output


def convert_source_lgbm_cache(
    source_cache: Mapping[str, Any],
    lgbm_grid: list[dict[str, Any]],
) -> dict[str, Any]:
    """把 prod_screen 矩阵 cache 无损转换为平台 Phase-A cache。"""
    required = {"test_dates", "preds", "probs", "valid", "n_configs"}
    missing = sorted(required - set(source_cache))
    if missing:
        raise RuntimeError(f"source lgbm cache missing keys: {missing}")
    n_configs = int(source_cache["n_configs"])
    if n_configs != len(lgbm_grid):
        raise RuntimeError(
            f"source/platform LGBM grid size mismatch: source={n_configs}, platform={len(lgbm_grid)}"
        )
    dates = pd.DatetimeIndex(pd.to_datetime(source_cache["test_dates"], errors="coerce"))
    if dates.isna().any() or dates.duplicated().any():
        raise RuntimeError("source lgbm cache has invalid or duplicate test_dates")
    date_strings = dates.strftime("%Y-%m-%d").tolist()
    preds = np.asarray(source_cache["preds"])
    probs = np.asarray(source_cache["probs"])
    valid = np.asarray(source_cache["valid"], dtype=bool)
    expected_shape = (n_configs, len(date_strings))
    if preds.shape != expected_shape or probs.shape != expected_shape:
        raise RuntimeError(
            "source lgbm cache matrix shape mismatch: "
            f"expected={expected_shape}, preds={preds.shape}, probs={probs.shape}"
        )
    if valid.shape != (n_configs,):
        raise RuntimeError(
            f"source lgbm cache valid shape mismatch: expected={(n_configs,)}, actual={valid.shape}"
        )
    if not np.isfinite(probs[valid]).all():
        raise RuntimeError("source lgbm cache contains non-finite valid probabilities")
    results = []
    for index, is_valid in enumerate(valid):
        if not is_valid:
            continue
        results.append(
            {
                "config": dict(lgbm_grid[index]),
                "preds": np.asarray(preds[index], dtype=np.int32).copy(),
                "probs": np.asarray(probs[index], dtype=np.float64).copy(),
            }
        )
    if not results:
        raise RuntimeError("source lgbm cache has no valid configs")
    return {"test_dates": date_strings, "results": results}


def build_original_rows(
    contexts: Mapping[str, Mapping[str, Any]],
    *,
    calendar: Any,
    feature_start: str,
    feature_end: str,
    target_cutoff: str,
) -> pd.DataFrame:
    """从 clean source context 构建按真实 T+5 对齐的 original 行。"""
    dates, labels, scores = _validated_source_series(contexts)
    signs = {name: np.sign(values).astype(np.int32) for name, values in scores.items()}
    prediction = build_all_k10_prediction(signs)
    vote_score = np.mean(np.column_stack([scores[name] for name in BASELINES]), axis=1)
    rows: list[dict[str, Any]] = []
    for index, feature_date in enumerate(dates):
        if feature_date < feature_start or feature_date > feature_end:
            continue
        target_date = str(calendar.nth_trading_day_after(feature_date, HORIZON))
        if target_date >= target_cutoff:
            continue
        direction = int(prediction[index])
        label = int(labels[index])
        row = _base_row(feature_date, target_date, direction, label, float(vote_score[index]))
        for baseline in BASELINES:
            row[f"{baseline}_score"] = float(scores[baseline][index])
            row[f"{baseline}_dir"] = int(signs[baseline][index])
        rows.append(row)
    return _strict_frame(rows, source="original")


def build_current_rows(
    detail: pd.DataFrame,
    *,
    calendar: Any,
    feature_start: str,
    feature_end: str,
    target_cutoff: str,
) -> pd.DataFrame:
    """把平台 core 的实际 run_window 输出转换成严格 current 行。"""
    rows: list[dict[str, Any]] = []
    for record in detail.to_dict(orient="records"):
        feature_date = _date_string(record.get("anchor_date"))
        if feature_date < feature_start or feature_date > feature_end:
            continue
        target_date = str(calendar.nth_trading_day_after(feature_date, HORIZON))
        if target_date >= target_cutoff:
            continue
        direction = int(record["prediction"])
        label = int(record["true_label"])
        score_map = record.get("baseline_scores")
        sign_map = record.get("baseline_signs")
        if not isinstance(score_map, Mapping) or not isinstance(sign_map, Mapping):
            raise RuntimeError(f"platform core row missing baseline maps: {feature_date}")
        row = _base_row(
            feature_date,
            target_date,
            direction,
            label,
            float(record["vote_score"]),
            confidence=float(record.get("confidence", abs(direction))),
        )
        for baseline in BASELINES:
            if baseline not in score_map or baseline not in sign_map:
                raise RuntimeError(f"platform core row missing {baseline} internals: {feature_date}")
            row[f"{baseline}_score"] = float(score_map[baseline])
            row[f"{baseline}_dir"] = int(sign_map[baseline])
        rows.append(row)
    return _strict_frame(rows, source="current")


def assert_exact_rows(
    original: pd.DataFrame,
    current: pd.DataFrame,
    *,
    tolerance: float = EXACT_TOLERANCE,
) -> None:
    """逐行比较 source/current；任一内部数值超容差即 fail-closed。"""
    for source, frame in (("original", original), ("current", current)):
        if tuple(frame.columns) != BENCHMARK_COLUMNS:
            raise RuntimeError(
                f"{source} benchmark schema mismatch: expected={BENCHMARK_COLUMNS}, actual={tuple(frame.columns)}"
            )
    key_fields = ("feature_date", "target_date", "target_tenor", "horizon", "benchmark_role")
    left = original.sort_values(list(key_fields)).reset_index(drop=True)
    right = current.sort_values(list(key_fields)).reset_index(drop=True)
    if len(left) != len(right):
        raise RuntimeError(f"benchmark row count mismatch: original={len(left)}, current={len(right)}")
    if left.empty:
        raise RuntimeError("benchmark comparison has no rows")
    exact_fields = (
        *key_fields,
        "direction",
        "label",
        "is_correct",
        "STD_dir",
        "DIV_dir",
        "ACCWT_dir",
        "CROSS_7Y_dir",
    )
    for field in exact_fields:
        mismatch = left[field].astype(str) != right[field].astype(str)
        if mismatch.any():
            index = int(np.flatnonzero(mismatch.to_numpy())[0])
            raise RuntimeError(
                f"strict benchmark mismatch field={field} key={tuple(left.loc[index, list(key_fields)])}: "
                f"original={left.loc[index, field]!r}, current={right.loc[index, field]!r}"
            )
    numeric_fields = (
        "confidence",
        "vote_score",
        "STD_score",
        "DIV_score",
        "ACCWT_score",
        "CROSS_7Y_score",
    )
    for field in numeric_fields:
        left_values = pd.to_numeric(left[field], errors="coerce").to_numpy(dtype=float)
        right_values = pd.to_numeric(right[field], errors="coerce").to_numpy(dtype=float)
        finite = np.isfinite(left_values) & np.isfinite(right_values)
        within = np.abs(left_values - right_values) <= tolerance
        mismatch = ~(finite & within)
        if mismatch.any():
            index = int(np.flatnonzero(mismatch)[0])
            raise RuntimeError(
                f"numeric benchmark mismatch field={field} key={tuple(left.loc[index, list(key_fields)])}: "
                f"original={left_values[index]!r}, current={right_values[index]!r}, "
                f"tolerance={tolerance}"
            )


def build_variant_from_source(
    spec: VariantSpec,
    *,
    source_group: Path,
    output_dir: Path,
    artifacts: ArtifactBundle,
    calendar: Any,
    core_module: ModuleType | Any,
    feature_start: str = DEFAULT_FEATURE_START,
    feature_end: str = DEFAULT_FEATURE_END,
    target_cutoff: str = DEFAULT_TARGET_CUTOFF,
    n_workers: int = 1,
) -> dict[str, Any]:
    """运行一套 source-configured experiment 并生成 CompareGate 证据。"""
    contexts, fingerprints = load_source_contexts(source_group)
    original = build_original_rows(
        contexts,
        calendar=calendar,
        feature_start=feature_start,
        feature_end=feature_end,
        target_cutoff=target_cutoff,
    )
    phase_a_caches: dict[str, dict[str, Any]] = {}
    for baseline in BASELINES:
        grid = _platform_lgbm_grid(core_module, baseline)
        phase_a_caches[baseline] = convert_source_lgbm_cache(
            contexts[baseline]["lgbm_cache"],
            grid,
        )

    source_dates = _context_date_strings(contexts["STD"])
    test_ranges = ((source_dates[0], source_dates[-1]),)
    detail = core_module.run_5y_all_for_feature_window(
        daily_df=artifacts.daily.dataframe,
        weekly_df=artifacts.weekly.dataframe,
        monthly_df=artifacts.monthly.dataframe,
        date_to_week=dict(artifacts.date_to_week),
        test_ranges=test_ranges,
        current_start=feature_start,
        current_end=feature_end,
        require_labels=True,
        n_workers=n_workers,
        phase_a_caches=phase_a_caches,
    )
    current = build_current_rows(
        detail,
        calendar=calendar,
        feature_start=feature_start,
        feature_end=feature_end,
        target_cutoff=target_cutoff,
    )
    assert_exact_rows(original, current)

    common_summary = _summary(
        spec,
        original,
        source_group=source_group,
        source_dates=source_dates,
        fingerprints=fingerprints,
        artifacts=artifacts,
    )
    original_summary = {
        **common_summary,
        "benchmark_provenance": {
            "source_role": "prod_screen_clean_source_pkl",
            "execution": "exact ALL_K10 consensus k=3 plus DIV streak_K=10 from source vs_full",
            "yearly_wiring_note": _yearly_wiring_note(spec),
        },
    }
    current_summary = {
        **common_summary,
        "benchmark_provenance": {
            "source_role": "platform_core_reexecution_from_source_phase_a_cache",
            "execution": "platform core run_5y_all_for_feature_window; current is not copied from original",
            "yearly_wiring_note": _yearly_wiring_note(spec),
        },
    }
    _write_bundle(
        output_dir,
        original=original,
        current=current,
        original_summary=original_summary,
        current_summary=current_summary,
        spec=spec,
    )
    return {
        "scheme_id": spec.scheme_id,
        "row_count": int(len(original)),
        "feature_start": str(original["feature_date"].min()),
        "feature_end": str(original["feature_date"].max()),
        "target_start": str(original["target_date"].min()),
        "target_end": str(original["target_date"].max()),
        "output_dir": str(output_dir),
    }


def load_source_contexts(
    source_group: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """加载并指纹化同一 clean source group 的四个 baseline pkl。"""
    contexts: dict[str, dict[str, Any]] = {}
    fingerprints: dict[str, str] = {}
    for baseline in BASELINES:
        path = source_group / f"{baseline}.pkl"
        if not path.is_file():
            raise RuntimeError(f"missing clean source cache: {path}")
        with path.open("rb") as handle:
            context = pickle.load(handle)
        if not isinstance(context, dict):
            raise RuntimeError(f"source cache is not a dict: {path}")
        required = {"final", "vs_full", "true_labels", "test_dates", "lgbm_cache"}
        missing = sorted(required - set(context))
        if missing:
            raise RuntimeError(f"source cache {path} missing keys: {missing}")
        contexts[baseline] = context
        fingerprints[baseline] = _sha256(path)
    _validated_source_series(contexts)
    return contexts, fingerprints


def build_artifact_bundle(
    spec: VariantSpec,
    *,
    calendar: Any,
    engine: Any,
    input_start: str,
    input_end: str,
    output_root: Path | None = None,
) -> ArtifactBundle:
    """只经 shared.input_artifacts 为平台 core 构建三频输入。"""
    common = {"engine": engine}
    if output_root is not None:
        common["output_root"] = output_root
    daily = build_daily_input_artifact(
        scheme_id=spec.scheme_id,
        predict_date=input_end,
        start_date=input_start,
        end_date=input_end,
        **common,
    )
    end_week = calendar.week_id_for_date(input_end)
    if end_week is None:
        raise RuntimeError(f"cannot resolve DB week_id for input_end={input_end}")
    weekly = build_weekly_input_artifact(
        scheme_id=spec.scheme_id,
        predict_date=input_end,
        end_week=int(end_week),
        as_of_date=input_end,
        **common,
    )
    monthly = build_monthly_input_artifact(
        scheme_id=spec.scheme_id,
        predict_date=input_end,
        start_date=input_start,
        end_date=input_end,
        **common,
    )
    dates = pd.to_datetime(daily.dataframe["date"], errors="coerce").dropna()
    date_to_week: dict[str, int | str] = {}
    for day in dates.dt.strftime("%Y-%m-%d").unique().tolist():
        week_id = calendar.week_id_for_date(day)
        if week_id is not None:
            date_to_week[str(day)] = int(week_id)
    return ArtifactBundle(daily=daily, weekly=weekly, monthly=monthly, date_to_week=date_to_week)


def read_json(path: Path) -> dict[str, Any]:
    """读取 builder 生成的 JSON，供聚焦验证和人工复核。"""
    return json.loads(path.read_text(encoding="utf-8"))


def _validated_source_series(
    contexts: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], np.ndarray, dict[str, np.ndarray]]:
    missing = [name for name in BASELINES if name not in contexts]
    if missing:
        raise RuntimeError(f"missing clean source baseline contexts: {missing}")
    reference_dates = _context_date_strings(contexts["STD"])
    labels = np.asarray(contexts["STD"]["true_labels"])
    if len(labels) != len(reference_dates) or not np.isin(labels, [-1, 0, 1]).all():
        raise RuntimeError("STD source labels are missing, invalid, or length-mismatched")
    scores: dict[str, np.ndarray] = {}
    for baseline in BASELINES:
        dates = _context_date_strings(contexts[baseline])
        if dates != reference_dates:
            raise RuntimeError(f"source baseline dates are not exactly aligned: {baseline}")
        values = np.asarray(contexts[baseline]["vs_full"], dtype=float)
        if values.shape != (len(reference_dates),) or not np.isfinite(values).all():
            raise RuntimeError(f"source baseline vs_full invalid: {baseline}")
        scores[baseline] = values
    return reference_dates, labels.astype(np.int32), scores


def _context_date_strings(context: Mapping[str, Any]) -> list[str]:
    dates = pd.DatetimeIndex(pd.to_datetime(context["test_dates"], errors="coerce"))
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise RuntimeError("source context test_dates must be valid, unique, and increasing")
    return dates.strftime("%Y-%m-%d").tolist()


def _platform_lgbm_grid(core_module: ModuleType | Any, baseline: str) -> list[dict[str, Any]]:
    cfg = core_module.model_config(baseline)
    return core_module.build_lgbm_grid(
        cfg["lgbm_windows"],
        cfg["lgbm_leaves"],
        cfg["lgbm_min_child"],
        cfg["lgbm_alpha"],
        cfg["lgbm_lambda"],
        cfg["lgbm_split"],
        slow_path=bool(cfg.get("lgbm_slow_path", False)),
        very_slow_path=bool(cfg.get("lgbm_very_slow_path", False)),
    )


def _base_row(
    feature_date: str,
    target_date: str,
    direction: int,
    label: int,
    vote_score: float,
    *,
    confidence: float | None = None,
) -> dict[str, Any]:
    return {
        "feature_date": feature_date,
        "target_date": target_date,
        "target_tenor": TARGET_TENOR,
        "horizon": HORIZON,
        "benchmark_role": BENCHMARK_ROLE,
        "direction": direction,
        "confidence": float(abs(direction) if confidence is None else confidence),
        "label": label,
        "is_correct": bool(direction != 0 and direction == label),
        "vote_score": vote_score,
    }


def _strict_frame(rows: list[dict[str, Any]], *, source: str) -> pd.DataFrame:
    if not rows:
        raise RuntimeError(f"{source} benchmark has no rows after target-date cutoff")
    frame = pd.DataFrame(rows)
    missing = [column for column in BENCHMARK_COLUMNS if column not in frame.columns]
    if missing:
        raise RuntimeError(f"{source} benchmark missing strict columns: {missing}")
    frame = frame.loc[:, BENCHMARK_COLUMNS].sort_values("feature_date").reset_index(drop=True)
    if frame.duplicated(
        ["feature_date", "target_date", "target_tenor", "horizon", "benchmark_role"]
    ).any():
        raise RuntimeError(f"{source} benchmark has duplicate strict keys")
    return frame


def _summary(
    spec: VariantSpec,
    rows: pd.DataFrame,
    *,
    source_group: Path,
    source_dates: list[str],
    fingerprints: dict[str, str],
    artifacts: ArtifactBundle,
) -> dict[str, Any]:
    traded = rows["direction"].astype(int) != 0
    correct = traded & (rows["direction"].astype(int) == rows["label"].astype(int))
    metric_samples = int(traded.sum())
    artifact_provenance = {}
    for name in ("daily", "weekly", "monthly"):
        artifact = getattr(artifacts, name)
        artifact_provenance[name] = {
            "path": str(artifact.path),
            "content_hash": str(artifact.content_hash),
            "source": str(artifact.source),
            "data_version": str(artifact.data_version),
        }
    return {
        "benchmark_id": spec.scheme_id,
        "scheme_id": spec.scheme_id,
        "target_tenor": TARGET_TENOR,
        "horizon": HORIZON,
        "benchmark_role": BENCHMARK_ROLE,
        "model_scope": MODEL_SCOPE,
        "screen_metric": spec.metric,
        "screen_rebal": spec.rebal,
        "vote_baselines": list(BASELINES),
        "fallback_baseline": "DIV",
        "streak_K": 10,
        "row_count": int(len(rows)),
        "samples": int(len(rows)),
        "metric_samples": metric_samples,
        "correct": int(correct.sum()),
        "accuracy": float(correct.sum() / metric_samples) if metric_samples else 0.0,
        "active_rate": float(metric_samples / len(rows)),
        "feature_start": str(rows["feature_date"].min()),
        "feature_end": str(rows["feature_date"].max()),
        "target_start": str(rows["target_date"].min()),
        "target_end": str(rows["target_date"].max()),
        "source_context_start": source_dates[0],
        "source_context_end": source_dates[-1],
        "source_cache_root": str(source_group),
        "source_cache_fingerprints": fingerprints,
        "input_artifacts": artifact_provenance,
        "trading_day_alignment": "shared.calendar_service DB calendar; target_date=T+5 trading days",
        "metric_month_alignment": "target_date",
        "flat_accuracy_policy": "prediction == 0 excluded",
        "numeric_tolerance": "1e-8 fail-closed",
    }


def _yearly_wiring_note(spec: VariantSpec) -> str:
    if spec.rebal != "yearly":
        return "not applicable: static screening"
    return (
        "prod_screen runner wf_ic wiring fix: "
        "wf_ic={'rebal':'yearly','cap':2000} supplied before clean cache generation"
    )


def _write_bundle(
    output_dir: Path,
    *,
    original: pd.DataFrame,
    current: pd.DataFrame,
    original_summary: dict[str, Any],
    current_summary: dict[str, Any],
    spec: VariantSpec,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    original.to_csv(output_dir / "original_predictions_sample.csv", index=False)
    current.to_csv(output_dir / "current_predictions_sample.csv", index=False)
    (output_dir / "original_backtest_summary.json").write_text(
        json.dumps(original_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "current_backtest_summary.json").write_text(
        json.dumps(current_summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    wiring = _yearly_wiring_note(spec)
    (output_dir / "README.md").write_text(
        "# 5Y ALL_K10 source-configured experiment benchmark\n\n"
        f"- Scheme: `{spec.scheme_id}`\n"
        f"- Screening: `{spec.metric}/{spec.rebal}`\n"
        f"- Role: `{BENCHMARK_ROLE}`\n"
        f"- Scope: `{MODEL_SCOPE}`\n"
        "- Original: exact ALL_K10 consensus and DIV_K10 streak-break from clean prod_screen pkl.\n"
        "- Current: actual platform core re-execution using strictly converted source Phase-A caches.\n"
        "- Alignment: DB trading calendar, target date is the fifth trading day after feature date.\n"
        "- Accuracy: flat predictions are excluded from the denominator.\n"
        f"- Yearly wiring: {wiring}\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date_string(value: Any) -> str:
    parsed = pd.Timestamp(value)
    if pd.isna(parsed):
        raise RuntimeError(f"invalid platform feature date: {value!r}")
    return parsed.strftime("%Y-%m-%d")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-cache-root",
        type=Path,
        required=True,
        help="prod_screen clean cache/5y root; must contain auc_static, auc_yearly, ic_yearly",
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--feature-start", default=DEFAULT_FEATURE_START)
    parser.add_argument("--feature-end", default=DEFAULT_FEATURE_END)
    parser.add_argument("--target-cutoff", default=DEFAULT_TARGET_CUTOFF)
    parser.add_argument("--input-start", default=DEFAULT_INPUT_START)
    parser.add_argument("--input-end", default=None)
    parser.add_argument("--artifact-output-root", type=Path, default=None)
    parser.add_argument("--n-workers", type=int, default=1)
    parser.add_argument(
        "--scheme-id",
        action="append",
        choices=[spec.scheme_id for spec in SPECS],
        help="只构建指定方案；可重复。默认构建全部三套。",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    selected = [spec for spec in SPECS if not args.scheme_id or spec.scheme_id in args.scheme_id]
    engine = create_input_engine()
    results: list[dict[str, Any]] = []
    try:
        calendar = get_calendar(engine=engine)
        for spec in selected:
            source_group = args.source_cache_root / spec.source_group
            contexts, _ = load_source_contexts(source_group)
            source_dates = _context_date_strings(contexts["STD"])
            input_end = args.input_end or calendar.nth_trading_day_after(source_dates[-1], HORIZON)
            artifacts = build_artifact_bundle(
                spec,
                calendar=calendar,
                engine=engine,
                input_start=args.input_start,
                input_end=input_end,
                output_root=args.artifact_output_root,
            )
            core_module = importlib.import_module(f"schemes.{spec.scheme_id}.core.v31_common")
            output_dir = args.project_root / "schemes" / spec.scheme_id / "benchmarks"
            results.append(
                build_variant_from_source(
                    spec,
                    source_group=source_group,
                    output_dir=output_dir,
                    artifacts=artifacts,
                    calendar=calendar,
                    core_module=core_module,
                    feature_start=args.feature_start,
                    feature_end=args.feature_end,
                    target_cutoff=args.target_cutoff,
                    n_workers=args.n_workers,
                )
            )
    finally:
        engine.dispose()
    print(json.dumps({"status": "success", "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
