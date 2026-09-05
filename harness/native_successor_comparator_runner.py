from __future__ import annotations

import argparse
import csv
import math
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd


REQUEST_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "daily_cutoff_key",
    "weekly_cutoff_key",
    "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)
W1_TARGETS = {
    ("t1_daily", "t1_daily_5y_bbv2", "5Y"),
    ("t1_daily", "t1_daily_10y_bbv2", "10Y"),
    ("t5_daily", "t5_daily_3y_bbv2", "3Y"),
    ("t5_daily", "t5_daily_5y_bbv2", "5Y"),
    ("t5_daily", "t5_daily_7y_bbv2", "7Y"),
    ("t5_daily", "t5_daily_10y_bbv2", "10Y"),
    (
        "weekly_5y_direct_0529",
        "weekly_5y_direct_0529_bbv2",
        "5Y",
    ),
    (
        "weekly_7y_cross_d_overlay_0529",
        "weekly_7y_cross_d_overlay_0529_bbv2",
        "7Y",
    ),
    (
        "weekly_10y_d_overlay_0529",
        "weekly_10y_d_overlay_0529_bbv2",
        "10Y",
    ),
}
W2_V28_TARGETS = {
    ("daily_5y_2_v28", "daily_5y_2_v28_bbv2", "5Y"),
    ("daily_7y_1_v28", "daily_7y_1_v28_bbv2", "7Y"),
}
APPROVED_TARGETS = W1_TARGETS | W2_V28_TARGETS
V28_ADDITIVE_MONTHLY_COLUMNS = {
    "M0041340",
    "M0041341",
    "M0041342",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute the exact W1/W2 Native core for migration comparison"
    )
    parser.add_argument("--old-scheme-id", required=True)
    parser.add_argument("--new-scheme-id", required=True)
    parser.add_argument("--target-tenor", required=True)
    parser.add_argument("--requests", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    identity = (
        args.old_scheme_id,
        args.new_scheme_id,
        args.target_tenor,
    )
    if identity not in APPROVED_TARGETS:
        raise ValueError(
            "controlled Native comparator only supports approved W1/W2 targets"
        )
    requests = _load_requests(args.requests, new_scheme_id=args.new_scheme_id)
    if identity in W2_V28_TARGETS:
        rows = _run_v28(
            requests,
            data_dir=args.data_dir,
            old_scheme_id=args.old_scheme_id,
        )
    elif args.old_scheme_id in {"t1_daily", "t5_daily"}:
        rows = _run_daily(
            requests,
            data_dir=args.data_dir,
            old_scheme_id=args.old_scheme_id,
            target_tenor=args.target_tenor,
        )
    else:
        rows = _run_weekly(
            requests,
            data_dir=args.data_dir,
            old_scheme_id=args.old_scheme_id,
        )
    _atomic_write(args.output, rows)
    return 0


def _load_requests(path: Path, *, new_scheme_id: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(REQUEST_FIELDS):
            raise ValueError("Request artifact fields are invalid")
        rows = list(reader)
    if not rows:
        raise ValueError("Request artifact must not be empty")
    seen: set[str] = set()
    for row in rows:
        for field in ("predict_date", "feature_date", "target_date"):
            parsed = date.fromisoformat(row[field])
            if parsed.isoformat() != row[field]:
                raise ValueError(f"Request {field} must use strict ISO format")
        expected_request_id = (
            f"{new_scheme_id}:{row['predict_date']}:"
            f"{row['feature_date']}:{row['target_date']}"
        )
        if row["request_id"] != expected_request_id:
            raise ValueError("Request id is not the canonical successor identity")
        if row["request_id"] in seen:
            raise ValueError("Request artifact contains duplicate request_id")
        seen.add(row["request_id"])
        if row["daily_cutoff_key"] != row["feature_date"]:
            raise ValueError("Request daily cutoff differs from feature_date")
        for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
            value = row[field]
            if len(value) != 6 or not value.isdigit():
                raise ValueError(f"Request {field} must be a six-digit key")
    return rows


def _run_v28(
    requests: list[dict[str, str]],
    *,
    data_dir: Path,
    old_scheme_id: str,
) -> list[dict[str, object]]:
    """按 Blackbox 月批次 cutoff 调用对应 V28 Native 窗口。"""
    calendar = pd.read_csv(
        data_dir / "api_wind_date.csv",
        dtype={"week_id": "string"},
        low_memory=False,
    )
    daily = pd.read_csv(data_dir / "daily_output.csv", low_memory=False)
    weekly = pd.read_csv(
        data_dir / "weekly_output.csv",
        dtype={"week_id": "string"},
        low_memory=False,
    )
    monthly = pd.read_csv(
        data_dir / "monthly_output.csv",
        dtype={"month_id": "string"},
        low_memory=False,
    )
    catalog = pd.read_csv(data_dir / "factor_catalog.csv", low_memory=False)
    if not {"rdate", "week_id"}.issubset(calendar.columns):
        raise ValueError("V28 comparator calendar lacks rdate/week_id")
    if "date" not in daily.columns:
        raise ValueError("V28 comparator daily input lacks date")
    if "week_id" not in weekly.columns or "month_id" not in monthly.columns:
        raise ValueError("V28 comparator periodic inputs lack cutoff keys")
    if catalog.empty:
        raise ValueError("V28 comparator factor catalog is empty")

    calendar["rdate"] = pd.to_datetime(calendar["rdate"], errors="raise")
    daily["date"] = pd.to_datetime(daily["date"], errors="raise")
    calendar["week_id"] = pd.to_numeric(
        calendar["week_id"], errors="raise"
    ).astype(int)
    weekly["week_id"] = pd.to_numeric(weekly["week_id"], errors="raise").astype(
        int
    )
    monthly["month_id"] = pd.to_numeric(
        monthly["month_id"], errors="raise"
    ).astype(int)
    calendar = calendar.sort_values("rdate").drop_duplicates("rdate", keep="last")
    daily = daily.sort_values("date").drop_duplicates("date", keep="last")
    weekly = weekly.sort_values("week_id").drop_duplicates("week_id", keep="last")
    monthly = monthly.sort_values("month_id").drop_duplicates(
        "month_id", keep="last"
    )
    monthly = monthly.drop(
        columns=sorted(V28_ADDITIVE_MONTHLY_COLUMNS), errors="ignore"
    )
    daily_dates = set(daily["date"])
    weekly_keys = set(weekly["week_id"])
    monthly_keys = set(monthly["month_id"])
    week_by_date = calendar.set_index("rdate")["week_id"]
    for request in requests:
        feature = pd.Timestamp(request["feature_date"])
        weekly_cutoff = int(request["weekly_cutoff_key"])
        monthly_cutoff = int(request["monthly_cutoff_key"])
        if (
            feature not in daily_dates
            or weekly_cutoff not in weekly_keys
            or monthly_cutoff not in monthly_keys
            or feature not in week_by_date.index
            or int(week_by_date.loc[feature]) != weekly_cutoff
        ):
            raise ValueError("V28 Request cutoff is absent or inconsistent")

    if old_scheme_id == "daily_5y_2_v28":
        from schemes.daily_5y_2_v28.inference import (
            run_v28_for_feature_window,
        )
    elif old_scheme_id == "daily_7y_1_v28":
        from schemes.daily_7y_1_v28.inference import (
            run_v28_for_feature_window,
        )
    else:
        raise ValueError("controlled V28 comparator identity is invalid")

    grouped: dict[str, list[dict[str, str]]] = {}
    for request in requests:
        grouped.setdefault(request["feature_date"][:7], []).append(request)
    predictions: dict[str, object] = {}
    for month, batch in sorted(grouped.items()):
        feature_end = max(request["daily_cutoff_key"] for request in batch)
        weekly_end = max(int(request["weekly_cutoff_key"]) for request in batch)
        monthly_end = max(int(request["monthly_cutoff_key"]) for request in batch)
        daily_input = daily.loc[daily["date"] <= pd.Timestamp(feature_end)].copy()
        weekly_input = weekly.loc[weekly["week_id"] <= weekly_end].copy()
        monthly_input = monthly.loc[monthly["month_id"] <= monthly_end].copy()
        calendar_input = calendar.loc[
            calendar["rdate"] <= pd.Timestamp(feature_end)
        ].copy()
        date_to_week = dict(
            zip(
                calendar_input["rdate"].dt.strftime("%Y-%m-%d"),
                calendar_input["week_id"],
                strict=True,
            )
        )
        details = run_v28_for_feature_window(
            daily_df=daily_input,
            weekly_df=weekly_input,
            monthly_df=monthly_input,
            date_to_week=date_to_week,
            window_start=f"{month}-01",
            window_end=feature_end,
            require_labels=False,
            n_workers=8,
        )
        if not isinstance(details, pd.DataFrame) or details.empty:
            raise RuntimeError(f"V28 Native core returned no rows for {month}")
        if not {"anchor_date", "prediction"}.issubset(details.columns):
            raise ValueError("V28 Native core result fields are invalid")
        anchors = details["anchor_date"].astype(str)
        if anchors.duplicated().any():
            raise ValueError("V28 Native core returned duplicate anchor_date")
        by_date = dict(zip(anchors, details["prediction"], strict=True))
        for request in batch:
            feature_date = request["feature_date"]
            if feature_date not in by_date:
                raise ValueError("V28 Native core omitted Request feature_date")
            predictions[request["request_id"]] = by_date[feature_date]
    return [
        _result(request, request["feature_date"], predictions[request["request_id"]])
        for request in requests
    ]


def _run_daily(
    requests: list[dict[str, str]],
    *,
    data_dir: Path,
    old_scheme_id: str,
    target_tenor: str,
) -> list[dict[str, object]]:
    daily = pd.read_csv(data_dir / "daily_output.csv", low_memory=False)
    if "date" not in daily.columns:
        raise ValueError("daily_output.csv is missing date")
    daily["date"] = pd.to_datetime(daily["date"], errors="raise")
    daily = daily.sort_values("date").drop_duplicates("date", keep="last")
    rows: list[dict[str, object]] = []
    if old_scheme_id == "t1_daily":
        from schemes.t1_daily.core.config import TENOR_CONFIGS
        from schemes.t1_daily.core.lgbm_predictor import predict_latest_for_config

        matching = [config for config in TENOR_CONFIGS.values() if config.tenor == target_tenor]
        if len(matching) != 1:
            raise ValueError("Native T+1 target config is not unique")
        config = matching[0]
        for request in requests:
            result = predict_latest_for_config(
                daily,
                config,
                target_date=request["target_date"],
            )
            rows.append(_result(request, result.feature_date, result.pred_label))
        return rows

    from schemes.t5_daily.latest_prediction import TENOR_MODULES, predict_latest_for_module

    module = TENOR_MODULES[target_tenor]
    for request in requests:
        native_predict_date = _exclusive_upper_bound_after_feature(
            daily,
            request["feature_date"],
        )
        result = predict_latest_for_module(
            module,
            daily,
            native_predict_date,
            n_jobs=8,
        )
        rows.append(_result(request, result.feature_date, result.vote_pred))
    return rows


def _exclusive_upper_bound_after_feature(
    daily: pd.DataFrame,
    feature_date: str,
) -> str:
    """把 Request 的闭区间 feature cutoff 转为旧 T+5 core 的开区间上界。"""
    feature = pd.Timestamp(feature_date)
    dates = daily["date"]
    if not dates.eq(feature).any():
        raise ValueError("Request feature_date is absent from daily input")
    later = dates.loc[dates.gt(feature)]
    if later.empty:
        raise ValueError("Native T+5 comparison requires one row after feature_date")
    return later.iloc[0].date().isoformat()


def _run_weekly(
    requests: list[dict[str, str]],
    *,
    data_dir: Path,
    old_scheme_id: str,
) -> list[dict[str, object]]:
    calendar = pd.read_csv(data_dir / "api_wind_date.csv", low_memory=False)
    daily = pd.read_csv(data_dir / "daily_output.csv", low_memory=False)
    weekly = pd.read_csv(data_dir / "weekly_output.csv", low_memory=False)
    if not {"rdate", "week_id"}.issubset(calendar.columns):
        raise ValueError("api_wind_date.csv lacks rdate/week_id")
    if "date" not in daily.columns or "week_id" not in weekly.columns:
        raise ValueError("weekly comparator inputs lack date/week_id")
    calendar["rdate"] = pd.to_datetime(calendar["rdate"], errors="raise")
    daily["date"] = pd.to_datetime(daily["date"], errors="raise")
    calendar["week_id"] = pd.to_numeric(calendar["week_id"], errors="raise").astype(int)
    weekly["week_id"] = pd.to_numeric(weekly["week_id"], errors="raise").astype(int)
    week_ends = (
        daily.merge(
            calendar.loc[:, ["rdate", "week_id"]],
            left_on="date",
            right_on="rdate",
            how="left",
            validate="one_to_one",
        )
        .dropna(subset=["week_id"])
        .groupby("week_id", sort=True, observed=True)["date"]
        .max()
        .to_dict()
    )
    week_ids = [int(request["weekly_cutoff_key"]) for request in requests]
    for request, week_id in zip(requests, week_ids, strict=True):
        feature = week_ends.get(week_id)
        if feature is None or feature.date().isoformat() != request["feature_date"]:
            raise ValueError("Request feature date differs from observed week endpoint")
    lookback = {
        "weekly_5y_direct_0529": 60,
        "weekly_7y_cross_d_overlay_0529": 80,
        "weekly_10y_d_overlay_0529": 600,
    }[old_scheme_id]
    frame = weekly.loc[
        weekly["week_id"].between(min(week_ids) - lookback, max(week_ids))
    ].copy()
    if old_scheme_id == "weekly_5y_direct_0529":
        from schemes.weekly_5y_direct_0529.core.rule_vote import build_rule_vote

        predictions = build_rule_vote(frame).loc[
            :, ["week_id", "final_pred_label"]
        ].rename(columns={"final_pred_label": "predicted_direction"})
    elif old_scheme_id == "weekly_7y_cross_d_overlay_0529":
        from schemes.weekly_7y_cross_d_overlay_0529.core.cross_d_overlay import (
            build_cross_d_overlay,
        )

        predictions = build_cross_d_overlay(frame).loc[
            :, ["week_id", "cross_d_pred_label"]
        ].rename(columns={"cross_d_pred_label": "predicted_direction"})
    else:
        from schemes.weekly_10y_d_overlay_0529.core.d_overlay import build_d_overlay

        frame["week_date"] = frame["week_id"].map(week_ends)
        frame["model_date"] = frame["week_id"].map(_legacy_segment_anchor_date)
        predictions = build_d_overlay(frame).loc[
            :, ["week_id", "d_pred_label"]
        ].rename(columns={"d_pred_label": "predicted_direction"})
    predictions["week_id"] = pd.to_numeric(
        predictions["week_id"], errors="raise"
    ).astype(int)
    if predictions["week_id"].duplicated().any():
        raise ValueError("Native core returned duplicate week_id")
    by_week = predictions.set_index("week_id")["predicted_direction"].to_dict()
    return [
        _result(request, request["feature_date"], by_week.get(week_id, 0))
        for request, week_id in zip(requests, week_ids, strict=True)
    ]


def _result(
    request: Mapping[str, str],
    actual_feature_date: str,
    direction: object,
) -> dict[str, object]:
    if actual_feature_date != request["feature_date"]:
        raise ValueError("Native core feature_date differs from Request")
    if isinstance(direction, bool):
        raise ValueError("Native core direction is invalid")
    numeric = float(direction)
    if not math.isfinite(numeric) or not numeric.is_integer() or int(numeric) not in {-1, 0, 1}:
        raise ValueError("Native core direction is invalid")
    return {
        "request_id": request["request_id"],
        "predict_date": request["predict_date"],
        "feature_date": request["feature_date"],
        "target_date": request["target_date"],
        "predicted_direction": int(numeric),
    }


def _legacy_segment_anchor_date(week_id: int) -> str:
    text = str(int(week_id))
    year = int(text[:4])
    ordinal = int(text[4:])
    jan1 = pd.Timestamp(f"{year}-01-01")
    first_thursday = jan1 + pd.Timedelta(days=(3 - jan1.weekday()) % 7)
    first_anchor = first_thursday - pd.Timedelta(days=3)
    return (first_anchor + pd.Timedelta(weeks=ordinal - 1)).date().isoformat()


def _atomic_write(path: Path, rows: list[dict[str, object]]) -> None:
    if os.path.lexists(path):
        raise ValueError("Native comparator output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
