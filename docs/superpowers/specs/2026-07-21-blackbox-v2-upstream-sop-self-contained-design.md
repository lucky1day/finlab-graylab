# Blackbox V2 上游交付 SOP 自包含梳理设计

**状态**：已确认，待实施

**日期**：2026-07-21

## 1. 目标

把 `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md` 梳理成上游算法工程师唯一需要阅读的人类文档。算法工程师读完该 SOP，并使用随包提供的机器 Schema 与三份脱敏 CSV 样例，即可完成方案开发、自验和两文件交付，不需要继续查阅仓库内其他 Markdown 文档。

本轮先完成 SOP 正文和文档测试；SOP 经用户审阅确认后，再制作可复现 ZIP 交付包。

## 2. 阅读边界

上游交付材料最终由一份人类文档和四类机器资产构成：

```text
BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md
data_bridge_v1_schema.json
manifest.json
samples/daily_output.sample.csv
samples/weekly_output.sample.csv
samples/monthly_output.sample.csv
```

其中 SOP 是唯一必读文档；JSON 和 CSV 是程序读取、表头核对和接口自验资产，不属于额外阅读材料。SOP 不再要求算法工程师阅读 DataBridge README、平台入库 SOP、架构文档或生产准备文档。

完整的 774、575、123 列表分别保留在机器 Schema 和样例表头中，不在 Markdown 正文重复约 1472 个字段名。SOP 必须解释如何使用这些资产，并给出可直接运行的读取代码。

## 3. SOP 保留内容

以下内容直接影响算法实现，必须保留并梳理为连续流程：

1. 同名 `.py + .json` 两文件交付规则；
2. Contract 1.0 的 `predict`、`backtest` CLI；
3. Metadata 字段、简洁名称规则和可选 `description`；
4. `task_type + horizon + target_rule` 固定组合；
5. 三频 CSV 的文件名、时间键、列数、数据类型、空值和排序规则；
6. 七字段 Request、逐 Request 截止截断和五字段 Result；
7. 单批 1 至 100 条、完整区间由平台分批的约束；
8. predict/backtest 一致、批次切分不变、顺序不变和重复执行确定性；
9. 只读输入、单一 Output、stdout/stderr 和原子写入规则；
10. 上游可自行执行的完整验收清单。

## 4. DataBridge 背景与每日更新说明

SOP 新增独立的“统一 DataBridge 数据来源与每日更新”章节，并区分开发阶段与平台运行阶段：

- 所有算法工程师使用同一套统一 DataBridge 导出工具和同一份 `data-bridge-v1` Schema，不能自行写 SQL、拼接其他数据源或手工修改三频 CSV。
- 开发和自验时，工程师使用统一导出工具准备本地三频文件；算法脚本本身仍只读取 `--data-dir`，不得在 `predict` 或 `backtest` 内调用导出工具、网络或数据库。
- 平台运行时由平台每天生成并校验三频文件，然后把同一代只读快照放入本次运行的 `--data-dir`；算法工程师不负责启动生产刷新。

SOP 公开以下与数据新鲜度直接相关的时间表：

| 时间（Asia/Shanghai） | 动作 | 结果 |
|---|---|---|
| 06:00 | 第一次全量导出 | 构建日、周、月三频候选数据 |
| 06:30 | 第一次完整检查 | 通过则等待最终校验；未通过则进入重试 |
| 06:35 | 条件全量重导 | 仅在 06:30 未通过时执行 |
| 07:00 | 最终完整校验 | 通过才允许当天 Blackbox V2 使用；失败则当天不运行、不自动补跑 |

每日完整校验需要在 SOP 中用算法工程师可理解的语言说明：

1. 文件集合恰好为三份 CSV，Schema 版本、字段名、字段顺序和列数一致；
2. 日频最大 `date` 至少覆盖当天上一交易日；
3. 三种时间键非空、唯一、升序，既有历史键不能消失；
4. 除时间键外只允许有限数值或空值；
5. 全量导出连续两轮业务摘要一致；
6. 三份文件作为同一个 generation 整体、原子发布，不允许混用不同批次；
7. 刷新失败时保留上一份完整成功数据，但旧数据不会被标记成当天新数据，也不会用于当天 Blackbox V2 正式调度。

日频从 `2010-01-01` 起按分段下载后合并；周频和月频执行全量导出。该实现事实可以解释数据为何是完整历史宽表，但 SOP 不公开 API 地址、账号、密码、运行目录、进程 PID、launchd label 或平台凭证文件。

## 5. Schema 与样例说明

SOP 内直接给出：

| 文件 | 时间键 | 固定列数 | 时间键含义 |
|---|---|---:|---|
| `daily_output.csv` | `date` | 774 | 规范日期，按交易日升序 |
| `weekly_output.csv` | `week_id` | 575 | 六位字符串周期键，不按 ISO 周自行换算 |
| `monthly_output.csv` | `month_id` | 123 | 六位字符串周期键，不自行换算为日期 |

SOP 同时说明：

- `data_bridge_v1_schema.json` 是字段名和顺序的机器权威；
- Schema SHA-256 固定记录在 SOP 和 `manifest.json`；
- 三份 sample CSV 保留完整表头，只含两行合成数据；
- 样例可用于读表、选列、截止截断和烟雾测试，不能用于训练、回测或推断生产分布；
- 提供 `pandas.read_csv` 字符串键读取示例以及逐 Request 截止截断示例；
- 上游代码引用业务字段时必须使用 Schema 中的原始列名，不能依赖列位置猜测含义。

## 6. 删除内容

从上游 SOP 中删除或不新增以下内容：

- 要求阅读其他 Markdown 文档的链接和导航；
- Registry、composite scheme ID、active/shadow/paused 生命周期；
- Harness 授权 token、Gate 签发和生产写库流程；
- scheduler 重启实现、Native V1 隔离细节；
- API、前端格子、指标展示、actual join 和数据库表；
- 平台路径、服务账号、密钥、网络地址和运维命令；
- 只对平台审计人员有意义的 generation 文件路径、PID 和内部状态文件格式。

但不能删除算法必须遵守的“当天最终校验失败则 V2 不运行”这一结果语义，也不能删除 Request、截止键、Snapshot 同代性和禁止网络/数据库访问等运行边界。

## 7. 文档结构

重写后的 SOP 按算法工程师实际工作顺序组织：

1. 只需准备什么；
2. 统一 DataBridge 是什么、每天如何更新；
3. 三频 Schema 和样例如何使用；
4. 选择任务组合并填写 Metadata；
5. 实现 predict/backtest；
6. 读取 Request、逐行截断并生成 Result；
7. 日志、失败和确定性约束；
8. 完整自验清单；
9. 最终两文件交付清单。

每节以算法工程师需要采取的动作开头，平台背景只保留解释约束所需的最少内容。

## 8. 验收

文档测试需要锁定：

- SOP 明确声明其为唯一必读人类文档；
- 包含四个刷新时间点和各自行为；
- 包含统一 DataBridge 导出工具、同一 Schema、同 generation 和原子发布语义；
- 包含三频文件名、时间键、列数和样例用途；
- 包含 Schema SHA-256；
- 不再链接其他 Markdown 文档；
- 不包含 Registry、前端、激活 token 等平台入库专属说明；
- 原有 Contract 1.0、Metadata、Request/Result、分批回测和确定性测试继续通过。

SOP 正文完成后由用户先审阅；ZIP 打包、manifest 更新和交付包摘要属于下一阶段。
