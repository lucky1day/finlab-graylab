# Blackbox V2 上游交付 SOP（Contract 1.0）

**文档状态**：`CURRENT`

**适用运行时**：`blackbox_v2`

**目标读者**：上游算法工程师

**最后核验日期**：2026-07-21

本文是上游算法工程师唯一需要阅读的人类文档。完成开发只需要本文、随包提供的 `data_bridge_v1_schema.json` 和三份脱敏 sample；不需要再阅读仓库内其他文档。

本地开发、训练和效果验证优先使用从统一 DataBridge 下载的真实 DataBridge 数据。三份 sample 只在 DataBridge 暂时不可用时用于读取、选列、截止截断和接口烟雾测试，不能用于训练或效果回测。最终交付物仍然只有同名的 `{scheme_id}.py + {scheme_id}.json`。

本文中的 `Blackbox V2` 是运行时代际，`schema_version=1.0` 是交付接口合同版本，`data-bridge-v1` 是三频数据 Schema；三者不能混作算法版本。

---

## 1. 开始前准备

### 1.1 取得开发材料

开始实现前，需要取得：

1. DataBridge 地址、用户名和密码；
2. `data_bridge_v1_schema.json`；
3. 合法的单点 `request.json` 和批量 `requests.csv` 样例；
4. 离线兜底用的三份脱敏 sample：

```text
samples/daily_output.sample.csv
samples/weekly_output.sample.csv
samples/monthly_output.sample.csv
```

平台运行环境、关键包和资源限制直接列在下一节。不得要求为单个方案临时增加私有包。

### 1.2 平台运行环境与资源限制

Blackbox V2 Contract 1.0 当前固定运行环境：

| 项目 | 平台值 |
|---|---|
| Runtime Profile | `blackbox-v2-v1` |
| Conda 环境 | `forecast_env_blackbox_v1` |
| 平台 | `osx-arm64` |
| Python | Python 3.13.12 |
| 数值与数据 | numpy 2.3.5、pandas 2.3.3、scipy 1.16.3 |
| 机器学习 | scikit-learn 1.8.0、lightgbm 4.6.0、xgboost 3.1.3、catboost 1.2.8 |
| 其他关键包 | joblib 1.5.3、pyarrow 23.0.0、openpyxl 3.1.5 |

环境指纹为：

```text
720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd
```

在装有该 Conda 环境的机器上，可以直接执行以下自检：

```bash
conda run --no-capture-output -n forecast_env_blackbox_v1 python - <<'PY'
import platform
from importlib.metadata import version

packages = (
    "numpy", "pandas", "scipy", "scikit-learn", "lightgbm",
    "xgboost", "catboost", "joblib", "pyarrow", "openpyxl",
)
print("python", platform.python_version())
for package in packages:
    print(package, version(package))
PY
```

资源边界：

| 项目 | 限制 |
|---|---:|
| CPU 线程 | 8 |
| 内存 | 64 GiB |
| `predict` 超时 | 3600 秒 |
| `backtest` 超时 | 14400 秒 |
| 单批 Request | 1 至 100 条 |
| Output 上限 | 50 MiB |
| 日志上限 | 5 MiB |
| 单次运行目录 | 64 MiB、最多 10000 个目录项 |

算法在 sandbox 中运行，不能访问网络或数据库。需要的训练逻辑、模型结构和固定参数必须全部包含在单一 `.py` 文件中。

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

---

## 2. 统一 DataBridge 数据

### 2.1 数据来源为什么必须统一

所有算法工程师使用同一个 DataBridge 导出接口和同一份 `data-bridge-v1` Schema。不得自行写 SQL、拼接其他数据源、复制另一套导出逻辑或手工修改三频文件来贴合算法结果。

开发和生产的区别只有谁来准备数据：

- **开发和自验**：算法工程师在算法运行前，按第 3 节命令从 DataBridge 下载三份真实 CSV 到本地 `sample_data/`。
- **平台运行**：平台准备同一代只读数据并通过 `--data-dir` 提供；算法脚本不得主动连接 DataBridge、网络或数据库。

因此，下载是独立的开发准备动作，不能写进 `{scheme_id}.py` 的 `predict` 或 `backtest` 路径。

### 2.2 三份 CSV

DataBridge 固定提供三种文件名和时间键：

| 文件 | 第一列时间键 | 时间键规则 |
|---|---|---|
| `daily_output.csv` | `date` | 可解析为日期，非空、唯一、升序 |
| `weekly_output.csv` | `week_id` | 六位数字字符串，非空、唯一、升序 |
| `monthly_output.csv` | `month_id` | 六位数字字符串，非空、唯一、升序 |

这三份文件是 CSV，不是 `.xlsx` 工作簿；可以用 Excel 打开查看，但算法必须按 CSV 读取。周、月时间键必须按字符串读取；不得把 `week_id` 当作 ISO 周，也不得自行把 `week_id` 或 `month_id` 换算为日期。

机器权威文件是 `data_bridge_v1_schema.json`。其当前 SHA-256 为：

```text
f959777b7f251937b6364843a81d8eb696072ca7671b1306c368aa0f3cf735dc
```

机器 Schema 中的字段列表是 `data-bridge-v1` 的最低兼容字段基线，不是永久完整表头。DataBridge 会随着新的指标接入而增加业务列，所以三份文件没有固定列数。兼容规则是：时间键始终位于第一列；基线字段必须继续存在且相对顺序不变；新增业务列允许出现，并参与当次数据摘要和快照身份。

上游算法必须按字段名选择自己实际消费的列，启动时明确检查这些列是否存在，并忽略未使用的新增业务列。不得按列位置切片、假定最后一列、要求实际表头与 sample 完全相等，或因为出现未使用的新列而失败。基线字段被删除、改名或改变相对顺序，以及时间键变化，才是不兼容的 Schema 变更。

### 2.3 每天如何更新

平台每天使用统一 DataBridge 导出逻辑全量构建三频数据：日频按日期分段导出后合并，周频和月频导出完整周期数据。平台完成数据校验后，把同一批次的三份文件作为一个 generation 整体原子发布；算法运行只会看到该 generation 的只读副本，不会混用不同批次。

新增指标会在后续导出中形成新增业务列，因此不同日期下载的数据列数可能不同。算法工程师本地下载用于开发验证；正式运行直接读取平台通过 `--data-dir` 提供的当次数据，不需要了解或实现平台内部刷新时间与检查流程。

---

## 3. 从 DataBridge 下载三份测试数据

### 3.1 配置地址和认证信息

向 DataBridge 管理方取得 `/api/` 地址、用户名和密码。不要把真实密码写进本文、Git、交付包或算法代码。

在准备下载的终端执行：

```bash
export DATABRIDGE_API_BASE_URL="<DataBridge 管理方提供的 /api/ 地址>"
export DATABRIDGE_API_USERNAME="<DataBridge 用户名>"
printf "DataBridge password: "
read -r -s DATABRIDGE_API_PASSWORD
printf "\n"
export DATABRIDGE_API_PASSWORD
export DATABRIDGE_END_DATE="<YYYY-MM-DD 数据截止日>"
mkdir -p sample_data
```

`DATABRIDGE_END_DATE` 应填写准备验证的数据截止日。回测需要更长历史时，日频开始日仍使用 `2010-01-01`；算法通过每条 Request 的截止键隔离未来数据。

### 3.2 下载日频文件

接口参数：`frequency=日`。

```bash
curl --fail-with-body --location --retry 3 \
  --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" \
  --data-urlencode "frequency=日" \
  --data-urlencode "start_date=2010-01-01" \
  --data-urlencode "end_date=$DATABRIDGE_END_DATE" \
  --output sample_data/daily_output.csv
```

### 3.3 下载周频文件

接口参数：`frequency=周`。

```bash
curl --fail-with-body --location --retry 3 \
  --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" \
  --data-urlencode "frequency=周" \
  --output sample_data/weekly_output.csv
```

### 3.4 下载月频文件

接口参数：`frequency=月`。

```bash
curl --fail-with-body --location --retry 3 \
  --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" \
  --data-urlencode "frequency=月" \
  --output sample_data/monthly_output.csv
```

周频和月频不传日历日期参数，直接下载统一导出的完整周期数据。三个命令任一出现非 2xx、空响应或非 CSV 内容时，都必须停止验证；不得拿旧文件、sample 或手工文件冒充本次真实下载。

下载结束后从当前 shell 清除密码：

```bash
unset DATABRIDGE_API_PASSWORD
```

不要把包含真实密码的命令复制到聊天、工单或日志。正式交付的 `{scheme_id}.py`、`{scheme_id}.json` 以及算法输出中不得出现 DataBridge 地址、用户名、密码或下载逻辑。

### 3.5 校验下载结果

确认 `data_bridge_v1_schema.json` 位于当前目录，并执行：

```bash
python - <<'PY'
import csv
import json
from pathlib import Path

import pandas as pd


root = Path("sample_data")
schema = json.loads(
    Path("data_bridge_v1_schema.json").read_text(encoding="utf-8")
)
keys = {
    "daily_output.csv": "date",
    "weekly_output.csv": "week_id",
    "monthly_output.csv": "month_id",
}

for filename, key in keys.items():
    path = root / filename
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_header = next(csv.reader(handle), [])
    if not raw_header:
        raise ValueError(f"{filename} header is empty")
    if len(raw_header) != len(set(raw_header)):
        raise ValueError(f"{filename} contains duplicate columns")
    frame = pd.read_csv(path, dtype="string", keep_default_na=False)
    actual = list(frame.columns)
    baseline = schema["files"][filename]["columns"]
    if frame.empty:
        raise ValueError(f"{filename} is empty")
    if not actual or actual[0] != key:
        raise ValueError(f"{filename} must start with {key}")
    missing = [column for column in baseline if column not in actual]
    if missing:
        raise ValueError(f"{filename} missing baseline columns: {missing[:10]}")
    positions = [actual.index(column) for column in baseline]
    if positions != sorted(positions):
        raise ValueError(f"{filename} baseline column order changed")
    if frame[key].str.strip().eq("").any() or frame[key].duplicated().any():
        raise ValueError(
            f"{filename} {key} must be non-empty and unique"
        )
    print(
        filename,
        "OK",
        f"rows={len(frame)}",
        f"columns={len(frame.columns)}",
    )
PY
```

输出中的 `columns` 是本次真实文件的实测值，不是验收常量，后续下载可能增加。算法还必须把自己实际消费的字段列成显式清单并逐项检查；未使用的新增业务列直接忽略。缺少基线字段、基线相对顺序变化、文件为空或时间键重复时，先重新下载；不得修改文件表头来绕过检查。

### 3.6 脱敏 sample 何时使用

随包 sample 保留制作时点的 `data-bridge-v1` 基线表头，每份只有两行合成数据。真实 DataBridge 后续可能增加业务列，sample 不代表未来文件的永久完整表头。它们只适合：

- 验证 CSV 能否读取；
- 验证字段选择和 dtype；
- 验证按截止键截断；
- 验证 CLI、Result 和失败处理。

sample 不来自生产，不得用于训练模型、效果回测、比较准确率，或推断真实数据的起止日期、分布和空值比例。DataBridge 恢复后，正式算法自验必须重新使用真实下载数据。

---

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

`horizon` 按任务频率计期：日频按后续交易日计数，周频按周频观测计数，月频按月频观测计数。必须整行选择任务组合，不得自由修改 `horizon` 或填写其他 `target_rule`。

### 4.2 填写 `{scheme_id}.json`

Metadata 必须是无 BOM 的 UTF-8 JSON。Contract 1.0 包含八个必填字段，并可选提供推荐字段 `description`：

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
- `scheme_id` 必须与目录名、Python 文件名和 JSON 文件名一致。
- `scheme_id` 和 `target_tenor` 必须满足第 1 节约束。
- `scheme_id` 是算法执行身份；`name` 是当前任务格子内用于区分候选方案的简洁业务名称，两者不要混用。
- `name` 不得重复 `target_tenor`、不得重复 `task_type` 或 `horizon`，也不得追加“方向预测”等已经由任务格子表达的说明。
- `name` 和 `algorithm_version` 必须是非空字符串；`algorithm_version` 不强制使用特定版本格式。
- `description` 是可选的算法逻辑摘要，缺失不阻断 Contract 1.0 交付和自验，但强烈建议提供，方便后续按算法版本回溯。
- `description` 建议简述主要输入、窗口或规则、模型类型以及最终方向形成方式；平台不会根据脚本或名称代写算法逻辑。
- `description` 存在时必须是单段非空纯文本，最多 300 个字符，不得包含换行、HTML 或其他标记文本。
- `task_type`、`horizon` 和 `target_rule` 必须来自上一节的同一行。
- 除可选 `description` 外，不得增加 `frequency`、输入路径、运行开关、可变阈值、特征列表或模型参数。

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
- `backtest` 读取一批 Request，每个 Request 输出一条结果；Contract 1.0 每批为一至 100 条。
- 两个命令必须复用相同的数据处理、算法逻辑和方向映射。
- 单个 Request 的结果不得因批次大小、批次切分或输入顺序改变。

这里的单批上限不是完整回测总量上限。完整历史区间可以超过 100 条；平台会在同一 scheme version、DataBridge snapshot 和 generation 下，把完整 Request 序列拆成多个不超过 100 条的批次并多次调用 `backtest`。上游脚本只处理当前收到的批次，不得保存跨批状态，也不得要求把完整区间一次性传入。任一 Request 在不同批次大小、分区边界或输入顺序下都必须得到相同结果。

脚本只使用冻结运行环境和 Request，只从 `--data-dir` 读取业务数据，只向 `--output` 写业务结果。脚本不得：

- 访问 DataBridge、网络、数据库或其他业务数据源；
- 安装依赖、修改运行环境或动态加载交付包之外的代码；
- 写入 `--data-dir`，或者硬编码本机绝对路径；
- 自行计算或修改平台给出的日期和截止键；
- 将业务结果或调试内容写入 `stdout`。

日志和错误信息统一写入 `stderr`。业务运行期间 `stdout` 必须为空。

---

## 6. 读取数据和 Request

### 6.1 读取三频数据

平台每次运行都通过 `--data-dir` 提供：

```text
<data-dir>/daily_output.csv
<data-dir>/weekly_output.csv
<data-dir>/monthly_output.csv
```

- 同一次运行的三份文件属于同一份只读快照，运行期间不会被替换。
- 平台始终提供三份文件；算法可以只读取当前方案实际需要的文件。
- 不得因为未使用的文件存在而失败，也不要求主动解析未使用文件。
- 三份文件的业务列只能是有限数值或空值。
- 数据行数、列数、起止区间、业务值和空值都可以变化；算法必须按字段名选择实际消费列，并忽略未使用的新增列。
- 不得假定固定行数、固定列数、固定终点或“文件最后一行就是当前 Request 截止点”。

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
```

### 6.2 Request 字段

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

Request 必须恰好包含以上七个字段：

- `request_id` 必须是非空字符串，批内唯一。
- 三个日期必须是规范 `YYYY-MM-DD`。
- 日期必须满足 `feature_date <= predict_date <= target_date` 且 `feature_date < target_date`。
- `daily_cutoff_key` 是规范 `YYYY-MM-DD`；周、月截止键是六位数字字符串。
- 日期和截止键由平台生成，算法只校验和使用，不得修改、顺延、回退或重新推导。
- 批量输入顺序就是输出顺序。任一行非法时必须全批失败，不能跳过后输出部分结果。

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

不得用文件最后一行代替截止键，也不得根据 `feature_date` 推导周、月截止键。批量回测必须逐行独立截断，不能按批次最大截止键一次截断后复用。

Contract 1.0 使用当前快照加截止键隔离后续行，不提供历史时点修订版本回放。这个回测口径可以验证同一当前快照下的 as-of 逻辑，不能宣称历史 vintage PIT。

---

## 7. 生成 Result

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

---

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

先执行第 1.2 节环境自检命令，再用第 3 节真实下载并校验通过的 `sample_data/` 执行：

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

逐项完成：

| 验证项 | 操作 | 通过标准 |
|---|---|---|
| DataBridge 下载 | 分别下载日、周、月三份真实 CSV | HTTP 成功，文件名固定，文件非空 |
| Schema 校验 | 执行第 3.5 节校验命令，并检查算法消费字段 | 基线字段和相对顺序兼容，时间键合法；不限制总列数 |
| 交付物 | 检查文件数量、命名、Metadata 八个必填字段、可选说明和任务组合 | 只有两个交付文件，身份和任务组合合法；缺少说明不阻断 |
| 命令与日志 | 执行 `--help`、`predict`、`backtest` 并分别捕获 stdout/stderr | 命令存在；成功运行 stdout 为空 |
| 单点预测 | 使用一个合法 Request 执行 `predict` | 退出码 `0`，恰好一条五字段结果 |
| 批量回测 | 使用至少两个不同截止键执行 `backtest` | 每个 Request 恰好一条结果，数量和顺序一致 |
| 预测/回测一致 | 将同一个 Request 分别交给两个命令 | 五个业务字段完全一致 |
| 后续行隔离 | 修改或追加实际消费文件中位于截止键之后的合法行 | 当前 Request 的五个业务字段不变 |
| 分批与顺序 | 改变批次切分和 Request 顺序后运行并按 ID 对齐 | 每个 Request 的结果不变 |
| 重复执行 | 相同环境、数据和 Request 连续执行两次 | 五个业务字段完全一致 |
| 失败处理 | 逐项构造第 8 节失败条件 | 非零退出，stderr 有错误，stdout 为空，不产生 Output |

准确率等算法效果门槛由当前方案的业务验收要求单独规定，不在本通用接口 SOP 中统一设定。

---

## 10. 最终交付检查

提交前逐项确认：

- [ ] 只交付同名 `{scheme_id}.py + {scheme_id}.json`；
- [ ] `.json` 的八个必填字段合法，`description` 如提供则符合约束；
- [ ] `name` 是任务格子内的简洁方案名，没有重复期限、任务或“方向预测”；
- [ ] `predict` 和 `backtest` 使用同一算法逻辑；
- [ ] 真实 DataBridge 数据下载和 Schema 校验已通过；
- [ ] 每个 Request 按自己的截止键独立截断；
- [ ] 单点、批量、分批、变序、重复执行和未来行隔离全部通过；
- [ ] 成功时 stdout 为空，失败时不产生 Output；
- [ ] 交付脚本和 Metadata 不含 DataBridge 地址、用户名、密码或下载逻辑；
- [ ] 没有网络、数据库、额外代码、模型或数据依赖。

全部完成后，只提交 `{scheme_id}.py` 和 `{scheme_id}.json`。
