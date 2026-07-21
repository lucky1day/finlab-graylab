# Blackbox V2 上游交付 SOP 自包含梳理设计

**状态**：已实施；2026-07-21 复审后已同步增量列契约与运行环境直列要求

**日期**：2026-07-21

## 1. 目标

把 `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md` 梳理成上游算法工程师唯一需要阅读的人类文档。算法工程师读完该 SOP，能够直接从统一 DataBridge 下载三份真实 CSV，使用随包提供的机器 Schema 完成校验，并在 DataBridge 暂时不可用时使用三份脱敏样例做接口烟雾测试，不需要继续查阅仓库内其他 Markdown 文档。

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

机器 Schema 和样例表头保留制作时点的字段基线，但不规定未来真实文件的总列数。SOP 必须解释“最低兼容字段基线 + 允许新增业务列”的含义，并给出可直接运行的读取和兼容性检查代码。

## 3. SOP 保留内容

以下内容直接影响算法实现，必须保留并梳理为连续流程：

1. 同名 `.py + .json` 两文件交付规则；
2. Contract 1.0 的 `predict`、`backtest` CLI；
3. Metadata 字段、简洁名称规则和可选 `description`；
4. `task_type + horizon + target_rule` 固定组合；
5. 三频 CSV 的文件名、时间键、增量列兼容规则、数据类型、空值和排序规则；
6. 七字段 Request、逐 Request 截止截断和五字段 Result；
7. 单批 1 至 100 条、完整区间由平台分批的约束；
8. predict/backtest 一致、批次切分不变、顺序不变和重复执行确定性；
9. 只读输入、单一 Output、stdout/stderr 和原子写入规则；
10. 上游可自行执行的完整验收清单。

## 4. DataBridge 背景、运行环境与更新说明

SOP 新增独立的“统一 DataBridge 数据来源与每日更新”章节，并区分开发阶段与平台运行阶段：

- 所有算法工程师使用同一套统一 DataBridge 导出工具和同一份 `data-bridge-v1` Schema，不能自行写 SQL、拼接其他数据源或手工修改三频 CSV。
- 开发和自验时，工程师按 SOP 中的 DataBridge 下载命令准备本地三频文件；算法脚本本身仍只读取 `--data-dir`，不得在 `predict` 或 `backtest` 内调用导出工具、网络或数据库。
- 平台运行时由平台每天生成三频文件，并把同一 generation 的只读快照放入本次运行的 `--data-dir`；算法工程师不负责启动生产刷新。

上游只需要知道数据如何形成：平台使用统一 DataBridge 导出逻辑全量构建三频文件，日频按日期分段后合并，周频和月频导出完整周期数据，完成平台校验后整体原子发布。SOP 不公开生产刷新时刻，也不展开内部完整检查清单。

SOP 直接列出 Python、关键包版本和资源限制，不再要求上游另行取得“冻结环境清单”。Runtime Profile、Conda 环境名、操作系统平台、环境指纹和平台环境自检命令属于内部实现信息，不进入上游 SOP。DataBridge 下载接口可以公开参数格式和基于环境变量的调用方法，但不得硬编码密码；平台运行路径、进程、凭证文件和调度实现不向上游公开。

## 5. 从 DataBridge 下载三份测试数据

SOP 必须在 Schema 说明之前提供可直接复制执行的下载步骤，不能只写文件名或只给脱敏样例。三份文件是 CSV，不是 `.xlsx`；可以用 Excel 打开，但算法必须按 CSV 读取。

下载前由 DataBridge 管理方提供地址、用户名和密码，上游在本机设置：

```bash
export DATABRIDGE_API_BASE_URL="<DataBridge /api/ 地址>"
export DATABRIDGE_API_USERNAME="<用户名>"
read -s DATABRIDGE_API_PASSWORD
export DATABRIDGE_API_PASSWORD
export DATABRIDGE_END_DATE="<需要验证的数据截止日，YYYY-MM-DD>"
mkdir -p sample_data
```

SOP 给出三个独立的 HTTPS/HTTP GET 示例，并使用 Basic Auth、`export/csv/` 与 URL 编码后的 `frequency` 参数：

```bash
curl --fail-with-body --location --retry 3 \
  --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" \
  --data-urlencode "frequency=日" \
  --data-urlencode "start_date=2010-01-01" \
  --data-urlencode "end_date=$DATABRIDGE_END_DATE" \
  --output sample_data/daily_output.csv

curl --fail-with-body --location --retry 3 \
  --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" \
  --data-urlencode "frequency=周" \
  --output sample_data/weekly_output.csv

curl --fail-with-body --location --retry 3 \
  --user "$DATABRIDGE_API_USERNAME:$DATABRIDGE_API_PASSWORD" \
  --get "${DATABRIDGE_API_BASE_URL%/}/export/csv/" \
  --data-urlencode "frequency=月" \
  --output sample_data/monthly_output.csv
```

日频显式指定开始和结束日期；周频和月频不传日历日期参数，直接下载统一导出的完整周期数据。下载失败、返回非 2xx、文件为空或内容不是 CSV 时必须停止，不得用旧文件或手工文件冒充本次下载。

SOP 随后提供一段基于 `data_bridge_v1_schema.json` 的 Python 校验命令，至少核对：三份文件全部存在且非空、没有重复字段、时间键位于第一列且非空唯一、基线字段全部存在且相对顺序稳定。检查不比较总列数；算法还必须按字段名检查自己实际消费的列，并忽略未使用的新增列。

这组下载命令只能用于开发和自验。正式交付的 `{scheme_id}.py` 不得包含 DataBridge URL、账号、密码或下载逻辑，也不得在算法运行期间访问网络。

## 6. Schema 与样例说明

SOP 内直接给出：

| 文件 | 第一列时间键 | 时间键含义 |
|---|---|---|
| `daily_output.csv` | `date` | 规范日期，按交易日升序 |
| `weekly_output.csv` | `week_id` | 六位字符串周期键，不按 ISO 周自行换算 |
| `monthly_output.csv` | `month_id` | 六位字符串周期键，不自行换算为日期 |

SOP 同时说明：

- `data_bridge_v1_schema.json` 是最低兼容字段基线；新增业务列不要求升级 Schema 版本；
- Schema SHA-256 固定记录在 SOP 和 `manifest.json`；
- 三份 sample CSV 保留制作时点的基线表头，只含两行合成数据，不代表未来永久完整表头；
- 从 DataBridge 下载的真实三频文件是算法开发、训练和回测验证入口；sample 只在 DataBridge 暂时不可用时用于读表、选列、截止截断和接口烟雾测试，不能用于训练、效果回测或推断生产分布；
- 提供 `pandas.read_csv` 字符串键读取示例以及逐 Request 截止截断示例；
- 上游代码引用业务字段时必须按字段名显式选择，不能依赖列位置猜测含义；未使用的新增列必须忽略。

## 7. 删除内容

从上游 SOP 中删除或不新增以下内容：

- 要求阅读其他 Markdown 文档的链接和导航；
- Registry、composite scheme ID、active/shadow/paused 生命周期；
- Harness 授权 token、Gate 签发和生产写库流程；
- scheduler 重启实现、Native V1 隔离细节；
- API、前端格子、指标展示、actual join 和数据库表；
- 平台运行路径、明文密钥和生产运维命令；DataBridge 下载接口格式和环境变量配置不属于删除范围；
- 只对平台审计人员有意义的 generation 文件路径、PID 和内部状态文件格式。

Request、截止键、Snapshot 同代性和禁止网络/数据库访问等算法运行边界仍须保留；生产刷新失败后的调度决策不属于上游 SOP。

## 8. 文档结构

重写后的 SOP 按算法工程师实际工作顺序组织：

1. 只需准备什么；
2. 平台 Python 环境、关键包与资源限制；
3. 统一 DataBridge 是什么、数据如何更新；
4. 如何从 DataBridge 下载日、周、月三份真实 CSV；
5. 如何按最低基线校验下载结果，脱敏样例何时使用；
6. 选择任务组合并填写 Metadata；
7. 实现 predict/backtest；
8. 读取 Request、逐行截断并生成 Result；
9. 日志、失败和确定性约束；
10. 自验与最终两文件交付清单。

每节以算法工程师需要采取的动作开头，平台背景只保留解释约束所需的最少内容。

## 9. 验收

文档测试需要锁定：

- SOP 明确声明其为唯一必读人类文档；
- 不包含生产刷新时间表或平台完整检查清单，只说明统一导出与原子发布；
- 直接包含 Python 和关键包版本以及资源限制；不包含 Runtime Profile、Conda 环境名、操作系统平台、环境指纹或平台自检命令；
- 包含统一 DataBridge 导出工具、同一 Schema、同 generation 和原子发布语义；
- 包含 DataBridge 地址和认证环境变量、`export/csv/`、`frequency=日/周/月`、三个固定输出文件名及下载后校验命令；
- 明确真实下载数据用于算法测试验证，脱敏 sample 只用于离线接口烟雾测试；
- 包含三频文件名、时间键、增量列兼容规则和样例用途，不规定总列数；
- 包含 Schema SHA-256；
- 不再链接其他 Markdown 文档；
- 不包含 Registry、前端、激活 token 等平台入库专属说明；
- 原有 Contract 1.0、Metadata、Request/Result、分批回测和确定性测试继续通过。

SOP 正文完成后由用户先审阅；ZIP 打包、manifest 更新和交付包摘要属于下一阶段。
