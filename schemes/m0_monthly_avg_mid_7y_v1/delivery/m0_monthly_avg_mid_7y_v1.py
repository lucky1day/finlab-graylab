#!/usr/bin/env python3
"""M0 月中均值方向 · 7Y 国债收益率方向预测（Blackbox V2 Contract 1.0）。

零参数、不训练：桶均 = 桶内交易日收盘收益率算术平均；桶收 = 桶内最后交易日收盘；
gap = 桶收 − 桶均；predicted_direction = +1 若 gap >= 0 否则 -1。收益率上行 = 债券价格下行。
scheme_id = m0_monthly_avg_mid_7y_v1。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

TENOR = '7Y'
YIELD_COL = 'TB7YWI0C'
TASK_TYPE = 'monthly_average'
TARGET_RULE = 'target_month_average_yield_vs_feature_month_average_yield'
BUCKET_KIND = 'monthly'
HORIZON = 1

REQUIRED_FIELDS = [
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SIXKEY_RE = re.compile(r"^\d{6}$")
_MIN_SPRING_GAP_DAYS = 6


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _parse_date(value: object) -> pd.Timestamp:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"非法日期: {value!r}")
    parsed = pd.to_datetime(value, format="%Y-%m-%d", errors="raise")
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"日期不是规范 YYYY-MM-DD: {value!r}")
    return parsed


def _validate_request(req: dict) -> None:
    if set(req) != set(REQUIRED_FIELDS):
        raise ValueError(f"Request 字段必须恰好为 {REQUIRED_FIELDS}, 实际 {sorted(req)}")
    rid = req["request_id"]
    if not isinstance(rid, str) or not rid.strip():
        raise ValueError("request_id 必须为非空字符串")
    feature = _parse_date(req["feature_date"])
    predict = _parse_date(req["predict_date"])
    target = _parse_date(req["target_date"])
    if not (feature <= predict <= target) or not feature < target:
        raise ValueError("要求 feature_date <= predict_date <= target_date 且 feature_date < target_date")
    if req["daily_cutoff_key"] != req["feature_date"]:
        raise ValueError("daily_cutoff_key 必须精确等于 feature_date")
    if target != feature + pd.Timedelta(days=1):
        raise ValueError("周期均值 target_date 必须是 feature_date 后一个自然日的桶指针")
    for key in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not isinstance(req[key], str) or not _SIXKEY_RE.fullmatch(req[key]):
            raise ValueError(f"{key} 必须为六位数字字符串")


def _read_daily(data_dir: Path) -> pd.DataFrame:
    path = data_dir / "daily_output.csv"
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {path}")
    frame = pd.read_csv(path, dtype="string", keep_default_na=False)
    if "date" not in frame.columns:
        raise ValueError("daily_output.csv 缺少 date 列")
    if YIELD_COL not in frame.columns:
        raise ValueError(f"daily_output.csv 缺少消费列 {YIELD_COL}")
    raw_dates = frame["date"].str.strip()
    if raw_dates.eq("").any():
        raise ValueError("daily_output.csv 的 date 不得为空")
    parsed_dates = pd.to_datetime(raw_dates, format="%Y-%m-%d", errors="raise")
    if not raw_dates.eq(parsed_dates.dt.strftime("%Y-%m-%d")).all():
        raise ValueError("daily_output.csv 的 date 必须为规范 YYYY-MM-DD")
    if raw_dates.duplicated().any():
        raise ValueError("daily_output.csv 的 date 必须唯一")
    return pd.DataFrame({
        "date": parsed_dates,
        "raw_y": frame[YIELD_COL].astype("string"),
    }).sort_values("date").reset_index(drop=True)


def _read_calendar(data_dir: Path) -> pd.DataFrame:
    path = data_dir / "api_wind_date.csv"
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {path}")
    frame = pd.read_csv(path, dtype="string", keep_default_na=False)
    if list(frame.columns) != ["rdate", "week_id"]:
        raise ValueError("api_wind_date.csv 列必须恰好为 rdate,week_id")
    if frame.empty:
        raise ValueError("api_wind_date.csv 不能为空")
    raw_dates = frame["rdate"].str.strip()
    if raw_dates.eq("").any() or raw_dates.duplicated().any():
        raise ValueError("api_wind_date.csv 的 rdate 必须非空且唯一")
    parsed_dates = pd.to_datetime(raw_dates, format="%Y-%m-%d", errors="raise")
    if not raw_dates.eq(parsed_dates.dt.strftime("%Y-%m-%d")).all():
        raise ValueError("api_wind_date.csv 的 rdate 必须为规范 YYYY-MM-DD")
    if not parsed_dates.is_monotonic_increasing:
        raise ValueError("api_wind_date.csv 的 rdate 必须严格升序")
    week_ids = frame["week_id"].str.strip()
    if not week_ids.str.fullmatch(r"\d{6}").all():
        raise ValueError("api_wind_date.csv 的 week_id 必须为六位数字字符串")
    return pd.DataFrame({"date": parsed_dates, "week_id": week_ids})


def _validate_calendar_mapping(calendar: pd.DataFrame, req: dict) -> pd.Timestamp:
    cutoff = _parse_date(req["daily_cutoff_key"])
    matches = calendar[calendar["date"] == cutoff]
    if len(matches) != 1:
        raise ValueError("daily_cutoff_key 必须在权威日历中唯一存在")
    if str(matches["week_id"].iloc[0]) != req["weekly_cutoff_key"]:
        raise ValueError("daily_cutoff_key 到 weekly_cutoff_key 的权威日历映射不一致")
    return cutoff


def _require_calendar_coverage(calendar: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> None:
    if calendar["date"].iloc[0] > start or calendar["date"].iloc[-1] < end:
        raise ValueError(
            f"权威日历未完整覆盖业务窗口 {start:%Y-%m-%d} 至 {end:%Y-%m-%d}"
        )


def _month_start(value: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(value.year, value.month, 1)


def _monthly_window(cutoff: pd.Timestamp, calendar: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    label_month = _month_start(cutoff) if cutoff.day <= 15 else _month_start(cutoff) + pd.offsets.MonthBegin(1)
    start = label_month - pd.offsets.MonthBegin(1) + pd.Timedelta(days=15)
    end = label_month + pd.Timedelta(days=14)
    _require_calendar_coverage(calendar, start, end)
    expected = calendar[(calendar["date"] >= start) & (calendar["date"] <= end)]["date"]
    if expected.empty or expected.iloc[-1] != cutoff:
        raise ValueError("feature_date 必须是 MID 桶的最后交易日锚点")
    return start, end


def _quarterly_window(cutoff: pd.Timestamp, calendar: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_month = ((cutoff.month - 1) // 3) * 3 + 1
    start = pd.Timestamp(cutoff.year, start_month, 1)
    end = start + pd.offsets.QuarterEnd(startingMonth=3)
    _require_calendar_coverage(calendar, start, end)
    expected = calendar[(calendar["date"] >= start) & (calendar["date"] <= end)]["date"]
    if expected.empty or expected.iloc[-1] != cutoff:
        raise ValueError("feature_date 必须是自然季度桶的最后交易日锚点")
    return start, end


def _spring_boundary(year: int, calendar: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(year, 1, 1)
    end = pd.Timestamp(year, 3, 15)
    _require_calendar_coverage(calendar, start, end)
    days = calendar[(calendar["date"] >= start) & (calendar["date"] <= end)]["date"].reset_index(drop=True)
    if len(days) < 2:
        raise ValueError(f"春节日历窗口交易日不足: year={year}")
    gaps = days.diff().dt.days.iloc[1:]
    maximum = int(gaps.max())
    if maximum < _MIN_SPRING_GAP_DAYS:
        raise ValueError(f"春节最长停市间隔不足 {_MIN_SPRING_GAP_DAYS} 天: year={year}")
    positions = list(gaps[gaps == maximum].index)
    if len(positions) != 1:
        raise ValueError(f"春节最长停市间隔不唯一: year={year}")
    index = positions[0]
    return days.iloc[index - 1], days.iloc[index]


def _annual_window(cutoff: pd.Timestamp, calendar: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    previous_day, _next_day = _spring_boundary(cutoff.year, calendar)
    if previous_day != cutoff:
        raise ValueError("feature_date 必须是春节前年桶的最后交易日锚点")
    _prior_previous, start = _spring_boundary(cutoff.year - 1, calendar)
    return start, cutoff


def _bucket_window(cutoff: pd.Timestamp, calendar: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    if BUCKET_KIND == "monthly":
        return _monthly_window(cutoff, calendar)
    if BUCKET_KIND == "quarterly":
        return _quarterly_window(cutoff, calendar)
    return _annual_window(cutoff, calendar)


def _strict_bucket(
    daily: pd.DataFrame,
    calendar: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    expected_dates = list(
        calendar[(calendar["date"] >= start) & (calendar["date"] <= end)]["date"]
    )
    if not expected_dates:
        raise ValueError("业务桶没有权威交易日")
    selected = daily[daily["date"].isin(set(expected_dates))].copy()
    actual_dates = list(selected["date"])
    if actual_dates != expected_dates:
        expected = {value.strftime("%Y-%m-%d") for value in expected_dates}
        actual = {value.strftime("%Y-%m-%d") for value in actual_dates}
        missing = sorted(expected - actual)
        raise ValueError(f"业务桶交易日不完整: missing={missing}")
    raw = selected["raw_y"].astype("string").str.strip()
    if raw.eq("").any():
        raise ValueError("业务桶消费列存在空值")
    try:
        values = pd.to_numeric(raw, errors="raise").astype("float64")
    except (TypeError, ValueError) as exc:
        raise ValueError("业务桶消费列存在非数值") from exc
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("业务桶消费列存在非有限值")
    return pd.DataFrame({"date": selected["date"], "y": values}).sort_values("date")


def _predict_one(daily: pd.DataFrame, calendar: pd.DataFrame, req: dict) -> int:
    cutoff = _validate_calendar_mapping(calendar, req)
    start, end = _bucket_window(cutoff, calendar)
    bucket = _strict_bucket(daily, calendar, start, end)
    # 保持原始交付的单组 groupby.agg 浮点路径，不改变 M0 方向语义。
    aggregate = bucket.groupby(
        np.zeros(len(bucket), dtype="int64"), sort=False
    ).agg(mean=("y", "mean"), close=("y", "last"))
    mean = float(aggregate["mean"].iloc[0])
    close = float(aggregate["close"].iloc[0])
    return 1 if (close - mean) >= 0 else -1


def _load_inputs(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    return _read_daily(data_dir), _read_calendar(data_dir)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def cmd_predict(args) -> int:
    req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    _validate_request(req)
    daily, calendar = _load_inputs(Path(args.data_dir))
    direction = _predict_one(daily, calendar, req)
    result = {
        "request_id": req["request_id"],
        "predict_date": req["predict_date"],
        "feature_date": req["feature_date"],
        "target_date": req["target_date"],
        "predicted_direction": int(direction),
    }
    _atomic_write(Path(args.output), json.dumps(result, ensure_ascii=False) + "\n")
    return 0


def cmd_backtest(args) -> int:
    with open(args.requests, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        if set(fields) != set(REQUIRED_FIELDS):
            raise ValueError(f"requests.csv 表头必须恰为 {REQUIRED_FIELDS}, 实际 {fields}")
        rows = list(reader)
    if not rows:
        raise ValueError("空批次")
    seen = set()
    for row in rows:
        _validate_request({key: row[key] for key in REQUIRED_FIELDS})
        if row["request_id"] in seen:
            raise ValueError(f"重复 request_id: {row['request_id']}")
        seen.add(row["request_id"])
    daily, calendar = _load_inputs(Path(args.data_dir))
    lines = ["request_id,predict_date,feature_date,target_date,predicted_direction"]
    for row in rows:
        direction = _predict_one(daily, calendar, row)
        lines.append(
            f"{row['request_id']},{row['predict_date']},{row['feature_date']},"
            f"{row['target_date']},{direction}"
        )
    _atomic_write(Path(args.output), "\n".join(lines) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=f"M0 {TASK_TYPE} {TENOR} 方向预测")
    commands = parser.add_subparsers(dest="cmd", required=True)
    predict = commands.add_parser("predict")
    predict.add_argument("--request", required=True)
    predict.add_argument("--data-dir", required=True)
    predict.add_argument("--output", required=True)
    backtest = commands.add_parser("backtest")
    backtest.add_argument("--requests", required=True)
    backtest.add_argument("--data-dir", required=True)
    backtest.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        return cmd_predict(args) if args.cmd == "predict" else cmd_backtest(args)
    except BaseException as exc:
        _log(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
