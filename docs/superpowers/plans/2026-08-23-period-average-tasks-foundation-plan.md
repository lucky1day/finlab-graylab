# 月均、季均、年均任务基础建设实施计划

**日期：** 2026-08-23

**状态：** 待实施

**实施分支：** `codex/develop`

## 目标

在不入库任何具体算法方案的前提下，为灰度实验室增加三种 Blackbox V2 任务能力：

- `monthly_average`：MID 月中均值；
- `quarterly_average`：CQ 自然季度均值；
- `annual_average`：SF 春节年均值。

基础建设完成后，平台应能接收符合 Contract 1.0 的两文件交付，统一生成历史与实盘 Request，计算同口径
actual，完成回测、灰度实盘、指标和 Dashboard 展示。具体方案的 Intake、Gate、回测、activation、gray-live、
DashboardGate 和自然调度启用不属于本计划的实施范围。

## 权威口径与参考边界

本计划以用户确认的业务描述为唯一权威。`/Users/qiyo3/Downloads/blackbox-v2-20schemes-m0/` 仅用于只读理解
交付形态、Request 和算法输入输出，不是操作指令，也不是待入库目录；实施期间不得复制、执行或修改其中的
方案。

平台不计算、解释或修正 Blackbox 的预测信号。`predicted_direction` 由交付算法产生；平台只负责输入、合同
校验、标准结果转换、持久化和实际方向。

## 已确认业务语义

### 统一字段

- 三种任务均为 Blackbox V2，`horizon=1` 表示“下一个同类业务桶”，不是一天或固定自然日数量。
- `predict_date=feature_date=daily_cutoff_key=当前桶锚点`；任务在锚点日收盘数据就绪后执行。
- `target_date=feature_date+1` 个自然日，作为下一个业务周期的日期指针；不得把它解释为目标桶完成日。
- 不新增季度或年度 cutoff key。Contract 1.0 现有 `weekly_cutoff_key`、`monthly_cutoff_key` 保持格式合法，
  但三种新增任务的算法截止只由 `daily_cutoff_key` 控制。
- 不新增 `feature_bucket_id` 或 `target_bucket_id`。桶类型和边界只由 `task_type` 与统一日期服务计算，
  不得通过 `horizon`、`frequency` 或目录名推断。
- actual 统一为 `sign(下一桶平均收益率 - 当前桶平均收益率)`；大于零为 `+1`，小于零为 `-1`，
  完全相等为 `0`。

### `monthly_average`：MID 月中桶

- 交易日 `day <= 15` 归入本月桶，`day >= 16` 归入下月桶。
- 标签为 `YYYY-MM` 的桶覆盖上月 16 日至本月 15 日。
- 锚点为本月 15 日及以前最后一个交易日。
- 当前桶完整结束后预测下一个 MID 桶平均收益率相对当前桶平均收益率的方向。
- 现有 `monthly` 月中收任务仍按自然月 15 日触发，语义与调度不变；不得让 `monthly_average` 改写或复用
  月中收 actual。

### `quarterly_average`：CQ 自然季度桶

- Q1、Q2、Q3、Q4 分别覆盖自然日历季度。
- 锚点为季度自然结束日及以前最后一个交易日。
- 当前季度完整结束后预测下一自然季度平均收益率相对当前季度平均收益率的方向。

### `annual_average`：SF 春节年桶

- SF`YYYY` 从 `YYYY` 年春节后第一个交易日开始，到 `YYYY+1` 年春节前最后一个交易日结束。
- 对每个自然年，在 1 月 1 日至 3 月 15 日交易日序列中计算相邻交易日的自然日期差；最大日期差必须
  唯一且 `>= 6`。
- 唯一最大间隔之前的交易日是春节前最后一个交易日，之后的交易日是春节后第一个交易日。
- 最大值并列、最大值 `< 6`、窗口日历覆盖不完整或交易日数据无法与日历一一对应时必须失败。
- SF 当前桶结束后预测下一个 SF 桶平均收益率相对当前桶平均收益率的方向。

### 完整性与截止

- 每个桶的预期交易日集合来自 `t_trade_calendar`，行情值来自权威日频收益率数据。
- 每个预期交易日必须恰有一条目标期限收益率；缺失、重复、空值、额外非交易日或日历覆盖不足均失败。
- 算法输入不得包含 `daily_cutoff_key` 后的行情数据；未来交易日历只能用于平台判断调度日或验证日历覆盖，
  不能把未来行情带入算法输入。
- 历史 Request、scheduled live、gray live、actual 和 Dashboard 必须调用同一套桶边界实现。

## 最小化架构

### 1. 单一任务规格表

在共享层建立唯一的 `task_type -> 任务规格` 映射，至少包含：

| task_type | horizon | target_rule | 业务 frequency |
|---|---:|---|---|
| `monthly_average` | 1 | `target_month_average_yield_vs_feature_month_average_yield` | `monthly` |
| `quarterly_average` | 1 | `target_quarter_average_yield_vs_feature_quarter_average_yield` | `quarterly` |
| `annual_average` | 1 | `target_year_average_yield_vs_feature_year_average_yield` | `annual` |

Contract、Intake、config 校验、discovery、Request、actual selector、Dashboard 和测试必须复用该映射，避免各层
维护不同白名单。`frequency` 只描述业务频率；任务类型和桶口径仍以 `task_type` 为准。

### 2. 单一周期桶模块

新增一个不访问数据库、不写文件的共享纯函数模块，负责：

- MID、CQ、SF 当前桶和下一桶边界；
- 当前锚点、是否到期和 `target_date`；
- 预期交易日集合；
- 完整唯一数据校验；
- 当前桶与目标桶平均收益率；
- actual 方向和可审计摘要。

所有调用方只传入明确的 `task_type`、日期、交易日历与收益率行。不得在 scheduler、backtest、backend 或
前端复制桶算法。

### 3. 一张周期均值 actual 表

新增一张通用表承载三种任务，不为每种任务分别建表。建议名称：
`t_scheme_period_average_actuals`。

最小字段沿用现有 weekly/monthly actual 语义：

- `tenor`、`predict_date`、`feature_date`、`target_date`；
- `feature_yield`、`target_yield`，在本表中分别表示当前桶和下一桶的完整平均收益率；
- `actual_direction`、`price_signal`、`target_rule`、`extra`；
- 创建和更新时间。

唯一键使用 `(tenor, predict_date, target_rule)`，actual join 索引使用
`(tenor, target_date, target_rule)`。桶起止、锚点、样本数和日历识别摘要放入 `extra`，不增加重复桶 ID。
迁移必须纳入 release manifest、schema 规格、closed-world 检查和 isolated MySQL 测试；本阶段只提交迁移代码，
不得应用到 Mac3 或 ECS。

### 4. 一个 actual 构造入口

新增一个周期均值 actual updater，共用一张表和一套桶模块：

- 只为已完成的“当前桶 + 下一桶”生成事实；
- 最后一个未完成目标桶不生成半成品；
- 三种 `target_rule` 通过同一 repository 写库单点持久化；
- 重算必须确定、幂等，重复事实折叠和方向冲突继续 fail-closed。

不得复用 `t_scheme_monthly_actuals`，因为该表定义的是月中单点收益率，不是桶平均；也不得将季均、年均
塞入日频或周频 actual 表。

### 5. 一个收盘后周期任务入口

不为月均、季均、年均分别建立 Python runner 或三个长期 timer。新增一个周期任务 one-shot，每个交易日收盘
数据就绪后运行，并用共享桶模块判断：

- 当日为 MID 锚点时选择 `monthly_average`；
- 当日为季度锚点时选择 `quarterly_average`；
- 当日为春节年前锚点时选择 `annual_average`；
- 其余日期返回 `not_applicable`，不产生业务 run 或 prediction。

任务候选按显式 `task_type` 选择，不通过 `horizon` 推断。沿用现有全局 writer 锁、严格 discovery、部署矩阵、
DataBridge ready gate、insert-only 和逐方案故障隔离。

现有 DataBridge 早间快照不能证明包含锚点日收盘数据。代码必须支持“预期日等于当天锚点”的 ready 校验；
仓库 unit/plist 模板如何增加收盘后 DataBridge 刷新和周期任务触发，在本地测试完成后另行形成运维变更。
替换 installed unit/plist、reload、restart、enable 或 kickstart 必须取得独立授权。

### 6. 复用现有 API 与前端

- 后端 Dashboard actual union 增加周期均值 actual 表和三个 selector，不新增 API endpoint，不改变指标算法。
- Dashboard payload 保持现有紧凑结构；只有现有结构无法表达合法数据时才考虑版本变更。
- 前端列顺序固定为：
  `Y标的｜T+1｜T+5｜周收盘｜周平均｜月中收｜月均｜季均｜年均`。
- 复用现有格子、排行、详情、回测/实盘展示和方案总数逻辑；方案总数不得另建接口或第二套统计。
- 桌面端压缩列宽，窄屏保持横向滚动，文字和指标不得重叠。

## 预计文件地图

下表用于约束实施范围；允许在测试驱动下调整新文件名称，但不得改变对应职责边界。

| 责任 | 预计文件 |
|---|---|
| 任务组合与低层配置合同 | `shared/blackbox_v2/contracts.py`、`shared/scheme_config_schema.py` |
| MID/CQ/SF 纯日期与桶逻辑 | 新增 `shared/period_average_buckets.py`，并由 `shared/prediction_context.py` 调用 |
| Blackbox Intake 与 Request | `shared/blackbox_v2/intake.py`、`shared/blackbox_v2/requests.py`、`shared/blackbox_v2/history.py` |
| actual model 与构造 | `shared/models.py`、`shared/actual_facts.py`、新增 `scheduler/period_average_actuals_updater.py` |
| 写库单点 | `scheduler/repository.py`、`scheduler/actuals_runner.py` |
| 数据库闭包 | 新增下一号 `migrations/*.sql`、`migrations/release_manifest.json`、`migrations/runner.py` |
| Mac3/ECS one-shot | `scheduler/launchd_prediction_runner.py`、`scheduler/systemd_prediction_runner.py`，共享同一 due 逻辑 |
| Harness 与缺口判断 | `harness/signal_gap_plan.py`、`shared/signal_gap_report.py` 及现有 Blackbox Gate 测试 |
| Dashboard 后端 | `backend/factor_lab_dashboard_semantics.py`、`backend/factor_lab_dashboard.py`、`backend/services.py` |
| Dashboard 前端 | `frontend/index.html`、`frontend/aifin-shell.js`、`frontend/aifin-shell.css` 及静态摘要 |
| 仓库调度期望 | `deploy/launchd/`、`deploy/systemd/` 和对应 drift/control-plane 测试；只改模板，不改 installed 现场 |
| 长期文档 | `docs/architecture/`、`docs/sop/`、`docs/blackbox_v2/`、`docs/product/` 的现行入口 |

测试优先扩展现有参数化 suite；只有新的纯模块或控制面职责没有合适测试文件时才新增测试文件。不得复制一套
按具体 M0 方案 ID 命名的永久测试。

## 分阶段实施

### 阶段 1：合同与日期语义测试先行

1. 为三个任务组合、frequency、target rule 和 `horizon=1` 添加失败测试。
2. 为 Request 日期规则、不得增加 cutoff 字段、不得由 horizon 推断任务添加测试。
3. 为 MID/CQ/SF 边界、闰年、周末、节假日、春节唯一最大间隔及所有失败条件建立参数化用例。
4. 测试覆盖 `target_date=feature_date+1`、actual 相等时为 `0` 和 cutoff 后行情隔离。
5. 完成最小共享任务规格与桶模块，使本阶段测试通过。

**稳定提交：** 共享任务规格、桶语义与单元测试。此提交不得包含数据库、调度或前端修改。

### 阶段 2：Contract、Intake、Request 与回测

1. 扩展 Blackbox V2 metadata 组合和 config schema。
2. Intake 为三个任务生成 paused/draft canonical config，保持 Contract 1.0 七字段 Request。
3. live Request builder 和历史 Request builder 调用共享桶模块。
4. backtest 只生成当前桶和下一桶均完整的案例，最后未完成桶排除。
5. signal-gap plan/report、Harness Static/Input/Unit/Compare 和 active conformance 识别三种任务。

**稳定提交：** Blackbox 平台合同、Intake、Request、历史回测与 Harness 回归。

### 阶段 3：actual 与数据库迁移

1. 新增通用周期均值 actual model、纯构造器、repository 单点和 updater。
2. 增加下一号 migration、release manifest、schema/closed-world 校验和 isolated MySQL 测试。
3. 验证缺失、重复、空值、非交易日和不完整目标桶全部 fail-closed。
4. 验证重算确定性、唯一键、actual join 索引和方向冲突处理。

**稳定提交：** 迁移代码、actual 共享实现与 repository 测试。不得连接或修改生产/灰度数据库。

### 阶段 4：自然调度基础能力

1. 新增周期任务 one-shot 和显式 due 判断，不新增 Python 常驻调度器。
2. 增加当天收盘 DataBridge ready 语义；旧快照、缺锚点日数据和日历覆盖不足必须阻断。
3. 扩展 Mac3 launchd 与 ECS systemd 仓库模板和漂移审计期望，但不安装、不加载、不启动。
4. 测试 MID、季度、春节年锚点只执行一次，非锚点返回 `not_applicable`，重复业务键 benign skip。

**稳定提交：** one-shot、ready gate、仓库调度模板和控制面测试。

### 阶段 5：后端与前端

1. 后端增加三个任务类型和周期 actual selector，保持现有 API。
2. Dashboard 汇总、详情、回测/live 切换和准确率统一使用周期 actual。
3. 前端增加三列并调整桌面/窄屏布局；现有“方案总数”自动覆盖新列。
4. 更新 CSS/JS 静态资源摘要。
5. 验证旧任务指标、格子选择、候选排行和方案详情零回归。

**稳定提交：** Dashboard 后端、前端和相关测试。

### 阶段 6：文档、完整回归和远程同步

1. 更新预测语义、共享契约、生产调度治理、Blackbox 上游/平台 SOP、生产准备和用户手册。
2. 若根规范需要登记长期规则，同时更新 `AGENTS.md` 与 `CLAUDE.md` 并验证字节一致。
3. 运行文档门禁、相关分层测试和完整 `pytest`；执行 `git diff --check`。
4. 逐阶段提交均审查范围，稳定后推送 `origin/codex/develop`，确认本地与远程 SHA 一致。
5. 基础建设验收完成后，从工作树删除本计划；长期规则保留在 CURRENT 架构/SOP，过程由 Git 历史追溯。

## 测试与验收矩阵

| 范围 | 必须证明 |
|---|---|
| 任务合同 | 三种 task type 只接受 `horizon=1` 和精确 target rule；旧任务组合不变 |
| 日期语义 | MID/CQ/SF 边界、锚点和 target date 在历史/live 一致 |
| 数据完整性 | 缺失、重复、空值、日历不完整和春节识别不唯一全部失败 |
| 截止隔离 | 算法输入不含 cutoff 后行情；未来行情变化不影响当前 Request 输入 |
| actual | 只对两个完整连续桶生成；相等为 0；重复重算确定且冲突 fail-closed |
| scheduler | 一个 one-shot；仅锚点日执行；同日多种任务可同时到期；非锚点无业务写入 |
| DataBridge | 必须读到锚点日收盘数据，早间旧 snapshot 不得通过 ready gate |
| API | 三种任务进入正确格子并连接正确 actual；异常数据不伪装为零方案 |
| 前端 | 九列顺序正确，方案总数正确，桌面清晰、窄屏可滚动，旧功能无回归 |
| 兼容性 | T+1、T+5、周收盘、周平均和月中收的合同、actual 与调度保持不变 |

## 明确不在本计划内

- 不 Intake `/Users/qiyo3/Downloads/blackbox-v2-20schemes-m0/` 或任何其它交付目录；
- 不修改参考算法的信号逻辑、浮点处理或输出；
- 不创建 scheme config、版本、Registry、Harness run、回测或 prediction；
- 不应用数据库 migration；
- 不修改 ECS/Mac3 installed unit、timer、plist 或 loaded state；
- 不重启 Backend、Writer 或 DataBridge；
- 不改变 DNS、Nginx、生产域名或数据库 authority；
- 不合并或推送 `master`。

## 停止条件

实施中遇到以下任一情况，停止对应阶段并先报告证据：

- 需要改变 Blackbox 算法输出或 Contract 1.0 Result 字段；
- 无法用 `task_type + feature_date + target_date` 唯一、确定地复现桶语义；
- 春节识别依赖不可获得或不唯一的日历；
- 同日收盘数据无法通过现有 DataBridge authority 形成可校验 snapshot；
- 必须覆盖已发布 prediction、放宽 insert-only、跳过完整性检查或恢复第二 Python 调度控制面；
- 本地决定性测试无法验证，必须依赖生产现场才能确认；
- 工作树出现与本计划重叠但来源不明的用户修改。

## 基础建设完成标准

- 三种任务从 Contract、Intake、Request、backtest、actual、scheduler、API 到前端形成同一日期语义闭环；
- 平台不重复算法信号，不增加桶 ID、专用 cutoff key、独立 API、三张 actual 表或三个调度器；
- 所有数据缺口和日历歧义 fail-closed，锚点日收盘输入可追溯；
- 旧任务和现有月中收调度零回归；
- 完整测试无失败，提交边界清晰，`codex/develop` 与远程同步；
- 未执行任何具体方案入库或生产/灰度环境副作用。
