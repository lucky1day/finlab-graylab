# 公网 Dashboard 刷新与链路可靠性

**文档状态**：`CURRENT`

**适用版本**：dashboard schema `factor-lab-dashboard-v5`

## 设计结论

Dashboard 展示的是数据库中已经发布的预测、Actual、Registry 和回测事实，不是实时行情。
这些事实会在一天内少数离散窗口变化，但不需要浏览器每分钟全量重建：

| 事实来源 | Mac3 自然调度窗口 |
|---|---|
| 日频预测 | 工作日 07:03 |
| Actuals | 每日 08:30、19:00、23:45 |
| 月频与周期预测 | 每日 18:00 到期判断 |
| 周频预测 | 周六 11:30 |
| 激活或授权补缺 | 独立人工操作窗口 |

前端的新鲜度目标为最多五分钟。首次认证后立即读取；页面可见时每五分钟兜底刷新；页面隐藏时
停止轮询；重新可见时只在成功快照已满五分钟或此前刷新失败时立即读取。任一时刻最多存在一个
Summary 请求。

## 失败展示合同

- 首次读取失败且没有成功快照：显示“数据不可用”，不生成或猜测任何数据。
- 已有成功快照后刷新失败：保留完整已提交视图，显示“刷新失败，显示 `<generated_at>` 数据”，
  并将 DOM 状态标为 `stale`。
- stale 只是浏览器对最近一次成功响应的暂存，不是服务端旧快照；API 仍直接读取当前数据库，失败
  仍返回 `503 dashboard_data_unavailable`。
- 后续成功响应必须原子替换整个 Summary candidate；不得混合新旧方案、月份或 Detail 缓存。
- 注销、认证失效或身份切换时必须清除浏览器中的已提交数据。

失败重试依次为 10 秒、30 秒、60 秒和 5 分钟；连续失败后保持五分钟间隔。成功一次后恢复正常
五分钟周期。用户切换到隐藏标签页时清除计时器，恢复可见时重新根据快照年龄决定下一次请求。

## 故障定位

生产请求链为：

```text
浏览器 → ECS Nginx → ECS 127.0.0.1:18100
       → SSH 反向转发 → Mac3 127.0.0.1:8100 → Mac3 MySQL
```

固定使用三点探针定位故障域：

1. Mac3 `127.0.0.1:8100/api/health` 成功、ECS 18100 失败：检查 SSH 隧道。
2. ECS 18100 成功、公网域名失败：检查 Nginx、TLS 与公网入口。
3. health 成功、Dashboard 503：按 `X-Request-ID` 检查认证、数据库和 Dashboard 构建阶段。
4. Dashboard 200、浏览器显示 stale：检查客户端网络、响应解析和刷新状态机。

Dashboard 结构化请求事件写入生产 Uvicorn error logger；Nginx timing log 以
`$time_iso8601 $request_id` 开头，并记录 `$upstream_status`。两端用同一 `X-Request-ID` 关联，避免
从无时间戳的片段猜测故障顺序。

生产 SSH 固定连接 ECS IPv4，不依赖被 fake-IP 接管的域名解析，也不得进入 Clash/Mihomo 的代理
故障域。TUN 开启时必须使用 `rule` 模式，在规则层将 ECS IPv4 指向 `DIRECT`，并在 TUN 路由层用 `route-exclude-address`
排除 ECS `/32`。验收标准是 Clash 日志不出现生产 SSH 的 `dial GLOBAL`，而不是只检查域名是否
解析为真实 IP。

## 分阶段执行方案

每阶段只在上一阶段证据完整后继续。应用发布、ECS Nginx、Mac3 Clash 与 installed launchd 分为
四个独立变更单元，任一单元失败只回滚该单元，不以重启数据库或 Writer 兜底。

### 阶段 A：代码与配置候选验证

1. 在 `codex/develop` 核对工作区，只纳入本问题相关文件；构建 release 前再次确认精确提交。
2. 校验 SSH plist 语法和安全参数：目标必须是固定 ECS IPv4，host key 必须 strict，keepalive、
   connect timeout 和 remote-forward failure 必须有界。
3. 运行 Dashboard builder、API、数据库 timeout、Nginx 和 launchd 合同测试；运行 JavaScript 语法
   检查与浏览器状态机验收。
4. 使用生产只读连接做五次串行 Summary 构建，确认返回的 scheme/live/backtest 行数稳定且没有
   503；这一步不是并发容量结论。

退出条件：所有测试通过、`git diff --check` 无错误、plist 可解析、五次只读构建全部成功。未经
另行批准，不在此阶段提交、发布、重载服务或修改现场配置。

### 阶段 B：Mac3 控制链路变更（独立生产授权）

1. 只读保存 installed plist、`launchctl print`、当前 SSH 进程、ECS 18100 listener、Clash 生效
   配置和最近 tunnel error 日志，作为回滚基线。
2. 通过独立可信渠道核对 ECS SSH host-key fingerprint；将固定 IPv4 对应的已核验 key 写入生产
   用户的 `known_hosts`。不得以 `StrictHostKeyChecking=no` 或未核验的 `ssh-keyscan` 输出代替身份
   校验。
3. 将 Clash 切到规则模式，增加 ECS IPv4 `DIRECT` 规则与 TUN `/32` route exclusion。先用一次
   非持久 SSH 握手确认实际对端为固定 IPv4，并检查 Clash 日志没有 `dial GLOBAL`。
4. 将仓库候选参数合并进 installed tunnel plist；确认 18100 没有第二个 owner 后，执行一次受控
   `bootout/bootstrap`。不得同时手工启动第二条 `ssh -R`。
5. 连续观察至少 15 分钟：launchd PID/启动次数不持续增长，ECS 18100 始终有唯一 listener，三点
   health 探针持续成功。任一条件失败立即回滚 plist 与 Clash 配置，并恢复原 tunnel owner。

### 阶段 C：应用 release 与 ECS Nginx（分别授权）

1. 从阶段 A 的精确提交生成不可变 release，先在候选环境验证；不从开发工作区直接覆盖生产
   runtime，不修改数据库 schema 或预测事实。
2. Mac3 Backend 切到该 release 后，先验证 loopback health、认证后的 Summary 200、
   `X-Request-ID`、`X-Dashboard-Snapshot-ID` 和 `Server-Timing`，再观察 Uvicorn 结构化事件已经落盘。
3. ECS Nginx 使用候选配置执行语法检查；通过后才受控 reload。验证 timing log 同时包含时间戳、
   request ID、入口状态和 upstream 状态，Dashboard read timeout 为五秒。
4. 从公网完成一次 fresh → 注入单次失败 → stale → 自动恢复 fresh 的演练。故障注入只能作用于
   Dashboard 读路径，不能停止 Writer、写库或制造真实预测缺口。

### 阶段 D：观察与关闭

1. 连续覆盖至少一个 Actuals 窗口和一个预测窗口，核对事实写入后五分钟内对可见页面生效。
2. 汇总每层的成功率和耗时：浏览器请求数、Nginx 200/499/503/504、upstream timing、Backend
   DB/build/encoding timing、tunnel 重连次数。
3. 只有在窗口内无隧道抖动、无非预期 503/504、无每分钟轮询且 stale 可恢复时关闭问题。
4. 若五秒入口预算仍被耗尽，停止继续放宽 timeout；以 request ID 定位 DB、构建或隧道阶段，另开
   容量优化任务。

### 回滚顺序

1. 前端或 Backend 异常：将 `current` 原子指回上一不可变 release；不回滚或覆盖数据库事实。
2. Nginx 异常：恢复上一份已验证 site 配置，语法检查通过后 reload。
3. tunnel 异常：恢复上一 installed plist 与 Clash 配置，确保旧会话退出后再恢复唯一 owner。
4. 回滚后重复三点 health 探针与认证 Summary 检查，并保留失败 request ID 和对应时间窗口。

## 变更与验收边界

仓库 plist 只定义期望值：SSH keepalive 为 15 秒乘 2 次、连接超时 10 秒、远端转发失败即退出，
并要求预置已核验的 host key。installed plist、Clash、launchd、sshd 或 Nginx 的任何替换、重载、
启停都属于独立生产操作，必须先只读核对现场状态并取得明确授权。

每次应用发布至少验证：

- 可见页面正常请求不超过每小时 12 次，隐藏页面没有轮询；
- 事实写入后最迟五分钟可见；
- 成功后注入一次 503，方案数、筛选和已打开月历不变，状态转为 stale；
- 下一次成功响应原子替换视图并恢复 fresh；
- Dashboard 读压测无 503/504，route p99 小于 1.5 秒、DB p99 小于 750 毫秒；
- ECS 18100 持续由预期 SSH 会话监听，Clash 不记录生产 SSH `dial GLOBAL`。

数据库或构建失败时不得为了恢复页面而写库、覆盖预测、重启 Writer 或复制另一主机数据库。
应用、入口网络和隧道变更必须使用各自独立的回滚步骤。

## 2026-09-04 生产执行记录

本次变更已完成阶段 A 至阶段 C，生产应用 release 为
`b736d3b21b1c57455cf36d1cdcaa22b00fdda455`，上一 release
`617113ed0b2e3c059d5b8a4d1390f453938966f5` 继续由 `previous` 指针保留。
源码归档 SHA-256 为
`8f240e6b97d612df460f53f3419f35bca578c3eaa9f0ca1a0b01a7d101fddb4d`。

已执行的生产控制面变更如下：

- Mac3 installed tunnel plist 改为固定 ECS IPv4、strict host key、15 秒乘 2 次保活、10 秒连接超时；
- Clash 运行态切到 `rule`，ECS `/32` 使用 `DIRECT` 并加入 TUN route exclusion；
- ECS sshd 使用 30 秒乘 3 次 client alive，并保留 TCP keepalive；
- ECS Nginx 部署带 ISO 时间、request ID、upstream status 的 timing log，Dashboard read timeout 为五秒；
- Mac3 Backend 和前端切换到上述不可变 release；
- Wi-Fi DNS 从含不可达 IPv6 resolver 的 DHCP 结果改为 `223.5.5.5`、`119.29.29.29`，
  原状态可用 `sudo networksetup -setdnsservers Wi-Fi Empty` 恢复。

受控向生产 tunnel 发送一次 `SIGTERM` 后，launchd 在首次检查前恢复服务；`runs=2` 中第二次启动
即本次故障注入。恢复后连续观察超过 15 分钟，13 个固定 IP + TLS/SNI 公网 health 样本全部为
200，端到端为 63–153 毫秒，PID 与启动次数未变化，tunnel stderr 未新增字节，ECS 18100 始终
只有一个 sshd listener。22:38 以后 Nginx error log 没有新事件。

全量测试结果为 627 passed、4 skipped、194 subtests passed。当前生产 release 直接构建
Dashboard 三次为 364–384 毫秒，97 个方案，gzip 后约 22.8 KB。Wi-Fi DNS 变更后的首次解析为
约 240 毫秒，后续十次探针的 DNS 耗时为 1.6–2.0 毫秒，原 5–9 秒 IPv6 resolver 回退长尾消失。

阶段 D 使用当前 Codex 任务的后台心跳覆盖 2026-09-04 23:45 Actuals 窗口和下一个周频预测
窗口。关闭前必须补齐两个窗口的数据库水位、launchd 退出状态、Dashboard 构建结果、公网 health、
Nginx error 和 tunnel 重连证据；未覆盖两个自然窗口前，本记录不把长期观察标记为完成。
