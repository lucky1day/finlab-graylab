# 阿里云 ECS 灰度 release 手工验收报告（2026-08-18）

## 1. 结论与边界

ECS 已安装并切换到单一源码 release
`a749b17d5ad3e3f248a1cb788d892aa36d30f518`。DataBridge、daily、weekly、monthly、
Actuals 五个 one-shot 均以 exit 0 完成手工真写库；Backend 在 `127.0.0.1:8100`
健康运行。ECS effective discovery 为 56 base / 60 composite，Registry 保持
60 active / 9 paused，9 个 Mac-only base 在本次 run 范围内零新增。

本轮仍然明确没有执行：

- 没有启用任何 systemd timer；
- 没有修改 Mac3、`master`、launchd、Nginx、DNS、域名或生产流量；
- 没有运行 macOS/Linux 数值 CompareGate；
- 没有执行历史回测、自动 backfill 或手工删除数据库审计行；
- 没有把 Backend 设置为开机自启。

因此本报告关闭的是“新 release 安装 + timer-disabled 手工真写库”验收，不构成
自然灰度 timer 的启用授权，也不构成生产切流授权。

## 2. Release 与回滚材料

- release commit：`a749b17d5ad3e3f248a1cb788d892aa36d30f518`
- archive：`bond-factor-lab-a749b17d5ad3e3f248a1cb788d892aa36d30f518.tar.gz`
- archive bytes：`13,912,171`
- archive SHA-256：`3daefba1e88f6ac6cf07a315a6904df7a3f5608c9e7d2948564a1f5deb1e185e`
- current：`/opt/bond-factor-lab/releases/a749b17d5ad3e3f248a1cb788d892aa36d30f518`
- previous release：`/opt/bond-factor-lab/releases/ab5fc0f940e1134201b27b330aef0e8ca6c1101d`
- unit backup：`/opt/bond-factor-lab/unit-backups/a749b17d5ad3e3f248a1cb788d892aa36d30f518`

旧 release 和六个旧 service 文件均已读回存在。installed service 与新 release 模板
逐字一致；六个 installed unit SHA-256 为：

| Service | SHA-256 |
|---|---|
| Backend | `8d55174fe4d7e3694f531d0fa47b4010efe84cce7c677a41eb6f517971f00ccc` |
| DataBridge | `ee05ef779ced7a025d4ef953f9fffb2a32fd345496aef94e74d4d51fd9fe99cb` |
| Actuals | `2fb4456d37b7912945e38092317f668a0b7e5a9119c6c5be03f111e9e5da2cd4` |
| daily | `b307c5660fbccef40a89d367f13eeeaaf20ccd8b307a7a47100252495eecfcda` |
| weekly | `df9e6c7e0581315032ac20b9354fc9c199582fb09608fe1b98c9804f079461e2` |
| monthly | `dbf70e487102b5e93ecbc449285999c73feb69edad9534f1c81093f4f658462c` |

## 3. 手工批次结果

| 批次 | 验收日期 | 成功 base | 预测行 | run IDs | 墙钟 | Peak RSS |
|---|---|---:|---:|---|---:|---:|
| DataBridge | 2026-08-18 | — | — | — | 3 分 4 秒 | 1.4 GiB |
| daily | 2026-08-18 | 39 / 39 | 43 | 3256–3294 | 49 分 33 秒 | 1.9 GiB |
| weekly | 2026-08-15 | 12 / 12 | 12 | 3295–3306 | 7 分 34 秒 | 774.8 MiB |
| monthly | 2026-08-15 | 5 / 5 | 5 | 3307–3311 | 1 分 51 秒 | 501.5 MiB |
| Actuals | 2026-08-18 | — | — | — | 29 秒 | 79.9 MiB |

三个预测批次均为 `failed=[]`、`skipped=[]`、`denied=[]`，所有预测均为
`scheduled_live`。数据库读回确认每个 run 的 `records_written` 与 prediction linkage
一致，60 条预测对应 60 个不同业务键，没有批次内重复；数据库残留 `running` run 为 0。

## 4. Liwei 缓存的实际语义

本次 daily 前，7 个 family 的迁移缓存水位为 `feature_date=2026-08-14`；当天 live
输入要求 `feature_date=2026-08-17`。因此第一次消费者不可能对尚未出现的交易日做纯 hit。
实际行为是：

- 7 / 7 family 均复用原 `migration_rebind` parent generation；
- 每个 family 只对唯一缺失日期 `2026-08-17` 执行 `build_mode=append`；
- 没有 `full rebuild`，没有再次执行 `migration_rebind`；
- 同 family 后续三个消费者均为 `status=hit`、`missing_dates=[]`；
- 终态仍为 7 个 `current.json`、21 个 manifest、81 个 pickle、0 个 `.invalid-*`；
  新 generation 进入后由既有 retention 淘汰旧代，文件总数保持稳定。

原实施计划把新交易日也要求为“7 / 7 纯 hit、`training_calls=0`”，与移动水位缓存的
正常每日语义不一致。发现 append 后按 fail-closed 规则暂停了 weekly/monthly；用户随后确认
49 分钟 daily 耗时可以接受并明确授权继续。最终验收采用“复用 parent + 只 append 唯一新
feature date + 禁止 full rebuild”的口径，不把本次结果误写成纯 hit。

## 5. DataBridge authority

canonical DataBridge 终态：

- generation：`full-20260818-130421-9cdf0632d47a`
- `refresh_date=2026-08-18`
- source cutoff：`2026-08-17`
- business digest：`9cdf0632d47a1b3fa5cdbc9b8def2e69d0abfc63bb4952f517c63a219a60d0ef`
- gate：`ready`

weekly/monthly 使用既有隔离 authority
`/var/lib/bond-factor-lab/manual-databridge-20260815-v2`：

- generation：`full-20260815-204342-dc0d9d08b57a`
- `refresh_date=2026-08-15`
- source cutoff：`2026-08-14`
- gate：`ready`

当前 release 的严格读分别验证了两套 authority。隔离路径只经 root-only
`/run/bond-factor-lab/manual-run.env` 临时注入 weekly/monthly，执行后该文件已删除；installed
service 和 timer 均不引用隔离路径，canonical 在两批执行后仍保持 2026-08-18。

release archive 中 DataBridge 根目录只有 tracked README，解压 mode 为 0775，且环境配置把
data/runtime root 绑定在 `/opt/bond-factor-lab/current` 下。为避免新 release 全量重建，部署时先把
旧 release 已通过新代码严格校验的 current/runtime 原样继承到新 release，并把 data root 收紧为
0700，再执行当天发布。当前 release 可稳定运行；但未来再次切 release 前仍需重复 handoff，或另行
批准把 DataBridge 运行根迁到 `/var/lib` 持久路径。该维护性问题不影响当前固定 release 的自然运行，
但必须在下一次 release 部署计划中显式处理。

## 6. Registry、范围与 Actuals

终态范围：

| 状态 | composite | distinct base |
|---|---:|---:|
| active | 60 | 56 |
| paused | 9 | 9 |

本轮 run 起点 3256 之后，9 个 Mac-only base 的 run 和 prediction 均为 0。current release
以 `BFL_DEPLOYMENT_TARGET=aliyun-gray` 严格 discovery 得到 56 / 60。

Actuals 终态：

| 表 | 执行前行数 | 执行后行数 | 最大业务日期 |
|---|---:|---:|---|
| `t_scheme_actuals` | 14,155 | 14,160 | 2026-08-17 |
| `t_scheme_weekly_actuals` | 5,954 | 5,954 | 2026-08-14 |
| `t_scheme_monthly_actuals` | 814 | 814 | 2026-08-14 |

日频源水位新增一个交易日，因此五个 tenor 各增加一行；周频/月频源水位无新目标日，按现有
业务键幂等保持。任务日志报告 daily 14,160、weekly 5,954、monthly upsert 684，未构造缺失数据。

执行后的 `check_production_daily_health.py` 返回 `status=ok`、`findings=[]`。

## 7. systemd、Backend 与资源终态

| Service | Result | Exit | 总时限 | 停止宽限 |
|---|---|---:|---:|---:|
| DataBridge | success | 0 | 1h | 5min |
| daily | success | 0 | 2h | 5min |
| weekly | success | 0 | 2h | 5min |
| monthly | success | 0 | 2h | 5min |
| Actuals | success | 0 | 1h | 5min |

五个 timer 全部 `disabled/inactive`。Backend 为 `active/static`，仅监听
`127.0.0.1:8100`；`/api/health` 返回 `status=ok`，调度显示
`mode=systemd_one_shot`、`overall=not_enabled`，与 timer-disabled 阶段一致。

终态安全与资源：

- `/run/bond-factor-lab/manual-run.env` 不存在；
- 无 prediction runner、scheme runner、DataBridge refresh 或 rebind 孤儿进程；
- 本轮内核日志无 OOM / oom-kill；
- 根盘 40 GiB，已用约 23 GiB，可用约 15 GiB，使用率 62%；
- 内存 14 GiB，验收结束时约 13 GiB available；
- 唯一 failed unit 是部署前已经存在的 `aegis.service`，与本项目无关。

## 8. 保留的失败尝试

为保证审计完整，本轮没有删除或掩盖以下失败：

1. 第一版切换命令错误使用了简写 unit 名和 8000 端口；rollback trap 成功恢复旧 current、
   六个旧 service 和 Backend，未启动数据库 writer。修正为 canonical unit 名和 8100 后切换成功。
2. 新 release 初始 DataBridge data root 为 0775 且没有 current；严格健康检查 fail-closed。
   经新代码验证旧 current 后执行上述继承和权限收紧。
3. 13:00 的第一次 DataBridge 手工启动因默认 06:55 deadline 快速拒绝，在读取数据库前退出。
   第二次只通过 root-only manual env 临时把本次 deadline 设为 23:59，发布成功后立即删除。
4. Actuals 启动前的只读水位查询误用了 `target_tenor`；SQL 在 `systemctl start` 前失败，Actuals
   未运行。按实际 schema 改用 `tenor` 和对应日期列后，one-shot 一次成功。

这些记录不改变终态成功证据，也没有授权手工删除任何业务行或审计行。

## 9. 下一道独立授权门

当前可以提交用户审阅是否启用五个 ECS timer，进入独立灰度自然运行。启用前不需要再次重跑
本次 daily/weekly/monthly；启用动作必须单独授权，并在启用后立即读回 next trigger、时区、
`Persistent=false`、Backend 和 Registry。Mac3、域名、Nginx、DNS 与生产流量继续保持不变。
