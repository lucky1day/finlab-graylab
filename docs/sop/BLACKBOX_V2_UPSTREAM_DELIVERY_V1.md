# Blackbox V2 上游交付 SOP（Contract 1.0）

**文档状态**：`CURRENT`

**适用运行时**：`blackbox_v2`

**目标读者**：上游算法工程师

本文是上游算法工程师唯一需要阅读的人类文档。完成开发只需要本文、随包提供的
`data_bridge_v1_schema.json`、Request 样例和四份脱敏 DataBridge sample；不需要再阅读仓库内其他文档。

本地开发、训练和效果验证优先使用从统一 DataBridge 下载的真实 DataBridge 数据。四份 sample 只在 DataBridge 暂时不可用时用于读取、选列、截止截断和接口烟雾测试，不能用于训练或效果回测。最终交付物仍然只有同名的 `{scheme_id}.py + {scheme_id}.json`。

`api_wind_date.csv` 是 DataBridge generation 固定提供的第四份文件，不是交付物；正式交付目录仍然只能包含同名 `.py + .json`。算法需要周历时读取，不需要时不读取。

本文中的 `Blackbox V2` 是运行时代际，`schema_version=1.0` 是交付接口合同版本，`data-bridge-v1` 是四文件数据 Schema；三者不能混作算法版本。

正式新交付必须在 `{scheme_id}.json` 中提供唯一的 `name`、`owner` 和 `description`；缺项或占位值均
fail-closed。`owner` 是页面“来源”字段，必须是不超过 64 字符、无首尾空白、换行、控制字符或 HTML
边界字符的明确文本，禁止 `--`、`unknown`、`待定` 等占位值。上游不得另交显示字段或依赖平台补写；
平台 Intake 原样保存 Metadata，并在注册时把 owner 写入 Registry。

---

## 1. 开始前准备

### 1.1 取得开发材料

开始实现前，需要取得：

1. DataBridge 地址、用户名和密码；
2. `data_bridge_v1_schema.json`；
3. 合法的单点 `samples/request.sample.json` 和批量 `samples/requests.sample.csv` 样例；
4. 离线兜底用的四份脱敏 sample：

```text
samples/daily_output.sample.csv
samples/weekly_output.sample.csv
samples/monthly_output.sample.csv
samples/api_wind_date.sample.csv
```

Python、关键包和资源限制直接列在下一节。不得要求为单个方案临时增加私有包。

### 1.2 Python、关键包与资源限制

Blackbox V2 Contract 1.0 当前使用的 Python 和关键包版本：

| 项目 | 版本 |
|---|---|
| Python | Python 3.13.12 |
| 数值与数据 | numpy 2.3.5、pandas 2.3.3、scipy 1.16.3 |
| 机器学习 | scikit-learn 1.8.0、lightgbm 4.6.0、xgboost 3.1.3、catboost 1.2.8 |
| 其他关键包 | joblib 1.5.3、pyarrow 23.0.0、openpyxl 3.1.5 |

以下为算法实际运行上限，超限将导致本次执行失败：

| 项目 | 上限 |
|---|---:|
| 常用数值计算库线程数 | 8 |
| 进程内存 | 64 GiB |
| `predict` 执行时间 | 3600 秒 |
| `backtest` 执行时间 | 14400 秒 |
| Output 文件 | 50 MiB |
| 标准错误日志 | 5 MiB |

线程数是平台为常用数值计算库设置的上限，不代表独占或保证提供 8 个 CPU 核心。批量回测每批 Request 的数量要求见第 5 节。

算法不得访问网络或数据库；该禁令由 Intake 和持久化回测前的脚本安全校验强制（禁 socket/urllib/requests/sqlalchemy 等 import、禁 eval/exec/os.system、禁绝对路径与相对路径穿越字面量），违规交付一律拒收。激活只接受安全校验和完整回测已通过的同 exact version，不再重复扫描。需要的训练逻辑、模型结构和固定参数必须全部包含在单一 `.py` 文件中。

### 1.3 明确最终只交付两个文件

一个算法方案最终只交付：

```text
{scheme_id}.py
{scheme_id}.json
```

- `{scheme_id}.py` 是唯一可执行文件；`{scheme_id}.json` 只描述方案身份和固定任务口径。
- 两个文件名以及 Metadata 中的 `scheme_id` 必须完全一致。
- `scheme_id` 必须匹配 `^[a-z][a-z0-9_]*$`。
- 一个脚本只对应一个 `target_tenor + task_type + horizon` 组合。
- `target_tenor` 只允许 `1Y`、`3Y`、`5Y`、`7Y` 或 `10Y`。
- 不得额外交付依赖文件、模型文件、配置文件、辅助模块、数据文件、凭证或项目目录。

如算法需要训练，训练逻辑和固定参数必须包含在 `.py` 中，并且只能使用当前 Request 允许的数据。DataBridge 下载文件、机器 Schema 和脱敏 sample 是开发材料，不是算法方案交付物。

Intake 不接收方案级输入声明。上游不得在 Metadata 中增加输入路径或平台控制字段，也不得用随包日历代替 DataBridge generation 中的权威文件。

### 1.4 交付不授予平台控制面权限

两文件和自验凭证只证明交付可被接收，不授予 activation、灰度写入或调度权限。Metadata 和交付目录
不得携带平台运行、审批或授权字段。

---

## 2. 统一 DataBridge 数据

机器字段基线、四份标准文件和 sample 用途统一见
[DataBridge V1 数据契约](../blackbox_v2/data_bridge_v1/README.md)；机器真值是
`shared/blackbox_v2/data_bridge_v1_schema.json`，本 SOP 不复制 Schema、列数或校验脚本。

上游和平台必须使用同一 DataBridge generation。四份文件任一摘要
不同，结果差异先标记 `data_vintage_mismatch`；不得自行写 SQL、拼接数据源、修改 CSV、把
`week_id` 当 ISO 周，或把下载逻辑写入交付脚本。正式运行只读取平台提供的只读 `--data-dir`。

## 3. 准备自测输入

从 DataBridge 管理方取得 `/api/` 地址和只读凭据，在同一连续批次下载四份文件。凭据只放在当前 shell，不进入 Git、交付包、日志或聊天记录。

```bash
export DATABRIDGE_API_BASE_URL="<DataBridge /api/ 地址>"
export DATABRIDGE_API_USERNAME="<只读用户名>"
read -r -s DATABRIDGE_API_PASSWORD && export DATABRIDGE_API_PASSWORD
export DATABRIDGE_END_DATE="<YYYY-MM-DD>"
mkdir -p sample_data

curl --fail-with-body --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" --data-urlencode "frequency=日" \
  --data-urlencode "start_date=2010-01-01" --data-urlencode "end_date=$DATABRIDGE_END_DATE" \
  --output sample_data/daily_output.csv
curl --fail-with-body --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" --data-urlencode "frequency=周" \
  --output sample_data/weekly_output.csv
curl --fail-with-body --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" --data-urlencode "frequency=月" \
  --output sample_data/monthly_output.csv
# 仅依赖平台周历的方案执行：
curl --fail-with-body --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  "${DATABRIDGE_API_BASE_URL%/}/export/tables/api_wind_date/csv/" \
  --output sample_data/api_wind_date.csv
unset DATABRIDGE_API_PASSWORD
```

任一下载失败、为空或混入旧文件时整批作废。使用仓库 `shared.data_bridge.validation.validate_dataset`
和当前机器 Schema 校验三频文件；周历必须精确为 `rdate,week_id`，日期唯一升序、周键为六位
字符串。算法按字段名选择消费列，忽略未使用的新增业务列，不得修改表头绕过校验。

交接材料只保存下载时间、声明截止日、各文件 SHA256/行数/首尾键、七字段 Request、日历映射、
实际消费列和自测结果。数据、sample、manifest、自测报告与日历都不进入正式两文件 delivery。

## 4. 选择任务并填写 Metadata

### 4.1 选择固定任务组合

Contract 1.0 只允许以下组合：

| `task_type` | `horizon` | `target_rule` | 业务含义 |
|---|---:|---|---|
| `T+1` | 1 | `target_date_yield_vs_feature_date_yield` | 第 1 个后续交易日相对 `feature_date` 的收益率方向 |
| `T+5` | 5 | `target_date_yield_vs_feature_date_yield` | 第 5 个后续交易日相对 `feature_date` 的收益率方向 |
| `weekly_point` | 1 | `target_week_end_yield_vs_feature_week_end_yield` | 下一周频点相对本周频点的收益率方向 |
| `weekly_average` | 1 | `target_week_average_yield_vs_feature_week_average_yield` | 下一周平均收益率相对本周平均收益率的方向 |
| `monthly` | 1 | `target_month_observation_yield_vs_feature_month_observation_yield` | 下一月观测相对本月观测的收益率方向 |
| `monthly_average` | 1 | `target_month_average_yield_vs_feature_month_average_yield` | 下一 MID 月中桶平均收益率相对当前桶平均收益率的方向 |
| `quarterly_average` | 1 | `target_quarter_average_yield_vs_feature_quarter_average_yield` | 下一自然季度平均收益率相对当前季度平均收益率的方向 |
| `annual_average` | 1 | `target_year_average_yield_vs_feature_year_average_yield` | 下一春节年平均收益率相对当前春节年平均收益率的方向 |

`horizon` 按任务业务步长计期：日频按后续交易日计数；周频按周频观测计数；月中收按月频观测计数；
周均、MID 月均、自然季均和春节年均的 `horizon=1` 均表示下一个同类业务桶。不得把后三种任务写成
`30/90/365`，也不得通过 `horizon` 推断任务类型、桶类型或目标日期。必须整行选择任务组合，不得自由修改
`horizon` 或填写其他 `target_rule`。

### 4.2 填写 `{scheme_id}.json`

Metadata 必须是无 BOM 的 UTF-8 JSON。所有正式新交付都必须提供 `name`、`owner` 和
`description`，三者共同构成前端展示信息：

```json
{
  "schema_version": "1.0",
  "scheme_id": "one_y_t5_liq_excess_a_w252_l7_v1",
  "name": "LIQ_EXCESS_A_W252_L7",
  "owner": "lw",
  "description": "使用流动性指标和滚动窗口构建特征，通过分类模型判断未来5个交易日1Y国债收益率方向。",
  "algorithm_version": "1.0.0",
  "target_tenor": "1Y",
  "task_type": "T+5",
  "horizon": 5,
  "target_rule": "target_date_yield_vs_feature_date_yield"
}
```

- `schema_version` 固定为字符串 `1.0`。
- `scheme_id` 必须与目录名、Python 文件名和 JSON 文件名一致。
- `scheme_id` 和 `target_tenor` 必须满足第 1 节约束。
- `scheme_id` 是算法执行身份；`name` 是当前任务格子内用于区分候选方案的简洁业务名称，两者不要混用。
- `name` 不得重复 `target_tenor`、不得重复 `task_type` 或 `horizon`，也不得追加“方向预测”等已经由任务格子表达的说明。
- `name`、`owner` 和 `algorithm_version` 必须是非空字符串；`algorithm_version` 不强制使用特定版本格式。
- `owner` 必须满足本文开头的来源字段约束；它只表达方案来源，不得承载运行环境或审批信息。
- `description` 必须简述主要输入、窗口或规则、模型类型以及最终方向形成方式；平台不会根据脚本或名称代写算法逻辑。
- `description` 必须是单段非空纯文本，最多 300 个字符，不得包含换行、HTML 或其他标记文本。
- `name` 和 `description` 职责不同，不得用方案名代替算法说明，或把任一字段留给平台推测。
- `task_type`、`horizon` 和 `target_rule` 必须来自上一节的同一行。
- 除本节规定的 `owner` 和 `description` 外，Metadata 不得增加输入路径、运行开关、可变阈值、特征列表或模型参数。

---

## 5. 实现同一个脚本的两个命令

`{scheme_id}.py` 必须支持：

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

- `predict` 读取一个 Request，输出一条预测结果。
- `backtest` 读取一批 Request，每个 Request 输出一条结果；每次调用至少包含一条，平台可以把完整历史区间一次传入。
- 两个命令必须复用相同的数据处理、算法逻辑和方向映射。
- 单个 Request 的结果不得因批次大小、批次切分或输入顺序改变。

平台对一个方案的完整历史回测只启动一个算法进程，并把该区间的完整 Request 序列写入同一份
`requests.csv`。上游脚本必须处理当前收到的全部 Request，不得假定最多 100 条，也不得保存跨调用状态。
任一 Request 在完整区间、100 条子集、不同分区边界或输入顺序下都必须得到相同结果。

脚本只使用冻结运行环境和 Request，只从 `--data-dir` 读取业务数据，只向 `--output` 写业务结果。脚本不得：

- 访问 DataBridge、网络、数据库或其他业务数据源；
- 安装依赖、修改运行环境或动态加载交付包之外的代码；
- 写入 `--data-dir`，或者硬编码本机绝对路径；
- 自行计算或修改平台给出的日期和截止键；
- 将业务结果或调试内容写入 `stdout`。

日志和错误信息统一写入 `stderr`。业务运行期间 `stdout` 必须为空。

---

## 6. 读取数据和 Request

### 6.1 读取平台提供的数据

平台每次运行都通过 `--data-dir` 提供：

```text
<data-dir>/daily_output.csv
<data-dir>/weekly_output.csv
<data-dir>/monthly_output.csv
<data-dir>/api_wind_date.csv
```

- 同一次运行的四份文件属于同一份只读快照，运行期间不会被替换。
- 平台始终提供四份文件；算法只读取当前方案实际需要的文件。
- 不得因为未使用的文件存在而失败，也不要求主动解析未使用文件。
- 三份因子文件的业务列只能是有限数值或空值。
- 数据行数、列数、起止区间、业务值和空值都可以变化；算法必须按字段名选择实际消费列，并忽略未使用的新增列。
- 不得假定固定行数、固定列数、固定终点或“文件最后一行就是当前 Request 截止点”。

该文件固定为 `rdate,week_id` 两列；`rdate` 是升序、唯一的
`YYYY-MM-DD`，`week_id` 是六位平台周键。算法可以用它按
`weekly_cutoff_key` 查询平台周映射，但不得改写文件、连接数据库补全
日历，或回退读取交付目录旁的同名文件。

`week_id` 只能作为不透明字符串键使用。算法不得将其解释为 ISO 周、
不得假定连续、不得执行加减一，也不得保留内嵌日历、脚本同目录日历
或网络/数据库 fallback。依赖周历的算法在
`--data-dir/api_wind_date.csv` 缺失、列不合法或截止映射不一致时必须
fail-closed。

推荐按字符串读取时间键：

```python
from pathlib import Path

import pandas as pd


data_dir = Path("<platform-provided-data-dir>")
daily = pd.read_csv(
    data_dir / "daily_output.csv",
    dtype={"date": "string"},
)
weekly = pd.read_csv(
    data_dir / "weekly_output.csv",
    dtype={"week_id": "string"},
)
monthly = pd.read_csv(
    data_dir / "monthly_output.csv",
    dtype={"month_id": "string"},
)
calendar = pd.read_csv(
    data_dir / "api_wind_date.csv",
    dtype={"rdate": "string", "week_id": "string"},
)
```

### 6.2 Request 字段

Request 固定为 `request_id`、`predict_date`、`feature_date`、`target_date` 和三个频率 cutoff 共七个
字段；字段类型、日期关系、批内唯一性与输出顺序以
[Blackbox Contract](../architecture/SCHEME_CONTRACT.md)为唯一机器语义。算法只能校验和使用平台给定值，
不得修改、顺延、回退或重新推导。依赖周历的方案还必须证明 `daily_cutoff_key` 在本批
`api_wind_date.csv` 中唯一映射到给定 `weekly_cutoff_key`，不一致时整批失败。

### 6.3 对每个 Request 独立截断

| 文件 | Request 字段 | 定位规则 |
|---|---|---|
| `daily_output.csv` | `daily_cutoff_key` | 将 `date` 规范化为 `YYYY-MM-DD` 后精确定位 |
| `weekly_output.csv` | `weekly_cutoff_key` | 与 `week_id` 字符串精确匹配 |
| `monthly_output.csv` | `monthly_cutoff_key` | 与 `month_id` 字符串精确匹配 |

对每个 Request，算法必须：

1. 读取当前方案实际消费的 CSV；
2. 在每个消费文件中精确定位对应截止键；
3. 保留第一行至截止键所在行，包含截止键行；
4. 只使用截断后的数据执行当前 Request。

缺少实际消费的 CSV、截止键不存在、时间键不唯一或截断后没有算法所需数据时，本次运行必须失败。算法只需格式校验 Request 中未消费频率的截止键，不需要读取对应文件验证。

不得用文件最后一行代替截止键，也不得根据 `feature_date` 推导周、月截止键。批量回测的**默认语义**是逐行独立截断。

对本身就是 walk-forward 结构、一次运行即产出整条逐点预测序列的算法，**必须**在算法内部
使用**等价的一次性计算**替代逐条重算。这不是可选优化：逐条重算会把持久化回测从十几分钟
拉长到数小时甚至一夜（80 条 Request、单次计算 4 分钟即约 5.6 小时），平台不接受这种成本。
实现时必须同时满足以下三条，缺一不可：

1. **结果等价**：每条 Request 的五个业务字段必须与该 Request 独立截断后计算的结果完全一致；
2. **批内自证**：算法必须在**每个批次内**实际抽样复算并逐字段比对，样本至少覆盖批内首条、中间一条和末条 Request；比对必须使用独立截断路径重新计算，且比较算法内部方向与概率字段，不能只比最终方向；
3. **失败回退**：任一抽样不一致时，必须整批回退到逐条独立截断计算，并把该事实写入 `stderr`。

抽样选取必须是确定性的，不得使用随机数，以免破坏重复执行一致性。不得在没有批内自证的情况下按批次最大截止键一次截断后复用，也不得把自证降级为一次性的开发期验证。

算法**不是** walk-forward 结构、无法做出等价一次性计算时，允许逐条重算，但交付时必须
显式声明这一点，并给出预估 Request 条数与单次计算耗时，供平台在收包阶段确定回测预算。
不得不声明就交付逐条实现。

该许可只影响算法内部如何计算，不改变第 5 节「单个 Request 的结果不得因批次大小、批次切分或输入顺序改变」这一不变量，也不放宽任何截止键定位规则。

### 6.4 强制性能自测与交接证据

算法交付方必须在交付前完成性能自测。平台不再接收“接口先交付、入库时再由平台优化”这种
工作方式；性能不达标属于交付未完成，与字段缺失或结果不合约同级处理。

自测必须使用冻结 Blackbox 环境、真实 DataBridge 输入和真实平台 Request 语义，至少覆盖：

1. 单条 `predict`；
2. 一个按时间升序的 100 条 `backtest` 批次；正式区间不足 100 条时使用全部合格 Request；
3. 一个乱序 100 条批次，证明输入顺序不改变逐条结果；
4. 从正式回测起点到拟议 `gray_target_start` 的完整区间，并在一次 `backtest` 调用中完成；
5. 每批首条、中间一条和末条的独立截断复算，内部方向、score/probability 和最终方向全部一致；
6. 同一批次至少重复三次，记录每次墙钟时间、退出码、输出行数和峰值 RSS。

当前平台性能准入线如下；Runtime Profile 的更宽超时是故障熔断上限，不能代替交付准入线：

| 项目 | 通过标准 |
|---|---:|
| 单条 `predict` | 每次不超过 120 秒 |
| 100 条 `backtest` | 每次不超过 600 秒 |
| 完整正式区间 | 总墙钟不超过 1800 秒 |
| 单个算法子进程峰值 RSS | 不超过 4 GiB |
| 批内自证 | 每批均通过，`fallback_used=false` |

任一项超限、只给平均值而不提供逐次结果、使用合成 sample 代替真实输入、没有覆盖完整区间，
或自证失败后依赖逐条 fallback 才完成，都必须先由算法交付方优化并重新自测，不能把优化工作
转交平台入库人员。算法确实不是 walk-forward 时也必须满足同一准入线；“无法一次性计算”不是
性能豁免。

交接时在两文件 delivery 目录**之外**提供
`{scheme_id}.performance.json`，至少记录：scheme ID、算法版本、测试时间、CPU/内存、Python
及关键包版本、四份输入摘要、Request 数和日期边界、三次逐项耗时、完整区间分批、峰值 RSS、
批内自证样本与 `fallback_used`。该文件是交接证据，不是 Contract 1.0 delivery 内容；不得放入
只允许 `.py + .json` 的 Intake 目录。测试机器明显强于平台参考机（当前 4 vCPU / 14 GiB）时，
交付方必须在不强于参考资源的约束下复测，不能用更强机器掩盖生产成本。

Contract 1.0 使用当前快照加截止键隔离后续行，不提供历史时点修订版本回放。这个回测口径可以验证同一当前快照下的 as-of 逻辑，不能宣称历史 vintage PIT。

### 6.5 一次性结果供平台分区复用的条件

平台可能把一次完整 batch 的结果按 `target_date` 分成 canonical backtest 和 `gray_live` 两段，以免对相同业务结果重复计算。上游不需要增加 Metadata 字段或第三个交付文件，但只有同时满足第 5 节的批次不变量、第 6.3 节的逐 Request 截止和本节的完整性能/自证证据时，平台才会把 batch Result 认定为可复用核心结果。

一次性计算不得依赖晚于单条 Request cutoff 的固定 `source_end`、未来 test window、跨样本未来标签、全区间 selector/calibration 或上一条 Request 遗留的进程状态。批内首/中/末独立复算必须证明方向及算法内部 score/probability 等价；仅最终方向碰巧一致不够。平台分区只改变结果进入 backtest 或 gray-live 的持久化阶段，不改变 Request、算法、结果或截止语义。

---

## 7. 生成 Result

Result 固定回传 `request_id`、三个标准日期和 `predicted_direction` 五个字段；每个 Request 恰好
一行且保持输入顺序。`predicted_direction` 只允许 `-1/0/1`：JSON 使用整数，CSV 使用对应文本。
三个 cutoff 不进入 Result，异常、缺数或低置信度不得伪装成 `0`。标准结果语义见
[Blackbox Contract](../architecture/SCHEME_CONTRACT.md#6-标准结果)。

## 8. Output、日志、失败和确定性

- 平台提供尚不存在的 `--output` 路径。
- 全部 Request 成功时退出码为 `0`；任一 Request 失败时整体退出码必须非 `0`。
- 业务结果只写入 `--output`，日志和错误只写入 `stderr`，`stdout` 保持为空。
- 成功时先完成全部计算和校验，再通过同目录临时文件原子替换 `--output`。
- 失败时清理本次临时文件，不得留下完整或部分 Output。
- 如算法使用随机过程，必须固定随机状态；相同环境、数据、Request 的结果必须一致。
- 脚本不得保存跨 Request、跨批或跨进程状态。

失败条件至少包括：空批次、缺字段、额外字段、非法日期、重复 ID、缺少实际消费文件、截止键不存在、截断后无数据、非法业务值和无法生成合法方向。异常、缺数、下载失败或低置信度不能伪装成 `predicted_direction=0`。

---

## 9. 上游自验

先确认实现只使用第 1.2 节列出的 Python 和关键包，再用第 3 节真实下载并校验通过的 `sample_data/` 执行：

```bash
python {scheme_id}.py --help

python {scheme_id}.py predict \
  --request request.json \
  --data-dir ./sample_data \
  --output prediction.json

python {scheme_id}.py backtest \
  --requests requests.csv \
  --data-dir ./sample_data \
  --output backtest.csv
```

最低自验范围：

1. 四份真实数据下载成功、无旧文件混用，并通过第 2–3 节 Schema、时间键和摘要检查；
2. Metadata、两个文件名、任务组合和 `name/owner/description` 全部合法，delivery 中没有第三个文件；
3. `--help`、`predict` 和 `backtest` 均可执行；成功时 stdout 为空，Result 数量、字段和输入顺序一致；
4. 同一 Request 在 predict/backtest、不同批次切分、乱序和重复执行下结果一致，截止键之后的合法行不
   改变当前结果；
5. 依赖周历时，逐条确认 `daily_cutoff_key` 唯一映射到 Request 的 `weekly_cutoff_key`；
6. walk-forward 一次性计算按第 6.3 节完成每次调用首/中/末独立复算；性能按第 6.4 节覆盖单条、100 条、
   乱序和完整区间，且 `fallback_used=false`；
7. 第 8 节每类失败均返回非零、stderr 有错误、stdout 为空且不产生 Output；
8. 保存输入摘要、Request、日期边界、逐次耗时和峰值 RSS；平台输入摘要不同时先标记
   `data_vintage_mismatch`，在平台选定 generation 上重跑后再比较。

准确率等算法效果门槛由当前方案的业务验收要求单独规定，不在本通用接口 SOP 中统一设定。

---

## 10. 最终交付检查

提交前确认：

- [ ] delivery 只有同名 `{scheme_id}.py + {scheme_id}.json`，Metadata 身份、任务组合和三个展示字段合法；
- [ ] 没有平台输入声明、审批字段、凭据、下载逻辑、网络/数据库访问或额外代码/模型/数据依赖；
- [ ] DataBridge 四文件输入通过校验并保存 generation 摘要，且没有进入 delivery；
- [ ] 每个 Request 按自身 cutoff 独立截断，predict/backtest、分批、变序、重复和未来行隔离自验通过；
- [ ] walk-forward 批内自证和第 6.4 节性能准入全部通过，`performance.json` 位于 delivery 目录外；
- [ ] Result、退出码、stdout/stderr 和失败无 Output 行为符合第 7–8 节；
- [ ] 输入 vintage 不同的结果没有被宣称为算法差异；
- [ ] 已明确两文件交付不授予平台 activation、写入或调度权限。

全部完成后，只提交 `{scheme_id}.py` 和 `{scheme_id}.json`。
