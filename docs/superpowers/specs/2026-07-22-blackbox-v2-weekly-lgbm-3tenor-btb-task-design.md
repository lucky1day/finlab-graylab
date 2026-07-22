# Blackbox V2 三期限算法改造 BTB 任务设计

**日期**：2026-07-22

**状态**：已确认，待实现

**输入算法**：`weekly_lgbm_predict_3tenors.py`
**目标**：构建一套与 `btb-433c9e49-2` 同类的独立 agent benchmark 任务，并交付 1Y、5Y、10Y 三个 Blackbox V2 方案的六个参考文件。

## 1. 目标与边界

本任务把一个同时预测 1Y、5Y、10Y 的多目标周频 LightGBM 脚本，改造成三个独立的 Blackbox V2 Contract 1.0 方案。一个方案只对应一个 `target_tenor + task_type + horizon` 组合，因此最终必须交付三对同名 `.py + .json`，共六个文件。

任务覆盖离线技术入库过程：

```text
Intake
→ static
→ input
→ unit
→ dry-run
→ compare
→ backtest
→ api-readiness
→ shadow + paused
```

任务不连接真实 MySQL、DataBridge 服务或现有 Bond Factor Lab 运行目录，不执行真实回测落库、Activation、gray live、scheduled live、Registry 更新、scheduler 变更或前端变更。所有状态和副作用检查都在任务容器的临时目录中模拟，任务结束后销毁。

任务包和参考产物放在仓库外的独立目录：

```text
/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/
```

这样不会把任务输入、生成 benchmark 或参考交付物放入现有 `schemes/`、`outputs/` 或灰度实验室运行链路。

## 2. 已知输入事实

源脚本 SHA-256：

```text
950fedc4b3bcd25ef6dde973c40f61cf8dea579cc8c6552f3892032dc6d730d8
```

周频数据 SHA-256：

```text
59049ce46a1f3f1efaa8f3a7c6a135349860f0f4ed84fdca3523fcc366287c7c
```

`weekly_output0613.csv` 包含 841 行、575 列，`week_id` 覆盖 `201001..202622`。三个目标列分别为：

| 期限 | 目标列 | 窗口 | 特征模式 | 额外列上限 |
|---|---|---:|---|---:|
| 1Y | `TB1YWI1C` | 全部可用历史 | `diff` | 160 |
| 5Y | `TB5YWI1C` | 156 | `diff` | 160 |
| 10Y | `TB0YWI1C` | 104 | `diff` | 80 |

三个方案均应推断为：

| 字段 | 值 |
|---|---|
| `task_type` | `weekly_point` |
| `horizon` | `1` |
| `target_rule` | `target_week_end_yield_vs_feature_week_end_yield` |
| `schema_version` | `1.0` |

参考交付使用新的 trial 身份，避免与现有 `weekly_10y_lgbm_point_v1` 及其他方案冲突：

```text
weekly_1y_lgbm_calibrated_trial_v1
weekly_5y_lgbm_calibrated_trial_v1
weekly_10y_lgbm_calibrated_trial_v1
```

评分器不要求 agent 必须使用这三个逐字相同的 ID；它要求三个 ID 合法、互不相同、未使用已知冲突 ID，且文件名、Metadata 和期限映射一致。

## 3. 任务包结构

```text
blackbox-v2-weekly-lgbm-3tenor-task/
├── instruction.md
├── task.toml
├── environment/
│   ├── Dockerfile
│   ├── docker-compose.yaml
│   ├── agent-python-requirements.txt
│   └── input/
│       ├── source/
│       │   ├── weekly_lgbm_predict_3tenors.py
│       │   └── weekly_output0613.csv
│       ├── benchmarks/
│       │   ├── weekly_1y_reference.csv
│       │   ├── weekly_5y_reference.csv
│       │   └── weekly_10y_reference.csv
│       └── contract/
│           ├── data_bridge_v1_schema.json
│           ├── request.sample.json
│           ├── requests.sample.csv
│           └── sample_data/
│               ├── daily_output.csv
│               ├── weekly_output.csv
│               └── monthly_output.csv
├── solution/
│   ├── solve.sh
│   └── reference_deliverables/
│       ├── weekly_1y_lgbm_calibrated_trial_v1.py
│       ├── weekly_1y_lgbm_calibrated_trial_v1.json
│       ├── weekly_5y_lgbm_calibrated_trial_v1.py
│       ├── weekly_5y_lgbm_calibrated_trial_v1.json
│       ├── weekly_10y_lgbm_calibrated_trial_v1.py
│       └── weekly_10y_lgbm_calibrated_trial_v1.json
└── tests/
    ├── grader.toml
    ├── judge-guidance.md
    ├── rubric.json
    ├── test.sh
    ├── deterministic_verifier.py
    └── hidden_fixtures/
```

任务运行目录为：

```text
/home/agent/workspace/blackbox_workspace/
```

最终交付目录为：

```text
/home/agent/workspace/blackbox_workspace/deliverables/
```

最终目录必须恰好包含六个文件。Agent 可以在工作区其他位置生成临时文件，但不得把 README、日志、模型、缓存、数据或辅助模块放入最终交付目录。

任务环境允许 agent 联网，`task.toml` 使用 `allow_internet = true`。但是 verifier 会在不依赖网络、数据库或外部服务的条件下运行六个交付脚本，以证明最终算法不存在外部运行依赖。

## 4. 六文件契约

每个 Python 文件独立实现：

```bash
python {scheme_id}.py predict \
  --request request.json \
  --data-dir <data-dir> \
  --output prediction.json

python {scheme_id}.py backtest \
  --requests requests.csv \
  --data-dir <data-dir> \
  --output backtest.csv
```

每个脚本只读取 `--data-dir` 下实际消费的 `weekly_output.csv`。平台式数据目录仍提供日、周、月三个文件；未消费的日频和月频文件不影响算法运行。

每个 Request 必须恰好包含：

```text
request_id
predict_date
feature_date
target_date
daily_cutoff_key
weekly_cutoff_key
monthly_cutoff_key
```

脚本对每个 Request 使用 `weekly_cutoff_key` 精确定位并包含截止行，只能使用截止行及以前的周频数据。不得用文件末行代替截止键，不得让截止键之后的合法行、新增无关业务列、批次大小或 Request 顺序改变同一 Request 的结果。

Result 必须恰好包含：

```text
request_id
predict_date
feature_date
target_date
predicted_direction
```

`predicted_direction` 只能是 JSON 整数或 CSV 文本 `-1/0/1`。当前源算法是二分类方案，正常结果只使用 `-1/1`，不得用 `0` 掩盖异常、缺数或低置信度。

成功执行只向 `--output` 写业务结果，`stdout` 为空，日志写 `stderr`。Output 先完整计算和校验，再通过同目录临时文件原子替换。失败时退出码非零且不得留下完整或部分 Output。

## 5. 源算法保真与 live-safe 口径

三个参考实现保留源脚本的：

- 目标列和期限映射；
- `window`、`feature_mode` 和 `max_extra_cols`；
- target lag、momentum、volatility、z-score 特征；
- 额外业务列筛选、原值和一阶差分特征；
- calibration split；
- LightGBM 超参数和随机种子；
- balanced calibration 阈值网格、目标上涨率和 penalty；
- `prob_up >= threshold_used` 的方向映射。

原脚本一次读取完整文件并在固定测试范围内循环。Contract 1.0 要求逐 Request 截止隔离，因此参考实现和隐藏 oracle 均在每个 Request 截断后的数据上运行同一算法逻辑。可见 benchmark 用于解释原始三期限输出；隐藏 live-safe oracle 才是逐 Request 截止、未来行隔离和 Contract Result 的权威评分口径。两种角色不得混称。

同一 Request 在 `predict` 与 `backtest`、单条与批量、不同批次切分、不同输入顺序和重复执行之间必须得到相同的五字段 Result。

## 6. 模拟技术入库

Verifier 只在临时目录构造 synthetic platform state，不读取或修改真实项目状态。

| 阶段 | 模拟检查 | 明确不做 |
|---|---|---|
| Intake | 六文件、同名身份、八字段 Metadata、三种期限和任务组合 | 不写 `schemes/` |
| static | 导入、路径、CLI、危险副作用和交付目录纯净性 | 不自动改代码 |
| input | 三频文件存在、周频 Schema、Snapshot 摘要和 Request 截止键 | 不连接 DataBridge |
| unit | 合法与非法 Request/Result、单点和批量边界 | 不写业务表 |
| dry-run | 三方案 Result、stdout/stderr、synthetic protected-state 零变化 | 不调用真实 executor |
| compare | 可见 benchmark、隐藏 live-safe oracle、重复/分批/变序/未来行隔离 | 不以调参贴结果 |
| backtest | 1–100 条 no-persist、顺序和摘要 | 不持久化历史表 |
| api-readiness | 构造三个 composite ID 和内存 SchemeConfig | 不请求真实 API |
| shadow | 临时 state 中 `status=paused`、`version_status=shadow` | 不 Activate、不 live |

Shadow 模拟完成后，三个 trial 必须在 synthetic active API 和 scheduler 视图中不可见。基础状态、输入文件和受保护 synthetic tables 的摘要必须保持不变。

## 7. 评分设计

`rubric.json` 固定包含 150 条细粒度标准，每条只判断一个事实：

| 区域 | 数量 | 评分方式 |
|---|---:|---|
| 六文件结构、命名与目录纯净性 | 15 | 机器 |
| 三份 Metadata 和任务语义推断 | 20 | 机器为主 |
| CLI、Request、Result 和原子 Output | 25 | 机器 |
| DataBridge 读取与逐 Request 截止隔离 | 20 | 机器 |
| 1Y/5Y/10Y 源算法与参数保真 | 25 | 机器 |
| predict/backtest、分批、变序和重复确定性 | 18 | 机器 |
| 七个 Gate 与 `shadow + paused` 模拟 | 15 | 机器 |
| 失败处理、日志和禁止副作用 | 8 | 机器 |
| `name`、`description` 和代码可读性 | 4 | LLM |
| **合计** | **150** | |

`deterministic_verifier.py` 为每条机器标准输出 rubric ID、通过状态、期望值、观察值和相关文件。Judge 必须先检查实际六文件并运行 verifier，机器证据为机器标准的权威事实；不得依据 agent 自述覆盖失败证据。

隐藏测试至少包括：

- 三个期限的多个截止周；
- 合法的未来行追加与未来业务值修改；
- 未使用新增列；
- Request 变序和批次切分；
- 相同 Request 的 predict/backtest 对照；
- 重复执行；
- 空批、重复 ID、缺字段、额外字段和非法日期；
- 缺少实际消费文件、缺失截止键和非法业务值；
- 已存在 Output、失败后临时文件清理；
- 三方案交叉期限污染检查；
- synthetic Intake、Gate、shadow 和 API/scheduler 不可见性。

机器评分占主要权重。LLM 只判断四项：三个方案名称/说明是否准确简洁，以及整体代码是否清晰地表达共享算法与期限隔离。

## 8. 测试与验收

实现完成后至少执行：

1. 三个参考脚本的 `--help`、`predict` 和 `backtest`；
2. 可见 benchmark 生成与 reference Result 对照；
3. 六文件 Intake 和 150 条 rubric schema 校验；
4. 完整 deterministic verifier；
5. `solution/solve.sh` 的空目录 oracle run；
6. Docker image 构建；
7. 容器内 agent workspace、verifier 权限和联网配置检查；
8. `tests/test.sh` 端到端试运行；
9. 确认现有仓库 `schemes/`、Registry 配置、数据库和灰度相关文件没有变化。

完成标准是：任务包结构完整、六个参考产物可执行、150 条 rubric 可逐项产生证据、oracle run 通过，且现有 Bond Factor Lab 运行状态零变化。
