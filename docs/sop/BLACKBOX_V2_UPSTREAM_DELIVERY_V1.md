# Blackbox V2 上游算法改造说明（Contract 1.0）

**文档状态**：`CURRENT`

**适用运行时**：`blackbox_v2`

**目标读者**：上游算法工程师

本文只说明如何把算法改造成平台可接收的两文件方案，以及交付前必须完成的自测。

随包材料：

```text
BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md
data_bridge_v1_schema.json
samples/
  metadata.sample.json
  request.sample.json
  requests.sample.csv
  prediction.sample.json
  backtest.sample.csv
  performance.sample.json
  daily_output.sample.csv
  weekly_output.sample.csv
  monthly_output.sample.csv
  api_wind_date.sample.csv
```

四份 DataBridge sample 只有少量合成数据，只用于检查读取、选列、截止截断和命令行接口，不能用于
训练、效果回测或性能验收。正式自测必须使用同一份真实 DataBridge 四文件数据。

本文中的 `Blackbox V2` 是运行时代际，`schema_version=1.0` 是接口合同版本，
`data-bridge-v1` 是输入 Schema；三者都不是上游算法版本。

## 1. 最终交付物

一个方案最终只交付同一目录内的两个普通文件：

```text
{scheme_id}.py
{scheme_id}.json
```

- 两个文件名、目录名和 Metadata 中的 `scheme_id` 必须完全一致。
- `scheme_id` 必须匹配 `^[a-z][a-z0-9_]*$`。
- 一个方案只对应一个 `target_tenor + task_type + horizon` 组合。
- 不得额外交付依赖文件、模型文件、辅助模块、数据、凭据或项目目录。
- 训练逻辑、模型结构和固定参数必须全部包含在唯一 `.py` 文件中。
- 自测凭证 `{scheme_id}.performance.json` 与两文件目录并列交接，不能放进两文件目录。

两文件只描述和实现算法，不得包含非算法控制字段。

## 2. Metadata

`{scheme_id}.json` 必须是无 BOM 的 UTF-8 JSON，并且精确包含以下十个字段：

```text
schema_version
scheme_id
name
owner
description
algorithm_version
target_tenor
task_type
horizon
target_rule
```

可直接参考 `samples/metadata.sample.json`。

字段约束：

- `schema_version` 固定为字符串 `1.0`。
- `scheme_id` 必须满足第 1 节的身份规则。
- `name` 是方案简称，不重复期限、任务类型、horizon 或“方向预测”等格子已有信息。
- `owner` 是方案来源，必须为不超过 64 字符的明确文本；不得有首尾空白、换行、控制字符、`<`、
  `>`，也不得使用 `--`、`unknown`、`待定` 等占位值。
- `description` 是不超过 300 字符的单段纯文本，简述主要输入、窗口或规则、模型类型和方向形成方式；
  不得包含换行、HTML 或标记文本。
- `algorithm_version` 是上游算法版本，必须为非空字符串。
- `target_tenor` 只允许 `1Y`、`3Y`、`5Y`、`7Y` 或 `10Y`。
- `task_type`、`horizon` 和 `target_rule` 必须整行选用下表组合。
- 不得增加输入路径、运行开关、可变阈值、特征列表、模型参数或平台控制字段。

| `task_type` | `horizon` | `target_rule` | 业务含义 |
|---|---:|---|---|
| `T+1` | 1 | `target_date_yield_vs_feature_date_yield` | 第 1 个后续交易日相对数据截止日的收益率方向 |
| `T+5` | 5 | `target_date_yield_vs_feature_date_yield` | 第 5 个后续交易日相对数据截止日的收益率方向 |
| `weekly_point` | 1 | `target_week_end_yield_vs_feature_week_end_yield` | 下一周频点相对本周频点的收益率方向 |
| `weekly_average` | 1 | `target_week_average_yield_vs_feature_week_average_yield` | 下一周平均收益率相对本周平均收益率的方向 |
| `monthly` | 1 | `target_month_observation_yield_vs_feature_month_observation_yield` | 下一月观测相对本月观测的收益率方向 |
| `monthly_average` | 1 | `target_month_average_yield_vs_feature_month_average_yield` | 下一 MID 月中桶平均收益率相对当前桶的方向 |
| `quarterly_average` | 1 | `target_quarter_average_yield_vs_feature_quarter_average_yield` | 下一自然季度平均收益率相对当前季度的方向 |
| `annual_average` | 1 | `target_year_average_yield_vs_feature_year_average_yield` | 下一春节年平均收益率相对当前春节年的方向 |

`horizon` 表示相应任务的一个业务步长。周、月和周期平均任务不得写成 `7/30/90/365`，算法也不得
根据 horizon 自行推导任务或目标日期。

## 3. 运行环境与代码边界

| 项目 | 版本或上限 |
|---|---|
| Python | 3.13.12 |
| 数值与数据 | numpy 2.3.5、pandas 2.3.3、scipy 1.16.3 |
| 机器学习 | scikit-learn 1.8.0、lightgbm 4.6.0、xgboost 3.1.3、catboost 1.2.8 |
| 其他可用包 | joblib 1.5.3、pyarrow 23.0.0、openpyxl 3.1.5 |
| 数值计算线程 | 最多 8，不保证独占 8 核 |
| 进程内存 | 64 GiB |
| `predict` 超时 | 3600 秒 |
| `backtest` 超时 | 14400 秒 |
| Output 文件 | 50 MiB |
| `stderr` 日志 | 5 MiB |
| 整个运行目录 | 64 MiB、最多 10000 个目录项 |

交付脚本不得访问网络、数据库或启动其他进程。以下 import 根会被拒绝：

```text
aiohttp ftplib http mysql pymysql psycopg requests socket sqlalchemy sqlite3 subprocess urllib
```

以下调用会被拒绝：

```text
eval exec compile __import__
os.system os.popen os.spawnl os.spawnv
```

脚本还必须满足：

- 不安装依赖，不动态加载交付文件以外的代码。
- 不硬编码本机绝对路径，不使用 `..` 路径穿越。
- 不读取交付目录旁的数据、配置、模型或日历。
- 只读取平台传入的 `--request/--requests` 和只读 `--data-dir`。
- 只向平台给出的 `--output` 写业务结果；日志只写 `stderr`，`stdout` 保持为空。

## 4. DataBridge 四文件输入

每次调用的 `--data-dir` 都包含同一份只读数据快照：

```text
daily_output.csv
weekly_output.csv
monthly_output.csv
api_wind_date.csv
```

算法只读取实际需要的文件；不需要的文件可以完全不解析，但不能因为它们存在而失败。

| 文件 | 时间键 | 读取要求 |
|---|---|---|
| `daily_output.csv` | `date` | `YYYY-MM-DD` 字符串 |
| `weekly_output.csv` | `week_id` | 六位字符串，不得按 ISO 周解释或加减 |
| `monthly_output.csv` | `month_id` | 六位字符串 |
| `api_wind_date.csv` | `rdate` | 精确两列 `rdate,week_id`，用于平台日期到周键的映射 |

`data_bridge_v1_schema.json` 为最低兼容字段基线。真实文件必须满足：

- 四个文件均存在、非空，表头没有重复字段。
- 第一列分别为 `date/week_id/month_id/rdate`。
- Schema 中每个基线字段都存在，且相对顺序不变；真实数据可以增加业务列。
- 三份因子文件的时间键唯一；其余业务值只能是有限数值或空值。
- `api_wind_date.csv` 只能有 `rdate,week_id` 两列；`rdate` 唯一，`week_id` 为六位字符串。
- 算法按字段名选择实际消费列，忽略未使用的新增业务列，不依赖固定行数、列数或数据终点。

上游和平台只有使用同一份四文件数据才能逐值比较结果。四份文件任一 SHA-256 不同，必须先按
输入版本不同处理，不能直接认定为算法差异。

## 5. Request

### 5.1 精确字段

单点 JSON 和批量 CSV 都精确包含以下七个字段，不允许缺少或增加：

```text
request_id
predict_date
feature_date
target_date
daily_cutoff_key
weekly_cutoff_key
monthly_cutoff_key
```

- 所有字段都是字符串。
- `request_id` 非空；批量 CSV 中必须唯一。
- 四个日期字段使用规范 `YYYY-MM-DD`。
- 日期必须满足 `feature_date <= predict_date <= target_date` 且 `feature_date < target_date`。
- `weekly_cutoff_key` 和 `monthly_cutoff_key` 是六位字符串。
- CSV 表头只允许上述七个字段，每行都必须完整。
- 算法只能校验和使用平台给出的值，不得顺延、回退、替换或重新推导。

`samples/request.sample.json` 是单点示例；`samples/requests.sample.csv` 是三行批量示例。

### 5.2 每条 Request 独立截止

| 实际消费文件 | Request 截止字段 | 截断规则 |
|---|---|---|
| `daily_output.csv` | `daily_cutoff_key` | 精确定位 `date`，保留首行至该行，包含该行 |
| `weekly_output.csv` | `weekly_cutoff_key` | 精确定位 `week_id`，保留首行至该行，包含该行 |
| `monthly_output.csv` | `monthly_cutoff_key` | 精确定位 `month_id`，保留首行至该行，包含该行 |

对未消费的频率只校验 Request 字段格式，不需要读取对应 CSV。实际消费文件缺失、截止键不存在、
时间键不唯一或截断后数据不足时，整次调用必须失败。

算法不得用文件最后一行代替截止键，也不得从 `feature_date` 推导周、月截止键。依赖平台周历时，
还必须确认 `daily_cutoff_key` 在 `api_wind_date.csv` 中唯一映射到给定 `weekly_cutoff_key`。

## 6. 两个命令

同一个 `{scheme_id}.py` 必须支持：

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

- `predict` 读取一条 Request，输出一条 JSON Result。
- `backtest` 读取至少一条 Request，输出行数相同、顺序相同的 CSV Result。
- 完整历史区间会在一个算法进程中一次传入，脚本不得假定最多 100 条。
- 两个命令必须复用相同的数据处理、算法逻辑和方向映射。
- 同一 Request 在单点、完整批次、不同子集、乱序和重复执行时必须得到完全相同的结果。
- 不得保留跨调用状态，也不得让当前 Request 的结果依赖前一行 Request 的执行结果。

### 6.1 批量计算效率

本身可以一次生成整条 walk-forward 序列的算法，应在一次 `backtest` 调用中完成等价批量计算，不能
对每条 Request 重复完整训练。批量优化必须满足：

1. 每条结果与该 Request 独立截止后的计算逐字段一致。
2. 每次调用确定性抽取首条、中间一条和末条，用独立截止路径比较内部 score/probability、方向和日期。
3. 任一比较不一致时整次调用失败，不生成 Output；不得用不等价结果继续运行。

算法确实无法共享计算时可以逐条计算，但仍必须满足第 8 节的完整区间性能要求。

严禁使用晚于单条 Request cutoff 的固定 `source_end`、未来标签、未来 test window、全区间
selector/calibration，或依赖 Request 顺序的隐藏状态。

## 7. Result 与失败语义

JSON 和 CSV Result 都精确包含以下五个字段：

```text
request_id
predict_date
feature_date
target_date
predicted_direction
```

- 每个 Request 恰好对应一条 Result，顺序保持一致。
- 前四个字段必须原样回显对应 Request。
- JSON 的 `predicted_direction` 是整数 `-1/0/1`；CSV 是文本 `-1/0/1`。
- `0` 表示算法真实预测方向为平，不得用来掩盖异常、缺数或低置信度。
- cutoff、概率、日志和调试字段不得进入 Result。

参考 `samples/prediction.sample.json` 和 `samples/backtest.sample.csv`。

成功时必须先完成全部计算和校验，再通过同目录临时文件原子替换 `--output`。任一 Request 失败时：

- 退出码非 0；
- 错误写入 `stderr`，`stdout` 为空；
- 不留下完整、部分或临时 Output。

失败条件至少包括：空批次、字段不符、非法日期、重复 ID、实际消费文件缺失、截止键不存在、输入
不足、非法业务值、无法生成合法方向，以及首中末独立复算不一致。

如使用随机过程，必须固定随机状态；相同环境、输入和 Request 的结果必须确定。

## 8. 交付前自测

使用冻结运行环境和同一份真实 DataBridge 四文件数据，至少完成：

1. 单条 `predict` 连续运行三次。
2. 时间升序的 100 条 `backtest` 连续运行三次；正式区间不足 100 条时使用全部 Request。
3. 同一 100 条乱序运行，按 `request_id` 比较后结果逐字段一致。
4. 在约定的完整历史区间上执行一次 `backtest` 调用。
5. 每次批量调用的首、中、末 Request 独立截止复算完全一致。
6. 在每类非法输入下验证非零退出、stderr 有错误、stdout 为空且没有 Output。
7. 在消费文件截止键之后追加合法未来行，当前 Request 结果保持不变。

性能准入：

| 项目 | 上限 |
|---|---:|
| 单条 `predict` | 120 秒 |
| 100 条 `backtest` | 600 秒 |
| 完整正式区间 | 1800 秒 |
| 单个算法子进程峰值 RSS | 4 GiB |

在两文件目录外提供 `{scheme_id}.performance.json`，可复制
`samples/performance.sample.json` 后替换为真实值。必须记录方案身份、测试环境、四文件 SHA-256、
Request 数与日期边界、三次实际耗时、峰值 RSS、首中末自证样本和 `fallback_used=false`。不得使用
脱敏 sample 代替真实输入做性能证明。

## 9. 最终检查

- [ ] 两文件目录只有同名 `.py + .json`。
- [ ] Metadata 十个字段、身份、任务组合和展示信息全部合法。
- [ ] 脚本没有网络、数据库、子进程、额外代码或硬编码路径依赖。
- [ ] 四文件按字段名读取，每条 Request 按自身 cutoff 截断。
- [ ] `predict/backtest`、子集、乱序、重复和未来行隔离结果一致。
- [ ] Result 字段、顺序、类型、原子 Output 和失败无 Output 符合第 7 节。
- [ ] 完整区间只启动一个算法进程并满足性能准入。
- [ ] 真实性能凭证位于两文件目录外，数据和凭据没有进入交付物。

全部通过后，只把 `{scheme_id}.py + {scheme_id}.json` 作为正式方案交付。
