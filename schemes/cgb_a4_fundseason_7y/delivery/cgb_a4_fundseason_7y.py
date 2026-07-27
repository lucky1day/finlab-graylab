#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cgb_a4_fundseason_7y —— 7Y 国债月频方向算法方案 (Blackbox V2 / Contract 1.0)。

单文件交付物。两个命令复用完全相同的数据处理、算法逻辑与方向映射:

    python cgb_a4_fundseason_7y.py predict  --request  request.json  \
        --data-dir <data-dir> --output prediction.json
    python cgb_a4_fundseason_7y.py backtest --requests requests.csv  \
        --data-dir <data-dir> --output backtest.csv

算法概要
--------
1. 15 日业务月桶: 交易日 day>=16 归入下一自然月; 每桶的"观察日"= 桶内目标收益率
   非空的最后一个交易日 (≈每月 15 号)。标签 y=1 当且仅当下一桶观察收益率 >= 本桶。
2. 冻结因子表 (内嵌 391 个指标码 -> 8 个宏观传导通道 + 方向先验) 逐码 as-of 到观察日
   (取 <=观察日 的最后一个值), 做扩展窗口因果 z-score(min_periods=12, shift(1) 排除当月),
   乘方向先验后按通道做 IC 加权平均 -> 8 维通道扩散指数 diff_*, 再取 3 期动量 mom_*。
3. 第 17 维 fund_seas = 季末月(3/6/9/12)哑变量 x causal_z(DR007-OMO 利差)。
4. 5 个期限 (1Y/3Y/5Y/7Y/10Y) 池化为一个样本表 + 期限哑变量; 用趋势状态核
   w=exp(-d^2/2h^2) 对历史月加权, 训练 Logistic (C=0.3, class_weight=balanced,
   liblinear); 只用 month_id < 当前特征月 且有已实现标签的行做训练。
5. 取 7Y 行的概率 p: p>=0.5 -> predicted_direction=+1 (收益率上行), 否则 -1。
   二分类, 不输出 0。

所有环节严格因果: as-of 取值、expanding().shift(1)、walk-forward 训练窗口,
并且每条 Request 都按自己的三个截止键独立截断后从零重建。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import warnings
from datetime import date as _date
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

SCHEME_ID = "cgb_a4_fundseason_7y"
ALGORITHM_VERSION = "1.0.0"

# --------------------------------------------------------------------------- #
# 冻结参数 (模型定义的一部分, 不可运行期修改)
# --------------------------------------------------------------------------- #
TARGET_TENOR = "7Y"

# 算法锚定期限: 趋势状态坐标与资金利差 z 的 as-of 对齐一律使用 10Y 的月中观察日。
# 这是 A4 的算法设计(全曲线共用一个趋势状态), 与被预测的 TARGET_TENOR 无关,
# 五个期限的交付包中该常量恒为 "10Y", 不随 TARGET_TENOR 变化。
ANCHOR_TENOR = "10Y"
TENORS: Tuple[str, ...] = ("1Y", "3Y", "5Y", "7Y", "10Y")
# 日频目标收益率列 (注意 "TB0YWI0C" 是 10Y 的历史命名)
TARGETS: Dict[str, str] = {
    "1Y": "TB1YWI0C",
    "3Y": "TB3YWI0C",
    "5Y": "TB5YWI0C",
    "7Y": "TB7YWI0C",
    "10Y": "TB0YWI0C",
}
DR_OMO_CODE = "90000002"          # DR007 与公开市场操作(OMO)利率之差, 日频
QUARTER_END_MONTHS = (3, 6, 9, 12)

MODEL_OBS_START = "2016-02"       # 最早建模月桶
TRAIN_START = "2016-01-01"        # IC 审核的 train 段 (冻结口径)
TRAIN_END = "2021-12-31"

MIN_PERIODS = 12                  # 扩展窗口 z-score 的最小样本
MOMENTUM_LAG = 3                  # 通道扩散指数动量滞后
REDUNDANCY_CORR = 0.98            # 通道内冗余去重阈值 |corr|
COVERAGE_MIN = 0.50               # 因子最低非空覆盖率
MIN_IC_MONTHS = 20                # 计算 train 段 IC 的最少月数

C_POOL = 0.3                      # Logistic 正则强度
KERNEL_H = 1.0                    # 趋势状态核带宽 (标准差单位)
MIN_HIST_ROWS = 40                # 训练所需最少池化行数
PROB_CLIP = 1e-4
RANDOM_STATE = 42                 # 固定随机状态

CALENDAR_FILENAME = "api_wind_date.csv"   # week_id -> 周末日期 对齐日历

# --------------------------------------------------------------------------- #
# 冻结因子表: channel|sign|freq|code,code,...
# 由研究期的元数据字典 (api_wind_indicators_all.csv, status=1 且 pre_forecast_flag=1)
# 按宏观传导框架关键词规则一次性生成并冻结; 运行期不再读取任何元数据文件。
# 排序口径 = (channel, freq, code), 与研究期一致。
# --------------------------------------------------------------------------- #
_FACTOR_MAP_RAW = """\
commodity|+1|daily|CLNYM01C,CLNYM01H,CLNYM01L,CLNYM01O,CLNYM01S,IDCE001C,JDCE001C,M0000005,\
PCUKO01S,RBSHF01C,S0029752,S0029756,S0029760,S0029764,S0029768,S0029772,S0031525,S0031553,\
S0105896,S0105897,S0260036,S1179664,S5111905,S5707798
commodity|+1|monthly|G1060820,S5105035,S5105044
commodity|+1|weekly|HWL00001,HWM00001,HWM00002,HWM00003,HWM00004,HWM00013,HWM00015,HWM00016,\
HWM00017,HWM00018,HWM00019,HWM00020,HWS00001,HWS00002,HWS00003,HWS00004,HWS00005,HWW00006,\
HWW00007,HWW00008,S0110152,S0179664,S0181750,S5133383,S5133384,S5133385,S5133386,S5133387,\
S5133388,S5133392,S5133393,S5133394,S5133852,S5441642,S5441857,S5441858,S5449386,S5705131,\
S5713191,S5716616,S6955340,WC000003
credit_broad|+1|daily|M0101385,M0109973,M1525763,M5216731
credit_broad|+1|monthly|M0001383,M0001385,M0009973,M0061683,M0209973,M0331593,M0409973,M0509973,\
M5206731,M5525763,X5100205
credit_broad|+1|weekly|M0201385,M0309973,M1331593,M2525763,M5226731,X0100205
fx|+1|daily|M0000271,M0067855,M0068008,M0290205,M0331438,M0331439,M0331440,M0331441,M0331442,\
M0331443,M0331444,USDCNH00,USDCNH0C,USDCNH0H,USDCNH0L
fx|+1|monthly|M0010039,M0010040,M0110039,M0110040,M0210039,M0210040
fx|+1|weekly|HWC00003,HWW00004,HWW00011,HWW00012
growth|+1|daily|M0117126,M0417126,M0717126,S5446170,S5448958,S5474437,S5474438,S5474439,S5474440,\
S5474441,S5474442,S5474444,S6404620,SWD00001,SWD00002,SWD00003,SWD00004,SWD00005,SWD00006,\
SWD00007,SWD00008,SWD00009,SWD00010,SWD00011,SWD00012,SWD00013,SWM00001,SWM00002,SWM00003,\
SWM00004,SWM00005,SWM00006,SWM00007,SWM00008,SWM00009,SWM00010,SWM00011,SWM00012,SWM00013,\
SWM00014,SWM00015,SWM00016,SWR00001,SWR00002,SWR00003,SWR00004,SWR00005,SWR00006,SWR00007,\
SWR00008,SWS00001
growth|+1|monthly|M0000545,M0000605,M0001428,M0017126,M0061571,M0061675,M0061678,M0061679,\
M0061680,M0061681,M0317126,M0332318,M0617126,M5440435,S0029657,S0029658,S0073123,S0129658,\
S0173088,S0229658,S0273088,S0329658,T2627623,V6384553
growth|+1|weekly|C1925068,L0139791,M0161675,M0161678,M0161679,M0161680,M0161681,M0217126,\
M0517126,M0817126,M1331594,S0000070,S0000071,S0114089,S0167711,S0167795,S0167796,S0237843,\
S0237844,S0237847,S2726996,S2727027,S2727048,S5440915,S5444097,S5444100,S5444189,S5444190,\
S5444191,S5446148,S5446171,S5469763,S5470009,S5470029,S5470116,S5470117,S5470118,S5470119,\
S5470120,S5471262,S5474446,S5475834,S5479502,S5479503,S5479504,S5479505,S5479506,S5479784,\
S5713327,S5715650,S5715670,S5715671,S5912470,S5912471,S5912472,S5912473,S5912474,S5912476,\
T0127623,V0184553
inflation|+1|daily|M0100612,M0101227
inflation|+1|monthly|90000005,90000006,G1254021,M0000612,M0001227,M0061676,M0061677
inflation|+1|weekly|M0161676,M0161677,M0200612,R0119018
liq_rates|+1|daily|90000001,90000002,90000003,90000004,90000014,90000016,90000017,DR007IB0,\
DR007IBC,DR007IBH,DR007IBL,M0017138,M0017139,M0017141,M0017142,M0017145,M0017152,M0017153,\
M0041739,M0048486,M1006336,M1006337,N2905001,N2907001,X2879873
liq_rates|+1|weekly|M0117142,M0141739,M0217142,M0241739,M1001795
policy_rate|+1|daily|M0041371,M0143821,M0161518,M1329545
policy_rate|+1|monthly|M0043821,M0061518,M0096870,M0331299
policy_rate|+1|weekly|M0141371,M0196870,M0241371,M0243821,M0261518,M1543249,W0192843
risk_equity|+1|daily|CSI90401,CSI93201,CSI98501,IFCFE00C,IFCFE00O,M0020188,M0020195,M0020202,\
M0020209,M0020216,M0020223,M0020251,M0062531,M0062541,M0330172,M0331251,M0342074,N2691645,\
SH000300,SH004701,SH013201,SH013301,SH090601,SI801010,SI801030,SI801040,SI801050,SI801080,\
SI801110,SI801120,SI801130,SI801140,SI801150,SI801160,SI801170,SI801180,SI801200,SI801210,\
SI801230,SI801710,SI801720,SI801730,SI801740,SI801750,SI801760,SI801770,SI801780,SI801790,\
SI801880,SI801890,SI801950,SI801960,SI801970,SI801980
risk_equity|+1|weekly|CSI90402,CSI90403,CSI93202,CSI93203,CSI98502,CSI98503,M0120188,M0120195,\
M0120202,M0120216,M0120223,M0162541,M0220188,M0220195,M0220202,M0220216,M0220223,M0262541,\
M1342074,M2342074,N0191645,N0291645,SH004702,SH004703,SH013202,SH013203,SH013302,SH013303,\
WC000004
"""


class SchemeError(Exception):
    """算法方案自身抛出的、需要以非零退出码结束的错误。"""


def _parse_factor_map() -> List[Dict[str, object]]:
    """展开冻结因子表 -> [{code, channel, sign, freq}], 顺序 = (channel, freq, code)。"""
    rows: List[Dict[str, object]] = []
    seen = set()
    for line in _FACTOR_MAP_RAW.strip().split("\n"):
        channel, sign, freq, codes = line.split("|")
        for code in codes.split(","):
            if code in seen:
                raise SchemeError(f"冻结因子表存在重复指标码: {code}")
            seen.add(code)
            rows.append({"code": code, "channel": channel,
                         "sign": int(sign), "freq": freq})
    return rows


FACTOR_MAP = _parse_factor_map()
FACTOR_CODES_BY_FREQ: Dict[str, List[str]] = {
    freq: [r["code"] for r in FACTOR_MAP if r["freq"] == freq]
    for freq in ("daily", "weekly", "monthly")
}
FACTOR_CHANNEL: Dict[str, str] = {r["code"]: r["channel"] for r in FACTOR_MAP}
FACTOR_SIGN: Dict[str, int] = {r["code"]: r["sign"] for r in FACTOR_MAP}
CHANNELS: List[str] = sorted({r["channel"] for r in FACTOR_MAP})

# 本方案实际消费的字段清单 (启动时逐项检查是否存在)
CONSUMED_DAILY = ([TARGETS[t] for t in TENORS] + [DR_OMO_CODE]
                  + [c for c in FACTOR_CODES_BY_FREQ["daily"] if c != DR_OMO_CODE])
CONSUMED_WEEKLY = list(FACTOR_CODES_BY_FREQ["weekly"])
CONSUMED_MONTHLY = list(FACTOR_CODES_BY_FREQ["monthly"])

TIME_KEYS = {"daily_output.csv": "date",
             "weekly_output.csv": "week_id",
             "monthly_output.csv": "month_id"}


def _log(msg: str) -> None:
    """日志只写 stderr; stdout 在业务运行期间必须为空。"""
    sys.stderr.write(f"[{SCHEME_ID}] {msg}\n")


# --------------------------------------------------------------------------- #
# 1. Request 校验
# --------------------------------------------------------------------------- #
REQUEST_FIELDS: Tuple[str, ...] = (
    "request_id", "predict_date", "feature_date", "target_date",
    "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key",
)
RESULT_FIELDS: Tuple[str, ...] = (
    "request_id", "predict_date", "feature_date", "target_date",
    "predicted_direction",
)
MAX_BATCH = 100
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY6_RE = re.compile(r"^\d{6}$")


def _parse_iso_date(value: str, field: str, where: str) -> _date:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise SchemeError(f"{where}: {field}='{value}' 不是规范 YYYY-MM-DD")
    try:
        parsed = _date.fromisoformat(value)
    except ValueError as exc:
        raise SchemeError(f"{where}: {field}='{value}' 不是合法日期 ({exc})") from exc
    if parsed.isoformat() != value:
        raise SchemeError(f"{where}: {field}='{value}' 不是规范 YYYY-MM-DD")
    return parsed


def validate_request(raw: Dict[str, object], where: str) -> Dict[str, str]:
    keys = set(raw.keys())
    missing = [f for f in REQUEST_FIELDS if f not in keys]
    extra = sorted(keys - set(REQUEST_FIELDS))
    if missing:
        raise SchemeError(f"{where}: 缺少字段 {missing}")
    if extra:
        raise SchemeError(f"{where}: 出现额外字段 {extra}")

    req: Dict[str, str] = {}
    for field in REQUEST_FIELDS:
        value = raw[field]
        if value is None:
            raise SchemeError(f"{where}: 字段 {field} 为空")
        if not isinstance(value, str):
            raise SchemeError(f"{where}: 字段 {field} 必须是字符串, 实际 {type(value).__name__}")
        req[field] = value

    if not req["request_id"].strip():
        raise SchemeError(f"{where}: request_id 不能为空")

    predict = _parse_iso_date(req["predict_date"], "predict_date", where)
    feature = _parse_iso_date(req["feature_date"], "feature_date", where)
    target = _parse_iso_date(req["target_date"], "target_date", where)
    _parse_iso_date(req["daily_cutoff_key"], "daily_cutoff_key", where)
    if not (feature <= predict <= target):
        raise SchemeError(
            f"{where}: 必须满足 feature_date <= predict_date <= target_date "
            f"({req['feature_date']}, {req['predict_date']}, {req['target_date']})")
    if not feature < target:
        raise SchemeError(f"{where}: 必须满足 feature_date < target_date")
    for field in ("weekly_cutoff_key", "monthly_cutoff_key"):
        if not _KEY6_RE.match(req[field]):
            raise SchemeError(f"{where}: {field}='{req[field]}' 必须是六位数字字符串")
    return req


def load_request_json(path: Path) -> Dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise SchemeError(f"无法读取 Request 文件 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SchemeError(f"Request 文件不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemeError("Request 文件必须是一个 JSON 对象")
    return validate_request(raw, f"request {path.name}")


def load_requests_csv(path: Path) -> List[Dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SchemeError(f"无法读取 Requests 文件 {path}: {exc}") from exc
    reader = csv.reader(text.splitlines())
    try:
        header = next(reader)
    except StopIteration:
        raise SchemeError("Requests 文件为空 (没有表头)") from None
    header = [h.strip() for h in header]
    if len(header) != len(set(header)):
        raise SchemeError("Requests 表头存在重复列")
    if set(header) != set(REQUEST_FIELDS):
        missing = sorted(set(REQUEST_FIELDS) - set(header))
        extra = sorted(set(header) - set(REQUEST_FIELDS))
        raise SchemeError(f"Requests 表头必须恰好是 7 个字段; 缺少={missing} 多余={extra}")

    requests: List[Dict[str, str]] = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(cell.strip() == "" for cell in row):
            raise SchemeError(f"Requests 第 {line_no} 行为空行")
        if len(row) != len(header):
            raise SchemeError(
                f"Requests 第 {line_no} 行有 {len(row)} 个字段, 期望 {len(header)} 个")
        raw = {key: value for key, value in zip(header, row)}
        requests.append(validate_request(raw, f"requests 第 {line_no} 行"))

    if not requests:
        raise SchemeError("Requests 批次为空 (至少需要 1 条)")
    if len(requests) > MAX_BATCH:
        raise SchemeError(f"Requests 批次为 {len(requests)} 条, 超过每批上限 {MAX_BATCH}")
    ids = [r["request_id"] for r in requests]
    if len(ids) != len(set(ids)):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise SchemeError(f"Requests 批内 request_id 重复: {dup}")
    return requests


# --------------------------------------------------------------------------- #
# 2. 读取三频数据 + 逐 Request 独立截断
# --------------------------------------------------------------------------- #
def _read_header(path: Path, filename: str) -> List[str]:
    if not path.is_file():
        raise SchemeError(f"缺少本方案实际消费的数据文件: {filename}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), [])
    except OSError as exc:
        raise SchemeError(f"无法读取 {filename}: {exc}") from exc
    if not header:
        raise SchemeError(f"{filename} 表头为空")
    if len(header) != len(set(header)):
        raise SchemeError(f"{filename} 存在重复列名")
    key = TIME_KEYS[filename]
    if header[0] != key:
        raise SchemeError(f"{filename} 第一列必须是时间键 {key}, 实际是 {header[0]}")
    return header


def _check_consumed(header: Sequence[str], consumed: Sequence[str], filename: str) -> None:
    """按字段名显式检查本方案实际消费的列; 未使用的新增业务列一律忽略。"""
    have = set(header)
    missing = [c for c in consumed if c not in have]
    if missing:
        raise SchemeError(
            f"{filename} 缺少本方案实际消费的字段 {len(missing)} 个 (前 10 个: "
            f"{missing[:10]})")


def _truncate_at_key(frame: pd.DataFrame, key_col: str, cutoff: str,
                     filename: str) -> pd.DataFrame:
    """保留首行至截止键所在行 (含)。时间键必须非空、唯一、升序。"""
    keys = frame[key_col]
    if keys.isna().any() or (keys.astype(str).str.strip() == "").any():
        raise SchemeError(f"{filename} 的 {key_col} 存在空值")
    if keys.duplicated().any():
        raise SchemeError(f"{filename} 的 {key_col} 不唯一")
    if not keys.is_monotonic_increasing:
        raise SchemeError(f"{filename} 的 {key_col} 不是升序")
    hit = np.flatnonzero(keys.to_numpy() == cutoff)
    if hit.size != 1:
        raise SchemeError(f"{filename} 中不存在截止键 {key_col}={cutoff}")
    return frame.iloc[: int(hit[0]) + 1].reset_index(drop=True)


def load_daily(data_dir: Path, cutoff: str) -> pd.DataFrame:
    filename = "daily_output.csv"
    path = data_dir / filename
    header = _read_header(path, filename)
    _check_consumed(header, ["date"] + CONSUMED_DAILY, filename)
    usecols = ["date"] + [c for c in header if c in set(CONSUMED_DAILY)]
    frame = pd.read_csv(path, usecols=usecols, dtype={"date": "string"},
                        encoding="utf-8-sig", low_memory=False)
    parsed = pd.to_datetime(frame["date"], errors="coerce")
    if parsed.isna().any():
        raise SchemeError(f"{filename} 的 date 存在无法解析为日期的值")
    frame["date"] = parsed.dt.strftime("%Y-%m-%d")
    frame = _truncate_at_key(frame, "date", cutoff, filename)
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").reset_index(drop=True)
    for col in frame.columns:
        if col != "date":
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if frame.empty:
        raise SchemeError(f"{filename} 按截止键 {cutoff} 截断后没有数据")
    return frame


def _load_keyed(data_dir: Path, filename: str, consumed: Sequence[str],
                cutoff: str) -> pd.DataFrame:
    key = TIME_KEYS[filename]
    path = data_dir / filename
    header = _read_header(path, filename)
    _check_consumed(header, [key] + list(consumed), filename)
    usecols = [key] + [c for c in header if c in set(consumed)]
    frame = pd.read_csv(path, usecols=usecols, dtype={key: "string"},
                        encoding="utf-8-sig", low_memory=False)
    frame[key] = frame[key].astype("string").str.strip()
    frame = _truncate_at_key(frame, key, cutoff, filename)
    for col in frame.columns:
        if col != key:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if frame.empty:
        raise SchemeError(f"{filename} 按截止键 {cutoff} 截断后没有数据")
    return frame


def load_calendar(data_dir: Path, weekly_cutoff: str) -> pd.Series:
    """week_id -> 周末日期。先找 <data-dir>, 再找脚本同目录; 都没有则失败。

    日历不按截止键截断: 它只提供"某个 week_id 对应的周末自然日", 用完整日历得到
    的是该周真实的周末日; 截断反而会把周末日提前到截止日, 造成周频数据提前可见。
    """
    candidates = [data_dir / CALENDAR_FILENAME,
                  Path(__file__).resolve().parent / CALENDAR_FILENAME]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise SchemeError(
            f"缺少周频对齐日历 {CALENDAR_FILENAME} (需要 week_id -> 周末日期 映射)。"
            f"请把该文件放入 --data-dir 或脚本同目录; 已查找: "
            f"{[str(p) for p in candidates]}")
    try:
        cal = pd.read_csv(path, usecols=["rdate", "week_id"],
                          dtype={"week_id": "string"}, encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        raise SchemeError(f"无法读取 {CALENDAR_FILENAME} ({path}): {exc}") from exc
    cal["rdate"] = pd.to_datetime(cal["rdate"], errors="coerce")
    cal["week_id"] = cal["week_id"].astype("string").str.strip()
    cal = cal.dropna(subset=["rdate", "week_id"])
    if cal.empty:
        raise SchemeError(f"{CALENDAR_FILENAME} 没有可用的 rdate/week_id 记录")
    week_end = cal.groupby("week_id")["rdate"].max()
    if weekly_cutoff not in week_end.index:
        raise SchemeError(
            f"{CALENDAR_FILENAME} 未覆盖 weekly_cutoff_key={weekly_cutoff}, "
            f"无法对齐周频因子 (日历覆盖至 week_id={week_end.index.max()})")
    return week_end


# --------------------------------------------------------------------------- #
# 3. 业务月桶与标签
# --------------------------------------------------------------------------- #
def add_month_bucket(daily: pd.DataFrame) -> pd.DataFrame:
    """15 日业务月桶: day>=16 归下一自然月。"""
    d = daily.copy()
    day = d["date"].dt.day.to_numpy()
    year = d["date"].dt.year.to_numpy()
    month = d["date"].dt.month.to_numpy()
    month2 = month + (day >= 16).astype(int)
    year2 = year + (month2 == 13).astype(int)
    month2 = np.where(month2 == 13, 1, month2)
    bucket_start = pd.to_datetime(
        {"year": year2, "month": month2, "day": np.ones_like(year2)})
    d["bucket"] = bucket_start.dt.to_period("M")
    d["month_id"] = year2 * 100 + month2
    return d


def _assign_split(ts) -> str | None:
    if pd.isna(ts):
        return None
    if pd.Timestamp(TRAIN_START) <= ts <= pd.Timestamp(TRAIN_END):
        return "train"
    return None


def build_target_frame(daily_bucketed: pd.DataFrame, tenor: str) -> pd.DataFrame:
    """逐期限的月级标签表: 观察日 = 桶内目标非空的最后一个交易日。"""
    target = TARGETS[tenor]
    sub = daily_bucketed[["date", "bucket", "month_id", target]].copy()
    first_date = sub.groupby("bucket", sort=True)["date"].min().rename("first_date")
    valid = sub.dropna(subset=[target])
    if valid.empty:
        raise SchemeError(f"截断后的日频数据里 {tenor} 目标列 {target} 全为空")
    obs = (valid.groupby("bucket", sort=True).tail(1)
           .set_index("bucket")[["date", "month_id", target]]
           .rename(columns={"date": "obs_date", target: "yield_close"}))
    frame = obs.join(first_date, how="left")
    # reindex 到无缺月的连续月度区间, 保证 shift(-1) 是真正的 T+1
    full = pd.period_range(frame.index.min(), frame.index.max(), freq="M")
    frame = frame.reindex(full)
    frame.index.name = "bucket"
    frame["yield_close_next"] = frame["yield_close"].shift(-1)
    frame["obs_date_next"] = frame["obs_date"].shift(-1)
    frame["future_change"] = frame["yield_close_next"] - frame["yield_close"]
    frame["y_label"] = np.where(
        frame["yield_close_next"].notna(),
        (frame["yield_close_next"] >= frame["yield_close"]).astype(float),
        np.nan)
    frame = frame.reset_index()
    frame["month_id"] = (frame["bucket"].dt.year * 100
                         + frame["bucket"].dt.month).astype(int)
    frame["split"] = frame["obs_date"].map(_assign_split)
    frame["tenor"] = tenor
    frame = frame[frame["bucket"] >= pd.Period(MODEL_OBS_START, freq="M")]
    frame = frame.dropna(subset=["obs_date"]).reset_index(drop=True)
    if frame.empty:
        raise SchemeError(f"截断后的日频数据不足以构造 {tenor} 的月度观察 (>= {MODEL_OBS_START})")
    return frame


# --------------------------------------------------------------------------- #
# 4. 因子原始序列 / as-of 取值
# --------------------------------------------------------------------------- #
def _month_end_from_month_id(month_ids: pd.Series) -> pd.Series:
    dt = pd.to_datetime(month_ids.astype(int).astype(str), format="%Y%m")
    return dt.dt.to_period("M").dt.to_timestamp(how="end").dt.normalize()


def build_code_series(daily: pd.DataFrame, weekly: pd.DataFrame,
                      monthly: pd.DataFrame,
                      week_end: pd.Series) -> Dict[str, pd.DataFrame]:
    """每个冻结指标码的 (date, value) 序列; 周频对齐到周末日, 月频对齐到月末日。"""
    weekly = weekly.copy()
    weekly["date"] = weekly["week_id"].map(week_end)
    unmapped = int(weekly["date"].isna().sum())
    if unmapped:
        _log(f"警告: 截断后的周频有 {unmapped} 个 week_id 在日历中没有对应周末日, 已忽略")
    monthly = monthly.copy()
    monthly["date"] = _month_end_from_month_id(monthly["month_id"])

    tables = {"daily": daily, "weekly": weekly, "monthly": monthly}
    series: Dict[str, pd.DataFrame] = {}
    for row in FACTOR_MAP:                       # 顺序 = 冻结因子表顺序
        code, freq = str(row["code"]), str(row["freq"])
        table = tables[freq]
        if code not in table.columns:
            continue
        s = table[["date", code]].copy()
        s[code] = pd.to_numeric(s[code], errors="coerce")
        s = s.dropna(subset=["date", code]).sort_values("date")
        if not s.empty:
            series[code] = s.rename(columns={code: "value"}).reset_index(drop=True)
    if not series:
        raise SchemeError("截断后没有任何可用的冻结因子序列")
    return series


def levels_as_of(series: Dict[str, pd.DataFrame], obs_dates: np.ndarray) -> pd.DataFrame:
    """每个码在每个观察日的水平值 (取 <= 观察日 的最后一个值, 严格因果)。"""
    obs = np.asarray(obs_dates, dtype="datetime64[ns]")
    data = {}
    for code, s in series.items():
        d = s["date"].to_numpy(dtype="datetime64[ns]")
        v = s["value"].to_numpy()
        idx = np.searchsorted(d, obs, side="right") - 1
        data[code] = np.where(idx >= 0, v[np.clip(idx, 0, len(v) - 1)], np.nan)
    return pd.DataFrame(data)


# --------------------------------------------------------------------------- #
# 5. 因子审核 (train 段 IC / 覆盖率 / 通道内冗余去重) —— 用截断后的数据运行期计算
# --------------------------------------------------------------------------- #
# ---------------------------------------------------------------------------
# 冻结因子审核结果 (固定参数, SOP §1.3 "训练逻辑和固定参数必须包含在 .py 中")
# ---------------------------------------------------------------------------
# 这两个常量是 A4 在研究期一次性完成的因子审核产物, 与研究基准逐位一致:
#   FROZEN_KEEP_CODES : 通过 coverage>=0.50 与通道内 |corr|>0.98 去重后入选的 270 个因子
#   FROZEN_WEIGHTS    : 各因子的软 IC 权重 w = max(方向先验 x 训练段(2016-02..2021-12)IC, 0)
# 冻结口径与维护见 交接文档.md §12。审核统计量不在运行期重算, 因此同一 Request
# 的结果与数据长度无关, 完全确定。
FROZEN_KEEP_CODES = frozenset((
    '90000001', '90000002', '90000003', '90000004', '90000005', '90000014',
    '90000017', 'C1925068', 'CLNYM01C', 'CSI90401', 'CSI90403', 'CSI93201',
    'CSI93203', 'CSI98501', 'CSI98503', 'DR007IB0', 'DR007IBC', 'DR007IBH',
    'DR007IBL', 'G1060820', 'G1254021', 'HWC00003', 'HWL00001', 'HWS00001',
    'HWS00002', 'HWS00003', 'HWS00004', 'HWS00005', 'HWW00004', 'HWW00006',
    'HWW00007', 'HWW00008', 'HWW00011', 'HWW00012', 'IDCE001C', 'IFCFE00C',
    'JDCE001C', 'L0139791', 'M0000271', 'M0000545', 'M0000605', 'M0000612',
    'M0001227', 'M0001383', 'M0001385', 'M0001428', 'M0009973', 'M0010039',
    'M0017126', 'M0017138', 'M0017139', 'M0017141', 'M0017145', 'M0017153',
    'M0020188', 'M0020202', 'M0020223', 'M0020251', 'M0041371', 'M0041739',
    'M0048486', 'M0061571', 'M0061675', 'M0061676', 'M0061678', 'M0061679',
    'M0061680', 'M0061681', 'M0062531', 'M0062541', 'M0067855', 'M0096870',
    'M0100612', 'M0101227', 'M0101385', 'M0109973', 'M0110039', 'M0110040',
    'M0117126', 'M0120216', 'M0141739', 'M0143821', 'M0161675', 'M0161678',
    'M0161679', 'M0161680', 'M0161681', 'M0209973', 'M0210039', 'M0217142',
    'M0220188', 'M0220202', 'M0220216', 'M0220223', 'M0241371', 'M0241739',
    'M0262541', 'M0309973', 'M0317126', 'M0330172', 'M0331251', 'M0331299',
    'M0331593', 'M0332318', 'M0342074', 'M0409973', 'M0417126', 'M0509973',
    'M0617126', 'M0717126', 'M1001795', 'M1331593', 'M1331594', 'M1525763',
    'M2342074', 'M5206731', 'M5216731', 'M5440435', 'M5525763', 'N0291645',
    'N2691645', 'N2905001', 'N2907001', 'R0119018', 'RBSHF01C', 'S0029657',
    'S0029658', 'S0029752', 'S0029756', 'S0029760', 'S0029764', 'S0029768',
    'S0029772', 'S0031553', 'S0073123', 'S0105896', 'S0105897', 'S0110152',
    'S0114089', 'S0129658', 'S0173088', 'S0181750', 'S0229658', 'S0237847',
    'S0273088', 'S0329658', 'S1179664', 'S2726996', 'S2727048', 'S5105035',
    'S5105044', 'S5133383', 'S5133384', 'S5133385', 'S5133386', 'S5133387',
    'S5133388', 'S5133392', 'S5133393', 'S5133394', 'S5133852', 'S5440915',
    'S5441642', 'S5441857', 'S5444097', 'S5444100', 'S5444189', 'S5444190',
    'S5444191', 'S5446148', 'S5446170', 'S5446171', 'S5448958', 'S5449386',
    'S5469763', 'S5470009', 'S5470029', 'S5470116', 'S5470117', 'S5470118',
    'S5470119', 'S5470120', 'S5471262', 'S5474437', 'S5474438', 'S5474439',
    'S5474440', 'S5474441', 'S5474442', 'S5474444', 'S5474446', 'S5475834',
    'S5479502', 'S5479503', 'S5479504', 'S5479505', 'S5479506', 'S5479784',
    'S5707798', 'S5713327', 'S5715650', 'S5715670', 'S5715671', 'S5716616',
    'S5912470', 'S5912471', 'S5912472', 'S5912473', 'S5912474', 'S5912476',
    'S6404620', 'SH004701', 'SH013201', 'SH013203', 'SH013301', 'SH013303',
    'SI801010', 'SI801030', 'SI801040', 'SI801050', 'SI801080', 'SI801110',
    'SI801120', 'SI801130', 'SI801140', 'SI801150', 'SI801160', 'SI801170',
    'SI801180', 'SI801200', 'SI801210', 'SI801230', 'SI801710', 'SI801720',
    'SI801730', 'SI801740', 'SI801750', 'SI801760', 'SI801770', 'SI801780',
    'SI801790', 'SI801880', 'SI801890', 'SI801950', 'SI801960', 'SI801970',
    'SI801980', 'SWD00001', 'SWD00002', 'SWD00003', 'SWD00005', 'SWD00006',
    'SWD00009', 'SWD00013', 'SWM00002', 'SWM00004', 'SWR00004', 'SWR00005',
    'SWR00006', 'SWR00007', 'SWR00008', 'SWS00001', 'T0127623', 'T2627623',
    'V0184553', 'V6384553', 'W0192843', 'X0100205', 'X2879873', 'X5100205',
))

FROZEN_WEIGHTS = {
    '90000001': 0.0896, '90000002': 0.0279, '90000003': 0.0387, '90000004': 0.0053,
    '90000005': 0.0477, '90000006': 0.0, '90000014': 0.0164, '90000016': 0.0433,
    '90000017': 0.0432, 'C1925068': 0.3306, 'CLNYM01C': 0.0, 'CLNYM01H': 0.0,
    'CLNYM01L': 0.0, 'CLNYM01O': 0.0, 'CLNYM01S': 0.0, 'CSI90401': 0.1459,
    'CSI90402': 0.1017, 'CSI90403': 0.0, 'CSI93201': 0.2384, 'CSI93202': 0.2365,
    'CSI93203': 0.0152, 'CSI98501': 0.1582, 'CSI98502': 0.1147, 'CSI98503': 0.0,
    'DR007IB0': 0.0, 'DR007IBC': 0.0, 'DR007IBH': 0.0, 'DR007IBL': 0.0,
    'G1060820': 0.0, 'G1254021': 0.0, 'HWC00003': 0.0, 'HWL00001': 0.0,
    'HWM00001': 0.0, 'HWM00002': 0.0, 'HWM00003': 0.0, 'HWM00004': 0.0,
    'HWM00013': 0.0, 'HWM00015': 0.0, 'HWM00016': 0.0, 'HWM00017': 0.0,
    'HWM00018': 0.0, 'HWM00019': 0.0, 'HWM00020': 0.0, 'HWS00001': 0.0,
    'HWS00002': 0.0, 'HWS00003': 0.0, 'HWS00004': 0.0, 'HWS00005': 0.0,
    'HWW00004': 0.0051, 'HWW00006': 0.0, 'HWW00007': 0.0, 'HWW00008': 0.0384,
    'HWW00011': 0.0, 'HWW00012': 0.0, 'IDCE001C': 0.0138, 'IFCFE00C': 0.0,
    'IFCFE00O': 0.0, 'JDCE001C': 0.0, 'L0139791': 0.0, 'M0000005': 0.0,
    'M0000271': 0.1238, 'M0000545': 0.0, 'M0000605': 0.0, 'M0000612': 0.0011,
    'M0001227': 0.0, 'M0001383': 0.1367, 'M0001385': 0.2098, 'M0001428': 0.0562,
    'M0009973': 0.0, 'M0010039': 0.1249, 'M0010040': 0.1282, 'M0017126': 0.155,
    'M0017138': 0.0, 'M0017139': 0.0, 'M0017141': 0.0, 'M0017142': 0.0,
    'M0017145': 0.0, 'M0017152': 0.0, 'M0017153': 0.0, 'M0020188': 0.1047,
    'M0020195': 0.1036, 'M0020202': 0.1138, 'M0020209': 0.0, 'M0020216': 0.0,
    'M0020223': 0.0, 'M0020251': 0.0866, 'M0041371': 0.0, 'M0041739': 0.0,
    'M0043821': 0.0681, 'M0048486': 0.0, 'M0061518': 0.0503, 'M0061571': 0.0666,
    'M0061675': 0.0, 'M0061676': 0.0011, 'M0061677': 0.0, 'M0061678': 0.0082,
    'M0061679': 0.0, 'M0061680': 0.0, 'M0061681': 0.0, 'M0061683': 0.2173,
    'M0062531': 0.1256, 'M0062541': 0.2428, 'M0067855': 0.1313, 'M0068008': 0.1412,
    'M0096870': 0.0, 'M0100612': 0.0, 'M0101227': 0.0, 'M0101385': 0.2291,
    'M0109973': 0.0, 'M0110039': 0.0, 'M0110040': 0.0, 'M0117126': 0.1513,
    'M0117142': 0.0, 'M0120188': 0.0631, 'M0120195': 0.0639, 'M0120202': 0.0705,
    'M0120216': 0.0, 'M0120223': 0.0, 'M0141371': 0.0, 'M0141739': 0.0,
    'M0143821': 0.0407, 'M0161518': 0.0504, 'M0161675': 0.0, 'M0161676': 0.0,
    'M0161677': 0.0, 'M0161678': 0.0004, 'M0161679': 0.0, 'M0161680': 0.0,
    'M0161681': 0.0, 'M0162541': 0.2085, 'M0196870': 0.0, 'M0200612': 0.0,
    'M0201385': 0.272, 'M0209973': 0.1863, 'M0210039': 0.1457, 'M0210040': 0.1334,
    'M0217126': 0.1949, 'M0217142': 0.0, 'M0220188': 0.0, 'M0220195': 0.0,
    'M0220202': 0.0, 'M0220216': 0.0, 'M0220223': 0.0, 'M0241371': 0.0,
    'M0241739': 0.0973, 'M0243821': 0.0265, 'M0261518': 0.04, 'M0262541': 0.0,
    'M0290205': 0.1403, 'M0309973': 0.0, 'M0317126': 0.2498, 'M0330172': 0.0,
    'M0331251': 0.0534, 'M0331299': 0.0, 'M0331438': 0.1373, 'M0331439': 0.1372,
    'M0331440': 0.1377, 'M0331441': 0.1354, 'M0331442': 0.1328, 'M0331443': 0.1333,
    'M0331444': 0.1341, 'M0331593': 0.2006, 'M0332318': 0.0589, 'M0342074': 0.1105,
    'M0409973': 0.1845, 'M0417126': 0.0756, 'M0509973': 0.1726, 'M0517126': 0.0474,
    'M0617126': 0.1951, 'M0717126': 0.3252, 'M0817126': 0.3105, 'M1001795': 0.0,
    'M1006336': 0.0, 'M1006337': 0.0, 'M1329545': 0.0, 'M1331593': 0.0796,
    'M1331594': 0.1569, 'M1342074': 0.0939, 'M1525763': 0.2249, 'M1543249': 0.0,
    'M2342074': 0.0, 'M2525763': 0.2439, 'M5206731': 0.0, 'M5216731': 0.2027,
    'M5226731': 0.1204, 'M5440435': 0.0, 'M5525763': 0.1176, 'N0191645': 0.2194,
    'N0291645': 0.0124, 'N2691645': 0.2357, 'N2905001': 0.0, 'N2907001': 0.0,
    'R0119018': 0.0, 'RBSHF01C': 0.0, 'S0000070': 0.0, 'S0000071': 0.0,
    'S0029657': 0.0, 'S0029658': 0.0058, 'S0029752': 0.151, 'S0029756': 0.2093,
    'S0029760': 0.1741, 'S0029764': 0.1781, 'S0029768': 0.0, 'S0029772': 0.1547,
    'S0031525': 0.0, 'S0031553': 0.0, 'S0073123': 0.0167, 'S0105896': 0.0,
    'S0105897': 0.0, 'S0110152': 0.0, 'S0114089': 0.0, 'S0129658': 0.0,
    'S0167711': 0.0, 'S0167795': 0.0, 'S0167796': 0.0, 'S0173088': 0.0,
    'S0179664': 0.0, 'S0181750': 0.1878, 'S0229658': 0.0, 'S0237843': 0.1165,
    'S0237844': 0.0708, 'S0237847': 0.0, 'S0260036': 0.0, 'S0273088': 0.0575,
    'S0329658': 0.0, 'S1179664': 0.0, 'S2726996': 0.1891, 'S2727027': 0.2002,
    'S2727048': 0.0901, 'S5105035': 0.0, 'S5105044': 0.1009, 'S5111905': 0.0,
    'S5133383': 0.1182, 'S5133384': 0.0, 'S5133385': 0.0, 'S5133386': 0.0,
    'S5133387': 0.0569, 'S5133388': 0.0027, 'S5133392': 0.1296, 'S5133393': 0.0,
    'S5133394': 0.0, 'S5133852': 0.0, 'S5440915': 0.0, 'S5441642': 0.0,
    'S5441857': 0.0, 'S5441858': 0.0, 'S5444097': 0.0286, 'S5444100': 0.0,
    'S5444189': 0.0, 'S5444190': 0.0, 'S5444191': 0.0, 'S5446148': 0.0053,
    'S5446170': 0.0, 'S5446171': 0.064, 'S5448958': 0.0, 'S5449386': 0.3227,
    'S5469763': 0.0449, 'S5470009': 0.0, 'S5470029': 0.0, 'S5470116': 0.0388,
    'S5470117': 0.0, 'S5470118': 0.0, 'S5470119': 0.0, 'S5470120': 0.0,
    'S5471262': 0.0479, 'S5474437': 0.2824, 'S5474438': 0.3346, 'S5474439': 0.0,
    'S5474440': 0.0, 'S5474441': 0.0, 'S5474442': 0.0, 'S5474444': 0.0716,
    'S5474446': 0.0688, 'S5475834': 0.0, 'S5479502': 0.3343, 'S5479503': 0.2859,
    'S5479504': 0.2208, 'S5479505': 0.0, 'S5479506': 0.0669, 'S5479784': 0.0,
    'S5705131': 0.0, 'S5707798': 0.0, 'S5713191': 0.0, 'S5713327': 0.0784,
    'S5715650': 0.0, 'S5715670': 0.0, 'S5715671': 0.045, 'S5716616': 0.0719,
    'S5912470': 0.0, 'S5912471': 0.0, 'S5912472': 0.0, 'S5912473': 0.0,
    'S5912474': 0.0, 'S5912476': 0.0, 'S6404620': 0.0, 'S6955340': 0.0988,
    'SH000300': 0.1179, 'SH004701': 0.0212, 'SH004702': 0.0, 'SH004703': 0.0,
    'SH013201': 0.176, 'SH013202': 0.141, 'SH013203': 0.0, 'SH013301': 0.2317,
    'SH013302': 0.2224, 'SH013303': 0.0, 'SH090601': 0.0354, 'SI801010': 0.1894,
    'SI801030': 0.1881, 'SI801040': 0.0, 'SI801050': 0.0926, 'SI801080': 0.0105,
    'SI801110': 0.0667, 'SI801120': 0.0962, 'SI801130': 0.205, 'SI801140': 0.1085,
    'SI801150': 0.162, 'SI801160': 0.1916, 'SI801170': 0.1172, 'SI801180': 0.1211,
    'SI801200': 0.1529, 'SI801210': 0.0, 'SI801230': 0.1191, 'SI801710': 0.0363,
    'SI801720': 0.1021, 'SI801730': 0.0874, 'SI801740': 0.0, 'SI801750': 0.0,
    'SI801760': 0.113, 'SI801770': 0.0052, 'SI801780': 0.151, 'SI801790': 0.1556,
    'SI801880': 0.054, 'SI801890': 0.1365, 'SI801950': 0.0, 'SI801960': 0.1917,
    'SI801970': 0.1975, 'SI801980': 0.1473, 'SWD00001': 0.0, 'SWD00002': 0.0,
    'SWD00003': 0.0, 'SWD00004': 0.0, 'SWD00005': 0.0, 'SWD00006': 0.0,
    'SWD00007': 0.0, 'SWD00008': 0.0, 'SWD00009': 0.1144, 'SWD00010': 0.0,
    'SWD00011': 0.0, 'SWD00012': 0.0, 'SWD00013': 0.0405, 'SWM00001': 0.0,
    'SWM00002': 0.0, 'SWM00003': 0.0, 'SWM00004': 0.0, 'SWM00005': 0.0,
    'SWM00006': 0.0, 'SWM00007': 0.0, 'SWM00008': 0.0, 'SWM00009': 0.0,
    'SWM00010': 0.0, 'SWM00011': 0.0, 'SWM00012': 0.0, 'SWM00013': 0.0,
    'SWM00014': 0.0, 'SWM00015': 0.0, 'SWM00016': 0.0, 'SWR00004': 0.0,
    'SWR00005': 0.0, 'SWR00006': 0.0223, 'SWR00007': 0.0, 'SWR00008': 0.2067,
    'SWS00001': 0.0284, 'T0127623': 0.0, 'T2627623': 0.0, 'USDCNH00': 0.1438,
    'USDCNH0C': 0.1401, 'USDCNH0H': 0.1352, 'USDCNH0L': 0.1452, 'V0184553': 0.0,
    'V6384553': 0.0, 'WC000003': 0.0, 'WC000004': 0.0, 'X0100205': 0.1775,
    'X2879873': 0.0, 'X5100205': 0.0866,
}


def signed_z(panel: pd.DataFrame) -> pd.DataFrame:
    """方向先验 x 因果扩展 z-score (min_periods=12, shift(1) 排除当月)。"""
    contrib = {}
    for code in panel.columns:
        s = panel[code].astype(float).reset_index(drop=True)
        mean = s.expanding(min_periods=MIN_PERIODS).mean().shift(1)
        std = s.expanding(min_periods=MIN_PERIODS).std(ddof=0).shift(1)
        z = (s - mean) / std.replace(0.0, np.nan)
        contrib[code] = FACTOR_SIGN.get(code, 1) * z
    return pd.DataFrame(contrib)


def _weighted_channel_mean(C: pd.DataFrame, codes: List[str],
                           weights: Dict[str, float]) -> pd.Series:
    if not codes:
        return pd.Series(np.nan, index=C.index)
    w = np.array([max(weights.get(c, 0.0), 0.0) for c in codes], dtype=float)
    if w.sum() <= 0:
        return C[codes].mean(axis=1, skipna=True)
    sub = C[codes]
    mask = sub.notna().to_numpy()
    vals = np.nan_to_num(sub.to_numpy(), nan=0.0)
    eff = mask * w
    num = (vals * eff).sum(axis=1)
    den = eff.sum(axis=1)
    return pd.Series(np.where(den > 0, num / den, np.nan), index=C.index)


def diffusion_from_panel(panel: pd.DataFrame, keep_codes: set,
                         weights: Dict[str, float]) -> pd.DataFrame:
    C = signed_z(panel)
    out = pd.DataFrame(index=range(len(panel)))
    for channel in CHANNELS:
        codes = [c for c in panel.columns
                 if FACTOR_CHANNEL.get(c) == channel and c in keep_codes]
        if codes:
            out[f"diff_{channel}"] = _weighted_channel_mean(C, codes, weights)
    for col in list(out.columns):
        out[f"mom_{col[len('diff_'):]}"] = out[col] - out[col].shift(MOMENTUM_LAG)
    return out


def causal_z(s: pd.Series, min_periods: int = MIN_PERIODS) -> pd.Series:
    mean = s.expanding(min_periods).mean().shift(1)
    std = s.expanding(min_periods).std().shift(1).replace(0, np.nan)
    return (s - mean) / std


def efficiency_ratio(y: pd.Series, k: int) -> pd.Series:
    net = (y - y.shift(k)).abs()
    gross = y.diff().abs().rolling(k).sum()
    return net / gross.replace(0, np.nan)


def regime_trend(base10: pd.DataFrame) -> pd.Series:
    """趋势状态坐标: 复合效率比 ER(3,6,12) 的因果标准化值, 以 month_id 为索引。"""
    d = base10.sort_values("month_id")[["month_id", "yield_close"]].copy()
    y = d["yield_close"]
    raw = pd.concat([efficiency_ratio(y, k) for k in (3, 6, 12)], axis=1).mean(axis=1)
    mu = raw.expanding(12).mean().shift(1)
    sd = raw.expanding(12).std().shift(1)
    trend = (raw - mu) / sd.replace(0, np.nan)
    return pd.Series(trend.to_numpy(), index=d["month_id"].to_numpy())


# --------------------------------------------------------------------------- #
# 7. 池化 Logistic + 趋势状态核加权
# --------------------------------------------------------------------------- #
def fit_predict(hist: pd.DataFrame, cur: pd.DataFrame, feats: List[str],
                dummies: List[str], w: np.ndarray | None = None) -> np.ndarray | None:
    if hist["y_label"].nunique() < 2 or len(hist) < MIN_HIST_ROWS:
        return None
    med = hist[feats].median()
    Xtr = pd.concat([hist[feats].fillna(med).fillna(0.0), hist[dummies]],
                    axis=1).to_numpy()
    scaler = StandardScaler().fit(Xtr)
    est = LogisticRegression(C=C_POOL, class_weight="balanced", solver="liblinear",
                             max_iter=1000, random_state=RANDOM_STATE)
    est.fit(scaler.transform(Xtr), hist["y_label"].astype(int), sample_weight=w)
    Xc = pd.concat([cur[feats].fillna(med).fillna(0.0), cur[dummies]],
                   axis=1).to_numpy()
    return np.clip(est.predict_proba(scaler.transform(Xc))[:, 1],
                   PROB_CLIP, 1 - PROB_CLIP)


# --------------------------------------------------------------------------- #
# 8. 单条 Request 的完整重建 -> 方向
# --------------------------------------------------------------------------- #
def predict_direction(data_dir: Path, req: Dict[str, str]) -> int:
    daily_raw = load_daily(data_dir, req["daily_cutoff_key"])
    weekly = _load_keyed(data_dir, "weekly_output.csv", CONSUMED_WEEKLY,
                         req["weekly_cutoff_key"])
    monthly = _load_keyed(data_dir, "monthly_output.csv", CONSUMED_MONTHLY,
                          req["monthly_cutoff_key"])
    week_end = load_calendar(data_dir, req["weekly_cutoff_key"])

    daily = add_month_bucket(daily_raw)
    label_frames = {t: build_target_frame(daily, t) for t in TENORS}
    series = build_code_series(daily_raw, weekly, monthly, week_end)
    # 使用冻结的因子审核结果(固定参数), 不在运行期重算 —— 见 交接文档.md §12
    keep_codes, weights = FROZEN_KEEP_CODES, FROZEN_WEIGHTS

    # DR007-OMO 的因果 z (以 10Y 的月中观察日 as-of 对齐)
    lf10 = label_frames[ANCHOR_TENOR].sort_values("month_id").reset_index(drop=True)
    if DR_OMO_CODE not in daily_raw.columns:
        raise SchemeError(f"日频缺少资金利差字段 {DR_OMO_CODE}")
    dr = (daily_raw[["date", DR_OMO_CODE]].dropna()
          .rename(columns={DR_OMO_CODE: "value"})
          .sort_values("date").reset_index(drop=True))
    if dr.empty:
        raise SchemeError(f"截断后 {DR_OMO_CODE} 没有任何取值")
    dr_level = levels_as_of({"dr_omo": dr},
                            pd.to_datetime(lf10["obs_date"]).to_numpy())
    dr_z = pd.Series(causal_z(pd.Series(dr_level["dr_omo"].to_numpy())).to_numpy(),
                     index=lf10["month_id"].to_numpy())

    bases: Dict[str, pd.DataFrame] = {}
    for tenor in TENORS:
        lf = label_frames[tenor].sort_values("month_id").reset_index(drop=True)
        panel = levels_as_of(series, pd.to_datetime(lf["obs_date"]).to_numpy())
        diff = diffusion_from_panel(panel, keep_codes, weights)
        diff.insert(0, "obs_date", lf["obs_date"].to_numpy())
        diff.insert(0, "month_id", lf["month_id"].to_numpy())
        base = lf.merge(diff, on=["month_id", "obs_date"])
        base["fund_seas"] = (
            ((base["month_id"] % 100).isin(QUARTER_END_MONTHS)).astype(float)
            * base["month_id"].map(dr_z).astype(float))
        bases[tenor] = base

    feats = ([c for c in bases[TARGET_TENOR].columns if c.startswith("diff_")]
             + [c for c in bases[TARGET_TENOR].columns if c.startswith("mom_")]
             + ["fund_seas"])
    trend = regime_trend(bases[ANCHOR_TENOR])

    parts = []
    for tenor in TENORS:
        b = bases[tenor]
        sub = b[["obs_date", "month_id", "y_label"] + feats].copy()
        sub["tenor"] = tenor
        parts.append(sub)
    A = pd.concat(parts, ignore_index=True).sort_values("obs_date").reset_index(drop=True)
    for tenor in TENORS:
        A[f"ten_{tenor}"] = (A["tenor"] == tenor).astype(float)
    dummies = [f"ten_{t}" for t in TENORS]

    # 特征月 = 截断后目标期限最后一个已完成观察的业务月桶
    month_id = int(bases[TARGET_TENOR]["month_id"].max())
    hist = A[(A["month_id"] < month_id) & (A["y_label"].isin([0.0, 1.0]))]
    cur = A[A["month_id"] == month_id]          # 预测月保留(不论有无标签)
    if cur.empty or TARGET_TENOR not in set(cur["tenor"]):
        raise SchemeError(f"截断后没有 {TARGET_TENOR} 在特征月 {month_id} 的可预测行")
    if len(hist) < MIN_HIST_ROWS:
        raise SchemeError(
            f"截断后可用历史仅 {len(hist)} 行, 少于训练下限 {MIN_HIST_ROWS} 行")

    rt = trend.get(month_id, np.nan)
    if pd.isna(rt):
        p = fit_predict(hist, cur, feats, dummies)
    else:
        rm = trend.reindex(hist["month_id"]).to_numpy(dtype=float)
        dist2 = (rm - float(rt)) ** 2
        w = np.exp(-dist2 / (2 * KERNEL_H * KERNEL_H))
        w = np.where(np.isnan(w), np.exp(-0.5), w)
        p = fit_predict(hist, cur, feats, dummies, w=w)
    if p is None:
        raise SchemeError("训练数据不足 (历史行数或标签类别不够), 无法生成合法方向")

    probs = dict(zip(cur["tenor"].tolist(), np.asarray(p, dtype=float).tolist()))
    prob_up = probs[TARGET_TENOR]
    direction = 1 if prob_up >= 0.5 else -1
    _log(f"request_id={req['request_id']} 特征月={month_id} "
         f"训练行={len(hist)} 因子={len(keep_codes)} prob_up={prob_up:.6f} "
         f"direction={direction:+d}")
    return direction


# --------------------------------------------------------------------------- #
# 9. Output (先算完再原子替换; 失败不留下任何 Output)
# --------------------------------------------------------------------------- #
def _atomic_write(output: Path, payload: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(output.parent),
                                        prefix=f".{output.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, output)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _result_row(req: Dict[str, str], direction: int) -> Dict[str, object]:
    return {"request_id": req["request_id"],
            "predict_date": req["predict_date"],
            "feature_date": req["feature_date"],
            "target_date": req["target_date"],
            "predicted_direction": int(direction)}


def cmd_predict(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    output = Path(args.output)
    req = load_request_json(Path(args.request))
    row = _result_row(req, predict_direction(data_dir, req))
    _atomic_write(output, json.dumps(row, ensure_ascii=False, indent=2) + "\n")
    _log(f"predict 完成 -> {output}")


def cmd_backtest(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir)
    output = Path(args.output)
    requests = load_requests_csv(Path(args.requests))
    _log(f"backtest 批次 {len(requests)} 条")
    rows = [_result_row(req, predict_direction(data_dir, req)) for req in requests]
    lines = [",".join(RESULT_FIELDS)]
    lines += [",".join(str(row[field]) for field in RESULT_FIELDS) for row in rows]
    _atomic_write(output, "\n".join(lines) + "\n")
    _log(f"backtest 完成 {len(rows)} 条 -> {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{SCHEME_ID}.py",
        description=("10Y 国债月频方向算法方案 (Blackbox V2 Contract 1.0)。"
                     "predict 处理单条 Request, backtest 处理一批 (1~100 条) Request; "
                     "两者复用同一套数据处理、算法逻辑与方向映射。"))
    parser.add_argument("--version", action="version",
                        version=f"{SCHEME_ID} {ALGORITHM_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("predict", help="单点预测: 一条 Request -> 一条结果 JSON")
    p1.add_argument("--request", required=True, help="单点 Request JSON 路径")
    p1.add_argument("--data-dir", required=True, help="平台提供的只读三频数据目录")
    p1.add_argument("--output", required=True, help="结果 JSON 输出路径")
    p1.set_defaults(func=cmd_predict)

    p2 = sub.add_parser("backtest", help="批量回测: 每条 Request 一行结果 CSV")
    p2.add_argument("--requests", required=True, help="批量 Requests CSV 路径")
    p2.add_argument("--data-dir", required=True, help="平台提供的只读三频数据目录")
    p2.add_argument("--output", required=True, help="结果 CSV 输出路径")
    p2.set_defaults(func=cmd_backtest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        data_dir = Path(args.data_dir)
        if not data_dir.is_dir():
            raise SchemeError(f"--data-dir 不是一个目录: {data_dir}")
        args.func(args)
    except SchemeError as exc:
        _log(f"错误: {exc}")
        return 2
    except Exception as exc:                    # noqa: BLE001 - 统一转为非零退出
        _log(f"未预期错误: {type(exc).__name__}: {exc}")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
