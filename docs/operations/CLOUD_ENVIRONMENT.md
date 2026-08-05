# 云服务器环境部署（第一步）

**文档状态**：`HISTORICAL`

**目标读者**：平台运维和环境复刻人员

**最后核验日期**：2026-06-28

> 本文仅保存云端环境复刻的第一步记录，不代表当前 Mac Studio 完整部署流程。

**更新日期**: 2026-06-28

本文只覆盖云服务器的 Conda 双环境复刻和烟测，不包含 MySQL 数据迁移、systemd 托管、正式 scheduler 启动或任何写库类操作。

## 环境快照文件

| 文件 | 用途 |
|------|------|
| `environment-service.from-history.yml` | 原 Mac 服务环境的 Conda 显式历史导出，保留本机 `prefix` 作为事实快照。 |
| `requirements-service.freeze.txt` | 原 Mac 服务环境的 `pip freeze` 原始导出。 |
| `environment-algo.from-history.yml` | 原 Mac 算法环境的 Conda 显式历史导出，保留本机 `prefix` 作为事实快照。 |
| `requirements-algo.freeze.txt` | 原 Mac 算法环境的 `pip freeze` 原始导出。 |
| `environment-service.cloud.yml` | 云端创建服务环境用，等价于去掉本机 `prefix` 的 Conda 基础环境。 |
| `requirements-service.freeze.installable.txt` | 云端安装服务环境 Python 包用，基于该 `pip list --format=freeze --exclude pip` 输出维护的当前可安装服务包清单。 |
| `environment-algo.cloud.yml` | 云端创建算法环境用，等价于去掉本机 `prefix` 的 Conda 基础环境。 |
| `requirements-algo.freeze.installable.txt` | 云端安装算法环境 Python 包用，由 `pip list --format=freeze --exclude pip` 生成。 |

`requirements-algo.freeze.txt` 是原始事实导出，其中 `packaging` 带有本机 Conda build 的 `file://` 来源；云服务器不能直接安装这条本机路径。因此云端安装使用 `requirements-algo.freeze.installable.txt`，其中 `packaging==25.0` 来自同一环境的安装元数据，不是人工猜测。

`requirements-service.freeze.txt` 同样只保留原始历史事实，可能继续含有当前服务已退役的依赖；
G8.11 退役的 `APScheduler` 与其唯一依赖 `tzlocal` 即保留在该 raw snapshot 中。当前服务安装应只使用
`requirements-service.freeze.installable.txt`，其中已排除这一对退役依赖。

## 云端创建环境

以下命令假设仓库已经放在云服务器，并且当前目录是仓库根目录。

```bash
conda env create -f environment-service.cloud.yml
conda run -n bond_factor_lab_service python -m pip install -r requirements-service.freeze.installable.txt

conda env create -f environment-algo.cloud.yml
conda run -n forecast_env python -m pip install -r requirements-algo.freeze.installable.txt
```

Ubuntu / Debian 服务器建议先安装基础运行库，尤其 LightGBM / XGBoost 常用的 OpenMP 运行库：

```bash
sudo apt-get update
sudo apt-get install -y build-essential libgomp1 graphviz
```

如果服务器不是 Linux x86_64，先不要直接进入后续写库或调度步骤；需要重新确认 `lightgbm`、`scikit-learn`、`catboost`、`xgboost`、`shap` 是否都有对应平台 wheel，并用 benchmark / harness 验证算法结果没有漂移。

## 环境变量

仓库根目录 `.env` 至少需要配置：

```bash
BOND_DB_HOST=127.0.0.1
BOND_DB_PORT=3306
BOND_DB_USER=...
BOND_DB_PASSWORD=...
BOND_DB_NAME=bond_db
BOND_DB_CHARSET=utf8mb4
BOND_ALGO_CONDA_ENV=forecast_env
```

对外开放 API 或 admin/trigger 接口前，建议额外配置：

```bash
BOND_ADMIN_TOKEN=...
BOND_CORS_ORIGINS=https://your-allowed-origin.example
```

进程管理器中继续显式设置 `PYTHONNOUSERSITE=1`，避免云服务器用户级 site-packages 污染复刻环境。

## 无数据库烟测

先验证两个环境的关键包版本：

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -c "import sys, fastapi, sqlalchemy, pandas, numpy, sklearn, lightgbm; from importlib.metadata import version; print(sys.version.split()[0]); print('fastapi', fastapi.__version__); print('sqlalchemy', sqlalchemy.__version__); print('pandas', pandas.__version__); print('numpy', numpy.__version__); print('sklearn', sklearn.__version__); print('lightgbm', lightgbm.__version__); print('PyMySQL', version('PyMySQL'))"

PYTHONNOUSERSITE=1 conda run -n forecast_env python -c "import sys, pandas, numpy, sklearn, lightgbm, scipy, xgboost, shap, catboost; from importlib.metadata import version; print(sys.version.split()[0]); print('pandas', pandas.__version__); print('numpy', numpy.__version__); print('sklearn', sklearn.__version__); print('lightgbm', lightgbm.__version__); print('scipy', scipy.__version__); print('xgboost', xgboost.__version__); print('shap', shap.__version__); print('catboost', catboost.__version__); print('PyMySQL', version('PyMySQL'))"
```

再验证服务环境能读取方案配置：

```bash
PYTHONNOUSERSITE=1 conda run -n bond_factor_lab_service python -c "from scheduler.discovery import discover_schemes; schemes = discover_schemes(); print(len(schemes)); print([s.scheme_id for s in schemes[:5]])"
```

## 数据库就绪后的只读烟测

数据库和源数据恢复后，先跑只读 dry-run，不启动 scheduler，不执行 live 写库：

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id t1_daily --predict-date 2026-06-11
```

如果 dry-run 通过，再临时启动后端验证 API：

```bash
PYTHONNOUSERSITE=1 BOND_ALGO_CONDA_ENV=forecast_env \
  conda run --no-capture-output -n bond_factor_lab_service \
  uvicorn backend.main:app --host 127.0.0.1 --port 8100
```

另开一个 shell 验证：

```bash
curl -sS http://127.0.0.1:8100/api/health
curl -sS http://127.0.0.1:8100/api/schemes
curl -sS http://127.0.0.1:8100/api/backtests/factor-lab
```

只有以上环境、DB、只读 API 验证通过后，才进入 systemd 托管、scheduler 挂载、灰度补齐或任何授权写库流程。
