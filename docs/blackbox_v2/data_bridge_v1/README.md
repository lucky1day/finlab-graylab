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

机器 Schema 是最低兼容基线，不是永久完整表头；删除、改名或重排基线字段必须创建新的 `data_schema_version`，增加业务列不需要改变现有 Schema。样例不能覆盖机器合同或把制作时点列数提升为限制。

## 五份标准文件

| 文件 | 第一列截止键 | 样例 |
|---|---|---|
| `daily_output.csv` | `date` | [daily_output.sample.csv](samples/daily_output.sample.csv) |
| `weekly_output.csv` | `week_id` | [weekly_output.sample.csv](samples/weekly_output.sample.csv) |
| `monthly_output.csv` | `month_id` | [monthly_output.sample.csv](samples/monthly_output.sample.csv) |
| `api_wind_date.csv` | `rdate` | [api_wind_date.sample.csv](samples/api_wind_date.sample.csv) |
| `factor_catalog.csv` | 不适用 | [factor_catalog.sample.csv](samples/factor_catalog.sample.csv) |

三份因子样例保留制作时点基线，只提供两行合成数据。实际读取规则由上游合同定义，样例不是生产表头或数据水位。

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

## generation 与运行视图

producer 独立校验五文件的 schema、freshness、cutoff 和完整性，达到连续两轮稳定后发布 generation 并一次构建 ready snapshot。任何新 publish 开始前先撤销旧 ready 指针，完成后原子发布新指针；发布失败保留原完整 current，但消费者不得绕过 ready gate 回退旧 generation。

方案只读取已有 receipt，核对 producer seal 并将匹配文件稳定复制为私有只读运行视图，不重新解析或验证 generation CSV；进程前后仍须检查私有输入未被篡改。派生状态的私有回测与发布边界见[平台 SOP](../../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#51-增量方案的显式预热重建)。

升级前四文件 receipt 仅供 `legacy_v1` 存量读取，`algorithm_managed` 必须等待五文件 ready generation；五文件发布失败不得破坏旧完整 current。历史运行的输入身份由已保存证据追溯，不能将“保留 current”理解成允许任意版本复用。

算法按平台传入的 `--data-dir` 读取；文件字段、周键、Request 截止和输入不同代时的比较规则以[上游交付 SOP 第 4—5 节](../../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#4-databridge-五文件输入)为准。源周历只读导出路径为 `/api/export/tables/api_wind_date/csv/`，正式算法不得绕过快照自行下载。

## 样例用途与限制

样例仅用于文件名、键类型、基线兼容、字段选择和 cutoff 截断的接口自测；不用于训练、效果回测或生产 freshness 验收，也不能推断真实起止日期、行列数、分布或空值比例。算法不得硬编码样例路径，或从合成 week_id/month_id 推导平台 Request。

运行期文件位置与 Git 边界见[data/data_bridge/README.md](../../../data/data_bridge/README.md)。完整读取与按版本选列示例见[上游交付 SOP](../../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md#4-databridge-五文件输入)，此处不维护第二份读取合同。

平台输入实现变更后，按[公共验证矩阵](../../onboarding/README.md#可复用测试矩阵)检查 DataBridge 合同；真实发布仍须有目标机 producer-ready 证据，样例测试通过不能替代。
