# Factor Lab Subsecond Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `https://bond.finailab.cn/bond-factor-lab/` 从 10 秒级刷新优化为真实公网浏览器硬刷新到完整数据绘制的 nearest-rank P95 小于 1 秒，同时保持 live/backtest canonical 数据语义不变。

**Architecture:** 新增单连接一致性只读事务构建的紧凑 dashboard 快照 API，用 1 秒 TTL、single-flight、显式 stale LKG 和 gzip-6 控制后端与链路成本；前端改为带代次和取消能力的原子状态机；Nginx 改为精确白名单并分阶段撤销旧公网 API。所有语义先抽成纯函数并与旧 API 做等价回归，发布必须经过 200 次真实公网浏览器验收，且未经用户再次授权不得操作生产、`master` 或远程。

**Tech Stack:** Python 3.12、FastAPI/Starlette、SQLAlchemy、MySQL 8.0、原生 HTML/CSS/JavaScript、Nginx、pytest、Node.js VM、Chrome DevTools Protocol、launchd。

---

## 约束、验收口径与文件地图

实施时必须同时阅读并遵守：

- `docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md`
- `docs/architecture/PREDICTION_SEMANTICS.md`
- `docs/architecture/CODE_ARCHITECTURE.md`
- `docs/operations/README.md`
- 根目录 `AGENTS.md`

完成定义沿用设计文档第 19 节，尤其是：

- 浏览器缓存禁用，至少 200 次公网硬刷新，成功、失败和超时全部入样；
- `nearest-rank P95 < 1000ms`，零 5xx、零 schema 错误、零 legacy fallback，健康状态下零 stale；
- dashboard raw JSON `<= 1.5MB`，gzip-6 `<= 100KB`；
- 正常页面加载只发出一个 dashboard 数据请求；
- 新旧数据的 scheme、行数、方向和派生指标通过 canonical 等价检查；
- 不修改算法逻辑、预测写库路径或 actual 写库路径；
- 没有新的用户授权时，只能完成本地代码、测试、文档和部署材料，不能部署、切换 Nginx、合并/覆盖 `master` 或推送远程。

### 预计新增文件

- `backend/factor_lab_dashboard_semantics.py`：canonical 选择、actual 折叠、紧凑行编码和 payload 校验纯函数。
- `backend/factor_lab_dashboard.py`：单连接事务、批量 SQL、live/backtest 快照构建。
- `backend/dashboard_snapshot.py`：TTL、single-flight 和 last-known-good 快照。
- `backend/http_compression.py`：`Accept-Encoding` q-value 解析与 gzip 中间件封装。
- `tests/test_factor_lab_dashboard_semantics.py`
- `tests/test_factor_lab_dashboard.py`
- `tests/test_dashboard_snapshot.py`
- `tests/test_http_compression.py`
- `tests/test_factor_lab_dashboard_api.py`
- `tests/test_public_access_config.py`
- `scripts/benchmark_factor_lab_dashboard.py`：API 基准。
- `scripts/benchmark_factor_lab_browser.py`：Chrome/Edge CDP 浏览器基准。
- `tests/test_factor_lab_performance_tools.py`
- `docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md`

### 预计修改文件

- `backend/services.py`：复用 canonical 纯函数，避免新旧接口语义分叉。
- `backend/main.py`：dashboard 路由、启动预热、gzip、静态缓存策略。
- `frontend/aifin-shell.js`：单请求解码、请求代次、原子提交、stale/readiness/退避。
- `frontend/aifin-shell.css`：移除 Google Fonts，补充 stale/error 状态。
- `frontend/index.html`：动态状态与版本化静态资源 URL。
- `tests/test_frontend_factor_lab.py`
- `tests/test_frontend_static_cache.py`
- `tests/test_backend_serving.py`
- `deploy/nginx/bond-factor-lab.conf`
- `deploy/nginx/snippets/bond-proxy-headers.conf`
- `scripts/check_public_access.sh`
- `deploy/README.md`
- `docs/operations/README.md`
- `docs/architecture/CODE_ARCHITECTURE.md`
- `docs/product/GRAY_LAB_USER_MANUAL.md`。
- `docs/CURRENT_STATUS.md`：仅在授权发布且验收完成后更新当前运行状态。
- `docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md`：仅在授权发布且验收完成后更新状态与实测结果。

## Task 0: 建立隔离工作区并冻结基线

**Files:**

- Read: `AGENTS.md`
- Read: `docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md`
- Read: `docs/architecture/PREDICTION_SEMANTICS.md`
- Read: `docs/architecture/CODE_ARCHITECTURE.md`
- No modification in this task

- [ ] **Step 1: 使用隔离 worktree**

先调用 `superpowers:using-git-worktrees`。检查当前状态和分支，不得把 `outputs/`、用户草稿或其它工作区修改带入本任务：

```bash
git status --short
git branch --show-current
git branch --list
```

基于当前开发分支创建 `codex/factor-lab-subsecond-dashboard` 隔离 worktree；如果该分支或目录已存在，按 skill 的安全检查复用，不得强删。

- [ ] **Step 2: 记录本地测试基线**

```bash
python -m pytest -q
node --version
python -V
```

记录测试数、耗时和所有既有失败。若全量测试非绿，先判断是否与当前变更无关；不得把既有失败包装成优化成果。

- [ ] **Step 3: 记录只读公网基线**

```bash
curl --fail --silent --show-error --location \
  --output /dev/null \
  --write-out 'code=%{http_code} total=%{time_total} size=%{size_download}\n' \
  https://bond.finailab.cn/bond-factor-lab/

curl --fail --silent --show-error \
  --output /tmp/bond-factor-lab-backtest.json \
  --write-out 'code=%{http_code} total=%{time_total} size=%{size_download}\n' \
  https://bond.finailab.cn/bond-factor-lab/api/backtests/factor-lab
```

只允许 GET/HEAD 读操作。不得在本任务执行重启、配置 reload 或数据库写入。

- [ ] **Step 4: 提交点**

本任务不产生代码提交。把基线结果放入实施日志或最终验收报告；不要把 `/tmp` 或 `outputs/` 纳入 Git。

## Task 1: 抽取并锁定 canonical 数据语义

**Files:**

- Create: `backend/factor_lab_dashboard_semantics.py`
- Create: `tests/test_factor_lab_dashboard_semantics.py`
- Modify: `backend/services.py`
- Test: `tests/test_backend_api.py`

- [ ] **Step 1: 先写失败测试**

测试至少覆盖以下不可变行为：

测试名固定为 `test_weekly_h1_prefers_latest_predict_date_then_latest_id`、`test_future_predict_date_is_filtered_after_canonical_choice`、`test_duplicate_actuals_with_same_direction_collapse`、`test_conflicting_actuals_fail_closed`、`test_compact_detail_preserves_feature_date_and_pending_actual` 和 `test_payload_rejects_unknown_task_type_or_row_width`。

为 weekly `horizon=1` 准备同一个 canonical key 下不同 `predict_date`/`id` 的候选，明确断言 tie-break 顺序与现有 `_canonical_prediction_rows` 完全相同。准备一条 canonical 获胜但 `predict_date > display_until` 的记录，断言它在选择后才被过滤，不能让旧记录回填。

actual 测试必须包括：

```python
same = [
    {"target_date": "2026-07-01", "target_tenor": "CDB10Y", "target_rule": "close", "actual_direction": 1},
    {"target_date": "2026-07-01", "target_tenor": "CDB10Y", "target_rule": "close", "actual_direction": 1},
]
conflict = same + [
    {"target_date": "2026-07-01", "target_tenor": "CDB10Y", "target_rule": "close", "actual_direction": -1},
]
```

actual 事实键固定为 `(target_tenor, target_date, target_rule)`。同方向重复折叠为一个事实；冲突方向必须抛出 `DashboardDataError`，禁止 `MAX()` 或任意选一。

- [ ] **Step 2: 确认测试先红**

```bash
python -m pytest tests/test_factor_lab_dashboard_semantics.py -q
```

预期：因新模块或符号不存在而失败。若测试直接通过，说明测试没有覆盖新增边界，应先修正测试。

- [ ] **Step 3: 实现纯函数模块**

模块公开接口固定为：

```python
DASHBOARD_SCHEMA_VERSION = "factor-lab-dashboard-v1"
ROW_FIELDS = (
    "predict_date",
    "feature_date",
    "target_date",
    "prediction_phase",
    "predicted_direction",
    "actual_direction",
)
VALID_TASK_TYPES = {"T+1", "T+5", "weekly_point", "weekly_average", "monthly"}
VALID_LIVE_PREDICTION_PHASES = {"gray_live", "scheduled_live"}

class DashboardDataError(RuntimeError):
    """展示快照存在冲突或结构错误。"""

```

公开函数签名固定为 `choose_live_prediction_rows(rows, *, display_until)`、`collapse_actual_facts(rows, *, fact_name)`、`compact_detail_row(row, *, source)` 和 `validate_dashboard_payload(payload)`。

实现时从 `backend/services.py` 现有 helper 逐行迁移规则，不重新发明 key。`compact_detail_row` 必须按 `ROW_FIELDS` 固定顺序输出六元素数组，`actual_direction` 允许 `None`；日期统一为 ISO 字符串。live phase 只能是上述两个枚举；backtest phase 必须编码为 `None`，且 backtest actual 不允许为 `None`。

- [ ] **Step 4: 让旧服务复用同一选择函数**

把 `services.py` 的 live canonical 路径改为调用 `choose_live_prediction_rows`，保留旧 API 返回结构。不要在此任务优化 SQL、缓存或 API。

- [ ] **Step 5: 运行语义回归**

```bash
python -m pytest \
  tests/test_factor_lab_dashboard_semantics.py \
  tests/test_backend_api.py -q
git diff --check
```

- [ ] **Step 6: 提交**

```bash
git status --short
git add backend/factor_lab_dashboard_semantics.py backend/services.py \
  tests/test_factor_lab_dashboard_semantics.py
git diff --cached --check
git commit -m "refactor: centralize factor lab canonical semantics"
```

## Task 2: 用单连接一致性事务构建 live 快照

**Files:**

- Create: `backend/factor_lab_dashboard.py`
- Create: `tests/test_factor_lab_dashboard.py`
- Modify: `backend/factor_lab_dashboard_semantics.py`

- [ ] **Step 1: 建立 SQLite 测试夹具并先写失败测试**

夹具至少包含：active/paused Registry、五种 `task_type`、两个 target、同 key 多版本预测、未来 `predict_date`、pending actual、同方向重复 monthly actual、冲突 actual 场景。

测试公开入口：

```python
payload = build_factor_lab_dashboard(engine, captured_at=fixed_shanghai_time)
```

断言：

- 只出现 active composite `scheme_id`；
- 格子只按 `target_tenor + task_type`；
- 所有 row 保留三日期；
- 月份由 `target_date[:7]` 派生；
- future target 保留，future predict 过滤；
- live actual 根据 Registry `task_type` 和 target rule 选择 daily/weekly/monthly 表，不用 horizon 猜测；
- builder 全程只 checkout 一个 SQLAlchemy Connection。
- MySQL stub 断言 isolation option 和 `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY` 都发生在第一个业务 SELECT 前；SQLite SQL 捕获断言从不发送该 MySQL 语句。

- [ ] **Step 2: 确认测试先红**

```bash
python -m pytest tests/test_factor_lab_dashboard.py -q
```

- [ ] **Step 3: 实现事务上下文**

公开接口：

```python
@contextmanager
def dashboard_read_connection(engine):
    """返回一次 dashboard 构建独占的同一只读一致性连接。"""
```

builder 的公开签名固定为 `build_factor_lab_dashboard(engine, *, captured_at=None) -> dict`。

MySQL 路径必须在 transaction 开始前设置 `isolation_level="REPEATABLE READ"`，随后在同一连接执行：

```sql
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY
```

所有业务行先在事务内完整物化，再显式 rollback/close；canonical 选择、DTO 构建、JSON 序列化和 gzip 必须在事务结束后执行，避免延长 MVCC snapshot。SQLite 测试路径使用同一 `Connection.begin()`，不得模拟不存在的 MySQL SQL。异常路径也必须 rollback/close。

`captured_at` 未传时只调用一次 `datetime.now(ZoneInfo("Asia/Shanghai"))`，然后冻结 `display_until`；禁止每条查询或每个方案重复读取系统时间。

- [ ] **Step 4: 实现四次批量 live 读取**

在同一连接读取：

1. active `t_scheme_registry`；
2. `t_target_registry`；
3. 所有相关 base scheme/tenor 的 live predictions；
4. daily、weekly、monthly actual 通过一个 `UNION ALL` 批量查询，结果显式带 `actual_kind`。

不要调用会自行 `engine.connect()` 的 `list_schemes`、`scheme_metrics` 或 `_registry_scheme_row`。SQL 参数必须绑定，scheme/tenor 集合必须来自 active registry。SQL 捕获测试还要断言 dashboard 路径没有 INSERT、UPDATE、DELETE 或 DDL。

- [ ] **Step 5: 验证连接、事务和冲突失败关闭**

在测试中给 Engine 增加 checkout 计数器，断言成功与失败各只 checkout 一次；捕获 connection identity、事务 active 区间和 SQL verb，证明所有业务 SELECT 同连接、没有 DML/DDL、序列化发生在事务结束后。对 actual 冲突断言整个 builder 失败且没有半成品 payload。

```bash
python -m pytest tests/test_factor_lab_dashboard.py -q
git diff --check
```

- [ ] **Step 6: 提交**

```bash
git status --short
git add backend/factor_lab_dashboard.py \
  backend/factor_lab_dashboard_semantics.py \
  tests/test_factor_lab_dashboard.py
git diff --cached --check
git commit -m "feat: build consistent factor lab live snapshot"
```

## Task 3: 批量选择 latest backtest 并完成紧凑 DTO

**Files:**

- Modify: `backend/factor_lab_dashboard.py`
- Modify: `backend/factor_lab_dashboard_semantics.py`
- Modify: `backend/services.py`
- Modify: `tests/test_factor_lab_dashboard.py`
- Test: `tests/test_backend_api.py`

- [ ] **Step 1: 写 backtest 等价和查询预算失败测试**

夹具为每个 registry 方案准备：多个 run、不同 data source、不同 scope、相同 created time 下不同 run ID、完全没有成功 run，以及选中的成功 run 没有 detail 的场景。断言选择规则与旧 `/api/backtests/factor-lab` 一致；没有成功 run 时 `backtest=null`，已有选中 run 但 detail 缺失属于损坏并整包 fail-closed，不能伪装成“没有回测”。

增加 SQL 事件监听计数，只统计 snapshot 事务内的 `SELECT`：

```python
assert select_count <= 6
```

增加新旧投影等价测试：把旧 API 结果标准化成 `(registry_scheme_id, target_date, predicted_direction, actual_direction)`，把 dashboard 解码成相同 tuple 集合，断言完全相等；另外分别断言 scheme 数、live 行数、backtest 行数。

- [ ] **Step 2: 确认测试先红**

```bash
python -m pytest tests/test_factor_lab_dashboard.py -q
```

- [ ] **Step 3: 实现两次 backtest 批量读取**

同一事务内增加：

1. 一次查询读出所有 active registry 作用域的 run 候选；
2. 在 Python 中按现有规则选 latest run IDs，再用 expanding bind parameter 一次读完 detail。

把 runtime default data source、latest-success、scope rank 和 tie-break 从 `services.py` 抽为 `choose_latest_backtest_runs(run_rows, registry_rows)` 纯函数，旧 `/api/backtests/factor-lab` 和新 builder 都调用它。禁止保留两份“看起来相似”的选择器。

禁止 per-scheme、per-run 查询。若 selected latest-success run 没 detail，必须抛 `DashboardDataError`，不能静默退到旧 run；只有没有任何可选 run 时才返回 `backtest=null`。

- [ ] **Step 4: 形成版本化 payload**

顶层至少包含：

```json
{
  "schema_version": "factor-lab-dashboard-v1",
  "snapshot_id": "opaque-id",
  "generated_at": "2026-07-22T12:00:00+08:00",
  "display_until": "2026-07-22",
  "stale": false,
  "snapshot_age_ms": 0,
  "row_fields": [
    "predict_date",
    "feature_date",
    "target_date",
    "prediction_phase",
    "predicted_direction",
    "actual_direction"
  ],
  "target_labels": {},
  "schemes": []
}
```

`snapshot_id` 每次成功 rebuild 生成一次不透明 ID；不能包含内部路径或算法秘密。`display_until` 是 Task 2 冻结的上海日期。每个 scheme 同时包含 Registry 展示元数据、target/task identity、live rows 和 latest backtest rows；没有 backtest 时字段值必须为 `null`，没有 live 时必须为空数组，不能省略字段。紧凑化只允许去除重复 key，不得删除行、三日期、phase、方向或 pending actual。

validator 还必须证明：composite ID 精确等于 `{base_scheme_id}__h{horizon}__{target_tenor}`、`deployed_at` 非空、scheme ID 唯一、每个 source 内 canonical point 唯一、backtest actual 非空。输出固定按 scheme、source、target_date、predict_date 排序；前端仍显式排序，不能依赖后端排序稳定性。

- [ ] **Step 5: 加入规模保护并通过完整语义测试**

builder 结束时校验：

```python
MAX_DETAIL_ROWS = 20_000
MAX_RAW_JSON_BYTES = 1_500_000
MAX_GZIP_JSON_BYTES = 100_000
```

事务结束后用与响应一致的 `json.dumps(payload, ensure_ascii=False, separators=(",", ":"))` 序列化，并以 `gzip.compress(raw_json_bytes, compresslevel=6)` 检查压缩预算；记录 raw/gzip bytes 供日志和 header 使用。超过任一预算抛出 `DashboardDataError`，不能返回截断数据。预算压缩只在每次 single-flight rebuild 执行一次，不在每个 waiter 或 cache HIT 重复执行。

```bash
python -m pytest \
  tests/test_factor_lab_dashboard.py \
  tests/test_factor_lab_dashboard_semantics.py \
  tests/test_backend_api.py -q
git diff --check
```

- [ ] **Step 6: 提交**

```bash
git status --short
git add backend/factor_lab_dashboard.py \
  backend/factor_lab_dashboard_semantics.py \
  backend/services.py tests/test_factor_lab_dashboard.py
git diff --cached --check
git commit -m "feat: add compact factor lab dashboard payload"
```

## Task 4: 实现 1 秒 TTL、single-flight 与显式 stale LKG

**Files:**

- Create: `backend/dashboard_snapshot.py`
- Create: `tests/test_dashboard_snapshot.py`

- [ ] **Step 1: 先写并发和时钟失败测试**

使用可注入的 monotonic clock 和受控 builder，覆盖：

测试名固定为 `test_fresh_snapshot_is_reused_within_one_second`、`test_only_one_builder_runs_under_concurrency`、`test_waiter_receives_new_snapshot_within_deadline`、`test_waiter_gets_visible_stale_lkg_after_deadline`、`test_builder_failure_returns_visible_stale_lkg`、`test_first_builder_failure_raises_without_fake_empty_snapshot` 和 `test_wall_clock_change_does_not_change_ttl`。

至少启动 20 个线程同时请求过期快照，断言 builder 调用次数为 1；不能依赖 `sleep` 猜并发，应使用 `threading.Event`/`Barrier` 控制时序。

- [ ] **Step 2: 确认测试先红**

```bash
python -m pytest tests/test_dashboard_snapshot.py -q
```

- [ ] **Step 3: 实现 SnapshotStore**

公开结构：

```python
@dataclass(frozen=True)
class SnapshotResult:
    payload: dict
    cache_status: Literal["HIT", "MISS", "STALE"]
    age_seconds: float
    build_seconds: float | None

class SnapshotUnavailable(RuntimeError):
    """首份 dashboard 快照不可用。"""
```

`DashboardSnapshotStore` 的构造签名固定为 `(builder, *, ttl_seconds=1.0, wait_timeout_seconds=0.6, monotonic=time.monotonic)`，并公开 `get() -> SnapshotResult` 与 `prewarm() -> SnapshotResult`。

实现使用 `threading.Condition`，共享 `_building` 状态。新鲜快照直接 HIT；首个过期请求成为 builder；其它请求最多等 0.6 秒。存在 LKG 时，等待超时或 build 失败返回 STALE；没有 LKG 时抛 `SnapshotUnavailable`。single-flight 必须在成功、异常和取消路径释放，等待超时返回 stale 时唯一后台 build 仍继续完成。

payload 在成功 build 时递归冻结一次（dict 使用不可变 dict subclass，list 转 tuple），route 每次只浅拷贝顶层来写动态 `stale`/`snapshot_age_ms`；禁止每次请求深拷贝 1.5MB 明细，也禁止让调用者修改缓存内部对象。验证标准 JSON encoder 能正确编码冻结结构。

- [ ] **Step 4: 运行测试与泄漏检查**

```bash
python -m pytest tests/test_dashboard_snapshot.py -q
python -m pytest tests/test_dashboard_snapshot.py -q -x --count=20
git diff --check
```

若环境没有 `pytest-repeat`，用以下命令替代第二条：

```bash
for i in $(seq 1 20); do python -m pytest tests/test_dashboard_snapshot.py -q || exit 1; done
```

- [ ] **Step 5: 提交**

```bash
git status --short
git add backend/dashboard_snapshot.py tests/test_dashboard_snapshot.py
git diff --cached --check
git commit -m "feat: add single flight dashboard snapshot cache"
```

## Task 5: 实现兼容 q-value 的 gzip-6

**Files:**

- Create: `backend/http_compression.py`
- Create: `tests/test_http_compression.py`

- [ ] **Step 1: 先写协议失败测试**

覆盖以下请求头：

```text
gzip
br, gzip;q=0.8
gzip;q=0
*;q=0.5
gzip;q=0, *;q=1
identity
空头、非法 q、重复 gzip token、大小写和空格
```

关键断言：显式 `gzip;q=0` 必须覆盖 wildcard；identity 路径仍有 `Vary: Accept-Encoding`；已有 `Vary` token 必须合并而非覆盖/重复；已带 `Content-Encoding` 的响应不能二次压缩；小于阈值不压缩；gzip 响应只含一层 encoding、Content-Length 正确，可解压且 JSON 字节完全一致。

- [ ] **Step 2: 确认测试先红**

```bash
python -m pytest tests/test_http_compression.py -q
```

- [ ] **Step 3: 实现 parser 和中间件封装**

公开函数签名固定为 `accepts_gzip(header_value: str | None) -> bool`；`QAwareGZipMiddleware` 只在客户端允许 gzip 时调用 Starlette `GZipMiddleware`。

实际压缩复用 Starlette `GZipMiddleware`，参数固定：

```python
minimum_size=500
compresslevel=6
```

不要手写流式 gzip 状态机。parser 对无效 q-value 按不可接受处理，不能抛 500。

- [ ] **Step 4: 运行协议测试**

```bash
python -m pytest tests/test_http_compression.py -q
git diff --check
```

- [ ] **Step 5: 提交**

```bash
git status --short
git add backend/http_compression.py tests/test_http_compression.py
git diff --cached --check
git commit -m "feat: add q aware gzip compression"
```

## Task 6: 接入 dashboard API、预热和静态缓存策略

**Files:**

- Modify: `backend/main.py`
- Create: `tests/test_factor_lab_dashboard_api.py`
- Modify: `tests/test_backend_serving.py`
- Modify: `tests/test_frontend_static_cache.py`
- Modify: `backend/factor_lab_dashboard.py`
- Modify: `backend/dashboard_snapshot.py`

- [ ] **Step 1: 先写 raw ASGI API 失败测试**

当前环境没有 `httpx` 时，用直接 ASGI scope/receive/send helper 测试，不能为了一个测试新增生产依赖。覆盖：

- GET `/api/factor-lab/dashboard` 返回 schema、`Cache-Control: no-store`、`Vary: Accept-Encoding`；
- HEAD 返回与对应 GET 相同的状态、Content-Type/Content-Encoding/Content-Length/cache/stale header，ASGI body 为空；
- 任何 query string 返回 400；
- 首次构建失败且无 LKG 返回 503；
- stale 返回 200，JSON 顶层显式 `stale=true`、`snapshot_age_ms`，header 使用稳定 warning code；
- gzip 与 identity body 解码后相等；
- versioned JS/CSS 和两个 SVG 在客户端允许时于 FastAPI 侧压缩，且一次解压成功、第二次解压失败；
- endpoint header 包含 `X-Request-ID`、`X-Dashboard-Snapshot-ID`、`X-Dashboard-Cache`、`X-Dashboard-Snapshot-Age`、`Server-Timing`；
- 结构化日志包含 request ID、snapshot ID、cache state、waiter count、DB/build/serialization 时间、scheme/live/backtest 行数和 raw bytes，但不含完整 scheme 列表、SQL 参数或数据库连接信息；
- 模块 import 不连接数据库、不构建快照。

现有 `/api/health` 兼容保留 HTTP 200 和 process status，但增加 `dashboard_snapshot.status=ready|degraded` 及不含内部异常文本的稳定 error code；prewarm 失败且无 LKG 时必须显示 degraded。公网检查脚本把 degraded 视为 dashboard 发布失败。

- [ ] **Step 2: 先写静态资源缓存失败测试**

缓存策略固定为：

```python
INDEX_CACHE_CONTROL = "no-cache, must-revalidate"
VERSIONED_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
UNVERSIONED_ASSET_CACHE_CONTROL = "no-cache, must-revalidate"
```

只有 URL query 含当前 HTML 引用的非空版本 token 的 CSS/JS 才 immutable；无版本资源必须 revalidate，不能让旧 JS 长期卡住。测试 index、versioned CSS/JS、unversioned CSS/JS、未知静态文件。

- [ ] **Step 3: 确认测试先红**

```bash
python -m pytest \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_backend_serving.py \
  tests/test_frontend_static_cache.py -q
```

- [ ] **Step 4: 在 FastAPI 接入组件**

在 `backend/main.py` 中：

1. 注册 `QAwareGZipMiddleware`；
2. 模块级只创建带 lazy builder 的 `DashboardSnapshotStore`，不得 import 时读取 DB；
3. startup 的 Registry 同步完成后调用 `prewarm()`；预热失败记录结构化 error，并更新 health 的 dashboard 子状态；
4. 新增 GET/HEAD `/api/factor-lab/dashboard`；
5. query 非空直接 400；
6. 由 route 设置 cache/stale/timing headers；
7. 采用 Nginx 覆盖传入的 `X-Request-ID`，本地未提供时生成 UUID；响应回传 request/snapshot ID；
8. 用 `time.perf_counter()` 分段记录 DB read、canonical build、serialization，写入 `Server-Timing` 和结构化日志；
9. 静态文件类按上述三类 cache policy 返回 header。

route 在响应顶层副本中更新：

```json
"stale": false,
"snapshot_age_ms": 12
```

动态字段不能污染 SnapshotStore 的 canonical payload。stale 时同时返回 `X-Dashboard-Warning: stale-last-known-good`；fresh 时不得残留 warning。

- [ ] **Step 5: 验证 startup 和本地 HTTP 行为**

```bash
python -m pytest \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_backend_serving.py \
  tests/test_frontend_static_cache.py \
  tests/test_http_compression.py \
  tests/test_dashboard_snapshot.py \
  tests/test_factor_lab_dashboard.py -q
```

用服务环境启动临时端口，不使用生产 launchd：

```bash
conda run -n bond_factor_lab_service \
  uvicorn backend.main:app --host 127.0.0.1 --port 18101
```

在另一终端验证：

```bash
curl --fail --silent --show-error --dump-header /tmp/dashboard.headers \
  --output /tmp/dashboard.json \
  http://127.0.0.1:18101/api/factor-lab/dashboard

curl --fail --silent --show-error --compressed \
  --dump-header /tmp/dashboard-gzip.headers \
  --output /tmp/dashboard-gzip.json \
  http://127.0.0.1:18101/api/factor-lab/dashboard
```

检查 raw/gzip 预算和 payload 校验；完成后正常中断临时 Uvicorn，不能 kill 生产 PID。

- [ ] **Step 6: 提交**

```bash
git status --short
git add backend/main.py backend/factor_lab_dashboard.py \
  backend/dashboard_snapshot.py tests/test_factor_lab_dashboard_api.py \
  tests/test_backend_serving.py tests/test_frontend_static_cache.py
git diff --cached --check
git commit -m "feat: expose prewarmed factor lab dashboard api"
```

## Task 7: 实现前端 happy-path 解码与等价聚合

**Files:**

- Modify: `frontend/aifin-shell.js`
- Modify: `tests/test_frontend_factor_lab.py`

- [ ] **Step 1: 扩展 Node VM 测试宿主**

在 fake DOM 中补齐：

```javascript
document.documentElement.dataset = {};
document.visibilityState = "visible";
document.addEventListener = function (name, handler) { /* retain handler */ };
window.AbortController = FakeAbortController;
window.performance = { now: fakeNow };
window.requestAnimationFrame = queueRaf;
window.setTimeout = queueTimeout;
window.clearTimeout = cancelTimeout;
window.setInterval = queueInterval;
window.clearInterval = cancelInterval;
```

让 `fetch` 记录 URL、signal 和调用顺序；rAF、timeout 和 interval 都由测试显式推进，不能同步执行导致递归或掩盖 readiness/退避时序。

- [ ] **Step 2: 先写单请求和严格 schema 失败测试**

happy path 断言只请求：

公网子路径测试预期 `/bond-factor-lab/api/factor-lab/dashboard`，本地根路径测试预期 `/api/factor-lab/dashboard`；URL 必须由当前页面 path 推导，不能硬编码域名。

并且不请求 `/api/schemes`、`/api/metrics/`、`/api/backtests/factor-lab`。

corruption table 至少包含：未知 `schema_version`、`row_fields` 顺序变化/重复、row 宽度错误、非法 task type、非法日期、非法 direction、重复或格式不符的 composite scheme ID、缺失 `deployed_at`/target/task identity、未知 live phase、非 null backtest phase、null backtest actual、同一 source 重复 canonical point。所有 corruption 都必须失败且不部分提交 UI。

- [ ] **Step 3: 确认测试先红**

```bash
python -m pytest tests/test_frontend_factor_lab.py -q
```

- [ ] **Step 4: 实现 decoder 和直接 UI 模型构建**

新增内部函数并暴露到现有 test hook：

```javascript
decodeDashboardPayload(payload)
buildFactorLabViewModel(decoded)
```

decoder 先严格核对 schema 和 `row_fields`，再按固定 index 解码。view model 直接按 source/live-backtest、`target_date.slice(0, 7)` 和 grid identity 分组；保留 `feature_date`、flat direction `0`、pending actual `null`。月度 cutoff 和指标公式复用现有纯函数。

禁止把紧凑 payload 膨胀成旧三 API 的伪响应再喂给旧 merge；这会保留重复解析和竞态。可以复用指标/绘制纯函数，但中间模型必须一次构建。

- [ ] **Step 5: 新旧结果做 fixture 等价检查**

对同一 fixture，旧 merge 输出与新 view model 标准化后必须相等：方案格子、月份列表、每月预测/actual 数、正确数、accuracy、累计 accuracy、pending 数和 drawer 行。专门覆盖跨 source 同一 target（日/周允许）、月频 live cutoff 裁剪、flat `0` 不进入指标分母但仍计入样本、pending `null` 不被当作 flat。

```bash
python -m pytest tests/test_frontend_factor_lab.py -q
git diff --check
```

- [ ] **Step 6: 提交**

```bash
git status --short
git add frontend/aifin-shell.js tests/test_frontend_factor_lab.py
git diff --cached --check
git commit -m "feat: decode compact factor lab dashboard payload"
```

## Task 8: 完成前端原子状态机、可见 stale 和资源瘦身

**Files:**

- Modify: `frontend/aifin-shell.js`
- Modify: `frontend/aifin-shell.css`
- Modify: `frontend/index.html`
- Modify: `tests/test_frontend_factor_lab.py`
- Modify: `tests/test_frontend_static_cache.py`

- [ ] **Step 1: 先写竞态、fallback 和 stale 失败测试**

状态机测试必须覆盖：

- refresh N+1 取消 N；即使 N 最后 resolve 也不能覆盖 N+1；
- 被新代次主动 abort 的旧请求不能增加 failure count、显示 stale/error 或安排退避；
- 404 或明确 unsupported（501 且 JSON `detail.code="dashboard_not_supported"`）才允许一次 legacy capability fallback；
- 403、429、500、503、timeout、network、schema error 不允许 fallback 风暴；
- fallback capability 一旦确定，本页面生命周期内不反复探测；
- 刷新失败时保留最后成功 view model，显示 stale/error 和 age；
- 首屏失败且没有 LKG 时显示 error empty state，而不是 OnLine；
- hidden 时暂停 interval，visible 后立即单次刷新；
- 连续失败使用有上限的指数退避，成功后复位；
- drawer 打开时 refresh 保持选中 scheme/month，若对象消失才安全关闭；
- `window.__factorLabReady` 只在 DOM 原子提交后的下一次 rAF 变为带时间戳的对象。
- 每次新加载开始先把 `window.__factorLabReady` 置为 `null`；ready 对象必须含当前 `seq` 和 `snapshotId`，性能探针不能误读上一轮标记。

- [ ] **Step 2: 确认测试先红**

```bash
python -m pytest tests/test_frontend_factor_lab.py tests/test_frontend_static_cache.py -q
```

- [ ] **Step 3: 实现请求代次与原子提交**

状态至少包含：

```javascript
{
  loadSeq: 0,
  controller: null,
  capability: "unknown",
  committedViewModel: null,
  stale: false,
  consecutiveFailures: 0,
  nextRefreshAt: 0
}
```

每次刷新先递增 `loadSeq` 并 abort 上次 controller；fetch、decode、view model 全部在局部变量完成，只有 `seq === state.loadSeq` 才一次替换 `committedViewModel` 并 render。旧请求的 catch/finally 也不能修改新请求状态。

- [ ] **Step 4: 实现 fallback matrix、退避和 readiness**

legacy fallback 仅用于滚动发布兼容，最终 Nginx 收口后正常路径不应触发。遇到 404/explicit unsupported 之外的错误，保留 LKG 并显示错误；没有 LKG 显示不可用。

退避建议固定并测试：`1s, 2s, 5s, 10s, 30s`，最大 30 秒；页面 hidden 时不启动新 timer。成功 commit 后：

```javascript
requestAnimationFrame(() => {
  window.__factorLabReady = {
    seq,
    snapshotId: candidate.snapshotId,
    committedAt: performance.now(),
    stale: candidate.stale,
    source: "dashboard"
  };
});
```

健康 ready 状态的自动刷新周期保持 60 秒；聚合指标按 `(snapshot_id, scheme_id, month_range, source)` 缓存，snapshot 切换时清理旧缓存，避免一次 render/drawer 重复扫描全部明细。

- [ ] **Step 5: 移除外部字体并动态呈现状态**

删除 `frontend/aifin-shell.css` 的 Google Fonts `@import`，改用系统字体栈。`index.html` 不再硬编码 `OnLine`，初始状态为 Loading；JS 根据 fresh/stale/error 切换可访问文本和 class。

将 HTML 中 CSS/JS 版本统一更新为 `20260722a`，同步更新静态缓存测试。

- [ ] **Step 6: 运行前端回归**

```bash
python -m pytest \
  tests/test_frontend_factor_lab.py \
  tests/test_frontend_static_cache.py \
  tests/test_backend_serving.py -q
rg -n "fonts.googleapis.com|fonts.gstatic.com|@import" frontend
git diff --check
```

`rg` 预期无匹配。

- [ ] **Step 7: 提交**

```bash
git status --short
git add frontend/aifin-shell.js frontend/aifin-shell.css frontend/index.html \
  tests/test_frontend_factor_lab.py tests/test_frontend_static_cache.py
git diff --cached --check
git commit -m "feat: make factor lab refresh atomic and resilient"
```

## Task 9: 收紧 Nginx 白名单并准备双阶段发布矩阵

**Files:**

- Modify: `deploy/nginx/bond-factor-lab.conf`
- Modify: `deploy/nginx/snippets/bond-proxy-headers.conf`
- Modify: `scripts/check_public_access.sh`
- Create: `tests/test_public_access_config.py`
- Modify: `deploy/README.md`

- [ ] **Step 1: 先写静态配置失败测试**

测试读取 Nginx 配置文本并验证 location 优先级和发布阶段约束。至少断言：

- 精确允许 `/bond-factor-lab/`、必要的 `/index.html`、`aifin-shell.css`、`aifin-shell.js`、`assets/aifin-lab-icon.svg`、`assets/aifin-lab-logo.svg`、`/api/health` 和 `/api/factor-lab/dashboard`；
- `/docs`、`/openapi.json`、未知 `/api/*`、路径穿越/编码绕过没有落入 broad proxy；
- dashboard location 在 deny location 之前且使用精确匹配；
- 存在 request timing access log；
- 存在 rate/connection limit，拒绝状态为 429；
- HTTP 跳转固定为 `https://bond.finailab.cn$request_uri`，无尾斜杠跳转固定为 `https://bond.finailab.cn/bond-factor-lab/`，不能由不可信 Host header 拼接；
- 对原始 `$request_uri` 中的双斜杠、编码 slash/backslash、编码 dot segment 先返回 403，避免 Nginx normalize 后命中白名单；
- rollout 配置暂时保留旧 schemes/metrics/backtest API；最终收口由明确标记的配置段或补丁步骤撤销，不能在新前端上线前先断旧前端。

- [ ] **Step 2: 确认测试先红**

```bash
python -m pytest tests/test_public_access_config.py -q
```

- [ ] **Step 3: 修改 Nginx 部署模板**

在 `http` 级配置样例加入：

```nginx
limit_req_zone $binary_remote_addr zone=bond_factor_dashboard:10m rate=2r/s;
limit_conn_zone $binary_remote_addr zone=bond_factor_conn:10m;
log_format bond_factor_timing '$request_id $remote_addr $request $status '
                              '$request_time $upstream_response_time '
                              '$body_bytes_sent $gzip_ratio';
access_log /var/log/nginx/bond-factor-lab-access.log bond_factor_timing;
```

dashboard 精确 location 使用：

```nginx
limit_req zone=bond_factor_dashboard burst=20 nodelay;
limit_req_status 429;
limit_conn bond_factor_conn 20;
proxy_connect_timeout 1s;
proxy_read_timeout 3s;
```

保留 `proxy_buffering on`，后端负责 gzip，Nginx 不得解压/二次压缩。在公共 proxy snippet 显式设置 `Accept-Encoding $http_accept_encoding` 和 `X-Request-ID $request_id`，保留 `Vary`。上述页面、两个静态文件、两个 SVG 和 health 必须逐项白名单；其它 `/bond-factor-lab/api/`、`/docs`、`/redoc`、`/openapi.json` 和未知路径 fail closed，大小写、尾斜杠和编码变体也不能命中宽泛代理。

在 server 入口用只执行 `return 403` 的安全 guard 检查原始 `$request_uri` 中的 `//`、`%2f`、`%5c`、`%2e`（大小写不敏感），再进入 location 匹配。HTTP server 和 `/bond-factor-lab` 补斜杠响应使用固定域名 HTTPS Location，不使用 `$host`。

本仓库模板必须清楚分为：

- rollout：新 dashboard + 旧三个展示 API 同时可达；
- final：仅 dashboard 展示 API 可达。

不能要求操作者在现场凭记忆编辑未记录的 location。

- [ ] **Step 4: 扩展公网检查脚本**

脚本接口固定：

```bash
scripts/check_public_access.sh --mode rollout https://bond.finailab.cn
scripts/check_public_access.sh --mode final https://bond.finailab.cn
```

移除 `curl -k`，TLS 证书验证失败必须失败。检查：

- HTTP 到 HTTPS redirect；
- 页面、版本资源、dashboard GET/HEAD，以及 health 的 `dashboard_snapshot.status=ready`；
- gzip、identity、`gzip;q=0`、`Vary`；
- `/docs`、`/redoc`、`/openapi.json`、未知 API、双斜线、`..`、百分号编码绕过（curl 使用 `--path-as-is`，确保客户端不先规范化）；
- rollout 旧 API 200，final 旧 API 被拒；
- 输出每项 code/size/time，不打印敏感 header 或内部连接信息。
- 使用固定 `User-Agent: bond-factor-lab-access-check/1.0`，便于兼容窗口从真实旧页面流量中排除探针。

- [ ] **Step 5: 文档纠偏并验证模板**

删除 `deploy/README.md` 中“公网改动不涉及后端/前端”和把历史 PRD 当当前规范的陈述，改为链接当前性能运行手册（Task 10 创建）。

```bash
python -m pytest tests/test_public_access_config.py -q
shellcheck scripts/check_public_access.sh
git diff --check
```

仓库里的 site 文件不是包含 `events/http` 的完整 `nginx.conf`，不得用 `nginx -t -c deploy/nginx/bond-factor-lab.conf` 产生误导性失败。`nginx -t` 必须在入口机把 rollout 文件纳入真实完整配置后执行，见 Task 12。若本机没有 `shellcheck`，记录为发布前强制检查，不能声称已验证；pytest 静态检查仍必须通过。

- [ ] **Step 6: 提交**

```bash
git status --short
git add deploy/nginx/bond-factor-lab.conf \
  deploy/nginx/snippets/bond-proxy-headers.conf deploy/README.md \
  scripts/check_public_access.sh tests/test_public_access_config.py
git diff --cached --check
git commit -m "ops: harden factor lab public access policy"
```

## Task 10: 建立 API 与真实 Chromium 浏览器性能探针

**Files:**

- Create: `scripts/benchmark_factor_lab_dashboard.py`
- Create: `scripts/benchmark_factor_lab_browser.py`
- Create: `tests/test_factor_lab_performance_tools.py`
- Create: `docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md`
- Modify: `docs/operations/README.md`
- Modify: `docs/architecture/CODE_ARCHITECTURE.md`

- [ ] **Step 1: 先写统计和失败样本测试**

公开纯函数：

公开函数固定为 `nearest_rank(values: list[float], percentile: float) -> float`（返回 `ceil(p*n)` 对应的有序样本，不插值）和 `summarize_attempts(attempts: list[Attempt]) -> dict`。

至少断言 200 个有序样本的 P95 是第 190 个；失败/超时尝试留在总样本并让验收失败，不能从 latency 数组静默删除。少于 200 次的公网验收模式必须非零退出。

- [ ] **Step 2: 确认测试先红**

```bash
python -m pytest tests/test_factor_lab_performance_tools.py -q
```

- [ ] **Step 3: 实现 API benchmark**

只用标准库 `http.client`/`socket`/`ssl`/`gzip`，通过小型 timed connection wrapper 真实记录 DNS、TCP、TLS、TTFB 和 total，参数至少包含：

```text
--url
--attempts
--timeout
--disable-keepalive
--minimum-attempt-period-seconds
--output-json
```

每次记录 status、DNS/TCP/TLS/TTFB/total、raw/gzip bytes、cache/stale headers、schema validity。复用连接时没有重复发生的阶段设为 `null` 并在元数据标明，不把首次连接成本摊平伪造；`--disable-keepalive` 必须为每次尝试新建连接。公网顺序验收使用 `--minimum-attempt-period-seconds 0.5` 与入口速率匹配，节流等待不计入 latency 但写入报告。API 与浏览器探针分别使用固定且可识别的 `bond-factor-lab-api-benchmark/1.0`、`bond-factor-lab-browser-benchmark/1.0` User-Agent。输出 nearest-rank P50/P95/P99 和失败清单。

- [ ] **Step 4: 实现通用 Chrome DevTools Protocol browser benchmark**

脚本接受 `--browser-binary`，优先使用 Google Chrome；本机开发 smoke 可使用当前已安装的 Microsoft Edge（两者均通过 CDP 驱动）：

```text
/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge
```

Python 脚本启动独立临时 profile、随机本地 debug port，并使用环境已有 `websockets` 连接 CDP；不得依赖未安装的 Playwright/Selenium。每个 attempt：

1. `Network.setCacheDisabled(true)`；
2. `Network.setBypassServiceWorker(true)` 并在每次 attempt 前 `Network.clearBrowserCache`；
3. 创建/重置 page；
4. 从 `Page.navigate` 前取 monotonic 起点；
5. 等待本次导航对应 frame 的 `window.__factorLabReady`；
6. 收集 dashboard/legacy 请求、console error、page error、ready.stale/source；
7. timeout/崩溃也写入 attempt；
8. 完成后终止本脚本启动的浏览器进程并删除临时 profile。

参数至少包含：

```text
--url
--attempts 200
--timeout-seconds
--output-json
--browser-binary
--ready-frame-url-substring
--minimum-attempt-period-seconds
--concurrency
--duration-seconds
--refresh-interval-seconds
```

脚本订阅 execution-context/frame 事件；direct URL 在主 frame 找 ready，iframe 模式在 URL 含 `--ready-frame-url-substring` 的目标 frame 找 `window.__factorLabReady`，禁止只等待父页面 load。`--attempts` 用于顺序 SLO；`--minimum-attempt-period-seconds=0.5` 从每次 attempt 起点节流，避免 200 次验收本身超过 2r/s 入口预算，等待时间不计入该 attempt latency但必须写入报告。`--concurrency + --duration-seconds + --refresh-interval-seconds` 用于 1/5/10 用户 soak，两个模式互斥并在参数测试中锁定。

验收成功条件硬编码为：attempts `>= 200`、P95 `< 1000ms`、零失败、零 legacy request/fallback、零 stale、零 schema/console/page error。报告要记录时间、时区、探测位置、浏览器产品与完整版本、设备、网络以及连接复用条件。Edge smoke 不能被标记为设计约定的 Chrome 正式验收；正式验收必须使用 Google Chrome，或在报告中记录用户对 Chromium Edge 替代口径的明确批准。

- [ ] **Step 5: 编写当前运行手册**

`PUBLIC_FACTOR_LAB_PERFORMANCE.md` 包含：

- dashboard 架构与 header 含义；
- 本地 API、rollout、公网浏览器三类命令；
- SLO 与配套容量预算；
- cold connection、backend cold start、iframe parent 分开报告；
- stale/503/429/actual conflict 的排障顺序；
- rollout/final/rollback 操作；
- 日志字段和不应记录的敏感信息；
- 明确生产发布授权边界。

把它加入 `docs/operations/README.md`，并在 `CODE_ARCHITECTURE.md` 的 backend 展示路径补充 dashboard snapshot，而不改变四条架构不变量。

- [ ] **Step 6: 验证工具**

```bash
python -m pytest tests/test_factor_lab_performance_tools.py -q
python scripts/benchmark_factor_lab_dashboard.py \
  --url http://127.0.0.1:18101/api/factor-lab/dashboard \
  --attempts 20 --timeout 5 \
  --output-json /tmp/factor-lab-api-benchmark.json

python scripts/benchmark_factor_lab_browser.py \
  --url http://127.0.0.1:18101/ \
  --attempts 5 --timeout-seconds 5 \
  --browser-binary '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge' \
  --output-json /tmp/factor-lab-browser-smoke.json
git diff --check
```

本地 5 次 browser smoke 只验证工具和页面协议，不得冒充公网 200 次 SLO 验收。

- [ ] **Step 7: 提交**

```bash
git status --short
git add scripts/benchmark_factor_lab_dashboard.py \
  scripts/benchmark_factor_lab_browser.py \
  tests/test_factor_lab_performance_tools.py \
  docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md \
  docs/operations/README.md docs/architecture/CODE_ARCHITECTURE.md
git diff --cached --check
git commit -m "test: add factor lab performance acceptance probes"
```

## Task 11: 完成本地集成、审查与发布前证据包

**Files:**

- Modify if needed: files from Tasks 1–10 only
- Read: `docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md`
- No production changes

- [ ] **Step 1: 运行聚焦测试**

```bash
python -m pytest \
  tests/test_factor_lab_dashboard_semantics.py \
  tests/test_factor_lab_dashboard.py \
  tests/test_dashboard_snapshot.py \
  tests/test_http_compression.py \
  tests/test_factor_lab_dashboard_api.py \
  tests/test_frontend_factor_lab.py \
  tests/test_frontend_static_cache.py \
  tests/test_backend_serving.py \
  tests/test_public_access_config.py \
  tests/test_factor_lab_performance_tools.py -q
```

- [ ] **Step 2: 运行全量测试和静态检查**

```bash
python -m pytest -q
python -m compileall -q backend scripts tests
git diff --check
rg -n "fonts.googleapis.com|fonts.gstatic.com|@import" frontend
rg -n "TODO|TBD|implement later|fill in" \
  backend frontend tests scripts deploy docs/operations
```

Google Fonts 查询预期无匹配。TODO/TBD 查询只允许与本任务无关、在基线已存在且记录过的条目。

- [ ] **Step 3: 运行本地完整服务与 200 次 API 基准**

在非生产端口启动服务后执行：

```bash
python scripts/benchmark_factor_lab_dashboard.py \
  --url http://127.0.0.1:18101/api/factor-lab/dashboard \
  --attempts 200 --timeout 5 \
  --output-json /tmp/factor-lab-api-200.json
```

检查 raw/gzip 预算、builder P95、cache/stale 分布、错误数。再做至少 20 次本地浏览器 smoke，确认单 dashboard 请求和 readiness 终点。

- [ ] **Step 4: 做 MySQL 存储引擎和只读数据前置检查**

用服务账户执行只读检查，确认 snapshot 涉及的表全部是 InnoDB：

```sql
SELECT table_name, engine
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name IN (
    't_scheme_registry', 't_target_registry', 't_scheme_predictions',
    't_scheme_actuals', 't_scheme_weekly_actuals',
    't_scheme_monthly_actuals', 't_backtest_runs',
    't_backtest_predictions'
  )
ORDER BY table_name;
```

还要用 builder 的只读诊断输出确认 monthly 同方向重复被折叠、不同方向冲突为零。任何非 InnoDB、缺表或冲突都阻断发布，不在本任务做 schema 迁移或数据清理。

- [ ] **Step 5: 核对设计要求逐项可追溯**

按设计文档第 19 节建立 checklist，每一项链接到测试名、命令输出或配置行。特别确认：

- 同一 Connection + consistent transaction；
- 5–7 次批量 SQL 上限；
- actual conflict fail closed；
- future predict/target 顺序；
- stale 可见；
- q=0 与 `Vary`；
- fallback matrix；
- 公网白名单和发布顺序；
- 200 次 nearest-rank 统计没有剔除失败。

- [ ] **Step 6: 请求独立代码审查**

调用 `superpowers:requesting-code-review`，把设计文档、实施计划、commit 范围和上述证据交给 reviewer。所有 P0/P1 以及与数据语义、竞态、访问控制、性能测量相关的有效问题必须修复并重跑受影响测试；不能仅记录为“后续优化”。

- [ ] **Step 7: 使用完成前验证技能复核**

调用 `superpowers:verification-before-completion`，重新运行它要求的最新证据命令。最后执行：

```bash
git status --short
git log --oneline --decorate -10
git diff codex/audit-bugfixes-20260613...HEAD --stat
git diff codex/audit-bugfixes-20260613...HEAD --check
```

- [ ] **Step 8: 停在生产授权闸门**

向用户报告：本地验证结果、已知风险、预计生产变更、rollout/final/rollback 命令和需要观察的指标。明确请求“是否授权部署到生产并执行 Nginx rollout + 200 次公网浏览器验收”。同时确认两个外部前置条件：

1. 正式 Google Chrome runner 可用；若仍只有 Edge，必须取得用户对 Edge 替代 Chrome 验收的明确批准；
2. 提供 panda_quantflow 实际生产 iframe 父页面 URL/访问方式，用于设计要求的独立 iframe 同口径验收。

缺少第 2 项不妨碍 direct URL canary，但阻断“设计全部完成”的结论。

没有该新授权时，本实施到此暂停。不得重启 launchd、修改生产工作树、reload Nginx、操作 `master` 或 push。

## Task 12: 经明确授权后分阶段发布、验收与收口

**Files:**

- Modify after successful production acceptance: `docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md`
- Modify after successful production acceptance: `docs/product/GRAY_LAB_USER_MANUAL.md`
- Modify after successful production acceptance: `docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md`
- Modify after successful production acceptance: `docs/CURRENT_STATUS.md`
- Deployment targets: production backend/frontend/Nginx only after explicit user authorization

> 本任务不是上一任务完成后的自动下一步。只有用户在看到 Task 11 证据后明确授权生产发布，才可执行。

- [ ] **Step 1: 捕获可恢复基线**

记录当前生产 commit、launchd 状态、Nginx 生效配置摘要、旧页面/旧 API 只读探测和回滚文件位置。确认 LKG 后再动任何生产状态。禁止使用 `git reset --hard` 或覆盖用户未提交内容。

把回滚命令在变更前写入操作记录，顺序固定为：先恢复旧 schemes/metrics/backtest 公网白名单并验证，再恢复旧 HTML/JS/CSS，再按需恢复后端版本，最后恢复上一份 Nginx 配置并 `nginx -t`/reload；结束时验证 direct URL、实际 iframe、旧只读 API、health，以及所有写/管理 API 仍为 403。不得用停隧道或摘站作为常规版本回滚。

- [ ] **Step 2: 先部署后端兼容版本**

后端先提供新 dashboard，同时旧页面仍使用旧 API。重启后检查 `/health`、本机 dashboard、prewarm、schema、raw/gzip 大小、cache/stale headers、错误日志。任何 503、actual conflict、payload 超预算或 startup 异常都立即回滚后端，不进入 Nginx。

- [ ] **Step 3: 应用 Nginx rollout 配置**

先 `nginx -t`，成功后 reload；运行：

```bash
scripts/check_public_access.sh --mode rollout https://bond.finailab.cn
```

检查 429、5xx、upstream time、gzip ratio 和路径矩阵。失败则恢复旧配置并 reload。

- [ ] **Step 4: 发布新前端并小流量 canary**

确认 index 引用 `20260722a`，浏览器请求只含 dashboard。先运行 20 次公网浏览器 canary；任何 legacy fallback、stale、schema error、指标不等价或 P95 明显超过预算都回滚前端，旧 API 仍保留。

- [ ] **Step 5: 执行正式 200 次公网浏览器验收**

```bash
python scripts/benchmark_factor_lab_browser.py \
  --url https://bond.finailab.cn/bond-factor-lab/ \
  --attempts 200 --timeout-seconds 10 \
  --minimum-attempt-period-seconds 0.5 \
  --browser-binary '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --output-json /tmp/factor-lab-public-browser-200.json
```

如果 Task 11 获批使用 Edge，命令只替换 `--browser-binary`，并在报告中记录批准。随后对用户提供的 panda_quantflow 父页面 URL 再运行至少 200 次同口径浏览器验收，脚本必须确认目标 iframe 内 `window.__factorLabReady`，不能只等父页面 load。

同时以 `--minimum-attempt-period-seconds 0.5` 运行 200 次外部 API benchmark，保留 Nginx timing 聚合；再用 `--disable-keepalive` 执行至少 50 次新 TCP/TLS 上下文并单独报告 DNS/TCP/TLS/TTFB/total。主验收必须满足 P95 `<1000ms`、零失败、零 fallback、零 stale、零 schema/console/page error；cold connection、backend cold start、iframe parent 单独报告，不得从主样本偷偷删除异常。

- [ ] **Step 6: 容量观察后撤销旧 API 公网访问**

先对 1、5、10 个并发浏览器用户分别持续 5 分钟，正常刷新周期为 60 秒，并增加受限的 identity/no-cache 压力场景。确认 cache miss 期间仍只有一个 rebuild，health 不受拖累，CPU、RSS、DB pool、线程、隧道流量有界；健康流量不应出现 429/503，过载应快速按设计拒绝。依据结果复核 `2r/s + burst=20 + 20 connections/IP` 对企业 NAT 是否合理；若调整，必须更新配置测试、运行手册和证据。

观察期内继续确认 DB 查询数、builder P95、snapshot MISS/STALE、CPU、内存、429、5xx 和 payload bytes 稳定。新前端上线后至少保留 30 分钟兼容窗口，并从 access log 排除验收脚本 User-Agent 后观察旧 schemes/metrics/backtest 流量；若仍有真实旧页面流量，延长窗口或通知用户刷新，不能为了收口立即打断已打开页面。

兼容窗口和证据满足后切换 final Nginx 配置，`nginx -t` 后 reload，再运行：

```bash
scripts/check_public_access.sh --mode final https://bond.finailab.cn
```

旧 schemes/metrics/backtest API 必须从公网关闭，但本地内部兼容代码暂不删除；若 final matrix 失败，恢复 rollout 配置。

- [ ] **Step 7: 更新状态文档并最终验证**

将设计文档状态改为“已实施并验收”，写入真实日期、commit、探测地点、浏览器产品与版本、200 次 P50/P95/P99、错误/fallback/stale 数、raw/gzip bytes、回滚点。同步更新 `docs/CURRENT_STATUS.md`、当前用户手册和运行手册。

调用 `superpowers:verification-before-completion`，重跑 final 公网矩阵和关键测试，再提交文档：

```bash
git status --short
git add docs/superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md \
  docs/operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md \
  docs/CURRENT_STATUS.md \
  docs/product/GRAY_LAB_USER_MANUAL.md
git diff --cached --check
git commit -m "docs: record factor lab performance acceptance"
```

- [ ] **Step 8: 单独请求分支集成授权**

调用 `superpowers:finishing-a-development-branch` 给出保留分支、PR、合并等选项。即使生产 canary 已通过，也不得自行合并/覆盖 `master` 或 push；这些仍需要用户明确确认。

## 最终交付清单

- [ ] 新旧 canonical 数据等价证据齐全。
- [ ] dashboard 单连接、查询预算、事务一致性测试通过。
- [ ] snapshot cache/single-flight/stale 并发测试通过。
- [ ] gzip q-value、Vary、identity、no-double-compression 测试通过。
- [ ] 前端只有一个 dashboard 请求，竞态、fallback、stale、visibility、readiness 测试通过。
- [ ] Google Fonts 已移除，静态缓存策略与版本 token 一致。
- [ ] Nginx rollout/final 路径矩阵与 rate limit 已验证。
- [ ] 全量 pytest 通过或所有基线既有失败被明确隔离。
- [ ] 本地 API 200 次和浏览器 smoke 报告已生成。
- [ ] 获授权后，公网 200 次硬刷新 P95 小于 1 秒且零失败/fallback/stale。
- [ ] 回滚点、运维手册、真实验收数据已记录。
- [ ] 未经额外授权，没有合并/覆盖 `master` 或推送远程。
