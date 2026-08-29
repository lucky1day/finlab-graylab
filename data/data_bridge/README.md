# DataBridge Current Data

平台将通过校验并达到连续两轮稳定的五份输入整体发布到 `current/`：

```text
current/
├── daily_output.csv
├── weekly_output.csv
├── monthly_output.csv
├── api_wind_date.csv
└── factor_catalog.csv
```

`current/` 是运行期目录，不进入 Git。算法不得硬编码该路径；新版 Blackbox V2 由平台把五份文件复制到单次运行的只读 `--data-dir`，算法只读取自己需要的文件。

平台只保留当前版本。刷新失败时继续保留上一份完整 current，历史修订轨迹由数据库承担。

`data-bridge-v1` 不固定完整三份因子宽表的总列数。机器 Schema 维护存量列基线；`api_wind_date.csv` 固定为 `rdate,week_id`，`factor_catalog.csv` 固定为 `indicators_code,frequency,factor_version`。全部五份文件都会参与 generation 内容校验、摘要和 Blackbox V2 Snapshot identity。

版本化资产：

- 机器 Schema：[`shared/blackbox_v2/data_bridge_v1_schema.json`](../../shared/blackbox_v2/data_bridge_v1_schema.json)
- 文档说明和脱敏结构样例：[`docs/blackbox_v2/data_bridge_v1/`](../../docs/blackbox_v2/data_bridge_v1/README.md)

完整业务数据、刷新 staging、previous 和单次算法 Snapshot 均属于运行期资产，不得复制到 `docs/` 或提交到 Git。
