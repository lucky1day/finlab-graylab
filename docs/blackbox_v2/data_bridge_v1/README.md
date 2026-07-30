# DataBridge V1 数据契约与样例

**文档状态**：`CURRENT`

**目标读者**：上游算法和平台数据接入人员

**最后核验日期**：2026-07-30

本目录提供 `data-bridge-v1` 的文档入口和三份脱敏结构样例。样例中的日期、周期键和业务值全部为合成值，不来自生产数据。

## 权威来源

机器可校验的最低兼容字段基线：

- [`shared/blackbox_v2/data_bridge_v1_schema.json`](../../../shared/blackbox_v2/data_bridge_v1_schema.json)

当前 Schema SHA256：

```text
f959777b7f251937b6364843a81d8eb696072ca7671b1306c368aa0f3cf735dc
```

机器 Schema 不是永久完整表头。基线字段必须存在且相对顺序不变；DataBridge 可以在不改变 `data-bridge-v1` 的情况下增加业务列。本目录的 [`manifest.json`](manifest.json) 和样例不能覆盖机器 Schema，也不能把制作时点的列数提升为平台限制。

## 三类文件

| 文件 | 第一列截止键 | 样例 |
|---|---|---|
| `daily_output.csv` | `date` | [daily_output.sample.csv](samples/daily_output.sample.csv) |
| `weekly_output.csv` | `week_id` | [weekly_output.sample.csv](samples/weekly_output.sample.csv) |
| `monthly_output.csv` | `month_id` | [monthly_output.sample.csv](samples/monthly_output.sample.csv) |

三份样例保留制作时点的最低兼容字段基线，只提供两行合成数据。除截止键和前两个业务字段外，其余业务字段留空，用于展示空值、字符串周期键和宽表读取方式。真实 DataBridge 会随指标接入增加业务列，算法必须按字段名选列并忽略未使用的新增业务列。

## 可选平台周历

依赖日期到平台周键映射的算法还必须从 DataBridge 下载：

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

`api_wind_date.csv` 是 `api-wind-date-v1` 平台输入制品，不属于上表
三频父 Schema，也不改变 `data-bridge-v1` 的三文件 generation。
上游可以把 DataBridge 下载文件用于本地自测，但正式 Blackbox
delivery 仍然只能包含同名 `.py + .json`。平台运行时只有在 Intake
显式声明 `--platform-input api-wind-date-v1` 后，才会在只读
`--data-dir` 中提供规范化日历。

上游自测与平台验收必须使用同一三频 generation 和相同规范化日历
摘要；三频或日历任一摘要不同，结果差异先标记
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

平台运行时始终通过 `--data-dir` 提供只读三频 Snapshot；全量 current 的管理约定见 [`data/data_bridge/README.md`](../../../data/data_bridge/README.md)。

需要周历的方案会在同一运行视图中额外看到只读
`api_wind_date.csv`。算法不得访问 DataBridge、数据库、交付目录旁
文件或内嵌日历作为运行时 fallback。

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
```

算法的完整读取和逐 Request 截止规则以[上游交付 SOP](../../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)为准。
