# 研究资料: 现有方案分析

**来源**: `model-mutitest-0529/codex_docs/repository_research.md`  
**日期**: 2026-05-29

---

## 1. 数据资产

两份日频数据（内容完全一致）:
- `t1/daily_output.csv`: 877列, 3844行, 2010/07/27 ~ 2026/05/28
- `t5/data/daily_output.csv`: 同上

数据来源: MySQL `bond_db` 的 `api_wind_indicators_all` / `api_wind_daily` / `api_wind_derivative_daily` 表，通过原始 `data_service.py` 重建宽表。

日频目标集合: `TB1YWI0C`(1Y), `TB3YWI0C`(3Y), `TB5YWI0C`(5Y), `TB7YWI0C`(7Y), `TB0YWI0C`(10Y)。原始 `data_service.py` 生成交易日锚定列为 `TB1YWI0C/TB5YWI0C/TB0YWI0C`；当前审计确认它与框架 shared 上游兼容版在历史窗口内生成结果完全一致。

---

## 2. t1 生产链路 (T+1)

**入口**: `python -m daily.run_daily [YYYY-MM-DD] [--dry-run]`

**配置**:

| frequency | tenor | close_col | window | min_child_samples | vote_scheme |
|-----------|-------|-----------|--------|-------------------|-------------|
| D1Y | 1Y | TB1YWI0C | 126 | 20 | legacy |
| D5Y | 5Y | TB5YWI0C | 126 | 40 | legacy |
| D10Y | 10Y | TB0YWI0C | 126 | 20 | legacy |

**建模逻辑**:
- 标签: `close.shift(-1) / close - 1` 的方向 (T+1)
- 模型: LightGBM LGBMClassifier 二分类
- 校准: 历史80/20切分，阈值在[0.38, 0.62]网格选择
- Fallback: 训练不足或概率贴近阈值时用`anti_lag20`
- 投票: `build_vote_signals` + `_combine_vote`

**输出**:
- 本地: JSON/模型/SHAP/audit文件
- 数据库: `t_pre_market_forecast`, `t_shap`
- 方向映射: `{0: "平", 1: "空", -1: "多"}` (收益率方向，与价格反向)

---

## 3. t5 复现链路 (T+5)

**入口**: `python t5/run_all.py`

**配置**:

| 脚本 | tenor | close_col | Horizon | Gap | Window | 投票信号 |
|------|-------|-----------|---------|-----|--------|----------|
| predict_3y.py | 3Y | TB3YWI0C | 5 | 5 | 550 | 3Y_z_anti180, 3Y_z_anti252, bf_z_anti120 |
| predict_5y.py | 5Y | TB5YWI0C | 5 | 5 | 756 | 5Y_z_anti90, 5Y_z_anti252, 5Y_ema_anti40, spr_5Y3Y_anti60, spr_5Y10Y_anti20 |
| predict_7y.py | 7Y | TB7YWI0C | 5 | 5 | 300 | 7Y_z_anti90, 7Y_z_highvol252, spr_7Y10Y_anti60, bf_z_anti40 |
| predict_10y.py | 10Y | TB0YWI0C | 5 | 5 | 240 | 10Y_z_anti90, 10Y_z_anti120, 10Y_z_anti252, 10Y_mom_sum120, bf7_z_anti60 |

**关键差异vs t1**:
- T+5预测，GAP=5排除最近5天避免标签重叠
- 各tenor独立训练参数和投票配方
- 训练窗口更大（240-756 vs 126）

**最新表现** (2025-01 ~ 2026-05):
- 7Y和10Y通过strict条件
- 3Y和5Y因2026-05最新段低于60%未通过

---

## 4. t1与t5关键不一致

| 维度 | t1 | t5 |
|------|----|----|
| 覆盖期限 | 1Y/5Y/10Y | 3Y/5Y/7Y/10Y |
| 标签跨度 | T+1 | T+5 |
| Gap | 无 | GAP=5 |
| 训练窗口 | 126 | 240-756 |
| 投票信号 | 按style硬编码 | 各tenor独立recipe |
| 输出 | JSON+DB | CSV+报告 |

---

## 5. 迁移注意事项

1. t1的`run_daily_predictions`需要改造为只返回预测结果，不写本地文件
2. t5的各`predict_*.py`需要从全量回测改为只预测最新一天
3. 当前 BFL adapter 统一通过 `shared.input_artifacts` 生成输入 CSV，再读回给算法；日频公共层内部动态加载原始 `schemes/_original_source/data_service.py`，不直接绕过 CSV 文件边界。
4. 数据库密码需迁移到环境变量
5. 方向映射需统一：1=涨(收益率上行/价格下跌), -1=跌(收益率下行/价格上涨)
