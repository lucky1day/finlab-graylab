# 历史档案：上游算法黑盒 V2 交付 SOP（Excel 工程包草案）

> **状态：已废弃，不得用于方案交付或平台验收。**
> 归档日期：2026-07-19
> 原文件 SHA256：`e7ae92ce7ad28ac55ea3f6075c8150b761d2c94216354513df6705f7b401f73b`

该草案要求 Excel、`delivery.yaml`、完整算法工程和 Reader/Core/Writer 分层，已被“两文件交付 + 三频 CSV + CLI”契约替代。

现行文档：

- [上游 Blackbox V2 交付 SOP](../../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- [平台 Blackbox V2 入库 SOP](../../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- [Blackbox V2 文档管理](../README.md)

以下正文按原始桌面文件保留，仅用于追溯决策演进。旧文中的 Excel、数据库、旧 horizon、Registry 和交付包要求均不再生效。

---

## 原始正文

# 上游算法黑盒 V2 交付 SOP

> 读者：会 Python、pandas、Excel 和基础 YAML，但完全不了解 Bond Factor Lab 的算法同事。
> 状态：本文是 Blackbox V2 的目标交付规范，不表示平台已经实现 Blackbox V2。
> 范围：只适用于后续新增方案；Legacy V1 存量方案不迁移、不修改。

Bond Factor Lab 是国债收益率方向算法的接入、定时运行、结果保存和展示平台。Blackbox V2 是后续新增算法使用的黑盒接入方式；Legacy V1 是现有存量方案使用的旧接入方式。本文只说明上游算法同事需要做什么，不包含生产数据库、落库、调度和内部方案注册的实现。

本文中的“必须”表示不满足时平台会退回交付包。“平台生成”表示该内容不需要上游准备。

---

## 1. 一页结论

### 1.1 上游只交付本地可运行的 Excel 方案

上游本地流程：

```text
wind_export.py 导出数据
        ↓
      Excel
        ↓
  Excel Reader
        ↓
  Frozen Core
        ↓
  Excel Writer
        ↓
   结果 Excel
```

平台接入后的生产流程：

```text
平台数据库
    ↓
DB Input Adapter
    ↓
同一个 Frozen Core
    ↓
Output Adapter
    ↓
平台统一保存结果
```

术语解释：

- `Excel Reader`：上游读取本地 Excel 并形成算法所需源数据对象的代码。
- `Frozen Core`：中文称“冻结算法核心”，是 Reader 和 Writer 之间所有会影响预测结果的代码、模型和配置。
- `Excel Writer`：上游把算法信号写入结果 Excel 的代码。
- `Adapter`：中文称“适配器”，只负责转换输入或输出，不改变算法逻辑。
- `DB Input Adapter`：平台把数据库数据转换成与 Excel Reader 相同算法输入的代码。
- `Output Adapter`：平台校验算法信号并交给平台统一保存的代码。

平台只替换 Excel Reader 和 Excel Writer，不修改 Frozen Core。

### 1.2 双方责任

| 事项 | 责任方 |
|---|---|
| 算法逻辑、模型效果、历史回测和结果正确性 | 上游 |
| 使用统一脚本导出 Excel 并完成本地复现 | 上游 |
| 提交实际使用的代码、模型、配置、Excel 和结果 | 上游 |
| 明确 Reader、Frozen Core、Writer 的逻辑边界 | 上游 |
| 核对统一导出脚本和查询参数 | 平台 |
| 将 Excel Reader 替换为 DB Input Adapter | 平台 |
| 将 Excel Writer 替换为 Output Adapter | 平台 |
| 检查运行、输入、输出和保存是否正确 | 平台 |
| 灰度实盘、正式实盘、调度和数据库写入 | 平台 |

上游不连接生产数据库，不获取生产数据库账号，不编写生产 SQL，不开发 DB Reader，也不写生产表。

### 1.3 上游最终只需要提交七类内容

1. 一份 `delivery.yaml`，说明方案是什么、怎样运行、读取哪些文件；
2. 实际使用的全平台统一 `wind_export.py`；
3. 原始算法代码、模型和配置，保持上游原有目录结构；
4. 交付脚本运行时实际读取的全部 Excel；
5. 能重建运行环境的依赖文件；
6. 一个可复现的单点 Request 和对应输出；
7. 历史回测逐点结果，以及上游已经确认算法通过的声明。

上游不需要编写 `manifest.yaml`、`input_contract.yaml`、`runtime.yaml`、`upstream_validation.yaml`、文件摘要或平台集成配置。这些信息中仍然必要的部分已经合并进 `delivery.yaml`，平台内部文件由平台收包后生成。

---

## 2. 先理解六个核心概念

| 概念 | 含义 |
|---|---|
| 算法方案 | 一套固定的数据需求、算法逻辑、目标期限和预测规则 |
| 评价点 | 一次独立预测，由 `predict_date`、`feature_date`、`target_date` 三个日期确定 |
| Request | 平台调用一次单点预测时传入的 JSON 文件，只包含三个日期 |
| dataset | 通过一次 `wind_export.py::export_dataframe()` 调用获得的一份源数据 |
| runner | 读取运行参数并依次调用 Reader、Frozen Core、Writer 的脚本；runner 本身不得包含另一套算法 |
| `delivery.yaml` | 上游交付说明，记录方案身份、运行入口、dataset、文件位置和验证结果 |

Blackbox V2 的“黑盒”含义是：平台不需要理解或修改 Frozen Core 内部如何清洗、构建特征、训练、投票和产生信号；平台只需要知道怎样提供源数据、怎样调用算法、怎样取得标准输出。

---

## 3. 方案身份和任务定义

### 3.1 上游必须提供的字段

| 字段 | 含义 | 规则 |
|---|---|---|
| `base_scheme_id` | 算法执行身份 | 上游提出，平台校验全局唯一；匹配 `^[a-z][a-z0-9_]{2,39}$` |
| `algorithm_version` | 本次算法版本 | 从 `v1` 开始，算法、模型、配置或有效数据口径变化时递增 |
| `name` | 方案名称 | 业务人员能够直接识别，1–128 个字符 |
| `description` | 方案说明 | 一句话说明使用什么数据、预测什么、预测多远 |
| `frequency` | 产生信号的频率 | `daily`、`weekly`、`monthly` |
| `task_type` | 预测任务类型 | 使用第 3.2 节固定值 |
| `horizon` | 平台登记的预测距离 | 使用第 3.2 节固定值 |
| `target_tenor` | 被预测收益率的期限 | `1Y`、`3Y`、`5Y`、`7Y`、`10Y` |
| `target_rule` | 三个日期和真实方向的计算规则 | 使用第 3.2 节固定代码值 |

一个算法只允许对应一个 `target_tenor`、一个 `frequency`、一个 `task_type`、一个 `horizon` 和一个 `target_rule`。预测不同期限或不同任务时，必须拆成不同的 `base_scheme_id`。

`base_scheme_id` 示例：

```text
macro_lgbm_10y_t5
```

不要使用 `new_model`、`test1` 等无法表达业务含义的名称。

### 3.2 允许的任务组合

| `frequency` | `task_type` | `horizon` | `target_rule` | 业务含义 |
|---|---|---:|---|---|
| `daily` | `T+1` | 1 | `nth_trading_day_after_feature` | 预测 feature date 后第 1 个交易日相对 feature date 的收益率方向 |
| `daily` | `T+5` | 5 | `nth_trading_day_after_feature` | 预测 feature date 后第 5 个交易日相对 feature date 的收益率方向 |
| `weekly` | `weekly_point` | 6 | `next_week_last_trading_day_vs_current_week_last_trading_day` | 预测下一周最后交易日相对本周最后交易日的收益率方向 |
| `weekly` | `weekly_average` | 6 | `next_week_average_yield_vs_current_week_average_yield` | 预测下一周有效交易日平均收益率相对本周平均收益率的方向 |
| `monthly` | `monthly` | 30 | `next_month_observation_yield_vs_feature_month_observation_yield` | 预测下一月观察日相对本月观察日的收益率方向；观察日取当月 15 日及以前最近交易日 |

四个字段必须整行选择，不得自由拼接。周频的 `6` 和月频的 `30` 是平台登记值，不表示直接增加 6 个或 30 个自然日；实际日期始终按同一行的 `target_rule` 计算。

### 3.3 `base_scheme_id` 与 Registry `scheme_id`

`Registry` 是平台内部保存业务方案身份和启停状态的注册清单。

- `base_scheme_id`：上游提出的算法执行身份。
- Registry `scheme_id`：平台根据任务生成的业务身份。

生成规则：

```text
{base_scheme_id}__h{horizon}__{target_tenor}
```

示例：

```text
macro_lgbm_10y_t5__h5__10Y
```

上游只填写 `base_scheme_id`，不填写 Registry `scheme_id`，也不设置 Registry 状态。

---

## 4. Request、三个日期和阶段

### 4.1 单点 Request

平台调用一个评价点时，只传入三个日期：

```json
{
  "predict_date": "2026-07-14",
  "feature_date": "2026-07-13",
  "target_date": "2026-07-20"
}
```

| 字段 | 唯一含义 |
|---|---|
| `predict_date` | 信号发出日；生产中通常也是任务运行日 |
| `feature_date` | 数据硬截止日；算法不得使用晚于该日才能获得的数据 |
| `target_date` | 预测验证目标日；也用于结果去重、实际值匹配和阶段划分 |

三个字段都必须是 `YYYY-MM-DD` 字符串，并满足：

```text
feature_date <= predict_date <= target_date
feature_date < target_date
```

Request 不包含数据库账号、Excel 路径、Registry 状态、阶段、随机种子或算法内部参数。算法需要固定随机种子时，应把它作为 Frozen Core 的固定配置，而不是让平台逐次传入。

### 4.2 日期如何生成

日期生成分为两种责任：

- 单点样例和生产运行：平台直接在 Request 中提供三个日期；算法只校验和使用，不重新推算。
- 批量历史回测和本地灰度参考：平台提供 `target_start`、`target_end_exclusive` 和已经确定的 `target_rule`；上游批量脚本按本节规则展开每个评价点的三个日期。

#### 日频

日频批量历史回测：

```text
predict_date = feature_date
target_date = feature_date 后第 horizon 个交易日
```

日频生产 Request：

```text
feature_date = predict_date 之前最近交易日
target_date = feature_date 后第 horizon 个交易日
```

例：T+5 方案在 2026-07-14 运行，之前最近交易日为 2026-07-13，且其后第 5 个交易日为 2026-07-20，则 Request 为：

```text
predict_date = 2026-07-14
feature_date = 2026-07-13
target_date  = 2026-07-20
```

#### 周频

先定义三个周频概念：

- `feature_week`：`feature_date` 所属的实际交易周。
- `target_week`：交易日历中紧接 `feature_week` 的下一实际交易周，不能用周编号直接加 1 推算。
- 周频 `target_date`：`target_week` 的最后交易日。`weekly_point` 和 `weekly_average` 使用相同三个日期，只是实际方向的计算对象不同。

周频批量历史回测：

```text
feature_date = feature_week 的最后交易日
predict_date = feature_date
target_date  = target_week 的最后交易日
```

周频生产 Request：

```text
feature_date = predict_date 之前最近交易日
feature_week = feature_date 所属的实际交易周
target_week  = feature_week 的下一实际交易周
target_date  = target_week 的最后交易日
```

例：当前实际交易周最后交易日为 2026-05-29，下一实际交易周最后交易日为 2026-06-05；平台在 2026-05-30 发出周频预测，则：

```text
predict_date = 2026-05-30
feature_date = 2026-05-29
target_date  = 2026-06-05
```

对于 `weekly_point`，方向比较下一周最后交易日收益率与本周最后交易日收益率；对于 `weekly_average`，方向比较下一周有效交易日平均收益率与本周平均收益率。两者的 `target_date` 都是下一实际交易周的最后交易日。

#### 月频

月频每个自然月 15 日发出一次预测，无论 15 日是否为交易日：

```text
predict_date = 当前自然月 15 日
feature_date = 当前月 15 日及以前最近交易日
target_date  = 下一自然月 15 日及以前最近交易日
```

批量历史回测和生产 Request 都使用这一规则，不能把 `predict_date` 顺延到 15 日之后的首个交易日。

例：2025-02-15 和 2025-03-15 均不是交易日，则：

```text
predict_date = 2025-02-15
feature_date = 2025-02-14
target_date  = 2025-03-14
```

#### 批量区间

`target_start` 是包含的第一个 `target_date`，`target_end_exclusive` 是不包含的结束边界：

```text
target_start <= target_date < target_end_exclusive
```

本地灰度参考也调用同一个批量脚本，使用上述历史评价日期语义。它只是上游对近期区间的 Excel 复现证据，不使用平台灰度实盘的调度日期，也不等同于平台灰度实盘。

上游可以自行选择正确的交易日历来源。平台不验收上游使用哪一份日历，但日期正确性由上游负责；接入生产后，平台只使用数据库中的交易日历生成 Request。周频和月频禁止直接按自然日增加 `horizon`。

### 4.3 数据截止规则

对每个评价点，任何进入算法计算的数据都必须满足：

```text
source_date <= feature_date
```

`source_date` 是源数据记录所代表的业务日期，例如日频数据的交易日。周频或月频数据应使用统一导出脚本返回的周期标识和数据内容，不自行用自然日反推另一套截止规则。

批量脚本可以一次读取覆盖整个回测区间的 Excel，但在计算每个评价点之前，必须按该点的 `feature_date` 单独截断。禁止用批量区间最后一天的数据计算更早评价点。

`target_date` 可以作为 Request 上下文传入算法，但不得用于读取目标日实际值、未来特征或任何晚于 `feature_date` 的数据。算法需要根据目标周期选择交付前已经存在的固定模型或固定配置时，该选择逻辑属于 Frozen Core，由上游负责验证。

### 4.4 历史回测、灰度实盘和正式实盘

三个阶段只按照 `target_date` 划分：

```text
历史回测：backtest_target_start <= target_date < gray_live_target_start
灰度实盘：gray_live_target_start <= target_date < scheduled_live_target_start
正式实盘：target_date >= scheduled_live_target_start
```

字段解释：

- `backtest_target_start`：平台保存的历史回测起始 target date。
- `gray_live_target_start`：平台 DB Input Adapter 首次产生灰度结果的 target date。
- `scheduled_live_target_start`：正式任务第一次成功结果的 target date，不是启用调度任务的日期。

上游批量脚本只接收 target date 区间，不需要接收“历史”“灰度”或“正式”等阶段参数。同一评价点不能因为阶段名称不同而产生不同信号。平台收包后根据 `target_date` 归类结果，并决定正式实盘起点。

示例：平台在 9 月 4 日启用正式任务，但该任务首次预测的 `target_date` 是 9 月 7 日，则 9 月 6 日及以前仍属于灰度实盘，正式实盘从 `target_date=9月7日` 开始。

---

## 5. 输入规范

### 5.1 第一性原则

平台只统一最源头的数据取得方式，不统一算法内部 Input。

这意味着：

- 平台规定数据必须由哪个函数取得；
- 上游声明调用该函数时使用什么参数；
- 上游决定怎样清洗、合并、构建 DataFrame、dict、numpy 数组、特征矩阵或模型对象；
- 平台不要求所有算法使用相同的内部数据结构。

### 5.2 唯一权威数据入口

所有算法源数据必须来自：

```text
wind_export.py::export_dataframe(frequency, start_date=None, end_date=None)
```

本文把它简称为 `wind_export.py::export_dataframe()`。

规则：

1. 全平台只有一份批准版本的 `wind_export.py`，不存在每个方案各自维护的权威版本；
2. 上游提交自己实际使用的 `wind_export.py` 文件；
3. 平台收包后计算文件摘要，并与全平台批准版本核对；
4. 上游本地 Excel 与平台 DB Input Adapter 必须使用同一脚本、同一参数和同一查询定义；
5. 上游不得自定义 SQL、复制另一套查询函数、隐藏过滤条件或绕过该函数。

文件摘要是平台用来判断两个文件是否完全一致的指纹，由平台生成；上游不需要计算或填写。

### 5.3 dataset 如何声明

每一次独立的 `export_dataframe()` 调用对应一个 dataset。每个 dataset 只需要声明：

| 字段 | 含义与规则 |
|---|---|
| `dataset_id` | 本方案内唯一的数据名称，例如 `daily_full_history` |
| `frequency` | 传给导出函数的源数据频率：`日`、`周`、`月` |
| `start_date` | 固定 `YYYY-MM-DD`，或 `all_history` 表示全部可用历史 |
| `end_date` | 固定写 `feature_date`，运行时替换为当前评价点的数据截止日 |
| `excel_file` | 上游脚本实际读取的 Excel 相对路径 |

示例：

```yaml
datasets:
  - dataset_id: daily_full_history
    frequency: 日
    start_date: all_history
    end_date: feature_date
    excel_file: data/wind_daily.xlsx

  - dataset_id: weekly_since_2018
    frequency: 周
    start_date: "2018-01-01"
    end_date: feature_date
    excel_file: data/wind_weekly.xlsx
```

多种频率、不同历史起点或不同 Excel 文件必须拆成不同 dataset。`frequency`、`start_date`、`end_date` 是唯一允许变化的查询参数；算法内部如何使用导出结果不属于平台输入规范。

`end_date: feature_date` 表示每个评价点的逻辑数据截止规则，不表示批量回测必须为每个评价点重新导出一个 Excel。批量 Excel 可以覆盖完整回测区间；Excel Reader 每次返回源数据对象之前，必须按当前评价点的 `feature_date` 截断。平台生产时不读取这份固定 Excel，而是把当前 Request 的 `feature_date` 作为 `export_dataframe()` 的 `end_date`。

### 5.4 Excel 规则

- 上游提交交付脚本运行时实际读取的完整 Excel，不得另外制作一份“看起来相同”的样例数据代替。
- Excel 必须由随包提交的统一 `wind_export.py` 按所声明参数导出。
- SOP 不强制 Excel sheet 名、列顺序、index 类型或算法内部 dtype；上游原有 Reader 必须能直接读取提交的 Excel。
- 文件路径必须相对于交付包根目录，不得使用个人电脑绝对路径。
- 如果单点或批量脚本运行时读取训练 Excel、日历 Excel 或其它辅助文件，这些文件也必须提交。
- 只在交付前离线训练使用、运行时不再读取的数据不需要提交；训练生成且运行时读取的模型文件必须提交。
- 如果算法每次预测时重新训练，则其运行时读取的训练 Excel 必须提交。

---

## 6. 算法代码边界和运行方式

### 6.1 只强制逻辑边界，不强制目录结构

上游可以保留原有代码目录和文件名，不需要强制拆成 `io/`、`algorithm/`、`models/`、`config/`。

但是必须能够明确指出三个逻辑入口：

```text
Excel Reader → Frozen Core → Excel Writer
```

这是平台能够只修改输入和输出、不修改算法内部逻辑的前提。

三个入口的 Python 模块名和函数名可以由上游自定，但必须在 `delivery.yaml` 中用 `模块路径:函数名` 声明。例如：

```text
project.reader:load_from_excel
project.algorithm:predict
project.writer:write_to_excel
```

三个入口可以放在任意目录，但 Excel Reader、Frozen Core、Excel Writer 必须位于互不重叠的代码文件中。否则平台修改 Reader 或 Writer 时会同时改变 Frozen Core，无法证明算法核心保持不变。

### 6.2 三个入口的最小语义

函数名可以不同，但行为必须等价于：

```python
def load_from_excel(feature_date: str) -> object:
    """读取交付包内的实际 Excel，返回本算法需要的源数据对象。"""


def predict(
    source_data: object,
    *,
    predict_date: str,
    feature_date: str,
    target_date: str,
) -> int:
    """运行 Frozen Core，只返回 -1、0、1 中的一个方向。"""


def write_to_excel(
    *,
    target_date: str,
    predicted_direction: int,
    output_file: str,
) -> None:
    """把标准信号写入上游结果 Excel。"""
```

`object` 表示算法内部数据结构不受平台限制，可以是 DataFrame、dict、list、numpy 数组或它们的组合。平台不要求上游为此定义统一 dtype 或统一 schema。

### 6.3 Reader、Frozen Core、Writer 和 runner 的严格边界

#### Excel Reader

Excel Reader 只负责把本地文件转换成截至 `feature_date` 的源数据对象。

允许：

- 读取 `delivery.yaml` 已声明的 Excel；
- 选择上游实际使用的 sheet；
- 解析日期、index 和 Excel 序列化产生的基础类型；
- 按 `source_date <= feature_date` 执行数据硬截止；
- 把多个 dataset 放入 DataFrame、dict、list 等容器，但不做业务合并。

禁止：

- 业务字段筛选、缺失值业务处理和样本筛选；
- 按业务键合并、对齐或聚合不同 dataset；
- 构建特征、训练或加载模型；
- 模型选择、投票、fallback 和方向映射；
- 读取未在 `delivery.yaml` 声明的文件。

平台接入时，DB Input Adapter 必须替代上述 Reader 职责：使用统一 `wind_export.py` 和 dataset 参数取得数据，执行相同 `feature_date` 硬截止，并向 Frozen Core 提供同一逻辑源数据对象。

#### Frozen Core

以下所有会影响预测结果的处理都必须位于 Frozen Core：

- 业务字段筛选、清洗、缺失值规则、合并、聚合和数据对齐；
- 算法内部 Input 和特征构建；
- 训练、模型加载和模型文件；
- 参数、算法配置、模型选择、投票和 fallback；
- 最终方向映射。

`fallback` 是算法正常路径无法得到信号时，上游预先定义的算法回退规则。fallback 属于算法逻辑，平台不能增加或修改。

Frozen Core 的运行输入只能来自函数参数，以及 `frozen_core_paths` 中已经声明并冻结的模型和配置文件；它最终只返回算法信号。它不得读取源数据 Excel、数据库或网络，不得写文件，不得引用个人电脑绝对路径，也不得 import Excel Reader、Excel Writer 或 runner。

#### Excel Writer

Excel Writer 只负责把 Frozen Core 已经产生的结果写入 Excel。

允许：

- 从 Request 原样复制 `target_date`；
- 把已经确定的 `predicted_direction` 写入声明的结果 sheet；
- 创建结果文件所需的表头和 Excel 格式。

禁止：

- 修改、取反、四舍五入或重新计算 `predicted_direction`；
- 根据异常、实际值或文件状态补方向；
- 执行模型逻辑、fallback 或任何业务判断；
- 把运行失败转换成方向 `0`。

#### runner

runner 只负责流程编排：解析 Request 或批量区间、生成日期、依次调用 Reader、Frozen Core、Writer，并原样传播失败。runner 不得包含清洗、特征、模型、投票、fallback 或方向映射。

#### 文件和依赖边界

- `algorithm_entrypoint` 必须位于 `frozen_core_paths` 内；
- Excel Reader、Excel Writer 和 runner 所在文件不得列入 `frozen_core_paths`；
- `frozen_core_paths` 不得与 Reader、Writer 或 runner 文件重叠；
- runner 可以调用三层入口；Frozen Core 不得反向 import Reader、Writer 或 runner；
- 平台只替换 Reader、Writer 及其调用接线，不得修改 `frozen_core_paths` 中任何文件。

平台接入时会对 `frozen_core_paths` 生成文件摘要。核心代码、模型或配置发生变化时，必须升级 `algorithm_version` 并重新交付。

### 6.4 单点 runner

上游必须提供一个可从交付包根目录执行的单点命令。脚本文件名不强制，但命令必须接受：

- `{request_file}`：平台生成的三日期 Request 文件；
- `{output_file}`：本次结果 Excel 的输出路径。

示例：

```text
{python} predict_runner.py --request {request_file} --output {output_file}
```

单点 runner 的固定流程是：

```text
读取 Request
→ 调用 Excel Reader
→ 调用 Frozen Core
→ 调用 Excel Writer
→ 生成一个标准信号
```

### 6.5 批量回测 runner

上游必须提供一个可从交付包根目录执行的批量命令。脚本文件名不强制，但命令必须接受：

- `{target_start}`；
- `{target_end_exclusive}`；
- `{target_rule}`；
- `{output_file}`。

示例：

```text
{python} backtest_runner.py \
  --target-start {target_start} \
  --target-end-exclusive {target_end_exclusive} \
  --target-rule {target_rule} \
  --output {output_file}
```

批量 runner 必须：

1. 用正确交易日历展开区间内所有评价点；
2. 为每个评价点生成三个日期；
3. 每个评价点单独按 `feature_date` 截断数据；
4. 调用与单点 runner 完全相同的 Frozen Core；
5. 每个 `target_date` 输出恰好一条信号。

单点和批量不得各自实现一套算法。同一组日期、相同输入和相同算法版本必须产生相同方向。

命令失败时必须返回非零退出码，不得把异常转换成方向 `0`。方向 `0` 只能是算法主动产生的有效信号。

---

## 7. 输出规范

### 7.1 单点输出

单点结果必须包含且只依赖两个标准字段：

```json
{
  "target_date": "2026-07-20",
  "predicted_direction": -1
}
```

| 字段 | 规则 |
|---|---|
| `target_date` | 必须与 Request 完全一致 |
| `predicted_direction` | 必须是整数 `-1`、`0`、`1` |

方向含义固定为：

- `1`：预测目标收益率高于 `target_rule` 规定的基准收益率；
- `-1`：预测目标收益率低于基准收益率；
- `0`：算法预测两者相同或给出中性信号。

上游不得在不同运行阶段改变方向映射。

一个单点 Request 必须恰好输出一条结果。算法不能因为实际值尚未产生而不出信号；实际值缺失由平台后续处理。

上游 Excel 可以保留自己需要的其它诊断列，但平台只把 `target_date` 和 `predicted_direction` 视为标准输出，也不会依赖其它列完成入库。如果某个附加字段未来必须进入生产，则需另行确认，不得默认加入本 SOP。

### 7.2 批量输出

批量历史回测结果每行代表一个评价点，至少包含：

| 字段 | 规则 |
|---|---|
| `predict_date` | 该评价点的信号发出日 |
| `feature_date` | 该评价点的数据截止日 |
| `target_date` | 位于命令指定的左闭右开区间内 |
| `predicted_direction` | 整数 `-1`、`0`、`1` |

同一个 `target_date` 必须恰好一行，不得缺少或重复。

`actual_direction`、准确率和其它上游评价指标可以附在同一文件或另一个报告中，但不属于平台算法输出契约。实际值缺失时由平台处理，不能影响 `predicted_direction` 的生成。

### 7.3 Excel sheet

平台不强制结果 Excel 使用固定 sheet 名。上游必须在 `delivery.yaml` 中声明哪一个 sheet 保存标准结果；平台只读取被声明的 sheet。

结果 sheet 中字段名必须与第 7.1、7.2 节一致，日期统一写为 `YYYY-MM-DD`，`predicted_direction` 不得为空。平台比较的是 sheet 中的业务值，不要求两个 `.xlsx` 文件的二进制内容完全相同。

---

## 8. 最小交付包

### 8.1 不强制上游重排目录

交付包只固定根目录存在 `delivery.yaml` 和实际使用的 `wind_export.py`。其余代码、模型、配置、Excel 和结果可以保留原有相对目录：

```text
<方案交付包>/
├── delivery.yaml
├── wind_export.py
└── <其余算法代码、模型、配置、Excel、依赖和结果保持原结构>
```

所有被 `delivery.yaml`、运行命令或代码读取的文件都必须位于交付包内。不得包含个人电脑绝对路径、指向包外的 `..` 路径或符号链接。

### 8.2 必须存在的逻辑内容

| 逻辑内容 | 是否固定目录 | 说明 |
|---|---|---|
| `delivery.yaml` | 根目录固定 | 唯一交付说明文件 |
| `wind_export.py` | 根目录固定 | 上游实际使用的全平台统一脚本 |
| 算法代码、模型、配置 | 不固定 | 保持原结构，全部随包提交 |
| 运行时 Excel | 不固定 | 提交单点和批量脚本实际读取的文件 |
| 依赖文件 | 不固定 | `requirements.txt`、`environment.yml` 或其它可重建文件 |
| 单点样例 | 不固定 | 一个 Request 和对应结果 Excel |
| 历史回测证据 | 不固定 | 标准逐点输出必须提交；效果摘要格式不强制 |
| 灰度参考证据 | 不固定 | 已经完成时提交；没有时可以省略 |

“灰度参考证据”是上游使用本地 Excel 方案对近期 target date 区间生成的结果；“灰度实盘”是平台完成 DB Input Adapter 后实际运行产生的结果。二者用途不同，不能把本地灰度参考称为平台灰度实盘。

### 8.3 `delivery.yaml` 完整模板

下面的路径和命令都是示例，必须改成实际相对路径和实际命令：

```yaml
scheme:
  base_scheme_id: macro_lgbm_10y_t5
  algorithm_version: v1
  name: "10Y 宏观因子 LGBM T+5 方向方案"
  description: "使用日频和周频市场数据，预测10Y国债收益率未来5个交易日方向。"
  frequency: daily
  task_type: "T+5"
  horizon: 5
  target_tenor: "10Y"
  target_rule: nth_trading_day_after_feature

code:
  excel_reader_entrypoint: project.reader:load_from_excel
  algorithm_entrypoint: project.algorithm:predict
  excel_writer_entrypoint: project.writer:write_to_excel
  frozen_core_paths:
    - project/algorithm.py
    - project/features/
    - model/model.pkl
    - config/model_config.yaml

commands:
  single_point: >-
    {python} predict_runner.py --request {request_file} --output {output_file}
  batch: >-
    {python} backtest_runner.py --target-start {target_start}
    --target-end-exclusive {target_end_exclusive}
    --target-rule {target_rule} --output {output_file}

inputs:
  datasets:
    - dataset_id: daily_full_history
      frequency: 日
      start_date: all_history
      end_date: feature_date
      excel_file: data/wind_daily.xlsx
    - dataset_id: weekly_since_2018
      frequency: 周
      start_date: "2018-01-01"
      end_date: feature_date
      excel_file: data/wind_weekly.xlsx

runtime:
  python_version: "3.12.4"
  dependency_file: requirements.txt
  setup_command: "{python} -m pip install -r requirements.txt"

sample:
  request_file: examples/request.json
  expected_output_file: examples/single_point_output.xlsx
  output_sheet: result

backtest:
  target_start: "2025-01-01"
  target_end_exclusive: "2026-06-01"
  output_file: results/backtest_output.xlsx
  output_sheet: predictions
  summary_file: results/backtest_summary.xlsx

upstream_confirmation:
  verified: true
  owner: "张三"
  verified_at: "2026-07-15T16:30:00+08:00"
```

字段说明：

- `entrypoint`：`模块路径:函数名`，用于让平台准确找到需要替换的两端和不得修改的算法入口。
- `frozen_core_paths`：所有会影响预测结果的代码、模型和配置路径；路径名称不固定，但内容必须完整。
- `commands`：平台可以直接执行的真实命令；花括号内容由平台运行时替换。
- `dependency_file`：能够重建上游验证环境的依赖文件。
- `setup_command`：平台在隔离环境中安装依赖的真实命令；`{python}` 由平台替换为该隔离环境的 Python 解释器。
- `output_sheet`：上游自行命名，只负责告诉平台标准结果在哪里，不是平台强制 sheet 名。
- `summary_file`：上游效果摘要，可使用原有格式；没有独立摘要文件时直接省略该字段。
- `upstream_confirmation.verified`：正式交付时必须是 YAML 布尔值 `true`，表示上游已经接受算法正确性和模型效果。

不存在的 `frozen_core_paths` 示例项必须删除，不能为了匹配模板创建空目录。所有路径均相对于交付包根目录。

使用 conda 时，可以改为：

```yaml
runtime:
  python_version: "3.12.4"
  dependency_file: environment.yml
  setup_command: "conda env create -p {environment_dir} -f environment.yml"
```

`{environment_dir}` 由平台替换为本方案的隔离环境目录。上游不得在命令中填写个人绝对路径或固定个人环境名。单点和批量命令中的 `{python}` 始终指向平台创建完成后的环境解释器。

已经完成灰度参考时，再增加以下可选段；没有灰度参考时完全省略，不写一组 `null`：

```yaml
gray_reference:
  target_start: "2026-06-01"
  target_end_exclusive: "2026-07-01"
  output_file: results/gray_reference_output.xlsx
  output_sheet: predictions
```

### 8.4 平台收包后自行生成的内容

以下内容不由上游提交：

- 全包文件摘要；
- 平台内部输入和输出契约；
- DB Input Adapter 和 Output Adapter；
- 平台内部方案配置和注册信息；
- 灰度、正式调度和落库配置；
- 平台验收报告。

平台生成这些内容时，不得改变 Frozen Core。

---

## 9. 上游交付步骤

### 第一步：完成本地算法验证

上游自行确认算法逻辑、模型效果、历史回测和结果正确性。平台不替上游重新决定算法是否有效。

### 第二步：整理原样运行包

保留原有项目结构，加入 `delivery.yaml`，并确认其中声明的代码、Excel、依赖、样例和证据文件全部存在。

### 第三步：验证单点命令

从交付包根目录执行 `commands.single_point`，使用 `sample.request_file`，生成的标准结果必须与 `sample.expected_output_file` 一致。

### 第四步：验证批量命令

从交付包根目录执行 `commands.batch`，使用 `backtest` 中的 target date 区间和 `scheme.target_rule`。输出必须覆盖区间内全部评价点，并逐点按 `feature_date` 截断。

### 第五步：冻结并一次性交付

把 `upstream_confirmation.verified` 设为 `true`，填写实际负责人和时间，然后一次性提交完整包。平台发现问题时退回完整包；上游修改后重新提交新的完整版本，不使用零散文件覆盖已经冻结的版本。

---

## 10. 直接退回条件

出现任一情况，平台直接退回：

- 一个方案预测多个期限或多个任务；
- 方案身份、任务字段或 `target_rule` 不完整、不合法；
- 实际使用的 `wind_export.py` 与全平台批准版本不一致；
- 使用自定义 SQL、另一套查询函数或未声明的查询参数；
- 交付脚本读取了未提交的 Excel、模型、配置或辅助文件；
- 运行时 Excel 不是实际运行文件，或不是由声明的统一导出脚本产生；
- Excel Reader、Frozen Core、Excel Writer 无法明确区分，或对应代码文件与 `frozen_core_paths` 重叠；
- Reader 包含业务清洗、合并、特征、模型或方向逻辑，或读取未声明文件；
- Frozen Core 直接读取 Excel、连接数据库、网络或写结果文件，或反向 import Reader、Writer、runner；
- Writer 修改方向、执行 fallback，或根据异常和实际值补方向；
- runner 包含日期编排和调用之外的算法逻辑；
- 单点和批量使用不同算法实现；
- 批量运行没有对每个评价点按 `feature_date` 截断；
- 周频直接计算周编号，或月频把自然 15 日 `predict_date` 顺延；
- 使用目标日实际值或晚于 `feature_date` 的数据；
- 单点没有恰好输出一个 `target_date + predicted_direction`；
- 批量存在 target date 缺失、重复或非法方向；
- 运行失败被转换为方向 `0`；
- 单点样例、批量结果或运行环境无法复现；
- `dependency_file` 缺失、`setup_command` 不可执行，或环境安装依赖未声明的个人路径；
- 代码、模型、配置变化后没有升级 `algorithm_version`；
- `delivery.yaml` 引用不存在的文件、个人绝对路径或包外路径；
- `upstream_confirmation.verified` 不是布尔值 `true`。

---

## 11. 最终检查表

- [ ] 一个方案只预测一个 `target_tenor` 和一种任务
- [ ] `base_scheme_id`、版本、name、description 和五个任务字段完整
- [ ] 实际使用的全平台统一 `wind_export.py` 已提交
- [ ] 每个 dataset 的频率、起点、截止规则和实际 Excel 已声明
- [ ] 单点和批量运行时读取的全部 Excel 与辅助文件已提交
- [ ] 离线训练数据没有被运行脚本读取；运行时重新训练所需 Excel 已提交
- [ ] 代码保持原有结构，但 Reader、Frozen Core、Writer 三个入口位于互不重叠的文件
- [ ] Reader 只读取声明文件、解析格式并按 `feature_date` 截止，不包含业务或模型逻辑
- [ ] `frozen_core_paths` 覆盖全部算法代码、模型和配置，且不含 Reader、Writer、runner
- [ ] Writer 只写 `target_date + predicted_direction`，不修改算法方向
- [ ] Request 只包含 `predict_date`、`feature_date`、`target_date`
- [ ] 单点命令和批量命令均能从交付包根目录运行
- [ ] 日频、周频、月频批量脚本均按第 4.2 节展开三个日期并逐点截断
- [ ] 单点输出恰好包含一个有效 `target_date + predicted_direction`
- [ ] 批量输出包含四个必填字段，target date 无缺失、无重复
- [ ] 实际值缺失不会阻止算法产生信号
- [ ] 结果 sheet 名已声明，但没有为了平台强制改名
- [ ] 单点样例、历史逐点结果和依赖文件完整可复现
- [ ] `setup_command` 能在隔离环境中安装依赖，运行命令使用平台提供的 `{python}`
- [ ] `upstream_confirmation.verified` 已由实际负责人确认
- [ ] 包内没有个人绝对路径、包外依赖或未提交运行文件

全部勾选后再提交。
