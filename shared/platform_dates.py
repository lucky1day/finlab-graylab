"""平台级日期与默认值的唯一定义；其它模块只导入，不再重复写字面量。"""

from __future__ import annotations

from datetime import date

# 特征基准日早于此日为回测分区，其余为实盘分区（数据库 result_segment 的 SQL 与之同值，由测试锁定）。
LIVE_START_DATE = date(2026, 6, 1)
LIVE_START_ISO = LIVE_START_DATE.isoformat()

# 历史回测、周期均值 Actual 与展示样本共同的起点。
BACKTEST_START_ISO = "2025-01-01"

# Blackbox 配置未声明 factor_input_mode 时的取值（166 个配置里有 66 个依赖该默认）。
DEFAULT_FACTOR_INPUT_MODE = "legacy_v1"
