#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wavg_5y_gapflip_v5 —— 周均方向预测 v5(gap 规则 + 失效翻转集)

任务:5Y · weekly_average · horizon=1
     target_rule = target_week_average_yield_vs_feature_week_average_yield

============================ 算法概要 ============================
基准规则 M0
    gap_t = 本周周收 − 本周周均(本周末已知)
    M0    = sign(gap_t)                      历史 acc≈0.71 / bacc≈0.70

v5 = M0 + 失效翻转集
    当本周被判定落入"M0 大概率出错"的极端区域时,翻转 M0 的方向。
    翻转区由四个**机制互异**的分量取并集(任一触发即翻转):

    ① q     元模型 P(M0 出错) 的 156 周滚动 94% 分位以上(尾 6%)
            元模型 = 逐周重训的 Logistic,27 个特征,核心是 sign(gap)×x 支持/反对编码
    ② twist 1Y 与 10Y 的 gap 当周反号(曲线扭转)且本期限 |z_gap| 小 —— 尾 2%
    ③ rev   最后交易日反转 × 收盘位置背离 gap 方向 —— 尾 2%
    ④ s2n   |gap|/EWM13 波动 的低尾 8%,**且过去 52 周该尾已实现错误率 >0.5**
            (自适应开关:让规则自己识别市场结构变化,不硬编码日期)

    实测(建模期 n=1899):翻转覆盖 8.2%、翻转区 M0 错误率 0.607
                        acc 0.7083→0.7209  bacc 0.7021→0.7142

============================ 数据来源 ============================
★ 周均(WI1C)与周收(WI3C)一律**由日频 WI0C 重算**,不使用周频表的 WI1C/WI3C 列:
  (1) DataBridge 周频基线**不含 TB3YWI1C / TB7YWI1C**,3Y/7Y 的周均只能由日频得到;
  (2) 历史核验显示官方周频聚合在最新 1–2 周可能与日频不符(含 gap 符号翻转),
      日频重算是权威口径。
  重算口径(与官方元数据一致):周均 = 当周该期限所有非空日收的算术均值;
  周收 = 当周该期限**自己最后一个非空日收**(逐期限 last-valid,不是统一取
  全期限共同的最后交易日 —— 10Y 在个别周的周五缺值)。
  524 周真值核验:3Y/7Y 最大差 3.6e-15,1Y/5Y/10Y 最大差 3.3e-07
  (周频只存六位小数,短周舍入放大)。★一致性检查容差必须用 1e-4,不可用 1e-9。

★ date → week_id 映射由数据本身推导,不假定 ISO 周、不由 week_id 反推日期:
  (1) 自然周切分 —— 同一 ISO 周一的交易日必属同一 week_id(权威日历全量核验
      6016 天 / 860 周,0 违例;官方唯一例外是整周休市周并入下一周成 14 天
      week_id,而休市半边无交易日,故物理交易周 → week_id 仍严格 1:1 保序);
  (2) 逐期限 last-valid 顺序锚定(容差 1e-6),锚点之间按顺序填充,周数对不上
      就丢弃该周、绝不猜。
  与权威日历一致率实测 = 1.000000(3822/3822)。改动此处必须重跑
  tools/check_weekmap.py(见交接文档 §3.2 的已修复 bug 说明)。

★权威交易日历:date → week_id 直接引用 --data-dir 下的 api_wind_date.csv
  (两列 rdate,week_id),由平台经 platform_inputs: api-wind-date-v1 提供,缺失即报错。
  引用点见下方 load_calendar()。
  日历总是滞后于平台数据,其未覆盖到的日期由上面的推导规则按同一套自然周口径延伸
  (两者已实测逐位等价:3822/3822 周 id 相同、5 期限重算周均最大差 0.00e+00、
   5 方案 × 67 周全链路输出逐字节一致)。详见交接文档 §3.4。

============================ 因果性 ============================
所有特征在"本周末"即可算出;所有滚动/扩展统计的窗口都不含未来:
  · 扩展 z 分数:mean/std 均 .shift(1)
  · 滚动分位阈值:第 t 周只用 [t−156, t−1]
  · 元模型:第 t 周只用 < t 的样本训练
  · 自适应开关:只用 t−1 及更早的已实现标签
因此对任一 Request,截止键之后的行不会影响结果(平台"后续行隔离"检查)。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ 方案身份
SCHEME_ID = "wavg_5y_gapflip_v5"
TARGET_TENOR = "5Y"
TASK_TYPE = "weekly_average"
HORIZON = 1
TARGET_RULE = "target_week_average_yield_vs_feature_week_average_yield"

# ------------------------------------------------------------------ 固定口径
TENORS = ["1Y", "3Y", "5Y", "7Y", "10Y"]
DAILY_CLOSE = {"1Y": "TB1YWI0C", "3Y": "TB3YWI0C", "5Y": "TB5YWI0C",
               "7Y": "TB7YWI0C", "10Y": "TB0YWI0C"}
WEEKLY_CLOSE = {"1Y": "TB1YWI3C", "3Y": "TB3YWI3C", "5Y": "TB5YWI3C",
                "7Y": "TB7YWI3C", "10Y": "TB0YWI3C"}
FUNDING_COL = "90000002"           # DR007−OMO 利差(日频)

COMMON_START = 201603              # 5 期限齐备起点
WARMUP_WEEKS = 156                 # 元模型冷启动
ROLL_WIN = 156                     # 尾部分位滚动窗
MIN_HIST = 200                     # 分位估计的最少历史观测
META_C = 0.3                       # 元模型正则强度
RANDOM_STATE = 42

TAIL_Q = 6                         # ① q 尾部百分比
TAIL_TWIST = 2                     # ② twist 尾部百分比
TAIL_REV = 2                       # ③ rev 尾部百分比
S2N_TAIL = 8                       # ④ s2n 低尾百分比
S2N_K = 52                         # ④ 自适应回看周数
S2N_THR = 0.5                      # ④ 启用门槛(近期错误率)
S2N_MINHIST = 15                   # ④ 启用所需的最少历史触发数

DIR_VARS = ["own_mom1_z", "own_mom4_z", "own_mom13_z", "own_mom26_z",
            "iw_slope", "iw_last_mom", "iw_two_mom", "iw_accel",
            "cv_slope_chg", "cv_curv_chg", "dr_omo_z", "rv_z"]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


class SchemeError(RuntimeError):
    pass


# ================================================================== 数据读取
def load_frames(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """只读取本方案实际消费的两份文件(日频、周频)。月频不消费。"""
    dpath, wpath = data_dir / "daily_output.csv", data_dir / "weekly_output.csv"
    for p in (dpath, wpath):
        if not p.is_file():
            raise SchemeError(f"missing consumed file: {p.name}")
    daily = pd.read_csv(dpath, dtype={"date": "string"}, encoding="utf-8-sig")
    weekly = pd.read_csv(wpath, dtype={"week_id": "string"}, encoding="utf-8-sig")
    if daily.empty or weekly.empty:
        raise SchemeError("consumed file is empty")
    if list(daily.columns)[:1] != ["date"]:
        raise SchemeError("daily_output.csv must start with column 'date'")
    if list(weekly.columns)[:1] != ["week_id"]:
        raise SchemeError("weekly_output.csv must start with column 'week_id'")

    need_d = [DAILY_CLOSE[t] for t in TENORS]
    miss = [c for c in need_d if c not in daily.columns]
    if miss:
        raise SchemeError(f"daily_output.csv missing consumed columns: {miss}")
    need_w = [WEEKLY_CLOSE[t] for t in TENORS]
    miss = [c for c in need_w if c not in weekly.columns]
    if miss:
        raise SchemeError(f"weekly_output.csv missing consumed columns: {miss}")

    daily["date"] = daily["date"].astype("string").str.strip()
    weekly["week_id"] = weekly["week_id"].astype("string").str.strip()
    if daily["date"].duplicated().any():
        raise SchemeError("daily_output.csv 'date' must be unique")
    if weekly["week_id"].duplicated().any():
        raise SchemeError("weekly_output.csv 'week_id' must be unique")
    dt = pd.to_datetime(daily["date"], errors="coerce")
    if dt.isna().any():          # 同一文件内混合日期格式(本数据源出现过)时再试一次
        dt = pd.to_datetime(daily["date"], errors="coerce", format="mixed")
    daily["_dt"] = dt
    if daily["_dt"].isna().any():
        raise SchemeError("daily_output.csv 'date' contains unparsable values")
    daily = daily.sort_values("_dt").reset_index(drop=True)
    weekly = weekly.sort_values("week_id").reset_index(drop=True)
    daily["_authwid"] = load_calendar(daily, data_dir)   # 必需平台输入,缺失即报错
    return daily, weekly


def truncate(daily: pd.DataFrame, weekly: pd.DataFrame,
             daily_key: str, weekly_key: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按各自截止键精确定位并保留首行至截止键行(含)。"""
    dnorm = daily["_dt"].dt.strftime("%Y-%m-%d")
    hit = np.flatnonzero((dnorm == daily_key).to_numpy())
    if len(hit) != 1:
        raise SchemeError(f"daily_cutoff_key {daily_key!r} not found exactly once")
    hitw = np.flatnonzero((weekly["week_id"] == weekly_key).to_numpy())
    if len(hitw) != 1:
        raise SchemeError(f"weekly_cutoff_key {weekly_key!r} not found exactly once")
    return (daily.iloc[: hit[0] + 1].copy().reset_index(drop=True),
            weekly.iloc[: hitw[0] + 1].copy().reset_index(drop=True))


# ==================== 权威交易日历(平台输入 api-wind-date-v1,交接文档 §3.4)
# ★ api_wind_date.csv 由平台经 platform_inputs: api-wind-date-v1 放入 --data-dir,
#   与三份 DataBridge CSV 同目录。两列:rdate(日期)、week_id(六位整数 YYYYWW)。
CALENDAR_FILE = "api_wind_date.csv"
_CAL_STATE = {"logged": False, "worst": 0}


def load_calendar(daily: pd.DataFrame, data_dir: Path) -> pd.Series:
    """读取 --data-dir 下的权威交易日历,返回逐日 week_id(日历未覆盖的日期为 NaN)。

    ★这是**必需**的平台输入(platform_inputs: api-wind-date-v1):缺失、缺列、无法解析
      → 直接报错,不静默降级。日历未覆盖到的日期(必然存在,日历总是滞后于平台数据)
      由 derive_week_map() 按同一套自然周口径延伸,两者已实测逐位等价。
    """
    p = data_dir / CALENDAR_FILE
    if not p.is_file():
        raise SchemeError(
            f"missing calendar input: {p.name} must be in --data-dir "
            f"(platform_inputs: api-wind-date-v1)")
    try:
        cal = pd.read_csv(p, encoding="utf-8-sig")
    except Exception as exc:                       # noqa: BLE001 交付物损坏须显式失败
        raise SchemeError(f"{p.name} unreadable: {exc}") from exc
    for c in ("rdate", "week_id"):
        if c not in cal.columns:
            raise SchemeError(f"{p.name} must contain column {c!r}")
    rd = pd.to_datetime(cal["rdate"], errors="coerce")
    if rd.isna().any():          # 日历里混合日期格式时再试一次
        rd = pd.to_datetime(cal["rdate"], errors="coerce", format="mixed")
    rd = rd.dt.normalize()
    wid = pd.to_numeric(cal["week_id"], errors="coerce")
    ok = rd.notna() & wid.notna()
    if not bool(ok.any()):
        raise SchemeError(f"{p.name} has no parsable (rdate, week_id) row")
    m = pd.Series(wid[ok].to_numpy(), index=rd[ok]).groupby(level=0).last()
    return daily["_dt"].dt.normalize().map(m).astype("float64")


def apply_calendar(daily: pd.DataFrame, weekly: pd.DataFrame,
                   wmap: pd.Series) -> pd.Series:
    """用权威日历定 week_id;日历未覆盖到的日期用推导规则延伸。

    只接受周频表里确实存在的 week_id(日历里 200901、202616 这类没有周频行的 id
    会被丢弃),保证周集合与周频表一致,否则不完整尾周会多出一行、污染 shift(-1) 标签。
    """
    if "_authwid" not in daily.columns:
        return wmap
    a = pd.to_numeric(daily["_authwid"], errors="coerce")
    if not bool(a.notna().any()):
        log(f"[{SCHEME_ID}] WARNING calendar covers none of the daily rows — "
            f"全部回退到推导规则,请检查 {CALENDAR_FILE} 的日期范围")
        return wmap
    a = a.where(a.isin(set(weekly["week_id"].astype(int).tolist())))
    use = a.notna()
    if not bool(use.any()):
        return wmap
    both = (use & (wmap > 0)).to_numpy()
    if both.any():
        dis = int((wmap.to_numpy()[both] != a.to_numpy()[both].astype("int64")).sum())
        if dis > _CAL_STATE["worst"]:
            _CAL_STATE["worst"] = dis
            log(f"[{SCHEME_ID}] WARNING calendar vs derived disagree on "
                f"{dis} daily rows — 请跑 tools/check_weekmap.py 定位")
    if not _CAL_STATE["logged"]:
        _CAL_STATE["logged"] = True
        log(f"[{SCHEME_ID}] week map: calendar {int(use.sum())} rows"
            f" / rule-extended {int((~use).sum())} rows")
    u = use.to_numpy()
    out = wmap.to_numpy(dtype="int64", copy=True)
    out[u] = a.to_numpy()[u].astype("int64")
    return pd.Series(out, index=wmap.index, name="week_id")


# ================================================== date → week_id(数据推导)
# 数据管道常量。与模型参数无关,正常情况下不需要改动。
MATCH_TOL = 1e-6       # 周频只保留六位小数,锚点比对不能用 1e-9(见交接文档 §3.2)
MIN_MATCH_TENORS = 2   # 至少两个期限可比才认一个锚点
ANCHOR_WINDOW = 12     # 首锚之后的前向搜索窗(周)


def derive_week_map(daily: pd.DataFrame, weekly: pd.DataFrame) -> pd.Series:
    """把日频行归入 week_id。返回与 daily 等长的 int 序列(未归入的行 = -1)。

    只用数据本身,不读交易日历文件、也不由 week_id 反推日期。两步:

    1) 自然周切分。同一 ISO 周一的交易日必属同一 week_id。已用权威交易日历
       全量核验:6016 天 / 860 个自然周,没有任何一个自然周被 week_id 拆开。
       官方唯一的例外是"整周休市的周(春节/国庆)并入下一周"形成 14 天的
       week_id,而休市那半边没有交易日,所以"物理交易周 → week_id"依然是
       严格 1:1 保序。

    2) 顺序锚定。官方周收对每个期限各取该期限自己最后一个有效日频值
       (逐期限 last-valid,不是统一取全期限共同的最后交易日 —— 10Y 日频在
       个别周的周五缺值,若要求"5 期限同日全命中"会匹配失败)。按 last-valid
       规则给每个物理交易周找唯一的 weekly 行做锚点,锚点之间按顺序填充;
       一旦两侧锚点之间的周数对不上就留 -1 丢弃该周,绝不猜 —— 避免把上一周
       的交易日并进下一周。
    """
    dk = [DAILY_CLOSE[t] for t in TENORS]
    wk = [WEEKLY_CLOSE[t] for t in TENORS]
    wv = weekly[wk].to_numpy(float)
    wid = weekly["week_id"].to_numpy()
    nw = len(weekly)

    # ---- 1) 自然周切分(daily 已按日期升序 ⇒ codes 非降)
    mon = daily["_dt"] - pd.to_timedelta(daily["_dt"].dt.weekday, unit="D")
    codes, _ = pd.factorize(mon, sort=False)
    if len(codes) == 0:
        raise SchemeError("no daily rows")
    m = int(codes.max()) + 1

    # 每个物理周、每个期限的 last-valid 日收(= 官方周收口径)
    D = daily.assign(_pw=codes)
    pv = np.full((m, len(TENORS)), np.nan)
    for c, col in enumerate(dk):
        s = D.dropna(subset=[col]).groupby("_pw")[col].last()
        pv[s.index.to_numpy(int), c] = s.to_numpy(float)

    def _hit(i: int, k: int) -> bool:
        a, b = pv[i], wv[k]
        ok = np.isfinite(a) & np.isfinite(b)
        if int(ok.sum()) < MIN_MATCH_TENORS:
            return False
        return bool(np.all(np.abs(a[ok] - b[ok]) <= MATCH_TOL))

    # ---- 2) 顺序锚定。首锚全表扫且必须唯一,之后只看前向小窗
    anchors: list[tuple[int, int]] = []
    k, wide = 0, True
    for i in range(m):
        if wide:
            cand = [kk for kk in range(nw) if _hit(i, kk)]
            if len(cand) == 1:
                anchors.append((i, cand[0]))
                k, wide = cand[0] + 1, False
            continue
        for kk in range(k, min(k + ANCHOR_WINDOW, nw)):
            if _hit(i, kk):
                anchors.append((i, kk))
                k = kk + 1
                break
    if not anchors:
        raise SchemeError("failed to anchor any trading week to a weekly row")

    # ---- 3) 锚点之间与两端按顺序填充;周数对不上就留 -1
    pw = np.full(m, -1, dtype=np.int64)
    for i, kk in anchors:
        pw[i] = int(wid[kk])
    for (i1, k1), (i2, k2) in zip(anchors, anchors[1:]):
        if i2 - i1 == k2 - k1:
            for d in range(1, i2 - i1):
                pw[i1 + d] = int(wid[k1 + d])
    i0, k0 = anchors[0]
    for d in range(1, i0 + 1):
        if k0 - d < 0:
            break
        pw[i0 - d] = int(wid[k0 - d])
    i9, k9 = anchors[-1]
    for d in range(1, m - i9):
        if k9 + d >= nw:
            break
        pw[i9 + d] = int(wid[k9 + d])

    if (pw > 0).sum() == 0:
        raise SchemeError("failed to derive any date→week_id mapping")
    return pd.Series(pw[codes], index=daily.index, name="week_id")


# ================================================================== 特征工程
def _czs(s: pd.Series, mp: int = 52) -> pd.Series:
    """因果扩展窗 z:均值与标准差都 shift(1)。"""
    mu = s.expanding(min_periods=mp).mean().shift(1)
    sd = s.expanding(min_periods=mp).std().shift(1)
    return (s - mu) / sd.replace(0, np.nan)


def weekly_from_daily(daily: pd.DataFrame, wmap: pd.Series) -> pd.DataFrame:
    """由日频重算 5 期限的周均/周收/交易日数,以及周内路径特征。"""
    d = daily.assign(week_id=wmap)
    d = d[d["week_id"] > 0]
    if d.empty:
        raise SchemeError("no daily rows mapped to a week")
    out = {}
    for t in TENORS:
        col = DAILY_CLOSE[t]
        g = d.dropna(subset=[col]).groupby("week_id")[col]
        out[f"mean_{t}"] = g.mean()
        out[f"close_{t}"] = g.last()
        if t == "10Y":
            out["ndays"] = g.size()
    W = pd.DataFrame(out)
    W.index.name = "week_id"
    W = W.reset_index()

    # 周内路径(逐期限)
    iw_all = {}
    for t in TENORS:
        col = DAILY_CLOSE[t]
        rows = []
        for w, g in d.dropna(subset=[col]).groupby("week_id"):
            v = g[col].to_numpy(float)
            n = len(v)
            rng = float(v.max() - v.min())
            rows.append({
                "week_id": w,
                "iw_last_mom": float(v[-1] - v[-2]) if n >= 2 else 0.0,
                "iw_two_mom": float(v[-1] - v[-3]) if n >= 3 else 0.0,
                "iw_slope": float(np.polyfit(np.arange(n), v, 1)[0]) if n >= 3 else 0.0,
                "iw_pos": float((v[-1] - v.min()) / rng) if rng > 0 else 0.5,
                "iw_accel": float((v[-1] - v[-2]) - (v[-2] - v[-3])) if n >= 3 else 0.0,
                "iw_rv": float(np.std(np.diff(v))) if n >= 3 else 0.0,
            })
        p = pd.DataFrame(rows).sort_values("week_id").reset_index(drop=True)
        p["iw_rv_chg"] = p["iw_rv"] - p["iw_rv"].shift(1)
        iw_all[t] = p

    # 日历属性:用交易日推导周跨度(平台不提供自然日历)
    span = d.groupby("week_id")["_dt"].agg(["min", "max"]).reset_index()
    span = span.sort_values("week_id").reset_index(drop=True)
    nxt_first = span["min"].shift(-1)
    span["cal_start"] = span["min"]
    span["cal_end"] = (nxt_first - pd.Timedelta(days=1)).fillna(
        span["max"] + pd.Timedelta(days=2))
    flags = []
    for _, r in span.iterrows():
        days = pd.date_range(r["cal_start"], r["cal_end"])
        flags.append({
            "week_id": r["week_id"],
            "has_month_end": int(any(x.is_month_end for x in days)),
            "has_quarter_end": int(any(x.is_quarter_end for x in days)),
            "has_tax": int(any(13 <= x.day <= 17 for x in days)),
            "has_month_start": int(any(x.day <= 3 for x in days)),
            "month": int(r["cal_end"].month),
        })
    CF = pd.DataFrame(flags).merge(
        d.groupby("week_id").size().rename("ndays_cal").reset_index(),
        on="week_id", how="left")
    nx = CF.shift(-1).add_prefix("next_")
    nx["week_id"] = CF["week_id"]
    CF = CF.merge(nx, on="week_id")
    CF["next_ndays"] = CF["next_ndays_cal"].fillna(5.0)
    for c in ("next_has_month_end", "next_has_quarter_end",
              "next_has_tax", "next_has_month_start"):
        CF[c] = CF[c].fillna(0.0)
    CF["next_month"] = CF["next_month"].fillna(CF["month"])
    CF["is_short_week"] = (CF["ndays_cal"] <= 3).astype(float)
    CF["next_is_short"] = (CF["next_ndays"] <= 3).astype(float)
    CF = CF[["week_id", "next_month", "next_has_month_end", "next_has_quarter_end",
             "next_has_tax", "next_has_month_start", "next_ndays",
             "next_is_short", "is_short_week"]]

    # 资金面:周末 as-of + 因果 z
    if FUNDING_COL in daily.columns:
        f = d.dropna(subset=[FUNDING_COL]).groupby("week_id")[FUNDING_COL].last()
        FD = pd.DataFrame({"week_id": W["week_id"]})
        FD["dr_omo"] = FD["week_id"].map(f)
        FD["dr_omo"] = FD["dr_omo"].ffill()
        FD["dr_omo_z"] = _czs(FD["dr_omo"])
    else:
        FD = pd.DataFrame({"week_id": W["week_id"], "dr_omo_z": np.nan})
    return W, iw_all, CF, FD[["week_id", "dr_omo_z"]]


def build_panel(daily: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    """构造 5 期限长表:week_id × tenor × 全部底盘特征 + gap 族 + 标签。"""
    wmap = apply_calendar(daily, weekly, derive_week_map(daily, weekly))
    W, iw_all, CF, FD = weekly_from_daily(daily, wmap)
    W = W.sort_values("week_id").reset_index(drop=True)

    # 曲线形态(用周收口径,5 期限共享)
    cv = pd.DataFrame({"week_id": W["week_id"]})
    slope = W["close_10Y"] - W["close_1Y"]
    curv = 2 * W["close_5Y"] - W["close_1Y"] - W["close_10Y"]
    for nm, v in (("cv_slope", slope), ("cv_curv", curv)):
        cv[nm + "_z"] = _czs(v)
        cv[nm + "_chg"] = v - v.shift(4)

    # 相对富贵度(周均口径的横截面 z,再做因果时序 z)
    lv = W[[f"mean_{t}" for t in TENORS]]
    mu_x, sd_x = lv.mean(axis=1), lv.std(axis=1).replace(0, np.nan)

    parts = []
    for t in TENORS:
        y = W[f"mean_{t}"]
        f = pd.DataFrame({"week_id": W["week_id"], "tenor": t, "y_t": y})
        # 标签:下周周均相对本周周均
        f["y_next"] = y.shift(-1)
        f["future_return"] = f["y_next"] / f["y_t"] - 1.0
        f["label"] = np.where(f["future_return"] > 0, 1.0,
                              np.where(f["future_return"] < 0, -1.0, 0.0))
        f.loc[f["future_return"].isna(), "label"] = np.nan
        # 自身动态
        for k in (1, 4, 13, 26):
            f[f"own_mom{k}_z"] = _czs(y - y.shift(k))
        d1 = y.diff()
        rv = d1.rolling(8).std()
        f["own_rv_z"] = _czs(rv)
        lo, hi = y.rolling(52).min(), y.rolling(52).max()
        f["own_pos52"] = (y - lo) / (hi - lo).replace(0, np.nan)
        ers = []
        for k in (4, 13, 26):
            ers.append((y - y.shift(k)).abs() / d1.abs().rolling(k).sum().replace(0, np.nan))
        f["own_er"] = pd.concat(ers, axis=1).mean(axis=1)
        # 曲线 / 富贵 / 资金
        for c in ("cv_slope_z", "cv_curv_z", "cv_slope_chg", "cv_curv_chg"):
            f[c] = cv[c]
        f["rv_z"] = _czs((W[f"mean_{t}"] - mu_x) / sd_x)
        # gap 族
        f["gap"] = W[f"close_{t}"] - W[f"mean_{t}"]
        scale = f["gap"].abs().expanding(min_periods=52).median().shift(1)
        f["z_gap"] = f["gap"] / scale.replace(0, np.nan)
        f["sign_gap"] = np.sign(f["gap"])
        f["abs_z"] = f["z_gap"].abs()
        # 周内路径 / 日历
        f = f.merge(iw_all[t], on="week_id", how="left")
        f = f.merge(CF, on="week_id", how="left")
        f = f.merge(FD, on="week_id", how="left")
        f["seas_m9"] = (f["next_month"] == 9).astype(float)
        parts.append(f)

    P = pd.concat(parts, ignore_index=True)
    P = P[P["week_id"] >= COMMON_START].sort_values(["week_id", "tenor"])
    P = P.reset_index(drop=True)
    P["pred_M0"] = np.where(P["gap"].to_numpy(float) >= 0, 1.0, -1.0)
    lab = P["label"].to_numpy(float)
    P["meta_y"] = np.where(np.isnan(lab) | (lab == 0), np.nan,
                           (np.sign(P["pred_M0"]) != np.sign(lab)).astype(float))
    return P


# ============================================================ 元模型特征 + q
def meta_features(P: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    P = P.copy()
    g = P["gap"].to_numpy(float)
    P["sg"] = np.where(g >= 0, 1.0, -1.0)
    P["mA_absz"] = P["abs_z"]
    prev = P.groupby("tenor")["gap"].shift(1)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(np.abs(g) / np.abs(prev).replace(0, np.nan))
    P["mA_logratio"] = pd.Series(lr, index=P.index).replace(
        [np.inf, -np.inf], np.nan).clip(-4, 4)
    runs = []
    for t, d in P.groupby("tenor", sort=False):
        s = np.sign(d["gap"].to_numpy(float))
        r = np.zeros(len(s))
        c = 0
        for i in range(len(s)):
            c = c + 1 if i > 0 and s[i] == s[i - 1] else 1
            r[i] = c
        runs.append(pd.Series(r, index=d.index))
    P["mA_runlen"] = pd.concat(runs).sort_index().clip(upper=8)
    for w in (52, 104):
        P[f"mA_pct{w}"] = P.groupby("tenor")["abs_z"].transform(
            lambda s: s.rolling(w, min_periods=26).rank(pct=True).shift(1))
    have = [c for c in DIR_VARS if c in P.columns]
    for c in have:
        P[f"mB_sup_{c}"] = P["sg"] * P[c]
    S = P[[f"mB_sup_{c}" for c in have]]
    P["mB_opp_count"] = (S < 0).sum(axis=1) / max(len(have), 1)
    P["mB_opp_strength"] = S.where(S < 0).abs().sum(axis=1)
    P["mB_sup_strength"] = S.where(S > 0).sum(axis=1)
    P["mB_net"] = P["mB_sup_strength"] - P["mB_opp_strength"]
    P["mC_ndays"] = P["next_ndays"]
    P["mC_gapsign"] = P["sg"]
    for t in TENORS[1:]:
        P[f"mC_ten_{t}"] = (P["tenor"] == t).astype(float)
    feats = [c for c in P.columns if c.startswith(("mA_", "mB_", "mC_"))]
    return P, feats


def walk_forward_q(P: pd.DataFrame, feats: list[str],
                   target_weeks: np.ndarray) -> np.ndarray:
    """逐周重训元模型,只为 target_weeks 输出 P(M0 出错)。第 t 周只用 < t 的样本。"""
    from sklearn.linear_model import LogisticRegression

    X = P[feats].to_numpy(float)
    y = P["meta_y"].to_numpy(float)
    wk = P["week_id"].to_numpy()
    ev = np.isfinite(y)
    uw = np.unique(wk)
    pos = {w: i for i, w in enumerate(uw)}
    q = np.full(len(P), np.nan)
    for w in target_weeks:
        if w not in pos or pos[w] < WARMUP_WEEKS:
            continue
        te = np.flatnonzero(wk == w)
        tr = np.flatnonzero((wk < w) & ev)
        if len(te) == 0 or len(tr) < MIN_HIST or len(np.unique(y[tr])) < 2:
            continue
        Xtr, Xte = X[tr], X[te]
        med = np.nan_to_num(np.nanmedian(Xtr, axis=0))
        Xtr = np.where(np.isnan(Xtr), med, Xtr)
        Xte = np.where(np.isnan(Xte), med, Xte)
        mu, sd = Xtr.mean(0), Xtr.std(0)
        sd[sd == 0] = 1.0
        clf = LogisticRegression(C=META_C, class_weight="balanced",
                                 solver="liblinear", max_iter=2000,
                                 random_state=RANDOM_STATE)
        clf.fit((Xtr - mu) / sd, y[tr].astype(int))
        q[te] = clf.predict_proba((Xte - mu) / sd)[:, 1]
    return q


# ================================================================ 翻转集 v5
def tail_mask_causal(P: pd.DataFrame, v: np.ndarray, pct: float, hi: bool,
                     win: int = ROLL_WIN) -> np.ndarray:
    """因果滚动分位尾部掩码:第 t 周阈值只用 [t−win, t−1] 的观测。"""
    wk = P["week_id"].to_numpy()
    uw = np.unique(wk)
    thr = np.full(len(P), np.nan)
    for i, w in enumerate(uw):
        lo = uw[max(0, i - win)]
        h = v[(wk < w) & (wk >= lo) & np.isfinite(v)]
        if len(h) >= MIN_HIST:
            thr[wk == w] = np.quantile(h, 1 - pct / 100 if hi else pct / 100)
    return (((v >= thr) if hi else (v <= thr)) & np.isfinite(thr) & np.isfinite(v))


def flip_mask_v5(P: pd.DataFrame) -> np.ndarray:
    absz = P["abs_z"].to_numpy(float)
    # ① 元模型 q
    m = tail_mask_causal(P, P["q"].to_numpy(float), TAIL_Q, True)
    # ② 曲线扭转
    piv = P.pivot_table(index="week_id", values="gap", columns="tenor",
                        aggfunc="first")
    if "1Y" in piv.columns and "10Y" in piv.columns:
        g1 = P["week_id"].map(piv["1Y"]).to_numpy(float)
        g10 = P["week_id"].map(piv["10Y"]).to_numpy(float)
        tw = (np.sign(g1) * np.sign(g10) < 0).astype(float) / (1.0 + absz)
        m |= tail_mask_causal(P, tw, TAIL_TWIST, True)
    # ③ 尾日反转 × 位置背离
    rev_flag = (np.sign(P["iw_last_mom"]) != np.sign(P["iw_slope"])).astype(float)
    pos_vs_gap = P["sg"] * (P["iw_pos"] - 0.5)
    m |= tail_mask_causal(P, (rev_flag * (0.5 - pos_vs_gap)).to_numpy(float),
                          TAIL_REV, True)
    # ④ 自适应信噪比
    ret = P.groupby("tenor", group_keys=False)["y_t"].diff()
    vol = ret.groupby(P["tenor"]).transform(
        lambda x: x.ewm(span=13, min_periods=8).std())
    S = (P["gap"].abs() / (vol + 1e-9)).to_numpy(float)
    bm = tail_mask_causal(P, S, S2N_TAIL, False)
    ey = P["meta_y"].to_numpy(float)
    wk = P["week_id"].to_numpy()
    uw = np.unique(wk)
    allow = np.zeros(len(P), bool)
    for i, w in enumerate(uw):
        lo = uw[max(0, i - S2N_K)]
        h = bm & (wk < w) & (wk >= lo) & np.isfinite(ey)
        if h.sum() >= S2N_MINHIST and float(ey[h].mean()) > S2N_THR:
            allow[wk == w] = True
    return m | (bm & allow)


# ================================================================== 单点决策
_CACHE: dict = {}


def decide(daily: pd.DataFrame, weekly: pd.DataFrame,
           daily_key: str, weekly_key: str) -> int:
    """返回 5Y 在 feature 周(= weekly_cutoff_key)的预测方向。"""
    ck = (daily_key, weekly_key)
    if ck in _CACHE:
        return _CACHE[ck]
    dtr, wtr = truncate(daily, weekly, daily_key, weekly_key)
    P = build_panel(dtr, wtr)
    if P.empty:
        raise SchemeError("panel empty after truncation")
    wid = int(weekly_key)
    if wid not in set(P["week_id"].to_numpy()):
        raise SchemeError(f"week {weekly_key} not usable after feature build")
    P, feats = meta_features(P)
    uw = np.unique(P["week_id"].to_numpy())
    idx = int(np.flatnonzero(uw == wid)[0])
    need = uw[max(0, idx - ROLL_WIN): idx + 1]
    P["q"] = walk_forward_q(P, feats, need)
    P["flip"] = flip_mask_v5(P)

    row = P[(P["week_id"] == wid) & (P["tenor"] == TARGET_TENOR)]
    if len(row) != 1:
        raise SchemeError(f"target row not unique for {weekly_key}/{TARGET_TENOR}")
    r = row.iloc[0]
    if not np.isfinite(r["gap"]):
        raise SchemeError(f"gap unavailable for {weekly_key}/{TARGET_TENOR}")
    base = 1 if float(r["gap"]) >= 0 else -1
    direction = -base if bool(r["flip"]) else base
    _CACHE[ck] = direction
    return direction


# ====================================================================== CLI
REQ_FIELDS = ["request_id", "predict_date", "feature_date", "target_date",
              "daily_cutoff_key", "weekly_cutoff_key", "monthly_cutoff_key"]
RES_FIELDS = ["request_id", "predict_date", "feature_date", "target_date",
              "predicted_direction"]


def _check_date(s: str, name: str) -> pd.Timestamp:
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise SchemeError(f"{name} must be canonical YYYY-MM-DD, got {s!r}")
    ts = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
    if pd.isna(ts):
        raise SchemeError(f"{name} is not a valid date: {s!r}")
    return ts


def validate_request(req: dict) -> dict:
    extra = set(req) - set(REQ_FIELDS)
    missing = set(REQ_FIELDS) - set(req)
    if extra:
        raise SchemeError(f"unexpected request fields: {sorted(extra)}")
    if missing:
        raise SchemeError(f"missing request fields: {sorted(missing)}")
    rid = req["request_id"]
    if not isinstance(rid, str) or not rid.strip():
        raise SchemeError("request_id must be a non-empty string")
    pd_, fd, td = (_check_date(req[k], k) for k in
                   ("predict_date", "feature_date", "target_date"))
    if not (fd <= pd_ <= td and fd < td):
        raise SchemeError("require feature_date <= predict_date <= target_date "
                          "and feature_date < target_date")
    _check_date(req["daily_cutoff_key"], "daily_cutoff_key")
    for k in ("weekly_cutoff_key", "monthly_cutoff_key"):
        v = str(req[k]).strip()
        if len(v) != 6 or not v.isdigit():
            raise SchemeError(f"{k} must be a six-digit string, got {req[k]!r}")
        req[k] = v
    return req


def atomic_write(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp)
    try:
        writer(tmp)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def cmd_predict(args) -> int:
    req = validate_request(json.loads(Path(args.request).read_text(encoding="utf-8")))
    daily, weekly = load_frames(Path(args.data_dir))
    d = decide(daily, weekly, req["daily_cutoff_key"], req["weekly_cutoff_key"])
    res = {k: req[k] for k in RES_FIELDS[:4]}
    res["predicted_direction"] = int(d)
    atomic_write(Path(args.output),
                 lambda p: p.write_text(json.dumps(res, ensure_ascii=False,
                                                   indent=2) + "\n",
                                        encoding="utf-8"))
    log(f"[{SCHEME_ID}] predict ok request_id={req['request_id']} direction={d}")
    return 0


def cmd_backtest(args) -> int:
    with Path(args.requests).open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SchemeError("requests.csv is empty")
    if len(rows) > 100:
        raise SchemeError(f"batch size {len(rows)} exceeds Contract 1.0 limit 100")
    reqs = [validate_request(dict(r)) for r in rows]
    ids = [r["request_id"] for r in reqs]
    if len(set(ids)) != len(ids):
        raise SchemeError("duplicate request_id within batch")
    daily, weekly = load_frames(Path(args.data_dir))
    out = []
    for r in reqs:
        d = decide(daily, weekly, r["daily_cutoff_key"], r["weekly_cutoff_key"])
        out.append({**{k: r[k] for k in RES_FIELDS[:4]},
                    "predicted_direction": int(d)})

    def _w(p: Path) -> None:
        with p.open("w", encoding="utf-8", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=RES_FIELDS)
            wr.writeheader()
            wr.writerows(out)
    atomic_write(Path(args.output), _w)
    log(f"[{SCHEME_ID}] backtest ok n={len(out)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog=f"{SCHEME_ID}.py",
        description=f"{SCHEME_ID}: {TARGET_TENOR} {TASK_TYPE} horizon={HORIZON}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("predict")
    p1.add_argument("--request", required=True)
    p1.add_argument("--data-dir", required=True)
    p1.add_argument("--output", required=True)
    p1.set_defaults(func=cmd_predict)
    p2 = sub.add_parser("backtest")
    p2.add_argument("--requests", required=True)
    p2.add_argument("--data-dir", required=True)
    p2.add_argument("--output", required=True)
    p2.set_defaults(func=cmd_backtest)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except SchemeError as e:
        log(f"[{SCHEME_ID}] ERROR {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        log(f"[{SCHEME_ID}] ERROR unexpected: {type(e).__name__}: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
