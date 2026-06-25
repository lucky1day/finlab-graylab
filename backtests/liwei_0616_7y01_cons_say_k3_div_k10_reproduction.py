from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import redirect_stdout
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
from schemes.liwei_0616_7y01_cons_say_k3_div_k10.core.v31_common import (
    MODEL_VERSION,
    PROD_CONFIG,
    SOURCE_MODEL_ID,
    model_config,
    required_baselines,
    run_prediction,
)
from schemes.liwei_0616_7y01_cons_say_k3_div_k10.inference import liwei_0616_pit_window, run_7y01_for_window_silent


SCHEME_ID = "liwei_0616_7y01_cons_say_k3_div_k10"
BENCHMARK_ID = "liwei_0616_7y_01"
DATA_SOURCE = "framework_db_aligned"
TARGET_TENOR = "7Y"
HORIZON = 5
BACKTEST_INPUT_START = "2010-07-27"
BACKTEST_INPUT_END = "2026-05-29"
BACKTEST_START = "2025-01-01"
BACKTEST_END = "2026-05-29"
BACKTEST_PREDICT_START_DATE = "2025-01-01"
LIVE_TARGET_CUTOFF = "2026-06-01"
DEFAULT_N_WORKERS = 10
DEFAULT_BATCH_MODE = "daily"


def run_historical_prediction(
    *,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None = None,
    n_workers: int = DEFAULT_N_WORKERS,
    feature_dates: Iterable[str] | None = None,
    batch_mode: str = DEFAULT_BATCH_MODE,
    use_phase_a_cache: bool = False,
) -> pd.DataFrame:
    """逐 feature_date 运行 7Y_01 PIT 窗口，返回历史预测明细。"""
    is_targeted_sample = feature_dates is not None
    dates = _historical_feature_dates(daily_df, feature_dates=feature_dates)
    if not dates:
        raise RuntimeError("liwei_0616 7Y_01 historical prediction has no feature dates")
    model_context_end = _source_context_end(daily_df, default=max(dates))

    if batch_mode == "monthly":
        groups = [dates] if is_targeted_sample else _month_groups(dates)
        phase_a_caches = (
            _build_phase_a_caches(
                dates=dates,
                daily_df=daily_df,
                weekly_df=weekly_df,
                monthly_df=monthly_df,
                date_to_week=date_to_week,
                n_workers=n_workers,
                model_context_end=model_context_end,
            )
            if use_phase_a_cache
            else None
        )
        records: list[dict[str, Any]] = []
        for group in groups:
            records.extend(
                _run_monthly_batch_group(
                    group_dates=group,
                    daily_df=daily_df,
                    weekly_df=weekly_df,
                    monthly_df=monthly_df,
                    date_to_week=date_to_week,
                    n_workers=n_workers,
                    model_context_end=model_context_end,
                    phase_a_caches=phase_a_caches,
                )
            )
        detail = pd.DataFrame(records)
        if detail.empty:
            raise RuntimeError("liwei_0616 7Y_01 historical prediction has no rows")
        return detail.sort_values("anchor_date").reset_index(drop=True)
    if batch_mode != "daily":
        raise ValueError(f"unsupported 7Y_01 batch mode: {batch_mode}")

    records: list[dict[str, Any]] = []
    for feature_date in dates:
        window = liwei_0616_pit_window(feature_date, source_end=model_context_end)
        detail = run_7y01_for_window_silent(
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
            raise RuntimeError(f"liwei_0616 7Y_01 produced no row for feature_date={feature_date}")
        records.append(clean_json(matched.tail(1).iloc[0].to_dict()))
    detail = pd.DataFrame(records)
    if detail.empty:
        raise RuntimeError("liwei_0616 7Y_01 historical prediction has no rows")
    return detail.sort_values("anchor_date").reset_index(drop=True)


def _run_monthly_batch_group(
    *,
    group_dates: list[str],
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None,
    n_workers: int,
    model_context_end: str,
    phase_a_caches: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    dates = sorted({str(day) for day in group_dates})
    batch_end = max(dates)
    source_current_start = _source_current_start(dates)
    window = liwei_0616_pit_window(
        batch_end,
        source_end=model_context_end,
        current_start=source_current_start,
        current_end=batch_end,
    )
    detail = run_7y01_for_window_silent(
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
    by_date = {str(row.get("anchor_date")): clean_json(row) for row in detail.to_dict("records")}
    missing = [day for day in dates if day not in by_date]
    if missing:
        raise RuntimeError(f"liwei_0616 7Y_01 monthly batch missed feature dates: {missing}")
    return [by_date[day] for day in dates]


def _build_phase_a_caches(
    *,
    dates: list[str],
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    monthly_df: pd.DataFrame,
    date_to_week: dict[str, int | str] | None,
    n_workers: int,
    model_context_end: str,
) -> dict[str, Any]:
    """为同一 source historical context 预计算各 baseline 的 Phase A 输出。"""
    if not dates:
        return {}
    cache_start = _source_current_start(dates)
    cache_end = max(dates)
    window = liwei_0616_pit_window(
        cache_end,
        source_end=model_context_end,
        current_start=cache_start,
        current_end=cache_end,
    )
    caches: dict[str, Any] = {}
    for baseline in required_baselines():
        with redirect_stdout(sys.stderr):
            _, ctx = run_prediction(
                model_config(
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
        if not isinstance(ctx, dict) or "phase_a_cache" not in ctx:
            raise RuntimeError(f"7Y_01 baseline {baseline} did not return Phase A cache")
        caches[str(baseline)] = ctx["phase_a_cache"]
    return caches


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
    """返回 source latest 窗口的当前段起点：样本起始月第一天。"""
    first = min(str(day) for day in dates)
    return pd.Timestamp(first).replace(day=1).strftime("%Y-%m-%d")


def _month_groups(dates: list[str]) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for day in sorted({str(item) for item in dates}):
        groups.setdefault(day[:7], []).append(day)
    return [groups[key] for key in sorted(groups)]


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


def run_liwei_0616_7y01_cons_say_k3_div_k10_reproduction(
    *,
    persist: bool = True,
    n_workers: int = DEFAULT_N_WORKERS,
    engine: Engine | None = None,
    sample_dates: Iterable[str] | None = None,
    batch_mode: str = DEFAULT_BATCH_MODE,
    input_end: str | None = None,
    use_phase_a_cache: bool = False,
) -> dict[str, Any]:
    """执行 liwei_0616 7Y_01 DB-aligned 历史复现。"""
    sample_date_list = sorted({str(day) for day in sample_dates or []})
    if persist and sample_date_list:
        raise ValueError("sample mode cannot persist")

    started = time.time()
    own_engine = engine is None
    engine = engine or create_sqlalchemy_engine()
    try:
        output_root = benchmark_input_root(BENCHMARK_ID)
        calendar = get_calendar(engine)
        effective_input_end = _effective_input_end(sample_date_list, calendar, requested_input_end=input_end)
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
        detail = run_historical_prediction(
            daily_df=daily_artifact.dataframe,
            weekly_df=weekly_artifact.dataframe,
            monthly_df=monthly_artifact.dataframe,
            date_to_week=date_to_week,
            n_workers=n_workers,
            feature_dates=sample_date_list or None,
            batch_mode=batch_mode,
            use_phase_a_cache=use_phase_a_cache,
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
            raise RuntimeError("liwei_0616 7Y_01 reproduction produced no rows")
        output = make_run_output(
            scheme_id=SCHEME_ID,
            data_source=DATA_SOURCE,
            start_date=min(str(row["predict_date"]) for row in rows),
            end_date=max(str(row["predict_date"]) for row in rows),
            rows=rows,
            benchmark_id=BENCHMARK_ID,
        )
        output.summary.update(
            {
                "source_model_id": SOURCE_MODEL_ID,
                "source_model_config": PROD_CONFIG["name"],
                "vote_baselines": list(PROD_CONFIG["baselines"]),
                "fallback_baseline": str(PROD_CONFIG["fallback"]),
                "streak_K": int(PROD_CONFIG["streak_K"]),
                "backtest_scope": "targeted_sample" if sample_date_list else "full_historical",
                "sample_dates": sample_date_list,
                "batch_mode": batch_mode,
                "phase_a_cache": bool(use_phase_a_cache),
                "daily_input_artifact_path": str(daily_artifact.path),
                "weekly_input_artifact_path": str(weekly_artifact.path),
                "monthly_input_artifact_path": str(monthly_artifact.path),
                "input_artifact_hash": getattr(daily_artifact, "content_hash", None),
                "weekly_input_artifact_hash": getattr(weekly_artifact, "content_hash", None),
                "monthly_input_artifact_hash": getattr(monthly_artifact, "content_hash", None),
                "weekly_input_end_week": int(weekly_end_week),
                "weekly_input_as_of_date": effective_input_end,
                **({"backtest_input_end": effective_input_end} if sample_date_list or input_end else {}),
            }
        )
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


def _effective_input_end(sample_dates: list[str], calendar, *, requested_input_end: str | None = None) -> str:
    if requested_input_end:
        return str(requested_input_end)
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
            "vote_score": extra.get("vote_score"),
        }
        for baseline in required_baselines():
            item[f"{baseline}_score"] = baseline_scores.get(baseline)
            item[f"{baseline}_dir"] = baseline_signs.get(baseline)
        compact_rows.append(item)
    return compact_rows


def _validate_before_persist(rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise RuntimeError("cannot persist empty 7Y_01 backtest output")
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
    parser.add_argument("--batch-mode", choices=["daily", "monthly"], default=DEFAULT_BATCH_MODE)
    parser.add_argument("--input-end")
    parser.add_argument("--phase-a-cache", action="store_true")
    args = parser.parse_args()
    result = run_liwei_0616_7y01_cons_say_k3_div_k10_reproduction(
        persist=not args.no_persist,
        n_workers=args.n_workers,
        sample_dates=_parse_sample_dates(args.sample_dates),
        batch_mode=args.batch_mode,
        input_end=args.input_end,
        use_phase_a_cache=args.phase_a_cache,
    )
    print(json.dumps(clean_json(result), ensure_ascii=False))


if __name__ == "__main__":
    main()
