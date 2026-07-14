from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd
from sqlalchemy.engine import Engine

from backtests._base_runner import make_run_output, persist_run_output
from backtests.repository import clean_json
from shared.artifact_paths import benchmark_input_root


DATA_SOURCE = "framework_db_aligned"
TARGET_TENOR = "5Y"
HORIZON = 5
BACKTEST_INPUT_START = "2010-07-27"
BACKTEST_INPUT_END = "2026-05-29"
BACKTEST_START = "2025-01-01"
BACKTEST_END = "2026-05-22"
BACKTEST_PREDICT_START_DATE = "2025-01-01"
LIVE_TARGET_CUTOFF = "2026-06-01"
DEFAULT_N_WORKERS = 10
DEFAULT_PARALLEL_SHARDS = 2
DEFAULT_BATCH_MODE = "monthly"
BENCHMARK_ROLE = "historical/platform-live-pit-variant"


@dataclass(frozen=True)
class VariantSpec:
    """三套 5Y ALL_K10 实验回测共享的不可变方案依赖。"""

    scheme_id: str
    benchmark_id: str
    source_model_id: str
    model_version: str
    cache_family: str
    screen_metric: str
    screen_rebal: str
    prod_config: Mapping[str, Any]
    model_config: Callable[..., dict[str, Any]]
    required_baselines: Callable[..., list[str]]
    run_prediction: Callable[[dict[str, Any]], Any]
    window_factory: Callable[..., Any]
    run_window: Callable[..., pd.DataFrame]
    runner_path: Path


@dataclass(frozen=True)
class RuntimeDeps:
    """由每个 runner 显式注入的平台输入和日历依赖。"""

    create_engine: Callable[[], Engine]
    get_calendar: Callable[..., Any]
    build_daily: Callable[..., Any]
    build_weekly: Callable[..., Any]
    build_monthly: Callable[..., Any]


def run_historical_prediction(
    spec: VariantSpec,
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None = None,
    n_workers: int = DEFAULT_N_WORKERS,
    feature_dates: Iterable[str] | None = None,
    cache_dir: str | Path | None = None,
    cache_key_parts: dict[str, Any] | None = None,
    disable_cache: bool = False,
    parallel_shards: int = 1,
    parallel_backend: str = "process",
    batch_mode: str = DEFAULT_BATCH_MODE,
    target_date_for_anchor: Callable[[str], str | None] | None = None,
    target_month_end_for_target: Callable[[str], str | None] | None = None,
    use_phase_a_cache: bool = False,
) -> pd.DataFrame:
    """按 source 连续上下文运行单个 5Y ALL_K10 实验变体。"""
    is_targeted_sample = feature_dates is not None
    dates = _historical_feature_dates(daily_df, feature_dates=feature_dates)
    if not dates:
        raise RuntimeError(f"{spec.scheme_id} historical prediction has no feature dates")
    model_context_end = _source_context_end(daily_df, default=max(dates))
    cache_root = None if disable_cache or cache_dir is None else Path(cache_dir)
    if cache_root is not None:
        cache_root.mkdir(parents=True, exist_ok=True)
    key_parts = dict(cache_key_parts or {})
    key_parts["date_to_week_fingerprint"] = _date_to_week_fingerprint(date_to_week)

    if batch_mode == "monthly":
        groups = _monthly_source_groups(
            dates=dates,
            model_context_end=model_context_end,
            target_date_for_anchor=target_date_for_anchor,
            target_month_end_for_target=target_month_end_for_target,
            require_target_month_end=is_targeted_sample,
        )
        all_cached = _all_monthly_row_cache_exists(
            spec,
            groups=groups,
            cache_dir=cache_root,
            cache_key_parts=key_parts,
        )
        phase_a_caches = (
            _build_phase_a_caches(
                spec,
                dates=dates,
                daily_df=daily_df,
                weekly_df=weekly_df,
                monthly_df=monthly_df,
                date_to_week=date_to_week,
                n_workers=n_workers,
                model_context_end=model_context_end,
            )
            if use_phase_a_cache and not all_cached
            else None
        )
        records = _run_monthly_groups(
            spec,
            groups=groups,
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            n_workers=n_workers,
            model_context_end=model_context_end,
            cache_dir=cache_root,
            cache_key_parts=key_parts,
            parallel_shards=parallel_shards,
            parallel_backend=parallel_backend,
            phase_a_caches=phase_a_caches,
        )
    elif batch_mode == "daily":
        records = _run_daily_dates(
            spec,
            dates=dates,
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            n_workers=n_workers,
            model_context_end=model_context_end,
            cache_dir=cache_root,
            cache_key_parts=key_parts,
            parallel_shards=parallel_shards,
            parallel_backend=parallel_backend,
        )
    else:
        raise ValueError(f"unsupported 5Y ALL_K10 batch mode: {batch_mode}")

    detail = pd.DataFrame(records)
    if detail.empty:
        raise RuntimeError(f"{spec.scheme_id} historical prediction has no rows")
    return detail.sort_values("anchor_date").reset_index(drop=True)


def _run_daily_dates(
    spec: VariantSpec,
    *,
    dates: list[str],
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None,
    n_workers: int,
    model_context_end: str,
    cache_dir: Path | None,
    cache_key_parts: dict[str, Any],
    parallel_shards: int,
    parallel_backend: str,
) -> list[dict[str, Any]]:
    if parallel_shards <= 1 or len(dates) <= 1:
        return [
            _run_one_feature_date(
                spec,
                daily_df=daily_df,
                weekly_df=weekly_df,
                monthly_df=monthly_df,
                date_to_week=date_to_week,
                feature_date=day,
                n_workers=n_workers,
                model_context_end=model_context_end,
                cache_dir=cache_dir,
                cache_key_parts=cache_key_parts,
            )
            for day in dates
        ]
    if parallel_backend == "inline":
        records: list[dict[str, Any]] = []
        for shard in _split_shards(dates, parallel_shards):
            records.extend(
                _run_daily_dates(
                    spec,
                    dates=shard,
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    date_to_week=date_to_week,
                    n_workers=n_workers,
                    model_context_end=model_context_end,
                    cache_dir=cache_dir,
                    cache_key_parts=cache_key_parts,
                    parallel_shards=1,
                    parallel_backend="inline",
                )
            )
        return records
    if parallel_backend != "process":
        raise ValueError(f"unsupported 5Y ALL_K10 parallel backend: {parallel_backend}")
    payloads = [
        {
            "spec": spec,
            "dates": shard,
            "daily_df": daily_df,
            "weekly_df": weekly_df,
            "monthly_df": monthly_df,
            "date_to_week": date_to_week,
            "n_workers": n_workers,
            "model_context_end": model_context_end,
            "cache_dir": str(cache_dir) if cache_dir is not None else None,
            "cache_key_parts": cache_key_parts,
        }
        for shard in _split_shards(dates, parallel_shards)
    ]
    records = []
    with ProcessPoolExecutor(max_workers=len(payloads)) as executor:
        for shard_rows in executor.map(_daily_shard_worker, payloads):
            records.extend(shard_rows)
    return records


def _daily_shard_worker(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _run_daily_dates(
        payload["spec"],
        dates=[str(day) for day in payload["dates"]],
        daily_df=payload["daily_df"],
        weekly_df=payload["weekly_df"],
        monthly_df=payload["monthly_df"],
        date_to_week=payload["date_to_week"],
        n_workers=int(payload["n_workers"]),
        model_context_end=str(payload["model_context_end"]),
        cache_dir=Path(payload["cache_dir"]) if payload.get("cache_dir") else None,
        cache_key_parts=dict(payload.get("cache_key_parts") or {}),
        parallel_shards=1,
        parallel_backend="inline",
    )


def _run_one_feature_date(
    spec: VariantSpec,
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None,
    feature_date: str,
    n_workers: int,
    model_context_end: str,
    cache_dir: Path | None,
    cache_key_parts: dict[str, Any],
) -> dict[str, Any]:
    path = cache_path(
        spec,
        cache_dir,
        feature_date,
        cache_key_parts,
        cache_mode="daily",
        window_end=feature_date,
        model_context_end=model_context_end,
    )
    if path is not None and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    window = spec.window_factory(feature_date, source_end=model_context_end)
    detail = spec.run_window(
        daily_df=daily_df,
        weekly_df=weekly_df,
        monthly_df=monthly_df,
        date_to_week=date_to_week,
        feature_date=feature_date,
        test_ranges=window.test_ranges,
        current_start=window.current_start,
        current_end=feature_date,
        require_labels=True,
        n_workers=n_workers,
    )
    matched = detail[detail["anchor_date"].astype(str) == feature_date]
    if matched.empty:
        raise RuntimeError(f"{spec.scheme_id} produced no row for feature_date={feature_date}")
    record = clean_json(matched.tail(1).iloc[0].to_dict())
    record["model_version"] = spec.model_version
    _write_cache(path, record)
    return record


def _monthly_source_groups(
    *,
    dates: list[str],
    model_context_end: str,
    target_date_for_anchor: Callable[[str], str | None] | None,
    target_month_end_for_target: Callable[[str], str | None] | None = None,
    require_target_month_end: bool = False,
) -> list[dict[str, Any]]:
    unique_dates = sorted({str(day) for day in dates})
    if not unique_dates:
        return []
    if target_date_for_anchor is None:
        if require_target_month_end:
            raise RuntimeError("target month end requires a target-date resolver")
        return [
            {"dates": group, "source_end": model_context_end}
            for group in _month_groups(unique_dates)
        ]
    if require_target_month_end and target_month_end_for_target is None:
        raise RuntimeError("target month end resolver is required for targeted sample")
    target_months: dict[str, dict[str, Any]] = {}
    for day in unique_dates:
        target_date = target_date_for_anchor(day)
        if target_date is None:
            raise RuntimeError(f"missing T+5 target_date for feature_date={day}")
        month = str(target_date)[:7]
        source_end = (
            _validated_target_month_end(
                str(target_date),
                target_month_end_for_target,
                model_context_end=model_context_end,
            )
            if target_month_end_for_target is not None
            else str(target_date)
        )
        group = target_months.setdefault(month, {"dates": [], "source_end": source_end})
        group["dates"].append(day)
        if target_month_end_for_target is not None and str(group["source_end"]) != source_end:
            raise RuntimeError(
                f"inconsistent target month end for target_month={month}: "
                f"{group['source_end']} != {source_end}"
            )
        if target_month_end_for_target is None:
            group["source_end"] = max(str(group["source_end"]), str(target_date))
    return [
        {"dates": sorted(group["dates"]), "source_end": str(group["source_end"])}
        for _, group in sorted(target_months.items())
    ]


def _all_monthly_row_cache_exists(
    spec: VariantSpec,
    *,
    groups: list[dict[str, Any]],
    cache_dir: Path | None,
    cache_key_parts: dict[str, Any],
) -> bool:
    if cache_dir is None:
        return False
    for group in groups:
        window_end = max(str(day) for day in group["dates"])
        source_end = str(group["source_end"])
        for day in group["dates"]:
            path = cache_path(
                spec,
                cache_dir,
                str(day),
                cache_key_parts,
                cache_mode="monthly",
                window_end=window_end,
                model_context_end=source_end,
            )
            if path is None or not path.exists():
                return False
    return True


def _run_monthly_groups(
    spec: VariantSpec,
    *,
    groups: list[dict[str, Any]],
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None,
    n_workers: int,
    model_context_end: str,
    cache_dir: Path | None,
    cache_key_parts: dict[str, Any],
    parallel_shards: int,
    parallel_backend: str,
    phase_a_caches: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if parallel_shards <= 1 or len(groups) <= 1:
        records: list[dict[str, Any]] = []
        for group in groups:
            records.extend(
                run_monthly_batch_group(
                    spec,
                    group_dates=[str(day) for day in group["dates"]],
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    date_to_week=date_to_week,
                    n_workers=n_workers,
                    model_context_end=model_context_end,
                    source_end=str(group["source_end"]),
                    cache_dir=cache_dir,
                    cache_key_parts=cache_key_parts,
                    phase_a_caches=phase_a_caches,
                )
            )
        return records
    if parallel_backend == "inline":
        records = []
        for shard in _split_shards(groups, parallel_shards):
            records.extend(
                _run_monthly_groups(
                    spec,
                    groups=shard,
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    date_to_week=date_to_week,
                    n_workers=n_workers,
                    model_context_end=model_context_end,
                    cache_dir=cache_dir,
                    cache_key_parts=cache_key_parts,
                    parallel_shards=1,
                    parallel_backend="inline",
                    phase_a_caches=phase_a_caches,
                )
            )
        return records
    if parallel_backend != "process":
        raise ValueError(f"unsupported 5Y ALL_K10 parallel backend: {parallel_backend}")
    payloads = [
        {
            "spec": spec,
            "groups": shard,
            "daily_df": daily_df,
            "weekly_df": weekly_df,
            "monthly_df": monthly_df,
            "date_to_week": date_to_week,
            "n_workers": n_workers,
            "model_context_end": model_context_end,
            "cache_dir": str(cache_dir) if cache_dir is not None else None,
            "cache_key_parts": cache_key_parts,
            "phase_a_caches": phase_a_caches,
        }
        for shard in _split_shards(groups, parallel_shards)
    ]
    records = []
    with ProcessPoolExecutor(max_workers=len(payloads)) as executor:
        for shard_rows in executor.map(_monthly_shard_worker, payloads):
            records.extend(shard_rows)
    return records


def _monthly_shard_worker(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _run_monthly_groups(
        payload["spec"],
        groups=payload["groups"],
        daily_df=payload["daily_df"],
        weekly_df=payload["weekly_df"],
        monthly_df=payload["monthly_df"],
        date_to_week=payload["date_to_week"],
        n_workers=int(payload["n_workers"]),
        model_context_end=str(payload["model_context_end"]),
        cache_dir=Path(payload["cache_dir"]) if payload.get("cache_dir") else None,
        cache_key_parts=dict(payload.get("cache_key_parts") or {}),
        parallel_shards=1,
        parallel_backend="inline",
        phase_a_caches=payload.get("phase_a_caches"),
    )


def run_monthly_batch_group(
    spec: VariantSpec,
    *,
    group_dates: list[str],
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None,
    n_workers: int,
    model_context_end: str,
    cache_dir: Path | None,
    cache_key_parts: dict[str, Any],
    source_end: str | None = None,
    phase_a_caches: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """运行一个 target-month batch，并保留 2024-01-01 source 锚点。"""
    dates = sorted({str(day) for day in group_dates})
    batch_end = max(dates)
    group_source_end = str(source_end or model_context_end)
    cached: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for feature_date in dates:
        path = cache_path(
            spec,
            cache_dir,
            feature_date,
            cache_key_parts,
            cache_mode="monthly",
            window_end=batch_end,
            model_context_end=group_source_end,
        )
        if path is not None and path.exists():
            cached[feature_date] = json.loads(path.read_text(encoding="utf-8"))
        else:
            missing.append(feature_date)
    if missing:
        window = spec.window_factory(
            batch_end,
            source_end=group_source_end,
            current_start=min(dates),
            current_end=batch_end,
        )
        detail = spec.run_window(
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            feature_date=batch_end,
            test_ranges=window.test_ranges,
            current_start=window.current_start,
            current_end=batch_end,
            require_labels=True,
            n_workers=n_workers,
            phase_a_caches=phase_a_caches,
        )
        missing_set = set(missing)
        for row in detail.to_dict("records"):
            anchor_date = str(row.get("anchor_date"))
            if anchor_date not in missing_set:
                continue
            record = clean_json(row)
            record["model_version"] = spec.model_version
            cached[anchor_date] = record
            path = cache_path(
                spec,
                cache_dir,
                anchor_date,
                cache_key_parts,
                cache_mode="monthly",
                window_end=batch_end,
                model_context_end=group_source_end,
            )
            _write_cache(path, record)
    missed = [day for day in dates if day not in cached]
    if missed:
        raise RuntimeError(f"{spec.scheme_id} monthly batch missed feature dates: {missed}")
    return [cached[day] for day in dates]


def _build_phase_a_caches(
    spec: VariantSpec,
    *,
    dates: list[str],
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None,
    n_workers: int,
    model_context_end: str,
) -> dict[str, Any]:
    """为同一完整 source context 预计算各 baseline 的 Phase A。"""
    window = spec.window_factory(
        max(dates),
        source_end=model_context_end,
        current_start=min(dates),
        current_end=max(dates),
    )
    caches: dict[str, Any] = {}
    for baseline in spec.required_baselines():
        with redirect_stdout(sys.stderr):
            output = spec.run_prediction(
                spec.model_config(
                    str(baseline),
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    date_to_week=date_to_week,
                    test_start=window.test_ranges[0][0],
                    test_end=window.test_ranges[-1][1],
                    test_ranges=window.test_ranges,
                    require_labels=True,
                    emit_report=False,
                    return_ctx=True,
                    phase_a_only=True,
                    n_workers=n_workers,
                )
            )
        ctx = output[1] if isinstance(output, tuple) and len(output) == 2 else output
        if not isinstance(ctx, dict) or "phase_a_cache" not in ctx:
            raise RuntimeError(f"{spec.scheme_id} baseline {baseline} did not return Phase A cache")
        caches[str(baseline)] = ctx["phase_a_cache"]
    return caches


def cache_path(
    spec: VariantSpec,
    cache_dir: Path | None,
    feature_date: str,
    cache_key_parts: dict[str, Any],
    *,
    cache_mode: str,
    window_end: str,
    model_context_end: str,
) -> Path | None:
    """返回含方案、算法、代码和输入指纹的不可别名缓存路径。"""
    if cache_dir is None:
        return None
    baseline_configs = {
        str(name): spec.model_config(str(name))
        for name in spec.required_baselines()
    }
    payload = {
        "scheme_id": spec.scheme_id,
        "source_model_id": spec.source_model_id,
        "model_version": spec.model_version,
        "cache_family": spec.cache_family,
        "screen_metric": spec.screen_metric,
        "screen_rebal": spec.screen_rebal,
        "baseline_configs": baseline_configs,
        "prod_config": dict(spec.prod_config),
        "feature_date": feature_date,
        "cache_mode": cache_mode,
        "pit_window_end": window_end,
        "model_context_end": model_context_end,
        "target_tenor": TARGET_TENOR,
        "horizon": HORIZON,
        "source_context_start": "2024-01-01",
        "backtest_input_start": BACKTEST_INPUT_START,
        "backtest_input_end": BACKTEST_INPUT_END,
        "code_fingerprint": _code_fingerprint(spec),
        "input_fingerprints": cache_key_parts,
        "runtime_environment": _runtime_environment(),
    }
    digest = hashlib.sha256(
        json.dumps(clean_json(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return cache_dir / spec.cache_family / f"{feature_date}_{digest}.json"


def _code_fingerprint(spec: VariantSpec) -> str:
    project_root = Path(__file__).resolve().parents[1]
    scheme_root = project_root / "schemes" / spec.scheme_id
    paths = [
        Path(__file__),
        spec.runner_path,
        scheme_root / "predict.py",
        scheme_root / "inference.py",
        scheme_root / "core" / "v31_common.py",
        scheme_root / "core" / "screen_metrics.py",
        scheme_root / "core" / "data_alignment.py",
    ]
    hasher = hashlib.sha256()
    for path in paths:
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _date_to_week_fingerprint(date_to_week: Mapping[str, int | str] | None) -> str:
    """返回实际日周映射的稳定指纹，防止周历口径变化复用旧预测。"""
    payload = {
        str(day): None if week_id is None else str(week_id)
        for day, week_id in sorted((date_to_week or {}).items(), key=lambda item: str(item[0]))
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _runtime_environment() -> dict[str, str]:
    """返回会影响模型与缓存数值的最小运行环境身份。"""
    return {
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "scikit-learn": _package_version("scikit-learn"),
        "lightgbm": _package_version("lightgbm"),
    }


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _write_cache(path: Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def build_backtest_rows(
    spec: VariantSpec,
    detail: pd.DataFrame,
    *,
    target_date_for_anchor: Callable[[str], str | None],
    daily_artifact: Any,
    weekly_artifact: Any,
    monthly_artifact: Any,
    exclude_live_cutoff: bool = True,
) -> list[dict[str, Any]]:
    """将 source 明细映射为真实 DB T+5 交易日的标准回测行。"""
    rows: list[dict[str, Any]] = []
    for record in detail.to_dict("records"):
        feature_date = str(record["anchor_date"])
        if feature_date < BACKTEST_PREDICT_START_DATE:
            continue
        target_date = target_date_for_anchor(feature_date)
        if target_date is None or (exclude_live_cutoff and target_date >= LIVE_TARGET_CUTOFF):
            continue
        prediction = _int_or_none(record.get("prediction"))
        rows.append(
            {
                "benchmark_id": spec.benchmark_id,
                "scheme_id": spec.scheme_id,
                "target_tenor": TARGET_TENOR,
                "horizon": HORIZON,
                "predict_date": feature_date,
                "feature_date": feature_date,
                "target_date": str(target_date),
                "predicted_direction": prediction,
                "label": _int_or_none(record.get("true_label")),
                "confidence": _float_or_default(record.get("confidence"), abs(prediction or 0)),
                "model_version": spec.model_version,
                "data_source": DATA_SOURCE,
                "extra": _row_extra(spec, record, daily_artifact, weekly_artifact, monthly_artifact),
            }
        )
    return rows


def run_reproduction(
    spec: VariantSpec,
    deps: RuntimeDeps,
    *,
    persist: bool = True,
    n_workers: int = DEFAULT_N_WORKERS,
    engine: Engine | None = None,
    sample_dates: Iterable[str] | None = None,
    parallel_shards: int = 1,
    cache_dir: str | Path | None = None,
    disable_cache: bool = False,
    batch_mode: str = DEFAULT_BATCH_MODE,
    use_phase_a_cache: bool = False,
) -> dict[str, Any]:
    """执行单套 5Y ALL_K10 DB-aligned 历史复现。"""
    if persist and batch_mode != DEFAULT_BATCH_MODE:
        raise ValueError(
            f"persist requires canonical monthly batch mode, got batch_mode={batch_mode}"
        )
    sample_date_list = sorted({str(day) for day in sample_dates or []})
    if persist and sample_date_list:
        raise ValueError("sample mode cannot persist")
    started = time.time()
    own_engine = engine is None
    engine = engine or deps.create_engine()
    try:
        output_root = benchmark_input_root(spec.benchmark_id)
        effective_cache_dir = cache_dir if cache_dir is not None else output_root.parent / "pit_cache"
        calendar = deps.get_calendar(engine=engine)
        target_month_end_for_target = lambda target_date: _target_month_end_from_calendar(
            calendar,
            target_date,
        )
        effective_input_end = _effective_input_end(
            sample_date_list,
            calendar,
            target_month_end_for_target=target_month_end_for_target,
        )
        daily_artifact = deps.build_daily(
            scheme_id=spec.scheme_id,
            predict_date="historical_backtest",
            start_date=BACKTEST_INPUT_START,
            end_date=effective_input_end,
            engine=engine,
            output_root=output_root,
        )
        weekly_end_week = calendar.week_id_for_date(effective_input_end)
        if weekly_end_week is None:
            raise RuntimeError(f"cannot resolve DB week_id for backtest end={effective_input_end}")
        weekly_artifact = deps.build_weekly(
            scheme_id=spec.scheme_id,
            predict_date="historical_backtest",
            end_week=int(weekly_end_week),
            as_of_date=effective_input_end,
            engine=engine,
            output_root=output_root,
        )
        monthly_artifact = deps.build_monthly(
            scheme_id=spec.scheme_id,
            predict_date="historical_backtest",
            start_date=BACKTEST_INPUT_START,
            end_date=effective_input_end,
            engine=engine,
            output_root=output_root,
        )
        date_to_week = _date_to_week_map(daily_artifact.dataframe, calendar)
        cache_key_parts = {
            "daily_input_artifact_hash": getattr(daily_artifact, "content_hash", None),
            "weekly_input_artifact_hash": getattr(weekly_artifact, "content_hash", None),
            "monthly_input_artifact_hash": getattr(monthly_artifact, "content_hash", None),
            "weekly_input_end_week": int(weekly_end_week),
            "weekly_input_as_of_date": effective_input_end,
            "backtest_input_end": effective_input_end,
        }
        detail = run_historical_prediction(
            spec,
            daily_df=daily_artifact.dataframe,
            weekly_df=weekly_artifact.dataframe,
            monthly_df=monthly_artifact.dataframe,
            date_to_week=date_to_week,
            n_workers=n_workers,
            feature_dates=sample_date_list or None,
            cache_dir=effective_cache_dir,
            cache_key_parts=cache_key_parts,
            disable_cache=disable_cache,
            parallel_shards=parallel_shards,
            batch_mode=batch_mode,
            target_date_for_anchor=lambda day: calendar.nth_trading_day_after(day, HORIZON),
            target_month_end_for_target=target_month_end_for_target,
            use_phase_a_cache=use_phase_a_cache,
        )
        rows = build_backtest_rows(
            spec,
            detail,
            target_date_for_anchor=lambda day: calendar.nth_trading_day_after(day, HORIZON),
            daily_artifact=daily_artifact,
            weekly_artifact=weekly_artifact,
            monthly_artifact=monthly_artifact,
            exclude_live_cutoff=not bool(sample_date_list),
        )
        if not rows:
            raise RuntimeError(f"{spec.scheme_id} reproduction produced no rows")
        output = make_run_output(
            scheme_id=spec.scheme_id,
            data_source=DATA_SOURCE,
            start_date=min(str(row["predict_date"]) for row in rows),
            end_date=max(str(row["predict_date"]) for row in rows),
            rows=rows,
            benchmark_id=spec.benchmark_id,
        )
        output.summary.update(
            {
                "source_model_id": spec.source_model_id,
                "source_model_config": str(spec.prod_config["name"]),
                "model_scope": "experimental/platform_live_pit_variant",
                "screen_metric": spec.screen_metric,
                "screen_rebal": spec.screen_rebal,
                "cache_family": spec.cache_family,
                "vote_baselines": list(spec.prod_config["baselines"]),
                "fallback_baseline": str(spec.prod_config["fallback"]),
                "streak_K": int(spec.prod_config["streak_K"]),
                "backtest_scope": "targeted_sample" if sample_date_list else "full_historical",
                "sample_dates": sample_date_list,
                "parallel_shards": int(parallel_shards),
                "batch_mode": batch_mode,
                "phase_a_cache": bool(use_phase_a_cache),
                "cache_enabled": bool(not disable_cache and effective_cache_dir is not None),
                "cache_dir": str(effective_cache_dir),
                "daily_input_artifact_path": str(daily_artifact.path),
                "weekly_input_artifact_path": str(weekly_artifact.path),
                "monthly_input_artifact_path": str(monthly_artifact.path),
                "input_artifact_hash": getattr(daily_artifact, "content_hash", None),
                "weekly_input_artifact_hash": getattr(weekly_artifact, "content_hash", None),
                "monthly_input_artifact_hash": getattr(monthly_artifact, "content_hash", None),
                "weekly_input_end_week": int(weekly_end_week),
                "weekly_input_as_of_date": effective_input_end,
            }
        )
        if sample_date_list:
            output.summary["backtest_input_end"] = effective_input_end
        if persist or not sample_date_list:
            _validate_before_persist(output.rows)
        run_id = persist_run_output(engine, output, benchmark_id=spec.benchmark_id) if persist else None
        compact_rows = compact_prediction_rows(output.rows)
        run_payload = {
            "scheme_id": output.scheme_id,
            "data_source": output.data_source,
            "start_date": output.start_date,
            "end_date": output.end_date,
            "rows": compact_rows,
            "row_count": len(output.rows),
            "monthly_count": len(output.monthly_metrics),
            "summary": output.summary,
        }
        return {
            "status": "success",
            "run_id": run_id,
            "benchmark_id": spec.benchmark_id,
            "scheme_id": output.scheme_id,
            "data_source": output.data_source,
            "start_date": output.start_date,
            "end_date": output.end_date,
            "row_count": len(output.rows),
            "monthly_count": len(output.monthly_metrics),
            "summary": output.summary,
            "rows": compact_rows,
            "runs": [run_payload],
            "elapsed_sec": round(time.time() - started, 3),
        }
    finally:
        if own_engine:
            engine.dispose()


def compact_prediction_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for row in rows:
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        scores = extra.get("baseline_scores") if isinstance(extra.get("baseline_scores"), dict) else {}
        signs = extra.get("baseline_signs") if isinstance(extra.get("baseline_signs"), dict) else {}
        direction = _int_or_none(row.get("predicted_direction"))
        label = _int_or_none(row.get("label"))
        item = {
            "predict_date": row["predict_date"],
            "feature_date": row["feature_date"],
            "target_date": row["target_date"],
            "tenor": row["target_tenor"],
            "target_tenor": row["target_tenor"],
            "horizon": int(row["horizon"]),
            "benchmark_role": BENCHMARK_ROLE,
            "direction": direction,
            "label": label,
            "is_correct": (
                None
                if label is None or direction is None
                else bool(direction in (-1, 1) and direction == label)
            ),
            "confidence": row["confidence"],
            "vote_score": extra.get("vote_score"),
        }
        for baseline in ("STD", "DIV", "ACCWT", "CROSS_7Y"):
            item[f"{baseline}_score"] = scores.get(baseline)
            item[f"{baseline}_dir"] = signs.get(baseline)
        compact.append(item)
    return compact


def _row_extra(
    spec: VariantSpec,
    record: dict[str, Any],
    daily_artifact: Any,
    weekly_artifact: Any,
    monthly_artifact: Any,
) -> dict[str, Any]:
    return clean_json(
        {
            "source_model_id": spec.source_model_id,
            "source_model_config": str(spec.prod_config["name"]),
            "model_scope": "experimental/platform_live_pit_variant",
            "screen_metric": spec.screen_metric,
            "screen_rebal": spec.screen_rebal,
            "cache_family": spec.cache_family,
            "vote_baselines": list(spec.prod_config["baselines"]),
            "fallback_baseline": str(spec.prod_config["fallback"]),
            "streak_K": int(spec.prod_config["streak_K"]),
            "vote_score": record.get("vote_score"),
            "baseline_signs": record.get("baseline_signs"),
            "baseline_scores": record.get("baseline_scores"),
            "input_artifact_path": str(daily_artifact.path),
            "input_artifact_source": daily_artifact.source,
            "input_artifact_data_version": daily_artifact.data_version,
            "daily_input_artifact_path": str(daily_artifact.path),
            "daily_input_artifact_source": daily_artifact.source,
            "daily_input_artifact_data_version": daily_artifact.data_version,
            "weekly_input_artifact_path": str(weekly_artifact.path),
            "weekly_input_artifact_source": weekly_artifact.source,
            "weekly_input_artifact_data_version": weekly_artifact.data_version,
            "monthly_input_artifact_path": str(monthly_artifact.path),
            "monthly_input_artifact_source": monthly_artifact.source,
            "monthly_input_artifact_data_version": monthly_artifact.data_version,
        }
    )


def _validate_before_persist(rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise RuntimeError("cannot persist empty 5Y ALL_K10 backtest output")
    seen: set[tuple[str, str, str, int]] = set()
    for row in rows:
        target_date = str(row.get("target_date") or "")
        if not target_date:
            raise RuntimeError(f"missing target_date for feature_date={row.get('feature_date')}")
        if target_date >= LIVE_TARGET_CUTOFF:
            raise RuntimeError(f"historical backtest row crosses live cutoff target_date={target_date}")
        key = (
            str(row.get("feature_date")),
            target_date,
            str(row.get("target_tenor")),
            int(row.get("horizon")),
        )
        if key in seen:
            raise RuntimeError(f"duplicate historical backtest key: {key}")
        seen.add(key)


def _historical_feature_dates(
    daily_df: pd.DataFrame,
    *,
    feature_dates: Iterable[str] | None = None,
) -> list[str]:
    if feature_dates is not None:
        return sorted({str(day) for day in feature_dates})
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return sorted({str(day) for day in dates.dropna() if BACKTEST_START <= str(day) <= BACKTEST_END})


def _source_context_end(daily_df: pd.DataFrame, *, default: str) -> str:
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna()
    return str(default) if dates.empty else max(str(default), str(dates.max()))


def _month_groups(dates: list[str]) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for day in sorted({str(item) for item in dates}):
        groups.setdefault(day[:7], []).append(day)
    return [groups[key] for key in sorted(groups)]


def _split_shards(items: list[Any], parallel_shards: int) -> list[list[Any]]:
    count = max(1, min(int(parallel_shards), len(items)))
    return [items[index::count] for index in range(count) if items[index::count]]


def _effective_input_end(
    sample_dates: list[str],
    calendar: Any,
    *,
    target_month_end_for_target: Callable[[str], str | None] | None = None,
) -> str:
    if not sample_dates:
        return BACKTEST_INPUT_END
    target_dates = [
        str(target)
        for target in (calendar.nth_trading_day_after(day, HORIZON) for day in sample_dates)
        if target is not None
    ]
    if len(target_dates) != len(sample_dates):
        raise RuntimeError("missing T+5 target_date while resolving targeted sample input end")
    if target_month_end_for_target is None:
        raise RuntimeError("target month end resolver is required for targeted sample input")
    month_ends = [
        _validated_target_month_end(target_date, target_month_end_for_target)
        for target_date in target_dates
    ]
    return max(month_ends)


def _target_month_end_from_calendar(calendar: Any, target_date: str) -> str:
    """从 DB 交易日历解析 target 所在月份的最后交易日。"""
    previous_trading_day = getattr(calendar, "previous_trading_day", None)
    if not callable(previous_trading_day):
        raise RuntimeError("target month end requires calendar.previous_trading_day")
    target = pd.Timestamp(str(target_date))
    next_month_start = (target.to_period("M") + 1).start_time.strftime("%Y-%m-%d")
    resolved = previous_trading_day(next_month_start)
    return _validated_target_month_end(str(target_date), lambda _target: resolved)


def _validated_target_month_end(
    target_date: str,
    resolver: Callable[[str], str | None],
    *,
    model_context_end: str | None = None,
) -> str:
    resolved = resolver(str(target_date))
    if resolved is None or not str(resolved).strip():
        raise RuntimeError(f"target month end is unavailable for target_date={target_date}")
    value = pd.Timestamp(str(resolved)).strftime("%Y-%m-%d")
    target = pd.Timestamp(str(target_date)).strftime("%Y-%m-%d")
    if value[:7] != target[:7] or value < target:
        raise RuntimeError(
            f"invalid target month end={value} for target_date={target}"
        )
    if model_context_end is not None and value > str(model_context_end):
        raise RuntimeError(
            f"target month end={value} exceeds available model context={model_context_end}"
        )
    return value


def _date_to_week_map(daily_df: pd.DataFrame, calendar: Any) -> dict[str, int | str]:
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    result: dict[str, int | str] = {}
    for day in dates.dropna().unique().tolist():
        week_id = calendar.week_id_for_date(day)
        if week_id is not None:
            result[str(day)] = int(week_id)
    return result


def _int_or_none(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def _float_or_default(value: Any, default: float) -> float:
    if value is None or pd.isna(value):
        return float(default)
    return float(value)


def parse_sample_dates(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]
