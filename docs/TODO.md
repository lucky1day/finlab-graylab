# 当前优先级与待办

**文档状态**：`CURRENT`

**目标读者**：项目负责人、平台开发、运维和审计人员

**最后核验日期**：2026-07-28

本文是当前未完成工作的唯一权威排序。它不替代[当前状态](CURRENT_STATUS.md)的已验证结论，也不替代专项记录的时点证据。

本批 10Y T+5 四方案的 `controlled activate`、`persistent backtest`、
`manual gray_live` 和前端验收已经完成，不再列为待办；终态见
[10Y T+5 四方案手工入库记录](blackbox_v2/records/GRAY_ONBOARDING_10Y_T5_4SCHEMES_20260726.md)。

## FengRL 五个月度方案手工灰度入库已完成

下列五个精确 Blackbox V2 月度方案已完成
`MANUAL_GRAY_ACCEPTED_5_OF_5`：

- `cgb_a4_fundseason_1y`
- `cgb_a4_fundseason_3y`
- `cgb_a4_fundseason_5y`
- `cgb_a4_fundseason_7y`
- `cgb_a4_fundseason_10y`

五方案 persisted all-stage 均为 7/7；每方案 16 条历史和 3 条手工
`gray_live` 已通过 DB/API/frontend 验收，每方案共 19 条，本批为
`80 + 15 = 95` 条。Registry/version 已 active，但完成手工灰度不授予自动调度；
本批没有 `scheduled_live`，也没有修改 rollout、admission 或 scheduler。时点证据见
[FengRL 五个月度方案记录](blackbox_v2/records/MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.md)。

## P0：全部 Active 方案实盘信号 MVP

用户已确认新的生产目标和边界：

- 每天巡检全部 44 个 active target / 40 个 execution；
- 日频 25 execution / 29 target（即 25 item/29 target）每个交易日自动写
  `scheduled_live`；
- 周频 7、月频 8 按自然频率到期执行，非到期日不制造重复记录；
- gray/formal 都承担自动实盘和缺失告警，业务标签本身不自动改变；四个
  daily gray只通过ledger准入，不能重新进入legacy daily cron；
- 只补已有信号缺口，不全量重跑；
- 20+20和连续观察不再作为MVP上线阻断；一次精确同机25/29
  forced-cold联跑通过后直接形成完整capacity admission。

执行必须严格遵循
[全部 Active 方案实盘信号 MVP 设计](superpowers/specs/2026-07-28-all-active-signal-production-mvp-design.md)
和
[实施计划](superpowers/plans/2026-07-28-all-active-signal-production-mvp.md)：

**当前状态：代码候选完成，真实 replay/切换待授权。** 开发代码已完成到
实施分支`8ee1f52`、开发分支等价`f8df6f4`：只读、内容寻址的信号gap plan、按控制面隔离的gray/formal准入、
daily policy从21/25扩展为25/29、
临时MySQL账本验证、forced-cold 25/29容量候选契约、policy v2生产入口绑定和旧
occurrence拒绝均已落地。权威日频口径为25 execution/29 target（17 Native +
8 V2）。生产仍为rollout=`legacy`、admission=`BLOCKED`，未部署、未切换epoch。

当前最前执行顺序固定为：

1. 取得独立维护授权后，仅维护BFL的backend、scheduler、v2-preflight三项服务：
   补齐三份已安装plist的coordinator mode，修复scheduler cache root为`0755`
   导致的crash-loop，并补齐source配置缺失键；不得修改或重启BondProjectPro。
   public replay preflight在这些条件闭合前继续以
   `CONTROL_PLANE_BOUNDARY_UNAVAILABLE` fail-closed。
2. 在同一台Mac Studio执行真实forced-cold 25/29联跑，验收17 Native +
   8 V2、29/29原子提交、无duplicate/partial/orphan及重入零新增。
3. 完成绑定生产MySQL identity的execute-only observation，生成有签名、expiry
   和replay floor的production-bound `ADMITTED`。
4. 演练并应用migration018及控制面前置后，取得部署授权，单次从legacy切换ledger。
5. 首个合法ledger交易日验收29/29，07:55前可见、08:00 SLA=`MET`。

当前只读基线为 canonical backtest 10,309条且完整；live应有1,343、
已有1,314、缺29，其中日频24、周频5、月频0。29个gap尚未写入，
待上述日频切换主线闭合后再执行受控补缺。历史漏跑只能补
`gray_live`，不得倒签`scheduled_live`；新调度结果统一写
`scheduled_live`。2026-07-28是决策/目标日期；实际scheduled起点不得
早于machine-global ledger epoch。

## P1：周度剩余对账与后端启用

已完成：

- 开发分支已集成 Actual Registry 化、周度 coverage/持久化起点门禁，
  以及全任务类型 backtest+live 的
  `predict_date >= 2025-01-01` 前端展示策略。
- 月度 updater 已按 active Registry 的
  `1Y/3Y/5Y/7Y/10Y` 幂等运行，8 个 active 月度方案保持
  `19 signal / 18 valid`。
- 已删除早期 100 条技术 Gate run `166–169`、非 canonical run
  `170–173`，以及跨过 gray 边界且已被替代的旧全量 run `174–177`。
  当前四个 1Y/T+5 方案分别只保留一条 canonical run `178–181`；API 和
  dashboard canonical 投影未变化。
- 已彻底删除3个错误 point-backed 周平均身份及其代码、policy 和数据库
  闭包。当前代码发现和生产 Registry 均只包含40个 active execution /
  44个 target，无 paused；删除前后 `/api/schemes` 和 dashboard 投影
  未变化。
- `weekly_10y_lgbm_point_v1` 已写入合规 run `191`，72 条历史；
  6/5–7/17 的 7 个灰度缺口已补齐，加原有 7/24 后为 `80/80`。旧 run
  `165` 在确认 2025+ 目标全部被新历史+live 覆盖后已删除。

剩余步骤必须按顺序执行：

1. 取得独立部署/重启授权后，在维护窗口重启 BFL Python 后端，使
   Registry Actual 范围、统一展示起点和 coverage 诊断由服务端生效；
   当前静态前端已使用 `aifin-shell.js?v=20260727b` 做同口径防护。
2. 当前合法 DataBridge 已达到 `refresh_date=2026-07-28`，
   `weekly_10y_lgbm_point_v1` 的 `target_date=2026-07-31` 缺口已可执行但尚未写入；
   仅在业务阶段 6（实施计划 Task 7）通过受控 Gate 补齐，禁止旧 generation fallback。
3. 上游修复源周数据 `week_id=202625` 与交易日历的契约冲突后，重新
   dry-run `weekly_10y_d_overlay_0529`。当前失败 run `1407` 只写入
   run/log、未写 prediction；冲突未修复前不得修改算法或继续后三个日期。
4. 冲突闭合后，逐点补该 Native 的
   `2026-07-10/07-17/07-24/07-31`，每个点独立 run，均写
   `gray_live`，不得伪造 `scheduled_live`。
5. 最终只读验收 7 个 active 周度候选均为
   `81 signal / 80 valid`，10Y weekly_point 两候选 target_date 集合
   完全一致；8 个月度候选均为 `19/18`，相关 API 返回 200，且无重复
   prediction、actual、run 或 target_date。

当前生产数据为：五个周度候选 `81/80`，
`weekly_10y_lgbm_point_v1=80/80`，
`weekly_10y_d_overlay_0529=77/77`。因此不能宣称周度已完成
`81/80` 对齐。

## 已完成但已被新决策扩展：Blackbox 自动调度防护 MVP

版本化 `blackbox_scheduler_admission_v1` 已冻结当前 5 个正式 Blackbox
身份为 `formal`、FengRL 五个月度与 10Y T+5 四个日频身份为 `gray`。
该实现原先把 `gray` 定义为禁止自动调度；2026-07-28 用户已经明确
改变这一业务规则：exact gray 也必须自动实盘并写 `scheduled_live`。
因此旧防护只能作为身份/version fail-closed底座，自动执行语义必须按P0
改造。manual aggregate和legacy `--run-once predictions`的既有绕过也
必须在切换前关闭。

该防护同时拒绝未知 Blackbox、版本漂移和保留 Blackbox ID 的 runtime
重分类。策略缺失、非 UTF-8 或定义漂移时仍只关闭 Blackbox 自动调度，
Native、actuals、health 和 watchdog 继续。

## P2：MVP 上线后增强

以下工作不再阻断本次功能MVP，但必须继续逐项完成：

1. 周/月自动调度及独立recurring admission；
2. 当前29个历史live缺口与`week_id=202625`上游日历契约冲突；
3. 三个0629公共generation adapter和逐方案CompareGate；
4. migrations019/020、generation归档、去重、磁盘上限和恢复；
5. 20次forced-cold、20次revision/suffix、连续十日与扩展故障注入；
6. 连续生产统计和P95可靠性证据；
7. 周频/月频统一迁入未来的多频率occurrence账本。

一次forced-cold admission只能证明当前精确候选达到工程发布条件，不能
表述为已取得统计P95或长期稳定性证明。
