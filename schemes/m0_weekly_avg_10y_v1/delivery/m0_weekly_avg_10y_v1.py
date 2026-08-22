#!/usr/bin/env python3
"""M0 周均值方向 · 10Y 国债收益率方向预测（Blackbox V2 Contract 1.0 扩展）。

零参数、不训练：桶均 = 桶内交易日收盘收益率算术平均；桶收 = 桶内最后交易日收盘；
gap = 桶收 − 桶均；predicted_direction = +1 若 gap >= 0 否则 -1。收益率上行 = 债券价格下行。
scheme_id = m0_weekly_avg_10y_v1。
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

TENOR = "10Y"
YIELD_COL = "TB0YWI0C"
TASK_TYPE = "weekly_average"
TARGET_RULE = "target_week_average_yield_vs_feature_week_average_yield"
HORIZON = 1
NEEDS_CALENDAR = True

REQUIRED_FIELDS = [
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SIXKEY_RE = re.compile(r"^\d{6}$")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _parse_date(s: str) -> pd.Timestamp:
    if not isinstance(s, str) or not _DATE_RE.match(s):
        raise ValueError(f"非法日期: {s!r}")
    return pd.Timestamp(s)


def _validate_request(req: dict) -> None:
    if set(req) != set(REQUIRED_FIELDS):
        raise ValueError(f"Request 字段必须恰好为 {REQUIRED_FIELDS}, 实际 {sorted(req)}")
    rid = req["request_id"]
    if not isinstance(rid, str) or rid.strip() == "":
        raise ValueError("request_id 必须为非空字符串")
    f = _parse_date(str(req["feature_date"]))
    p = _parse_date(str(req["predict_date"]))
    t = _parse_date(str(req["target_date"]))
    if not (f <= p <= t) or not (f < t):
        raise ValueError("要求 feature_date <= predict_date <= target_date 且 feature_date < target_date")
    if not _DATE_RE.match(str(req["daily_cutoff_key"])):
        raise ValueError("daily_cutoff_key 必须为 YYYY-MM-DD")
    for k in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not _SIXKEY_RE.match(str(req[k])):
            raise ValueError(f"{k} 必须为六位数字字符串")


def _read_daily(data_dir: Path) -> pd.DataFrame:
    p = data_dir / "daily_output.csv"
    if not p.exists():
        raise FileNotFoundError(f"缺少 {p}")
    df = pd.read_csv(p, dtype={"date": "string"})
    if "date" not in df.columns:
        raise ValueError("daily_output.csv 缺少 date 列")
    if YIELD_COL not in df.columns:
        raise ValueError(f"daily_output.csv 缺少消费列 {YIELD_COL}")
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"], format="%Y-%m-%d", errors="raise"),
        "y": pd.to_numeric(df[YIELD_COL], errors="coerce"),
    })
    if out["date"].duplicated().any():
        raise ValueError("daily_output.csv 的 date 必须唯一")
    return out.sort_values("date").reset_index(drop=True)


def _read_calendar(data_dir: Path) -> pd.DataFrame:
    p = data_dir / "api_wind_date.csv"
    if not p.exists():
        raise FileNotFoundError(f"周历方案需要平台提供 {p}（--platform-input api-wind-date-v1）")
    cal = pd.read_csv(p, dtype={"rdate": "string", "week_id": "string"})
    if list(cal.columns) != ["rdate", "week_id"]:
        raise ValueError("api_wind_date.csv 必须恰为 rdate,week_id 两列")
    if cal["rdate"].duplicated().any():
        raise ValueError("api_wind_date.csv 的 rdate 必须唯一")
    return cal


def _current_bucket(sub_all: pd.DataFrame, sub: pd.DataFrame, calendar, req: dict, cutoff: pd.Timestamp) -> pd.DataFrame:
    wk = str(req["weekly_cutoff_key"])
    cut_s = req["daily_cutoff_key"]
    hit = calendar.loc[calendar["rdate"] == cut_s, "week_id"]
    if len(hit) != 1:
        raise ValueError("daily_cutoff_key 未在 api_wind_date 中唯一映射")
    if str(hit.iloc[0]) != wk:
        raise ValueError("daily_cutoff_key 映射的 week_id 不等于 weekly_cutoff_key")
    cal = calendar.set_index("rdate")["week_id"]
    s = sub.assign(_wid=sub["date"].dt.strftime("%Y-%m-%d").map(cal))
    return s[s["_wid"] == wk]


def _predict_one(daily: pd.DataFrame, calendar, req: dict) -> int:
    cutoff = _parse_date(str(req["daily_cutoff_key"]))
    sub_all = daily[daily["date"] <= cutoff]
    sub = sub_all.dropna(subset=["y"])
    if sub.empty:
        raise ValueError("截断后无可用数据")
    bucket = _current_bucket(sub_all, sub, calendar, req, cutoff)
    if bucket.empty:
        raise ValueError("本桶截断后无数据")
    # 桶均/桶收用单组 groupby.agg 计算：与回测基线的分组聚合走同一浮点路径，
    # 保证 gap 恰好为 0 的极端桶（收盘==均值）方向判定与基线逐位一致。
    b = bucket.sort_values("date")
    agg = b.groupby(np.zeros(len(b), dtype="int64"), sort=False).agg(
        mean=("y", "mean"), close=("y", "last"))
    mean = float(agg["mean"].iloc[0])
    close = float(agg["close"].iloc[0])
    return 1 if (close - mean) >= 0 else -1


def _load_inputs(data_dir: Path):
    daily = _read_daily(data_dir)
    calendar = _read_calendar(data_dir)
    return daily, calendar


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def cmd_predict(args) -> int:
    data_dir = Path(args.data_dir)
    req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    _validate_request(req)
    daily, calendar = _load_inputs(data_dir)
    direction = _predict_one(daily, calendar, req)
    out = {
        "request_id": req["request_id"],
        "predict_date": req["predict_date"],
        "feature_date": req["feature_date"],
        "target_date": req["target_date"],
        "predicted_direction": int(direction),
    }
    _atomic_write(Path(args.output), json.dumps(out, ensure_ascii=False) + "\n")
    return 0


def cmd_backtest(args) -> int:
    data_dir = Path(args.data_dir)
    with open(args.requests, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        if set(fields) != set(REQUIRED_FIELDS):
            raise ValueError(f"requests.csv 表头必须恰为 {REQUIRED_FIELDS}, 实际 {fields}")
        rows = list(reader)
    if not rows:
        raise ValueError("空批次")
    seen = set()
    for r in rows:
        _validate_request({k: r[k] for k in REQUIRED_FIELDS})
        if r["request_id"] in seen:
            raise ValueError(f"重复 request_id: {r['request_id']}")
        seen.add(r["request_id"])
    daily, calendar = _load_inputs(data_dir)
    lines = ["request_id,predict_date,feature_date,target_date,predicted_direction"]
    for r in rows:
        d = _predict_one(daily, calendar, r)
        lines.append(f"{r['request_id']},{r['predict_date']},{r['feature_date']},{r['target_date']},{d}")
    _atomic_write(Path(args.output), "\n".join(lines) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=f"M0 {TASK_TYPE} {TENOR} 方向预测")
    subs = parser.add_subparsers(dest="cmd", required=True)
    pp = subs.add_parser("predict")
    pp.add_argument("--request", required=True)
    pp.add_argument("--data-dir", required=True)
    pp.add_argument("--output", required=True)
    bb = subs.add_parser("backtest")
    bb.add_argument("--requests", required=True)
    bb.add_argument("--data-dir", required=True)
    bb.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.cmd == "predict":
            return cmd_predict(args)
        return cmd_backtest(args)
    except BaseException as exc:
        _log(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
