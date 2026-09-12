# DataBridge V1 数据契约与样例

**文档状态**：`CURRENT`

**目标读者**：上游算法和平台数据接入人员

本目录提供 `data-bridge-v1` 的文档入口、算法合同样例和五份脱敏结构样例。样例中的日期、周期键和业务值全部为合成值，不来自生产数据。

算法合同样例：

- [metadata.sample.json](samples/metadata.sample.json)
- [request.sample.json](samples/request.sample.json)
- [requests.sample.csv](samples/requests.sample.csv)
- [prediction.sample.json](samples/prediction.sample.json)
- [backtest.sample.csv](samples/backtest.sample.csv)
- [performance.sample.json](samples/performance.sample.json)

## 权威来源

机器可校验的最低兼容字段基线：

- [`shared/blackbox_v2/data_bridge_v1_schema.json`](../../../shared/blackbox_v2/data_bridge_v1_schema.json)

当前 Schema SHA256：

```text
130c1acbb1cd13d49155334d3bba57f43896bf26d1ec7983c188eb93a7d0cf28
```

机器 Schema 不是永久完整表头。基线字段必须存在且相对顺序不变；DataBridge 可以在不改变 `data-bridge-v1` 的情况下增加业务列。本目录的样例不能覆盖机器 Schema，也不能把制作时点的列数提升为平台限制。

## 五份标准文件

| 文件 | 第一列截止键 | 样例 |
|---|---|---|
| `daily_output.csv` | `date` | [daily_output.sample.csv](samples/daily_output.sample.csv) |
| `weekly_output.csv` | `week_id` | [weekly_output.sample.csv](samples/weekly_output.sample.csv) |
| `monthly_output.csv` | `month_id` | [monthly_output.sample.csv](samples/monthly_output.sample.csv) |
| `api_wind_date.csv` | `rdate` | [api_wind_date.sample.csv](samples/api_wind_date.sample.csv) |
| `factor_catalog.csv` | 不适用 | [factor_catalog.sample.csv](samples/factor_catalog.sample.csv) |

三份因子样例保留制作时点的最低兼容字段基线，只提供两行合成数据；日历样例固定为 `rdate,week_id`。真实 DataBridge 会随指标接入增加业务列，算法必须按字段名选列并忽略未使用的新增业务列。

## 因子版本与存量输入保护

- `factor_version` 是因子所属批次，不是算法版本或 generation；格式为 `V<major>.<minor>`。
  `indicators_code` 全局唯一、版本归属不可变，源表维护由数据所有者负责，平台只读消费。
- catalog 只包含本 generation 宽表实际提供的因子，按 daily、weekly、monthly 及各表列序排列；
  每频率的代码集合必须与宽表非主键列精确一致。非法版本、重复代码或集合不符均拒绝发布。
- 一个版本首次正式发布即封版。后续 generation 不得改归属、增加或删除该版本成员；新增因子进入新版本。
- 新 Intake 固定使用 `algorithm_managed`，读取完整五文件，由算法自行选择版本与因子；平台不保存每方案因子清单。
- 存量 `legacy_v1` 按机器 Schema 冻结的精确列集合及相对顺序读取共享视图，不能动态解释成“当前所有 V1.0”。
  该视图沿用旧四文件消费合同；每 generation 只构建一次，全部实际因子均为 V1 时复用相同 snapshot，
  不按方案重复过滤。W4 Native 继续使用共享输入构建边界，不扩大其原字段集合。
- 因子版本目录参与输入身份，不参与 Request 截止、日历或 target 日期计算。
  目标机 current 与实际可回滚 release 都必须支持五文件和存量输入保护，才允许开放新版本因子。
  generation 发布、源表变更与服务操作须独立授权；历史升级计划不构成操作入口。

## 平台周历

DataBridge generation 固定提供：

```text
api_wind_date.csv
```

只读下载入口为：

```text
/api/export/tables/api_wind_date/csv/
```

该文件精确包含 `rdate,week_id` 两列。`rdate` 是唯一、严格升序的
`YYYY-MM-DD`；`week_id` 是六位字符串形式的平台业务键。它不是 ISO
周，也不保证数值连续，算法不得自行换算、加减或推导相邻周。

`api_wind_date.csv` 是平台周历；`factor_catalog.csv` 按宽表实际列顺序提供
`indicators_code,frequency,factor_version`。上游可以把五份文件用于本地自测，但正式 Blackbox delivery 仍然
只能包含同名 `.py + .json`。平台运行时始终在只读 `--data-dir`
提供日历；算法需要时读取，不需要时不读取。

上游自测与平台验收必须使用同一五文件 generation；任一文件
摘要不同，结果差异先标记
`data_vintage_mismatch`，需要精确比较时必须在平台选定的同代输入上
重跑。正式生产运行继续使用当天最新且已封存的 generation，不永久
冻结自测数据版本。

## 正确用途

- 检查固定文件名、第一列时间键和基线字段兼容性。
- 编写 CSV 读取、字段选择和截止截断代码。
- 确认 `week_id`、`month_id` 按字符串读取。
- 在没有全量数据时完成接口层烟雾测试。

## 禁止用途

- 不用于训练模型、回测或算法效果比较。
- 不用于推断生产数据起点、终点、行数、列数、分布或空值比例。
- 不用于判断 DataBridge 当日是否刷新成功。
- 不得把样例路径硬编码进上游脚本。
- 不得从样例中的 `week_id` 或 `month_id` 自行推导平台 Request。

平台运行时始终通过 `--data-dir` 提供同代只读五文件 Snapshot；全量 current 的管理约定见 [`data/data_bridge/README.md`](../../../data/data_bridge/README.md)。算法不得访问 DataBridge、数据库、交付目录旁文件或内嵌日历作为运行时 fallback。

## 读取示例

```python
from pathlib import Path

import pandas as pd


data_dir = Path("<platform-provided-data-dir>")

daily = pd.read_csv(data_dir / "daily_output.csv", dtype={"date": "string"})
weekly = pd.read_csv(data_dir / "weekly_output.csv", dtype={"week_id": "string"})
monthly = pd.read_csv(data_dir / "monthly_output.csv", dtype={"month_id": "string"})
calendar = pd.read_csv(
    data_dir / "api_wind_date.csv",
    dtype={"rdate": "string", "week_id": "string"},
)
catalog = pd.read_csv(data_dir / "factor_catalog.csv", dtype="string")

daily_columns = catalog.loc[
    (catalog["frequency"] == "daily")
    & catalog["factor_version"].isin(["V1.0"]),
    "indicators_code",
].tolist()
missing = [column for column in daily_columns if column not in daily.columns]
if missing:
    raise ValueError(f"missing daily factor columns: {missing[:10]}")
daily_features = daily.loc[:, daily_columns]
```

算法的完整读取和逐 Request 截止规则以[上游交付 SOP](../../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)为准。
