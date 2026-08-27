# Bond Factor Lab 公网 Dashboard 读路径

**文档状态**：`CURRENT`
**适用版本**：dashboard schema `factor-lab-dashboard-v2`

## 当前合同

`GET /api/factor-lab/dashboard` 每次请求直接从数据库构建当前 dashboard；服务端不保存
TTL、single-flight 或 last-known-good（LKG）快照。

```text
浏览器 → Nginx 只读入口 → FastAPI dashboard route
       → dashboard 专用只读 SQLAlchemy Engine
       → 当前 Registry / signal / actual / backtest 批量查询
       → compact V2 JSON
```

- 数据库和 dashboard 构建成功：返回 `200` 当前数据。
- 数据库连接、查询或构建失败：返回 `503`，正文仅为
  `{"error_code":"dashboard_data_unavailable"}`。
- 前端收到失败后清空已提交的数据并显示“数据不可用”；不得继续显示旧数据、旧时间或
  “数据已过期”。

Dashboard 使用独立只读 Engine，连接超时为 `0.5s`、读超时为 `0.75s`、写超时为 `0.5s`，
单条 MySQL 查询上限为 `500ms`；连接池开启 `pool_pre_ping`，并在 `300s` 回收连接，避免
复用断开的长连接。

一次请求内，信号状态计算按相同 cadence 和日期上下文复用日历规划结果。日历快照只读取
Dashboard 历史窗口前一自然年起的数据，以保留年度任务的完整边界，同时避免每次扫描无关的
早期历史。该复用仅存在于当前请求内，请求完成即释放；不得演变成跨请求 TTL、结果缓存或
LKG。最终 compact payload 仍执行完整合同校验，Registry、预测、Actual 和回测查询口径不变。

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

Gate 使用生产端唯一响应预算，验证 V2 JSON、active composite identity、signal 与 backtest 分区；它不接受
非 200、超限、非法或缺失结果。gzip、`no-store` 与响应头由后端 API 合同测试保护。历史信号检查与补齐不属于
Dashboard 读路径，统一遵循[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 故障处理边界

收到 `503 dashboard_data_unavailable` 时，先检查数据库可用性和当前 route 的安全日志；
不要用旧 dashboard 数据掩盖故障，也不要为诊断擅自重启服务、修改 installed plist、执行
`launchctl` 或写生产数据库。这些操作均需要独立授权。
