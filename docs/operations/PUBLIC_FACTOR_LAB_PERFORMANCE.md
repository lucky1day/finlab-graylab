# Dashboard 合同与运行验收

**文档状态**：`CURRENT`

**适用版本**：`factor-lab-dashboard-v7`

本文维护 Dashboard HTTP 表示、浏览器刷新、故障定位及验收。账户和会话规则见
[认证合同](../architecture/AUTHENTICATION_AND_ACCOUNT_MANAGEMENT.md)，主机、Origin 和访问链路见
[部署访问入口](DEPLOYMENT_ACCESS.md)，产品日期与统计口径见[预测语义](../architecture/PREDICTION_SEMANTICS.md)。

## 唯一读路径与 HTTP 合同

`GET /api/factor-lab/dashboard` 的 Summary 和 Detail 均要求有效登录，每次通过专用只读 SQLAlchemy Engine
从当前数据库构建；不保存服务端 TTL、single-flight、LKG、Redis 或 Nginx microcache。

- 成功返回 `200` 当前数据；连接、查询或构建失败返回 `503`，正文仅为
  `{"error_code":"dashboard_data_unavailable"}`。
- active Registry 即使没有预测也出现在 Summary；只聚合已发布事实，不生成占位行或推导应运行状态。
  owner 只取 Registry，缺失/非法则整个请求失败，不回退 Metadata 或仓库映射。
- 读路径不查 run、DataBridge 或日历，不计算 `missing/not_due/no_run`；调度缺口由
  [调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)处理，不混入产品读模型。
- 认证与 Dashboard 使用彼此独立的 Backend HTTP Engine；每进程各自 `pool_size=5`、
  `max_overflow=0`、`pool_timeout=0.25s`，合计最多 10 个池连接。连接/读/写超时分别为
  0.5/2/0.5 秒，单条 MySQL 查询上限 1000ms，开启 pool_pre_ping，300 秒回收连接。这些是压测起点，
  不是最终容量结论；总连接数须乘以实际应用进程数核算。

无参数请求返回 `representation=summary`，包含方案身份、owner、回测展示元数据、完整 `month + source` 月度计数和全历史区间计数，不携带逐点明细。Summary 也接受且只接受成对的 `start-date=YYYY-MM-DD&end-date=YYYY-MM-DD`，按特征基准日闭区间统计；非法日期、逆序、缺失、重复、额外或混用 Detail 参数均返回 400。

`monthly_rows/monthly_row_fields` 始终保留完整月份计数，供所选日区间触及月份的整月详情和趋势使用。`range_rows/range_row_fields` 每方案按 `backtest/live` 返回最多两行同口径计数，供任务矩阵、方案排行和区间指标使用；纯待验证来源保留全零行，无预测来源不造行。`selected_feature_range` 回显所请求闭区间，无参数时为 null；`feature_date_bounds` 分别给出 `all/backtest/live` 全历史可见事实（含待验证）的首尾特征日期，无数据时为 null，不随所选区间缩窄。前端只对相应 Summary 计数按 source 求和，按[统计定义](../architecture/PREDICTION_SEMANTICS.md#6-指标统计口径)计算准确率、precision/recall 和方向分布，不从整月计数近似日级区间。

Detail 仅在用户打开某方案月份时按需请求，首屏不请求；浏览器不得扫描明细、使用 Detail 缓存或旧 `monthly_metrics` 重建第二套 Summary，
服务端也不得用旧 `monthly_metrics` 替代缺失产品事实或反推明细。
`source` 是按特征基准日确定的产品展示分区，不是物理表或真实执行来源；公开响应不返回 run phase。V7 以 `live_feature_start_date` 返回分界日，取代 V6 的 `live_target_start_date`；Summary 和 Detail 均使用 V7，不能混用旧月份或旧分区表示。

月历 Detail 在同一路径携带且只携带以下三个参数：

```text
scheme-id=<composite_registry_id>&month=YYYY-MM&source=all|backtest|live
```

返回 `representation=detail`；方案、按[预测语义](../architecture/PREDICTION_SEMANTICS.md)确定的 feature 自然月份和 source 条件下推数据库。未知、重复、缺少或额外参数
返回 400；月份必须是可表示前后相邻月边界的规范 `YYYY-MM`，当前允许 `0001-02` 至 `9999-11`，
因此 `0000-01`、`9999-12` 和非法月份在请求解析层返回 400，不进入 builder。未知/非 active Registry
返回 404；合法空月为 `rows=[]`；合法请求遇到损坏业务事实仍按服务端失败返回 503，不能把 builder 的普通
`ValueError` 统称为参数错误。纯待验证月份仍可打开明细，统计只计已验证样本。

Summary 与 Detail 使用独立内部查询编排。Detail 先验证 active composite Registry，再把展示月对应的
`feature_date` 半开区间与 `source` 的特征日期分区求交后下推预测 SQL；空交集不读取预测或 Actual。
Detail 不读取或选择 `t_backtest_runs` 展示元数据，也不以 run ID、物理回测表或数据库来源替代产品
`source`。先在所选预测事实中完成 canonical 选择；只有仍缺 `backtest_actual_direction` 的选中行才读取并
关联对应范围的 live Actual，不能仅因为请求 `source=backtest` 就跳过 Actual。

成功响应包含 `Cache-Control: no-store`、`Vary: Accept-Encoding`、`X-Request-ID`、
`X-Dashboard-Snapshot-ID`（等于 body snapshot_id）。canonical payload 只做一次 JSON 和 gzip 编码，按
Accept-Encoding 返回对应字节；HEAD/GET 表示与 Content-Length 语义一致，middleware 不得再次压缩。
请求进入 Backend middleware 时只生成一次 request ID 和单调时钟起点，认证依赖、连接获取、数据库访问、
构建与编码共用 4 秒累计后端预算；每次进入后续 SQL 前检查剩余预算，耗尽后不再开始下一条查询。
`Server-Timing` 诊断 `request_auth`、`request_pool_acquire`、Dashboard DB/canonical/serialization、
`request_route` 与 `backend_total`；其中 pool acquire 包含池等待以及 checkout 时发生的 pre-ping/新建连接，
不是纯队列时间。结构化日志另记录 `auth_ms`、`pool_acquire_ms`、`db_ms`、`build_ms`、`encode_ms`、
`backend_total_ms` 和失败时的 `failure_stage`，由同一 `X-Request-ID` 关联。

4 秒是小于当前入口 5 秒读取上限的后端正常失败边界，不代表 P95 已达到目标。同步 PyMySQL 已经开始的调用
不会因累计预算检查被异步取消，仍由驱动超时和 MySQL 单条执行上限收敛；因此不能把应用预算描述为数据库
取消机制，也不能仅靠放宽预算处理容量问题。浏览器对 Summary 和 Detail 均由 `fetchJson()` 自建的
`AbortController` 在 6 秒触发超时，并合并注销、身份切换或视图替换产生的外部取消；计时持续到 JSON
响应体读取完成，而不是收到响应头就结束。该保护略晚于正常入口失败边界，超时进入既有 stale/error 与
退避重试状态机；只有当前 `loadSeq` 可以提交结果或清理 loading，旧身份的晚到响应不能恢复数据或安排重试。

HTTP 失败携带合法 `Retry-After` 时，同时支持 delta-seconds 和标准 IMF-fixdate；自动重试取既有退避与该时刻的
较晚者。超过浏览器定时器可表示范围的等待不创建可能提前触发的 timer，保持 fail-closed，等待用户重新进入
或其他显式刷新契机；非法值不替代既有退避。

### Summary 历史增长基线

Summary 的回测展示元数据由数据库按 `updated_at DESC, id DESC` 为每个 base scheme 只选择一个确定性候选，
不再把全部成功 run 传给 Python。预测事实仍逐行执行原有 canonical 选择、日期、方向和 Actual 冲突校验，
但按业务键排序流式读取并直接累计紧凑月份计数；不物化全历史明细列表，不截断超过 100,000 条的合法历史。
纯 pending 月份仍保留，Actual 重复/冲突校验保持不变；V7 的月份及产品 source 分区统一使用 `feature_date`。Detail 继续使用
单方案、单月、单来源的有界查询，不复用 Summary 流。

可复现的本机合成测试入口为：

```bash
python -B scripts/benchmark_dashboard_summary.py --rows 10000 50000 100001
```

2026-09-16 在本机 Python 3.13 与 SQLite 内存库记录如下。DB、canonical、总构建和编码时间来自未开启
`tracemalloc` 的第一次构建；峰值分配来自相同数据集的第二次独立构建。该结果只用于同机实现对比，不代替
ECS MySQL 的认证 HTTP 压测或容量结论。

| 预测事实 | DB 读取 | canonical 构建 | 总构建 | 编码 | 构建期峰值分配 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 0.0161s | 0.0549s | 0.0728s | 0.0005s | 2.14 MiB |
| 50,000 | 0.0840s | 0.2730s | 0.3633s | 0.0017s | 2.84 MiB |
| 100,001 | 0.1682s | 0.5468s | 0.7278s | 0.0034s | 3.71 MiB |

为与修改前基线保持相同测量开销，再比较开启 `tracemalloc` 的完整构建：10,000 条由
0.7122 秒 / 6.26 MiB 降至 0.5973 秒 / 2.14 MiB；50,000 条由 3.8774 秒 / 32.20 MiB 降至
2.9695 秒 / 2.84 MiB；旧路径在 100,001 条读取到 cap 后失败且峰值 52.10 MiB，新路径完成构建为
5.9504 秒 / 3.71 MiB。这里的 traced wall time 包含逐分配追踪开销，不是 HTTP 延迟目标。

同一环境下，单方案回测候选从 10,000 / 50,000 / 100,001 条增长时，旧路径分别物化全部候选并使用
6.56 / 32.84 / 65.42 MiB（100,001 条失败）；数据库选择后 Python 均只接收 1 条，峰值约 0.02 MiB，
对应读取与选择总耗时约 0.0124 / 0.0865 / 0.1726 秒。ECS 验证必须另记录真实 MySQL 查询计划、
`backend_total`、DB timing、响应大小和并发错误率；上述合成结果不用于放宽 4 秒请求预算。

## 生产方案标记

Summary 每条 `schemes[]` 必须包含布尔值 `is_production`。唯一依据是本机外置
`<BFL_RUNTIME_ROOT>/config/production_schemes.json`；前端只透传该布尔值，在候选排行的方案名称右侧
显示 12px 深绿色实心菱形，间距 8px，保留名称截断与标记完整性；不增加可见文字、列、筛选或交互。
名单由运维确认，不从 active、部署矩阵、主机角色或自然运行证据推断。名单不改变可见方案、排序、统计或执行资格。

文件为 UTF-8 对象，仅接受 `{}` 或 `{"scheme_ids": [...]}`；数组元素必须为无首尾空白的非空字符串，
不得重复或含未知字段。空数组也表示空名单。按完整 composite Registry ID 精确匹配，不限制期限/horizon，
不扩展同 base 的其他 target；同 ID 升级 exact 保留标记，新 ID 不继承。

每次 Summary 在事务外读取并校验一次文件，不缓存旧名单。`BFL_RUNTIME_ROOT` 必须显式指定存在的绝对目录，
无开发或仓库路径回退。非空合法名单在本次只读一致性事务中执行一次参数化批量 Registry 存在性查询，
不按 status 过滤；不存在项逐项忽略并记 `production_scheme_not_found`，其余有效项继续匹配。
paused/archived 身份仍通过存在性校验，但按原有 active 可见性规则隐藏，恢复可见时仍可匹配。

路径、读取、编码、JSON 或结构错误只在名单读取边界降级：记 `production_schemes_invalid` 原因，
本次全部返回 `false`，不记录完整文件；合法空名单不记错，也不增加存在性 SQL。
数据库查询或原有 Dashboard 合同失败仍返回 503。Detail 不读取名单、不增加字段，仅共用 V7 schema。

完整文件替换后下一次成功 Summary 撤下旧标记并应用新名单，沿用五分钟刷新与下述 stale 行为；
HTTP 失败仍保留已提交旧快照。两机各读本地文件，程序不自动同步。
初始化、结构预检、原子替换、双机一致性及纠错步骤见[部署手册](../../deploy/README.md#生产方案名单维护)。
公共检查复用现有 Dashboard builder/Gate 测试；非空、替换、故障恢复及长名称视觉在隔离环境验收，
真实环境按已获授权的完整名单核对 API 与页面，不能注入示例名单或以文件存在代替展示验收。

## 浏览器刷新与失败展示

Dashboard 展示离散发布的数据库事实。首次认证后立即读取；可见页面每五分钟刷新，隐藏时停止计时器；
恢复可见时仅在成功快照满五分钟或先前刷新失败时立即读取。任一时刻最多一个 Summary 请求。

- 无成功快照时失败：显示“数据不可用”，不猜测数据。
- 已有成功快照后失败：保留完整已提交视图、筛选和已开月历，显示“刷新失败，显示 `<generated_at>` 数据”，
  DOM 标记 `stale`，不得称为最新；这只是当前浏览器会话暂存，服务端仍按上述失败合同返回。
- 后续成功响应原子替换整个 Summary、已提交日期范围并恢复 fresh，不混合新旧方案、月份或 Detail 缓存。筛选请求未完成或失败时，不得用新日期标题标注旧区间指标。
- 注销、认证失效或身份切换按认证合同清空前端业务状态。

重试间隔依次 10 秒、30 秒、60 秒、5 分钟，连续失败后维持五分钟；成功恢复正常周期。

## 认证响应与合同验收

先通过对应环境的正常授权会话取得真实 HTTP Summary/Detail，记录状态、响应头、payload 及证据时间，
不保存密码、Cookie 或 token。对照本机 Registry、预测和 Actual，逐方案检查身份、owner、月份、明细和待验证状态；
exact 另由数据库与 release 核验，Dashboard 不提供该字段。

现有 [`DashboardGate`](../../harness/gates/dashboard_gate.py) 支持真实认证入口和 `fetcher` 注入。默认 fetcher
接收 URL、`timeout_sec`、`max_response_bytes` 和仅在内存中使用的会话 token，按限制读取响应并校验
UTF-8/JSON；返回 payload、HTTP status 以及安全的获取时间和 `X-Request-ID` 元数据。自定义 fetcher 不能
绕过读取预算、构造成功状态或伪造 payload。会话只允许从 owner-only 的绝对路径普通文件，或调用方已经打开的
文件描述符传入；输入内容只是一行 opaque token。CLI 不接受明文 Cookie/token 参数，结果与 Evidence 不保存
token、Cookie 或会话文件内容。HTTP 错误只保留稳定状态码，不把响应正文写入结果；响应派生的错误与
Evidence 在返回前还会递归清除本次 session token，防止目标服务误回显凭据。

携带会话的探针把首次请求 URL 的 scheme、host 和有效端口固定为目标 Origin；同 Origin 跳转可继续，任何
跨 Origin 跳转在发送凭据前失败。TLS 使用 Python 默认校验，不提供关闭证书校验的选项。会话缺失、过期或
无效均使 Gate 失败，不能为运行验收关闭认证。单方案调用仍受支持；同一次验收可重复 `--scheme-id` 选择多个
base scheme，例如：

```bash
python -B -m harness gate dashboard \
  --api-base-url https://factor.example.invalid \
  --api-prefix /bond-factor-lab \
  --scheme-id first_scheme \
  --scheme-id second_scheme \
  --session-file /absolute/private/path/dashboard-session-token
```

也可通过 `--session-fd <fd>` 从 pipe 或已打开的私有文件读取；调用方必须在启动命令前写入完整 token，未就绪的
pipe 会立即失败而不会在 Gate 总预算开始前无限等待。会话文件权限不得向 group/world 开放，两个会话入口互斥
且必须提供其一。命令只获取一次真实 Summary、校验 HTTP 与 V7 schema，并在一个只读事务中批量读取
本次全部 Registry，然后逐方案比较名称、描述、owner、active composite、任务字段和回测分区；名称去首尾
空白、空描述转空字符串，与 API 表示一致，不从 canonical 推断新身份。每个方案分别输出通过/失败，并引用相同
的 response `snapshot_id`、获取时间和 request ID。一个方案失败不会抹掉其他方案的结果，但整次 Gate 失败。
普通方案没有 live 月度计数合法，但仍须有回测分区。唯一已登记且旧回测已清除的 5Y/h5 范围可无回测分区，
前提是 Summary 至少有一行 `samples > 0` 的 live 月度计数；该结构检查不能替代独立 DataConsistency
对冻结的 78 条 live 事实逐值核验。非 200、响应超限或非法结构均失败。`GateContext.engine_factory` 必须显式绑定该 HTTP
服务的本机数据库，执行前按[手工环境绑定](../../deploy/README.md#手工-harness-的目标环境绑定)核实；未提供
或查询失败则 Gate 失败，不回退默认库。gzip/no-store/响应头由 API 合同测试保护。

预测值、Actual 与统计数的完整对账使用独立 `DataConsistencyGate`，不能以 Dashboard schema 校验替代。
该 Gate 从显式绑定的只读数据库快照独立检查业务键、三日期、方向、run 引用与 Actual 事实，并按特征基准日独立形成
月份/source 计数后对照真实 Summary 及逐分区 Detail；标准答案不得调用 Dashboard builder 或其聚合 helper。
`timeout_sec` 是整次对账共享的总预算；Summary、各 Detail 和首尾数据库快照都从同一单调时钟 deadline 扣减，
不会按方案或月份重新获得完整预算。
末次重读失败、选定数据库范围首尾摘要不同或上海展示日跨界时结果为 `blocked`，不把可观测到的自然增长
报告为数据错误；该首尾围栏不是 CDC，不承诺识别内容完全恢复的瞬态变化。
批量入口与 Dashboard Gate 共用安全会话输入：

```bash
python -B -m harness gate data-consistency \
  --api-base-url https://factor.example.invalid \
  --api-prefix /bond-factor-lab \
  --scheme-id first_scheme \
  --scheme-id second_scheme \
  --session-file /absolute/private/path/dashboard-session-token
```

普通发布在[部署流程](../../deploy/README.md#构建预安装和晋级)的健康、数据与调度读回之外，完成上述认证 HTTP
对照，并刷新浏览器核验 fresh、方案数与明细，不使用旧视图作证。批量请求遇 429 时降低频率并按 Retry-After
退避，不调整 Nginx 或认证来完成验收。

仅在读路径、浏览器刷新或入口网络发生相关变更时，按影响范围开展专项验收：

- 可见页面正常轮询不超过每小时 12 次、隐藏无轮询，已发布事实最迟五分钟可见；
- 经授权的读路径故障注入：一次 503 保留完整视图并标 stale，后续成功原子恢复 fresh；
- 经授权的读压测无 503/504，完整 `backend_total` p99 < 1.5 秒、DB p99 < 750ms；
- 涉及隧道/代理时核对唯一 listener 与 Clash 不出现生产 SSH `dial GLOBAL`。

故障注入和生产压测须有专项授权，不因普通算法包发布或文档整理自动执行；不得停止 Writer、写业务库或制造预测缺口。

## 故障定位与恢复

固定地址和链路图仅维护于访问入口。对 Mac3 公网链路使用三点探针：

1. Mac3 本地 health 成功、中继转发端口失败：检查 SSH 隧道。
2. 中继转发成功、公网失败：检查 Nginx、TLS 和公网入口。
3. health 成功、Dashboard 503：按 X-Request-ID 检查数据库和构建阶段；401 则检查入口和会话。
4. Dashboard 200、浏览器 stale：检查客户端网络、解析和刷新状态机。

Dashboard 结构化事件写 Uvicorn error logger；认证在进入 Dashboard 路由前失败时也用同一 request ID 记录
安全的耗时与 `failure_stage=auth`，不记录异常正文、token 或 DSN。Nginx timing log 以
`$time_iso8601 $request_id` 开头并记录 `$upstream_status`，用同一 X-Request-ID 关联时间。入口预算耗尽时
据此定位，不继续放宽 timeout 掩盖故障。

生产 SSH 固定连接中继 IPv4，host-key 核验按访问入口执行。有限超时、保活和转发失败退出的期望值见
[tunnel plist](../../deploy/launchd/com.bond-factor-lab.ssh-tunnel.plist)。
Clash/Mihomo TUN 开启时使用 rule 模式，将中继 IPv4 指向 DIRECT，并用 route-exclude-address 排除其 /32；
验收看生产 SSH 不出现 dial GLOBAL，不能只检查 DNS 是否返回真实 IP。

应用 release、Nginx、Clash 和 installed tunnel 分别保存生效配置与只读基线，只处理故障所属的已授权单元：

- 应用异常按[部署手册](../../deploy/README.md#回滚与认证部署)核验 previous 与 schema/状态兼容后恢复。
- Nginx 恢复上一已验证 site，语法检查通过并获授权后 reload。
- tunnel/代理恢复上一 installed plist 和 Clash 配置，确认旧会话退出及唯一 owner 后恢复；
  修改前检查转发端口的唯一 listener，不手工启动第二条 ssh -R。
- DNS/代理以现场生效配置定位，保留原值，不照抄历史发布窗口配置。
- 恢复后重复三点 health、认证 Summary、响应标识和两端日志检查，保留失败 request ID 与时间窗口。

读路径诊断不授予上述配置变更或启停权限，也不能通过业务写库、覆盖预测、重启 Writer 或复制另一机数据库恢复页面。
