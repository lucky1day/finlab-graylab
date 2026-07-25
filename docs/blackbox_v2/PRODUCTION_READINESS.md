# Blackbox V2 生产晋级条件

**文档状态**：`BLOCKED_DRAFT`
**适用运行时**：`blackbox_v2`
**目标读者**：平台开发、运维、风险控制和授权人员
**最后核验日期**：2026-07-26

本文不是可执行的生产 SOP。它只列出 Blackbox V2 从 `shadow + paused` 晋级为 `active/live` 前必须完成并验证的阻塞项。

> PR-01 至 PR-08 的代码前置项已完成生产路径认证。`weekly_10y_lgbm_point_v1` 和 1Y T+5 批次四个指定方案于 2026-07-20 分别获得专项授权并进入生产灰度，但真实交付覆盖门槛仍未满足；本文件仍不是面向所有新方案的通用生产授权。

## 1. 当前边界

当前已具备并完成隔离认证：

- 两文件 Intake、Metadata 和目录身份校验。
- DataBridge 三频同代快照、freshness 证据及 Request 生成。
- 七个自动 Gate、10 轮重复运行和 100/101/500/1000 条 no-persist 回测。
- 正式授权的 `shadow + paused` 登记、ActivationGate 和生命周期恢复。
- 正式授权的日期区间完整回测：范围绑定 token、单批最多 100 条、跨批总预算、单一事务落库及 API/前端读取；1Y T+5 四方案的 canonical latest 已切换为 run `178..181`，各 333 条 / 17 个月且全部 `target_date < 2026-06-01`。
- gray live、scheduler live、actual join、真实 API 探针和前端显示。
- sandbox 文件读取 allowlist、环境清理及严格 JSON Result 契约。
- stale generation 非零退出、零预测副作用和暂停后立即隐藏。

当前仍不具备广义 `PRODUCTION_READY` 结论：

- 当前有两个真实上游交付批次：`10Y + weekly_point + LightGBM`，以及同一 ZIP 中的四个 `1Y + T+5 + daily` 算法；后者不能计作四个独立交付包。
- 尚未用独立真实交付覆盖月频和周平均，真实交付总批次数仍少于 3。
- 当前 `weekly_10y_lgbm_point_v1` 与四个 1Y T+5 指定方案获得专项生产灰度授权；四个日频方案目标日 `2026-07-24` 的首条 gray-live actual 已入库并完成数据库 join，API 和前端准确率仍需按当前结果复验。
- 四个日频方案均已 active；截至 2026-07-26，每方案各有 40 条连续 `gray_live`，`LIQ_EXCESS_A`、`LIQ_EXCESS_A_W252_L7`、`LIQ_EXCESS_A_W350_L7`、`LIQ_EXCESS_B_W252_L7` 的 `scheduled_live` 数量依次为 `3/2/2/0`。前三个方案已有自然调度证据，第四个仍无正式结果，因此批次尚未整体通过 `Production Observed`。
- 原 06:00/06:30/06:35/07:00 V2 preflight + scheduler restart
  路径已被 2026-07-24 的架构复审判定为不适合日频 SLA，ledger 模式下已经
  fail-closed 退休。候选路径在 06:30 由单一 occurrence coordinator 同时构建
  Native 和当天全新 DataBridge generation；V2 只在 DataBridge SEALED 后按
  `+0/+2/+4/+6` 独立释放。
- 在 ledger 获准切换前，仓库 launchd 仍以 legacy mode 保留上述 preflight，
  因为它是 legacy 跨日运行的每日 DataBridge refresh owner；切换必须先 unload
  preflight，并同步切换/restart backend 与 scheduler，不能形成双 owner。
- 日频候选协调器仍处于“改造/观测中”：生产 rollout 保持 legacy，迁移、
  三个 Native 输入适配、容量门禁、故障注入和连续 10 个交易日 25/25
  尚未完成，不能宣称 08:00 SLA 稳定。
- 四个日频方案已通过专用 `gray-backfill` Gate 补齐 `target_date >= 2026-06-01` 的连续 `gray_live`，历史/live target overlap 为 0，API 和前端阶段分界已验收并达到 `Onboarding Complete`。2026-07-24 旧 scheduler 仅在 11:23 为 `LIQ_EXCESS_A` 产生晚到结果，另外三个正式任务缺失；这既不能补足第四个方案的 `Production Observed`，也不能作为 08:00 SLA 稳定证据。
- 这些专项激活不代表 Blackbox V2 已获得面向任意新交付的通用生产授权。

## 2. 必须完成的阻塞项

| 编号 | 前置条件 | 状态 | 2026-07-20 认证证据 |
|---|---|---|---|
| PR-01 | Gate 绑定 generation/freshness | `PASS` | all-stage、持久化回测和 live 绑定 generation、snapshot、digest；stale generation fail-closed |
| PR-02 | Runtime Profile 成为唯一配置源 | `PASS` | `blackbox-v2-v1` 解析、环境指纹、资源与权限漂移测试通过 |
| PR-03 | Harness 审计持久化 fail-closed | `PASS` | Shadow、Activation 和持久化回测均校验最新通过 run、精确版本和签名授权 |
| PR-04 | Shadow 原子化或自动 reconciliation | `PASS` | journal、补偿、reconciliation、并发锁和幂等重试测试通过 |
| PR-05 | Blackbox 专用 activate/live 门禁 | `PASS` | 正式 ActivationGate、gray live、scheduler live 通过；未批准和 paused 路径被阻断 |
| PR-06 | 真实 Registry/API/scheduler 探针 | `PASS` | 真实服务 fingerprint、Registry、schemes、metrics、backtest 和 scheduler 证据一致 |
| PR-07 | JSON Result 严格整数 | `PASS` | JSON 字符串、布尔和浮点方向拒绝；整数 `-1/0/1` 接受 |
| PR-08 | Sandbox 读取与环境收紧 | `PASS` | 外部文件、继承密钥、网络、data-dir 写入和非授权路径均被阻断 |

## 3. 生产 SOP 形成条件

上述八项已全部完成。形成 `CURRENT` 生产 SOP 前仍需单独完成：

1. 再取得至少一个独立真实上游交付包，使总批次数不少于 3 个，并补齐月频覆盖。
2. 对每个真实交付重复执行 Intake、10 轮 Gate、回测落库、live、actual、API、前端和失败恢复矩阵。
3. 确认不同依赖栈在冻结 Runtime Profile 中可运行，或以版本化 Profile 显式管理，不临时安装依赖。
4. 由业务、平台和运维审核认证证据并明确通用生产激活、暂停和回退责任人。
5. 将平台生产晋级操作文档标记为 `CURRENT` 后，才允许把生产授权作为后续新方案的标准流程；现有专项灰度授权不得作为自动放行依据。
6. 每个生产日保存 occurrence、冻结 Registry digest、Native/DataBridge
   generation/digest、四个 V2 release/start/accepted 时间、21/25 验收账本和
   write-once SLA 结果；不再以 scheduler PID 切换或旧
   `v2-scheduler-gate-v1` 凭证作为当前日频生产证据。
7. 完成同一 Mac Studio 的 20 次 forced-cold、20 次真实 revision/suffix、
   故障注入和连续 10 个交易日 25/25；forced-cold P95 不高于 80 分钟、
   最大值不高于 85 分钟。

本文件在此之前保持 `BLOCKED_DRAFT`。这里的阻塞来自真实方案覆盖和上线授权，不再来自 BBV2-01 至 BBV2-07 的代码能力缺失。

## 4. 每次复核记录

复核只在本表追加，不覆盖历史结论：

| 日期（Asia/Shanghai） | 复核范围 | 完成项 | 未完成项 | 结论 |
|---|---|---|---|---|
| 2026-07-19 | 初始治理基线 | Intake 至 shadow 文档边界 | PR-01 至 PR-08 | 阻塞，维持 shadow + paused |
| 2026-07-20 | BBV2-01 至 BBV2-07 修复后隔离全链路 | PR-01 至 PR-08；正式 Activation、persist、live、actual、API、前端和恢复 | BBV2-08：至少 3 个真实交付覆盖日/周/月 | `PRODUCTION_PATH_READY`，尚未 `PRODUCTION_READY` |
| 2026-07-20 | 真实交付生产灰度激活 | 生产 ActivationGate、100 条回测落库、1 条 gray live、Registry/API/前端/scheduler 可见 | 首条 actual 待 2026-07-24；仍缺另外 2 个真实交付和日/月频覆盖 | 当前方案 `PRODUCTION_GRAY_ACTIVE`；平台总体仍未 `PRODUCTION_READY` |
| 2026-07-20 | 1Y T+5 日频批次代表性 Canary | 10 轮稳定性、Activation、100 条回测、gray live、本地/公网 API 和前端；其余三方案保持 shadow | 下一交易日自然 scheduled live、2026-07-24 actual、其余三方案分阶段激活；仍缺月频和第 3 个独立交付批次 | Canary `CANARY_GRAY_ACTIVE`；平台总体仍未 `PRODUCTION_READY` |
| 2026-07-20 | 1Y T+5 日频批次四方案专项全量激活 | 用户明确调整时序；四方案 active、各 100 条回测、各 1 条 gray live；短名称 API/前端与公网 14/14 通过 | 下一交易日四方案自然 scheduled live、2026-07-24 actual；仍缺月频和第 3 个独立交付批次 | 四方案 `GRAY_ACTIVE`；平台总体仍未 `PRODUCTION_READY` |
| 2026-07-20 | 1Y T+5 四方案完整历史刷新 | 持久化默认起点 `2025-01-01`；起止范围绑定授权；四方案各 367 条、19 个月、`100/100/100/67` 分批原子写入；授权范围、批次预算和实际日期范围已写入 durable run summary；API、前端同月合并和公网复验通过 | 下一交易日四方案自然 scheduled live、2026-07-24 actual；仍缺月频和第 3 个独立交付批次 | 四方案继续 `GRAY_ACTIVE`；canonical latest-success 已切换为完整 run 174–177，先前 run 均 immutable 保留 |
| 2026-07-20 | 1Y T+5 灰度/前端口径复核 | 对照 Native V1 和共享预测语义，将 `gray_target_start=2026-06-01`、部署即进入实盘、gray 回补和前端 phase 展示写入 V2 平台 SOP | run 174–177 含 gray target，四方案仅各 1 条 gray live，需追加正确历史 run 并补齐连续 gray live 后重验前端 | 撤销此前同月 backtest/live 并存的验收结论；四方案 active，但尚未 Onboarding Complete |
| 2026-07-20 | 1Y T+5 历史/灰度分区最终收口 | canonical run 178–181 各 333 条/17 月；专用 exact-date HMAC `gray-backfill` 以 insert-only 补齐每方案 38 个历史缺口；最终每方案 39 gray、0 scheduled、target overlap=0；API/前端显示 4 个短名称且 2026-06 为灰度目标期起点 | 下一交易日自然 `scheduled_live`、持续 actual 观察；仍缺月频和第 3 个独立交付批次 | 四方案 `ONBOARDING_COMPLETE`；尚未 `PRODUCTION_OBSERVED`，平台总体仍未 `PRODUCTION_READY` |
| 2026-07-21 | V2 日级时间管理与旧进程覆盖整改 | 安装 06:00/06:30/06:35/07:00 preflight；V2-only day credential；成功才重启、失败不补跑；单任务不再同步 Registry；部署重启以 blocked 凭证拒绝四个 V2 catchup；17/17 Native V1 正常；本地/公网 API 恢复短名称 | 下一交易日自然四阶段、自动重启和四方案 `scheduled_live` 待观察 | 控制已安装，V1/V2 故障域已隔离；四方案仍为 `ONBOARDING_COMPLETE`，尚未 `PRODUCTION_OBSERVED` |
