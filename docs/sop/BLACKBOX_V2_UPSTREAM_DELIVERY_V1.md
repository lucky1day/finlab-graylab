# Blackbox V2 上游算法交付说明（Contract 1.0）

**文档状态**：`CURRENT`　**目标读者**：平台外的上游算法工程师

本文是交付一个 Blackbox V2 方案所需的全部合同。随本文外发：
[`data_bridge_v1_schema.json`](../../shared/blackbox_v2/data_bridge_v1_schema.json) 与
[`samples/`](../blackbox_v2/data_bridge_v1/samples/)（Metadata、Request、Result 及五份合成数据样例）。
平台收到交付后自行完成校验、回测和上线，上游无需也无法执行平台命令。

## 1. 交付物

一个目录，只含两个普通文件（不得有子目录、符号链接或其他文件）：

```text
{scheme_id}.py     # 唯一的算法脚本：数据处理、训练、预测全部在内
{scheme_id}.json   # Metadata
```

- 两个文件名主体必须等于 Metadata 中的 `scheme_id`；`scheme_id` 匹配 `^[a-z][a-z0-9_]*$`，且不得与平台已有方案重名。
- 一个方案只对应一个 `target_tenor` + `task_type` 组合。
- 不交付模型文件、辅助模块、数据、配置或凭据；运行时也无法导入脚本旁的模块。

## 2. Metadata

`{scheme_id}.json` 为 UTF-8（无 BOM）JSON 对象，恰好包含下表十个字段，不得重复键或增加字段。
字符串值的首尾空白会被去除。参考 [`metadata.sample.json`](../blackbox_v2/data_bridge_v1/samples/metadata.sample.json)。

| 字段 | 类型 | 取值规则 | 示例 |
|---|---|---|---|
| `schema_version` | string | 固定 `"1.0"` | `"1.0"` |
| `scheme_id` | string | `^[a-z][a-z0-9_]*$` | `"ten_y_t1_lgbm_v1"` |
| `name` | string | 非空；建议为简称，不重复期限和任务信息 | `"LGBM_SPREAD"` |
| `owner` | string | 1–64 字符单行文本；无首尾空白、控制字符、`<`、`>`；不得为 `--`、`unknown`（不区分大小写）、`待定` | `"ALGO-A"` |
| `description` | string | 非空，≤300 字符，不含换行、`<`、`>`；简述输入、窗口、模型和方向形成方式 | `"使用期限利差和滚动分类模型形成方向信号。"` |
| `algorithm_version` | string | 非空，上游自定 | `"1.0.0"` |
| `target_tenor` | string | `1Y`、`3Y`、`5Y`、`7Y`、`10Y` | `"10Y"` |
| `task_type` | string | 见下表 | `"T+1"` |
| `horizon` | integer | JSON 整数（不能是字符串或布尔），必须与 `task_type` 匹配 | `1` |
| `target_rule` | string | 必须与 `task_type` 匹配 | 见下表 |

`task_type`、`horizon`、`target_rule` 必须整行取自下表：

| `task_type` | `horizon` | `target_rule` | 预测含义 |
|---|---:|---|---|
| `T+1` | 1 | `target_date_yield_vs_feature_date_yield` | 第 1 个后续交易日相对数据截止日 |
| `T+5` | 5 | `target_date_yield_vs_feature_date_yield` | 第 5 个后续交易日相对数据截止日 |
| `weekly_point` | 1 | `target_week_end_yield_vs_feature_week_end_yield` | 下一周频点相对本周频点 |
| `weekly_average` | 1 | `target_week_average_yield_vs_feature_week_average_yield` | 下周平均相对本周平均 |
| `monthly` | 1 | `target_month_observation_yield_vs_feature_month_observation_yield` | 下月观测相对本月观测 |
| `monthly_average` | 1 | `target_month_average_yield_vs_feature_month_average_yield` | 下一月中桶平均相对当前桶 |
| `quarterly_average` | 1 | `target_quarter_average_yield_vs_feature_quarter_average_yield` | 下一自然季度平均相对当前季度 |
| `annual_average` | 1 | `target_year_average_yield_vs_feature_year_average_yield` | 下一春节年平均相对当前春节年 |

目标日期由平台计算并写入 Request；算法不得根据 `horizon` 自行推导日期。

## 3. 运行接口

平台以如下方式调用脚本（`-I` 隔离模式：忽略 `PYTHONPATH` 和用户 site-packages，脚本目录不在 `sys.path`；脚本以 `__main__` 运行）：

```bash
python -I {scheme_id}.py predict  --request  <request.json> --data-dir <dir> --output <prediction.json>
python -I {scheme_id}.py backtest --requests <requests.csv> --data-dir <dir> --output <backtest.csv>
```

**Request**（平台生成）：JSON 对象（`predict`）或带表头的 CSV（`backtest`，至少一行），每条恰好七个字符串字段，按字段名读取：

| 字段 | 格式 | 说明 |
|---|---|---|
| `request_id` | 非空；批内唯一 | 不透明字符串，原样回显，不要解析 |
| `predict_date` / `feature_date` / `target_date` | `YYYY-MM-DD` | 满足 `feature_date <= predict_date <= target_date` 且 `feature_date < target_date` |
| `daily_cutoff_key` | `YYYY-MM-DD` | `daily_output.csv` 的截止行 |
| `weekly_cutoff_key` | 六位数字 | `weekly_output.csv` 的截止行 |
| `monthly_cutoff_key` | 六位数字 | `monthly_output.csv` 的截止行 |

`backtest` 会在一个进程内一次传入完整历史区间（行数不设上限）。

**Result**：恰好五个字段 `request_id`、`predict_date`、`feature_date`、`target_date`、`predicted_direction`。

- 前四个字段原样回显对应 Request；`backtest` 输出行数和顺序与输入完全一致。
- `predicted_direction`：JSON 中为整数 `-1`、`0`、`1`（不能是字符串或布尔）；CSV 中为文本 `-1`、`0`、`1`。`0` 只表示算法预测为平，不得用来掩盖缺数或异常。
- `predict` 输出 UTF-8（无 BOM）JSON 对象；`backtest` 输出带表头 CSV。不得增加概率、cutoff 等字段。
- 参考 [`prediction.sample.json`](../blackbox_v2/data_bridge_v1/samples/prediction.sample.json)、[`backtest.sample.csv`](../blackbox_v2/data_bridge_v1/samples/backtest.sample.csv)。

**成功与失败**：

- 成功：退出码 0，`--output` 为普通文件，`stdout` 为空。建议在同目录写临时文件后 `os.replace` 到 `--output`。
- 失败：任一 Request 无法正常计算（字段非法、截止键不存在、数据不足、非法值等）即以非 0 退出，原因写 `stderr`，不写部分结果。平台不接受部分成功，失败后会清空本次运行目录。

**运行环境与限制**（超出任一项即判失败）：

| 项目 | 规定 |
|---|---|
| Python 与主要包 | Python 3.13.12；numpy 2.3.5、pandas 2.3.3、scipy 1.16.3、scikit-learn 1.8.0、lightgbm 4.6.0、xgboost 3.1.3、catboost 1.2.8、joblib 1.5.3、pyarrow 23.0.0、shap 0.50.0、numba 0.63.1、numexpr 2.14.1、bottleneck 1.4.2、boruta 0.4.3、openpyxl 3.1.5（Linux 与 macOS 两台主机版本一致；不得安装其他包） |
| 时限 | `predict` 3600 秒，`backtest` 14400 秒；平台可为单次调用设更短期限 |
| 内存 | 整个进程组 RSS 64 GiB |
| 线程 | `OMP_NUM_THREADS` 等数值库线程变量为 8，不保证独占 8 核 |
| 输出 | `--output` ≤ 50 MiB；`stdout`+`stderr` 合计 ≤ 5 MiB |
| 工作目录 | 当前目录即 `--output` 所在的空目录，`HOME`、`TMPDIR` 和各类缓存目录都指向它；目录总量 ≤ 64 MiB、≤ 10000 个条目，单文件 ≤ 64 MiB |
| 环境变量 | 只保留 `LANG`（`C.UTF-8`）、`LC_ALL`、`TZ`（`Asia/Shanghai`）及上述平台设置；`PATH` 只含 Python 所在目录 |

## 4. 输入数据

`--data-dir` 是只读的 DataBridge 快照，含以下五个文件（新方案固定为此模式，即 `factor_input_mode: algorithm_managed`）。
不得修改其中任何文件，平台在运行后校验文件指纹，有变化即判失败。

| 文件 | 第一列（时间键） | 内容 |
|---|---|---|
| `daily_output.csv` | `date`，`YYYY-MM-DD` | 日频因子宽表 |
| `weekly_output.csv` | `week_id`，六位数字 | 周频因子宽表；`week_id` 是平台周键，不是 ISO 周，不得加减推算 |
| `monthly_output.csv` | `month_id`，六位数字 | 月频因子宽表 |
| `api_wind_date.csv` | `rdate`，`YYYY-MM-DD` | 恰好两列 `rdate,week_id`，交易日到平台周键的映射 |
| `factor_catalog.csv` | 无 | 恰好三列 `indicators_code,frequency,factor_version` |

平台保证：

- 各文件非空、表头无重复字段；时间键唯一且严格升序；因子值为有限数值或空值。
- 三张宽表包含 Schema 中的全部基线字段且相对顺序不变，但可能在任意位置有新增列。按列名选列，不依赖列位置、列数、行数或数据终点。
- `factor_catalog.csv` 的 `indicators_code` 全局唯一；`frequency` 为 `daily`/`weekly`/`monthly`；`factor_version` 形如 `V1.0`；行序与三张宽表（按 daily、weekly、monthly）的因子列序完全一致。
- Request 中三个截止键都存在于对应文件；`daily_cutoff_key` 是不晚于 `feature_date` 的最后一个交易日（`*_average` 任务中等于 `feature_date`）；`api_wind_date.csv` 中 `daily_cutoff_key` 对应的 `week_id` 等于 `weekly_cutoff_key`。

**截止规则**：对每条 Request，每个实际使用的文件都只能用首行至截止键所在行（含该行）的数据；不得用文件最后一行代替截止键，不得自行推导周、月截止键，不得读取其他来源的数据或日历。不使用的文件可以不读。

按版本选因子时读取 catalog，不要硬编码上千个列名：

```python
catalog = pd.read_csv(data_dir / "factor_catalog.csv", dtype="string")
cols = catalog.loc[(catalog["frequency"] == "daily") & catalog["factor_version"].isin(["V1.0"]), "indicators_code"].tolist()
daily = pd.read_csv(data_dir / "daily_output.csv", dtype={"date": "string"})
features = daily.loc[:, cols]   # 缺列即 KeyError，应视为失败
```

周、月键请按字符串读取（如 `dtype={"week_id": "string"}`）。
`legacy_v1` 仅用于存量方案：只有前四个文件，且三张宽表列与 Schema 基线完全一致；新交付不会使用该模式。

## 5. 禁止事项

平台在接收时对脚本做静态扫描，命中以下任一项即拒收：

- 导入以下模块（含其子模块）：`aiohttp` `ftplib` `http` `mysql` `pymysql` `psycopg` `requests` `socket` `sqlalchemy` `sqlite3` `subprocess` `urllib`。
- 调用 `eval`、`exec`、`compile`、`__import__`、`os.system`、`os.popen`、`os.spawnl`、`os.spawnv`（通过 `import ... as` 或 `from ... import ... as` 起别名同样会被识别）。
- 任何以 `/Users/`、`/home/` 或盘符（如 `C:\`）开头的字符串字面量。
- 任何含 `..` 路径段的字符串字面量（如 `"../data"`）。
- 文件读写调用（`open`、`Path(...)`、`os.path.join`、`pd.read_*`、`np.load`、`to_csv` 等）或 `/` 路径拼接中以 `/`、`\\`、`file:/` 开头的路径。以变量开头的拼接如 `f"{data_dir}/daily_output.csv"` 允许。

静态扫描无法覆盖的规则同样必须遵守：不访问网络或数据库，不启动子进程，不安装包，不动态加载其他代码；只读 `--request`/`--requests`、`--data-dir`（及第 6 节的状态输入），只写 `--output`、工作目录和 `stderr`；日志不写 `stdout`。

## 6. 可选：增量状态

默认每次调用都从原始输入完整计算，不保留跨调用状态。若冷启动太慢，可与平台约定启用增量状态（由平台在接收时开启，Metadata 和 Result 不变）。启用后：

- 每次调用都会额外传入 `--state-output <path>`（位于 `--output` 同目录的新路径）；恢复计算时还会传入只读的 `--state-input <path>`。没有 `--state-input` 时必须从原始输入完整计算。
- 脚本成功时必须把完整新状态写到 `--state-output`，≤ 16 MiB；不得修改 `--state-input` 和 Request 文件。
- 状态只能包含可由输入重建的本方案派生计算结果；使用状态与不使用状态的结果必须逐字段一致。无法证明可复用（损坏、历史数据修订超出可重算范围等）时非 0 退出，不得回退旧结果。
- 启用后限制更严：日常 `predict` 120 秒；`backtest` 和冷重建默认 7200 秒；内存默认 4 GiB。

## 7. 自测

用 [`samples/`](../blackbox_v2/data_bridge_v1/samples/) 验证接口（样例数据是合成值，只用于接口检查）：

```bash
mkdir -p selftest/data selftest/out1 selftest/out2 && S=<samples 目录>
for f in daily_output weekly_output monthly_output api_wind_date factor_catalog; do cp $S/$f.sample.csv selftest/data/$f.csv; done
cp $S/request.sample.json selftest/request.json && cp $S/requests.sample.csv selftest/requests.csv
python -I {scheme_id}.py predict  --request  selftest/request.json  --data-dir selftest/data --output selftest/out1/prediction.json > selftest/stdout1; echo "exit=$?"
python -I {scheme_id}.py backtest --requests selftest/requests.csv --data-dir selftest/data --output selftest/out2/backtest.csv > selftest/stdout2; echo "exit=$?"
```

检查：退出码 0、`stdout` 文件为空、输出字段与第 3 节一致且逐条回显 Request。若样例数据不足以运行模型，脚本应非 0 退出并给出清晰 `stderr`，这同样验证了失败路径。

再用同一份真实 DataBridge 五文件数据（文件名同上）完成：

1. 同一单条 `predict` 连续运行三次，结果一致。
2. 完整历史区间一次 `backtest`，连续运行三次结果一致，且在第 3 节时限内完成。
3. 将同一批 Request 乱序、取子集运行，按 `request_id` 比较，结果与完整批次一致；抽取首、中、末三条单独 `predict`，结果与批次一致。
4. 在截止键之后追加若干合法未来行，已有 Request 的结果不变。
5. 构造非法输入（缺字段、多字段、非法日期、重复 `request_id`、不存在的截止键），确认非 0 退出、`stderr` 有原因、`stdout` 为空、无输出文件。
6. 如使用随机过程，固定随机种子。

## 8. 交付检查表

- [ ] 目录只含 `{scheme_id}.py` 和 `{scheme_id}.json`，文件名与 `scheme_id` 一致
- [ ] Metadata 恰好十个字段，`task_type`/`horizon`/`target_rule` 取自第 2 节同一行，`horizon` 为整数
- [ ] `owner`、`description` 符合第 2 节规则
- [ ] 脚本支持 `predict` 和 `backtest` 两个子命令及第 3 节参数，只使用第 3 节所列包
- [ ] 脚本不含第 5 节任一禁止项，不硬编码路径
- [ ] 每条 Request 都按截止键截断后计算，不使用未来数据
- [ ] Result 恰好五个字段，回显正确，`backtest` 行数和顺序与输入一致
- [ ] 成功时 `stdout` 为空；失败时非 0 退出且无输出文件
- [ ] 第 7 节样例自测和真实数据自测全部通过
- [ ] 交付目录中没有自测数据、模型、缓存或临时文件
