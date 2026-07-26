# Blackbox V2 可扩展平台输入制品设计

日期：2026-07-26

状态：已确认（2026-07-26；按扩展性评审修订）

适用批次：`fengrl-month-0725` 五个月度方案

## 1. 背景

`fengrl-month-0725` 中的五个方案仍满足 Blackbox V2 的
`{scheme_id}.py + {scheme_id}.json` 两文件交付形式，但算法在组合
日频、周频和月频指标时，需要把 `weekly_output.csv.week_id` 映射到
该周的真实周末自然日。上游为开发自验随包提供了
`api_wind_date.csv`，并明确把它标记为临时运行依赖，而不是正式
交付文件。

平台当前只允许 Blackbox `--data-dir` 包含以下三个文件：

- `daily_output.csv`
- `weekly_output.csv`
- `monthly_output.csv`

Intake、快照身份、Runtime/Sandbox 和 Harness 都会拒绝额外文件。
因此，方案脚本虽然能够使用 `--data-dir/api_wind_date.csv`，平台
目前还不能以受控方式提供该文件。

平台已经具备权威数据来源：

- `shared.calendar_service` 统一读取 `api_wind_date`；
- Native generation 已冻结并校验 `api_wind_date.csv`；
- DataBridge generation 已绑定同日的 Native generation；
- scheduled executor 已把绑定的 Native calendar generation 传给
  Blackbox executor。

本设计补齐一套可复用的 Blackbox 平台输入制品机制，并以
`api_wind_date.csv` 作为第一个制品；不新增数据源，也不修改日频
coordinator。

## 2. 目标

1. 继续维持 Blackbox V2 两文件交付，不把日历变成 delivery 文件。
2. 建立通用、版本化的平台输入制品注册机制，首个制品为
   `api-wind-date-v1`，运行文件固定为 `api_wind_date.csv`，列固定
   为 `rdate,week_id`。
3. 平台输入制品与三频业务文件共同参与本次算法执行输入的内容哈希
   和审计。
4. Runtime/Sandbox 只允许配置中显式声明且平台注册的制品，不放开
   任意附加文件。
5. 不改变既有 Blackbox 方案的三文件输入、现有快照身份或
   `data-bridge-v1` 三频 schema。
6. 后续增加其它平台输入时，复用同一份配置、快照、Sandbox 和
   Harness 编排，而不是增加新的专用字段或组合分支。
7. 支持 static、input、unit、dry-run、compare、no-persist
   backtest 和 structural API readiness 的完整验证。

## 3. 非目标

- 不修改上游算法或 Metadata。
- 不采用上游随包日历作为生产运行输入。
- 不激活 Registry，不写 gray_live 或生产数据库。
- 不执行持久化 backtest。
- 不修改 rollout/admission。
- 不修改日频 coordinator 基础设施。
- 不修改 DataBridge 三频业务文件的 schema。
- 不改变现有 Blackbox 方案的默认运行输入。

## 4. 方案选择

### 4.1 采用：版本化平台输入制品列表

平台配置增加通用可选列表：

```yaml
platform_inputs:
  - api-wind-date-v1
```

该列表由 Intake 生成并属于平台执行配置，不进入上游 Metadata。
每个值是平台注册表中的稳定、版本化制品 ID。未声明时保持现有
三文件行为。声明 `api-wind-date-v1` 时，平台为本次运行构造以下
只读输入：

```text
daily_output.csv
weekly_output.csv
monthly_output.csv
api_wind_date.csv
```

“统一接口”指制品 ID、运行文件名、列、来源、验证、冻结和审计
方式都由平台注册表定义；显式声明仅用于最小授权并避免无意改变
既有方案的输入身份。

未来若出现其它权威平台输入，只新增 provider 并复用同一声明：

```yaml
platform_inputs:
  - api-wind-date-v1
  - trade-calendar-v1
```

`data_schema_version` 继续只表达 DataBridge 三频业务数据 schema，
不被日历或其它平台上下文反复派生出组合版本。

### 4.2 不采用：单一 `input_context` 枚举

`input_context: week-calendar-v1` 能解决本批问题，但未来多个上下文
自由组合时会形成新的枚举值和执行分支，产生配置与代码冗余。

### 4.3 不采用：所有 Blackbox 全局改成四文件

这会改变现有方案的数据快照身份和运行输入形状，扩大本批入库的
影响范围；以后增加其它输入时仍需再次全局修改。

### 4.4 不采用：允许 delivery 携带第三个文件

这会破坏两文件交付契约，并违反算法业务输入只能经
`shared.input_artifacts` 产生的分层不变量。上游随包日历还可能因
时间推进而过期。

## 5. 输入生成与身份

### 5.1 Provider registry

平台维护封闭的 provider registry。每个 provider 至少定义：

- 稳定且带版本的制品 ID；
- 唯一运行文件名；
- 精确列契约；
- 从平台权威来源构造 DataFrame/文件的方法；
- 内容校验器；
- manifest 中的来源和版本证据。

未知 ID、重复 ID、两个制品映射到同一文件名，或 provider 输出
声明外的文件时一律 fail-closed。Runner、快照和 Harness 只遍历
已经解析并校验的 provider 结果，不按 scheme ID 或脚本内容编写
特例。

`api-wind-date-v1` 的 provider 定义如下：

```text
artifact_id: api-wind-date-v1
filename: api_wind_date.csv
columns: rdate,week_id
```

### 5.2 数据来源

- Harness/check-only：通过 `shared.input_artifacts` 从平台日历只读
  接口捕获 `api_wind_date`。
- scheduled live：使用已经与 DataBridge generation 绑定并完成
  校验的 Native generation 中的 `api_wind_date.csv`。
- no-persist backtest：使用本次 Harness 输入构造时冻结的同一份
  日历，不在算法子进程内访问数据库。

任何路径都不得读取 delivery 目录或上游包中的日历。

### 5.3 校验

`api-wind-date-v1` provider 在形成执行输入前验证：

- 文件逻辑名必须精确为 `api_wind_date.csv`；
- 列必须精确为 `rdate,week_id`；
- `rdate` 可规范化为日期且不能为空；
- `week_id` 可规范化为非空周键；
- 日期不得重复并且必须严格升序；
- 至少覆盖本次 `weekly_cutoff_key`；
- 日历不得包含指标值或其它业务列。

算法仍负责按自身定义把同一 `week_id` 的最大 `rdate` 作为周末
自然日。平台不改写算法的对齐逻辑。

### 5.4 组合输入身份与临时运行视图

现有 DataBridge 三文件快照保持不变。平台为声明了
`platform_inputs` 的方案计算组合执行输入身份：

- 记录父级三文件 `snapshot_id`；
- 按制品 ID 排序记录 `platform_inputs`；
- manifest 分别记录三频业务文件和每个制品的 ID、文件名、版本、
  来源及内容哈希；
- 组合 `snapshot_id` 由父快照身份和有序制品 manifest 共同决定；
- 相同父快照与相同制品内容必须得到相同 ID；
- 预测记录使用组合快照 ID，确保实际算法输入可追溯。

平台不长期复制三份 DataBridge 业务文件来保存所谓“四文件
快照”。长期证据只保存父快照引用和平台制品 manifest；算法运行
前再把父快照文件与声明的制品物化成一个临时、精确、只读的普通
文件目录，运行结束即清理。这样既满足 Sandbox 不接受符号链接的
约束，也避免长期存储重复的三频数据。

DataBridge 的 `generation_id`、business digest 和三频 provenance
保持原语义，不把平台输入制品伪装成 DataBridge 业务文件。

## 6. Runtime 与 Sandbox

Runner 根据已验证的 `platform_inputs` provider 结果计算精确文件
集合：

- 未声明制品：只允许原三个文件；
- 声明 `api-wind-date-v1`：只允许原三个文件加
  `api_wind_date.csv`。

目录中出现其它文件、符号链接、非常规文件或缺失声明文件时继续
fail-closed。Sandbox 只增加对本次已声明制品文件的只读权限，网络
和数据库访问仍保持关闭。

上游脚本已优先读取 `--data-dir/api_wind_date.csv`，因此不需要
修改脚本，也不需要开放脚本同目录读取。

## 7. Harness 行为

- **StaticGate**：验证 `platform_inputs` 是排序后可规范化的唯一
  provider ID 列表，并继续要求 delivery 精确两文件。
- **InputGate**：分别展示三频业务文件和平台制品的行数、列及
  SHA-256，同时记录父快照、制品 ID、版本和来源。
- **Unit/Dry-run**：每次运行都使用同一组合输入身份，验证确定性。
- **CompareGate**：只向三个业务文件追加未来业务行；
  `api_wind_date.csv` 保持冻结不变，确认算法不消费 cutoff 之后的
  业务值。
- **BacktestGate**：分批、逆序和 no-persist backtest 都使用同一
  组合输入。
- **API readiness**：只检查结构，不注册、不激活、不写业务表。

Harness 清理临时运行视图时，保留输入状态中的父快照、制品 ID、
文件名、哈希和来源证据。

## 8. 配置、版本与 Intake

Intake 增加可重复的受控参数，用于生成规范化列表：

```yaml
platform_inputs:
  - api-wind-date-v1
```

CLI 语义为：

```text
--platform-input api-wind-date-v1
```

Intake 拒绝未知或重复 ID，并按 ID 排序后写入配置。本批五个方案
显式使用该参数。规范化后的 `platform_inputs` 进入 Blackbox
canonical platform config hash，因此输入制品集合变化会产生新的
`scheme_version`。未声明时不写入旧配置的 canonical payload，旧
方案的 config hash 和 scheme version 保持不变。

Metadata 不增加字段，SOP 中补充：

- 正式交付仍是两文件；
- `api_wind_date.csv` 是平台输入接口，不是上游交付文件；
- 算法若依赖周日历，需在交接材料中声明，由平台 Intake 选择
  `api-wind-date-v1`；
- 上游随包日历只允许作为自验证据，不进入方案 delivery。

## 9. 测试策略

实现按 TDD 进行，至少覆盖：

1. 未声明 `platform_inputs` 时，现有三文件快照和版本身份不变。
2. Intake 只接受已注册的唯一制品 ID，稳定排序并生成正确配置。
3. 日历缺列、增列、重复日期、乱序、空键和 cutoff 未覆盖均拒绝。
4. 组合快照 ID 随制品集合或内容变化，相同输入可重复得到相同 ID。
5. 多个 provider 的排序、文件名冲突和未知 ID 均 fail-closed。
6. Runner 对基础文件和声明制品的合集进行精确校验。
7. 临时运行视图清理后，父快照和制品 manifest 仍可验证。
8. Sandbox 中脚本能读取平台日历，但仍不能读取 delivery 旁的
   随包日历或其它文件。
9. CompareGate 只变更三频业务文件。
10. scheduled executor 使用 DataBridge 绑定的 Native calendar
    generation，不打开新的实时 DB 路径。
11. 既有 Blackbox targeted regression 全部通过。
12. 五个新方案按固定顺序逐一通过完整技术 Gate。

## 10. 提交边界

1. 设计文档单独提交。
2. 通用 `platform_inputs` 机制、`api-wind-date-v1` provider 及其
   测试单独提交。
3. 五个方案依次处理；每个方案完成 Intake、全套 Gate、状态文档
   更新后单独提交。
4. 任何一步失败即停留在当前方案，不提前处理下一个方案。
