#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""one_y_t1_cross_tenor_intraday_v1 —— 1Y 国债日频 T+1 方向算法方案
(Blackbox V2 / Contract 1.0)。

单文件交付物。两个命令复用完全相同的数据处理、算法逻辑与方向映射:

    python one_y_t1_cross_tenor_intraday_v1.py predict  --request  request.json \
        --data-dir <data-dir> --output prediction.json
    python one_y_t1_cross_tenor_intraday_v1.py backtest --requests requests.csv \
        --data-dir <data-dir> --output backtest.csv

算法概要
--------
零拟合、零阈值搜索、无随机过程，只依赖 numpy 与 pandas。只消费 daily_output.csv。

    交易日序列 = TB1YWI0C 非空的行（按 date 升序）
    direction  = sign(TB1YWI0C[t] - TB1YWI0C[t-1])          方向源: 一日正动量
    B          = 3Y/5Y/7Y/10Y 中至少 1 个期限当日方向 == direction
    I          = sign(TB1YWI0C[t] - TB1YWI00[t]) == direction  日内延续
    predicted_direction = direction  当 (B 且 I 且 direction != 0)
                        = 0          其余情况

三个条件只使用 feature_date 当日及以前的信息，运行时点为 feature_date 收盘后。
`predicted_direction = +1` 表示 target_date 收盘收益率高于 feature_date 收盘,
`-1` 表示低于, `0` 表示本方案在该日不给出方向（见下）。

关于 0
------
本方案是三分类: B、I 两个确认条件构成一个显式的"中性/不出手"状态，历史上约 56%
的交易日落在这里（出手率约 44%）。两种情况会产生 0:
  * direction == 0 —— 1Y 当日收盘与昨收持平, 方向源本身不存在;
  * B 或 I 不成立 —— 跨期限确认或日内确认缺失。
这是算法设计上的中性方向，不是把异常、缺数或低置信度伪装成 0: 数据缺列、截止键
缺失、时间键非法等情况一律 fail-closed 非零退出, 不产生 Output。

关于 A 门与 signal_level
-----------------------
原方案还计算一个局部波动比 A = |Δ1Y| / mean(|Δ1Y|, 前 90 个交易日)，
按 A 是否 >= 0.45 把已出手的信号分成 CORE / EXTENSION 两层。
A **不参与出手判定**（a_mode="level"），因此不影响 predicted_direction。
Blackbox V2 的 Result 只有五个字段，无处承载 signal_level；本文件仍然计算它并写入
stderr 供审计，但分层记账需要在平台侧另行安排。

来源
----
等价重构自 `models-day/方案1y-BI/bi_model.py`（默认 Config，即 D03 冻结口径），
与 `models-day/方案1y-0823/predict_1y.py` 冻结版逐日一致。
"""
from __future__ import annotations

import argparse
import csv
from datetime import date as _date
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Sequence

import numpy as np
import pandas as pd

SCHEME_ID = "one_y_t1_cross_tenor_intraday_v1"
ALGORITHM_VERSION = "1.0.0"
TARGET_TENOR = "1Y"

# ---- 冻结参数（D03 口径，改任何一个都必须重新做样本外验证）----
TARGET_COLUMN = "TB1YWI0C"                  # 1Y 活跃券收盘（方向源）
OPEN_COLUMN = "TB1YWI00"                    # 1Y 活跃券开盘（条件 I）
TENOR_COLUMNS = ("TB3YWI0C", "TB5YWI0C", "TB7YWI0C", "TB0YWI0C")  # TB0Y 即 10Y
VOTE_MIN = 1                                # 条件 B: 至少几个期限同向
VOL_WINDOW = 90                             # 条件 A 的回看窗口（仅用于分层）
VOL_CUT = 0.45                              # 条件 A 的阈值（仅用于分层）
MIN_ROWS = 2                                # 至少两个交易日才能算出当日方向

CONSUMED_COLUMNS = (TARGET_COLUMN, OPEN_COLUMN, *TENOR_COLUMNS)
REQUEST_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
RESULT_FIELDS = (
    "request_id", "predict_date", "feature_date", "target_date",
    "predicted_direction",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY6_RE = re.compile(r"^\d{6}$")


class SchemeError(ValueError):
    """本方案的所有业务失败，统一转成非零退出码。"""


def _log(message: str) -> None:
    """日志只写 stderr；业务运行期间 stdout 必须为空。"""
    sys.stderr.write(f"[{SCHEME_ID}] {message}\n")


# --------------------------------------------------------------------------- #
# 1. Request 校验
# --------------------------------------------------------------------------- #
def _iso_date(value: Any, field: str, where: str) -> _date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise SchemeError(f"{where}: {field}='{value}' 不是规范 YYYY-MM-DD")
    try:
        parsed = _date.fromisoformat(value)
    except ValueError as exc:
        raise SchemeError(f"{where}: {field}='{value}' 不是合法日期") from exc
    if parsed.isoformat() != value:
        raise SchemeError(f"{where}: {field}='{value}' 不是规范 YYYY-MM-DD")
    return parsed


def validate_request(raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise SchemeError(f"{where}: Request 必须是一个对象")
    missing = [f for f in REQUEST_FIELDS if f not in raw]
    extra = sorted(set(raw) - set(REQUEST_FIELDS))
    if missing:
        raise SchemeError(f"{where}: 缺少字段 {missing}")
    if extra:
        raise SchemeError(f"{where}: 出现额外字段 {extra}")
    request: dict[str, str] = {}
    for field in REQUEST_FIELDS:
        value = raw[field]
        if not isinstance(value, str):
            raise SchemeError(
                f"{where}: 字段 {field} 必须是字符串, 实际 {type(value).__name__}")
        request[field] = value
    if not request["request_id"].strip():
        raise SchemeError(f"{where}: request_id 不能为空")
    feature = _iso_date(request["feature_date"], "feature_date", where)
    predict = _iso_date(request["predict_date"], "predict_date", where)
    target = _iso_date(request["target_date"], "target_date", where)
    cutoff = _iso_date(request["daily_cutoff_key"], "daily_cutoff_key", where)
    if not (feature <= predict <= target and feature < target):
        raise SchemeError(
            f"{where}: 必须满足 feature_date <= predict_date <= target_date "
            f"且 feature_date < target_date")
    if cutoff != feature:
        raise SchemeError(f"{where}: T+1 任务要求 daily_cutoff_key == feature_date")
    # 本方案不消费周频与月频文件，按 SOP §6.3 只做格式校验。
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not KEY6_RE.match(request[field]):
            raise SchemeError(f"{where}: {field}='{request[field]}' 必须是六位数字字符串")
    return request


def _reject_constant(value: str) -> None:
    raise SchemeError(f"Request JSON 中不允许常量 {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemeError(f"Request JSON 出现重复键: {key}")
        result[key] = value
    return result


def load_request_json(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SchemeError(f"无法读取 Request: {exc}") from exc
    return validate_request(raw, f"request {path.name}")


def load_requests_csv(path: Path) -> list[dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SchemeError(f"无法读取 Requests: {exc}") from exc
    reader = csv.reader(text.splitlines())
    try:
        header = [item.strip() for item in next(reader)]
    except StopIteration:
        raise SchemeError("Requests 文件为空 (没有表头)") from None
    if len(header) != len(set(header)) or set(header) != set(REQUEST_FIELDS):
        raise SchemeError(
            f"Requests 表头必须恰好是 7 个字段; "
            f"缺少={sorted(set(REQUEST_FIELDS) - set(header))} "
            f"多余={sorted(set(header) - set(REQUEST_FIELDS))}")
    requests: list[dict[str, str]] = []
    for number, row in enumerate(reader, start=2):
        if len(row) != len(header):
            raise SchemeError(f"Requests 第 {number} 行有 {len(row)} 个字段, 期望 7 个")
        requests.append(validate_request(dict(zip(header, row)), f"requests 第 {number} 行"))
    if not requests:
        raise SchemeError("Requests 批次为空 (至少需要 1 条)")
    ids = [item["request_id"] for item in requests]
    if len(ids) != len(set(ids)):
        raise SchemeError("Requests 批内 request_id 重复")
    return requests


# --------------------------------------------------------------------------- #
# 2. 读取 daily_output.csv（本方案唯一消费的文件）
# --------------------------------------------------------------------------- #
def load_daily(data_dir: Path) -> pd.DataFrame:
    if not data_dir.is_dir():
        raise SchemeError(f"--data-dir 不是一个目录: {data_dir}")
    path = data_dir / "daily_output.csv"
    if not path.is_file():
        raise SchemeError("缺少本方案实际消费的数据文件: daily_output.csv")
    try:
        header = pd.read_csv(path, encoding="utf-8-sig", nrows=0).columns.tolist()
    except Exception as exc:
        raise SchemeError(f"无法读取 daily_output.csv: {exc}") from exc
    header = [str(column).strip().lstrip("﻿") for column in header]
    if not header or header[0] != "date":
        raise SchemeError("daily_output.csv 第一列必须是时间键 date")
    if len(header) != len(set(header)):
        raise SchemeError("daily_output.csv 存在重复列名")
    missing = [column for column in CONSUMED_COLUMNS if column not in header]
    if missing:
        raise SchemeError(f"daily_output.csv 缺少本方案实际消费的字段: {missing}")
    try:
        frame = pd.read_csv(
            path, encoding="utf-8-sig", usecols=["date", *CONSUMED_COLUMNS],
            dtype={"date": "string"}, low_memory=False)
    except Exception as exc:
        raise SchemeError(f"无法读取 daily_output.csv: {exc}") from exc
    if frame.empty:
        raise SchemeError("daily_output.csv 没有数据行")
    parsed = pd.to_datetime(frame["date"], errors="coerce")
    if parsed.isna().any():
        raise SchemeError("daily_output.csv 的 date 存在无法解析为日期的值")
    frame["date"] = parsed.dt.normalize()
    if frame["date"].duplicated().any():
        raise SchemeError("daily_output.csv 的 date 必须唯一")
    if not frame["date"].is_monotonic_increasing:
        raise SchemeError("daily_output.csv 的 date 必须升序")
    for column in CONSUMED_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def slice_for_cutoff(daily: pd.DataFrame, cutoff_key: str) -> pd.DataFrame:
    """按本条 Request 的 daily_cutoff_key 独立截断（SOP §6.3）。"""
    cutoff = pd.Timestamp(cutoff_key).normalize()
    hit = np.flatnonzero(daily["date"].eq(cutoff).to_numpy())
    if hit.size != 1:
        raise SchemeError(f"daily_output.csv 中不存在截止键 date={cutoff_key}")
    frame = daily.iloc[: int(hit[0]) + 1]
    # 交易日序列由目标列非空定义（与研究实现一致）
    frame = frame.loc[frame[TARGET_COLUMN].notna()].reset_index(drop=True)
    if len(frame) < MIN_ROWS:
        raise SchemeError(
            f"截断后 {TARGET_COLUMN} 仅有 {len(frame)} 个有效交易日, "
            f"不足 {MIN_ROWS} 个, 无法计算当日方向")
    if frame["date"].iloc[-1] != cutoff:
        raise SchemeError(
            f"daily_cutoff_key={cutoff_key} 当日 {TARGET_COLUMN} 为空, "
            f"不是本方案定义的交易日")
    return frame


# --------------------------------------------------------------------------- #
# 3. 条件与方向（等价于 bi_model.compute_conditions + build_signal，默认 Config）
# --------------------------------------------------------------------------- #
def build_signal(frame: pd.DataFrame) -> tuple[int, str, dict[str, Any]]:
    close = frame[TARGET_COLUMN]
    change = close.diff()
    direction = np.sign(change)

    # 条件 A：仅用于 CORE / EXTENSION 分层，不参与出手判定。
    # shift(1) 必须保留：今日变动不得进入自己的归一化分母，否则是前视。
    prior_abs_mean = change.abs().shift(1).rolling(VOL_WINDOW).mean()
    vol_ratio = change.abs() / prior_abs_mean
    a_volgate = vol_ratio.ge(VOL_CUT)

    # 条件 B：跨期限同向投票。某期限缺失或零变动 -> sign 不等于 direction -> 不计票。
    votes = None
    for column in TENOR_COLUMNS:
        vote = np.sign(frame[column].diff()).eq(direction)
        votes = vote.astype(int) if votes is None else votes + vote.astype(int)
    b_cross_tenor = votes.ge(VOTE_MIN)

    # 条件 I：日内方向与相对昨收方向一致。close == open 时 sign=0 -> 不通过。
    i_intraday = np.sign(close - frame[OPEN_COLUMN]).eq(direction)

    gate = b_cross_tenor & i_intraday & direction.ne(0)
    predicted = direction.where(gate, 0.0).fillna(0.0).astype(int)

    row = len(frame) - 1
    signal = int(predicted.iloc[row])
    if signal not in (-1, 0, 1):
        raise SchemeError(f"算法产生了非法方向: {signal}")
    level = ("CORE" if bool(gate.iloc[row] and a_volgate.iloc[row])
             else "EXTENSION" if bool(gate.iloc[row]) else "NO_SIGNAL")
    detail = {
        "direction_source": float(direction.iloc[row])
        if np.isfinite(direction.iloc[row]) else None,
        "vote_sum": int(votes.iloc[row]),
        "B": bool(b_cross_tenor.iloc[row]),
        "I": bool(i_intraday.iloc[row]),
        "A": bool(a_volgate.iloc[row]),
        "vol_ratio": float(vol_ratio.iloc[row])
        if np.isfinite(vol_ratio.iloc[row]) else None,
        "signal_level": level,
        "trading_days_used": int(len(frame)),
    }
    return signal, level, detail


def predict_direction(daily: pd.DataFrame, request: dict[str, str]) -> int:
    frame = slice_for_cutoff(daily, request["daily_cutoff_key"])
    signal, level, detail = build_signal(frame)
    _log(f"request_id={request['request_id']} feature_date={request['feature_date']} "
         f"direction={signal:+d} signal_level={level} "
         f"vote_sum={detail['vote_sum']} B={detail['B']} I={detail['I']} "
         f"A={detail['A']}")
    return signal


def generate(requests: Sequence[dict[str, str]], daily: pd.DataFrame) -> list[dict[str, Any]]:
    """逐条 Request 按自己的截止键独立截断后计算（无跨 Request 状态）。"""
    rows: list[dict[str, Any]] = []
    for request in requests:
        direction = predict_direction(daily, request)
        rows.append({**{field: request[field] for field in RESULT_FIELDS[:-1]},
                     "predicted_direction": direction})
    return rows


# --------------------------------------------------------------------------- #
# 4. Output：先算完再原子替换；失败不留下任何 Output
# --------------------------------------------------------------------------- #
def validate_output_path(output: Path, data_dir: Path) -> Path:
    if os.path.lexists(output):
        raise SchemeError(f"--output 路径已存在: {output}")
    if not output.parent.is_dir():
        raise SchemeError(f"--output 的父目录不存在: {output.parent}")
    resolved = output.parent.resolve() / output.name
    root = data_dir.resolve()
    if resolved == root or root in resolved.parents:
        raise SchemeError(f"--output 不能位于只读的 --data-dir 之内: {output}")
    return output


def atomic_write(output: Path, payload: str) -> None:
    descriptor: int | None = None
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.lexists(output):
            raise SchemeError("原子替换前 --output 路径已出现")
        os.replace(temporary, output)
        temporary = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def serialize_csv(rows: Sequence[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(RESULT_FIELDS)
    writer.writerows([[row[field] for field in RESULT_FIELDS] for row in rows])
    return buffer.getvalue()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{SCHEME_ID}.py",
        description=(f"{TARGET_TENOR} 国债日频 T+1 方向算法方案 "
                     f"(Blackbox V2 Contract 1.0)。predict 处理单条 Request, "
                     f"backtest 处理一批 Request; "
                     f"两者复用同一套数据处理、算法逻辑与方向映射。"))
    parser.add_argument("--version", action="version",
                        version=f"{SCHEME_ID} {ALGORITHM_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("predict", help="单点预测: 一条 Request -> 一条结果 JSON")
    one.add_argument("--request", type=Path, required=True)
    one.add_argument("--data-dir", type=Path, required=True)
    one.add_argument("--output", type=Path, required=True)
    many = commands.add_parser("backtest", help="批量回测: 每条 Request 一行结果 CSV")
    many.add_argument("--requests", type=Path, required=True)
    many.add_argument("--data-dir", type=Path, required=True)
    many.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data_dir = Path(args.data_dir)
        output = validate_output_path(Path(args.output), data_dir)
        requests = ([load_request_json(Path(args.request))]
                    if args.command == "predict"
                    else load_requests_csv(Path(args.requests)))
        daily = load_daily(data_dir)
        rows = generate(requests, daily)
        payload = (json.dumps(rows[0], ensure_ascii=False, separators=(",", ":")) + "\n"
                   if args.command == "predict" else serialize_csv(rows))
        atomic_write(output, payload)
        return 0
    except SchemeError as exc:
        _log(f"错误: {exc}")
        return 2
    except Exception as exc:                    # noqa: BLE001
        _log(f"未预期错误: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
