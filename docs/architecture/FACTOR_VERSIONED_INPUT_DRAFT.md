# 因子版本目录与存量方案输入保护设计

**文档状态**：`LOCAL_CANDIDATE`

**最后更新日期**：2026-08-29

**当前阶段**：本地候选代码、永久测试、全量测试、独立审查和 V4 上游交付包候选均已完成。Mac3 与 ECS
源表已增加 nullable 字段并将全部存量成员回填为 `V1.0`，旧 release dry-run 已证明原四文件零漂移。源表写入
属于独立项目，本项目不接管其 CRUD；五文件 producer 和 release 晋级尚未执行。

## 1. 目标与原则

后续因子库会继续增加新因子，也会出现只使用部分因子或跨版本因子的算法。当前方案需要同时满足：

1. 每个因子有清晰且唯一的版本归属；
2. 存量方案继续只使用当前 V1.0 因子，不因未来增加 V2.0 列而产生预测漂移；
3. 新算法自行决定使用哪些版本和因子，平台不保存方案级因子清单；
4. 不要求每个算法维护上千个字段名；
5. 不修改存量算法字节、exact version、预测、回测或 Registry；
6. 不为该能力新增服务、数据库业务表、Harness Gate、报告目录或审计字段；
7. 版本筛选不得退化为每个方案重复解析和重写三份大 CSV。

## 2. 已确定的设计决策

### 2.1 因子版本是所属批次

`factor_version` 表示因子属于哪一批因子体系，不表示算法版本、数据刷新批次或 DataBridge generation。

```text
factor_version       因子所属批次，例如 V1.0、V2.0
indicators_code      因子目录中的唯一身份
DataBridge generation 某次具体输入数据发布
scheme_version       某个算法方案的精确版本
```

每个因子只归属一个版本。当前因子统一归属 `V1.0`；未来新因子必须显式登记版本。一个因子一旦首次登记
`factor_version`，其版本归属永久不可修改；因子需要进入其他版本时必须使用新的 `indicators_code`。算法如需组合
V1.0 与 V2.0，由算法自行选择两个版本。

一个版本首次进入成功发布的 DataBridge 正式 generation 时，其实际可用成员集合立即封版。封版集合以该次
`factor_catalog.csv` 中属于该版本的精确 `indicators_code` 集合为准。后续 generation 不得向该版本增加或删除
成员；新增因子必须登记到新的版本，例如 `V2.1` 或 `V3.0`。当前 `V1.0` 在首个
五文件 generation 发布时按三份宽表实际输出列封版；已经存在于 Metadata、但当时未进入宽表和 catalog 的 V1.0
代码，以后也不得再加入已封版的 V1.0 输出，若要正式提供必须使用新的 code 和新版本。

### 2.2 DataBridge V1 直接升级为五文件

继续使用 `data-bridge-v1` 名称，不建立 V1/V2 两套并行合同。标准 generation 从精确四文件升级为精确五文件：

```text
daily_output.csv
weekly_output.csv
monthly_output.csv
api_wind_date.csv
factor_catalog.csv
```

`factor_catalog.csv` 是因子目录，不是第五张时间序列。它只负责把宽表列名关联到频率和因子版本。

### 2.3 存量方案与未来方案采用不同责任边界

- 升级前已经存在的方案由共享运行时统一固定为升级时三张 CSV 实际输出的精确 V1.0 列集合及相对顺序；不逐个
  修改算法，也不把 Metadata 表中未进入宽表的记录纳入存量基线。
- 后续通过新版 Intake 新增的 Blackbox 方案读取完整五文件，由算法内部自行管理版本和因子。
- 平台只为存量兼容提供共享 V1 视图，不为新算法记录或裁剪方案级因子集合。

目前数据库存在 96 个 composite Registry 行，仓库执行身份数量少于 Registry 行数。实现时必须按 canonical
`base_scheme_id` 和运行类型发现存量集合，不得把 96 当作算法执行数量或维护一份手工 ID 清单。

### 2.4 生产启用必须从源表字段开始

生产启用时，第一阶段只在获批的因子 Metadata 源表增加并回填 `factor_version`，不同时切换
DataBridge 文件合同、snapshot、Blackbox runtime、Native 输入或方案配置。本地候选代码可以提前完成和验证，
但在源表阶段闭环前不得安装或运行于生产环境。

第一阶段验收通过前，不得在生产启用五文件和共享 V1 视图。这样可以先独立证明：

- 增加一个未被旧逻辑消费的 Metadata 字段不会改变原四份 CSV；
- 现有 producer、Native 输入和 Blackbox 运行仍保持原结果；
- 源数据新增因子的写入方已经能够提供合法版本；
- 字段本身的约束和回填没有缺口。

第一阶段还必须冻结当时三份宽表的精确列名和相对顺序，作为存量方案的全局 V1 基线。存量保护不能在以后每次
运行时动态选择“当前所有 `factor_version=V1.0`”，必须始终读取首次冻结的精确 V1 输入集合。

## 3. 数据库因子目录

### 3.1 字段合同

由源数据所有者在只读源表 `api_wind_indicators_all` 增加：

```sql
factor_version VARCHAR(16) NULL DEFAULT NULL
```

固定规则：

- 无业务默认值；任何准备进入 DataBridge 的因子必须先显式登记版本；
- 格式严格为大小写敏感的 `V<major>.<minor>`，例如 `V1.0`、`V2.0`；
- DataBridge 消费时禁止空值、首尾空白、控制字符和非规范格式；
- 当前全部存量行一次性回填为 `V1.0`；
- 任一因子首次登记 `factor_version` 后永久不可修改，不以是否已经进入 DataBridge catalog 为前提；
- 本方案不管理因子计算公式、数据源口径或 frequency 的修改，也不为这些变化增加连续性控制或测试；它们由因子
  源系统自行负责，本方案只控制 `indicators_code -> factor_version` 归属和版本成员集合；
- 本阶段假定因子启用状态保持不变，不设计停用、重新启用、依赖分析或版本迁移流程。若实际输出成员意外变化，
  只由既有 catalog 成员集合一致性检查统一拒绝，不增加专用控制。

该表属于平台只读源数据边界。本仓库不新增应用 Migration 022，也不管理另一个项目的 CRUD。数据库列允许
nullable 是两个项目之间的明确解耦：本项目在构建 generation 前要求所消费的 Metadata 零空值且格式合法，
否则 fail-closed；不通过数据库默认值、trigger 或外部项目改造替本项目补版本。

### 3.2 首次回填顺序

首次初始化按以下顺序操作：

1. 增加 nullable、无默认值的 `factor_version`；
2. 将冻结时全部现有行回填为 `V1.0`，且不得顺带修改 `create_time`、`update_time` 或其他字段；
3. 校验零空值、零非法值、零重复 `indicators_code`；`indicators_code` 必须全表唯一，不能只在 frequency 内唯一；
4. 后续新因子在进入 DataBridge 前由数据维护方设置版本；本项目不关心其使用哪个录入工具。

平台代码不得包含生产 UUID、DSN、凭据或一次性 owner 映射。

### 3.3 第一阶段独立验收

字段增加和回填完成后，只使用旧 release 做只读或零写入验证：

1. `factor_version` 字段存在，全部存量行精确为 `V1.0`；
2. 当前存量成员零空值，候选 producer 对缺失或非法版本在生成 staging 前 fail-closed；
3. `indicators_code`、frequency、status 和其他 Metadata 字段未被修改；
4. `create_time`、`update_time` 的集合及最大值不变，避免无业务变化的回填污染现有 source evidence；
5. 旧 producer 在相同数据库快照和 cutoff 下构建的四份 CSV 与字段增加前逐字节一致；
6. 冻结 daily、weekly、monthly 的精确输出列和相对顺序，形成唯一的 legacy V1 基线；
7. Native 固定输入和代表性 Blackbox Request 结果一致；
8. prediction、run、backtest、Actual 和 Registry 表零写入；
9. 不发布五文件 generation，不切换 release，不改变 timer 或服务。

只有以上验收全部通过，才把第二阶段“五文件 producer 与共享 V1 视图实现”从草案推进为候选代码任务。

## 4. `factor_catalog.csv` 合同

### 4.1 文件结构

文件固定三列：

```csv
indicators_code,frequency,factor_version
M0000001,daily,V1.0
M0000002,weekly,V1.0
M0000003,daily,V2.0
```

固定行为：

- 只描述本 generation 三份宽表实际提供的因子列；
- 不包含 `date`、`week_id`、`month_id`；
- `frequency` 只允许 `daily`、`weekly`、`monthly`；
- `indicators_code` 必须全局唯一；
- 行顺序固定为 daily、weekly、monthly；
- 每个频率内部保持对应宽表列的相对顺序，不按字母重新排序；
- 目录中每个版本值必须来自同一次一致性快照中的 `api_wind_indicators_all.factor_version`；
- 每个频率的目录代码集合必须与对应宽表非主键列精确一致。

任一版本缺失、格式非法、代码重复、频率冲突或目录与宽表不一致，producer 必须在 staging 发布前失败。

从第二个五文件 generation 开始，candidate 还必须与当前已发布 catalog 比较：同一 `indicators_code` 的
`factor_version` 不得变化；current 中已经存在的每个版本，其完整 `indicators_code` 成员集合必须与 candidate
精确相同。candidate 只允许增加此前从未发布的新版本及其首次成员集合，不允许向
已有版本追加成员。该连续性校验直接复用 current catalog，不增加审计表或历史映射。

### 4.2 Generation 身份

第五份文件与原四份文件共同进入：

- DataBridge business digest；
- publication manifest；
- Blackbox snapshot identity；
- producer-ready receipt；
- runtime view 的只读和进程前后篡改检查。

`factor_catalog.csv` 只参与输入身份，不参与日期 cutoff、日历、灰度目标规划、Actual join 或 target 计算，也不向
prediction `extra`、run 或数据库增加新的审计字段。

## 5. 存量方案保护

### 5.1 现有 Blackbox

现有 canonical config 没有因子输入模式字段。发现时规范化为内部模式：

```text
factor_input_mode = legacy_v1
```

处理规则：

- 不修改现有 `.py`、Metadata 或 `config.yaml`；
- 不重新 Intake、回测或 activate；
- 不改变 code/config/manifest hash 和 exact version；
- 不改变 active、paused、archived Registry；
- scheduled live、gap-fill 和 backtest 均读取共享 V1 兼容 snapshot；
- 完整 generation 增加 V2.0 后，旧算法仍只能看到升级前四文件中的冻结宽表列；
- 现有 paused/archived 方案未来再次执行时仍使用 V1，除非上游交付新的 algorithm-managed exact version。

不使用方案 ID allowlist、owner、部署日期或目录时间推断模式。历史 config 缺少字段是唯一受控兼容入口；所有
新版 Intake 自动写入新模式，防止新方案误入 legacy。

### 5.2 现有 Native

Native 不读取 Blackbox runtime view。共享 Native 输入构建入口统一只选择首次升级冻结的 legacy V1 列集合，
不能在每次运行时重新解释全部 `factor_version=V1.0`：

```text
legacy_v1_baseline
```

处理规则：

- 不修改 Native core、adapter、source runner 或 config；
- 不增加每方案因子列表；
- daily、weekly 输入只查询冻结基线；monthly 保留既有 Native 边界，不纳入仅由 DataBridge
  `include_databridge_additions` 提供的三个宏观附加列；
- 新增 V2 Metadata 不得使 Native Phase-A cache 误判为输入变化；
- Native cache 身份只由实际使用的 V1 输入决定；
- 不允许无关 V2 Metadata 触发 full/suffix rebuild。

项目已禁止新增 Native，因此不为未来 Native 扩展第二种模式。

## 6. 每 Generation 一个共享 V1 视图

### 6.1 构建方式

在现有 immutable Blackbox snapshot 流程内增加一个最小的版本视图步骤：

```text
完整五文件 generation
        │
        ├── algorithm_managed：使用完整 snapshot
        │
        └── legacy_v1：使用共享 V1 snapshot
```

每个 generation 最多构建一次 V1 snapshot，所有存量 Blackbox 共用。不得在每个方案执行前重复读取、筛选、
哈希或重写三份大 CSV。

### 6.2 当前全部 V1 时零复制

首个五文件 generation 中，全部可用因子仍为 V1.0。此时：

- 完整 snapshot 与 V1 snapshot 的业务内容完全相同；
- 两个逻辑模式直接指向同一个 snapshot identity；
- 不复制三份宽表，不增加一份重复存储。

只有真实 V2 因子进入完整宽表后才物化一次 V1 派生 snapshot。

### 6.3 V2 出现后的 V1 视图

V1 snapshot 固定按首次升级冻结的 legacy V1 基线构造，而不是按当前 Metadata 动态收集所有 V1.0。它只包含
升级前已有的四文件，不向历史脚本暴露新增目录文件：

```text
daily_output.csv   = date + frozen daily/V1.0 列
weekly_output.csv  = week_id + frozen weekly/V1.0 列
monthly_output.csv = month_id + frozen monthly/V1.0 列
api_wind_date.csv  = 原样
```

派生 snapshot 复用现有 generation snapshot 根、锁、staging、原子发布、只读权限和清理生命周期，不建立第二套
cache 服务或方案级缓存。Ready identity 内部记录完整 snapshot 与 legacy V1 snapshot；当前全部 V1 时二者相同。

## 7. 新算法责任

新版 Intake 生成：

```yaml
factor_input_mode: algorithm_managed
```

该字段只表示算法读取完整输入，不声明具体版本或因子。新算法在自己的 `.py` 内：

1. 读取一次 `factor_catalog.csv`；
2. 选择一个或多个版本；
3. 根据频率取得因子代码；
4. 检查所选列全部存在；
5. 在算法进程内构建内存 DataFrame；
6. 自行决定使用全量、部分或跨版本因子。

参考行为：

```python
catalog = pd.read_csv(data_dir / "factor_catalog.csv")
columns = catalog.loc[
    (catalog["frequency"] == "daily")
    & catalog["factor_version"].isin(["V1.0", "V2.0"]),
    "indicators_code",
].tolist()

missing = [column for column in columns if column not in daily.columns]
if missing:
    raise ValueError(f"missing factor columns: {missing[:10]}")

features = daily.loc[:, columns]
```

平台不保存算法选择的版本、不生成方案级字段清单，也不静态推断算法实际使用了哪些因子。

## 8. Producer 与过渡设计

### 8.1 Producer 一次读取 Metadata

每个 DataBridge round 只读取一次 `api_wind_indicators_all`。同一 Metadata DataFrame 同时用于：

- daily 输出选择；
- weekly 输出选择；
- monthly 输出选择；
- `factor_catalog.csv`。

不能因为新增 catalog 再执行第四次 Metadata 查询。相同数据库快照和 cutoff 下，升级前后的四份原业务 CSV 必须
逐字节一致。

### 8.2 四到五文件升级

现有合法四文件 current 仅允许 producer 作为一次升级 continuity baseline：

- 过渡 release 可在五文件尚未发布时继续读取既有合法四文件 current，但只允许存量 `legacy_v1` 方案使用；
- `algorithm_managed` 方案必须等待五文件 ready generation，四文件下直接 fail-closed；
- producer 成功时原子发布五文件 generation；
- producer 失败时原四文件 current 保持不变；
- 已完成历史使命的三文件升级逻辑在实现时删除；
- 最小四文件升级读取保留到 ECS、Mac3 都越过回滚窗口后再单独删除；五文件一旦发布，正常 scheduler、Harness
  和算法不得回退消费四文件。

不得建立公共兼容 CLI，也不得让四文件与五文件长期混合运行。

## 9. 测试与验收计划

### 9.1 数据目录测试

- `factor_version` 缺失、null、空白或格式非法时 producer 在写 staging 前失败；
- catalog 三列、排序、唯一性和频率规范正确；
- catalog 与三份宽表逐频率精确一致；
- catalog 拒绝已有 code 的 factor_version 漂移；
- catalog 拒绝向已发布版本增加或删除成员；
- 全新版本可以在首次发布时登记其完整成员集合，并从下一代 generation 起保持封版；
- Metadata 每 round 只查询一次；
- 四份原业务 CSV 在同一输入下逐字节不变；
- manifest、digest、snapshot 和 ready receipt 精确覆盖五份文件；
- catalog 删除、替换、篡改或重排后消费者 fail-closed；
- cutoff、日历和 target 规划不读取 catalog。

### 9.2 存量方案结果测试

构造包含原 V1 因子和合成 V2 因子的固定 generation：

- 所有现有 Blackbox 自动使用共享 V1 snapshot；
- 完整 snapshot 能看到 V2，legacy snapshot 看不到 V2；
- 全部 active Blackbox 至少运行一个代表性 Request；
- 与升级前基线逐字段一致；
- Native 输入不包含 V2；
- 首次冻结时不在宽表中的 V1 Metadata 行不得在以后加入已封版的 V1 catalog；
- Native Phase-A cache 继续 hit；
- 不发生 V2 引起的 Native 训练；
- 一个 algorithm-managed 合同样例可自行选择 V1、V2 或 V1+V2。

需要比较的结果包括方向、日期、tenor、horizon、confidence 和算法必要输出字段。

### 9.3 性能测试

- 当前全部 V1 时 V1 视图零复制；
- V2 出现后 V1 视图每 generation 只构建一次；
- 不出现“方案数 × 三份 CSV”的重复过滤；
- producer 总耗时相对旧版本增幅不超过 10%；
- 现有 Blackbox 单次运行不得明显变慢；
- Native 不因 V2 Metadata 增加输入构建或 Phase-A 训练。

### 9.4 审查门槛

实现完成后必须运行相关测试、全量 `pytest` 和独立代码审查。Reviewer 重点检查：

- 是否修改了存量方案字节或 exact version；
- 是否真正只构建一次 V1 视图；
- mode 是否只来自 canonical config；
- catalog 是否误入日期和 target 逻辑；
- V2 Metadata 是否错误触发 Native cache；
- 是否新增了方案级映射、重复缓存或审计字段。

Critical 和 Important 问题全部修复并重新全量验证后才允许提交发布。

## 10. 文档、交付包与未来发布边界

### 10.1 上游交付包

本地候选验证完成后生成新的、不可覆盖旧包的：

```text
blackbox-v2-upstream-delivery-kit-v4-<date>-<commit>.zip
```

上游包只描述五文件、catalog、算法内版本选择和缺列 fail-closed，不暴露 legacy V1 视图、数据库 DDL、ECS、
Mac3、Registry、release 或 Harness 内部机制。

### 10.2 发布范围

当前方案若后续获批，固定分为四个阶段：

1. 源表阶段：只增加并回填 `factor_version`，使用旧 release 验证四文件和结果零漂移；
2. 代码阶段：第一阶段通过后，才实现五文件、共享 V1 视图、测试、文档和 V4 上游包；
3. ECS 兼容阶段：代码审查通过后构建第一份兼容 release，只在既有四文件 current 上验证存量方案，不发布五文件；
4. ECS 开放阶段：下一次有真实内容的正常 release 晋级、使 current 和 previous 都具备兼容能力后，才单独授权
   producer 发布首个五文件 generation 并验证运行结果。

任一阶段失败即停止，不把后续阶段作为修复手段。本轮计划不修改 Mac3。

未来 ECS 执行仍需对以下操作分别取得明确授权：

- 源表 DDL 和 V1.0 回填；
- DataBridge one-shot producer；
- release 安装和 current 切换；
- writer、timer 或服务操作。

ECS 验证通过后，Mac3 只能使用同一份已验证 archive，并作为独立晋级任务执行。

首个真实 V2 因子进入任一环境前，该环境的 `current` 和实际可回滚 release 都必须具备 legacy V1 保护能力。
第一次部署五文件 release 后，不能在 `previous` 仍为四文件旧逻辑时开放 V2；否则代码回滚会使旧算法重新看到
V2 列。不得为制造 previous 指针而发布空变更，必须等待下一次正常、已验证的 release 晋级后再开放 V2。

## 11. 明确不做

- 不修改任何存量算法、Metadata、config 或 exact version；
- 不给每个方案保存上千个因子字段；
- 不在 Registry 增加因子版本；
- 不让平台替新算法选择因子；
- 不设计因子停用、重新启用或依赖迁移机制；
- 不增加应用数据库表、Migration 022、服务、timer 或 Harness Gate；
- 不增加 prediction/run 审计字段；
- 不为每个方案生成版本快照；
- 本地候选阶段不改生产数据库、不构建 release，也不操作 ECS/Mac3。

## 12. 本次复核新增的关键结论

1. **V1 基线只认实际算法输入**：存量方案必须使用首次升级时 daily、weekly、monthly 三张 CSV 实际输出的精确
   因子列及相对顺序，不能使用“Metadata 全表”或“当前所有 V1.0”。
2. **字段回填不能污染时间证据**：当前源表 `update_time` 没有自动更新约束，回填 SQL 仍必须只更新新字段，并以
   前后只读摘要证明其他列和时间戳不变。
3. **当前机器 Schema 尚不是完整 V1 冻结集**：只读对照显示 daily 基线与当前输出一致，但 weekly 还有 2 个、
   monthly 还有 3 个已批准输出列未进入机器基线。第二阶段必须先把首次冻结的真实表头完整写入 Schema，不能直接
   把当前旧 Schema 当作 legacy V1 列表。
4. **单因子版本归属永久不可变**：这是源数据事实；producer 通过 catalog continuity 拒绝已发布 code 的
   factor_version 变化。计算公式、数据源口径和 frequency 的修改不属于本方案治理范围。
5. **版本首次正式发布即封版**：封版成员是首次正式 generation 的 catalog 实际可用成员集合。后续不得向相同
   版本增加、删除或移动 code；新因子必须进入 V2.1、V3.0 等新版本。Metadata 中未进入首次 catalog 的 V1.0 code
   也不能在以后加入已封版 V1.0。
6. **回滚 release 也必须理解 V1 保护**：只有 current 具备 legacy 视图不够；开放 V2 前，previous 也必须具备
   相同保护，否则回滚会改变存量方案输入。
7. **源表写入项目不属于本方案**：本仓库只消费 `api_wind_indicators_all`，不需要其他项目源码，也不改造其
   CRUD。未设置合法版本的因子不能进入 DataBridge generation；由数据维护方先补齐后再刷新即可。

## 13. 已收敛的最小工程方案

### 13.1 不增加第二份 V1 列映射

直接复用现有 `shared/blackbox_v2/data_bridge_v1_schema.json`：

- daily、weekly、monthly 的既有 `columns` 就是 frozen legacy V1 列集合和相对顺序；
- 首次实施时先把现场实际输出但 Schema 尚缺少的 2 个 weekly 和 3 个 monthly 列补齐；
- `factor_catalog.csv` 在同一 Schema 的 `files` 中只声明固定三列表头；
- 完整 DataBridge 宽表仍允许在 frozen baseline 之后增加新版本列；
- legacy V1 视图只按三个 `columns` 列表选择，不再建立 JSON、数据库表或方案 allowlist。

Schema 自身的 SHA-256 已经进入 generation snapshot identity。修改 Schema 后同步更新现有 release 内的固定合同
摘要，不增加第二套 Schema 版本服务。

### 13.2 只扩展现有 generation receipt

继续使用现有 generation snapshot cache、同一把 lock、同一个 ready pointer 和同一份 receipt：

- receipt 保存完整 snapshot ID 和 legacy V1 snapshot ID；legacy runtime view 仍只物化原四文件；
- 当前全部实际输出因子均为 V1 时，两个 ID 必须相同，三份宽表零复制；
- 新版本因子出现后，在同一 generation root 下额外创建一个内容寻址、只读的 legacy V1 snapshot；
- legacy snapshot 与完整 snapshot 共用 `api_wind_date.csv` 的业务内容，但仍沿用既有私有 runtime view 复制边界；
- `get_ready_blackbox_snapshot` 按已经由 canonical config 解析出的 input mode 返回对应 snapshot；
- 不增加第二个 ready 文件、生命周期目录、清理器、缓存服务或数据库字段。

现有 receipt cache version 做一次明确升级；旧 receipt 不原地猜测或修复，producer 在合法刷新时按新合同发布。

### 13.3 Native 只固定默认全量输入，不改 Phase-A 合同

只读核对确认 Phase-A `input_state` 当前由实际 daily、weekly、monthly DataFrame 生成，不包含 Metadata 全表身份。
因此：

- 不修改 Phase-A cache manifest、lineage、build decision 或 generation 格式；
- `shared.input_artifacts` 的 daily 默认 builder 使用 Schema 的 frozen 列集合；monthly 使用同一 Schema，
  但继续排除既有 DataBridge-only 三列，确保 Native 输入字节不扩张；
- weekly 未显式提供 `schema_columns` 时同样使用 frozen weekly 列集合；
- 已经显式传入更小 `schema_columns` 的存量专项方案继续使用原集合，不扩大输入；
- Native 作业级共享输入仍按实际 builder 参数复用，V2 Metadata 和列不进入 identity，也不触发 Phase-A 重训；
- DryRun receipt 继续证明实际文件，不增加 factor version 审计字段。

DataBridge producer 自己在一个一致性事务内只读取一次 Metadata，并把同一 DataFrame 传给三频 builder 和 catalog
构造；Native 各作业不读取或解析 `factor_catalog.csv`。

### 13.4 四到五文件采用两次正常 release 的回滚边界

当前 DataBridge `previous` 目录只服务一次 publish 的事务回滚，发布成功后会被删除，不能承担 release 级回滚。
因此五文件开放按以下顺序执行：

1. 发布第一份同时理解合法四文件和五文件的兼容 release，但暂不触发五文件 producer；
2. 使用既有四文件 current 验证全部存量方案，`algorithm_managed` 保持不可运行；
3. 等待下一次有真实内容、正常审查通过的 release 晋级，使实际 `current` 和 `previous` 都具备五文件与 legacy V1
   能力；不得复制相同 archive 或制造空提交来移动 previous；
4. 这时才授权 producer 原子发布首个五文件 generation；
5. 发布失败自动保留原四文件 current；发布成功后，代码回滚到 previous 仍能读取五文件并保护存量 V1；
6. ECS、Mac3 都越过各自回滚窗口后，另行删除临时四文件读取分支。

整个过程复用现有 DataBridge lock、staging、publication manifest、current 切换和失败恢复，不新增恢复模块。

### 13.5 等价验证不抽样方案

在相同冻结输入、固定 feature date 和零业务写入环境中执行：

- 三份升级前业务 CSV 与加入未消费 Metadata 字段后的旧 producer 输出逐字节一致；
- 全仓静态扫描已确认现有 Blackbox delivery 不枚举或断言输入目录只能有四个文件；运行验证仍需证明第五个 catalog
  对存量脚本零影响；
- 全部存量 Blackbox base scheme 各执行一个由现有任务日历生成的固定 Request；
- 全部存量 Native base scheme 各执行一次固定日期 DryRun；
- 对比方向、confidence、predict/feature/target date、tenor、horizon、scheme version 和算法必要 extra；
- 验证全部 Liwei Phase-A family 继续 hit，训练调用为 0；
- synthetic V2 只用于隔离测试：完整 snapshot 可见 V2，legacy snapshot 不可见，algorithm-managed 样例可选择
  V1、V2 或组合版本；
- 运行相关测试、全量 `pytest` 和独立代码审查。

如果单 Request 暴露任何输入或结果漂移，停止发布，不通过兼容分支、字段兜底或结果修正绕过。

### 13.6 首个真实新版本的开放硬门

首个真实 V2.0、V2.1 或以后版本进入目标环境前，必须同时满足：

1. 源表字段和全量 V1.0 回填已完成，候选 producer 对非法或缺失版本 fail-closed；
2. 首个五文件 generation 已封版 V1 实际成员集合，catalog 与三份宽表精确一致；
3. 目标环境 current 和实际 previous release 都理解五文件并提供 frozen legacy V1；
4. 全部存量 Blackbox 与 Native 固定运行结果零漂移；
5. Native Phase-A cache 未因新版本 Metadata 发生 full、suffix 或 append 训练；
6. 完整与 legacy snapshot 当前全 V1 时同 ID，出现新版本后每 generation 最多各一份；
7. producer Metadata 查询为一次，四份原业务 CSV 内容不变，总耗时增幅不超过 10%；
8. 五文件发布失败、代码回滚和 ready receipt 不匹配均已在隔离环境证明 fail-closed；
9. prediction、run、backtest、Actual 和 Registry 表零修改；
10. ECS 全部门槛通过后，Mac3 仍作为独立授权的同 archive 晋级任务。

## 14. 下一步授权边界

本文已经没有未决业务或工程设计选择，用户已明确批准开始本地实现。后续仍按以下授权边界逐段执行：

1. 源表增加 nullable `factor_version` 并完成存量回填（已完成）；
2. 第一阶段旧 release 四文件零漂移验收（已完成）；
3. 五文件、共享 legacy V1 和 Intake mode 本地候选代码（已完成）；
4. 本地全量验证与独立审查（已完成）；
5. 候选提交和推送（已完成）；
6. 发布 ECS 兼容 release；
7. 满足双 release 回滚前提后，另行授权首个五文件 producer；
8. ECS 新版本因子验收完成后，再单独决定 Mac3 晋级。

五文件 producer、release 切换、服务操作和 Mac3 晋级仍按项目边界逐项确认。
