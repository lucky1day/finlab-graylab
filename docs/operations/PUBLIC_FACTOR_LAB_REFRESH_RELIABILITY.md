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

## 维护与回滚

应用 release、ECS Nginx、Mac3 Clash 和 installed launchd 是独立变更单元。
修改前保存对应生效配置及只读基线，只回滚失败单元，不以重启数据库或 Writer 兜底。

- 发布使用 clean commit 和已验证 immutable archive，不从开发目录覆盖生产。
- SSH 使用固定 ECS IPv4、strict host key、有限超时与保活；host-key fingerprint 必须通过独立可信渠道核验。
  不以关闭校验或未核验的 ssh-keyscan 输出替代。
- 修改 tunnel 前检查 ECS 18100 唯一 listener 和当前会话；不得手工启动第二条 ssh -R。
- Nginx 配置先语法检查再授权 reload。Backend/入口变更后检查认证 Summary、X-Request-ID、
  X-Dashboard-Snapshot-ID、Server-Timing 和两端结构化日志。
- DNS 或代理故障按实际生效配置定位；修改前保留原值，不能照抄旧发布窗口的 DNS/Clash 配置。
- 故障注入只允许作用于经授权的读路径，不得停止 Writer、写库或制造预测缺口。
  入口预算耗尽时按 request ID 定位，不能继续放宽 timeout 掩盖问题。

### 回滚顺序

1. 前端或 Backend 异常：先确认上一不可变 release 与当前 schema/状态兼容，再受控回滚；不回滚或覆盖数据库事实。
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
