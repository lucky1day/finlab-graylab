# 上游算法黑盒 V2 交付 SOP（V1 试运行版）

请依次完成：确认交付物、按契约实现预测与回测、完成交付前验证。

## 1. 你应该做什么

### 1.1 交付一个算法脚本

一个算法方案只交付两个文件：

```text
{scheme_id}.py
{scheme_id}.json
```

- `{scheme_id}.py` 是唯一可执行文件。
- `{scheme_id}.json` 只用于说明方案身份。
- 两个文件的 `{scheme_id}` 必须完全一致。
- 一个脚本只对应一个 `target_tenor + task_type + horizon` 组合。

不得额外交付依赖安装文件、模型文件、配置文件、辅助 Python 模块或项目目录。如需训练，必须由脚本在运行时基于当次截止数据完成。

### 1.2 让脚本完成两类任务

同一个 `.py` 必须支持：

- `predict`：接收一个预测 Request，输出一条预测结果；
- `backtest`：接收多个回测 Request，逐条计算并输出回测结果。

预测和回测必须复用同一套数据处理、算法逻辑和方向映射。

### 1.3 遵守脚本运行边界

脚本只从平台传入的 CSV 读取数据，只将业务结果写入 `--output`。脚本不得：

- 下载或更新业务数据；
- 访问网络或绕过 CSV 读取其他数据源；
- 安装或动态加载新依赖；
- 自行计算交易日、预测日或目标日；
- 硬编码本机绝对路径。

---

## 2. 你应该怎么做

### 第一步：使用平台指定环境

V1 使用 Python 3.12 和环境标识 `forecast_env`。开始前，平台会提供：

- 冻结后的依赖及版本；
- CPU、内存和超时限制；
- 环境自检方式。

你需要在该环境中完成脚本适配，不能要求平台为单个方案增加私有包。

### 第二步：读取平台 CSV

平台通过 `--data-dir` 传入数据目录：

```text
<data-dir>/daily.csv
<data-dir>/weekly.csv
<data-dir>/monthly.csv
```

- 平台在运行前拉取最新数据并替换旧文件。
- 三个文件都会存在，你可以只读取方案需要的文件。
- `--data-dir` 是只读目录，不得向其中写入文件。

CSV 的字段、编码、日期列、空值、重复行和排序规则，以平台发布的《数据桥 CSV 输入契约 V1》和三份脱敏样例为准。这些资料未就绪时，不开始交付验收。

### 第三步：按 `feature_date` 截断数据

每个 Request 都包含独立的 `feature_date`。在执行算法前，你必须：

1. 读取当前方案需要的 CSV；
2. 按输入契约指定的日期列截断至当前 `feature_date`；
3. 只使用截断后的数据执行算法。

回测时必须对每行 Request 分别截断。不得按整个批次的最大 `feature_date` 一次截断后复用，也不得使用晚于当前 `feature_date` 的数据。

### 第四步：填写 Metadata

`{scheme_id}.json` 必须为 UTF-8 JSON，且只包含以下字段：

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

- `schema_version`：V1 固定为字符串 `1.0`。
- `scheme_id`：与两个交付文件名一致。
- `name`：方案名称。
- `algorithm_version`：算法侧可追溯版本。
- `target_tenor`：本脚本唯一预测的目标期限。
- `task_type`：`T+1`、`T+5`、`weekly_point`、`weekly_average` 或 `monthly`。
- `horizon`：大于 `0` 的整数。
- `target_rule`：方向比较基准的文字说明。

Metadata 只描述方案身份，不得保存可变阈值、特征列表、模型参数、输入路径或运行开关。

### 第五步：实现两个命令

```bash
python {scheme_id}.py predict --request request.json --data-dir <data-dir> --output prediction.json
python {scheme_id}.py backtest --requests requests.csv --data-dir <data-dir> --output backtest.csv
```

- `predict` 使用 `--request` 读取一个 JSON Request，并恰好输出一条结果。
- `backtest` 使用 `--requests` 读取 CSV Request，每行独立计算。
- `--data-dir` 指定 CSV 目录，`--output` 指定结果文件。
- 批量结果顺序必须与 Request 输入顺序一致。

### 第六步：读取 Request

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

- `request_id` 必须非空，且在同一批次内唯一。
- 三个日期必须为 `YYYY-MM-DD`。
- 日期均由平台给定；你只校验格式，不得修改、顺延、回退或重新推导。

### 第七步：生成 Result

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

- V1 Result 只包含以上五个字段，并原样回传 Request 的四个字段。
- JSON 和 CSV 均使用 UTF-8。
- 每个 Request 必须恰好对应一条结果。
- `predicted_direction` 必须是整数 `-1`、`0` 或 `1`。
- `1` 表示目标收益率高于 `target_rule` 基准；`-1` 表示低于基准；`0` 表示相同或中性。
- V1 默认每个 Request 都产生方向，运行异常不得转换成方向 `0`。

### 第八步：处理日志和失败

- 全部 Request 成功时退出码为 `0`；任一 Request 失败时整体退出码非 `0`。
- 业务结果只写入 `--output`；日志和错误信息写入 `stderr`。
- 缺少输入、字段非法、日期非法、`request_id` 重复或方向非法时必须失败。
- 失败时不得留下完整或部分结果文件。
- 应先完成全部计算和校验，再通过临时文件原子替换到 `--output`。

---

## 3. 你应该怎么验证

### 3.1 验证环境和命令

```bash
python --version
python {scheme_id}.py --help
```

确认 Python 版本为 3.12，`--help` 正常退出并显示 `predict` 和 `backtest`。

### 3.2 验证单点预测

```bash
python {scheme_id}.py predict \
  --request request.json \
  --data-dir ./sample_data \
  --output ./prediction.json
```

确认退出码为 `0`，`prediction.json` 只包含一个对象，且字段和方向均合法。

### 3.3 验证批量回测

```bash
python {scheme_id}.py backtest \
  --requests requests.csv \
  --data-dir ./sample_data \
  --output ./backtest.csv
```

确认输入和输出 Request 数量一致、`request_id` 无缺失无重复，且顺序一致。

### 3.4 验证没有使用未来数据

在样例 CSV 中保留晚于某个 `feature_date` 的数据，修改这些未来行后重新运行该 Request。两次的业务结果必须完全一致。

### 3.5 验证重复执行

在相同环境中，使用相同 CSV 和 Request 连续执行两次。`request_id`、三个日期和 `predicted_direction` 必须完全一致。

### 3.6 验证失败场景

分别使用缺失 CSV、缺少字段、非法日期和重复 `request_id` 的输入，确认：

- 进程退出码非 `0`；
- 错误信息写入 `stderr`；
- 不产生成功结果文件；
- 不使用方向 `0` 代替运行失败。

### 3.7 交付前最终确认

- [ ] 交付物只有一个 `.py` 和一个 `.json`
- [ ] 文件名与 Metadata 中的 `scheme_id` 完全一致
- [ ] 一个脚本只对应一个 `target_tenor + task_type + horizon`
- [ ] 脚本能在平台 Python 3.12 环境中直接运行
- [ ] `--help`、`predict` 和 `backtest` 都可正常执行
- [ ] 每个 Request 都独立按 `feature_date` 截断
- [ ] 每个 Request 恰好输出一个 `-1`、`0` 或 `1`
- [ ] 预测与回测使用同一算法逻辑和方向映射
- [ ] 结果只写入 `--output`，日志只写入 `stderr`
- [ ] 失败时非零退出，且不留下结果文件
- [ ] 相同输入重复执行时结果完全一致

全部验证通过后，再将 `{scheme_id}.py` 和 `{scheme_id}.json` 交付给平台。
