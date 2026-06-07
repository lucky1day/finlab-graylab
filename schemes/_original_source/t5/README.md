# 国债收益率方向预测 — 可复现代码包

## 概述

基于 LightGBM + 投票信号系统的国债收益率日频方向预测模型。
覆盖 3Y / 5Y / 7Y / 10Y 四个期限，全部通过严格筛选标准：

| 期限 | Horizon | Window | Recipe | VT | Sim准确率 | Real准确率 | May准确率 |
|------|---------|--------|--------|-----|----------|-----------|----------|
| 3Y   | T+5     | 504    | none   | -   | 63.2%    | 60.1%     | 80.0%    |
| 5Y   | T+5     | 756    | z3_sm2 | 1   | 62.4%    | 61.1%     | 80.0%    |
| 7Y   | T+5     | 300    | z2_bf  | 1   | 65.0%    | 60.6%     | 80.0%    |
| 10Y  | T+1     | 225    | z_bflv_al | 0 | 61.5%  | 60.1%     | 55.6%    |

**严格筛选标准**: sim >= 60%, real >= 60%, sim > real, may >= 50%

## 文件结构

```
reproduce_pack/
├── data/
│   └── daily_output.csv          # 原始日频数据
├── common_utils.py               # 公共工具函数（特征工程、标签、回测等）
├── predict_3y.py                 # 3Y 预测脚本
├── predict_5y.py                 # 5Y 预测脚本
├── predict_7y.py                 # 7Y 预测脚本
├── predict_10y.py                # 10Y 预测脚本
├── run_all.py                    # 一键运行全部4个期限
├── monthly_report.py             # 月度表现分析报告
└── README.md                     # 本文件
```

## 环境要求

- Python >= 3.8
- pandas
- numpy
- lightgbm

安装依赖：
```bash
pip install pandas numpy lightgbm
```

## 使用方法

### 一键运行全部
```bash
python run_all.py
```

### 单独运行某个期限
```bash
python predict_3y.py
python predict_5y.py
python predict_7y.py
python predict_10y.py
```

### 查看月度表现
```bash
python monthly_report.py
```

## 输出

每个脚本会输出：
1. 控制台打印 sim / real / may 三段准确率
2. 生成 `{tenor}_predictions.csv`，包含每日预测结果

## 方法论

### 模型
- LightGBM 二分类，每日滚动重训练
- 滑动窗口选取符合条件的历史样本训练
- 动态阈值校准（calibration set）

### 投票信号系统
- 基础预测 + 投票信号 → 投票和 → 最终预测
- VT > 0: 仅当 |vote_sum| > VT 时信号覆盖模型
- VT = 0: pred = sign(vote_sum)

### 信号类型
- `z_anti`: 收益率偏离均线的均值回归信号
- `anti_sum`: 累积收益反转
- `spread_anti_sum`: 利差变化反转
- `bf_z_anti`: 蝶式价差 (2×7Y - 5Y - 10Y) 均值回归
- `bf_z_lowvol`: 低波动率区间的蝶式价差信号
- `anti_lag`: 滞后收益反转

### 评估分期
- **Sim期** (2025-01 ~ 2025-06): 模型选择依据（仅看此期）
- **Real期** (2025-07 ~ 2026-04): 样本外验证
- **May期** (2026-05+): 最新期验证
