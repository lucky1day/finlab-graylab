# Bond Factor Lab — 项目规范

## 项目定位

独立的国债因子实盘测试平台，前端通过iframe嵌入panda_quantflow的AIFin Lab Shell。

## 技术栈

- **后端**: Python 3.12 + FastAPI + SQLAlchemy + APScheduler
- **前端**: 原生HTML/CSS/JS（从panda_quantflow提取的因子实验室页面）
- **数据库**: MySQL 8.0 (bond_db)
- **部署**: Mac Studio, launchd管理进程

## 目录结构约定

```
bond-factor-lab/
├── schemes/           # 方案目录（约定式发现）
│   └── {scheme_id}/
│       ├── config.yaml
│       ├── predict.py  # 必须暴露 run(predict_date: str) -> list[PredictionRecord]
│       └── core/       # 方案核心逻辑
├── scheduler/         # APScheduler调度器
├── backend/           # FastAPI后端
├── frontend/          # 原生HTML/CSS/JS因子实验室页面
├── shared/            # 共享代码（DB配置、数据服务）
├── migrations/        # SQL迁移脚本
└── docs/              # 项目文档
```

## 方案接口规范

每个方案必须在`predict.py`中暴露：

```python
def run(predict_date: str) -> list[PredictionRecord]:
    """
    Args:
        predict_date: 预测发出日期，格式 YYYY-MM-DD
    Returns:
        预测记录列表，每个tenor一条记录
    """
```

## 数据库表

- `t_scheme_predictions` — 统一预测结果表
- `t_scheme_actuals` — 实际方向表
- `t_scheme_registry` — 方案注册表
- `t_scheme_run_log` — 运行日志表

## 编码规范

- Python: 遵循PEP 8, type hints, docstring用中文
- 前端: 原生JS，无构建步骤，直接由FastAPI serve
- 数据库字段: snake_case
- API路径: kebab-case

## 关键设计决策

1. 所有方案预测结果写入同一张MySQL表，通过scheme_id隔离
2. 准确率指标由后端实时计算（JOIN predictions和actuals表）
3. 方案通过约定式目录结构自动发现，新增方案无需改动框架代码
4. 前端构建为静态文件，由FastAPI serve
