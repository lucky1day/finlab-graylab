# Bond Factor Lab 平台接入 SOP（Blackbox V2 V1 试运行版）

本 SOP 用于平台侧接收、运行和验收新的上游算法脚本。V1 先跑通一个方案，不自动迁移或修改已有方案。

---

## 1. 平台应该做什么

### 1.1 接收两个交付文件

```text
{scheme_id}.py
{scheme_id}.json
```

- `.py` 是唯一可执行文件，`.json` 只描述方案身份。
- 一个脚本只对应一个 `target_tenor + task_type + horizon` 组合。
- 平台不接收依赖安装文件、模型文件、配置文件、辅助 Python 模块或项目目录。
- 平台不修改上游 `.py` 的算法逻辑。

### 1.2 为脚本准备运行条件

平台负责准备 Python 环境、三类 CSV、日期 Request、输出路径、资源限制和日志采集。完整链路固定为：

```text
接收 .py + .json
  → 校验交付物
  → 准备环境和三类 CSV
  → 生成 Request
  → 子进程执行 predict 或 backtest
  → 校验 Result
  → 将合法结果交给后续链路
```

任一步失败时必须终止当次运行，不得提交部分结果。

---

## 2. 平台应该怎么做

### 第一步：发布运行环境

V1 使用 Python 3.12 和环境标识 `forecast_env`。收包前，平台必须发布精确的 Python/依赖版本、CPU、内存、超时、输出文件限制和环境自检命令。

单个方案不得要求平台临时安装私有包。脚本运行期间禁止网络访问。

### 第二步：校验交付物

先确认交付目录只有 `{scheme_id}.py` 和 `{scheme_id}.json`，再解析 Metadata：

```json
{
  "schema_version": "1.0",
  "scheme_id": "daily_10y_t1_demo",
  "name": "10年国债收益率日频方向预测",
  "algorithm_version": "1.0.0",
  "target_tenor": "10Y",
  "task_type": "T+1",
  "horizon": 1,
  "target_rule": "比较 target_date 与 feature_date 的 10Y 国债收益率"
}
```

必须校验：

- Metadata 是 UTF-8 JSON，且只包含上述八个字段。
- `schema_version` 是字符串 `1.0`。
- `scheme_id` 与两个文件名完全一致。
- `task_type` 是 `T+1`、`T+5`、`weekly_point`、`weekly_average` 或 `monthly`。
- `horizon` 是大于 `0` 的整数。
- 其余字段是非空字符串。
- Metadata 不包含阈值、特征、模型参数、路径或运行开关。

通过后记录两个文件的摘要和 `algorithm_version`。文件变化时必须作为新版本重新验收。

### 第三步：准备运行目录和 CSV

每次预测或回测使用独立的 `<run-dir>`：

```text
<run-dir>/
├── delivery/   # {scheme_id}.py + {scheme_id}.json
├── data/       # daily.csv + weekly.csv + monthly.csv
├── request/    # request.json 或 requests.csv
├── output/     # prediction.json 或 backtest.csv
└── logs/       # stderr.log
```

- 平台通过完整路径传参，不让脚本猜测工作目录。
- `data/` 对脚本只读，`output/` 只允许写入指定文件。
- 每次预测前，从数据桥拉取最新的日、周、月 CSV，校验后原子替换固定文件名。
- 运行前确认三个 CSV 都存在且可读。
- 回测使用固定快照，不在批次执行中替换数据。

平台必须发布《数据桥 CSV 输入契约 V1》和三份脱敏样例，定义字段、编码、日期列、空值、重复行和排序规则。该契约未就绪时，不开始交付验收。

### 第四步：生成 Request

平台使用自己的日历和任务规则生成 `predict_date`（信号发出日）、`feature_date`（数据截止日）和 `target_date`（验证目标日）。

单点 `request.json`：

```json
{
  "request_id": "predict-20260716-001",
  "predict_date": "2026-07-16",
  "feature_date": "2026-07-15",
  "target_date": "2026-07-17"
}
```

批量 `requests.csv`：

```csv
request_id,predict_date,feature_date,target_date
backtest-001,2026-06-01,2026-06-01,2026-06-02
backtest-002,2026-06-02,2026-06-02,2026-06-03
```

- `request_id` 由平台生成，且在当次批次内唯一。
- 三个日期使用 `YYYY-MM-DD`，不要求脚本计算或调整。
- 批量 Request 的输入顺序就是预期结果顺序。

### 第五步：通过子进程调用脚本

```bash
python {scheme_id}.py predict --request request.json --data-dir <data-dir> --output prediction.json
python {scheme_id}.py backtest --requests requests.csv --data-dir <data-dir> --output backtest.csv
```

调用前清理 `--output` 路径，切换到 `forecast_env`，然后：

1. 用完整路径传入脚本、Request、`data-dir` 和 Output；
2. 以子进程运行，不直接 import 上游脚本；
3. 应用资源限制、超时和无网络约束；
4. 记录退出码，将 `stderr` 保存到当次日志。

`stdout` 不作为业务结果，业务结果只从 `--output` 读取。

### 第六步：校验 Result

单点 `prediction.json`：

```json
{
  "request_id": "predict-20260716-001",
  "predict_date": "2026-07-16",
  "feature_date": "2026-07-15",
  "target_date": "2026-07-17",
  "predicted_direction": -1
}
```

批量 `backtest.csv`：

```csv
request_id,predict_date,feature_date,target_date,predicted_direction
backtest-001,2026-06-01,2026-06-01,2026-06-02,1
backtest-002,2026-06-02,2026-06-02,2026-06-03,-1
```

只在退出码为 `0` 时校验 Output：

- Output 存在，且为 UTF-8 JSON 或 CSV。
- Result 只包含上述五个字段。
- 四个 Request 字段与输入完全一致。
- `predicted_direction` 是整数 `-1`、`0` 或 `1`，不是字符串、布尔值或空值。
- `1` 表示目标收益率高于 `target_rule` 基准，`-1` 表示低于基准，`0` 表示相同或中性。
- 单点恰好一条结果；批量行数、`request_id` 和顺序与输入一致。

非零退出、超时、Output 缺失或结果非法时，当次运行失败。不得用方向 `0` 代替失败，也不得提交部分结果。

### 第七步：保存运行记录

验收通过后，将 `.py` 和 `.json` 作为只读版本保存。每次运行记录：

- `run_id`、`scheme_id`、`algorithm_version` 和交付文件摘要；
- CSV 快照标识、Request 和完整 Result；
- 开始/结束时间、退出码和 `stderr`。

只有通过校验的 Result 才能进入后续链路。交付文件或 Metadata 变化时，必须生成新版本并重新验收。

---

## 3. 平台应该怎么验证

### 3.1 验证交付物和环境

确认交付物只有两个文件，Metadata 八个字段合法，文件名与 `scheme_id` 一致，并在 `forecast_env` 中执行：

```bash
python --version
python {scheme_id}.py --help
```

Python 必须为 3.12，`--help` 必须显示 `predict` 和 `backtest`。

### 3.2 验证预测和回测

- 使用一份合法 Request 执行 `predict`：退出码为 `0`，Output 恰好一条，字段全部合法。
- 使用至少两个不同 `feature_date` 执行 `backtest`：输入输出行数、`request_id` 和顺序一致。

### 3.3 验证数据截止和重复执行

- 只修改晚于某个 `feature_date` 的 CSV 行，用相同 Request 重新运行；业务结果必须不变。
- 在相同环境中使用相同 CSV 和 Request 连续运行两次；五个结果字段必须完全一致。

### 3.4 验证失败场景

| 场景 | 预期结果 |
|---|---|
| 缺少任一 CSV | 运行失败，不进入后续链路 |
| Request 缺字段、日期非法或 `request_id` 重复 | 运行失败 |
| 进程非零退出或超时 | 运行失败，不解析为成功结果 |
| Output 不存在、字段非法或方向非法 | 运行失败 |
| 批量结果缺行、重复或乱序 | 运行失败 |

### 3.5 验证端到端试运行

```text
数据桥刷新三类 CSV
  → 平台生成 Request
  → forecast_env 子进程执行 predict
  → 校验退出码和 prediction.json
  → 结果进入后续处理
  → 核对最终结果与 prediction.json 一致
```

链路全部成功，且交付物、CSV 快照、Request、Result、退出码和日志均可追溯，才能判定 V1 试运行通过。

### 3.6 平台最终检查

- [ ] 已发布 `forecast_env` 依赖、资源限制和 CSV 输入契约
- [ ] 交付物、Metadata 和方案粒度校验通过
- [ ] 平台生成三个日期和 `request_id`
- [ ] 平台通过子进程而非 import 调用脚本
- [ ] 单点、批量、未来数据隔离和重复执行验证通过
- [ ] 非零退出、超时和非法 Result 都会终止当次运行
- [ ] 只有合法 Result 进入后续链路，并且全链路可追溯

全部验证通过后，再将该方案加入 V1 试运行链路。
