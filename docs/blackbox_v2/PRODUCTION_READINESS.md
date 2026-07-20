# Blackbox V2 生产晋级条件

**文档状态**：`BLOCKED_DRAFT`
**适用运行时**：`blackbox_v2`
**目标读者**：平台开发、运维、风险控制和授权人员
**最后核验日期**：2026-07-20

本文不是可执行的生产 SOP。它只列出 Blackbox V2 从 `shadow + paused` 晋级为 `active/live` 前必须完成并验证的阻塞项。

> PR-01 至 PR-08 的代码前置项已完成生产路径认证。`weekly_10y_lgbm_point_v1` 和 1Y T+5 批次的代表性 Canary 于 2026-07-20 分别获得专项授权并进入生产灰度，但真实交付覆盖门槛仍未满足；本文件仍不是面向所有新方案的通用生产授权。

## 1. 当前边界

当前已具备并完成隔离认证：

- 两文件 Intake、Metadata 和目录身份校验。
- DataBridge 三频同代快照、freshness 证据及 Request 生成。
- 七个自动 Gate、10 轮重复运行和 100/101/500/1000 条 no-persist 回测。
- 正式授权的 `shadow + paused` 登记、ActivationGate 和生命周期恢复。
- 正式授权的 100 条回测事务落库及 API/前端读取。
- gray live、scheduler live、actual join、真实 API 探针和前端显示。
- sandbox 文件读取 allowlist、环境清理及严格 JSON Result 契约。
- stale generation 非零退出、零预测副作用和暂停后立即隐藏。

当前仍不具备广义 `PRODUCTION_READY` 结论：

- 当前有两个真实上游交付批次：`10Y + weekly_point + LightGBM`，以及同一 ZIP 中的四个 `1Y + T+5 + daily` 算法；后者不能计作四个独立交付包。
- 尚未用独立真实交付覆盖月频和周平均，真实交付总批次数仍少于 3。
- 当前 `weekly_10y_lgbm_point_v1` 与 `one_y_t5_liq_excess_a_w252_l7_v1` 获得专项生产灰度授权；二者首条 gray live actual 均要到目标日 `2026-07-24` 后才能验证。
- 日频 Canary 尚待下一交易日自然 `scheduled_live`；其余三个同批方案仍为 `shadow + paused`。
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

本文件在此之前保持 `BLOCKED_DRAFT`。这里的阻塞来自真实方案覆盖和上线授权，不再来自 BBV2-01 至 BBV2-07 的代码能力缺失。

## 4. 每次复核记录

复核只在本表追加，不覆盖历史结论：

| 日期（Asia/Shanghai） | 复核范围 | 完成项 | 未完成项 | 结论 |
|---|---|---|---|---|
| 2026-07-19 | 初始治理基线 | Intake 至 shadow 文档边界 | PR-01 至 PR-08 | 阻塞，维持 shadow + paused |
| 2026-07-20 | BBV2-01 至 BBV2-07 修复后隔离全链路 | PR-01 至 PR-08；正式 Activation、persist、live、actual、API、前端和恢复 | BBV2-08：至少 3 个真实交付覆盖日/周/月 | `PRODUCTION_PATH_READY`，尚未 `PRODUCTION_READY` |
| 2026-07-20 | 真实交付生产灰度激活 | 生产 ActivationGate、100 条回测落库、1 条 gray live、Registry/API/前端/scheduler 可见 | 首条 actual 待 2026-07-24；仍缺另外 2 个真实交付和日/月频覆盖 | 当前方案 `PRODUCTION_GRAY_ACTIVE`；平台总体仍未 `PRODUCTION_READY` |
| 2026-07-20 | 1Y T+5 日频批次代表性 Canary | 10 轮稳定性、Activation、100 条回测、gray live、本地/公网 API 和前端；其余三方案保持 shadow | 下一交易日自然 scheduled live、2026-07-24 actual、其余三方案分阶段激活；仍缺月频和第 3 个独立交付批次 | Canary `CANARY_GRAY_ACTIVE`；平台总体仍未 `PRODUCTION_READY` |
