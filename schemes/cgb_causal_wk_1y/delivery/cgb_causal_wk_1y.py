#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cgb_causal_wk_1y —— 1Y 国债周频方向算法方案 (Blackbox V2 / Contract 1.0)。

单文件交付物。两个命令复用完全相同的数据处理、算法逻辑与方向映射:

    python cgb_causal_wk_1y.py predict  --request  request.json \
        --data-dir <data-dir> --output prediction.json
    python cgb_causal_wk_1y.py backtest --requests requests.csv \
        --data-dir <data-dir> --output backtest.csv

算法概要
--------
1. 周频面板: 以 weekly_output.csv 的 week_id 为主键; 51 个因果指标按"通道(5) x
   子通道"组织, 逐码按冻结的来源频率取值(42 个直接取周频列, 7 个由日频按周聚合,
   聚合口径 mean/last 冻结), 其中 3 个为对基准码的利差。
2. 逐码做 52 周滚动 z-score(min_periods=12), 乘冻结的收益率方向先验 yield_sign,
   clip 到 [-3,3] 得到 51 个 __yield_pressure 因子; 按通道取可用值均值得到 5 个
   通道压力, 再取 5 通道均值得到 yield_pressure_score, 取负得到 bond_price_score。
3. 标签 y=+1 当且仅当下一周 TB1YWI3C 高于本周(阈值 0)。TB1YWI3C 是
   1Y 活跃券周度收盘观测, 单位为到期收益率(%)，因此 y=+1 即"收益率上行"。
4. Walk-forward: 对每个特征周, 只用该周之前、标签已实现且非持平的样本训练
   Logistic(C=0.6, liblinear, class_weight=balanced) 与 RidgeClassifier(alpha=1.0),
   58 维特征(51 因子 + 5 通道 + 2 汇总); 两模型上行概率取均值, p>=0.5 -> +1, 否则 -1。
所有环节严格因果: 滚动窗口只回看、训练窗口只用特征周之前的已实现标签、每条 Request
按自己的截止键独立截断后从零重建, 不保存任何跨 Request / 跨批状态。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import tempfile
import warnings
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, RidgeClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


# --------------------------------------------------------------------------- #
# 方案身份 (与 {scheme_id}.json 必须完全一致)
# --------------------------------------------------------------------------- #
SCHEME_ID = "cgb_causal_wk_1y"
ALGORITHM_VERSION = "1.0.0"
TARGET_TENOR = "1Y"
TASK_TYPE = "weekly_point"
HORIZON = 1

# --------------------------------------------------------------------------- #
# 冻结参数 (模型定义的一部分, 不可在运行期修改)
# --------------------------------------------------------------------------- #
TARGET_COL = "TB1YWI3C"          # 预测标的: 活跃券周度收盘观测(到期收益率 %)
YIELD_COL = "ZY000009"            # 同期限中债到期收益率周处理值 (诊断/极端态判定)
LABEL_THRESHOLD = 0.0                  # 标签阈值: 下一周相对本周的变动率
MIN_TRAIN_WEEKS = 156                  # 训练所需最少的已实现非持平样本数
MODEL_START_WEEK = 201501              # 最早允许出预测的 week_id
ROLL_WINDOW = 52                       # 滚动 z-score 窗口(周)
ROLL_MIN_PERIODS = 12
CLIP_ABS = 3.0
LOGIT_C = 0.6
RIDGE_ALPHA = 1.0

DAILY_FILE = "daily_output.csv"
WEEKLY_FILE = "weekly_output.csv"
MONTHLY_FILE = "monthly_output.csv"
CALENDAR_FILE = "api_wind_date.csv"    # ★ 必需输入: week_id 映射的唯一来源, 见 §5

REQUEST_FIELDS: Tuple[str, ...] = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "daily_cutoff_key",
    "weekly_cutoff_key",
    "monthly_cutoff_key",
)
RESULT_FIELDS: Tuple[str, ...] = (
    "request_id",
    "predict_date",
    "feature_date",
    "target_date",
    "predicted_direction",
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_KEY_RE = re.compile(r"^\d{6}$")

MODULE_ORDER: Tuple[str, ...] = (
    "macro_fundamental",
    "policy_liquidity",
    "supply_institution",
    "market_sentiment",
    "overseas_cross_asset",
)


class SchemeError(RuntimeError):
    """业务失败: 统一由 main 捕获, 写 stderr 并以非 0 退出。"""


def _log(message: str) -> None:
    print(f"[{SCHEME_ID}] {message}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 冻结因子表
#   source : weekly | daily     —— 该码的取数频率, 冻结, 不在运行期重新探测
#   agg    : last | mean        —— source=daily 时的按周聚合口径
#   transform: level_z | change_z | pct_change_z | spread_change_z
#   yield_sign: 该因子对收益率的方向先验; 0 表示只做诊断、不进入通道压力
#   spread_against: 非空时因子值 = code - spread_against(同一聚合口径)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CausalSpec:
    module: str
    submodule: str
    code: str
    transform: str
    yield_sign: float
    source: str
    agg: str = "last"
    spread_against: Optional[str] = None
    spread_source: Optional[str] = None
    note: str = ""

    @property
    def feature_id(self) -> str:
        base = self.code if self.spread_against is None else f"{self.code}_minus_{self.spread_against}"
        return f"{self.module}__{self.submodule}__{base}"


CAUSAL_SPECS: Tuple[CausalSpec, ...] = (
    CausalSpec("macro_fundamental", "growth", "M0217126", "level_z", 1.0, "weekly", note="制造业PMI"),
    CausalSpec("macro_fundamental", "growth", "M0517126", "change_z", 1.0, "weekly", note="PMI环比"),
    CausalSpec("macro_fundamental", "growth", "M2525763", "level_z", 1.0, "weekly", note="社融存量同比"),
    CausalSpec("macro_fundamental", "growth", "M5226731", "level_z", 1.0, "weekly", note="社融新增人民币贷款"),
    CausalSpec("macro_fundamental", "growth", "M0161684", "level_z", 1.0, "weekly", note="人民币贷款同比预测"),
    CausalSpec("macro_fundamental", "growth", "M0161675", "level_z", 1.0, "weekly", note="工业增加值预测"),
    CausalSpec("macro_fundamental", "growth", "V0184553", "level_z", 1.0, "weekly", note="房地产投资预测"),
    CausalSpec("macro_fundamental", "growth", "S0114089", "change_z", 1.0, "weekly", note="上海出口集装箱指数"),
    CausalSpec("macro_fundamental", "inflation", "M0200612", "level_z", 1.0, "weekly", note="CPI同比"),
    CausalSpec("macro_fundamental", "inflation", "M0161676", "level_z", 1.0, "weekly", note="CPI预测"),
    CausalSpec("macro_fundamental", "inflation", "M0161677", "level_z", 1.0, "weekly", note="PPI预测"),
    CausalSpec("macro_fundamental", "inflation", "HWM00017", "pct_change_z", 1.0, "weekly", note="南华综合"),
    CausalSpec("macro_fundamental", "inflation", "HWM00018", "pct_change_z", 1.0, "weekly", note="南华工业品"),
    CausalSpec("macro_fundamental", "inflation", "HWM00015", "pct_change_z", 1.0, "weekly", note="布伦特原油"),
    CausalSpec("macro_fundamental", "inflation", "HWM00013", "pct_change_z", 1.0, "weekly", note="WTI原油"),
    CausalSpec("policy_liquidity", "policy", "M0061614", "change_z", -1.0, "weekly", note="公开市场净投放"),
    CausalSpec("policy_liquidity", "policy", "M1543249", "level_z", 1.0, "weekly", note="MLF1年利率"),
    CausalSpec("policy_liquidity", "policy", "M0196870", "level_z", 1.0, "weekly", note="LPR1年"),
    CausalSpec("policy_liquidity", "policy", "W0192843", "level_z", 1.0, "weekly", note="准备金率代理"),
    CausalSpec("policy_liquidity", "funding", "M1001795", "level_z", 1.0, "weekly", note="R007"),
    CausalSpec("policy_liquidity", "funding", "DR007IBC", "level_z", 1.0, "daily", "mean", note="DR007收盘"),
    CausalSpec("policy_liquidity", "funding", "M1006337", "level_z", 1.0, "daily", "mean", note="DR007"),
    CausalSpec("policy_liquidity", "funding", "M1014902", "level_z", 1.0, "weekly", note="AAA同业存单1年"),
    CausalSpec("policy_liquidity", "funding", "90000002", "level_z", 1.0, "daily", "mean", note="DR007-OMO"),
    CausalSpec("policy_liquidity", "funding", "90000014", "level_z", 1.0, "daily", "mean", note="GC007-DR007"),
    CausalSpec("supply_institution", "supply_proxy", "ZY000126", "spread_change_z", 1.0, "weekly", "last",
               spread_against="M1011654", spread_source="weekly", note="地方债10Y利差"),
    CausalSpec("supply_institution", "institution", "M1148909", "change_z", -1.0, "weekly", note="商业银行托管"),
    CausalSpec("supply_institution", "institution", "M2341115", "change_z", -1.0, "weekly", note="境外机构托管"),
    CausalSpec("supply_institution", "trading", "TB0YWI1V", "change_z", 0.0, "weekly", note="10Y活跃券成交量"),
    CausalSpec("supply_institution", "trading", "TB0YWI2V", "level_z", 0.0, "weekly", note="10Y活跃券成交量方差"),
    CausalSpec("supply_institution", "trading", "TCFE010I", "change_z", 0.0, "weekly", note="T合约持仓"),
    CausalSpec("supply_institution", "trading", "TCFE001V", "change_z", 0.0, "weekly", note="T合约成交"),
    CausalSpec("market_sentiment", "risk_appetite", "WC000004", "pct_change_z", 1.0, "weekly", note="沪深300周收盘"),
    CausalSpec("market_sentiment", "risk_appetite", "M0120188", "pct_change_z", 1.0, "weekly", note="上证综指"),
    CausalSpec("market_sentiment", "risk_appetite", "N0191645", "pct_change_z", 1.0, "weekly", note="中证1000"),
    CausalSpec("market_sentiment", "credit", "N1110001", "spread_change_z", -1.0, "daily", "mean",
               spread_against="M1001654", spread_source="daily", note="AA+城投10Y利差"),
    CausalSpec("market_sentiment", "credit", "N1310001", "spread_change_z", -1.0, "daily", "mean",
               spread_against="M1001654", spread_source="daily", note="AA+企业债10Y利差"),
    CausalSpec("market_sentiment", "bond_market", "TCFE001C", "pct_change_z", -1.0, "weekly", note="T合约收盘"),
    CausalSpec("market_sentiment", "bond_market", "M1011654", "change_z", 1.0, "weekly", note="10Y收益率动量"),
    CausalSpec("market_sentiment", "curve", "ZY000009", "spread_change_z", 1.0, "weekly", "last",
               spread_against="M1011654", spread_source="weekly", note="1Y与10Y曲线代理"),
    CausalSpec("market_sentiment", "technical", "BIAS10YW", "level_z", 1.0, "weekly", note="10Y收益率BIAS"),
    CausalSpec("market_sentiment", "technical", "ATR10Y0W", "level_z", 0.0, "weekly", note="10Y收益率ATR"),
    CausalSpec("overseas_cross_asset", "overseas_rates", "G0100891", "change_z", 1.0, "weekly", note="美债10Y"),
    CausalSpec("overseas_cross_asset", "overseas_rates", "ZO000010", "change_z", 1.0, "weekly", note="美债10Y周处理"),
    CausalSpec("overseas_cross_asset", "overseas_rates", "G0100887", "change_z", 1.0, "weekly", note="美债2Y"),
    CausalSpec("overseas_cross_asset", "fx", "HWC00003", "change_z", 1.0, "weekly", note="USDCNH周累计变动"),
    CausalSpec("overseas_cross_asset", "fx", "HWW00004", "change_z", 1.0, "weekly", note="USDCNY周变动"),
    CausalSpec("overseas_cross_asset", "fx", "M0000271", "pct_change_z", 1.0, "daily", "last", note="美元指数"),
    CausalSpec("overseas_cross_asset", "commodity", "WC000001", "pct_change_z", -1.0, "weekly", note="COMEX黄金"),
    CausalSpec("overseas_cross_asset", "commodity", "S0179664", "change_z", 1.0, "weekly", note="螺纹钢现货"),
    CausalSpec("overseas_cross_asset", "commodity", "HWW00006", "change_z", -1.0, "weekly", note="LME铜库存变动"),
)


def _consumed_columns() -> Tuple[List[str], List[str]]:
    """返回 (weekly 必需列, daily 必需列)。显式清单, 启动时逐项检查。"""
    weekly_cols: List[str] = [TARGET_COL, YIELD_COL]
    daily_cols: List[str] = []
    for spec in CAUSAL_SPECS:
        target = weekly_cols if spec.source == "weekly" else daily_cols
        if spec.code not in target:
            target.append(spec.code)
        if spec.spread_against is not None:
            other = weekly_cols if spec.spread_source == "weekly" else daily_cols
            if spec.spread_against not in other:
                other.append(spec.spread_against)
    return weekly_cols, daily_cols


WEEKLY_CONSUMED, DAILY_CONSUMED = _consumed_columns()


# --------------------------------------------------------------------------- #
# 业务周日历
#   自然日 -> week_id 的对应关系【一律以 <data-dir>/api_wind_date.csv 为唯一来源】。
#   本脚本不做任何推算: 不按 ISO 周换算、不按规则外推、不内嵌任何日历常量。
#   原因: 上游把整周无交易的节假日周并入相邻周(跨度 14 天), 该口径无法由日期算出,
#         且只有上游这张表是权威的。日历缺失、非法或不覆盖当前 Request 时本次运行必须失败。
# --------------------------------------------------------------------------- #
def read_week_calendar(path: Path) -> pd.DataFrame:
    """读取 rdate -> week_id 映射; 只做合法性校验, 不做任何补全、推算或外推。"""
    if not path.exists():
        raise SchemeError(
            f"缺少业务周日历 {path}。本方案的 week_id 映射必须来自该表, 不允许由日期推算。"
            f"请把 {CALENDAR_FILE} 与三份 DataBridge CSV 一起放入 --data-dir(详见交接文档 §5)。"
        )
    frame = pd.read_csv(path, usecols=["rdate", "week_id"], encoding="utf-8-sig")
    frame["rdate"] = pd.to_datetime(frame["rdate"], errors="coerce")
    frame["week_id"] = pd.to_numeric(frame["week_id"], errors="coerce")
    frame = frame.dropna(subset=["rdate", "week_id"])
    if frame.empty:
        raise SchemeError(f"{path} 没有可用的 rdate/week_id 记录")
    frame["week_id"] = frame["week_id"].astype(int)
    if frame["rdate"].duplicated().any():
        dup = (
            frame.loc[frame["rdate"].duplicated(), "rdate"]
            .head(3)
            .dt.strftime("%Y-%m-%d")
            .tolist()
        )
        raise SchemeError(f"{path} 把同一自然日映射到多个 week_id: {dup}")
    return frame.sort_values("rdate").reset_index(drop=True)


def calendar_series(calendar: pd.DataFrame) -> pd.Series:
    """rdate 为索引、week_id 为值的查表 Series。"""
    return pd.Series(
        calendar["week_id"].to_numpy(),
        index=pd.DatetimeIndex(calendar["rdate"]),
    )


# --------------------------------------------------------------------------- #
# 数值工具 (与研究基准逐行一致)
# --------------------------------------------------------------------------- #
def safe_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)


def rolling_z(series: pd.Series) -> pd.Series:
    values = safe_numeric(series)
    mean = values.rolling(ROLL_WINDOW, min_periods=ROLL_MIN_PERIODS).mean()
    std = values.rolling(ROLL_WINDOW, min_periods=ROLL_MIN_PERIODS).std()
    return values.sub(mean).div(std.replace(0, np.nan))


def clipped(series: pd.Series) -> pd.Series:
    return safe_numeric(series).clip(-CLIP_ABS, CLIP_ABS)


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def primary_score(raw: pd.Series, transform: str) -> pd.Series:
    if transform == "level_z":
        return rolling_z(raw)
    if transform == "change_z":
        return rolling_z(raw.diff())
    if transform == "pct_change_z":
        return rolling_z(raw.pct_change())
    if transform == "spread_change_z":
        return rolling_z(raw.diff())
    raise SchemeError(f"未知 transform: {transform}")


# --------------------------------------------------------------------------- #
# Request 读取与校验
# --------------------------------------------------------------------------- #
def _parse_iso_date(value: str, field: str, request_id: str) -> _date:
    text = "" if value is None else str(value).strip()
    if not DATE_RE.match(text):
        raise SchemeError(f"[{request_id}] {field} 必须是 YYYY-MM-DD: {value!r}")
    try:
        return _date.fromisoformat(text)
    except ValueError as exc:
        raise SchemeError(f"[{request_id}] {field} 不是合法日期: {value!r}") from exc


def validate_request(raw: Dict[str, Any]) -> Dict[str, str]:
    if not isinstance(raw, dict):
        raise SchemeError("Request 必须是对象")
    keys = set(raw.keys())
    missing = [field for field in REQUEST_FIELDS if field not in keys]
    extra = sorted(keys.difference(REQUEST_FIELDS))
    if missing:
        raise SchemeError(f"Request 缺少字段: {missing}")
    if extra:
        raise SchemeError(f"Request 存在多余字段: {extra}")

    request_id = "" if raw["request_id"] is None else str(raw["request_id"]).strip()
    if not request_id:
        raise SchemeError("request_id 必须是非空字符串")

    predict_date = _parse_iso_date(raw["predict_date"], "predict_date", request_id)
    feature_date = _parse_iso_date(raw["feature_date"], "feature_date", request_id)
    target_date = _parse_iso_date(raw["target_date"], "target_date", request_id)
    if not (feature_date <= predict_date <= target_date and feature_date < target_date):
        raise SchemeError(
            f"[{request_id}] 日期必须满足 feature_date <= predict_date <= target_date 且 feature_date < target_date"
        )

    daily_cutoff = str(raw["daily_cutoff_key"]).strip()
    if not DATE_RE.match(daily_cutoff):
        raise SchemeError(f"[{request_id}] daily_cutoff_key 必须是 YYYY-MM-DD: {daily_cutoff!r}")
    _parse_iso_date(daily_cutoff, "daily_cutoff_key", request_id)

    weekly_cutoff = str(raw["weekly_cutoff_key"]).strip()
    monthly_cutoff = str(raw["monthly_cutoff_key"]).strip()
    for field, value in (("weekly_cutoff_key", weekly_cutoff), ("monthly_cutoff_key", monthly_cutoff)):
        if not PERIOD_KEY_RE.match(value):
            raise SchemeError(f"[{request_id}] {field} 必须是六位数字字符串: {value!r}")

    return {
        "request_id": request_id,
        "predict_date": predict_date.isoformat(),
        "feature_date": feature_date.isoformat(),
        "target_date": target_date.isoformat(),
        "daily_cutoff_key": daily_cutoff,
        "weekly_cutoff_key": weekly_cutoff,
        "monthly_cutoff_key": monthly_cutoff,
    }


def read_single_request(path: Path) -> Dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise SchemeError(f"找不到 request 文件: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SchemeError(f"request 文件不是合法 JSON: {path} ({exc})") from exc
    return validate_request(payload)


def read_request_batch(path: Path) -> List[Dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise SchemeError(f"找不到 requests 文件: {path}") from exc
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise SchemeError(f"requests 文件没有表头: {path}")
    header = [name.strip() for name in reader.fieldnames]
    if sorted(header) != sorted(REQUEST_FIELDS):
        raise SchemeError(f"requests 表头必须恰好是 {list(REQUEST_FIELDS)}, 实际 {header}")
    requests: List[Dict[str, str]] = []
    for line_no, row in enumerate(reader, start=2):
        if any(value is None for value in row.values()):
            raise SchemeError(f"requests 第 {line_no} 行列数与表头不一致")
        requests.append(validate_request({key.strip(): row[key] for key in reader.fieldnames}))
    if not requests:
        raise SchemeError("requests 批次为空")
    seen: Dict[str, int] = {}
    for index, request in enumerate(requests):
        if request["request_id"] in seen:
            raise SchemeError(f"request_id 批内重复: {request['request_id']}")
        seen[request["request_id"]] = index
    if len(requests) > 100:
        raise SchemeError(f"Contract 1.0 单批最多 100 条 Request, 实际 {len(requests)}")
    return requests


# --------------------------------------------------------------------------- #
# 三频数据读取与逐 Request 截断
# --------------------------------------------------------------------------- #
def load_frames(data_dir: Path) -> Dict[str, Any]:
    if not data_dir.is_dir():
        raise SchemeError(f"--data-dir 不是目录: {data_dir}")

    weekly_path = data_dir / WEEKLY_FILE
    daily_path = data_dir / DAILY_FILE
    for path in (weekly_path, daily_path):
        if not path.exists():
            raise SchemeError(f"缺少实际消费的数据文件: {path}")

    weekly = pd.read_csv(weekly_path, dtype={"week_id": "string"}, encoding="utf-8-sig", low_memory=False)
    weekly.columns = [str(c).strip().lstrip("\ufeff") for c in weekly.columns]
    if not list(weekly.columns) or weekly.columns[0] != "week_id":
        raise SchemeError(f"{WEEKLY_FILE} 第一列必须是 week_id")
    if weekly.columns.duplicated().any():
        raise SchemeError(f"{WEEKLY_FILE} 存在重复列名")
    if weekly.empty:
        raise SchemeError(f"{WEEKLY_FILE} 为空")
    week_key = weekly["week_id"].astype("string").str.strip()
    if week_key.isna().any() or week_key.eq("").any():
        raise SchemeError(f"{WEEKLY_FILE} 的 week_id 不允许为空")
    if not week_key.str.fullmatch(r"\d{6}").all():
        raise SchemeError(f"{WEEKLY_FILE} 的 week_id 必须是六位数字字符串")
    if week_key.duplicated().any():
        raise SchemeError(f"{WEEKLY_FILE} 的 week_id 必须唯一")
    week_numeric = week_key.astype(int)
    if not bool((week_numeric.diff().dropna() > 0).all()):
        raise SchemeError(f"{WEEKLY_FILE} 的 week_id 必须升序")
    weekly = weekly.drop(columns=["week_id"])
    weekly.insert(0, "week_id", week_numeric.to_numpy())

    daily = pd.read_csv(daily_path, dtype={"date": "string"}, encoding="utf-8-sig", low_memory=False)
    daily.columns = [str(c).strip().lstrip("\ufeff") for c in daily.columns]
    if not list(daily.columns) or daily.columns[0] != "date":
        raise SchemeError(f"{DAILY_FILE} 第一列必须是 date")
    if daily.columns.duplicated().any():
        raise SchemeError(f"{DAILY_FILE} 存在重复列名")
    if daily.empty:
        raise SchemeError(f"{DAILY_FILE} 为空")
    daily_key = pd.to_datetime(daily["date"].astype("string").str.strip(), errors="coerce")
    if daily_key.isna().any():
        raise SchemeError(f"{DAILY_FILE} 的 date 存在无法解析的值")
    if daily_key.duplicated().any():
        raise SchemeError(f"{DAILY_FILE} 的 date 必须唯一")
    if not bool((daily_key.diff().dropna() > pd.Timedelta(0)).all()):
        raise SchemeError(f"{DAILY_FILE} 的 date 必须升序")
    daily = daily.drop(columns=["date"])
    daily.insert(0, "date", daily_key.to_numpy())

    missing_weekly = [c for c in WEEKLY_CONSUMED if c not in weekly.columns]
    if missing_weekly:
        raise SchemeError(f"{WEEKLY_FILE} 缺少本方案消费的列: {missing_weekly}")
    missing_daily = [c for c in DAILY_CONSUMED if c not in daily.columns]
    if missing_daily:
        raise SchemeError(f"{DAILY_FILE} 缺少本方案消费的列: {missing_daily}")

    weekly = weekly[["week_id"] + WEEKLY_CONSUMED].copy()
    daily = daily[["date"] + DAILY_CONSUMED].copy()

    calendar = read_week_calendar(data_dir / CALENDAR_FILE)
    _log(
        f"业务周日历: {CALENDAR_FILE} {len(calendar)} 行, "
        f"week_id {int(calendar['week_id'].min())} ~ {int(calendar['week_id'].max())}"
    )

    return {"weekly": weekly, "daily": daily, "calendar": calendar}


def truncate_for_request(frames: Dict[str, Any], request: Dict[str, str]) -> Dict[str, Any]:
    weekly: pd.DataFrame = frames["weekly"]
    daily: pd.DataFrame = frames["daily"]
    request_id = request["request_id"]

    weekly_cutoff = int(request["weekly_cutoff_key"])
    positions = np.flatnonzero(weekly["week_id"].to_numpy() == weekly_cutoff)
    if positions.size != 1:
        raise SchemeError(
            f"[{request_id}] weekly_cutoff_key={request['weekly_cutoff_key']} 在 {WEEKLY_FILE} 中不存在或不唯一"
        )
    weekly_cut = weekly.iloc[: int(positions[0]) + 1].reset_index(drop=True)

    daily_cutoff = pd.Timestamp(request["daily_cutoff_key"])
    day_positions = np.flatnonzero(daily["date"].to_numpy() == daily_cutoff.to_datetime64())
    if day_positions.size != 1:
        raise SchemeError(
            f"[{request_id}] daily_cutoff_key={request['daily_cutoff_key']} 在 {DAILY_FILE} 中不存在或不唯一"
        )
    daily_cut = daily.iloc[: int(day_positions[0]) + 1].reset_index(drop=True)

    if weekly_cut.empty or daily_cut.empty:
        raise SchemeError(f"[{request_id}] 截断后没有可用数据")
    if weekly_cutoff < MODEL_START_WEEK:
        raise SchemeError(
            f"[{request_id}] weekly_cutoff_key={weekly_cutoff} 早于本方案最早可建模周 {MODEL_START_WEEK}"
        )

    calendar = frames["calendar"]
    lookup = calendar_series(calendar)
    if weekly_cutoff not in set(calendar["week_id"].to_numpy().tolist()):
        raise SchemeError(
            f"[{request_id}] 业务周日历 {CALENDAR_FILE} 未覆盖 weekly_cutoff_key={weekly_cutoff}; "
            f"该表覆盖 {int(calendar['week_id'].min())} ~ {int(calendar['week_id'].max())}。"
            f"请让平台刷新 {CALENDAR_FILE}(详见交接文档 §5)。"
        )
    if daily_cutoff not in lookup.index:
        raise SchemeError(
            f"[{request_id}] 业务周日历 {CALENDAR_FILE} 未收录 daily_cutoff_key="
            f"{request['daily_cutoff_key']}; 该表覆盖 "
            f"{lookup.index.min():%Y-%m-%d} ~ {lookup.index.max():%Y-%m-%d}。"
        )
    anchor = int(lookup.loc[daily_cutoff])
    if anchor != weekly_cutoff:
        raise SchemeError(
            f"[{request_id}] 业务周日历与 Request 锚点不一致: daily_cutoff_key="
            f"{request['daily_cutoff_key']} 在 {CALENDAR_FILE} 里落在 week_id={anchor}, "
            f"但 weekly_cutoff_key={weekly_cutoff}。"
        )
    # 直接查表得到每个自然日的 week_id, 与研究基准的 daily.merge(date_map) 等价;
    # 日历未收录的自然日直接丢弃, 不做任何推算。
    daily_week = pd.Series(
        lookup.reindex(pd.DatetimeIndex(daily_cut["date"])).to_numpy(),
        index=daily_cut.index,
    )

    daily_cut = daily_cut.assign(week_id=daily_week)
    daily_cut = daily_cut[daily_cut["week_id"].notna()].copy()
    daily_cut["week_id"] = daily_cut["week_id"].astype(int)
    return {"weekly": weekly_cut, "daily": daily_cut}


# --------------------------------------------------------------------------- #
# 特征、打分与 walk-forward 模型
# --------------------------------------------------------------------------- #
def aggregate_daily(daily: pd.DataFrame, code: str, agg: str) -> pd.Series:
    values = safe_numeric(daily[code])
    grouped = pd.DataFrame({"week_id": daily["week_id"], "value": values})
    if agg == "mean":
        return grouped.groupby("week_id")["value"].mean()
    return grouped.groupby("week_id")["value"].last()


def resolve_raw(spec: CausalSpec, weekly: pd.DataFrame, daily: pd.DataFrame) -> pd.Series:
    def one(code: str, source: str, agg: str) -> pd.Series:
        if source == "weekly":
            return safe_numeric(weekly[code])
        aggregated = aggregate_daily(daily, code, agg)
        return weekly["week_id"].map(aggregated).astype(float)

    base = one(spec.code, spec.source, spec.agg)
    if spec.spread_against is None:
        return base
    other = one(spec.spread_against, spec.spread_source or spec.source, spec.agg)
    return base.sub(other)


def build_feature_frame(weekly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    features = weekly[["week_id"]].copy()
    pressure_cols: List[str] = []
    for spec in CAUSAL_SPECS:
        raw = resolve_raw(spec, weekly, daily)
        column = f"{spec.feature_id}__yield_pressure"
        features[column] = clipped(primary_score(raw, spec.transform) * spec.yield_sign)
        pressure_cols.append(column)

    for module in MODULE_ORDER:
        cols = [
            f"{spec.feature_id}__yield_pressure"
            for spec in CAUSAL_SPECS
            if spec.module == module and spec.yield_sign != 0.0
        ]
        out_col = f"{module}_yield_pressure"
        if cols:
            matrix = features[cols]
            features[out_col] = matrix.sum(axis=1, min_count=1).div(matrix.notna().sum(axis=1))
        else:
            features[out_col] = np.nan
    module_cols = [f"{module}_yield_pressure" for module in MODULE_ORDER]
    features["yield_pressure_score"] = features[module_cols].mean(axis=1, skipna=True)
    features["bond_price_score"] = -features["yield_pressure_score"]

    rule = np.where(features["bond_price_score"].ge(0), 1.0, -1.0)
    features["causal_rule_pred_label"] = rule
    features.loc[features["bond_price_score"].isna(), "causal_rule_pred_label"] = np.nan
    module_abs = features[module_cols].abs()
    features["top_pressure_module_id"] = module_abs.idxmax(axis=1).str.replace(
        "_yield_pressure", "", regex=False
    )
    features.attrs["model_feature_cols"] = pressure_cols + module_cols + [
        "yield_pressure_score",
        "bond_price_score",
    ]
    return features


def build_labels(weekly: pd.DataFrame) -> pd.DataFrame:
    target = safe_numeric(weekly[TARGET_COL])
    future_return = target.shift(-1).div(target).sub(1.0)
    actual = np.select(
        [future_return > LABEL_THRESHOLD, future_return < -LABEL_THRESHOLD], [1.0, -1.0], default=0.0
    )
    actual = actual.astype(float)
    actual[future_return.isna().to_numpy()] = np.nan
    return pd.DataFrame(
        {
            "week_id": weekly["week_id"].to_numpy(),
            "target_value": target.to_numpy(),
            "yield_value": safe_numeric(weekly[YIELD_COL]).to_numpy(),
            "actual_label": actual,
        }
    )


def fit_walk_forward(features: pd.DataFrame, labels: pd.DataFrame, request_id: str) -> Dict[str, float]:
    feature_cols: List[str] = features.attrs["model_feature_cols"]
    matrix = features[feature_cols]
    y_all = labels["actual_label"].to_numpy()
    last = len(features) - 1

    train_idx = np.arange(0, last)
    train_idx = train_idx[~pd.isna(y_all[train_idx])]
    train_idx = train_idx[y_all[train_idx] != 0]
    if len(train_idx) < MIN_TRAIN_WEEKS:
        raise SchemeError(
            f"[{request_id}] 可用训练样本 {len(train_idx)} 少于 {MIN_TRAIN_WEEKS}, 无法产生合法方向"
        )
    if len(np.unique(y_all[train_idx])) < 2:
        raise SchemeError(f"[{request_id}] 训练标签只有一个类别, 无法产生合法方向")

    x_train = matrix.iloc[train_idx]
    y_train = (y_all[train_idx] == 1).astype(int)
    x_pred = matrix.iloc[[last]]

    logit = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(C=LOGIT_C, class_weight="balanced", solver="liblinear", max_iter=1000)),
        ]
    )
    ridge = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", RidgeClassifier(class_weight="balanced", alpha=RIDGE_ALPHA)),
        ]
    )

    probs: List[float] = []
    try:
        logit.fit(x_train, y_train)
        probs.append(float(logit.predict_proba(x_pred)[0, 1]))
    except Exception as exc:  # noqa: BLE001 - 与研究基准一致: 单模型失败不阻断
        _log(f"[{request_id}] logistic 拟合失败, 已跳过: {exc}")
    try:
        ridge.fit(x_train, y_train)
        probs.append(sigmoid(float(ridge.decision_function(x_pred)[0])))
    except Exception as exc:  # noqa: BLE001
        _log(f"[{request_id}] ridge 拟合失败, 已跳过: {exc}")
    if not probs:
        raise SchemeError(f"[{request_id}] 两个基模型都无法拟合, 无法产生合法方向")

    prob_up = float(np.mean(probs))
    return {
        "prob_up": prob_up,
        "pred_label": 1.0 if prob_up >= 0.5 else -1.0,
        "train_samples": float(len(train_idx)),
        "feature_count": float(len(feature_cols)),
    }


# --------------------------------------------------------------------------- #
# 单条 Request 的完整算法
# --------------------------------------------------------------------------- #
def predict_one(frames: Dict[str, Any], request: Dict[str, str]) -> int:
    cut = truncate_for_request(frames, request)
    weekly = cut["weekly"]
    daily = cut["daily"]

    features = build_feature_frame(weekly, daily)
    labels = build_labels(weekly)
    model = fit_walk_forward(features, labels, request["request_id"])

    direction = model["pred_label"]

    if pd.isna(direction) or int(direction) not in (-1, 1):
        raise SchemeError(f"[{request['request_id']}] 算法未能产生合法方向: {direction!r}")
    _log(
        f"[{request['request_id']}] week_id={request['weekly_cutoff_key']} "
        f"prob_up={model['prob_up']:.6f} train={int(model['train_samples'])} "
        f"direction={int(direction)}"
    )
    return int(direction)


# --------------------------------------------------------------------------- #
# 输出 (原子写入; 业务结果只进 --output, 日志只进 stderr)
# --------------------------------------------------------------------------- #
def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=str(path.parent), prefix=f".{path.name}.", delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def write_prediction(path: Path, request: Dict[str, str], direction: int) -> None:
    payload = {
        "request_id": request["request_id"],
        "predict_date": request["predict_date"],
        "feature_date": request["feature_date"],
        "target_date": request["target_date"],
        "predicted_direction": int(direction),
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_backtest(path: Path, requests: Sequence[Dict[str, str]], directions: Sequence[int]) -> None:
    lines = [",".join(RESULT_FIELDS)]
    for request, direction in zip(requests, directions):
        lines.append(
            ",".join(
                [
                    request["request_id"],
                    request["predict_date"],
                    request["feature_date"],
                    request["target_date"],
                    str(int(direction)),
                ]
            )
        )
    atomic_write(path, "\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{SCHEME_ID}.py",
        description=(
            f"{SCHEME_ID} | Blackbox V2 Contract 1.0 | target_tenor={TARGET_TENOR} "
            f"task_type={TASK_TYPE} horizon={HORIZON} | "
            f"predicted_direction=1 表示目标周收益率高于特征周"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    predict = sub.add_parser("predict", help="单点预测")
    predict.add_argument("--request", type=Path, required=True)
    predict.add_argument("--data-dir", type=Path, required=True)
    predict.add_argument("--output", type=Path, required=True)

    backtest = sub.add_parser("backtest", help="批量回测 (单批 1~100 条)")
    backtest.add_argument("--requests", type=Path, required=True)
    backtest.add_argument("--data-dir", type=Path, required=True)
    backtest.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        frames = load_frames(args.data_dir)
        if args.command == "predict":
            request = read_single_request(args.request)
            direction = predict_one(frames, request)
            write_prediction(args.output, request, direction)
        else:
            requests = read_request_batch(args.requests)
            directions = [predict_one(frames, request) for request in requests]
            write_backtest(args.output, requests, directions)
    except SchemeError as exc:
        _log(f"FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        _log(f"FAILED (unexpected): {type(exc).__name__}: {exc}")
        return 1
    _log("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
