# 方案迁移方案: t1/t5 → bond-factor-lab

**原则**: 不修改核心预测逻辑，只改输入数据来源和输出目标。

算法隔离边界:
- `core/` 目录保留原始算法、特征工程、模型训练和投票逻辑。
- `predict.py` adapter 只负责准备输入数据、调用 core、转换输出为 `PredictionRecord`。
- 数据库写入由框架层统一完成，不让算法 core 直接写 `t_scheme_*` 表。
- 缺失因子/预测因子数据由人工补充，adapter 不用临时规则伪造或污染算法输入。
- 后端/API/调度器运行在 `bond_factor_lab_service`，调度执行方案时通过子进程调用 `forecast_env`，避免服务依赖和算法依赖互相污染。
- `t5_daily` 已在数据补齐后恢复为 `active`；adapter 位于框架侧，核心算法目录保持不改动。

---

## 1. 当前代码位置

原始代码曾复制到 `_original_source` 作为过渡区；当前已完成框架化迁移，运行代码位于:
```
bond-factor-lab/
├── shared/data_service.py
├── shared/input_artifacts.py
├── schemes/t1_daily/
├── schemes/t5_daily/
├── schemes/weekly_10y_d_overlay/
└── benchmarks/model_muti_0529/daily_output.csv
```

---

## 2. 目标目录结构

```
bond-factor-lab/
├── shared/
│   ├── __init__.py
│   ├── db_config.py          # 环境变量化的DB配置
│   ├── data_service.py       # 日频 data service，改用 shared.db_config
│   └── models.py             # PredictionRecord 数据类
├── schemes/
│   ├── t1_daily/
│   │   ├── config.yaml
│   │   ├── predict.py        # adapter: 调用core逻辑，返回PredictionRecord
│   │   └── core/             # 从t1/daily_project/src/daily/ 整体复制
│   │       ├── __init__.py
│   │       ├── config.py     # 不动
│   │       ├── lgbm_predictor.py  # 不动
│   │       ├── feature_engineering.py  # 不动
│   │       ├── model_store.py     # 不动
│   │       └── shap_analysis.py   # 不动
│   └── t5_daily/
│       ├── config.yaml
│       ├── predict.py        # adapter: 调用core逻辑，返回PredictionRecord
│       └── core/             # 从t5/ 整体复制
│           ├── __init__.py
│           ├── common_utils.py    # 不动
│           ├── predict_3y.py      # 不动
│           ├── predict_5y.py      # 不动
│           ├── predict_7y.py      # 不动
│           └── predict_10y.py     # 不动
└── benchmarks/model_muti_0529/ # 历史复现 canonical CSV
```

---

## 3. 需要修改的文件（仅I/O层）

### 3.1 shared/db_config.py（新建）

```python
"""数据库配置 — 从环境变量读取，不再硬编码密码"""
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class DatabaseConfig:
    user: str = os.getenv("BOND_DB_USER", "root")
    password: str = os.getenv("BOND_DB_PASSWORD", "")
    host: str = os.getenv("BOND_DB_HOST", "localhost")
    port: int = int(os.getenv("BOND_DB_PORT", "3306"))
    database: str = os.getenv("BOND_DB_NAME", "bond_db")
    charset: str = "utf8mb4"
```

### 3.2 shared/input_artifacts.py 公共输入文件层

历史设计: `shared/data_service.py` 从原始 data_service 复制并改为环境变量化 DB 配置。

当前修正（2026-06-07）: live adapter 和历史复现 upstream 分支不直接拼接 DB 输入，也不自行决定输入文件路径，而是统一通过 `shared.input_artifacts` 调用对应 data service 生成输入 CSV，再从 CSV 读回给算法。日频公共层内部调用 `shared.data_service`；周频公共层内部调用 `weekly_data_service` 的 `wind_export(1)` 口径。旧 `_original_source` 运行依赖已移除。

### 3.3 shared/models.py（新建）

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class PredictionRecord:
    scheme_id: str
    target_tenor: str        # "1Y", "3Y", "5Y", "7Y", "10Y"
    horizon: int             # 1 or 5
    predict_date: str        # YYYY-MM-DD
    target_date: str         # YYYY-MM-DD
    predicted_direction: int # 1=涨, -1=跌, 0=平
    confidence: Optional[float] = None
    model_version: Optional[str] = None
    extra: Optional[dict] = None
```

### 3.4 schemes/t1_daily/predict.py（新建adapter）

```python
"""T+1 方案 adapter — 只负责I/O转换，不修改核心逻辑"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

# 将core加入path
sys.path.insert(0, str(Path(__file__).parent))

from shared.input_artifacts import build_daily_input_artifact
from shared.models import PredictionRecord
from core.config import TENOR_CONFIGS
from core.lgbm_predictor import predict_latest_for_config

SCHEME_ID = "t1_daily"
HORIZON = 1

def run(predict_date: str) -> list[PredictionRecord]:
    """
    执行T+1预测。
    
    输入: predict_date (YYYY-MM-DD)
    输出: 每个tenor一条PredictionRecord
    
    核心逻辑完全不动，只是:
    1. 用公共输入文件层生成 daily_output CSV，再读回给算法
    2. 调用原始predict_latest_for_config（不动）
    3. 将PredictionResult转换为统一的PredictionRecord（替代原来的write_db）
    """
    end_date = predict_date
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=7*365)).strftime("%Y-%m-%d")
    
    # 输入: 从DB获取数据（和原来一样）
    input_artifact = build_daily_input_artifact(
        scheme_id=SCHEME_ID,
        predict_date=predict_date,
        start_date=start_date,
        end_date=end_date,
    )
    daily_df = input_artifact.dataframe
    
    # 核心预测（完全不动）
    records = []
    for frequency, config in TENOR_CONFIGS.items():
        result = predict_latest_for_config(daily_df, config, current_date=predict_date)
        
        # 输出: 转换为统一格式（替代原来的write_prediction_results）
        records.append(PredictionRecord(
            scheme_id=SCHEME_ID,
            target_tenor=config.tenor,      # "1Y", "5Y", "10Y"
            horizon=HORIZON,
            predict_date=result.rdate,
            target_date=result.target_date,
            predicted_direction=result.pred_label,  # -1/0/1
            confidence=result.prob_up,
            model_version=f"lgbm_w{config.window}",
            extra={
                "base_pred": result.base_pred,
                "vote_sum": result.vote_sum,
                "threshold": result.threshold_used,
                "frequency": result.frequency,
            }
        ))
    
    return records
```

### 3.5 schemes/t5_daily/predict.py（新建adapter）

```python
"""T+5 方案 adapter — 只负责I/O转换，不修改核心逻辑"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from shared.input_artifacts import build_daily_input_artifact
from shared.models import PredictionRecord

SCHEME_ID = "t5_daily"
HORIZON = 5

# 各tenor的配置（从原始predict_*.py提取，不改参数）
TENOR_CONFIGS = {
    "3Y": {"close_col": "TB3YWI0C", "script": "core.predict_3y"},
    "5Y": {"close_col": "TB5YWI0C", "script": "core.predict_5y"},
    "7Y": {"close_col": "TB7YWI0C", "script": "core.predict_7y"},
    "10Y": {"close_col": "TB0YWI0C", "script": "core.predict_10y"},
}

def run(predict_date: str) -> list[PredictionRecord]:
    """
    执行T+5预测。
    
    核心逻辑不动，只改:
    1. 数据来源: 用公共输入文件层生成 daily_output CSV，再读回
    2. 只预测最新一天（替代原来的全量回测）
    3. 输出格式: 返回PredictionRecord（替代原来的to_csv）
    
    注意: 原始predict_*.py是全量回测模式（遍历所有test日期）。
    adapter通过schemes/t5_daily/latest_prediction.py在框架侧复用其特征、
    标签、训练、阈值和投票逻辑，只取最新可用特征日。
    core目录保持不改动。
    """
    end_date = predict_date
    start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=10*365)).strftime("%Y-%m-%d")
    
    # 输入: 从公共层生成 daily_output CSV，再读回给算法
    input_artifact = build_daily_input_artifact(
        scheme_id=SCHEME_ID,
        predict_date=predict_date,
        start_date=start_date,
        end_date=end_date,
    )
    daily_df = input_artifact.dataframe
    
    # 保存为临时DataFrame供core脚本使用
    # （core脚本的read_daily()期望CSV格式的DataFrame，这里直接传入即可）
    
    records = []
    for tenor, cfg in TENOR_CONFIGS.items():
        # 调用框架侧latest_prediction.py包装的预测逻辑
        result = predict_latest_for_module(TENOR_MODULES[tenor], daily_df, predict_date)
        
        records.append(PredictionRecord(
            scheme_id=SCHEME_ID,
            target_tenor=tenor,
            horizon=HORIZON,
            predict_date=predict_date,
            target_date=_target_date_from_feature_date(result.feature_date, HORIZON),
            predicted_direction=result.vote_pred,
            confidence=result.confidence,
            model_version=f"lgbm_t5_{tenor.lower()}",
            extra={
                "feature_date": result.feature_date,
                "model_pred": result.model_pred,
                "vote_signals": result.vote_signals,
            }
        ))
    
    return records
```

---

## 4. t5 latest_prediction.py框架侧提取

原始`predict_*.py`的main()是一个完整的滚动回测循环。为了只预测最新一天，当前实现没有修改 core 文件，而是在 `schemes/t5_daily/latest_prediction.py` 中复用原始模块暴露的常量和工具函数，抽取单日预测流程:

```python
def predict_latest_for_module(module, df: pd.DataFrame, predict_date: str) -> LatestPrediction:
    """
    只预测最新可用特征日。复用原始特征构建、标签、LightGBM、
    阈值选择和自定义投票信号逻辑。
    """
    # ... 选取 predict_date 之前最近可用 feature_date
    # ... 复用原始训练窗口和 GAP=5 排除逻辑
    # ... 返回 LatestPrediction(feature_date, vote_pred, model_pred, confidence, ...)
```

**关键**: core目录保持零改动；`latest_prediction.py` 只承担“把原始回测循环收束成最新一天预测”的 adapter 职责。

---

## 5. 不需要修改的文件（核心逻辑）

| 文件 | 说明 |
|------|------|
| `t1/core/lgbm_predictor.py` | 模型训练、预测、阈值校准、投票 |
| `t1/core/feature_engineering.py` | 特征构建、投票信号 |
| `t1/core/config.py` | tenor配置（D1Y/D5Y/D10Y参数） |
| `t1/core/shap_analysis.py` | SHAP解释 |
| `t5/core/common_utils.py` | 标签、特征、投票工具函数 |
| `t5/core/predict_3y.py` (main部分) | 3Y滚动训练+投票 |
| `t5/core/predict_5y.py` (main部分) | 5Y滚动训练+投票 |
| `t5/core/predict_7y.py` (main部分) | 7Y滚动训练+投票 |
| `t5/core/predict_10y.py` (main部分) | 10Y滚动训练+投票 |

---

## 6. 修改汇总

| 修改类型 | 文件 | 改动内容 |
|----------|------|----------|
| 新建 | `shared/db_config.py` | 环境变量化DB配置 |
| 新建 | `shared/input_artifacts.py` | 所有预测方案统一输入文件生成入口 |
| 删除 | `shared/original_daily_data_service.py` | 旧 `_original_source` 动态桥接已移除 |
| 微调 | `shared/data_service.py` | 环境变量化 DB 配置，作为日频 data service |
| 新建 | `shared/models.py` | PredictionRecord数据类 |
| 新建 | `schemes/t1_daily/predict.py` | adapter（~40行） |
| 新建 | `schemes/t1_daily/config.yaml` | 方案元数据 |
| 新建 | `schemes/t5_daily/predict.py` | adapter |
| 新建 | `schemes/t5_daily/latest_prediction.py` | 框架侧最新预测提取，不改 core |
| 新建 | `schemes/t5_daily/config.yaml` | 方案元数据 |

**总改动量**: 以 adapter / shared / scheduler / backend / frontend 为主
**核心逻辑改动**: 0行

---

## 7. 在Mac Studio上的执行步骤

1. 将`bond-factor-lab/`整体复制到Mac Studio
2. 创建`.env`文件填入DB密码
3. 算法预测使用现有conda环境: `conda activate forecast_env`
4. `forecast_env` 已验证具备算法核心依赖；如后续缺包，再按需安装
5. 运行migration SQL创建新表
6. 按上述方案创建shared/和schemes/目录结构
7. 手动测试: `conda run -n forecast_env python -c "from schemes.t1_daily.predict import run; print(run('2026-06-01'))"`
8. 当前测试口径数据补齐后，已进行 t1/t5 手动链路验证；后续补充新因子/预测因子时继续复核一致性
9. 后端/API/调度器使用独立环境: `conda activate bond_factor_lab_service`
10. 确认预测结果正确后，启动scheduler
