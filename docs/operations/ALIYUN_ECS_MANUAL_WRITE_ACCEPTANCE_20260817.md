# 阿里云 ECS 手工写库验收报告（2026-08-17）

## 1. 结论与范围

本报告记录专用 ECS 上的 timer-disabled 部署与手工真写库验收。验收使用 release
`ab5fc0f940e1134201b27b330aef0e8ca6c1101d`，`/opt/bond-factor-lab/current`
指向对应不可变 release 目录。

本轮已经完成：

- Liwei Phase A 缓存结果复用与 Linux `input_state` 重绑定；
- DataBridge、daily、weekly、monthly、Actuals 的一次性 systemd 服务执行；
- 56 个 active base scheme 的最新调度运行与数据库结果读回；
- 私有 Backend 启动和 loopback 健康检查。

本轮明确没有执行：

- 没有启用任何 systemd timer；
- 没有运行 macOS/Linux 数值 CompareGate；
- 没有配置 Nginx、DNS、安全组或公网流量；
- 没有运行历史回测或自动 backfill；
- 没有让 9 个 paused Native 执行。

## 2. Release 与缓存

- release commit：`ab5fc0f940e1134201b27b330aef0e8ca6c1101d`
- release archive SHA-256：
  `9786f14969636f5bf9e8b20fb47a2c2cd2f79df84342d32d7cf5ce174683f9b9`
- 缓存迁移 receipt SHA-256：
  `eb9055df882f1df158067d4511b592723dbe6ebe19de6e8ab07c56246818b45d`
- Linux cache root：
  `/var/lib/bond-factor-lab/cache-builds/linux-x86_64-20260817-v1/liwei_0616`
- 7 个 cache family 均创建不可变 `migration_rebind` 子 generation；父 generation
  保留，父子语义字段与逐字段 SHA-256 相同。
- 正常消费者路径复核结果为 7/7 `status=hit`、`build_mode=hit`，
  `training_calls=0`。

本轮只复用已有缓存结果并重绑定 Linux 输入身份，不声称 Linux 重算结果与 macOS
数值等价。

## 3. Registry 与数据库运行结果

终态 Registry：

| 状态 | composite 数 | base 数 |
| --- | ---: | ---: |
| active | 60 | 56 |
| paused | 9 | 9 |

active base 按 cadence 分布：daily 39、weekly 12、monthly 5。9 个 paused base 在
本次新 run 范围内的运行数为 0。

最新运行验收：

| Cadence | Runtime | 成功方案 | 写入预测 | 验收日期 | 最新 run IDs |
| --- | --- | ---: | ---: | --- | --- |
| daily | Blackbox V2 | 25 | 25 | 2026-08-17 | 3202–3226 |
| daily | Native | 14 | 18 | 2026-08-17 | 3188–3201 |
| weekly | Blackbox V2 | 9 | 9 | 2026-08-15 | 3239–3246、3248 |
| weekly | Native | 3 | 3 | 2026-08-15 | 3247、3249、3250 |
| monthly | Blackbox V2 | 5 | 5 | 2026-08-15 | 3251–3255 |

56 个 active base 的最新 run 全部为 `success`；latest non-success 数为 0，数据库
`running` run 数为 0。

预测结果读回：

| Cadence | 预测行 | distinct base | feature_date | target_date |
| --- | ---: | ---: | --- | --- |
| daily | 43 | 39 | 2026-08-14 | 2026-08-17 至 2026-08-21 |
| weekly | 12 | 12 | 2026-08-14 | 2026-08-21 |
| monthly | 5 | 5 | 2026-08-14 | 2026-09-15 |

## 4. DataBridge authority

终态 canonical DataBridge：

- generation：`full-20260817-140120-dc0d9d08b57a`
- `refresh_date=2026-08-17`
- source `feature_date=2026-08-14`
- business digest：
  `dc0d9d08b57a4b1319cafa9e698d19d6d6bd721f966bd3441f16848039413a31`
- gate：`ready`
- publication manifest SHA-256：
  `fe812524f11cd1a2272c4cd51ccfa49afc667683be7c02c859716b75b675aa69`

为在 2026-08-17 手工验收合法周六/月中日期，创建了隔离 authority：

- 路径：`/var/lib/bond-factor-lab/manual-databridge-20260815-v2`
- generation：`full-20260815-204342-dc0d9d08b57a`
- `refresh_date=2026-08-15`
- source `feature_date=2026-08-14`
- business digest 与 canonical 相同；gate 为 `ready`；根目录均为 root-owned 0700。

第一次创建隔离 authority 时，transient unit 的 `EnvironmentFile` 优先级覆盖了
`--setenv` 路径，8 月 15 日 authority 曾短暂发布到 canonical。所有 timer 和其它
writer 当时均为关闭状态。原始 8 月 17 日 authority 已预先完整复制到隔离 v1；随后：

1. 把有效的 8 月 15 日 authority 复制并严格验收为隔离 v2；
2. 通过 `DataBridgeStore.publish` 从 v1 原子恢复 canonical 8 月 17 日 authority；
3. 重写并读回 8 月 17 日 ready gate；
4. 读回确认 canonical generation、business digest 和 manifest SHA-256 均恢复原值；
5. weekly/monthly 只通过 root-only 临时环境读取隔离 v2，完成后删除临时环境文件。

保留的手工验收制品没有被任何 installed service 引用：

- `/var/lib/bond-factor-lab/manual-databridge-20260815-v1`
- `/var/lib/bond-factor-lab/manual-databridge-20260815-v2`
- `/var/lib/bond-factor-lab/databridge-restore-candidate-20260817-v1`

## 5. 失败尝试的审计保留

数据库未删除或改写失败历史：

- 2026-08-17 的 16 个 Native 失败 run：包括初始数据库配置失败、首次 10Y 超时和
  人工停止的 5Y 缓存 miss 运行；后续对应最新 daily run 全部成功。
- 2026-08-15 的 9 个 weekly Blackbox 失败 run：首次手工运行使用
  `refresh_date=2026-08-17` 的 current 回放 8 月 15 日，被
  `refresh_date == predict_date` 安全门拒绝；隔离 8 月 15 日 authority 建立后，
  对应 9 个最新 run 全部成功。

这些失败行属于可审计操作历史；最终验收以每个 active base 的最新精确运行为准。

## 6. Actuals

`bond-factor-lab-actuals.service` 以 exit 0 完成。日志报告：

- daily records：14,155
- weekly records：5,954
- monthly 本次 upsert records：684

终态表水位：

| 表 | 行数 | tenor 数 | 最大业务日期 | 本次更新时间已推进 |
| --- | ---: | ---: | --- | --- |
| `t_scheme_actuals` | 14,155 | 5 | 2026-08-14 | 是 |
| `t_scheme_weekly_actuals` | 5,954 | 5 | 2026-08-14 | 是 |
| `t_scheme_monthly_actuals` | 814 | 5 | 2026-08-14 | 是 |

行数未被伪造扩大；updater 对既有业务键执行幂等 upsert。

## 7. systemd 与 Backend 终态

一次性服务：

| Service | 终态 | Result | ExecMainStatus |
| --- | --- | --- | ---: |
| DataBridge | inactive/dead | success | 0 |
| daily prediction | inactive/dead | success | 0 |
| weekly prediction | inactive/dead | success | 0 |
| monthly prediction | inactive/dead | success | 0 |
| Actuals | inactive/dead | success | 0 |
| Backend | active/running | success | 0 |

五个 timer 均为 `disabled` 且 `inactive`：DataBridge、daily、weekly、monthly、
Actuals。`/run/bond-factor-lab/manual-run.env` 已删除。

Backend 只监听 `127.0.0.1:8100`；`/api/health` 返回 `status=ok`，
`daily_schedule.mode=systemd_one_shot`。没有配置或开放公网入口。

## 8. 终态安全检查

- 无 `systemd_prediction_runner`、`scheme_runner`、DataBridge refresh 或 rebind
  孤儿进程；
- 本次时间窗口内内核日志无 OOM/oom-kill；
- 根文件系统使用率 61%，剩余约 15 GiB；
- `systemctl --failed` 只显示 ECS 预存的 `aegis.service` 失败，和本次应用部署无关；
- canonical DataBridge 与隔离 authority 均通过 strict read 与 ready gate 校验；
- Backend 是唯一保持运行的本项目服务，且仅绑定 loopback；
- 所有业务 writer timer 继续关闭。

## 9. 后续独立授权项

timer-disabled 部署与手工真写库验收已经完成。以下动作仍需独立授权和切换窗口：

- 启用 DataBridge/prediction/Actuals timers；
- 配置 Nginx/TLS、DNS 或公网流量；
- 停用或切换其它主机上的生产 writer；
- 清理本报告列出的隔离验收制品；
- 任何跨平台数值 CompareGate 或算法变更。
