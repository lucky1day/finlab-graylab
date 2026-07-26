# Blackbox V2 平台周日历输入设计

日期：2026-07-26

状态：待用户确认

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

本设计只补齐 Blackbox 输入表达，不新增数据源，也不修改日频
coordinator。

## 2. 目标

1. 继续维持 Blackbox V2 两文件交付，不把日历变成 delivery 文件。
2. 由平台统一提供只读的 `api_wind_date.csv`，固定列为
   `rdate,week_id`。
3. 日历与三频业务文件共同参与本次算法执行输入的内容哈希和审计。
4. Runtime/Sandbox 只允许预先声明的日历上下文，不放开任意附加
   文件。
5. 不改变既有 Blackbox 方案的三文件输入和现有快照身份。
6. 支持 static、input、unit、dry-run、compare、no-persist
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

### 4.1 采用：显式平台输入上下文

平台配置增加可选字段：

```yaml
input_context: week-calendar-v1
```

该字段由 Intake 生成并属于平台执行配置，不进入上游 Metadata。
未声明时保持现有三文件行为。声明 `week-calendar-v1` 时，平台为
本次运行构造以下只读输入：

```text
daily_output.csv
weekly_output.csv
monthly_output.csv
api_wind_date.csv
```

“统一接口”指文件名、列、来源、验证、冻结和审计方式都由平台
定义；显式声明仅用于避免无意改变既有方案的输入身份。

### 4.2 不采用：所有 Blackbox 全局改成四文件

这会改变现有方案的数据快照身份和运行输入形状，扩大本批入库的
影响范围。

### 4.3 不采用：允许 delivery 携带第三个文件

这会破坏两文件交付契约，并违反算法业务输入只能经
`shared.input_artifacts` 产生的分层不变量。上游随包日历还可能因
时间推进而过期。

## 5. 输入生成与身份

### 5.1 数据来源

- Harness/check-only：通过 `shared.input_artifacts` 从平台日历只读
  接口捕获 `api_wind_date`。
- scheduled live：使用已经与 DataBridge generation 绑定并完成
  校验的 Native generation 中的 `api_wind_date.csv`。
- no-persist backtest：使用本次 Harness 输入构造时冻结的同一份
  日历，不在算法子进程内访问数据库。

任何路径都不得读取 delivery 目录或上游包中的日历。

### 5.2 校验

平台在形成执行快照前验证：

- 文件逻辑名必须精确为 `api_wind_date.csv`；
- 列必须精确为 `rdate,week_id`；
- `rdate` 可规范化为日期且不能为空；
- `week_id` 可规范化为非空周键；
- 日期不得重复并且必须严格升序；
- 至少覆盖本次 `weekly_cutoff_key`；
- 日历不得包含指标值或其它业务列。

算法仍负责按自身定义把同一 `week_id` 的最大 `rdate` 作为周末
自然日。平台不改写算法的对齐逻辑。

### 5.3 派生执行快照

现有 DataBridge 三文件快照保持不变。平台为声明了
`week-calendar-v1` 的方案创建派生执行快照：

- 记录父级三文件 `snapshot_id`；
- 记录 `input_context=week-calendar-v1`；
- manifest 分别记录三频业务文件和日历上下文文件；
- 派生 `snapshot_id` 由父快照身份、上下文版本和日历内容哈希共同
  决定；
- 四个文件全部只读；
- 预测记录继续使用派生快照 ID，确保实际算法输入可追溯。

DataBridge 的 `generation_id`、business digest 和三频 provenance
保持原语义，不把日历伪装成 DataBridge 业务文件。

## 6. Runtime 与 Sandbox

Runner 根据 `input_context` 计算精确文件集合：

- 无上下文：只允许原三个文件；
- `week-calendar-v1`：只允许原三个文件加
  `api_wind_date.csv`。

目录中出现其它文件、符号链接、非常规文件或缺失声明文件时继续
fail-closed。Sandbox 只增加对派生快照中该日历文件的只读权限，
网络和数据库访问仍保持关闭。

上游脚本已优先读取 `--data-dir/api_wind_date.csv`，因此不需要
修改脚本，也不需要开放脚本同目录读取。

## 7. Harness 行为

- **StaticGate**：验证 `input_context` 只取支持值，并继续要求
  delivery 精确两文件。
- **InputGate**：展示四个输入文件的行数、列和 SHA-256，同时记录
  父快照、上下文版本和日历来源。
- **Unit/Dry-run**：每次运行都使用同一派生快照，验证确定性。
- **CompareGate**：只向三个业务文件追加未来业务行；
  `api_wind_date.csv` 保持冻结不变，确认算法不消费 cutoff 之后的
  业务值。
- **BacktestGate**：分批、逆序和 no-persist backtest 都使用同一
  派生快照。
- **API readiness**：只检查结构，不注册、不激活、不写业务表。

Harness 清理派生运行快照时，保留输入状态中的路径、哈希和来源
证据。

## 8. 配置、版本与 Intake

Intake 增加受控参数，用于生成：

```yaml
input_context: week-calendar-v1
```

本批五个方案显式使用该参数。`input_context` 进入 Blackbox
canonical platform config hash，因此输入能力变化会产生新的
`scheme_version`。默认值不写入旧配置，旧方案的 config hash 和
scheme version 保持不变。

Metadata 不增加字段，SOP 中补充：

- 正式交付仍是两文件；
- `api_wind_date.csv` 是平台输入接口，不是上游交付文件；
- 算法若依赖周日历，需在交接材料中声明，由平台 Intake 选择
  `week-calendar-v1`；
- 上游随包日历只允许作为自验证据，不进入方案 delivery。

## 9. 测试策略

实现按 TDD 进行，至少覆盖：

1. 未声明上下文时，现有三文件快照和版本身份不变。
2. Intake 只接受支持的上下文版本并生成正确配置。
3. 日历缺列、增列、重复日期、乱序、空键和 cutoff 未覆盖均拒绝。
4. 派生快照 ID 随日历内容变化，且相同输入可重复得到相同 ID。
5. Runner 对三文件/四文件按声明精确校验。
6. Sandbox 中脚本能读取平台日历，但仍不能读取 delivery 旁的
   随包日历或其它文件。
7. CompareGate 只变更三频业务文件。
8. scheduled executor 使用 DataBridge 绑定的 Native calendar
   generation，不打开新的实时 DB 路径。
9. 既有 Blackbox targeted regression 全部通过。
10. 五个新方案按固定顺序逐一通过完整技术 Gate。

## 10. 提交边界

1. 设计文档单独提交。
2. 平台 `week-calendar-v1` 能力及其测试单独提交。
3. 五个方案依次处理；每个方案完成 Intake、全套 Gate、状态文档
   更新后单独提交。
4. 任何一步失败即停留在当前方案，不提前处理下一个方案。
