# DataBridge 周历自测与平台部署对齐设计

日期：2026-07-30

状态：已确认（2026-07-30）

适用范围：Blackbox V2 上游自测、平台 Onboarding 验收与生产运行

## 1. 背景

依赖周度口径的算法不仅消费 DataBridge 的日、周、月三频业务文件，
还需要 `api_wind_date.csv` 将自然日映射到平台 `week_id`。DataBridge
已经提供该表的专用只读下载入口：

```text
/api/export/tables/api_wind_date/csv/
```

现行 SOP 只说明上游可以在本地放置该文件，没有明确：

- 文件必须从统一 DataBridge 下载；
- 自测与平台验收必须使用同一批数据；
- 哪些身份和摘要构成“同一批”；
- 平台滚动到新 generation 后应如何解释结果差异。

这会让同一算法在本地和平台使用不同的 `date -> week_id` 映射，
从而产生周分组、feature week、target week、月度归属或结果数量差异，
并被误判为算法逻辑问题。

## 2. 已确认方案

采用“验收同代、生产滚动”的口径：

1. 上游自测从统一 DataBridge 下载日、周、月三频文件和
   `api_wind_date.csv`。
2. 平台 Onboarding 的算法结果验收必须使用与上游自测完全相同的
   DataBridge generation 和日历内容。
3. 正式生产不永久冻结 Onboarding generation；每次 scheduled live
   继续使用当天最新且已封存的 generation。
4. 比较双方 generation 或日历内容不一致时，差异只能先归类为
   `data_vintage_mismatch`，不得直接归责算法。需要精确验收时，必须
   在平台选定的同代输入上重新执行自测。

不采用以下方案：

- 只比较文件名或日期范围：无法发现同范围内历史修订或周键变化。
- 只记录 DataBridge generation ID：平台的三频父 generation 不包含
  `api_wind_date.csv`，无法证明周历相同。
- 永久冻结生产在 Onboarding generation：会阻止正式运行读取新数据。

## 3. 对齐身份

平台继续维持现有两层输入身份：

- 三频业务文件由 `generation_id + refresh_date + business_digest`
  标识；
- `api_wind_date.csv` 由 `api-wind-date-v1` provider 规范化并记录
  SHA256；
- 三频父快照和日历共同形成 `combined_snapshot_id`。

因此一次可比较验收的完整身份是：

```text
generation_id
refresh_date
daily_output.csv SHA256
weekly_output.csv SHA256
monthly_output.csv SHA256
api_wind_date.csv canonical SHA256
combined_snapshot_id
```

DataBridge 的普通 CSV 下载响应目前不返回平台内部 `generation_id`。
上游不能自行编造 generation ID。上游先记录四文件 SHA256、下载时间、
行数和起止键；平台 Intake 时把三频 SHA 对应到选定 generation，并把
`generation_id`、`refresh_date` 和 `combined_snapshot_id` 补入验收
记录。无法精确对应时视为未对齐，平台应提供或指定验收 generation，
上游再用该代输入重跑自测。

## 4. 上游自测规则

### 4.1 下载

上游通过同一 DataBridge 账号和同一连续下载批次取得：

```text
daily_output.csv
weekly_output.csv
monthly_output.csv
api_wind_date.csv
```

其中日历使用：

```bash
curl --fail-with-body --location --retry 3 \
  --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  "${DATABRIDGE_API_BASE_URL%/}/export/tables/api_wind_date/csv/" \
  --output sample_data/api_wind_date.csv
```

任一下载失败、为空、列不合法或下载期间数据发生切换时，整批作废，
不得混用旧文件或 sample。

### 4.2 周键语义

`api_wind_date.csv` 固定为 `rdate,week_id`：

- `rdate` 必须是唯一、严格升序的 `YYYY-MM-DD`；
- `week_id` 必须作为六位字符串读取；
- `week_id` 是平台业务键，不是 ISO 周，不保证数值连续；
- 不得对 `week_id` 做 `+1`、按数值大小推导相邻周，或自行重算；
- `daily_cutoff_key` 对应的业务周必须由同批
  `api_wind_date.csv` 精确映射，并与 Request 的
  `weekly_cutoff_key` 一致；
- `weekly_output.csv.week_id` 必须包含算法实际使用的 cutoff 周。

算法只能读取 `--data-dir/api_wind_date.csv`。不得保留内嵌日历、
交付目录旁同名文件、ISO 周换算或网络/数据库回退。

### 4.3 自测证据

自测报告或交接记录必须包含：

- 下载时间和声明的数据截止日；
- 四文件 SHA256、行数、首尾时间键；
- 日历列名、最小/最大 `rdate`、首尾 `week_id`；
- 自测 Request 的三日期与三个 cutoff key；
- 自测结果所使用的完整输入摘要。

这些是验收证据，不是 Blackbox delivery 文件。正式交付目录仍然只
包含同名 `{scheme_id}.py + {scheme_id}.json`。

## 5. 平台验收规则

平台 Intake 仍通过：

```text
--platform-input api-wind-date-v1
```

声明周历依赖。平台不得直接采用上游随包日历作为生产输入，而应从
平台权威链生成规范化日历。执行结果对比前必须：

1. 核对上游三频 SHA 与选定 DataBridge generation 的三频 SHA；
2. 核对上游日历规范化 SHA 与 `api-wind-date-v1` artifact SHA；
3. 记录匹配后的 `generation_id`、`refresh_date` 和
   `combined_snapshot_id`；
4. 核对 Request 的 `daily_cutoff_key -> weekly_cutoff_key` 映射；
5. 只有全部相同时，才允许把输出差异判定为算法或平台适配差异。

任一项不一致时：

- Gate 或人工验收结果标记 `data_vintage_mismatch`；
- 不得把方向、数量、月度统计或内部字段差异写成算法失败；
- 精确比较必须改用同一 generation 与日历重跑；
- 原自测报告可保留为旧数据版本证据，但不能冒充当前部署验收。

## 6. 生产运行与报告解释

正式 scheduled live 使用当天最新、完整校验并封存的 generation，
不受 Onboarding generation 永久约束。每次生产运行仍记录当次
`generation_id + combined_snapshot_id`。

跨 generation 报告只能用于观察稳定性，不能直接作为逐行复现结论。
需要声明“与算法自测一致”时，必须同时列出并核对完整输入身份。

`week_id`、交易日、`predict_date`、`feature_date` 和 `target_date`
分别按平台契约生成；算法输出不得覆盖平台 Request。周度实盘仍以
`feature_date = previous_trading_day(predict_date)` 为基础，再使用
同代 `api_wind_date.csv` 映射 `feature_week_id`。

## 7. 文档修改范围

本次只修改通用文档，不改变运行代码、数据表或算法：

1. `BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`
   - 增加日历下载命令；
   - 增加周键、四文件自测、证据和同代重测规则。
2. `BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
   - 增加上游自测输入与平台 generation 的对齐 Gate；
   - 明确 `data_vintage_mismatch` 与生产滚动规则。
3. `PREDICTION_SEMANTICS.md`
   - 明确日期、交易日与周键的权威来源和同代约束。
4. `data_bridge_v1/README.md`
   - 补充 `api_wind_date.csv` 是可下载的开发/自测平台输入制品，
     但不改变三频父 Schema，也不成为第三个交付文件。

## 8. 验收

- 文档中的下载路径与 DataBridge 当前接口一致；
- 上游和平台 SOP 使用同一套“验收同代、生产滚动”术语；
- 不把 `api_wind_date.csv` 写成 Blackbox delivery 文件；
- 不把日历并入 `data-bridge-v1` 三频父 generation；
- 不要求上游伪造 DataBridge 未返回的 generation ID；
- 最终检查表包含四文件摘要、Request 映射和跨代重测项；
- 文档交叉引用与既有平台输入制品设计一致。
