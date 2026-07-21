# Bond Factor Lab 公网数据刷新 P95 小于 1 秒设计

**日期**：2026-07-22

**状态**：设计已获用户确认，待实施

**范围**：公网因子实验室页面、展示只读 API、前端加载状态、入口层访问控制与性能验收

**生产基准分支**：`master`

**实施分支**：`codex/audit-bugfixes-20260613`

## 1. 决策摘要

本次优化采用“展示专用一致性快照”方案：

1. 新增 `GET /api/factor-lab/dashboard`，一次返回前端完整展示所需的 active Registry 元数据、live 明细和 latest backtest 明细。
2. dashboard 不是旧 `/api/schemes`、35 个 `/api/metrics/{scheme_id}` 与 `/api/backtests/factor-lab` 响应的简单拼接；后端在同一个数据库连接和一致性只读事务内批量读取并复用现有 canonical 选择语义。
3. 明细仍是前端指标的唯一事实源。只压缩协议，不删除明细，不增加汇总替代或异步懒加载。
4. 使用紧凑、版本化的 DTO，方向明细用定长数组表达；FastAPI 在 SSH 隧道之前执行标准兼容的 gzip-6 压缩。
5. 增加启动预热、最长 1 秒的进程内快照缓存和 single-flight 重建，防止并发请求重复查询和构包。
6. 前端使用带请求代次的原子状态机；刷新失败时保留最后可用快照并明确显示 stale，不清空已有数据，也不静默回退到慢链路。
7. 公网 Nginx 从宽泛页面代理改为精确白名单，并在新前端稳定后撤销旧展示 API 的公网白名单。
8. 发布验收以真实公网 Chrome 硬刷新到“完整数据完成绘制”为终点，主门槛为至少 200 次样本的 P95 小于 1000 毫秒且零 legacy fallback。

## 2. 背景与基线

当前公网入口为：

```text
https://bond.finailab.cn/bond-factor-lab/
```

当前加载链路为：

```text
index + CSS + JS
  -> GET /api/schemes
  -> 并发 GET /api/metrics/{scheme_id} × 35
  -> GET /api/backtests/factor-lab
  -> 前端合并、按 target_date 分月、重算指标、绘制页面
```

2026-07-22 只读基线：

- active Registry 方案：35 个。
- live 明细：1,035 行。
- latest backtest 明细：8,925 行。
- 旧接口完整响应合计约 2.72 MB。
- 公网 `/api/backtests/factor-lab` 单包约 2.12 MB，实测总耗时约 4.4–7.5 秒。
- 公网直达页一次硬刷新到数据可用实测约 7.9 秒。
- 本机服务构建旧三类响应约 0.31–0.79 秒，存在明显冷热差异。
- 当前响应没有 gzip；SSH 隧道有效传输速度是主要瓶颈之一。
- 完整但紧凑的明细原型可以压到 gzip-6 约 40–100 KB，证明目标在当前数据规模下具备工程可行性。

当前慢加载不是单一 SQL 或单一前端渲染问题，而是请求扇出、重复查询、响应冗余、无隧道前压缩、冷路径、第三方字体和缺少并发保护叠加的结果。

## 3. 目标与 SLO

### 3.1 主目标

在约定业务探测地点、Chrome 浏览器和真实公网入口下：

```text
direct public navigation start
  -> dashboard 数据获取
  -> schema 校验与完整解码
  -> live/backtest 合并与指标派生
  -> DOM 原子提交
  -> 下一次 requestAnimationFrame 完成
```

上述端到端时间满足：

- 硬刷新、浏览器缓存禁用、至少 200 次连续尝试，成功、失败和超时全部计入；
- nearest-rank P95 `< 1000ms`；
- 零 5xx、零结构错误、零 legacy fallback；
- 页面 scheme 数、live/backtest 行数和展示指标与 canonical 旧接口等价。

### 3.2 配套预算

- dashboard gzip-6 响应：当前规模 `<= 100KB`。
- dashboard raw JSON：当前规模 `<= 1.5MB`。
- 后端 snapshot 批量读取、canonical 选择和 DTO 构建 P95：目标 `<= 300ms`。
- 单次正常请求只允许一个 dashboard 数据请求，不允许请求旧 schemes/metrics/backtest API。
- 健康状态下 stale 响应率必须为 0。
- 数据快照正常最大缓存年龄为 1 秒；异常降级时必须显式标记 stale 和快照年龄。

这些预算既是验收条件，也是数据规模增长时的保护阈值；不能只记录平均值或只验收 API TTFB。

### 3.3 SLO 边界

主 SLO 必须记录探测位置、浏览器版本、设备、网络、DNS/TCP/TLS 是否复用。任意地区、任意公网质量和任意终端的绝对 1 秒保证不属于可实现承诺。

另行报告但不从样本中删除：

- 新 TCP/TLS 冷连接；
- 后端授权重启后的首个请求；
- panda_quantflow 外层 iframe 的父页面开销；
- 网络不可达、隧道重连或数据库不可用场景。

## 4. 非目标

- 不修改任何 Native V1 或 Blackbox V2 算法逻辑。
- 不改变预测、actual、回测的事实语义、Registry composite ID 或任务分列规则。
- 不用月度汇总替代预测明细。
- 不把所有历史 run 或管理 API 暴露到公网。
- 本次不清理或迁移周/月 actual 表的历史唯一键；先增加读取冲突保护，唯一键迁移另行审计。
- 不在本次把服务扩展到多个 Uvicorn worker；如未来增加 worker，缓存和 single-flight 必须改为跨进程设计。
- 不承诺任意公网访问者在任意网络下都小于 1 秒。

## 5. 不可破坏的数据语义

dashboard 必须遵守现有架构不变量和 `PREDICTION_SEMANTICS.md`：

1. 只返回 `status='active'` 的 Registry composite `scheme_id`。
2. 前端格子只由 `target_tenor + task_type` 决定，不能从 `frequency/horizon` 猜测。
3. `predict_date`、`feature_date`、`target_date` 三个标准日期都保留。
4. live 月份和 backtest 月份都只按 `target_date[:7]` 归属。
5. 只过滤未来 `predict_date`；未来 `target_date` 必须保留为待验证预测。
6. live actual 类型由 Registry `task_type` 决定：T+1、T+5、weekly point、weekly average 和 monthly 不得混用。
7. live 同一 prediction point 的 canonical 选择继续复用现有规则；周频必须保留 feature/predict/id tie-break，不能简单取最大 ID。
8. backtest 只读取 canonical latest-success run，继续按 runtime type 选择默认 data source，并按精确 `run_id` 读取不可变明细。
9. `predicted_direction=0` 计入样本总数和方向分布，但不进入任何准确率或 precision/recall 分母。
10. `actual_direction=null` 表示待验证，不得变成 0 或错误样本。
11. 日/周频同月的 backtest 与 live 明细可以同时存在；月频从最早 live `target_date` 月开始整月裁掉 backtest。
12. `deployed_at` 只来自 active Registry，缺失时整份快照 fail-closed。
13. `feature_date` 即使当前表格未直接绘制，仍是平台、业务和前端标准字段，不得为了压缩删除。

## 6. 总体架构

```text
Browser / iframe
  -> exact public URL and versioned local static assets
  -> GET /api/factor-lab/dashboard
  -> Nginx exact allowlist + rate/connection limit
  -> SSH reverse tunnel
  -> FastAPI dashboard endpoint
     -> in-process snapshot store
        -> fresh cache hit: return cached canonical payload
        -> expired/missing: single-flight rebuild
           -> one SQLAlchemy Connection
           -> one consistent read-only transaction
           -> bulk reads + canonical selectors
           -> compact DTO + validation + response budgets
     -> standards-compliant gzip-6 before tunnel
  -> strict decode into local candidate snapshot
  -> one atomic frontend commit
  -> next animation frame marks data-ready
```

旧 API 继续保留在 FastAPI 中供本机兼容、harness 和回滚使用。新前端稳定后，它们不再属于公网白名单。

## 7. Dashboard API 合同

### 7.1 路径与方法

- `GET /api/factor-lab/dashboard`：返回当前展示快照。
- `HEAD /api/factor-lab/dashboard`：显式支持，与 GET 使用相同可用性和表示头，但不返回 body。
- 不提供查询参数；出现任何查询参数统一返回 400，避免产生未定义的缓存变体。
- dashboard 响应设置 `Cache-Control: no-store`；浏览器每次刷新都向服务请求，服务端内部缓存负责去重。

### 7.2 顶层协议

示意结构：

```json
{
  "schema_version": "factor-lab-dashboard-v1",
  "snapshot_id": "opaque-id",
  "generated_at": "2026-07-22T12:00:00.123+08:00",
  "display_until": "2026-07-22",
  "stale": false,
  "snapshot_age_ms": 12,
  "row_fields": [
    "predict_date",
    "feature_date",
    "target_date",
    "prediction_phase",
    "predicted_direction",
    "actual_direction"
  ],
  "target_labels": {
    "5Y": "5Y国债活跃"
  },
  "schemes": []
}
```

`snapshot_id` 是不透明生成标识，只用于前端竞态、日志和验收，不承载算法版本或内部路径。

### 7.3 Scheme 结构

Scheme 数量很少，元数据继续使用可读对象；只有高数量明细使用数组压缩：

```json
{
  "scheme_id": "base__h5__5Y",
  "base_scheme_id": "base",
  "name": "方案名称",
  "description": "",
  "target_tenor": "5Y",
  "target_label": "5Y国债活跃",
  "task_type": "T+5",
  "horizon": 5,
  "frequency": "daily",
  "status": "active",
  "deployed_at": "2026-06-04",
  "live_rows": [],
  "backtest": {
    "benchmark_id": "...",
    "benchmark_label": "...",
    "data_source": "...",
    "data_source_label": "...",
    "latest_run_date": "2026-05-30",
    "rows": []
  }
}
```

没有 backtest 时 `backtest` 为 `null`；没有 live 行时 `live_rows` 为空数组。不能用缺字段表达这两种状态。

### 7.4 明细字段

每个明细数组严格对应顶层 `row_fields`：

```json
[
  "2026-06-05",
  "2026-06-04",
  "2026-06-11",
  "scheduled_live",
  1,
  null
]
```

规则：

- `predict_date`、`feature_date`、`target_date` 必须是非空 ISO 日期。
- live `prediction_phase` 只能是 `gray_live` 或 `scheduled_live`。
- backtest `prediction_phase` 固定为 `null`。
- `predicted_direction` 只能是 `-1`、`0`、`1`。
- `actual_direction` 只能是 `-1`、`0`、`1` 或 `null`；backtest canonical 明细必须可评价，因此不能为 `null`。
- `is_correct` 不传，由前端从两方向计算。
- `confidence`、`run_id`、`scheme_version`、`model_version`、`input_artifact_hash`、harness/generation 等当前 UI 未使用字段不进入公网 DTO。
- `monthly_metrics` 和 summary 不作为展示输入，因此不传；所有指标继续由明细派生。

### 7.5 协议校验

后端构建完成后、写入缓存前先自校验；前端收到后再独立校验：

- schema version 必须完全匹配。
- `row_fields` 顺序、数量和内容必须完全匹配且无重复。
- 每行数组长度必须等于字段数。
- scheme ID 必须唯一，且与 `base_scheme_id + horizon + target_tenor` 的 Registry 身份一致。
- `task_type`、`target_tenor`、`deployed_at` 缺失或非法时整包拒绝。
- 每个 source 内 canonical prediction point 必须唯一；跨 source 同一 target 允许。
- 日期、方向和 phase 必须通过枚举与格式校验。
- 禁止 truthy fallback；解码必须原样保留 `0`、`false` 和 `null`。

已知 V1 协议内容损坏必须 fail-closed，不得进入 legacy fallback。未知 schema version 只允许在明确兼容策略下处理；V1 首次发布默认 fail-closed。

## 8. 后端一致性快照

### 8.1 单 Connection

不能在 endpoint 外层开启事务后继续把 `Engine` 传给现有服务函数，因为现有函数会自行 checkout 新连接。实现必须拆出接受 `Connection` 的私有读取核心：

- 旧 API wrapper 仍可自行获取连接，保持兼容。
- dashboard builder 把同一个 `Connection` 传给所有查询核心。
- 测试记录 connection identity，证明 dashboard 全部业务 SELECT 使用同一连接。
- 测试捕获 SQL，证明 dashboard 路径没有 INSERT、UPDATE、DELETE 或 DDL。

### 8.2 数据库事务

MySQL：

- 在任何业务 SELECT 前设置 `REPEATABLE READ`。
- 显式开启一致性、只读事务。
- 所有相关表当前必须为 InnoDB；启动或集成验证检查这一前提。
- 数据全部物化后立即结束事务，再进行 JSON 序列化和 gzip，避免不必要地延长 MVCC snapshot 生命周期。

SQLite 测试：

- 使用同一个 Connection 和普通事务/default SERIALIZABLE。
- 不发送 MySQL 专用隔离级别或 `START TRANSACTION ... READ ONLY` SQL。
- 通过 dialect 分支和 SQL 捕获测试锁定行为。

### 8.3 批量读取计划

目标是把约 145 条查询收敛到约 5–7 条，而不是循环调用 35 次旧 `scheme_metrics()`：

1. 一次读取 active Registry 与展示元数据。
2. 一次读取 active target registry/labels。
3. 一次读取所有 active scope 的 live predictions。
4. 批量读取 daily/weekly/monthly actual；可以用带 source/type 标记的 `UNION ALL` 或少量独立查询。
5. 一次读取 canonical latest backtest run 候选。
6. Python 复用 runtime default source 和 latest-by-scope 选择规则，得到精确 run IDs。
7. 一次使用 expanding `IN (:run_ids)` 读取所有选定 run 的 backtest 明细。

批量查询只改变 I/O 形态，不能改变 canonical Python 选择语义。

### 8.4 Canonical 选择

必须抽取和复用现有选择器，而不是另写相似但不等价的逻辑：

- live prediction point 以 `target_date` 为事实点。
- 非周频 rerun 继续按现有 ID/审批语义选优。
- 周频先比较 feature window，再比较 predict date 和 ID。
- actual source 由 Registry `task_type` 和 target rule 决定。
- backtest view 只允许 latest-success。
- data source 继续按 runtime type 选择默认值。
- 多 benchmark 场景继续按现有 scope rank 选最新项。
- 回测只读取选中 `run_id` 的 `t_backtest_predictions`，不得改读月度汇总表。

### 8.5 Actual 冲突保护

业务事实键为：

```text
target_tenor + target_date + target_rule
```

当前库只读审计发现：

- weekly 重复事实键：0 组。
- monthly 重复事实键：130 组。
- weekly/monthly 不同方向冲突：均为 0 组。

新读取核心必须：

1. 对事实键聚合并检查 `COUNT(DISTINCT direction)`。
2. 不同方向数大于 1 时 fail-closed，并记录不含敏感数据的冲突定位信息。
3. 多行但方向相同时只折叠成一个事实值。
4. 不允许继续用 `MAX(direction_monthly)` 静默决定方向。

长期的表唯一键迁移需要先清理历史重复并单独评审，不塞入本次性能发布。

### 8.6 日期截止

dashboard 请求开始时按 `Asia/Shanghai` 捕获一次 `display_until`，在整份 snapshot 中复用：

- 只排除 `predict_date > display_until`。
- 不排除 `target_date > display_until`，它们是待验证预测。
- 所有分月都用 `target_date`。
- 不在 35 个方案循环中重复调用系统日期，避免跨午夜得到不同 cutoff。

## 9. 快照缓存与 single-flight

### 9.1 缓存对象

当前单 worker 进程内维护一个 `DashboardSnapshotStore`：

- 最近一次成功的 canonical payload。
- `snapshot_id`、`generated_at` 和单调时钟生成时间。
- 预序列化 raw bytes 或可快速序列化的只读 payload。
- 最近一次构建耗时、行数和大小预算。
- 当前 single-flight Future/锁。

缓存只保存展示 DTO，不缓存数据库连接、SQLAlchemy Row 或事务对象。

### 9.2 新鲜度

- 正常 TTL 最大为 1 秒，使用单调时钟判断，避免系统时钟回拨。
- 应用启动并完成 registry startup sync 后预构建一次。
- TTL 内直接返回最近成功快照。
- TTL 过期或尚无快照时触发重建。
- 数据正常传播延迟由 TTL 加一次构建时间界定；响应必须返回 `generated_at` 和 `snapshot_age_ms`。

### 9.3 Single-flight

- 同一进程同时只能有一个 snapshot rebuild。
- 后续请求复用同一个 Future，不再查询数据库或重复构包。
- single-flight 状态必须在成功、异常和取消路径都释放。
- 增加并发测试，证明 1/5/10 个同时请求只执行一次 builder。

### 9.4 失败和 stale

- 重建成功：原子替换 LKG，返回 `stale=false`。
- 重建失败且存在 LKG：保留 LKG，返回 `stale=true`、快照年龄和稳定警告码；不得返回半成品。
- 重建失败且没有 LKG：返回 503，不能返回空的 200。
- 并发等待超过端到端预算且存在 LKG：允许返回显式 stale；后台唯一 rebuild 继续完成。
- 健康验收期间任何 stale 都算失败样本；生产异常时 stale 是可见的降级能力，不是 SLO 作弊。

## 10. 压缩和静态资源

### 10.1 Gzip

压缩必须发生在 Mac FastAPI 侧、进入 SSH 隧道之前：

- `minimum_size` 采用合理小阈值，覆盖 JSON、JS、CSS 和 SVG。
- `compresslevel=6`；level 9 的额外 CPU 与节省字节不成比例。
- 正确解析 `Accept-Encoding` quality factor；`gzip;q=0` 不得压缩。
- gzip 表示和 identity 表示都必须包含正确的 `Vary: Accept-Encoding`。
- 已存在 `Content-Encoding` 时不能重复压缩。
- 公网验收确认只有一层 gzip，一次解压成功、第二次解压失败。

若当前 Starlette middleware 不能正确处理 q-value 和 Vary，则增加小型兼容 wrapper，不把已知协议问题带入共享缓存。

### 10.2 第三方字体

删除 CSS 顶部 Google Fonts `@import`，改用系统字体栈，避免国内网络和第三方域名成为 render-blocking 尾延迟来源。本次不自托管额外字体文件，以免增加硬刷新字节数。

### 10.3 静态缓存

- `index.html` 使用 `no-cache, must-revalidate`，确保获得最新 asset 版本引用。
- JS/CSS 修改后必须更新 query cache-buster。
- 带明确版本参数的 JS/CSS 可使用长缓存；无版本资源保守 revalidate。
- 主 P95 验收始终禁用浏览器缓存，因此静态缓存优化不能掩盖冷加载问题。

## 11. 前端状态机

### 11.1 状态

```text
cold -> loading-dashboard

loading-dashboard -> ready-dashboard
loading-dashboard -> loading-legacy   only 404 / explicitly unsupported during rollout
loading-dashboard -> error

ready-dashboard -> refreshing
refreshing -> ready-dashboard         new snapshot committed atomically
refreshing -> stale                   refresh failed, keep previous snapshot
stale -> refreshing
```

`OnLine` 不能再充当 data-ready。页面应区分 service online、data loading、data ready、data stale 和 data error。

### 11.2 请求代次

- 每次加载增加 `loadSeq`。
- decode、derive 和 commit 前都检查当前 sequence。
- 支持时使用 `AbortController` 取消旧请求，但 sequence 校验始终保留。
- 迟到响应不得覆盖更新的 snapshot。
- dashboard 首次成功后，本页 capability 锁定为 dashboard；后续 transient failure 不得降级到 legacy。
- dashboard 首次返回 404/明确不支持时，本页标记 legacy-only，后续刷新不再每分钟多请求一次 dashboard。

### 11.3 Fallback

旧协议 fallback 只服务于分阶段发布和回滚：

- 允许：dashboard 404 或明确的“协议未部署”状态。
- 禁止：403、429、5xx、网络超时、V1 schema 损坏、字段缺失、重复 canonical point、数据语义错误。
- 403 表示 Nginx/访问控制发布错误，必须暴露。
- 5xx/超时不能通过 37 个旧请求继续放大后端压力。
- 性能验收要求 `mode=dashboard`，任何 fallback 样本失败。

### 11.4 原子解码和提交

前端不得在 schema 尚未全部验证时修改全局 labels 或 scheme 状态：

1. 在局部对象校验顶层、scheme 和 row arrays。
2. 单遍解码并直接生成最终 daily row。
3. 按 scheme/month/source 分组，source 由 `live_rows`/`backtest.rows` 容器隐含。
4. 派生 phase ranges、latest run、monthly rows 和指标缓存。
5. 应用月频 live 边界裁剪，日/周频保留跨 source 同月数据。
6. 所有步骤成功后一次替换 `factorTaskSchemes`、target labels 和状态。
7. 调用 render，等待下一次 `requestAnimationFrame` 后设置 data-ready 标记。

刷新失败时保留旧 DOM 和数据，只更新 stale/error banner。首次加载失败且没有 LKG 才展示空错误状态。

### 11.5 计算和渲染

- 解码时不同时保留 compact arrays、临时旧 API 对象和最终 UI 对象三份数据。
- 对 `(snapshot_id, scheme_id, month_range, source)` 缓存聚合指标，避免一次 render 重复遍历全部明细。
- 后端固定 scheme、source、target_date、predict_date 顺序；前端关键展示仍显式排序，不依赖排序稳定性。
- drawer 打开期间 snapshot 更新时必须重绘或安全关闭，不能继续展示旧 scheme 对象。
- iframe/tab 隐藏时暂停 60 秒轮询；恢复可见时立即刷新，并使用失败退避防止错误风暴。

## 12. 公网访问控制

### 12.1 精确白名单

当前宽泛 `location /bond-factor-lab/` 会把 `/docs` 和 `/openapi.json` 转发给 FastAPI，实测均为 200，不符合 default-deny。

发布后公网只放行：

- `/bond-factor-lab` 到 `/bond-factor-lab/` 的固定 HTTPS 跳转。
- `/bond-factor-lab/` 和必要时的精确 `/index.html`。
- `/bond-factor-lab/aifin-shell.js`。
- `/bond-factor-lab/aifin-shell.css`。
- 两个明确的 SVG assets。
- `/bond-factor-lab/api/health`。
- `/bond-factor-lab/api/factor-lab/dashboard`。

其余路径默认 403，包括：

- `/docs`、`/redoc`、`/openapi.json`。
- 旧 public schemes/metrics/backtest APIs（完成灰度后撤销）。
- predictions、actuals、targets、run/diff/data-check APIs。
- trigger、admin 和所有写入接口。
- 未知页面、大小写变体、双斜杠、编码斜杠、dot-segment 和尾斜杠变体。

dashboard 精确 location 必须出现在通用 `/api` deny 规则之前。

### 12.2 限流和并发

- dashboard 设置独立 `limit_req` 和 `limit_conn`。
- 限额由 1/5/10 并发容量测试结果确定，必须考虑企业 NAT，不能随意设置过低的单 IP 阈值。
- 过载快速返回 429/503，不让请求占用 30 秒 upstream timeout。
- health 使用独立、低成本路径，dashboard 滥用不能拖垮健康检查。
- `Accept-Encoding: identity` 高频请求也受限，不能依赖 gzip 作为唯一 DoS 防护。

### 12.3 TLS 和路径验收

公网验收脚本不得使用 `curl -k` 作为主 TLS 验收；必须验证证书链、主机名和有效期。重定向还必须校验固定 HTTPS `Location`。

## 13. 可观测性

### 13.1 应用指标和日志

每个 dashboard 请求记录或暴露：

- request ID、snapshot ID。
- cache hit/miss/stale 和 snapshot age。
- DB read、canonical build、serialization 的耗时。
- scheme 数、live/backtest 行数、raw bytes、gzip bytes。
- rebuild success/failure 和 single-flight waiter 数。
- HTTP 状态与错误类别，但不记录完整预测 payload。

可使用 `Server-Timing` 暴露安全的阶段耗时，便于浏览器探针区分后端和网络时间。

### 13.2 Nginx 日志

为 Bond Factor Lab 配置独立 access log，至少包含：

- request time。
- upstream connect/header/response time。
- status、wire bytes 和 Content-Encoding。
- request method/path、限流状态和 request ID。

日志不能包含 admin token 或敏感查询内容。

### 13.3 前端 readiness

页面在下一帧绘制后设置机器可读状态，至少包括：

- `mode=dashboard`。
- snapshot ID。
- scheme/live/backtest row counts。
- ready/stale/error。
- navigation-to-ready duration。

探针必须校验预期数量，避免“空页面很快”被误判为成功。

## 14. 测试策略

### 14.1 后端语义 parity

把 dashboard 解码成规范化对象，与旧 API canonical 输出逐行比较，覆盖：

- active-only 和 composite registry identity。
- T+1、T+5。
- Blackbox `weekly_point h=1`。
- weekly average 和 target rule。
- monthly。
- 同 target rerun 和周频 tie-break。
- 多 benchmark、latest-success、runtime default data source。
- 缺回测明细 fail-closed。
- deployed_at 缺失 fail-closed。
- 未来 predict date 被过滤、未来 target date 被保留。
- target-date 跨月归属。
- gray/scheduled live phase。
- flat 0、pending null、错误方向和精确指标分母。

### 14.2 数据冲突

- 同 actual 事实键、相同方向的重复行安全折叠。
- 同 actual 事实键、不同方向整包失败。
- live/backtest source 内重复 canonical point 整包失败。
- 跨 source 同 target 对日/周频允许，对月频按 live cutoff 裁剪。

### 14.3 Connection 和事务

- dashboard 所有 SELECT 使用同一 connection identity。
- MySQL transaction 在业务 SELECT 前进入正确隔离和只读模式。
- SQLite 不执行 MySQL 专用 SQL。
- dashboard SQL 捕获中不存在 DML/DDL。
- 事务在序列化和 gzip 前结束。

### 14.4 Cache 和并发

- startup prewarm 成功与失败。
- TTL hit、expiry 和原子替换。
- 1/5/10 并发只调用一次 builder。
- builder 异常释放 single-flight。
- 有 LKG 时返回显式 stale；无 LKG 时返回 503。
- 系统时钟变化不破坏 TTL。

### 14.5 前端协议和竞态

- V1 完整解码。
- schema version、row fields、row length、方向、日期、phase、identity 各类损坏 fail-closed。
- `0` 不变成 null，null 不变成 0。
- 迟到响应不能覆盖新 generation。
- AbortController 不可用时 sequence 保护仍有效。
- 404 rollout fallback；403/429/5xx/timeout/schema error 不 fallback。
- dashboard 成功后 capability 锁定。
- refresh failure 保留旧数据并显示 stale。
- target labels 和 scheme state 原子提交。
- drawer、visibility pause/resume 和刷新退避。

### 14.6 HTTP 和 gzip

- gzip 响应只含一层 `Content-Encoding: gzip`。
- gzip/identity 都有正确 Content-Length 和 `Vary`。
- `gzip;q=0`、identity 和不支持的编码不错误压缩。
- 解压后的 schema 与 identity body 等价。
- GET/HEAD 合同一致。
- JS/CSS/SVG 在隧道前得到预期压缩。

### 14.7 公网安全矩阵

- 精确白名单返回 200。
- docs/openapi/redoc、旧 API、写 API 和未知路径返回 403。
- 大小写、双斜杠、尾斜杠、`%2f`、`%2e%2e` 和编码字符不能绕过。
- TLS 不使用 `-k` 时通过。
- HTTP 到 HTTPS 的 Location 正确。

## 15. 性能和容量验收

### 15.1 Dashboard API

- 外部顺序请求至少 200 次。
- 报告 P50/P95/P99，P95 使用 `sorted_samples[ceil(0.95*n)-1]`。
- 失败样本、stale 和 fallback 不得从统计中删除。
- 单独报告 cache hit、TTL rebuild 和冷连接。

### 15.2 浏览器硬刷新

- 真实 Chrome、真实公网入口、禁用缓存。
- 直接 URL 至少 200 次。
- panda_quantflow 实际 iframe 另做至少一轮同口径验收。
- 计时终点是完整数据原子提交后的下一帧。
- P95 `<1000ms`，零错误、零 stale、零 fallback。

### 15.3 容量

- 1、5、10 个并发用户，各持续 5 分钟。
- cache miss 时只发生一个 snapshot rebuild。
- CPU、RSS、DB pool、线程池和隧道流量有界。
- 正常请求满足延迟预算；超载按设计返回 429/503。
- 高频 identity/no-cache 请求不能拖垮 health 或正常 gzip 请求。

### 15.4 冷路径

- 至少 50 个新 TCP/TLS 上下文，分别报告 DNS、TCP、TLS、TTFB 和 total。
- 经用户授权的后端重启后单列首请求结果。
- 冷启动结果不能隐藏，但不与稳定热路径混为同一诊断指标。

## 16. 发布顺序

1. 核对 `git status --short`、当前分支和生产基准；保存最后已知良好的应用 commit、launchd plist、远端 `nginx -T`、公网基线和回滚命令。
2. 在隔离 worktree 中实施后端 dashboard、批量快照、协议、gzip、缓存和测试；旧前端与旧 API 保持不变。
3. 本机验证数据 parity、事务、缓存、压缩、响应预算和并发。
4. 经用户明确授权后部署后端；确认旧前端仍可正常工作。
5. Nginx 只做加法：增加 dashboard 精确白名单、日志和经过容量验证的限流；`nginx -t` 后 reload。
6. 公网直接验证 dashboard 的 TLS、gzip、schema、数据 parity、路径矩阵和性能。
7. 切换前端到 dashboard，更新 JS cache-buster，移除 Google Fonts，验证 direct URL 和 iframe。
8. 运行至少 200 次浏览器 P95 和容量 soak，持续观察 stale、错误、隧道和 DB 指标。
9. 稳定后撤销旧 schemes/metrics/backtest 的公网白名单；FastAPI 内部路由继续保留。
10. 只有用户明确确认后，才能合并或覆盖到 `master` 并推送远程。

由于当前生产 FastAPI 直接从工作目录 serve 静态文件，不能在生产工作树中提前修改 frontend；否则未重启后端也可能让公网立即读到尚未部署 dashboard 的新 JS。

## 17. 回滚

回滚必须保证旧前端所需 API 先恢复：

1. 先恢复旧 schemes/metrics/backtest 公网白名单并通过状态码矩阵。
2. 再回退前端 HTML/JS/CSS 到最后已知良好版本。
3. 必要时回退后端 dashboard/gzip/cache 版本；旧 API 一直保留，因此不要求数据库回滚。
4. 最后恢复上一份 Nginx 配置并执行 `nginx -t`、reload。
5. 验证 direct URL、iframe、旧 API、health 和写接口仍为拒绝状态。

不能使用“摘站点、停隧道”作为本次性能发布的常规版本回滚，因为那会造成完全下线。

## 18. 风险与后续

### 18.1 当前发布内处理

- N+1 和混合 snapshot。
- actual 冲突 fail-closed。
- compact array 的严格协议校验。
- fallback/race/partial state。
- gzip q-value 和 Vary。
- Google Fonts 外部阻塞。
- Nginx docs/openapi 暴露和 dashboard 403。
- single-flight、响应预算、限流和观测。

### 18.2 后续单独处理

- 审计并迁移 weekly/monthly actual 唯一键到业务事实键。
- 多 worker 或多实例时引入共享 snapshot/cache/revision 机制。
- 数据规模超出当前预算时设计时间分片或按需历史加载；任何方案仍必须保持明细事实源。
- SSH tunnel 历史重启原因、host key 固定、BatchMode/ConnectTimeout 和故障恢复专项。
- 是否收敛公网 health 中的 service fingerprint。
- HTTP/2/HTTP/3 和进一步静态资产指纹化。

## 19. 完成定义

本设计只有同时满足以下条件才算完成：

1. 新 dashboard 数据与现有 canonical 数据逐行、逐指标等价。
2. 明细仍是前端唯一事实源，三日期、phase、flat 和 pending 语义完整。
3. dashboard 使用一个一致性只读 snapshot，查询不再按 scheme N+1。
4. compact 协议、gzip、缓存和 single-flight 均通过单元及集成测试。
5. 前端只用一个 dashboard GET，状态原子提交，刷新失败不清屏，性能验收零 fallback。
6. 公网精确白名单生效，docs/openapi/旧展示 API/写 API 均按最终矩阵拒绝。
7. 公网 direct URL 和真实 iframe 的端到端硬刷新 P95 小于 1 秒。
8. 容量、滥用、TLS、路径、冷连接和回滚验收完成。
9. 文档、部署脚本、访问探针和当前架构说明同步更新。
10. 发布到 `master` 前取得用户明确确认。
