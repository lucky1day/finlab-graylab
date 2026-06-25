from __future__ import annotations

import argparse
import hashlib
import json
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
from sqlalchemy.engine import Engine

from backtests._base_runner import make_run_output, persist_run_output
from backtests.repository import clean_json
from shared.artifact_paths import benchmark_input_root
from shared.calendar_service import get_calendar
from shared.data_service import create_sqlalchemy_engine
from shared.input_artifacts import (
    build_daily_input_artifact,
    build_monthly_input_artifact,
    build_weekly_input_artifact,
)
from schemes.liwei_0616_10y02_cons_say_k3_div_k5.core.v31_common import (
    MODEL_VERSION,
    PROD_CONFIG,
    SOURCE_MODEL_ID,
)
from schemes.liwei_0616_10y02_cons_say_k3_div_k5.inference import (
    liwei_0616_pit_window,
    run_10y02_for_window_silent,
)


SCHEME_ID = "liwei_0616_10y02_cons_say_k3_div_k5"
BENCHMARK_ID = "liwei_0616_10y_02"
DATA_SOURCE = "framework_db_aligned"
TARGET_TENOR = "10Y"
HORIZON = 5
BACKTEST_INPUT_START = "2010-07-27"
BACKTEST_INPUT_END = "2026-05-29"
BACKTEST_START = "2025-01-01"
BACKTEST_END = "2026-05-22"
BACKTEST_PREDICT_START_DATE = "2025-01-01"
LIVE_TARGET_CUTOFF = "2026-06-01"
DEFAULT_N_WORKERS = 10
DEFAULT_PARALLEL_SHARDS = 2
DEFAULT_BATCH_MODE = "daily"


def run_historical_prediction(
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
    batch_mode: str = "daily",
) -> pd.DataFrame:
    """逐 feature_date 运行 10Y_02 PIT 窗口，返回历史预测明细。"""
    dates = _historical_feature_dates(daily_df, feature_dates=feature_dates)
    if not dates:
        raise RuntimeError("liwei_0616 10Y_02 historical prediction has no feature dates")
    model_context_end = _source_context_end(daily_df, default=max(dates))

    cache_root = None if disable_cache or cache_dir is None else Path(cache_dir)
    if cache_root is not None:
        cache_root.mkdir(parents=True, exist_ok=True)

    if batch_mode == "monthly":
        records = _run_historical_prediction_monthly_batches(
            dates=dates,
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            n_workers=n_workers,
            model_context_end=model_context_end,
            cache_dir=cache_root,
            cache_key_parts=cache_key_parts or {},
            parallel_shards=parallel_shards,
            parallel_backend=parallel_backend,
            group_all_dates=True,
        )
        detail = pd.DataFrame(records)
        if detail.empty:
            raise RuntimeError("liwei_0616 10Y_02 historical prediction has no rows")
        return detail.sort_values("anchor_date").reset_index(drop=True)
    if batch_mode != "daily":
        raise ValueError(f"unsupported 10Y_02 batch mode: {batch_mode}")

    def run_one(day: str) -> dict[str, Any]:
        return _run_one_feature_date(
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            feature_date=day,
            n_workers=n_workers,
            model_context_end=model_context_end,
            cache_dir=cache_root,
            cache_key_parts=cache_key_parts or {},
            cache_mode="daily",
            window_end=day,
        )

    if parallel_shards <= 1 or len(dates) <= 1:
        records = [run_one(day) for day in dates]
    elif parallel_backend == "inline":
        records = []
        for shard in _split_shards(dates, parallel_shards):
            records.extend(run_one(day) for day in shard)
    elif parallel_backend == "process":
        records = _run_historical_prediction_process_shards(
            dates=dates,
            daily_df=daily_df,
            weekly_df=weekly_df,
            monthly_df=monthly_df,
            date_to_week=date_to_week,
            n_workers=n_workers,
            model_context_end=model_context_end,
            cache_dir=cache_root,
            cache_key_parts=cache_key_parts or {},
            parallel_shards=parallel_shards,
        )
    else:
        raise ValueError(f"unsupported 10Y_02 parallel backend: {parallel_backend}")

    detail = pd.DataFrame(records)
    if detail.empty:
        raise RuntimeError("liwei_0616 10Y_02 historical prediction has no rows")
    return detail.sort_values("anchor_date").reset_index(drop=True)


def _run_historical_prediction_monthly_batches(
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
    group_all_dates: bool = False,
) -> list[dict[str, Any]]:
    month_groups = [sorted({str(day) for day in dates})] if group_all_dates else _month_groups(dates)
    if parallel_shards <= 1 or len(month_groups) <= 1:
        records: list[dict[str, Any]] = []
        for group in month_groups:
            records.extend(
                _run_monthly_batch_group(
                    group_dates=group,
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    date_to_week=date_to_week,
                    n_workers=n_workers,
                    model_context_end=model_context_end,
                    cache_dir=cache_dir,
                    cache_key_parts=cache_key_parts,
                )
            )
        return records
    if parallel_backend == "inline":
        records = []
        for shard in _split_shards(month_groups, parallel_shards):
            for group in shard:
                records.extend(
                    _run_monthly_batch_group(
                        group_dates=group,
                        daily_df=daily_df,
                        weekly_df=weekly_df,
                        monthly_df=monthly_df,
                        date_to_week=date_to_week,
                        n_workers=n_workers,
                        model_context_end=model_context_end,
                        cache_dir=cache_dir,
                        cache_key_parts=cache_key_parts,
                    )
                )
        return records
    if parallel_backend != "process":
        raise ValueError(f"unsupported 10Y_02 parallel backend: {parallel_backend}")

    payloads = [
        {
            "month_groups": shard,
            "daily_df": daily_df,
            "weekly_df": weekly_df,
            "monthly_df": monthly_df,
            "date_to_week": date_to_week,
            "n_workers": n_workers,
            "model_context_end": model_context_end,
            "cache_dir": str(cache_dir) if cache_dir is not None else None,
            "cache_key_parts": cache_key_parts,
        }
        for shard in _split_shards(month_groups, parallel_shards)
    ]
    records = []
    with ProcessPoolExecutor(max_workers=min(len(payloads), int(parallel_shards))) as executor:
        for shard_records in executor.map(_run_monthly_shard_worker, payloads):
            records.extend(shard_records)
    return records


def _run_monthly_shard_worker(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cache_root = Path(payload["cache_dir"]) if payload.get("cache_dir") else None
    records: list[dict[str, Any]] = []
    for group in payload["month_groups"]:
        records.extend(
            _run_monthly_batch_group(
                group_dates=[str(day) for day in group],
                daily_df=payload["daily_df"],
                weekly_df=payload["weekly_df"],
                monthly_df=payload["monthly_df"],
                date_to_week=payload["date_to_week"],
                n_workers=int(payload["n_workers"]),
                model_context_end=str(payload["model_context_end"]),
                cache_dir=cache_root,
                cache_key_parts=dict(payload.get("cache_key_parts") or {}),
            )
        )
    return records


def _run_monthly_batch_group(
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
) -> list[dict[str, Any]]:
    dates = sorted({str(day) for day in group_dates})
    batch_end = max(dates)
    cached: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for feature_date in dates:
        cache_path = (
            _cache_path(
                cache_dir,
                feature_date,
                cache_key_parts,
                cache_mode="monthly",
                window_end=batch_end,
                model_context_end=model_context_end,
            )
            if cache_dir is not None
            else None
        )
        if cache_path is not None and cache_path.exists():
            cached[feature_date] = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            missing.append(feature_date)

    if missing:
        source_current_start = _source_current_start(dates)
        window = liwei_0616_pit_window(
            batch_end,
            source_end=model_context_end,
            current_start=source_current_start,
            current_end=batch_end,
        )
        detail = run_10y02_for_window_silent(
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
        )
        missing_set = set(missing)
        for row in detail.to_dict("records"):
            anchor_date = str(row.get("anchor_date"))
            if anchor_date not in missing_set:
                continue
            record = clean_json(row)
            record["model_version"] = str(record.get("model_version") or MODEL_VERSION)
            cached[anchor_date] = record
            cache_path = (
                _cache_path(
                    cache_dir,
                    anchor_date,
                    cache_key_parts,
                    cache_mode="monthly",
                    window_end=batch_end,
                    model_context_end=model_context_end,
                )
                if cache_dir is not None
                else None
            )
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    missing_after_batch = [day for day in dates if day not in cached]
    if missing_after_batch:
        raise RuntimeError(f"liwei_0616 10Y_02 monthly batch missed feature dates: {missing_after_batch}")
    return [cached[day] for day in dates]


def _run_historical_prediction_process_shards(
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
) -> list[dict[str, Any]]:
    shards = _split_shards(dates, parallel_shards)
    payloads = [
        {
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
        for shard in shards
    ]
    records: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=min(len(payloads), int(parallel_shards))) as executor:
        for shard_records in executor.map(_run_shard_worker, payloads):
            records.extend(shard_records)
    return records


def _run_shard_worker(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cache_root = Path(payload["cache_dir"]) if payload.get("cache_dir") else None
    records: list[dict[str, Any]] = []
    for feature_date in payload["dates"]:
        records.append(
            _run_one_feature_date(
                daily_df=payload["daily_df"],
                weekly_df=payload["weekly_df"],
                monthly_df=payload["monthly_df"],
                date_to_week=payload["date_to_week"],
                feature_date=feature_date,
                n_workers=int(payload["n_workers"]),
                model_context_end=str(payload["model_context_end"]),
                cache_dir=cache_root,
                cache_key_parts=dict(payload.get("cache_key_parts") or {}),
                cache_mode="daily",
                window_end=feature_date,
            )
        )
    return records


def _run_one_feature_date(
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
    cache_mode: str,
    window_end: str,
) -> dict[str, Any]:
    cache_path = (
        _cache_path(
            cache_dir,
            feature_date,
            cache_key_parts,
            cache_mode=cache_mode,
            window_end=window_end,
            model_context_end=model_context_end,
        )
        if cache_dir is not None
        else None
    )
    if cache_path is not None and cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    window = liwei_0616_pit_window(feature_date, source_end=model_context_end)
    detail = run_10y02_for_window_silent(
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
        raise RuntimeError(f"liwei_0616 10Y_02 produced no row for feature_date={feature_date}")
    record = clean_json(matched.tail(1).iloc[0].to_dict())
    record["model_version"] = str(record.get("model_version") or MODEL_VERSION)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return record


def _historical_feature_dates(daily_df: pd.DataFrame, *, feature_dates: Iterable[str] | None = None) -> list[str]:
    """生成历史回测需要逐日 PIT 推理的 feature_date 列表。"""
    if feature_dates is not None:
        return sorted({str(day) for day in feature_dates})
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return sorted(
        {
            str(day)
            for day in dates.dropna().tolist()
            if BACKTEST_START <= str(day) <= BACKTEST_END
        }
    )


def _source_context_end(daily_df: pd.DataFrame, *, default: str) -> str:
    """返回原始算法固定历史上下文可使用的数据终点。"""
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna()
    if dates.empty:
        return str(default)
    return max(str(default), str(dates.max()))


def _source_current_start(dates: list[str]) -> str:
    """返回 source batch 需要抽取的第一条样本日期。"""
    return min(str(day) for day in dates)


def _split_shards(dates: list[str], parallel_shards: int) -> list[list[str]]:
    shard_count = max(1, min(int(parallel_shards), len(dates)))
    return [dates[index::shard_count] for index in range(shard_count) if dates[index::shard_count]]


def _month_groups(dates: list[str]) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for day in sorted({str(item) for item in dates}):
        groups.setdefault(day[:7], []).append(day)
    return [groups[key] for key in sorted(groups)]


def _cache_path(
    cache_dir: Path | None,
    feature_date: str,
    cache_key_parts: dict[str, Any],
    *,
    cache_mode: str,
    window_end: str,
    model_context_end: str,
) -> Path | None:
    if cache_dir is None:
        return None
    key_payload = {
        "scheme_id": SCHEME_ID,
        "source_model_id": SOURCE_MODEL_ID,
        "model_version": MODEL_VERSION,
        "feature_date": feature_date,
        "cache_mode": cache_mode,
        "pit_window_end": window_end,
        "model_context_end": model_context_end,
        "target_tenor": TARGET_TENOR,
        "horizon": HORIZON,
        "prod_config": PROD_CONFIG,
        "backtest_input_start": BACKTEST_INPUT_START,
        "backtest_input_end": BACKTEST_INPUT_END,
        "code_hash": _code_hash(),
        "parts": cache_key_parts,
    }
    digest = hashlib.sha256(
        json.dumps(clean_json(key_payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return cache_dir / f"{feature_date}_{digest}.json"


def _code_hash() -> str:
    paths = [
        Path(__file__),
        Path(__file__).resolve().parents[1] / "schemes" / SCHEME_ID / "predict.py",
        Path(__file__).resolve().parents[1] / "schemes" / SCHEME_ID / "inference.py",
        Path(__file__).resolve().parents[1] / "schemes" / SCHEME_ID / "core" / "v31_common.py",
    ]
    hasher = hashlib.sha256()
    for path in paths:
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def build_backtest_rows(
    detail: pd.DataFrame,
    *,
    target_date_for_anchor: Callable[[str], str | None],
    daily_artifact,
    weekly_artifact,
    monthly_artifact,
    exclude_live_cutoff: bool = True,
) -> list[dict[str, Any]]:
    """把算法明细转换为 backtests 标准逐样本行。"""
    rows: list[dict[str, Any]] = []
    for record in detail.to_dict("records"):
        anchor_date = str(record["anchor_date"])
        if anchor_date < BACKTEST_PREDICT_START_DATE:
            continue
        target_date = target_date_for_anchor(anchor_date)
        if target_date is None or (exclude_live_cutoff and target_date >= LIVE_TARGET_CUTOFF):
            continue
        prediction = _int_or_none(record.get("prediction"))
        true_label = _int_or_none(record.get("true_label"))
        rows.append(
            {
                "benchmark_id": BENCHMARK_ID,
                "scheme_id": SCHEME_ID,
                "target_tenor": TARGET_TENOR,
                "horizon": HORIZON,
                "predict_date": anchor_date,
                "feature_date": anchor_date,
                "target_date": target_date,
                "predicted_direction": prediction,
                "label": true_label,
                "confidence": _float_or_default(record.get("confidence"), abs(prediction or 0)),
                "model_version": MODEL_VERSION,
                "data_source": DATA_SOURCE,
                "extra": _row_extra(record, daily_artifact, weekly_artifact, monthly_artifact),
            }
        )
    return rows


def run_liwei_0616_10y02_cons_say_k3_div_k5_reproduction(
    *,
    persist: bool = True,
    n_workers: int = DEFAULT_N_WORKERS,
    engine: Engine | None = None,
    sample_dates: Iterable[str] | None = None,
    parallel_shards: int = 1,
    cache_dir: str | Path | None = None,
    disable_cache: bool = False,
    batch_mode: str = DEFAULT_BATCH_MODE,
) -> dict[str, Any]:
    """执行 liwei_0616 10Y_02 DB-aligned 历史复现。"""
    sample_date_list = sorted({str(day) for day in sample_dates or []})
    if persist and sample_date_list:
        raise ValueError("sample mode cannot persist")

    started = time.time()
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        output_root = benchmark_input_root(BENCHMARK_ID)
        default_cache_dir = output_root.parent / "pit_cache"
        effective_cache_dir = cache_dir if cache_dir is not None else default_cache_dir
        calendar = get_calendar(engine)
        effective_input_end = _effective_input_end(sample_date_list, calendar)
        daily_artifact = build_daily_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date="historical_backtest",
            start_date=BACKTEST_INPUT_START,
            end_date=effective_input_end,
            engine=engine,
            output_root=output_root,
        )
        weekly_end_week = calendar.week_id_for_date(effective_input_end)
        if weekly_end_week is None:
            raise RuntimeError(f"无法从 DB 日历解析 backtest end week: {effective_input_end}")
        weekly_artifact = build_weekly_input_artifact(
            scheme_id=SCHEME_ID,
            predict_date="historical_backtest",
            end_week=int(weekly_end_week),
            as_of_date=effective_input_end,
            engine=engine,
            output_root=output_root,
        )
        monthly_artifact = build_monthly_input_artifact(
            scheme_id=SCHEME_ID,
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
        )
        rows = build_backtest_rows(
            detail,
            target_date_for_anchor=lambda anchor: calendar.nth_trading_day_after(anchor, HORIZON),
            daily_artifact=daily_artifact,
            weekly_artifact=weekly_artifact,
            monthly_artifact=monthly_artifact,
            exclude_live_cutoff=not bool(sample_date_list),
        )
        if not rows:
            raise RuntimeError("liwei_0616 10Y_02 reproduction produced no rows")
        output = make_run_output(
            scheme_id=SCHEME_ID,
            data_source=DATA_SOURCE,
            start_date=min(str(row["predict_date"]) for row in rows),
            end_date=max(str(row["predict_date"]) for row in rows),
            rows=rows,
            benchmark_id=BENCHMARK_ID,
        )
        summary_metadata = {
            "source_model_id": SOURCE_MODEL_ID,
            "source_model_config": PROD_CONFIG["name"],
            "vote_baselines": list(PROD_CONFIG["baselines"]),
            "fallback_baseline": str(PROD_CONFIG["fallback"]),
            "streak_K": int(PROD_CONFIG["streak_K"]),
            "backtest_scope": "targeted_sample" if sample_date_list else "full_historical",
            "sample_dates": sample_date_list,
            "parallel_shards": int(parallel_shards),
            "batch_mode": batch_mode,
            "cache_enabled": bool(not disable_cache and effective_cache_dir is not None),
            "cache_dir": str(effective_cache_dir) if effective_cache_dir is not None else None,
            "daily_input_artifact_path": str(daily_artifact.path),
            "weekly_input_artifact_path": str(weekly_artifact.path),
            "monthly_input_artifact_path": str(monthly_artifact.path),
            "input_artifact_hash": getattr(daily_artifact, "content_hash", None),
            "weekly_input_artifact_hash": getattr(weekly_artifact, "content_hash", None),
            "monthly_input_artifact_hash": getattr(monthly_artifact, "content_hash", None),
            "weekly_input_end_week": int(weekly_end_week),
            "weekly_input_as_of_date": effective_input_end,
        }
        if sample_date_list:
            summary_metadata["backtest_input_end"] = effective_input_end
        output.summary.update(summary_metadata)
        if persist or not sample_date_list:
            _validate_before_persist(output.rows)
        run_id = persist_run_output(engine, output, benchmark_id=BENCHMARK_ID) if persist else None
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
            "benchmark_id": BENCHMARK_ID,
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


def _effective_input_end(sample_dates: list[str], calendar) -> str:
    if not sample_dates:
        return BACKTEST_INPUT_END
    target_dates = [
        str(target)
        for target in (calendar.nth_trading_day_after(day, HORIZON) for day in sample_dates)
        if target is not None
    ]
    if not target_dates:
        return BACKTEST_INPUT_END
    return max(target_dates)


def compact_prediction_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact_rows: list[dict[str, Any]] = []
    for row in rows:
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        baseline_scores = extra.get("baseline_scores") if isinstance(extra.get("baseline_scores"), dict) else {}
        baseline_signs = extra.get("baseline_signs") if isinstance(extra.get("baseline_signs"), dict) else {}
        item = {
            "predict_date": row["predict_date"],
            "feature_date": row["feature_date"],
            "target_date": row["target_date"],
            "tenor": row["target_tenor"],
            "direction": row["predicted_direction"],
            "label": row["label"],
            "confidence": row["confidence"],
        }
        for baseline in ("STD", "ACCWT", "V55_7Y", "DIV"):
            item[f"{baseline}_score"] = baseline_scores.get(baseline)
            item[f"{baseline}_dir"] = baseline_signs.get(baseline)
        item["vote_score"] = extra.get("vote_score")
        compact_rows.append(item)
    return compact_rows


def _validate_before_persist(rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise RuntimeError("cannot persist empty 10Y_02 backtest output")
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


def _date_to_week_map(daily_df: pd.DataFrame, calendar) -> dict[str, int | str]:
    dates = pd.to_datetime(daily_df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    result: dict[str, int | str] = {}
    for day in dates.dropna().unique().tolist():
        week_id = calendar.week_id_for_date(day)
        if week_id is not None:
            result[str(day)] = int(week_id)
    return result


def _row_extra(record: dict[str, Any], daily_artifact, weekly_artifact, monthly_artifact) -> dict[str, Any]:
    return clean_json(
        {
            "source_model_id": SOURCE_MODEL_ID,
            "source_model_config": PROD_CONFIG["name"],
            "vote_baselines": list(PROD_CONFIG["baselines"]),
            "fallback_baseline": str(PROD_CONFIG["fallback"]),
            "streak_K": int(PROD_CONFIG["streak_K"]),
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


def _int_or_none(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def _float_or_default(value: Any, default: float) -> float:
    if value is None or pd.isna(value):
        return float(default)
    return float(value)


def _parse_sample_dates(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--n-workers", type=int, default=DEFAULT_N_WORKERS)
    parser.add_argument("--sample-dates")
    parser.add_argument("--parallel-shards", type=int, default=DEFAULT_PARALLEL_SHARDS)
    parser.add_argument("--batch-mode", choices=["daily", "monthly"], default=DEFAULT_BATCH_MODE)
    parser.add_argument("--cache-dir")
    parser.add_argument("--disable-cache", action="store_true")
    args = parser.parse_args()
    result = run_liwei_0616_10y02_cons_say_k3_div_k5_reproduction(
        persist=not args.no_persist,
        n_workers=args.n_workers,
        sample_dates=_parse_sample_dates(args.sample_dates),
        parallel_shards=args.parallel_shards,
        batch_mode=args.batch_mode,
        cache_dir=args.cache_dir,
        disable_cache=args.disable_cache,
    )
    print(json.dumps(clean_json(result), ensure_ascii=False))


if __name__ == "__main__":
    main()
