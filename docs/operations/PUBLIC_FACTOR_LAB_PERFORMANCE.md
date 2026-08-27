# Bond Factor Lab 公网 Dashboard 读路径

**文档状态**：`CURRENT`
**适用版本**：dashboard schema `factor-lab-dashboard-v3`

## 当前合同

`GET /api/factor-lab/dashboard` 每次请求直接从数据库构建当前 dashboard；服务端不保存
TTL、single-flight 或 last-known-good（LKG）快照。

```text
浏览器 → Nginx 只读入口 → FastAPI dashboard route
       → dashboard 专用只读 SQLAlchemy Engine
       → active Registry / prediction / Actual / backtest 批量查询
       → compact V3 JSON
```

- 数据库和 dashboard 构建成功：返回 `200` 当前数据。
- 数据库连接、查询或构建失败：返回 `503`，正文仅为
  `{"error_code":"dashboard_data_unavailable"}`。
- 前端收到失败后清空已提交的数据并显示“数据不可用”；不得继续显示旧数据、旧时间或
  “数据已过期”。

Dashboard 使用独立只读 Engine，连接超时为 `0.5s`、读超时为 `0.75s`、写超时为 `0.5s`，
单条 MySQL 查询上限为 `500ms`；连接池开启 `pool_pre_ping`，并在 `300s` 回收连接，避免
复用断开的长连接。

Dashboard 是数据库业务结果视图：active Registry 即使尚无 live prediction 也可带空
`live_rows` 展示；有多少 prediction 就展示多少，不生成占位行，也不推导方案是否应当运行。
读路径不读取 run、DataBridge 日期或交易日历，不计算 `missing`、`not_due`、`no_run` 等调度
状态。调度缺口与运行失败由 scheduler、数据库 run、systemd/launchd 日志和受控 gap-fill 链路
处理，不混入产品读模型。

## 响应与探针

成功响应必须包含：

- `Cache-Control: no-store`
- `Vary: Accept-Encoding`
- `X-Request-ID`
- `X-Dashboard-Snapshot-ID`（与 body `snapshot_id` 一致）

`Server-Timing` 仅用于当前请求的 DB、canonical、serialization 和 route 诊断，不能作为缓存或
可用性真相。

只读合同探针统一使用现有 Dashboard Gate：

```bash
python -m harness gate dashboard \
  --scheme-id <base_scheme_id> \
  --api-base-url http://127.0.0.1:8100
```

Gate 使用生产端唯一响应预算，验证 V3 JSON、active composite identity、展示身份、任务字段和 backtest 分区；
空 `live_rows` 是合法结果。它不接受非 200、超限或非法结果。gzip、`no-store` 与响应头由后端 API 合同测试保护。历史信号检查与补齐不属于
Dashboard 读路径，统一遵循[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 故障处理边界

收到 `503 dashboard_data_unavailable` 时，先检查数据库可用性和当前 route 的安全日志；
不要用旧 dashboard 数据掩盖故障，也不要为诊断擅自重启服务、修改 installed plist、执行
`launchctl` 或写生产数据库。这些操作均需要独立授权。
