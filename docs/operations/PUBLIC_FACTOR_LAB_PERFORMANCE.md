# Bond Factor Lab 公网 Dashboard 读路径

**文档状态**：`CURRENT`
**适用版本**：dashboard schema `factor-lab-dashboard-v1`

## 当前合同

`GET /api/factor-lab/dashboard` 每次请求直接从数据库构建当前 dashboard；服务端不保存
TTL、single-flight 或 last-known-good（LKG）快照。

```text
浏览器 → Nginx 只读入口 → FastAPI dashboard route
       → dashboard 专用只读 SQLAlchemy Engine
       → 当前 Registry / signal / actual / backtest 批量查询
       → compact V1 JSON
```

- 数据库和 dashboard 构建成功：返回 `200` 当前数据。
- 数据库连接、查询或构建失败：返回 `503`，正文仅为
  `{"error_code":"dashboard_data_unavailable"}`。
- 前端收到失败后清空已提交的数据并显示“数据不可用”；不得继续显示旧数据、旧时间或
  “数据已过期”。
- `stale=false` 与 `snapshot_age_ms=0` 暂时保留为 V1 兼容字段；它们不是缓存状态，前端
  不得据此显示 age/LKG。

Dashboard 使用独立只读 Engine，连接超时为 `0.5s`、读超时为 `0.75s`、写超时为 `0.5s`，
单条 MySQL 查询上限为 `500ms`；连接池开启 `pool_pre_ping`，并在 `300s` 回收连接，避免
复用断开的长连接。

## 响应与探针

成功响应必须包含：

- `Cache-Control: no-store`
- `Vary: Accept-Encoding`
- `X-Request-ID`
- `X-Dashboard-Snapshot-ID`（与 body `snapshot_id` 一致）

不再存在 `X-Dashboard-Cache`、`X-Dashboard-Snapshot-Age` 或
`X-Dashboard-Warning`。`Server-Timing` 仅用于当前请求的 DB、canonical、serialization
和 route 诊断，不能作为缓存或可用性真相。

只读性能探针可使用：

```bash
python scripts/benchmark_factor_lab_dashboard.py \
  --url http://127.0.0.1:8100/api/factor-lab/dashboard \
  --attempts 20 --timeout 5 \
  --output-json /tmp/factor-lab-api-smoke.json
```

探针每次都验证 JSON、单层 gzip、`Content-Length`、`no-store`、`Vary`、snapshot ID 和
字节预算；它不接受任何 stale 响应。

历史 live 信号缺口使用独立的只读报告：

```bash
python scripts/report_signal_gaps.py --as-of 2026-08-07
```

报告按 Blackbox 当前精确 active version 的 `approved_at`、或 Native Registry 的
`created_at` 之后首个信号点开始计算，绝不读取 `deployed_at`。它只报告缺口，不写库；受控
补齐仍必须另行授权，并只写缺失键的 `gray_live`。

## 故障处理边界

收到 `503 dashboard_data_unavailable` 时，先检查数据库可用性和当前 route 的安全日志；
不要用旧 dashboard 数据掩盖故障，也不要为诊断擅自重启服务、修改 installed plist、执行
`launchctl` 或写生产数据库。这些操作均需要独立授权。
