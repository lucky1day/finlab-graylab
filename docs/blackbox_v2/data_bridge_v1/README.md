# DataBridge V1 数据契约与样例

**文档状态**：`CURRENT`

**目标读者**：上游算法和平台数据接入人员

**最后核验日期**：2026-07-20

本目录提供 `data-bridge-v1` 的文档入口和三份脱敏结构样例。样例中的日期、周期键和业务值全部为合成值，不来自生产数据。

## 权威来源

机器可校验的唯一 Schema：

- [`shared/blackbox_v2/data_bridge_v1_schema.json`](../../../shared/blackbox_v2/data_bridge_v1_schema.json)

当前 Schema SHA256：

```text
f959777b7f251937b6364843a81d8eb696072ca7671b1306c368aa0f3cf735dc
```

本目录的 [`manifest.json`](manifest.json) 和样例不能覆盖或扩展机器 Schema。两者不一致时必须修正文档资产，不能据此接受不符合机器 Schema 的输入。

## 三类文件

| 文件 | 截止键 | V1 列数 | 样例 |
|---|---|---:|---|
| `daily_output.csv` | `date` | 774 | [daily_output.sample.csv](samples/daily_output.sample.csv) |
| `weekly_output.csv` | `week_id` | 575 | [weekly_output.sample.csv](samples/weekly_output.sample.csv) |
| `monthly_output.csv` | `month_id` | 123 | [monthly_output.sample.csv](samples/monthly_output.sample.csv) |

三份样例均保留完整 V1 表头，只提供两行合成数据。除截止键和前两个业务字段外，其余业务字段留空，用于展示空值、字符串周期键和宽表读取方式。

## 正确用途

- 检查固定文件名和完整表头。
- 编写 CSV 读取、字段选择和截止截断代码。
- 确认 `week_id`、`month_id` 按字符串读取。
- 在没有全量数据时完成接口层烟雾测试。

## 禁止用途

- 不用于训练模型、回测或算法效果比较。
- 不用于推断生产数据起点、终点、行数、分布或空值比例。
- 不用于判断 DataBridge 当日是否刷新成功。
- 不得把样例路径硬编码进上游脚本。
- 不得从样例中的 `week_id` 或 `month_id` 自行推导平台 Request。

平台运行时始终通过 `--data-dir` 提供只读三频 Snapshot；全量 current 的管理约定见 [`data/data_bridge/README.md`](../../../data/data_bridge/README.md)。

## 读取示例

```python
from pathlib import Path

import pandas as pd


data_dir = Path("<platform-provided-data-dir>")

daily = pd.read_csv(data_dir / "daily_output.csv", dtype={"date": "string"})
weekly = pd.read_csv(data_dir / "weekly_output.csv", dtype={"week_id": "string"})
monthly = pd.read_csv(data_dir / "monthly_output.csv", dtype={"month_id": "string"})
```

算法的完整读取和逐 Request 截止规则以[上游交付 SOP](../../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)为准。
