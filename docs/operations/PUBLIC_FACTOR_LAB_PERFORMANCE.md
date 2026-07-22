# Bond Factor Lab 公网性能运行手册

**文档状态**：`CURRENT`

**适用版本**：dashboard schema `factor-lab-dashboard-v1`

**最后核验日期**：2026-07-22
**目标读者**：发布执行人、平台运维和性能验收人员

本文把[公网亚秒设计](../superpowers/specs/2026-07-22-factor-lab-subsecond-dashboard-design.md)
转成可执行检查。部署和 Nginx 原子切换仍以
[`deploy/README.md`](../../deploy/README.md) 为准；本手册不授予部署、重启、Nginx
reload、生产数据库操作、合并 `master` 或推送远程的权限。

## 1. 展示链路与合同

```text
Chrome / panda_quantflow iframe
  -> 公网 Nginx 精确只读白名单、限流、TLS
  -> SSH 反向隧道
  -> FastAPI GET /api/factor-lab/dashboard
  -> DashboardSnapshotStore（TTL 1 秒、single-flight、显式 stale LKG）
  -> 同一只读事务中的 active registry + live + actual + latest backtest 批量快照
  -> compact V1 解码、view model、DOM 原子提交
  -> 下一次 requestAnimationFrame 设置 window.__factorLabReady
```

正常页面导航只能发起一个 dashboard 数据 GET，不得调用旧 `/api/schemes`、
`/api/metrics/{scheme_id}` 或 `/api/backtests/factor-lab`。快照层是 L4 只读展示路径，
不改变输入单点、写库单点、Native core 纯净和源算法保真四条不变量。

关键响应头：

| Header | 含义 | 健康验收期要求 |
|---|---|---|
| `Cache-Control: no-store` | 浏览器每次刷新都请求服务；进程内快照负责去重 | 必须存在 |
| `Vary: Accept-Encoding` | gzip/identity 表示正确分流 | 必须存在 |
| `Content-Encoding: gzip` | FastAPI 在进入 SSH 隧道前压缩一次 | gzip 探针应存在且只可解压一次 |
| `X-Request-ID` | 单次请求关联标识 | 记录，不含 token |
| `X-Dashboard-Snapshot-ID` | 不透明快照标识 | 与 body `snapshot_id` 一致 |
| `X-Dashboard-Cache` | `HIT`、`MISS`、`STALE` 或 `UNAVAILABLE` | 只允许 fresh `HIT/MISS` |
| `X-Dashboard-Snapshot-Age` | 快照年龄，毫秒 | 正常缓存年龄不超过 1 秒 |
| `X-Dashboard-Warning` | `stale-last-known-good` 降级提示 | 健康验收不得存在 |
| `Server-Timing` | route、DB、canonical build、serialization 等安全阶段 | 用于诊断，不替代端到端 SLO |

`window.__factorLabReady` 是浏览器探针终点。它必须由本次导航对应 frame 在完整 DOM
提交后的下一帧发布，至少包含 `seq`、`snapshotId`、`committedAt`、`stale`、
`source`、`schemeCount`、`liveRowCount`、`backtestRowCount`。端到端耗时由外部探针以
`Page.navigate` 前后的 monotonic 时钟计算；不得采用前端字段反推导航耗时。

## 2. SLO、样本和容量预算

正式公网 direct URL 和真实 panda_quantflow iframe 分开验收，均要求：

- 约定地点和设备上的 Google Chrome，禁用缓存、绕过 Service Worker；
- 顺序硬刷新至少 200 次，P95 用 nearest-rank
  `sorted_samples[ceil(0.95*n)-1]`，不得插值；
- navigation-to-ready P95 `< 1000ms`；
- 零失败、零超时、零 stale、零 legacy fallback、零 schema/console/page error；
- 每次恰好一个 dashboard 数据请求，ready 来源是 `dashboard`；
- 所有失败和超时仍保留在 attempts 和失败清单，不得删除后再算 P95。

配套保护预算：

- dashboard gzip-6 当前规模 `<= 100KB`；
- dashboard raw JSON 当前规模 `<= 1.5MB`；
- 后端 snapshot 批量读取、canonical 构建和 DTO 生成 P95 `<= 300ms`；
- 正常快照 TTL 最大 1 秒，健康窗口 stale 率为 0；
- 公网顺序探针使用 `--minimum-attempt-period-seconds 0.5`，不超过入口 2r/s
  预算；节流从 attempt start 计算、写入报告，但不计入该次 latency；
- 容量验收为 1、5、10 独立页面/用户各持续 5 分钟，观察 CPU、RSS、DB pool、
  single-flight waiter 和隧道流量；超载应按策略显式返回 429/503。

SLO 不是“任意网络、任意设备都绝对小于 1 秒”的承诺。报告必须写探测地点、设备、
网络、连接条件、浏览器完整产品版本、时间和时区。

## 3. 三类探针命令

所有报告写入已存在的目录，脚本以临时文件加 `os.replace` 原子落盘。报告仅记录
origin/path，不记录 URL query、Cookie、Authorization、token 或响应 body。

### 3.1 本地 API smoke

先确认本地已授权启动的服务在 `127.0.0.1:8100`；本步骤本身不启动、停止或重启服务：

```bash
python scripts/benchmark_factor_lab_dashboard.py \
  --url http://127.0.0.1:8100/api/factor-lab/dashboard \
  --attempts 20 --timeout 5 \
  --output-json /tmp/factor-lab-api-smoke.json
```

20 次只验证工具和 API 协议，报告 `formal_acceptance=false`。正式 API 外部顺序检查：

```bash
python scripts/benchmark_factor_lab_dashboard.py \
  --url https://bond.finailab.cn/bond-factor-lab/api/factor-lab/dashboard \
  --attempts 200 --timeout 5 --minimum-attempt-period-seconds 0.5 \
  --probe-location '<城市/机房>' --device '<设备>' \
  --network '<网络>' --connection-condition '<有线/Wi-Fi/公网条件>' \
  --enforce-slo --output-json /tmp/factor-lab-api-public-200.json
```

默认复用 keepalive；复用请求中没有再次发生的 DNS/TCP/TLS 阶段记为 `null`，不会把
首个连接成本摊平。冷连接另跑至少 50 次：

```bash
python scripts/benchmark_factor_lab_dashboard.py \
  --url https://bond.finailab.cn/bond-factor-lab/api/factor-lab/dashboard \
  --attempts 50 --timeout 5 --disable-keepalive \
  --minimum-attempt-period-seconds 0.5 \
  --output-json /tmp/factor-lab-api-cold-50.json
```

### 3.2 Rollout 后 direct Chrome

先按 `deploy/README.md` 完成 rollout 访问矩阵；必须使用 Google Chrome 做正式验收：

```bash
python scripts/benchmark_factor_lab_browser.py \
  --url https://bond.finailab.cn/bond-factor-lab/ \
  --attempts 200 --timeout-seconds 5 \
  --enforce-slo \
  --minimum-attempt-period-seconds 0.5 \
  --browser-binary '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --expected-scheme-count '<canonical active数量>' \
  --expected-live-row-count '<canonical live数量>' \
  --expected-backtest-row-count '<canonical latest backtest数量>' \
  --probe-location '<城市/机房>' --device '<设备>' \
  --network '<网络>' --connection-condition '<有线/Wi-Fi/公网条件>' \
  --output-json /tmp/factor-lab-browser-direct-200.json
```

本机 Microsoft Edge 可做 5 次工具 smoke，但默认不能形成正式结论。只有用户明确批准
Edge 替代口径时，才同时传入 `--allow-edge-acceptance` 和可审计的
`--edge-approval-reference`；批准只作用于该份报告。

浏览器探针显式传 `--enforce-slo` 而样本少于 200 时，即使所有尝试成功也以退出码 2
拒绝正式验收；不传该 flag 的少量样本是 smoke，成功时可退出 0，但报告仍保持
`formal_acceptance=false`。达到 200 次后无论是否传 flag 都自动按正式门槛退出 0/1。

### 3.3 panda_quantflow iframe

URL 使用真实 Shell 页面；`--ready-frame-url-substring` 应唯一匹配 Bond Factor Lab
iframe URL。探针使用 browser websocket 的 flattened Target session，递归 auto-attach
OOPIF/后代 target，并严格按 `targetInfo.type` 启用域：page/iframe 使用 Page、Runtime、
Network、Log、Inspector 及禁缓存/绕过 Service Worker/UA 设置；worker、shared_worker、
service_worker 只启用其支持的 Runtime、Network、Log，绝不调用 Page/Inspector。未知
target 会先恢复执行再 detach，避免 `waitForDebuggerOnStart` 冻结。ready evaluate、
dashboard/legacy request 和错误都按 owning session 汇总；父页面的同名 ready 不会通过：

```bash
python scripts/benchmark_factor_lab_browser.py \
  --url '<panda_quantflow真实Shell URL>' \
  --ready-frame-url-substring '/bond-factor-lab/' \
  --attempts 200 --timeout-seconds 5 \
  --enforce-slo \
  --minimum-attempt-period-seconds 0.5 \
  --browser-binary '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --expected-scheme-count '<canonical active数量>' \
  --expected-live-row-count '<canonical live数量>' \
  --expected-backtest-row-count '<canonical latest backtest数量>' \
  --probe-location '<城市/机房>' --device '<设备>' \
  --network '<网络>' --connection-condition '<公网条件>' \
  --output-json /tmp/factor-lab-browser-iframe-200.json
```

### 3.4 1/5/10 用户 soak

Soak 与 `--attempts` 模式互斥，永远不标成正式 200 次 SLO：

```bash
for users in 1 5 10; do
  python scripts/benchmark_factor_lab_browser.py \
    --url https://bond.finailab.cn/bond-factor-lab/ \
    --concurrency "$users" --duration-seconds 300 --refresh-interval-seconds 60 \
    --timeout-seconds 5 \
    --browser-binary '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
    --probe-location '<城市/机房>' --device '<设备>' \
    --network '<网络>' --connection-condition '<公网条件>' \
    --output-json "/tmp/factor-lab-soak-${users}.json"
done
```

## 4. 必须分栏报告的冷路径

稳定热路径报告之外，以下样本不得隐藏，也不得混成一个无法解释的平均数：

1. `--disable-keepalive` 的新 DNS/TCP/TLS 冷连接，至少 50 次；
2. 经用户专项授权重启 backend 后的首请求，单列 backend cold start；
3. panda_quantflow iframe 报告相对于 direct URL 的父页面开销；
4. 隧道重连、网络不可达、数据库不可用和 429/503，保留失败尝试原貌。

未经授权不得为制造 cold-start 样本重启 backend。API 的 `Server-Timing` 可帮助区分
backend cold 与公网传输，但浏览器 navigation-to-ready 才是主验收终点。

## 5. 异常排障顺序

### stale / `X-Dashboard-Cache: STALE`

1. 立即把样本判为失败，不从性能数组移除；核对 `snapshot_id`、age、warning 和 health
   稳定错误码 `dashboard_snapshot_stale`。
2. 查应用结构化日志中的 request/snapshot ID、cache state、rebuild error、waiter 数、
   DB/canonical/serialization/build duration。
3. 查 single-flight 是否释放、是否只存在一个 rebuild；再查 DB 连接池和 actual 冲突。
4. LKG 可保持页面可见，但不能用 stale 伪装健康 SLO。

### 503 / `UNAVAILABLE`

1. 区分尚未 prewarm、无 LKG 的 rebuild failure、等待超时和后端不可达。
2. 先查 `/api/health` dashboard 子状态，再按 request ID 关联应用日志；不要盲目重启。
3. 查数据库只读连接、SQL 超时和 payload budget；保持旧 API/rollout 回滚能力。

### 429

1. 查 Nginx access log 的 traffic class、limit status、request time 和 UA。
2. 确认顺序验收确实使用 0.5 秒最小 attempt 周期，且没有并行运行多个正式探针。
3. 容量测试中的 429 应记录为显式过载；不得调高入口限制掩盖 single-flight/DB 问题。

### actual conflict

1. canonical actual 冲突必须 fail closed；不得任取一行或修改算法输出贴合。
2. 按 target tenor、task type 和 target date 定位冲突事实表，确认 daily/weekly/monthly
   scope 未混用。
3. 数据修复属于单独授权的写库操作；性能发布只能诊断和报告，不能直接改生产数据。

## 6. Rollout、final 和回滚

发布步骤遵循 `deploy/README.md` 的不可变 release 与原子 symlink 流程：

1. 保存最后已知良好的应用 commit、当前 Nginx `-T`、公网基线和回滚目标。
2. 用户明确授权后先发布后端，旧前端仍可工作；完成本地 parity/API 检查。
3. 入口机以 rollout 配置只增加 dashboard 白名单并保留旧三类展示 API；真实完整配置
   `nginx -t` 成功后才 reload。
4. 验证 TLS、路径、gzip 单层、schema、direct/iframe browser 及容量。
5. 前端已经稳定使用 dashboard 且用户确认后，才切 final 收口旧公网展示 API。
6. 只有用户再次明确确认，才允许合并/覆盖 `master` 和推送远程。

常规回滚保持站点和 SSH 隧道在线：先把入口恢复 rollout 使旧 API 可达，再恢复上一版
前端/后端，最后恢复上一份 Nginx release 并执行真实 `nginx -t`/reload。不得用停隧道、
摘站点或清除 admin token 代替版本回滚。

## 7. 观测与敏感信息边界

应用日志应记录 request ID、snapshot ID、cache hit/miss/stale、snapshot age、DB/canonical/
serialization/build duration、scheme/live/backtest 数、raw/gzip bytes、rebuild 状态、waiter
数、HTTP 状态和稳定错误类别。Nginx access log 应记录 method/path、status、wire bytes、
content encoding、request/upstream connect/header/response time、限流状态和 request ID。

日志和 benchmark 报告不得记录：Cookie、Authorization、admin token、SSH key、完整 query、
预测 JSON body、数据库凭据或个人身份信息。浏览器 console/page error 只记录稳定错误类别，
不复制可能含敏感内容的消息正文。报告放入受控证据目录前再次检查权限和内容。

## 8. 验收结论模板

最终证据包至少分别列出：direct Chrome 200、iframe Chrome 200、API warm 200、API cold
50、1/5/10 用户 soak，以及经授权的 backend cold first request。对每份报告记录 commit、
release ID、探测元数据、P50/P95/P99、失败/stale/fallback 数、请求字节和 cache 分布。

任一正式报告样本少于 200、P95 不小于 1000ms、含失败/stale/fallback/schema/console/page
error、关键元数据为 `unknown`，或浏览器不是 Google Chrome（且没有该报告专属 Edge 批准），
都必须明确写“未通过”，不能以本地 5 次 smoke 或 API TTFB 代替浏览器端到端结论。
