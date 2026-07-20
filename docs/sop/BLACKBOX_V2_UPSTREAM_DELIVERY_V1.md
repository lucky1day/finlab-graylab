# Blackbox V2 上游交付 SOP（Contract 1.0）

**文档状态**：`CURRENT`
**适用运行时**：`blackbox_v2`
**目标读者**：上游算法工程师
**最后核验日期**：2026-07-20

本文中的 `Blackbox V2` 是运行时代际，`schema_version=1.0` 是交付接口合同版本，`data-bridge-v1` 是三频数据 Schema。运行环境由平台另行发布；算法侧不需要了解平台内部管理和生命周期。

## 1. 你应该做什么

### 1.1 交付两个文件

一个算法方案只交付：

```text
{scheme_id}.py
{scheme_id}.json
```

- `{scheme_id}.py` 是唯一可执行文件；`{scheme_id}.json` 只描述方案身份和固定任务口径。
- 两个文件名以及 Metadata 中的 `scheme_id` 必须完全一致。
- `scheme_id` 必须匹配 `^[a-z][a-z0-9_]*$`。
- 一个脚本只对应一个 `target_tenor + task_type + horizon` 组合。
- `target_tenor` 只允许 `1Y`、`3Y`、`5Y`、`7Y` 或 `10Y`。
- 不得额外交付依赖文件、模型文件、配置文件、辅助模块或项目目录。

如算法需要训练，训练逻辑和固定参数必须包含在 `.py` 中，并且只能使用当前 Request 允许的数据。

### 1.2 让同一个脚本支持两个命令

```bash
python {scheme_id}.py predict --request request.json --data-dir <data-dir> --output prediction.json
python {scheme_id}.py backtest --requests requests.csv --data-dir <data-dir> --output backtest.csv
```

- `predict`：读取一个 Request，输出一条预测结果。
- `backtest`：读取平台传入的一批 Request，每个 Request 输出一条结果；Contract 1.0 每批为一至 100 条。
- 两个命令必须复用相同的数据处理、算法逻辑和方向映射。
- 单个 Request 的结果不得因批次大小、批次切分或输入顺序改变。

这里的单批上限不是完整回测总量上限。完整历史区间可以超过 100 条；平台会在同一 scheme version、DataBridge snapshot 和 generation 下，把完整 Request 序列拆成多个不超过 100 条的批次并多次调用 `backtest`。上游脚本只处理当前收到的批次，不得保存跨批状态，也不得要求把完整区间一次性传入。任一 Request 在不同批次大小、分区边界或输入顺序下都必须得到相同结果。

### 1.3 只在指定范围内运行

脚本只使用指定运行环境和 Request，只从 `--data-dir` 读取业务数据，只向 `--output` 写入业务结果。

脚本不得：

- 访问网络、数据库或其他业务数据源；
- 安装依赖、修改运行环境或动态加载交付包之外的代码；
- 写入 `--data-dir`，或者硬编码本机绝对路径；
- 自行计算或修改平台给出的日期和截止键；
- 将业务结果或调试内容写入 `stdout`。

日志和错误信息统一写入 `stderr`。业务运行期间 `stdout` 必须为空。

---

## 2. 你应该怎么做

### 第一步：确认运行材料

开始实现前，确认已经取得：

1. 冻结运行环境清单、资源限制和环境自检命令；
2. `daily_output.csv`、`weekly_output.csv`、`monthly_output.csv` 三份脱敏样例；
3. 合法的单点 Request 和批量 Request 样例。

仓库内的 `data-bridge-v1` Schema 入口和三频脱敏结构样例见 [DataBridge V1 数据契约与样例](../blackbox_v2/data_bridge_v1/README.md)。

Python 和第三方包版本只以冻结运行环境清单为准。不得根据环境名称猜测版本，也不得要求为单个方案临时增加私有包。

### 第二步：选择固定任务组合

Contract 1.0 只允许以下组合：

| `task_type` | `horizon` | `target_rule` | 业务含义 |
|---|---:|---|---|
| `T+1` | 1 | `target_date_yield_vs_feature_date_yield` | 第 1 个后续交易日相对 `feature_date` 的收益率方向 |
| `T+5` | 5 | `target_date_yield_vs_feature_date_yield` | 第 5 个后续交易日相对 `feature_date` 的收益率方向 |
| `weekly_point` | 1 | `target_week_end_yield_vs_feature_week_end_yield` | 下一周频点相对本周频点的收益率方向 |
| `weekly_average` | 1 | `target_week_average_yield_vs_feature_week_average_yield` | 下一周平均收益率相对本周平均收益率的方向 |
| `monthly` | 1 | `target_month_observation_yield_vs_feature_month_observation_yield` | 下一月观测相对本月观测的收益率方向 |

`horizon` 按任务频率计期：日频按后续交易日计数，周频按周频观测计数，月频按月频观测计数。必须整行选择任务组合，不得自由修改 `horizon` 或填写其他 `target_rule`。

### 第三步：填写 Metadata

`{scheme_id}.json` 必须是无 BOM 的 UTF-8 JSON。Contract 1.0 包含以下八个必填字段，并可选提供推荐字段 `description`：

```json
{
  "schema_version": "1.0",
  "scheme_id": "one_y_t5_liq_excess_a_w252_l7_v1",
  "name": "LIQ_EXCESS_A_W252_L7",
  "description": "使用流动性指标和滚动窗口构建特征，通过分类模型判断未来5个交易日1Y国债收益率方向。",
  "algorithm_version": "1.0.0",
  "target_tenor": "1Y",
  "task_type": "T+5",
  "horizon": 5,
  "target_rule": "target_date_yield_vs_feature_date_yield"
}
```

- `schema_version` 固定为字符串 `1.0`。
- `scheme_id` 和 `target_tenor` 必须满足第一节约束。
- `scheme_id` 是算法执行身份；`name` 是当前任务格子内用于区分候选方案的简洁业务名称，两者不要混用。
- `name` 不得重复 `target_tenor`、不得重复 `task_type` 或 `horizon`，也不得追加“方向预测”等已经由任务格子表达的说明。
- `name` 和 `algorithm_version` 必须是非空字符串；`algorithm_version` 不强制使用特定版本格式。
- `description` 是可选的算法逻辑摘要，缺失不阻断 Contract 1.0 交付和自验；但强烈建议提供，方便后续按算法版本回溯。
- `description` 建议简述主要输入、窗口或规则、模型类型以及最终方向形成方式；平台不会根据脚本或名称代写算法逻辑。
- `description` 存在时必须是单段非空纯文本，最多 300 个字符，不得包含换行、HTML 或其他标记文本。
- `task_type`、`horizon` 和 `target_rule` 必须来自第二步的同一行。
- 除可选 `description` 外，不得增加 `frequency`、输入路径、运行开关、可变阈值、特征列表或模型参数。

### 第四步：读取三频 CSV

平台通过 `--data-dir` 提供：

```text
<data-dir>/daily_output.csv
<data-dir>/weekly_output.csv
<data-dir>/monthly_output.csv
```

| 文件 | 时间键 | `data-bridge-v1` 列数 | 时间键规则 |
|---|---|---:|---|
| `daily_output.csv` | `date` | 774 | 可解析为日期，唯一且升序 |
| `weekly_output.csv` | `week_id` | 575 | 六位数字字符串，唯一且升序 |
| `monthly_output.csv` | `month_id` | 123 | 六位数字字符串，唯一且升序 |

- 同一次运行收到的三份文件属于同一份只读快照，运行期间不会被替换。
- 平台始终提供三份文件；算法可以只读取当前方案需要的文件。
- 不得因为未使用的文件存在而失败，也不要求算法主动解析未使用文件。
- 三份文件的表头名称和顺序按当前数据 Schema 版本冻结；业务列只能是有限数值或空值。
- 数据行数、起止区间、业务值和空值可以变化；算法不得假定固定行数、固定起点或固定长度窗口。
- `week_id` 和 `month_id` 必须按字符串读取；不得把 `week_id` 当作 ISO 周，也不得自行换算为日期。
- 表头增删、改名、重排或时间键格式变化属于 Schema 升级，旧版本脚本不得继续运行。

### 第五步：按截止键截断实际消费的数据

每个 Request 都包含三个截止键：

| 文件 | Request 字段 | 定位规则 |
|---|---|---|
| `daily_output.csv` | `daily_cutoff_key` | 将 `date` 规范化为 `YYYY-MM-DD` 后精确定位 |
| `weekly_output.csv` | `weekly_cutoff_key` | 与 `week_id` 字符串精确匹配 |
| `monthly_output.csv` | `monthly_cutoff_key` | 与 `month_id` 字符串精确匹配 |

对每个 Request，算法必须：

1. 读取当前方案实际消费的 CSV；
2. 在每个消费文件中定位对应截止键；
3. 保留第一行至截止键所在行，包含截止键所在行；
4. 只使用截断后的数据执行当前 Request。

缺少实际消费的 CSV、消费文件的截止键不存在、时间键不唯一或截断后没有算法所需数据时，本次运行必须失败。算法只需格式校验 Request 中未消费频率的截止键，不需要读取对应文件验证。

不得用文件最后一行代替截止键，也不得根据 `feature_date` 推导周/月截止键。批量回测必须逐行独立截断，不能按批次最大截止键一次截断后复用。Contract 1.0 使用当前快照加截止键隔离后续行，不提供历史时点修订版本回放。

### 第六步：读取 Request

单点 `request.json`：

```json
{
  "request_id": "predict-20260718-001",
  "predict_date": "2026-07-18",
  "feature_date": "2026-07-17",
  "target_date": "2026-07-24",
  "daily_cutoff_key": "2026-07-17",
  "weekly_cutoff_key": "202628",
  "monthly_cutoff_key": "202607"
}
```

批量 `requests.csv`：

```csv
request_id,predict_date,feature_date,target_date,daily_cutoff_key,weekly_cutoff_key,monthly_cutoff_key
backtest-001,2026-07-17,2026-07-17,2026-07-24,2026-07-17,"202628","202607"
backtest-002,2026-07-18,2026-07-17,2026-07-24,2026-07-17,"202628","202607"
```

Request 规则：

- 必须恰好包含以上七个字段，不接受缺失字段或额外字段。
- `request_id` 必须是非空字符串；平台每批提供一至 100 行，算法必须完整支持该范围，并且校验批内 ID 唯一。超过 100 条由平台切分，不要求算法自行分批。
- 三个日期必须是合法的规范 `YYYY-MM-DD`。
- 日期必须满足 `feature_date <= predict_date <= target_date` 且 `feature_date < target_date`。
- `daily_cutoff_key` 是规范 `YYYY-MM-DD`；周/月截止键是六位数字字符串。
- 日期和截止键由平台生成，算法只校验和使用，不得修改、顺延、回退或重新推导。
- 批量输入顺序就是输出顺序。任一行非法时必须全批失败，不能跳过后继续输出部分结果。

### 第七步：生成 Result

单点 `prediction.json`：

```json
{
  "request_id": "predict-20260718-001",
  "predict_date": "2026-07-18",
  "feature_date": "2026-07-17",
  "target_date": "2026-07-24",
  "predicted_direction": 1
}
```

批量 `backtest.csv`：

```csv
request_id,predict_date,feature_date,target_date,predicted_direction
backtest-001,2026-07-17,2026-07-17,2026-07-24,1
backtest-002,2026-07-18,2026-07-17,2026-07-24,-1
```

- Result 必须恰好包含以上五个字段；三个截止键不写入 Result。
- 每个 Request 恰好对应一条结果，四个 Request 字段必须原样回传。
- 批量输出行数和顺序必须与输入一致。
- JSON 中 `predicted_direction` 必须是整数；CSV 中必须是可解析的 `-1`、`0` 或 `1` 文本。
- `1` 表示高于 `target_rule` 基准，`-1` 表示低于基准，`0` 表示算法给出的有效持平或中性方向。
- 二分类算法可以只输出 `-1` 和 `1`；异常、缺数或低置信度不得转换为 `0`。

平台按解析后的字段和值验收，不要求 JSON 键顺序、缩进、末尾换行或 CSV 换行符逐字节一致。

### 第八步：处理 Output、日志和失败

- 平台提供尚不存在的 `--output` 路径。
- 全部 Request 成功时退出码为 `0`；任一 Request 失败时整体退出码必须非 `0`。
- 业务结果只写入 `--output`，日志和错误只写入 `stderr`，`stdout` 保持为空。
- 成功时先完成全部计算和校验，再通过同目录临时文件原子替换 `--output`。
- 失败时清理本次临时文件，不得留下完整或部分 Output。
- 如算法使用随机过程，必须固定随机状态；相同环境、快照和 Request 的结果必须一致。

---

## 3. 你应该怎么验证

先执行冻结运行环境清单中的自检命令，再执行：

```bash
python {scheme_id}.py --help
python {scheme_id}.py predict --request request.json --data-dir ./sample_data --output prediction.json
python {scheme_id}.py backtest --requests requests.csv --data-dir ./sample_data --output backtest.csv
```

逐项完成：

| 验证项 | 操作 | 通过标准 |
|---|---|---|
| 交付物 | 检查文件数量、命名、Metadata 八个必填字段、可选说明和任务组合 | 只有两个文件，身份和任务组合合法；缺少说明不阻断 |
| 命令与日志 | 执行 `--help`、`predict`、`backtest` 并分别捕获 stdout/stderr | 命令存在；成功运行 stdout 为空 |
| 单点预测 | 使用一个合法 Request 执行 `predict` | 退出码 `0`，恰好一条五字段结果 |
| 批量回测 | 使用至少两个不同截止键执行 `backtest` | 每个 Request 恰好一条结果，数量和顺序一致 |
| 预测/回测一致 | 将同一个 Request 分别交给两个命令 | 五个业务字段完全一致 |
| 后续行隔离 | 修改或追加实际消费文件中位于截止键之后的合法行 | 当前 Request 的五个业务字段不变 |
| 分批与顺序 | 改变批次切分和 Request 顺序后运行并按 ID 对齐 | 每个 Request 的结果不变 |
| 重复执行 | 相同环境、快照和 Request 连续执行两次 | 五个业务字段完全一致 |
| 失败处理 | 测试空批次、缺字段、额外字段、非法日期、重复 ID、缺少消费文件、截止键不存在和截断后无数据 | 非零退出，stderr 有错误，stdout 为空，不产生 Output |

准确率等算法效果门槛由当前方案的业务验收要求单独规定，不在本通用接口 SOP 中统一设定。

全部验证通过后，只交付 `{scheme_id}.py` 和 `{scheme_id}.json`。
