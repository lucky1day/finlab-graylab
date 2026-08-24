# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-24

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和调度状态必须从各自权威数据源
读取；待推进工作见[统一后续推进计划](TODO.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成迁移、逐次 Gate、运行 ID、
发布窗口和一次性验收证据不在工作树维护副本，通过 Git、Harness、数据库与目标机 journal 追溯。

## 双主机边界

- Mac3 继续承载生产域名、前端、数据库和 Writer；`launchd + installed plist` 是生产调度控制面。
- ECS 是独立灰度实验室，使用自己的 MySQL、DataBridge、Registry、run、prediction 和 systemd timer；
  Backend 只监听 loopback，不承载生产公网流量。
- 两端不建立持续复制、双写、共享数据库或共享 DataBridge。经明确授权的单次缺口修复可以在停止目标 Writer
  后，从同一已验证 immutable release 的源端只读导出精确业务键与核心预测结果，再由目标端 repository
  insert-only 导入；不得复制数据库主键、`run_id`、Actuals、回测或 Harness 历史。
- 两端共用唯一 `codex/develop` source release 代码线；ECS 先验证、Mac3 后晋级时允许 `current` 不同，
  不因此建立环境分支。

## 当前 immutable release

- ECS 与 Mac3 `current` 均为 exact release
  `053d562fc40d7ecf5596f56f1beb00e4a3b58178`。ECS `previous` 为
  `86e9f32cda9d1cb573a14d202ed376c995e249f0`，Mac3 `previous` 为
  `3adcc96f790c740e2052b3f6283704829d30f5d9`。
- 两端使用同一份已验证 archive：source archive SHA-256 为
  `58dd3398d3e85cd3f91497eb4f4b5113749c010780b183c14b215e8ffea4dadf`，manifest 文件 SHA-256 为
  `a50cc3d566f08d196db83b3048d9c62c33ea9b20c200d2154639eec28e68c569`，安装后 source-tree SHA-256 为
  `1c01ed4b67c657a3eb456dad39b0ccd4d2b5c3ec00a560519e6f2f7fd43adf05`。
- 该 release 汇总了周期均值历史范围修复、月均目标月/日期格式、季均目标季度及 Q2 live scope、年均目标年度及
  2026 live scope。正式回归测试继续保留；一次性 archive、导出 JSON、候选目录和浏览器验收状态不进入工作树。
- Mac3 生产应用从 `/Users/macstudio0/bond-factor-lab-production/current` 启动，运行状态位于
  `/Users/macstudio0/bond-factor-lab-runtime`；ECS 从 `/opt/bond-factor-lab/current` 启动。两端运行时均不引用
  Git 工作区。

## 周期均值基础建设

- `monthly_average`、`quarterly_average`、`annual_average` 的平台基础已完成：统一任务规格、MID/CQ/SF
  纯桶语义、Contract/Request/回测、通用周期 Actual、close-period one-shot、Dashboard/metrics 和九列前端。
- Mac3 数据库 migration 001–020 已全部精确 `APPLIED`；周期均值目标表、既有预测、Actual、Registry 和
  scheme version 在迁移期间保持一致。ECS 对应迁移也已通过唯一受控迁移入口完成。
- monthly installed plist 已闭环为每日 18:00 到期判断，`ProgramArguments` 显式包含
  `--refresh-start 18:00 --refresh-deadline 18:55`，不包含同名刷新环境变量。全局 `service.env` 继续保留
  晨间 `05:30/06:45`，immutable launcher 的冲突保护未放宽。
- 非到期 probe 已验证 `not_applicable / exit_code=0 / refresh_required=false`，且 DataBridge、run、prediction
  和周期 Actual 均零副作用。
- 参考包中的周均五方案及本轮月均、季均、年均 15 个方案均已在 ECS 完成入库；15 个周期均值方案也已按同一
  exact release 和部署矩阵在 Mac3 active，不建立环境分支或第二套算法代码。

## Mac3 调度与 Dashboard 终态

- 七个 installed/loaded plist 的只读 drift audit 为 `ok=true`；Backend 与 SSH tunnel 正在运行，其余
  one-shot 当前空闲。daily 保持工作日 07:03，weekly 保持既有触发，monthly 每日 18:00。
- 2026-08-21 日频缺口终态为 `expected=48 / present=48 / actionable=0 / blocked=0`；2026-08-22 周频缺口
  终态为 `expected=21 / present=21 / actionable=0 / blocked=0`。
- 既有 active 方案的 `2026-06-01` target 边界已完成 canonical backtest / live 重分区；历史 run 与
  insert-only live 记录保持不可变，当前 latest-success 与 live target 零重叠。
- Mac3 当前有 88 个 active base scheme、92 个 active composite Registry。公网 Dashboard 快照非 stale，
  M0 live 记录为 100 条；与 ECS 按 base scheme、期限、horizon、三个日期组成的业务键逐条比较后，
  `ECS_ONLY=0 / MAC3_ONLY=0 / VALUE_DIFF=0`。
- Backend `/api/health` 正常；本机与公网
  `https://bond.finailab.cn/bond-factor-lab/` 页面和 Dashboard 均 HTTP 200，公网 HTML/JS/CSS 摘要与当前
  release 字节一致。

## ECS 灰度状态

- DataBridge、daily、weekly、monthly、Actuals 五个 timer 均保持 `enabled/active/waiting`；Backend
  active，loopback 页面和 Dashboard 均 HTTP 200。
- installed DataBridge、daily、weekly、monthly service 均不读取历史
  `/run/bond-factor-lab/manual-run.env`；monthly timer 每日 18:00 运行 close-period 到期判断。
- ECS 既有 active 方案也已完成 canonical backtest / live 重分区；其结果保持本机输入与数据库 authority，
  不用 Mac3 结果覆盖。
- 本轮 15 个 M0 周期均值方案激活后，ECS active base/composite 从 `64/68` 增至 `79/83`。Dashboard
  payload 全局契约通过，15 个新 composite 均通过 DashboardGate；loopback HTML、JS、CSS、SVG、health 和
  Dashboard 为 HTTP 200。浏览器读回三类 M0 排行、owner、样本数和准确率正常，控制台无 warning/error。
- ECS 已有的到期周期 Actual 保持本机 authority；未完成目标桶继续为 `null/pending`。人工 gap-fill 与
  Actual 刷新只用于补齐已到期历史，不计作首次自然 `scheduled_live` 或 Production Observed。

## 新方案入库状态

- `weekly_1y_causal_v1_31_0_standalone` 已完成技术 Gate、shadow、持久化回测、activation、单日
  `gray_live` 和 DashboardGate，Registry 为 active。
- `m0_weekly_avg_{1y,3y,5y,7y,10y}_v1` 已逐方案完成 Blackbox Intake、技术 Gate、shadow、持久化回测、
  activation、单日 `gray_live` 和 DashboardGate；五个 composite Registry 均为
  `active + weekly_average`，部署范围仅为 `aliyun-gray`。
- `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 已在 ECS、Mac3 分别完成 Blackbox
  技术 Gate、shadow、完整持久化回测、activation、单日 `gray_live` 和 DashboardGate；两端仍等待首次真实
  `scheduled_live` 自然触发，不能由 gray live 或人工运行预先宣称 Production Observed。

### M0 月均、季均、年均 15 方案

- 上游验收范围明确为 `feature_date >= 2025-01-01`。15 个修订后两文件 delivery 均为
  `algorithm_version=1.0.1`；120 个范围内金标 Request 方向零差异，合同失败为 0，15 份性能报告均低于
  `120s / 600s / 1800s / 4GiB` 且 `fallback_used=false`。更早的原始缺失值和错误锚点只保留为历史审计，
  不作为本轮准入样本。
- 15 个方案均在 ECS 完成 Intake、四段技术 Gate、shadow、canonical backtest、activation、合法 `gray_live` 和
  DashboardGate；同一 exact version 与 composite Registry 也已在 Mac3 active。共同绑定 DataBridge generation
  `full-20260823-063324-33751cc9bcd9`、combined snapshot
  `snapshot-2557d605845236cbeb21ea73` 和 `api-wind-date-v1` SHA-256
  `b24d10fca383bdb9ad9806ec3ef3db01cb0bc163ea4e24eac8d613f4e5a96cdf`。
- canonical backtest 均满足 `predict_date=feature_date`，且与 `target_date >= 2026-06-01` 的 live 区间
  零重叠；月均每方案 16 行、季均每方案 4 行、年均每方案 1 行。gray-live 月均每方案 3/3，季均每方案
  1/1，年均 0/0；20 个已到期业务键复核均为 `present=1 / actionable=0 / blocked=0`。

| 方案 | exact version | latest passed all Gate | canonical backtest | gray-live | Dashboard |
|---|---|---|---|---|---|
| `m0_monthly_avg_mid_1y_v1` | `646080e1aadf` | `hr_20260823T170216Z_c8a1d9082020` | `237`，16 行，2025-01-15..2026-04-15 | 3/3，run 3695–3697 | passed |
| `m0_monthly_avg_mid_3y_v1` | `0a6e8ba4a908` | `hr_20260823T170242Z_7056b0ebb75f` | `238`，16 行，2025-01-15..2026-04-15 | 3/3，run 3698–3700 | passed |
| `m0_monthly_avg_mid_5y_v1` | `f7ab01ebeded` | `hr_20260823T170307Z_69c6369d76f0` | `239`，16 行，2025-01-15..2026-04-15 | 3/3，run 3701–3703 | passed |
| `m0_monthly_avg_mid_7y_v1` | `00f29f7c44e8` | `hr_20260823T170333Z_71a12eaf0ccf` | `240`，16 行，2025-01-15..2026-04-15 | 3/3，run 3704–3706 | passed |
| `m0_monthly_avg_mid_10y_v1` | `dad07dc48a4e` | `hr_20260823T170358Z_09fd26f73a31` | `241`，16 行，2025-01-15..2026-04-15 | 3/3，run 3707–3709 | passed |
| `m0_quarterly_avg_1y_v1` | `ef9b4f1df701` | `hr_20260823T170423Z_7ecc14488c90` | `242`，4 行，2025-03-31..2025-12-31 | 1/1，run 3710 | passed |
| `m0_quarterly_avg_3y_v1` | `cf139be9cbb4` | `hr_20260823T170448Z_0a336623c9f7` | `243`，4 行，2025-03-31..2025-12-31 | 1/1，run 3711 | passed |
| `m0_quarterly_avg_5y_v1` | `51c5255e1173` | `hr_20260823T170513Z_5ac016dd0c52` | `244`，4 行，2025-03-31..2025-12-31 | 1/1，run 3712 | passed |
| `m0_quarterly_avg_7y_v1` | `06da7eca6a5b` | `hr_20260823T170538Z_9dd977bd4487` | `245`，4 行，2025-03-31..2025-12-31 | 1/1，run 3713 | passed |
| `m0_quarterly_avg_10y_v1` | `72bb3391683d` | `hr_20260823T170603Z_01e96c8bef98` | `246`，4 行，2025-03-31..2025-12-31 | 1/1，run 3714 | passed |
| `m0_annual_avg_sf_1y_v1` | `5365fd103351` | `hr_20260823T170629Z_fd4dd49f7058` | `247`，1 行，2025-01-27 | 0/0，未到期 | passed |
| `m0_annual_avg_sf_3y_v1` | `cd0d09ee4a05` | `hr_20260823T170653Z_cb5176b3ed19` | `248`，1 行，2025-01-27 | 0/0，未到期 | passed |
| `m0_annual_avg_sf_5y_v1` | `614e0d6f7657` | `hr_20260823T170718Z_5c0d18e58b6e` | `249`，1 行，2025-01-27 | 0/0，未到期 | passed |
| `m0_annual_avg_sf_7y_v1` | `982156ceecf6` | `hr_20260823T170743Z_dd1d4789538f` | `250`，1 行，2025-01-27 | 0/0，未到期 | passed |
| `m0_annual_avg_sf_10y_v1` | `71bf770309df` | `hr_20260823T170808Z_75fa2a8c45c8` | `251`，1 行，2025-01-27 | 0/0，未到期 | passed |

15 个方案现已达到 **Onboarding Complete**。Mac3 后续一次性同步闭环如下：

- 年均目标年度 2026 五条 Prediction 由 ECS 只读导出，Mac3 经 repository insert-only 写入本地 run
  `3680–3684`；Actual 仍为 `null`，不伪造未完成年度结果。
- 月均目标月 2026/06 五条 Prediction 写入本地 run `3685–3689`；季均目标季度 2026/Q2 五条 Prediction
  写入本地 run `3690–3694`。没有复制 ECS 数据库主键、`run_id`、Actual、回测或 Harness 历史。
- Mac3 从本机权威日频数据生成 20 条到期周期 Actual，写前与 ECS 的 20 条参考记录逐字段一致；其中包括
  季均 2026/Q2 五条和三组月均目标指针共 15 条。季均 2026/Q3 与年均 2026 继续 pending。
- 最终两端 M0 live 均为 100 条且业务键和值零差异；公网月均有 20 条 live、5 条 pending，季均有 10 条
  live、5 条 pending，年均有 5 条 live、5 条 pending。浏览器确认 2026/Q2 已验证、2026/Q3 待验证。

它们仍等待真实宿主时钟首次生成 `scheduled_live` 后才能标记 **Production Observed**。权威日历给出的下一月均
锚点为 2026-09-15、下一季均锚点为 2026-09-30；当前日历止于 2026-12-31，尚不能权威推导下一年均锚点。

## 当前治理边界

- config active、exact version active、Registry target active 且 cadence 匹配，是进入一次性 runner 的
  唯一资格；自然运行写 `scheduled_live`，单日授权补缺只写 insert-only `gray_live`。
- 后续新方案若使用逐 Request 截止等价的一次性 batch，统一只计算一次并冻结结果，再按方案级
  `gray_target_start` 分流：历史段形成新的 immutable canonical backtest，gray 段复用核心结果并按
  repository insert-only 物化；live `predict_date` 仍按任务日历重新生成。不能证明 live-safe 等价时回到
  逐点计算，不得以性能理由放宽截止、版本、lineage 或唯一键安全门。
- 跨主机补缺优先复用同一 immutable release 下已存在的精确预测结果；源端必须只读，目标端 Writer 必须先
  停止，release、方案版本、日期和业务键必须完全匹配，已有键整组拒绝。源端不存在的键才允许受控计算。
- 新方案只走 Blackbox V2 两文件 Intake；Native V1 只维护政策清单内存量身份。平台不反编译或改写
  Blackbox 算法逻辑，只验证平台接入和标准输出边界。
- Mac3 production 与 ECS gray 的 installed plist/unit、服务状态、数据库写入、激活、补数和 DDL 都是
  独立操作，代码或文档提交不能外推为现场授权。
- 未来把生产域名或 Writer 切到 ECS 是新的生产项目，不属于当前完成条件。
