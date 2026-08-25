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

只读合同探针统一使用现有 Dashboard Gate：

```bash
python -m harness gate dashboard \
  --scheme-id <base_scheme_id> \
  --api-base-url http://127.0.0.1:8100
```

Gate 使用生产端唯一响应预算，验证 V1 JSON、active composite identity、signal 与 backtest 分区；它不接受
非 200、超限、非法或缺失结果。gzip、`no-store` 与响应头由后端 API 合同测试保护，不再维护第二个 868 行的
独立 benchmark 实现。

历史 live 信号缺口使用独立的只读报告：

```bash
python scripts/report_signal_gaps.py --as-of 2026-08-07
```

报告按 Blackbox 当前精确 active version 的 `approved_at`、或 Native Registry 的
`created_at` 之后首个信号点开始计算，绝不读取 `deployed_at`。它只报告缺口，不写库；补齐需
显式执行单日运维命令 `python -m harness signal-gap-fill --predict-date YYYY-MM-DD
[--scheme-id BASE_SCHEME_ID]`，该命令只对缺失键 insert-only 写 `gray_live`，不再增加第二层
用户、token 或确认授权。

## 故障处理边界

收到 `503 dashboard_data_unavailable` 时，先检查数据库可用性和当前 route 的安全日志；
不要用旧 dashboard 数据掩盖故障，也不要为诊断擅自重启服务、修改 installed plist、执行
`launchctl` 或写生产数据库。这些操作均需要独立授权。
