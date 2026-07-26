# 当前状态

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台运维和审计人员

**最后核验日期**：2026-07-26

本文只保留当前有效结论。较早的逐日状态、数据库快照和整改过程已冻结到[历史状态记录](records/status/README.md)。

## 当前政策

- Native V1 只维护版本化政策清单中的既有身份，不接受新方案或算法升级。
- 新算法、新方案 ID、新目标、新任务和替代版本一律通过 Blackbox V2 两文件交付。
- Blackbox V2 自动 Gate 通过不等于生产授权；每个方案仍需独立完成生产准备核验和专项授权。
- 具体方案的授权不得外推为后续新方案的默认权限。

## 平台状态

| 项目 | 当前结论 |
|---|---|
| Native V1 | 现有方案保持原 Registry、scheduler、数据库和历史结果，只做存量维护 |
| Blackbox V2 技术入库 | Intake、统一 DataBridge 输入、七个 Gate、预测和 no-persist 回测已形成稳定路径 |
| Blackbox V2 生产路径 | 已完成一个真实周频方案和同一上游批次四个日频方案的专项生产灰度激活；尚未形成面向任意新方案的通用生产授权 |
| 日频 08:00 保障 | `FUNCTIONAL_MVP_VERIFIED` 仅指受控 recorder 的 21/25 ledger 功能验证；真实 17+4 尚未联跑，08:00 SLA 与 07:55 容量均无证据，生产门禁仍阻断 |
| 平台总体评级 | `PRODUCTION_PATH_READY`，尚未取得覆盖所有任务和依赖的 `PRODUCTION_READY` |
| 新方案默认终点 | 先进入受控技术验收；生产运行必须逐方案专项授权 |

## 当前生产灰度方案

- `weekly_10y_lgbm_point_v1` 已专项授权进入生产灰度，并完成一次持久化回测和一次 `gray_live`；目标数据到达后仍需复验 actual join。
- 1Y T+5 四方案 `LIQ_EXCESS_A`、`LIQ_EXCESS_A_W252_L7`、`LIQ_EXCESS_A_W350_L7`、`LIQ_EXCESS_B_W252_L7` 已专项授权并达到 `Onboarding Complete`；每方案保留 333 条 canonical backtest 和 40 条连续 `gray_live`。
- 四方案批次尚未整体达到 `Production Observed`。同一上游批次的证据不能外推到独立交付、周平均或月频；完整证据见本节后的专项记录链接，当前生产行数不在状态文档中固化。

### 日频 08:00 整改状态

- 2026-07-24 旧 APScheduler 路径仅生成 16/21 个 run（13 success、3 failed、5 未运行），因此当前生产不能认定为稳定。
- 步骤 6 已完成：受控 recorder 在隔离 MySQL 精确展开 21 item/25 target，其中 Native 17 item/21 target、输入模式 14/3；双 lane、失败隔离、重入幂等、claim 单 winner、原子提交和 write-once watchdog 均通过。该功能验证没有执行真实 17+4 算法。
- 步骤 7 已完成：四个 V2 同代并按 `+0/+2/+4/+6` 独立释放，最大并发 2；
  四个真实 delivery 的冻结输入、确定性、120 秒超时、父 generation fence、
  late 后继续执行和失败隔离均通过。
- 2026-07-26 在候选 `2bf9f5f` 上重新认证四个真实 sealed delivery：同一 DataBridge generation 和 Native calendar parent 上各运行两次，完整 `PredictionRecord` 一致；mutable `current`、实时数据库、错误 parent ID/hash 均被拒绝，生产表行数未变化。
- `5aed35f` 与 `1f81f3e` 已建立 21/25 结构 gate 和 Engine-bound replay epoch：只接受父子摘要一致的同日 generation，完成 001–018、17/4 绑定，并在 claim/process/commit 重验隔离 Engine/Connection。
- `1f101b8`、`9a76586` 建立受保护的双池 replay runtime：唯一入口 `run()` 硬绑定 canonical executor；2 Native / 2 V2 受 governor 限制，owner 锁内每轮重验 21 个 execution envelope，并以线性化 stop fence 阻止异常后的跨池新任务。
  `5943b88`、`9b2624e`、`5b00981` 进一步要求 replay `predict_date` 必须是冻结日历中的交易日，并在 occurrence 创建、generation 注册和 runtime 构造三个边界重新打开、rehash 和比较完整 generation context；磁盘 payload、内存审计字段或非法嵌套 context 均在 ledger 写入前以稳定错误拒绝。结果仍为 `EXCLUDED`；尚未执行真实 21 算法，也不与生产 scheduler 共锁，不构成 SLA/容量证据。
- `08d9827` 已增加 `python -m harness daily-real-replay --check-only` 瞬时业务数据只读预检：双读 generation/候选，校验 21/25、14/3/4、生产 migration/Registry/version、source-readonly、控制面和全局静默；结果固定为 `CHECK_PASSED + qualification=EXCLUDED`，本地只保留 `0600` fence 文件。`04cb709` 将共用探针下沉到 `scheduler` 并禁止 `harness -> scripts`。
- `b00d381` 将 operator/runtime 双锁提升为同进程可验证会话：绑定创建 PID、固定路径和设备/inode，内部预检只借用既有锁而不重抢或提前释放，成功路径首尾验锁。
- `b99e24f` 已让真实 replay runtime 复用该会话持有的同一 runtime 锁：借用入口和核心执行入口都会在任何 DB/快照/线程池副作用前验证真实 session capability；伪造对象、跨 PID、已释放、路径或 inode 漂移均 fail-closed，成功和异常路径都不替外层 acquire/release。在该阶段 execute CLI 与逐 dispatch 运行中 fence 尚未提供。
- `349475f` 进一步把 exact operator session 绑定到 runtime 运行态，并在主线程 submit 前、worker 进入 canonical claim 前复验；借用模式省略或替换 session 均在 DB/claim 前拒绝。该提交只完成 session fence，不代表候选、generation、控制面和 replay-aware 进程动态 fence 已完成。
- `0d1b70d` 在成功的二次预检后为同一锁会话一次性绑定脱敏 dispatch identity，覆盖候选、generation、21/25 定义、控制面、生产 migration/Registry/version、source 连接身份和起止水位；未绑定、重复绑定或 session 已释放均拒绝。该提交只建立 write-once 基线，在该阶段尚未逐 dispatch 重读。
- `cab7b0d` 提供 dispatch identity 动态重读：重新打开两份 manifest，并复核候选 Git/policy、21/25 定义、legacy/BLOCKED、生产 001–017/Registry/version 及 source endpoint/principal/table；source 水位继续允许前进。该提交只提供 helper，在该阶段尚未接入 runtime submit/claim。
- `5129451` 已把动态 identity 重读线性化接入主线程 submit 前和 worker canonical claim 前；submit 漂移与 submit 后/worker 前漂移都会发布 stop fence、返回 `recovery_blocked`，且零 attempt/claim。该提交只完成 identity fence；OS process boundary 与最小 start-window 后续由 `ed0f362`、`8b59489` 和 `3c24ec6` 闭合。
- `c76e96c` 新增 repository 只读查询，只从本次 replay occurrence、runtime 当前 active future 和 current `operator_recovery + scheduled_live` running attempt 返回已登记 leader PID/PGID；查询闭合 scheme/version/runtime/date/state/token 以及 `PID=PGID>1`。这是进程 allowlist 的账本身份底座，不是 OS 进程探针；`ed0f362` 消费该查询，`3c24ec6` 完成 runtime 接线。
- `ed0f362` 已完成隔离 replay 专用 OS process-boundary probe：以账本 allowlist 和 fail-closed 进程表快照区分当前已登记 leader/进程组、operator 后代、旧或未登记后代、外部日频平台进程及 UID 漂移。该提交先落地独立只读探针，后由 `3c24ec6` 接入 real replay runtime 的每次 dispatch。
- `8b59489` 已完成 canonical `ProcessStartGuard`：`DefaultDailyRuntimeServices` 内的单一共享实例同时约束 Native/Blackbox，在 pre-fence、`Popen`、PID/PGID 登记和 post-fence 之间串行化；长时间 communicate/poll 在锁外。登记窗口清理未确认会 poison guard 并在后续 `Popen` 前 fail-closed，普通 `Lock` 被生产入口拒绝。
- `3c24ec6` 已把上述 OS probe 与 canonical `ProcessStartGuard` 接入 real replay 的每次 dispatch：item 在 submit 前精确预留并进入 active snapshot，submit 前、worker 入口、attempt claim 前以及每次 `Popen` 前和进程组登记后均 fail-closed 复核。probe report 必须是精确 `ReplayProcessBoundaryReport`，且 service UID、occurrence 和 active item 与同一快照完全一致；claim 前 process fence 失败保持零 run、零 claim，claim 后且进程组清理确认的 fence 失败返回 `recovery_blocked`。若清理无法确认则走 `ProcessGroupTerminationError`：completion fence 持久化成功返回 `fenced_pending_cleanup`，持久化失败才返回 `recovery_blocked`。所有路径均发布 stop fence 并释放 reservation。该提交未提供 execute-only CLI，也未执行真实 17+4 联跑。
- `93de8ad` 建立隔离 replay MySQL 生命周期：新 datadir/UUID、loopback 非 3306、固定安全参数、唯一 schema/账号、per-connection guard，以及 Engine→进程→fd 锚定目录的异常安全清理；不含 migration、Registry 或算法执行。
- 当前真实环境预检仍会 fail-closed：已安装的 backend LaunchAgent 尚未携带合法
  coordinator mode，因此控制面检查返回 `CONTROL_PLANE_BOUNDARY_UNAVAILABLE`。
  本轮未修改已安装 plist、未 bootout/kickstart 服务；只能在获准的独占维护窗口
  修复后重新检查。
- 当前全量验证为 `2744 passed, 13 skipped`（836 个 subtest）；显式启用真实临时 MySQL 的 replay gate/runtime 为 `77 passed`（17 个 subtest）。这些是代码与隔离功能证据，不是 execute-only 真实 17+4 联跑或容量证据。
- 候选已实现 06:30 readiness 后单次冻结、Native/V2 双池、三层账本、attempt fence、原子提交和动态 21/25 健康投影；07:45/08:00 可幂等补写，后续源数据修正不重启当前 occurrence。
- 生产仍为 migration 017、rollout=`legacy`、admission=`BLOCKED`，ledger
  三层账本与 generation 计数均为 0、未发生变化；三个 0629 仅处于受控兼容桥，
  其他 Native 禁止 fallback，该桥尚无生产资格。
- 本轮只证明分层功能 MVP：真实 21 算法同轮、07:55 容量、生产 clone 迁移、
  generation 长期归档/磁盘上限、20+20 样本、故障注入和连续 10 日均未通过；
  admission 继续 `BLOCKED`。
- cache 候选虽具备单 prewarmer、不可变 generation、原子 pointer 和硬配额，
  但完整 cached-vs-cold 内部字段证据不足；DataBridge 与 Native 同机
  forced-cold 也未准入。
- migration runner 已在隔离 MySQL 覆盖 017 clean/legacy/APPLYING 恢复；
  生产同构 clone 的 018 partial-DDL 演练仍未完成，未授权当前生产执行。

权威设计与门禁见[日频信号 08:00 SLA 架构](architecture/DAILY_SIGNAL_SLA.md)。

详细证据见[首个生产灰度激活记录](blackbox_v2/records/PRODUCTION_GRAY_ACTIVATION_20260720.md)、[1Y T+5 四方案分阶段记录](blackbox_v2/records/PRODUCTION_GRAY_1Y_T5_4SCHEMES_20260720.md)和[Blackbox V2 试验记录](blackbox_v2/records/README.md)。

## Native V1 当前摘要

- 版本化政策清单保留 29 个 Native V1 仓库身份；新增身份继续由机器门禁拒绝。
- 当前业务展示目标覆盖 `1Y/3Y/5Y/7Y/10Y`，具体 active 范围以 Registry 和 API 为准。
- 日频候选中的 14 个 `generation_v1` Native 和三个 0629 兼容方案已经完成逐类真实
  no-persist 认证；三个 0629 仍未完成公共 generation adapter 或生产容量准入。
- 5Y/7Y 周点值方案已使用 `no_signal_to_flat_v1` 处理算法有效无信号；异常、缺数和执行失败仍然 fail-closed。
- 10Y 周点值方案的历史周历和输入冲突尚未通过数据治理修复，不能用补平或修改算法绕过。
- 两个 Full-OOS 灰度方案已经观察到真实 scheduler 触发，历史细节只保留在状态快照中。

## 当前观察项

1. `3c24ec6` 已完成 real replay 逐 dispatch process fence：submit、worker、preclaim 和 `Popen` 登记窗口共享 exact active reservation、精确 report identity 与 canonical start guard；这仍不代表 execute-only 入口或真实算法联跑已完成。
2. 下一工程步骤是补齐 execute-only CLI；该入口仍须钉住 production audit-readonly endpoint/server UUID、覆盖 `.so/.pyc`/Conda 身份并控制重复扫描负载。CLI 尚未提供，真实 17+4 隔离 replay 也尚未执行。
3. execute-only CLI 完成且获准独占维护窗口后，修复 BFL 三份已安装 plist 的 mode 一致性并重跑 `--check-only`；当前 backend 仍因缺少合法 coordinator mode 而 fail-closed。下一次交易业务日的真实同日 generation 到位后，只通过 execute 入口执行 17 Native + 4 V2、21 item/25 target 的隔离 MySQL 全量联跑。禁止伪造 seal、使用 recorder、写生产库、触碰 BondProjectPro 或计作容量样本。
4. 真实联跑通过后复核并收口 Blackbox V2 从两文件 Intake、七个自动 Gate 到签名
   gray admission 的标准路径，使后续新方案可按 SOP 进入灰度，同时保持
   `gray_live` 与正式 21/25 occurrence 解耦。
5. 在生产同构脱敏 clone 演练 migration 018 的 apply、重复执行、断连和
   `APPLYING` 恢复；当前生产仍停留在 migration 017。
6. 三个 0629 方案逐个改为公共 generation adapter，每个方案独立执行
   CompareGate 和 commit；若触及 L2 算法语义则停止并改走 Blackbox V2 replacement。
7. 真实联跑功能通过后才进入单 Mac cache/I/O 容量优化、20 次 forced-cold、
   20 次 revision/suffix、故障注入和 07:55 门禁。
8. generation 长期归档/去重/磁盘上限、019 composite FK 与连续 10 个交易日
   25/25 仍是生产切换前置条件；在此之前 rollout 保持 `legacy`、admission
   保持 `BLOCKED`。
9. 持续复验 gray live 的 actual join、指标 API 和前端准确率；用独立真实交付覆盖周平均/月频，不人工补 actual、不复用已有方案授权。

## 权威入口

- 方案入库：[统一入库导航](onboarding/README.md)
- 上游交付：[Blackbox V2 上游交付 SOP](sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台操作：[Blackbox V2 平台入库 SOP](sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[Blackbox V2 生产准备清单](blackbox_v2/PRODUCTION_READINESS.md)
- 历史状态：[状态记录索引](records/status/README.md)
