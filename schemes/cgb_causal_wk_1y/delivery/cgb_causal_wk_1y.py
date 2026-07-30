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
CALENDAR_FILE = "api_wind_date.csv"    # 可选覆盖文件, 见 §日历

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
# 冻结业务周日历
#   每条记录是 "week_id:该周起始自然日(周一)"; 相邻两条之间即该 week_id 的自然日跨度。
#   节假日整周无交易时上游把该周并入下一周(跨度 14 天), 因此不能用纯 ISO 周推算。
#   覆盖 201001(2010-01-04) ~ 202630(2026-07-27); 之后按同一规则运行期外推,
#   并用 Request 自带的 (daily_cutoff_key, weekly_cutoff_key) 做锚点校验。
# --------------------------------------------------------------------------- #
_CALENDAR_BLOB = """201001:20100104 201002:20100111 201003:20100118 201004:20100125 201005:20100201 201006:20100208 201007:20100215 201008:20100301
201009:20100308 201010:20100315 201011:20100322 201012:20100329 201013:20100405 201014:20100412 201015:20100419 201016:20100426
201017:20100503 201018:20100510 201019:20100517 201020:20100524 201021:20100531 201022:20100607 201023:20100614 201024:20100621
201025:20100628 201026:20100705 201027:20100712 201028:20100719 201029:20100726 201030:20100802 201031:20100809 201032:20100816
201033:20100823 201034:20100830 201035:20100906 201036:20100913 201037:20100920 201038:20100927 201039:20101004 201040:20101011
201041:20101018 201042:20101025 201043:20101101 201044:20101108 201045:20101115 201046:20101122 201047:20101129 201048:20101206
201049:20101213 201050:20101220 201051:20101227 201101:20110103 201102:20110110 201103:20110117 201104:20110124 201105:20110131
201106:20110207 201107:20110214 201108:20110221 201109:20110228 201110:20110307 201111:20110314 201112:20110321 201113:20110328
201114:20110404 201115:20110411 201116:20110418 201117:20110425 201118:20110502 201119:20110509 201120:20110516 201121:20110523
201122:20110530 201123:20110606 201124:20110613 201125:20110620 201126:20110627 201127:20110704 201128:20110711 201129:20110718
201130:20110725 201131:20110801 201132:20110808 201133:20110815 201134:20110822 201135:20110829 201136:20110905 201137:20110912
201138:20110919 201139:20110926 201140:20111003 201141:20111017 201142:20111024 201143:20111031 201144:20111107 201145:20111114
201146:20111121 201147:20111128 201148:20111205 201149:20111212 201150:20111219 201151:20111226 201201:20120102 201202:20120109
201203:20120116 201204:20120123 201205:20120206 201206:20120213 201207:20120220 201208:20120227 201209:20120305 201210:20120312
201211:20120319 201212:20120326 201213:20120402 201214:20120409 201215:20120416 201216:20120423 201217:20120430 201218:20120507
201219:20120514 201220:20120521 201221:20120528 201222:20120604 201223:20120611 201224:20120618 201225:20120625 201226:20120702
201227:20120709 201228:20120716 201229:20120723 201230:20120730 201231:20120806 201232:20120813 201233:20120820 201234:20120827
201235:20120903 201236:20120910 201237:20120917 201238:20120924 201239:20121001 201240:20121015 201241:20121022 201242:20121029
201243:20121105 201244:20121112 201245:20121119 201246:20121126 201247:20121203 201248:20121210 201249:20121217 201250:20121224
201301:20121231 201302:20130107 201303:20130114 201304:20130121 201305:20130128 201306:20130204 201307:20130211 201308:20130225
201309:20130304 201310:20130311 201311:20130318 201312:20130325 201313:20130401 201314:20130408 201315:20130415 201316:20130422
201317:20130429 201318:20130506 201319:20130513 201320:20130520 201321:20130527 201322:20130603 201323:20130610 201324:20130617
201325:20130624 201326:20130701 201327:20130708 201328:20130715 201329:20130722 201330:20130729 201331:20130805 201332:20130812
201333:20130819 201334:20130826 201335:20130902 201336:20130909 201337:20130916 201338:20130923 201339:20130930 201340:20131007
201341:20131014 201342:20131021 201343:20131028 201344:20131104 201345:20131111 201346:20131118 201347:20131125 201348:20131202
201349:20131209 201350:20131216 201351:20131223 201401:20131230 201402:20140106 201403:20140113 201404:20140120 201405:20140127
201406:20140203 201407:20140210 201408:20140217 201409:20140224 201410:20140303 201411:20140310 201412:20140317 201413:20140324
201414:20140331 201415:20140407 201416:20140414 201417:20140421 201418:20140428 201419:20140505 201420:20140512 201421:20140519
201422:20140526 201423:20140602 201424:20140609 201425:20140616 201426:20140623 201427:20140630 201428:20140707 201429:20140714
201430:20140721 201431:20140728 201432:20140804 201433:20140811 201434:20140818 201435:20140825 201436:20140901 201437:20140908
201438:20140915 201439:20140922 201440:20140929 201441:20141006 201442:20141013 201443:20141020 201444:20141027 201445:20141103
201446:20141110 201447:20141117 201448:20141124 201449:20141201 201450:20141208 201451:20141215 201452:20141222 201453:20141229
201501:20150105 201502:20150112 201503:20150119 201504:20150126 201505:20150202 201506:20150209 201507:20150216 201508:20150223
201509:20150302 201510:20150309 201511:20150316 201512:20150323 201513:20150330 201514:20150406 201515:20150413 201516:20150420
201517:20150427 201518:20150504 201519:20150511 201520:20150518 201521:20150525 201522:20150601 201523:20150608 201524:20150615
201525:20150622 201526:20150629 201527:20150706 201528:20150713 201529:20150720 201530:20150727 201531:20150803 201532:20150810
201533:20150817 201534:20150824 201535:20150831 201536:20150907 201537:20150914 201538:20150921 201539:20150928 201540:20151005
201541:20151012 201542:20151019 201543:20151026 201544:20151102 201545:20151109 201546:20151116 201547:20151123 201548:20151130
201549:20151207 201550:20151214 201551:20151221 201552:20151228 201601:20160104 201602:20160111 201603:20160118 201604:20160125
201605:20160201 201606:20160208 201607:20160222 201608:20160229 201609:20160307 201610:20160314 201611:20160321 201612:20160328
201613:20160404 201614:20160411 201615:20160418 201616:20160425 201617:20160502 201618:20160509 201619:20160516 201620:20160523
201621:20160530 201622:20160606 201623:20160613 201624:20160620 201625:20160627 201626:20160704 201627:20160711 201628:20160718
201629:20160725 201630:20160801 201631:20160808 201632:20160815 201633:20160822 201634:20160829 201635:20160905 201636:20160912
201637:20160919 201638:20160926 201639:20161003 201640:20161017 201641:20161024 201642:20161031 201643:20161107 201644:20161114
201645:20161121 201646:20161128 201647:20161205 201648:20161212 201649:20161219 201650:20161226 201701:20170102 201702:20170109
201703:20170116 201704:20170123 201705:20170130 201706:20170206 201707:20170213 201708:20170220 201709:20170227 201710:20170306
201711:20170313 201712:20170320 201713:20170327 201714:20170403 201715:20170410 201716:20170417 201717:20170424 201718:20170501
201719:20170508 201720:20170515 201721:20170522 201722:20170529 201723:20170605 201724:20170612 201725:20170619 201726:20170626
201727:20170703 201728:20170710 201729:20170717 201730:20170724 201731:20170731 201732:20170807 201733:20170814 201734:20170821
201735:20170828 201736:20170904 201737:20170911 201738:20170918 201739:20170925 201740:20171002 201741:20171016 201742:20171023
201743:20171030 201744:20171106 201745:20171113 201746:20171120 201747:20171127 201748:20171204 201749:20171211 201750:20171218
201751:20171225 201801:20180101 201802:20180108 201803:20180115 201804:20180122 201805:20180129 201806:20180205 201807:20180212
201808:20180219 201809:20180226 201810:20180305 201811:20180312 201812:20180319 201813:20180326 201814:20180402 201815:20180409
201816:20180416 201817:20180423 201818:20180430 201819:20180507 201820:20180514 201821:20180521 201822:20180528 201823:20180604
201824:20180611 201825:20180618 201826:20180625 201827:20180702 201828:20180709 201829:20180716 201830:20180723 201831:20180730
201832:20180806 201833:20180813 201834:20180820 201835:20180827 201836:20180903 201837:20180910 201838:20180917 201839:20180924
201840:20181001 201841:20181015 201842:20181022 201843:20181029 201844:20181105 201845:20181112 201846:20181119 201847:20181126
201848:20181203 201849:20181210 201850:20181217 201851:20181224 201901:20181231 201902:20190107 201903:20190114 201904:20190121
201905:20190128 201906:20190204 201907:20190218 201908:20190225 201909:20190304 201910:20190311 201911:20190318 201912:20190325
201913:20190401 201914:20190408 201915:20190415 201916:20190422 201917:20190429 201918:20190506 201919:20190513 201920:20190520
201921:20190527 201922:20190603 201923:20190610 201924:20190617 201925:20190624 201926:20190701 201927:20190708 201928:20190715
201929:20190722 201930:20190729 201931:20190805 201932:20190812 201933:20190819 201934:20190826 201935:20190902 201936:20190909
201937:20190916 201938:20190923 201939:20190930 201940:20191007 201941:20191014 201942:20191021 201943:20191028 201944:20191104
201945:20191111 201946:20191118 201947:20191125 201948:20191202 201949:20191209 201950:20191216 201951:20191223 202001:20191230
202002:20200106 202003:20200113 202004:20200120 202005:20200127 202006:20200210 202007:20200217 202008:20200224 202009:20200302
202010:20200309 202011:20200316 202012:20200323 202013:20200330 202014:20200406 202015:20200413 202016:20200420 202017:20200427
202018:20200504 202019:20200511 202020:20200518 202021:20200525 202022:20200601 202023:20200608 202024:20200615 202025:20200622
202026:20200629 202027:20200706 202028:20200713 202029:20200720 202030:20200727 202031:20200803 202032:20200810 202033:20200817
202034:20200824 202035:20200831 202036:20200907 202037:20200914 202038:20200921 202039:20200928 202040:20201005 202041:20201012
202042:20201019 202043:20201026 202044:20201102 202045:20201109 202046:20201116 202047:20201123 202048:20201130 202049:20201207
202050:20201214 202051:20201221 202052:20201228 202101:20210104 202102:20210111 202103:20210118 202104:20210125 202105:20210201
202106:20210208 202107:20210215 202108:20210222 202109:20210301 202110:20210308 202111:20210315 202112:20210322 202113:20210329
202114:20210405 202115:20210412 202116:20210419 202117:20210426 202118:20210503 202119:20210510 202120:20210517 202121:20210524
202122:20210531 202123:20210607 202124:20210614 202125:20210621 202126:20210628 202127:20210705 202128:20210712 202129:20210719
202130:20210726 202131:20210802 202132:20210809 202133:20210816 202134:20210823 202135:20210830 202136:20210906 202137:20210913
202138:20210920 202139:20210927 202140:20211004 202141:20211011 202142:20211018 202143:20211025 202144:20211101 202145:20211108
202146:20211115 202147:20211122 202148:20211129 202149:20211206 202150:20211213 202151:20211220 202152:20211227 202201:20220103
202202:20220110 202203:20220117 202204:20220124 202205:20220131 202206:20220214 202207:20220221 202208:20220228 202209:20220307
202210:20220314 202211:20220321 202212:20220328 202213:20220404 202214:20220411 202215:20220418 202216:20220425 202217:20220502
202218:20220509 202219:20220516 202220:20220523 202221:20220530 202222:20220606 202223:20220613 202224:20220620 202225:20220627
202226:20220704 202227:20220711 202228:20220718 202229:20220725 202230:20220801 202231:20220808 202232:20220815 202233:20220822
202234:20220829 202235:20220905 202236:20220912 202237:20220919 202238:20220926 202239:20221003 202240:20221017 202241:20221024
202242:20221031 202243:20221107 202244:20221114 202245:20221121 202246:20221128 202247:20221205 202248:20221212 202249:20221219
202250:20221226 202301:20230102 202302:20230109 202303:20230116 202304:20230123 202305:20230206 202306:20230213 202307:20230220
202308:20230227 202309:20230306 202310:20230313 202311:20230320 202312:20230327 202313:20230403 202314:20230410 202315:20230417
202316:20230424 202317:20230501 202318:20230508 202319:20230515 202320:20230522 202321:20230529 202322:20230605 202323:20230612
202324:20230619 202325:20230626 202326:20230703 202327:20230710 202328:20230717 202329:20230724 202330:20230731 202331:20230807
202332:20230814 202333:20230821 202334:20230828 202335:20230904 202336:20230911 202337:20230918 202338:20230925 202339:20231002
202340:20231016 202341:20231023 202342:20231030 202343:20231106 202344:20231113 202345:20231120 202346:20231127 202347:20231204
202348:20231211 202349:20231218 202350:20231225 202401:20240101 202402:20240108 202403:20240115 202404:20240122 202405:20240129
202406:20240205 202407:20240212 202408:20240226 202409:20240304 202410:20240311 202411:20240318 202412:20240325 202413:20240401
202414:20240408 202415:20240415 202416:20240422 202417:20240429 202418:20240506 202419:20240513 202420:20240520 202421:20240527
202422:20240603 202423:20240610 202424:20240617 202425:20240624 202426:20240701 202427:20240708 202428:20240715 202429:20240722
202430:20240729 202431:20240805 202432:20240812 202433:20240819 202434:20240826 202435:20240902 202436:20240909 202437:20240916
202438:20240923 202439:20240930 202440:20241007 202441:20241014 202442:20241021 202443:20241028 202444:20241104 202445:20241111
202446:20241118 202447:20241125 202448:20241202 202449:20241209 202450:20241216 202451:20241223 202501:20241230 202502:20250106
202503:20250113 202504:20250120 202505:20250127 202506:20250203 202507:20250210 202508:20250217 202509:20250224 202510:20250303
202511:20250310 202512:20250317 202513:20250324 202514:20250331 202515:20250407 202516:20250414 202517:20250421 202518:20250428
202519:20250505 202520:20250512 202521:20250519 202522:20250526 202523:20250602 202524:20250609 202525:20250616 202526:20250623
202527:20250630 202528:20250707 202529:20250714 202530:20250721 202531:20250728 202532:20250804 202533:20250811 202534:20250818
202535:20250825 202536:20250901 202537:20250908 202538:20250915 202539:20250922 202540:20250929 202541:20251006 202542:20251013
202543:20251020 202544:20251027 202545:20251103 202546:20251110 202547:20251117 202548:20251124 202549:20251201 202550:20251208
202551:20251215 202552:20251222 202553:20251229 202601:20260105 202602:20260112 202603:20260119 202604:20260126 202605:20260202
202606:20260209 202607:20260216 202608:20260302 202609:20260309 202610:20260316 202611:20260323 202612:20260330 202613:20260406
202614:20260413 202615:20260420 202616:20260427 202617:20260504 202618:20260511 202619:20260518 202620:20260525 202621:20260601
202622:20260608 202623:20260615 202624:20260622 202626:20260629 202627:20260706 202628:20260713 202629:20260720 202630:20260727"""


def _parse_frozen_calendar() -> List[Tuple[int, _date]]:
    out: List[Tuple[int, _date]] = []
    for token in _CALENDAR_BLOB.split():
        wid, _, start = token.partition(":")
        out.append((int(wid), _date(int(start[:4]), int(start[4:6]), int(start[6:8]))))
    if not out:
        raise SchemeError("内嵌业务周日历为空")
    return out


def _next_week_id(prev_week_id: int, prev_start: _date, new_start: _date) -> int:
    """业务周编号规则: 年份取该周周三所在年, 年内自 01 起密集编号。"""
    prev_year = (prev_start + _timedelta(days=2)).year
    new_year = (new_start + _timedelta(days=2)).year
    if new_year != prev_year:
        return new_year * 100 + 1
    return prev_week_id + 1


def build_week_calendar(
    trading_dates: Sequence[_date],
    needed_through: _date,
    override: Optional[List[Tuple[int, _date]]] = None,
) -> List[Tuple[int, _date]]:
    """返回 [(week_id, week_start)] 升序, 覆盖到 needed_through 所在周。

    override 非空时直接使用(来自 <data-dir>/api_wind_date.csv), 否则用内嵌日历,
    并按业务周规则向后外推: 整周无交易日的周并入下一周, 不单独占用编号。
    """
    calendar = list(override) if override else _parse_frozen_calendar()
    trading = set(trading_dates)
    while True:
        last_id, last_start = calendar[-1]
        if needed_through < last_start + _timedelta(days=7):
            break
        candidate_start = last_start + _timedelta(days=7)
        # 整周无交易日 -> 该周并入下一周(不占编号), 等价于把上一周跨度延长 7 天
        probe = candidate_start
        while probe + _timedelta(days=6) <= needed_through and not any(
            (probe + _timedelta(days=offset)) in trading for offset in range(7)
        ):
            candidate_start = probe + _timedelta(days=7)
            probe = candidate_start
        calendar.append((_next_week_id(last_id, last_start, candidate_start), candidate_start))
    return calendar


def calendar_lookup(calendar: Sequence[Tuple[int, _date]]) -> Tuple[Dict[int, _date], Dict[int, _date]]:
    """week_id -> (week_start, week_end)。末周按 7 天补齐。"""
    starts: Dict[int, _date] = {}
    ends: Dict[int, _date] = {}
    for index, (week_id, start) in enumerate(calendar):
        starts[week_id] = start
        if index + 1 < len(calendar):
            ends[week_id] = calendar[index + 1][1] - _timedelta(days=1)
        else:
            ends[week_id] = start + _timedelta(days=6)
    return starts, ends


def assign_week_ids(dates: pd.Series, calendar: Sequence[Tuple[int, _date]]) -> pd.Series:
    """把自然日映射到业务周 week_id; 日历未覆盖的日期返回 NA。"""
    bounds = pd.DatetimeIndex([pd.Timestamp(start) for _, start in calendar])
    ids = np.array([week_id for week_id, _ in calendar], dtype="int64")
    positions = np.searchsorted(bounds.values, dates.values, side="right") - 1
    out = np.full(len(dates), -1, dtype="int64")
    valid = positions >= 0
    out[valid] = ids[positions[valid]]
    last_end = bounds[-1] + pd.Timedelta(days=6)
    out[dates.values > last_end.to_datetime64()] = -1
    result = pd.Series(out, index=dates.index, dtype="int64")
    return result.mask(result.eq(-1))


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

    override = None
    calendar_path = data_dir / CALENDAR_FILE
    if calendar_path.exists():
        override = _read_calendar_override(calendar_path)
        _log(f"使用 {calendar_path} 覆盖内嵌业务周日历 ({len(override)} 周)")

    return {"weekly": weekly, "daily": daily, "calendar_override": override}


def _read_calendar_override(path: Path) -> List[Tuple[int, _date]]:
    frame = pd.read_csv(path, usecols=["rdate", "week_id"], encoding="utf-8-sig")
    frame["rdate"] = pd.to_datetime(frame["rdate"], errors="coerce")
    frame["week_id"] = pd.to_numeric(frame["week_id"], errors="coerce")
    frame = frame.dropna(subset=["rdate", "week_id"])
    if frame.empty:
        raise SchemeError(f"{path} 没有可用的 rdate/week_id 记录")
    frame["week_id"] = frame["week_id"].astype(int)
    grouped = frame.groupby("week_id")["rdate"].min().sort_index()
    calendar = [(int(week_id), start.date()) for week_id, start in grouped.items()]
    # 丢弃跨度异常(<7 天)的首尾残缺周, 避免污染自然日 -> week_id 的分段
    cleaned: List[Tuple[int, _date]] = []
    for index, (week_id, start) in enumerate(calendar):
        if index + 1 < len(calendar) and (calendar[index + 1][1] - start).days < 7:
            continue
        cleaned.append((week_id, start))
    return cleaned


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

    calendar = build_week_calendar(
        [ts.date() for ts in pd.DatetimeIndex(daily_cut["date"])],
        daily_cutoff.date(),
        frames["calendar_override"],
    )
    starts, _ends = calendar_lookup(calendar)
    if weekly_cutoff not in starts:
        raise SchemeError(
            f"[{request_id}] 业务周日历未覆盖 weekly_cutoff_key={weekly_cutoff}; "
            f"内嵌日历覆盖至 {max(starts)}。请按交接文档 §日历 刷新日历常量或在 --data-dir 提供 {CALENDAR_FILE}"
        )
    daily_week = assign_week_ids(daily_cut["date"], calendar)
    anchor = daily_week.iloc[-1]
    if pd.isna(anchor) or int(anchor) != weekly_cutoff:
        raise SchemeError(
            f"[{request_id}] 业务周日历与 Request 锚点不一致: daily_cutoff_key={request['daily_cutoff_key']} "
            f"落在 week_id={None if pd.isna(anchor) else int(anchor)}, 但 weekly_cutoff_key={weekly_cutoff}。"
            f"请按交接文档 §日历 刷新日历常量或在 --data-dir 提供 {CALENDAR_FILE}"
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
