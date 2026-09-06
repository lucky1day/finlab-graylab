# Native V1 全量迁移至 Blackbox V2

**文档状态**：`CURRENT`

**执行状态**：`W1_ECS_BACKTEST_READY; W2_ECS_BLOCKED_PERFORMANCE; W3_BLOCKED_PERFORMANCE; W4_MAC3_BINARY_BUNDLE_ACCEPTED`

**基线日期**：2026-09-05 Asia/Shanghai

**基线 Git 提交**：`20e98934e9d5399e3d509f486a8a650d95ad7639`

**W2 算法候选提交**：`f947426d969d6c3e3879e707f7a0564345788d9a`

## 1. 目标与非目标

本项目把 26 个 Native V1 base scheme 替换为 30 个新的 Blackbox V2 successor，使 Scheduler、回测、
生命周期、调度和最终清理只保留 Blackbox V2 一条可执行主路径。迁移不是修改旧身份的 `runtime_type`，而是
建立新身份、验证同输入结果、原子转移业务格子所有权、通过真实 unit/plist 环境的人工 one-shot 验证控制面，
最后删除没有调用者的 Native 路径；不再等待跨日、跨周或跨月的自然触发观察次数。

长期不变量如下：

- 旧 Native prediction、run、backtest 和 Registry 历史永久不修改、不覆盖、不删除；
- successor 使用独立 base ID 和 composite Registry ID，不建立 predecessor alias、历史拼接、双写或 fallback；
- Blackbox Result 顶层字段精确为 `request_id`、`predict_date`、`feature_date`、`target_date`、
  `predicted_direction`；
- confidence、vote score、阈值和算法内部统计只用于迁移期诊断，不进入长期合同或数据库；
- 所有新算法输入只来自五文件 DataBridge generation 与 Request；
- 现有 Blackbox 的 `legacy_v1` 输入模式不属于本项目，不随 Native 清理删除；
- ECS 是独立灰度实验室，Mac3 是独立生产环境；验证结果可复用同一 immutable archive，但数据库事实不可复制。

2026-09-06 用户明确决定本项目不等待日/周/月自然调度次数，以真实 installed unit/plist 环境的人工 one-shot
模拟替代原 5/3/2 次自然观察。该决定接受了“不能证明跨日历触发连续稳定性”的剩余风险；替代证据必须同时
覆盖精确调度命令与环境、唯一 writer、真实 `scheduled_live` run、journal、业务键、Dashboard、next trigger
和一次完整 rollback/re-cutover 演练，不能用直接调用算法脚本代替控制面模拟。

## 2. 权威身份映射

迁移工具只接受仓库内静态映射 `deploy/native_to_blackbox_migration_v1.json`。映射保存旧 Native 配置中的
原始 `target_rule`，先对旧配置做精确校验，再由共享 `task_type` 规格规范化为 Blackbox 的 canonical
`target_rule`、frequency 和 horizon。切换覆盖匹配键固定为 `task_type + target_tenor + target_rule`；
Native 周/月的历史 horizon `6/30` 不与 Blackbox 的 `1` 直接比较。
该临时映射的锁定文件 SHA-256 为
`370706acf55d436f4e420cd7be54d1c3254b2e3d5caa2c80d8beb4e6322c2045`；任何字节变化都必须先更新本计划并重新审查。

| Wave | Native base | Native target | Successor base | Blackbox task/horizon | ECS 目标状态 | Mac3 目标状态 |
|---|---|---:|---|---|---|---|
| W1A | `t1_daily` | 5Y | `t1_daily_5y_bbv2` | T+1 / 1 | active | active |
| W1A | `t1_daily` | 10Y | `t1_daily_10y_bbv2` | T+1 / 1 | active | active |
| W1A | `t5_daily` | 3Y | `t5_daily_3y_bbv2` | T+5 / 5 | active | active |
| W1A | `t5_daily` | 5Y | `t5_daily_5y_bbv2` | T+5 / 5 | active | active |
| W1A | `t5_daily` | 7Y | `t5_daily_7y_bbv2` | T+5 / 5 | active | active |
| W1A | `t5_daily` | 10Y | `t5_daily_10y_bbv2` | T+5 / 5 | active | active |
| W1B | `weekly_5y_direct_0529` | 5Y | `weekly_5y_direct_0529_bbv2` | weekly_point / 1 | active | active |
| W1B | `weekly_7y_cross_d_overlay_0529` | 7Y | `weekly_7y_cross_d_overlay_0529_bbv2` | weekly_point / 1 | active | active |
| W1B | `weekly_10y_d_overlay_0529` | 10Y | `weekly_10y_d_overlay_0529_bbv2` | weekly_point / 1 | active | active |
| W2 | `daily_5y_2_v28` | 5Y | `daily_5y_2_v28_bbv2` | T+5 / 5 | active | active |
| W2 | `daily_7y_1_v28` | 7Y | `daily_7y_1_v28_bbv2` | T+5 / 5 | active | active |
| W3A | `liwei_0616_cons_sda_k3_div_k10` | 5Y | `liwei_0616_cons_sda_k3_div_k10_bbv2` | T+5 / 5 | active | active |
| W3A | `liwei_0616_5y01_full_oos_k3_div_k10` | 5Y | `liwei_0616_5y01_full_oos_k3_div_k10_bbv2` | T+5 / 5 | active | active |
| W3B | `liwei_0616_10y01_full_oos_k3_div_k10` | 10Y | `liwei_0616_10y01_full_oos_k3_div_k10_bbv2` | T+5 / 5 | active | active |
| W3B | `liwei_0616_10y01_cons_say_k3_div_k10` | 10Y | `liwei_0616_10y01_cons_say_k3_div_k10_bbv2` | T+5 / 5 | active | active |
| W3B | `liwei_0616_10y02_cons_say_k3_div_k5` | 10Y | `liwei_0616_10y02_cons_say_k3_div_k5_bbv2` | T+5 / 5 | active | active |
| W3C | `liwei_0616_5y_auc_static_all_k3_div_k10` | 5Y | `liwei_0616_5y_auc_static_all_k3_div_k10_bbv2` | T+5 / 5 | active | active |
| W3C | `liwei_0616_5y_auc_yearly_all_k3_div_k10` | 5Y | `liwei_0616_5y_auc_yearly_all_k3_div_k10_bbv2` | T+5 / 5 | active | active |
| W3C | `liwei_0616_5y_ic_yearly_all_k3_div_k10` | 5Y | `liwei_0616_5y_ic_yearly_all_k3_div_k10_bbv2` | T+5 / 5 | active | active |
| W3D | `liwei_0616_7y01_cons_say_k3_div_k10` | 7Y | `liwei_0616_7y01_cons_say_k3_div_k10_bbv2` | T+5 / 5 | active | active |
| W3D | `liwei_0616_7y03_cons_all_k3_div_k8` | 7Y | `liwei_0616_7y03_cons_all_k3_div_k8_bbv2` | T+5 / 5 | active | active |
| W4A | `daily_1y_xgb_1y13_0629` | 1Y | `daily_1y_xgb_1y13_0629_bbv2` | T+1 / 1 | not_deployed | active |
| W4A | `daily_5y_lgbm_5y10_0629` | 5Y | `daily_5y_lgbm_5y10_0629_bbv2` | T+1 / 1 | not_deployed | active |
| W4A | `daily_10y_lgbm_10y04_0629` | 10Y | `daily_10y_lgbm_10y04_0629_bbv2` | T+1 / 1 | not_deployed | active |
| W4B | `weekly_avg_1y_lgbm_0529` | 1Y | `weekly_avg_1y_lgbm_0529_bbv2` | weekly_average / 1 | not_deployed | active |
| W4B | `weekly_avg_5y_lgbm_0529` | 5Y | `weekly_avg_5y_lgbm_0529_bbv2` | weekly_average / 1 | not_deployed | active |
| W4B | `weekly_avg_10y_lgbm_0529` | 10Y | `weekly_avg_10y_lgbm_0529_bbv2` | weekly_average / 1 | not_deployed | active |
| W4C | `monthly_1y_rf_top30_0629` | 1Y | `monthly_1y_rf_top30_0629_bbv2` | monthly / 1 | not_deployed | active |
| W4C | `monthly_5y_knn_top20_0629` | 5Y | `monthly_5y_knn_top20_0629_bbv2` | monthly / 1 | not_deployed | active |
| W4C | `monthly_10y_rf_top5_0629` | 10Y | `monthly_10y_rf_top5_0629_bbv2` | monthly / 1 | not_deployed | active |

W3A 和 W3B 各自作为不可拆分 cache-family wave。W3C、W3D 在各自 wave 内允许逐方案切换，但必须先证明
其余 active 方案不再依赖被切方案的 publisher。W4A/W4B/W4C 不进入 ECS；它们只在 17 个可读源码 Native
完成 ECS 验证并以同一 immutable archive 晋级 Mac3 后，作为 Mac3-only binary bundle 单独推进。

## 3. 冻结基线

### 3.1 Git 与本地验证

- 分支：`codex/develop`
- commit：`20e98934e9d5399e3d509f486a8a650d95ad7639`
- 开始状态：clean worktree
- 迁移前回归：`585 passed, 4 skipped, 194 subtests passed`
- 当前候选回归：`654 passed, 5 skipped, 5 warnings, 194 subtests passed`
- 原子迁移 isolated MySQL 8 验证：`1 passed`；随机测试 schema 清理后残留数为 0
- Native inventory：26 base / 30 target
- Blackbox inventory：67 active base；其中 66 个仍使用 `legacy_v1`，不属于本项目

### 3.2 ECS 只读基线

以下是本轮开始前已读回的现场摘要；执行任何 wave 前必须重新生成完整 preflight，不能把本段当实时权威：

- SSH 入口：`root@47.103.45.193`
- runtime：`/opt/bond-factor-lab/current`
- current release：`617113ed0b2e3c059d5b8a4d1390f453938966f5`
- migration：024
- DataBridge generation：`full-20260905-063321-21c5c7188fa5`
- DataBridge business digest：`21c5c7188fa597f6fa7b7e457ae777e2b578fd8a8410919a40ec54ef7ac69a4d`
- DataBridge：五文件，factor catalog 1474 行
- Registry：17 个 active Native base / 21 个 active Native target；9 个 paused Native target；67 个 active Blackbox base
- systemd：DataBridge、daily、weekly、monthly、Actuals timer 与 Backend 正常
- scheme run：零 running

2026-09-05 已使用 ECS 专用 SSH 身份完成新的非交互只读复核。当前 generation 的五文件 SHA-256 为：
`api_wind_date.csv=fc42e4b6a59edcaf81ded827f82ea639a2a17a81d1763f23aa3bb386d153a4b2`、
`daily_output.csv=8e98c84d577304b69c432abfd3bf7438133add3af542b69534d3365949a9f223`、
`factor_catalog.csv=bb5ee17f097369ed4498744b604d51597e4814c9e31887e90aad62c2b2bd8965`、
`monthly_output.csv=1ee62d0939ec73350db5b01d98343557cc11ef0a48497c93f4d31f4ba655ed27`、
`weekly_output.csv=d51c65df76ab816b1d777ac7fd7377aac7d62b93b12ce30ebc4c5da26b0685bb`。
这只解除 SSH 和 generation 再采集阻塞；正式 cutover 仍须从待部署 immutable release 重新执行完整 preflight。

### 3.3 Mac3 本机数据库只读水位

下表为 2026-09-05 从本机 Registry 和事实表直接读取的参考基线。数据库身份已在会话内核验但不写入文档；
任何 Mac3 切换前仍须在同一 preflight 重新读取。`方向` 采用 `-1/0/1:数量`，不是业务比例。

| Native target | P count / target range / 方向 | B count / target range / 方向 |
|---|---|---|
| `daily_10y_lgbm_10y04_0629/10Y/h1` | 406 / 2025-01-03..2026-09-04 / -1:153,1:253 | 337 / 2025-01-03..2026-05-29 / -1:131,1:206 |
| `daily_1y_xgb_1y13_0629/1Y/h1` | 406 / 2025-01-03..2026-09-04 / -1:209,0:161,1:36 | 337 / 2025-01-03..2026-05-29 / -1:185,0:121,1:31 |
| `daily_5y_2_v28/5Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:138,0:80,1:188 | 333 / 2025-01-09..2026-05-29 / -1:132,0:73,1:128 |
| `daily_5y_lgbm_5y10_0629/5Y/h1` | 406 / 2025-01-03..2026-09-04 / -1:130,0:155,1:121 | 337 / 2025-01-03..2026-05-29 / -1:108,0:131,1:98 |
| `daily_7y_1_v28/7Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:194,1:212 | 333 / 2025-01-09..2026-05-29 / -1:160,1:173 |
| `liwei_0616_10y01_cons_say_k3_div_k10/10Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:125,0:62,1:219 | 333 / 2025-01-09..2026-05-29 / -1:124,0:54,1:155 |
| `liwei_0616_10y01_full_oos_k3_div_k10/10Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:114,0:63,1:229 | 333 / 2025-01-09..2026-05-29 / -1:113,0:56,1:164 |
| `liwei_0616_10y02_cons_say_k3_div_k5/10Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:117,0:48,1:241 | 333 / 2025-01-09..2026-05-29 / -1:116,0:44,1:173 |
| `liwei_0616_5y01_full_oos_k3_div_k10/5Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:141,0:81,1:184 | 333 / 2025-01-09..2026-05-29 / -1:101,0:70,1:162 |
| `liwei_0616_5y_auc_static_all_k3_div_k10/5Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:155,0:44,1:207 | 333 / 2025-01-09..2026-05-29 / -1:115,0:40,1:178 |
| `liwei_0616_5y_auc_yearly_all_k3_div_k10/5Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:157,0:43,1:206 | 333 / 2025-01-09..2026-05-29 / -1:116,0:40,1:177 |
| `liwei_0616_5y_ic_yearly_all_k3_div_k10/5Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:156,0:46,1:204 | 333 / 2025-01-09..2026-05-29 / -1:116,0:40,1:177 |
| `liwei_0616_7y01_cons_say_k3_div_k10/7Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:127,0:99,1:180 | 333 / 2025-01-09..2026-05-29 / -1:115,0:98,1:120 |
| `liwei_0616_7y03_cons_all_k3_div_k8/7Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:121,0:45,1:240 | 333 / 2025-01-09..2026-05-29 / -1:116,0:43,1:174 |
| `liwei_0616_cons_sda_k3_div_k10/5Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:113,0:83,1:210 | 333 / 2025-01-09..2026-05-29 / -1:101,0:63,1:169 |
| `monthly_10y_rf_top5_0629/10Y/h30` | 20 / 2025-02-14..2026-09-15 / -1:16,1:4 | 16 / 2025-02-14..2026-05-15 / -1:12,1:4 |
| `monthly_1y_rf_top30_0629/1Y/h30` | 20 / 2025-02-14..2026-09-15 / -1:8,1:12 | 16 / 2025-02-14..2026-05-15 / -1:6,1:10 |
| `monthly_5y_knn_top20_0629/5Y/h30` | 20 / 2025-02-14..2026-09-15 / -1:14,1:6 | 16 / 2025-02-14..2026-05-15 / -1:11,1:5 |
| `t1_daily/10Y/h1` | 406 / 2025-01-03..2026-09-04 / -1:118,1:288 | 337 / 2025-01-03..2026-05-29 / -1:103,1:234 |
| `t1_daily/5Y/h1` | 406 / 2025-01-03..2026-09-04 / -1:169,1:237 | 337 / 2025-01-03..2026-05-29 / -1:140,1:197 |
| `t5_daily/10Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:151,1:255 | 333 / 2025-01-09..2026-05-29 / -1:146,1:187 |
| `t5_daily/3Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:217,1:189 | 333 / 2025-01-09..2026-05-29 / -1:167,1:166 |
| `t5_daily/5Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:172,1:234 | 333 / 2025-01-09..2026-05-29 / -1:169,1:164 |
| `t5_daily/7Y/h5` | 406 / 2025-01-09..2026-09-10 / -1:243,1:163 | 333 / 2025-01-09..2026-05-29 / -1:229,1:104 |
| `weekly_10y_d_overlay_0529/10Y/h6` | 86 / 2025-01-10..2026-09-04 / -1:69,1:17 | 72 / 2025-01-10..2026-05-29 / -1:58,1:14 |
| `weekly_5y_direct_0529/5Y/h6` | 86 / 2025-01-10..2026-09-04 / -1:37,0:3,1:46 | 72 / 2025-01-10..2026-05-29 / -1:32,0:1,1:39 |
| `weekly_7y_cross_d_overlay_0529/7Y/h6` | 86 / 2025-01-10..2026-09-04 / -1:55,0:6,1:25 | 72 / 2025-01-10..2026-05-29 / -1:47,0:4,1:21 |
| `weekly_avg_10y_lgbm_0529/10Y/h6` | 86 / 2025-01-10..2026-09-04 / -1:49,1:37 | 72 / 2025-01-10..2026-05-29 / -1:37,1:35 |
| `weekly_avg_1y_lgbm_0529/1Y/h6` | 86 / 2025-01-10..2026-09-04 / -1:60,1:26 | 72 / 2025-01-10..2026-05-29 / -1:49,1:23 |
| `weekly_avg_5y_lgbm_0529/5Y/h6` | 86 / 2025-01-10..2026-09-04 / -1:63,1:23 | 72 / 2025-01-10..2026-05-29 / -1:53,1:19 |

注意：`t_scheme_predictions` 已包含 Migration 024 发布的历史回测事实，因此 P count 不是 live run 数；任何
cutover 验证必须继续按 lineage 与 target 区间拆分。旧表中合法方向包含 `0`，successor 必须逐行保真，不能
把它静默映射成 `-1/1`。

### 3.4 每批必须冻结的机器可验证证据

`preflight` 必须在一个只读 consistent snapshot 与只读控制面检查中输出 canonical JSON：

- release manifest、current/previous 精确路径和 Git/archive hash；
- 数据库 `DATABASE()`、`@@server_uuid`、migration version、零 running run；
- old/new Registry、exact version、code/config/manifest hash；
- old prediction/backtest 的 count、最小/最大日期、方向分组摘要；
- success persisted backtest evidence、校验策略摘要、环境指纹；
- DataBridge generation、五文件 SHA-256、business digest、catalog 成员集合和 ready receipt；
- installed unit、active state、next trigger、唯一 writer；
- Dashboard active scheme 集合与规范响应摘要。

plan SHA-256 必须对去除采集时间和采集 payload SHA 后的 canonical plan JSON 字节计算；这两个瞬时字段
只用于 freshness/审计，不进入授权摘要。任何权威状态变化都使旧 plan 失效。preflight 与
cutover/rollback 均由命令直接读取目标机 `current` symlink、install record/source-tree、部署矩阵、五文件
snapshot、installed unit/plist、loaded/进程状态和 Dashboard；人工 JSON 不能替代现场采集。cutover/rollback
在取得 old/new advisory lock 并开启事务后再次现场采集，并重读全部数据库权威条件。数据库必须严格处于
完整 `APPLIED` 的 migration 024；successor 回测 generation、data snapshot、runtime profile 和当前平台 frozen
environment fingerprint 必须全部一致，且待发布历史回测的 `target_date` 必须严格小于 `2026-06-01`。旧 Native
已发布历史与当前 generation 的日期 grid 分别要求非空且无重复；两侧日期数量与摘要必须进入 plan SHA，并以
`data_vintage_drift` 显式记录，但不得因交易日历或历史数据修订而要求 successor 复制旧错误日期。迁移等价仍只
能在当前冻结 generation 的同一完整 Request 集上通过，三个日期与方向必须逐行零差异。

同输入零差异证据使用随 immutable release 发布的临时 receipt：整批 wave 为
`deploy/native_successor_equivalence/<wave>.json`，逐方案 wave 为
`deploy/native_successor_equivalence/<wave>--<old_base_scheme_id>.json`。receipt 必须由当前 release 的
`native-successor-controlled-comparator` v1 生成，并绑定 comparator 源码 SHA-256、generation、data snapshot、
五文件 SHA-256、Native/Blackbox 环境指纹及 old/new code hash。comparator 使用同一七字段 Request CSV 直接
执行两侧程序，读取两份标准五字段 CSV 结果，逐行核对 Request 顺序、ID、三个日期和方向；任一差异直接拒绝
生成 receipt。preflight 会在
数据库只读快照中锁定 successor 的完整
持久化 backtest fact 集，并从每行 `source_row` 重新推导完整七字段 Request 摘要、Request 数量、ID/三日期摘要
和标准五字段结果摘要；因此任一 cutoff key 变化也会 fail-closed。receipt 的
Native/Blackbox 结果摘要必须相等并与该完整事实集一致，五类 mismatch count 必须全为 0。规范化 receipt
中的 Native 环境指纹还必须等于现场 `forecast_env` 的 conda explicit package 集指纹。receipt 进入 plan SHA，
apply 在事务内重新采集、重新推导并复验。receipt 缺失、抽样数量、手工摘要、旧 comparator 源码
或任一 identity 不匹配均 fail-closed。receipt 不保存包含自身的 Git commit，避免 tracked receipt 的不可满足
自引用；其内容由 comparator 源码 SHA、当前 release source-tree/archive、receipt 文件 SHA 和 plan SHA 共同
冻结。当前候选的 W1A/W1B canonical receipt 仍与各自 exact code 一致，但尚未随新的 immutable release
部署至 ECS，也尚未通过现场 preflight、持久化回测与切换事务，因此仍不能执行 ECS cutover。W2 只保留
2026-09-05 旧 exact code 的历史 receipt；提交 `f947426d969d6c3e3879e707f7a0564345788d9a` 已改变
successor script hash，该 receipt 已 superseded，只作审计，禁止用于 f947 preflight/cutover。f947 因 ECS
性能失败没有生成 canonical W2 receipt，也不得推进持久化回测或 cutover。

2026-09-05 已使用 ECS 专用 SSH 身份完成一次新的只读现场复核：`current` 仍指向 release
`617113ed0b2e3c059d5b8a4d1390f453938966f5`；数据库为 `bond_db`、migration 024、running scheme run 为 0；
Backend 为 active/running，DataBridge、daily、weekly、monthly 与 Actuals timer 均为 active/waiting。当前
DataBridge generation 为 `full-20260905-063321-21c5c7188fa5`，business digest 为
`21c5c7188fa597f6fa7b7e457ae777e2b578fd8a8410919a40ec54ef7ac69a4d`，factor catalog 为 1474 行。
因此“无法读取 ECS”不再是阻塞；但该复核只解除连接和现场读取问题，不替代 successor 完整 receipt、持久化
回测、部署矩阵变更、timer fence 或 cutover 独立授权。

## 4. Successor 交付证据台账

所有 successor 的输入都是 DataBridge 五文件：`api_wind_date.csv`、`daily_output.csv`、
`weekly_output.csv`、`monthly_output.csv`、`factor_catalog.csv`，以及 Blackbox predict JSON /
backtest CSV Request。具体算法列集合由 metadata 的 `required_columns` 与 Intake 后生成的
canonical config 固定。Request 区间和代码 hash 在测试
前不得预填。

| Wave | Successor 集合 | 输入字段摘要 | Request 区间 | script SHA-256 | 状态 |
|---|---|---|---|---|---|
| W1A | `t1_daily_{5y,10y}_bbv2` | daily date + 1Y/5Y/10Y；其余四文件做合同校验 | feature 2025-01-02..2026-05-28；各 337 条 | `126667bf95e24aeab77771d96d73cd8e43c66c39db26fd61b0ba8109d9bdc78d` | ECS_0905_FULL_COMPARATOR_PASSED |
| W1A | `t5_daily_{3y,5y,7y,10y}_bbv2` | daily date + 1Y/3Y/5Y/7Y/10Y；其余四文件做合同校验 | feature 2025-01-02..2026-05-22；各 333 条 | `126667bf95e24aeab77771d96d73cd8e43c66c39db26fd61b0ba8109d9bdc78d` | ECS_0905_FULL_COMPARATOR_PASSED |
| W1B | `weekly_5y_direct_0529_bbv2` | week_id + 1Y/5Y/7Y/10Y 周频收益率；其余文件做合同校验 | feature 2025-01-03..2026-05-22；72 条 | `59909549fee682c61a90c1394672f40b7204f67e35f692b04577cc49498a19c8` | ECS_0905_FULL_COMPARATOR_PASSED |
| W1B | `weekly_7y_cross_d_overlay_0529_bbv2` | week_id + 1Y/5Y/7Y/10Y 周频收益率；其余文件做合同校验 | feature 2025-01-03..2026-05-22；72 条 | `fb2baa38fa8b614737f0c2f87bff90626e2d8d268e5375362bf863554096e680` | ECS_0905_FULL_COMPARATOR_PASSED |
| W1B | `weekly_10y_d_overlay_0529_bbv2` | week_id + 1Y/5Y/7Y/10Y 周频收益率；其余文件做合同校验 | feature 2025-01-03..2026-05-22；72 条 | `e52e221a0046e8623107359b4fe3c2f7643e422b3ed9068c0cf317e9cdeafeed` | ECS_0905_FULL_COMPARATOR_PASSED |
| W2 | 两个 `daily_*_v28_bbv2` | 完整五文件；V28 daily/weekly/monthly 因子与真实 T+5 grid | feature 2025-01-02..2026-05-22；各 333 条 | 5Y `11059c610e802bd6a00d0ef6bd506be662fe932eb9d285a15ec3bd84817d94c0`；7Y `1d3c5b3748ba53c55c2a491ed28e83d7624bfae1d8171ad42aa9e6ae0a6ab761` | ECS_BLOCKED_PERFORMANCE_NO_CURRENT_RECEIPT |
| W3A | 两个 5Y cache-family successor | 五文件中算法所需因子；仅进程内 cutoff-keyed cache | feature 2026-08-28 | 未生成 | BLOCKED_PERFORMANCE |
| W3B | 三个 10Y cache-family successor | 五文件中算法所需因子；仅进程内 cutoff-keyed cache | feature 2026-08-28 | 未生成 | BLOCKED_PERFORMANCE |
| W3C-D | 五个 `liwei_0616_*_bbv2` | 五文件中算法所需因子；仅进程内 cutoff-keyed cache | feature 2026-08-28 | 未生成 | BLOCKED_PERFORMANCE |
| W4A-C | 九个编译主体 successor | DataBridge 五文件 + Request；加密 payload 进入 manifest closure | 待 Mac3 冻结 | 待生成 | MAC3_BINARY_BUNDLE_PLANNED |

每个 successor 的详细 conformance 输出属于一次性执行证据，不把大体积 Request/Output 或算法诊断内容提交
到代码线。文档只保留 exact script/config/metadata hash、输入摘要、结果摘要、性能和结论。

W1A/W1B 已从 ECS 当前数据库只读生成完整正式区间 Request，并在复制出的同一 ECS generation
`full-20260905-063321-21c5c7188fa5`、`snapshot-f42540ebc533428ca6c869e2` 上完成受控双环境 comparator。
W1A 共 2×337 + 4×333 = 2006 条，W1B 共 3×72 = 216 条；九个 target 的 Request ID、三个日期和方向
mismatch 均为 0。最终 comparator 源码下的 canonical W1A/W1B receipt SHA-256 分别为
`203b1678e9f1cd672a7dc2854dbc21d59b231718f6fd1df3e4903b1ce3513f21` 和
`819e5e2fc16db98c5682bb7cd54f2a4f6abbc62e8c28a0dbc42ceb76fb4bcb83`，共同绑定 comparator source SHA-256
`d0f3659e0ba7b41831a2b733e4e7e99f235a3cbf26c84f4f03631b4bd1d3c07f`。它们将随下一份 immutable release
发布，但不替代该 release 上的持久化 backtest 与现场 preflight。
九个完整七字段 Request artifact SHA-256 为：

- `t1_daily_5y_bbv2=d3f59f2d7100f4236d053433b75b6b805da870390e6a72dd53c754048286afc7`
- `t1_daily_10y_bbv2=543f5bf8e56ecf983ffb58599874355c583c0e1735f0df70c7a9da4afff94146`
- `t5_daily_3y_bbv2=89ea6b954ef8ba37e8bc4f8236d4e6ced0133bbc08eee285f38ae53611b3a2e4`
- `t5_daily_5y_bbv2=31ab83bd89c35baa3c1d1ab20bc959bfaf4d99ddf57a3651b2ab1974b458d1a4`
- `t5_daily_7y_bbv2=2f2a7b9aef608995ae95109da394c664824b9e76b3d29e058b6a6445ab743a0d`
- `t5_daily_10y_bbv2=f5d17b7922a584e0f18b98a6ca6611034e16c8ca63c2d93c2f20d01023f812bf`
- `weekly_5y_direct_0529_bbv2=d95bae52cd56caea73fdec3064cb751f429369a3116f3c0cd7f5b84f549201cc`
- `weekly_7y_cross_d_overlay_0529_bbv2=2b3bee8859a42ea28516fda1b90b3f5a0bf4fdffd129069d7dee3f8ac0731d92`
- `weekly_10y_d_overlay_0529_bbv2=f41136ac974a4502c198ae510f7c3aea16d8616d5f9cf88b17c032e2899989e6`

W1A 六方案先完成 600 条固定样本同输入 Native 对照，方向差异为 0；六方案的 100 条 batch 连续三次、升降序、乱序、
子集、首中末单点、未来数据追加隔离、非法输入失败无 Output 均通过。100 条耗时为 2.42–6.65 秒，峰值 RSS
约 281–375 MiB。W1B 三方案各 100 条方向差异为 0，同一组合同验收通过；5Y/7Y batch 约 0.16 秒，10Y
三次为 22.107/21.931/21.898 秒，最大单点 19.664 秒。此后又完成上述 ECS 0905 generation 的完整正式区间
双环境比较；仍未执行持久化回测或生产切换。9 个交付均额外通过 101 条 Request 的读取合同测试，批量入口
不存在 100 条人工上限；100 条只是固定性能样本。

完整比较首次暴露并修正了 T+5 comparator 的截止边界：Blackbox Request 的 `feature_date` 是闭区间 cutoff，
旧 Native helper 的 `predict_date` 参数却是开区间上界。比较器现在只把 feature 后首个交易日作为旧 helper 的
内部独占上界，并继续要求 Native 实际 feature 精确等于 Request；节假日跨段单测及 4×333 条正式比较均通过。

W2 在冻结 generation `full-20260901-063115-5636b51dacf6` 的完整五文件上，用 `forecast_env` 完成两份
自包含 delivery；去除只含环境路径的 header 后，其 `conda list --explicit` package URL rows 与冻结的
`forecast_env_blackbox_v1` 逐字节一致，但尚未冒充正式 Blackbox executor 现场验收。原 V28 core 与频率
对齐逻辑内联；`multiprocessing.Pool` 已移除，改为 4-worker 有序线程池，
每个 LightGBM `n_jobs=1`，Numba `cache=False`，并显式把 BLAS/OpenMP/VECLIB/Numba 线程设为 1。实测两个
delivery 的进程峰值均为 7 个 OS thread、无子进程。predict/backtest 共用 `generate_results`，继续按月初到
当前 batch cutoff 执行原 test-window，并按 Request 截断三频输入。

两个方案首/中/末独立单点与冻结 Native 基线逐字节一致：5Y 方向为 `-1/-1/0`，耗时 `6/38/16s`；7Y 为
`-1/-1/1`，耗时 `6/27/13s`。该冻结包集合环境的 100 条单 CLI 为 5Y 242 秒、7Y 175 秒；完整正式 333 条单 CLI 为
5Y 963 秒、峰值 RSS 2,722,912 KiB、Output SHA-256
`d6fce015cd4fcdd33cbd3354b07f0151d86a0d517fbe8d3204f122a23defba23`，7Y 589 秒、峰值 RSS
2,673,312 KiB、Output SHA-256 `47a6f9b8037a271f34ffa5406d37f629d55736196329a9607fe65c56d8dd3b6b`。
全部运行 stdout 为空，Result 精确五字段且顺序与 Request 一致。向任一 LightGBM config 注入异常时，整个 batch
非零失败、stderr 保留根因且不生成 Output；不再允许部分 grid 静默失败。额外字段、缺少 cutoff、重复 ID、
数据不足和缺失五文件等负向测试同样 fail-closed。两个 delivery 已通过本地 Intake；metadata SHA-256 分别为
5Y `4224283519ed1d0af5095a3f9532846d451cede313f9e1e723b7d28d15e9cb71`、7Y
`4e4ccfbaa52b48b4674e6b979955ed72504308aee27e6f3634003107f1d5a4d6`。

一次使用非目标 `bond_factor_lab_service` 解释器的 5Y 333 条诊断运行与 `forecast_env` 结果存在 8/333 行方向
差异，因此该结果明确不计入验收，也不能跨环境复用。此证据再次证明 runtime environment fingerprint 是迁移
等价身份的一部分。W2 的旧 exact code 曾在 ECS generation `full-20260905-063321-21c5c7188fa5` 上经正式
`forecast_env_blackbox_v1` executor 完成 2×333 条全量比较，五类 mismatch 均为 0；其历史 receipt
SHA-256 为 `c9eaf7bbc4c5f5c648cac9d7e6e3bdf472e6cf4d4af9f5542501cfd5a541a9fa`，comparator source SHA-256 与
W1 相同。该 receipt 绑定的 5Y/7Y script SHA 分别为
`bb1323ae7dfdf7eb2c31d52a676bfef6593ee65ed3d326126d424bfbe2ef7986` 和
`caad2438c5fa6ad627689e24aa4eff2f0a02062efa6790b375e73e94c05118a2`；f947 已改变 exact code，因此该
receipt 现为 `superseded/historical-only`，不得用于当前 preflight/cutover。尚无成功的持久化回测，也未执行
现场切换。

2026-09-06 在 ECS 4-vCPU 主机上使用预安装 immutable release 和正式 `forecast_env_blackbox_v1` executor
执行 5Y 完整持久化回测时，于 1800 秒硬超时终止；算法仍在正常逐月计算，未生成完整 Output，repository 未写入
任何 W2 backtest run/fact，7Y 因原子 wave fail-fast 未启动。ECS 增加第五 worker 会超卖 CPU，不能作为满足
门槛的可靠修复。因此 W2 两个 old Native 保留 `aliyun-gray` 部署范围，W2 不进入 cutover；后续必须提供降低
总计算量但不改变 grid、seed、cutoff 与窗口语义的实现，或更换满足既定门槛的 ECS 计算规格。

同日继续在提交 `f947426d969d6c3e3879e707f7a0564345788d9a` 上完成 Dataset 复用优化：冻结 Request 的本机
333 条输出与旧结果逐字节一致，5Y/7Y 分别为 630.99/407.91 秒，峰值 RSS 分别约 2.86/2.71 GiB；完整回归为
`654 passed, 5 skipped, 5 warnings, 194 subtests passed`，独立审查无 Critical/Important/Minor 发现。ECS 使用 generation
`full-20260906-063526-3baeb4277bae` 运行受控 W2 comparator 时，Native 参考完成后，5Y successor 再次在
1800 秒硬超时，最大 RSS 986,048 KiB；没有 successor Output、receipt、backtest 或数据库写入，7Y 未启动。
随后测试的月份级 8×1、2×2、共享 Dataset、训练索引预计算和 LightGBM 2×2 线程组合，要么没有稳定净收益，
要么在 ECS 触发 `SIGBUS`；全部实验改动均已撤销，未降低性能门槛。

一次 Native 性能对照暴露 `cache=True` 的 Numba 编译会在 immutable release 源目录写入 `.nbc/.nbi`，使
3162 release 的 source digest 被 preflight 正确拒绝。现场先切到已核验 f947，把受污染目录移动到独立
quarantine，再从原始、SHA-256 已核验的 3162 archive 重新预安装并激活。恢复后 preflight 已通过 release
完整性校验，只按预期拒绝 `old_still_deployed=['daily_5y_2_v28','daily_7y_1_v28']`；六个项目控制面 active，
没有遗留算法进程或部分 Output。后续禁止再从 immutable release 直接运行会触发源码旁 Numba cache 的 Native
诊断；此类比较必须在私有可写副本中执行。

W3A 在冻结 generation `full-20260901-063115-5636b51dacf6` 上使用
`feature_date=2026-08-28` 做了决定性性能试验。输入规模为 daily 3908×774、weekly
853×577、monthly 199×126，PIT 下界有 41 个有效测试行；三个 baseline 各需 265 个 config × 2 seeds。
完全关闭 Phase-A 持久 cache，使用最多 8 个进程内 worker、每个 LightGBM `n_jobs=1`、无子进程时，
`real 121.45s / user 469.44s / sys 31.85s`，峰值 RSS 790,052,864 bytes，仍停留在第一个 `STD`
baseline 的 `LGBMClassifier.fit`，`DIV` 与 `ACCWT` 尚未开始且没有结果输出。因此已触发单条 predict 120 秒
硬停止条件；未创建 successor，未修改 Native。另一个 full-OOS 方案有 644 个有效测试行，约为该下界的
15.7 倍，不再继续无效消耗。W3A 保持 Native，除非算法方能在不恢复跨方案持久状态、不缩减冻结 grid、
不放宽 cutoff 的前提下提供满足性能门槛的独立实现。

W3B 在同一冻结 generation 上选择计算下界 `liwei_0616_10y01_cons_say_k3_div_k10`
做可行性预检。Request 为 `feature_date=2026-08-28`，只使用 2025-08 与 2026-08 两个较短 PIT
test range。关闭持久 Phase-A cache、跨方案状态和子进程，使用 8 个进程内线程、每个
LightGBM `n_jobs=1`；四个 required baseline 共需 1060 个 config。120.70 秒时仍在多个
`LGBMClassifier.fit`，峰值 RSS 709,443,584 bytes。因时间门槛失败，W3B 未创建 successor，
也未继续更重的 full-OOS 方案。

W3C/W3D 已在无其他训练进程的独占窗口重新验证，排除了先前资源竞争。W3C 选择计算下界
`liwei_0616_5y_auc_static_all_k3_div_k10`，使用完整 2024-01-01..2026-08-28 OOS、四个 baseline、
每个 265 个配置和两个 seed；120.63 秒时仍未完成首个 baseline，累计 user 383.09 秒、sys 45.33 秒，
峰值 RSS 727,252,992 bytes。W3D 选择计算下界 `liwei_0616_7y01_cons_say_k3_div_k10`，使用两个
2026-08 PIT 区间、四个 baseline、每个 265 个配置和既有 2/2/3/3 seed；120.55 秒时仍未完成首个
baseline，累计 user 507.72 秒、sys 24.94 秒，峰值 RSS 713,015,296 bytes。两组均未使用持久 cache、
跨方案状态或子进程，仍触发单条 predict 120 秒硬停止条件；未创建 successor，未修改 Native 或共享路径。

## 5. 阶段与状态机

每个 wave 只能按下列顺序前进。W1-W3 的 delivery 是严格两文件；W4 的 `DELIVERY_INTAKE` 是本计划锁定的
Mac3-only binary-bundle Intake，不能解释为普通 Blackbox 两文件 Intake：

```text
DELIVERY_READY
  -> DELIVERY_INTAKE
  -> CONFORMANCE_PASSED
  -> PERSISTED_BACKTEST_PASSED
  -> PREFLIGHT_FROZEN
  -> CUTOVER_COMMITTED
  -> GRAY_RANGE_COMMITTED
  -> CONTROL_PLANE_SIMULATION_PASSED
  -> ROLLBACK_WINDOW_CLOSED
  -> NATIVE_CLEANUP_ELIGIBLE
```

缺少任一前置状态时后续状态不可写入。失败只允许回到当前 wave 的安全前态，不能跳过、补记或用人工结果冒充
真实 unit/plist 环境的人工 one-shot 控制面验证。

### 5.1 Phase 1：本地候选

本阶段只允许：

1. 本文档与静态 26→30 映射；
2. 只读 preflight 和 repository 单事务 cutover/rollback 的 isolated 测试；
3. Blackbox `T+1/h1` target 半开区间 gray batch；
4. W1A/W1B 九个 successor 两文件交付；
5. Intake、合同、等价、性能、全量回归、deterministic archive 和独立代码审查。

本阶段禁止 ECS/Mac3 activation、Registry 切换、业务写库、timer/unit/plist 变更和服务重启。

### 5.2 Phase 2：V28 与 Liwei

V28 删除 `multiprocessing.Pool`，predict/backtest 共用按 Request cutoff 的一条算法路径；单进程内数值线程不
超过 8。Liwei 不迁移持久 Phase-A publisher/consumer/cache wave；successor 必须独立运行，只允许单次 CLI
进程内按 cutoff 精确键控的临时 cache。三个 5Y ALL-K10 保持当前 `platform_live_pit_variant`。

若去除持久 cache 后不满足性能标准，保持 Native 并停止该方案迁移，不得恢复跨方案状态或读取未来窗口。

### 5.3 Phase 3：Mac3-only 编译主体方案

W4 九个方案不进入 ECS，也不再以取得可读源码为开始条件。17 个可读源码 Native 完成 ECS 验证并晋级 Mac3
后，W4 才在 Mac3 形成唯一获准的 Blackbox binary bundle 例外：`scheme.py + metadata.json + payload/`。
payload 只允许包含锁定的 CPython 3.13 macOS ARM64 `.so` 及其包内运行依赖；全部文件名、大小和 SHA-256
进入 canonical manifest 与 exact version hash closure。bundle 固定 Mac3 Runtime Profile，只能读取平台提供的
DataBridge 五文件与 Request，必须输出精确五字段，不得访问源数据库、网络、包外路径或持久状态。平台增加的
能力必须是通用 manifest/intake/executor 边界，不允许恢复九个方案各自的 Scheduler 分支，也不得把 binary
bundle 例外开放给新方案。

W4 的 `runtime_type` 仍为 `blackbox_v2`，Scheduler 继续执行 manifest 中的 `scheme.py` 标准 CLI，不新增
binary 专属执行分支。Intake 只为静态映射中的九个 successor 接受 `payload-manifest.json`，并要求固定
`blackbox-v2-mac3-cpython313-arm64-v1` profile；exact version 同时哈希 script、metadata、payload manifest 和
每个 payload 字节。ABI、架构或任一 payload hash 不匹配必须在启动算法前 fail-closed，不能退回 Native adapter。

### 5.4 Phase 4：ECS cutover

临时命令由目标机现场直接采集控制面；数据库 identity 参数必须来自本轮独立只读 identity query，不得写入本文档：

```bash
python -m harness migrate-native-successor prepare-equivalence --wave <wave> \
  --comparison-bundle <comparison-input.json>
python -m harness migrate-native-successor preflight --wave <wave> \
  --expected-database-name <name> --expected-server-uuid <uuid>
python -m harness migrate-native-successor cutover --wave <wave> \
  --expected-plan-sha256 <sha> --approved-by <operator> \
  --expected-database-name <name> --expected-server-uuid <uuid>
python -m harness migrate-native-successor rollback --wave <wave> \
  --expected-plan-sha256 <sha> --approved-by <operator> \
  --expected-database-name <name> --expected-server-uuid <uuid>
```

`prepare-equivalence` 的 bundle 顶层只允许
`schema_version/wave/generation_id/data_snapshot_id/data_dir/comparisons`；`data_dir` 必须包含精确五文件，工具现场
计算其 SHA-256。每个 comparison 只允许
`old_base_scheme_id/new_base_scheme_id/target_tenor/requests_path`，Request CSV 必须是该 successor 的完整正式
Request 集。工具不接受 operator 提供的 Native 或 successor 结果文件：它在 `forecast_env` 中调用映射锁定的旧
Native core runner，在 Blackbox frozen profile 环境中调用 canonical successor delivery，两边共用同一 Request
artifact 和五文件目录；输出只存在于私有临时目录，逐行比较后立即销毁。receipt 的 comparator source SHA 同时
覆盖主控程序和 Native runner，old/new code hash 由 canonical config 重新计算。per-scheme wave 还必须传
`--old-scheme-id <old_base_scheme_id>`。工具独占创建 canonical receipt，已有文件时拒绝覆盖；如需重做，必须先由
operator 明确撤销旧候选证据并形成新的 clean commit，不能静默替换。

每批顺序固定：在候选树生成受控 receipt 并更新 deployment matrix → 形成 clean commit 和 deterministic archive →
把该 archive 安装为目标机 current → fence cadence timer 并等待 one-shot 退出 → 从 current release 重做 preflight →
使用其 plan SHA 单事务切换 → 单 batch gray 区间 → 恢复 timer → 通过真实 systemd one-shot 的人工触发验证
唯一 writer、journal、Dashboard 与 next trigger。preflight 之前的安装只部署代码，不授予 Registry 或业务
事实写入权。

cutover 单事务必须：

1. 按稳定顺序取得 old/new advisory lock 和 Registry/version/fact 行锁；
2. 验证 old/new 的 task type、target tenor、target rule 业务格子覆盖精确相等，并把跨数据 vintage 的历史日期
   grid 数量、摘要和 drift 标记纳入计划；
3. 验证 successor exact version 的成功持久化回测及 evidence；
4. insert-only 发布或验证复用同 exact version 的 successor backtest facts；
5. 激活 successor version/Registry，archive old Registry，retire old exact version；
6. 同事务权威读回；任一步失败全部 rollback。

gray 区间从 `target_date >= 2026-06-01` 到下一个自然目标前，单 successor 只启动一个 batch，每条 Request
独立 cutoff；任一业务键已存在则整组拒绝，一次 repository 事务提交。失败立即反向 cutover，不开放 timer。

不等待日频、周频或月频的自然触发次数。每个 cadence 至少人工触发一次真实 installed systemd one-shot；其
命令、WorkingDirectory、EnvironmentFiles、运行用户和 Runtime Profile 必须与 timer 触发完全相同。service
成功退出、唯一 `scheduled_live` run、业务键唯一、old 无新 run、Dashboard 与 journal 相互证明同一次执行，
才可关闭该 wave 的模拟验证；任一失败立即 rollback。

### 5.5 Rollback

rollback 顺序固定：fence timer → 确认无进程和 running run → 单事务 archive/pause successor、恢复 old
version/Registry → 保留 successor facts → current 切回已核验 previous release → 恢复 timer → 人工触发同一
installed one-shot，验证 old 恢复运行且 successor 不再新增 run。

同 exact version 允许重新切换，但必须复用已经发布的完全相同事实；禁止重复插入、覆盖或更换版本规避冲突。

### 5.6 Phase 5：Mac3 晋级

Mac3 对 W1-W3 只使用 ECS 已验证的同一 immutable archive，独立重做 release、launchd、数据库、DataBridge、
backtest、cutover 和 rollback preflight，不得复制 ECS 的主键、run、prediction、backtest、Actual 或 Registry
行。Mac3 使用 installed plist 的精确 ProgramArguments、WorkingDirectory、EnvironmentVariables、运行用户和
Runtime Profile 人工触发一次 one-shot，不等待自然日历。W4 随后形成新的 Mac3-only binary-bundle archive，
该 archive 不需要也不得在 ECS 运行。30 个 target 全部完成对应控制面模拟验证后，才允许最终删除 Native
可执行路径。

## 6. 测试与验收

### 6.1 单 successor conformance

- 单条 predict 连续 3 次逐字段一致；
- 100 条 backtest 连续 3 次；
- 同 Request 集升序、降序、乱序、子集按 `request_id` 完全一致；
- 首、中、末 Request 用独立单点复算；
- cutoff 后追加未来数据，原 Request 不变；
- 非法字段、重复 ID、缺少 cutoff、数据不足、非法方向均非零退出、stderr 明确、stdout 为空、无 Output；
- Result 顶层字段精确等于五字段；
- W1-W3 不 import 平台代码，不访问 DB/网络/额外代码/持久状态，不启动子进程；W4 只允许读取 manifest 内
  已锁定的 binary payload，其他边界相同。

### 6.2 同输入等价

比较证据必须同时绑定 `generation_id + 五文件 SHA-256 + data_snapshot_id + 完整七字段 Request SHA-256 +
old/new code hash + runtime environment fingerprint`。Request 数量/顺序/ID、三个日期与方向必须逐行零差异，
三个 cutoff key 必须与持久化回测 `source_row` 完全一致。摘要不同只能
标记 `data_vintage_mismatch` 并同代重跑，不能豁免差异或调参贴历史结果。

### 6.3 性能

- 单条 predict ≤ 120 秒；
- 100 条 backtest ≤ 600 秒；
- 完整正式区间 ≤ 1800 秒；
- 单算法进程峰值 RSS ≤ 4 GiB；
- `fallback_used=false`；
- 无子进程，数值库线程不超过 8。

### 6.4 数据库与控制面

切换前后核对 old/new Registry/version/hash、持久化 backtest evidence、旧事实 count/date/direction 摘要、
successor 唯一键、`run_id/backtest_run_id` XOR、gray/backtest 区间、Actual 水位、其他 active 集合、人工触发的
真实 one-shot run、Dashboard 和 timer/journal。任何非计划变化都使 wave 失败。

临时迁移事务已在本机回环 MySQL 8 的随机高熵隔离 schema 中通过：真实覆盖 `GET_LOCK`、
`FOR UPDATE`、`CAST(... AS JSON)`、`ON DUPLICATE KEY UPDATE`、MySQL affected-row 语义、
中途约束失败的整事务回滚，cutover、rollback 和同 exact version re-cutover。测试每次只创建
`bfl_native_successor_pytest_<uuid>`，不预删同名 schema，仅在确认本次创建成功后于 `finally` 删除精确目标；
测试后残留 schema 计数为 0，未读写 `bond_db`。这只验证 MySQL 方言和事务原子性，不代表完整生产 schema
兼容验证，也不替代 ECS/Mac3 各自的现场 preflight、回测证据和授权。

## 7. 立即停止条件

出现任一项立即停止当前 wave：

- 无法证明同 generation、同 Request 或同 runtime fingerprint；
- 任一日期或方向不一致；
- successor 需要数据库、网络、持久 cache、跨方案 import 或子进程；W1-W3 需要额外代码，或 W4 读取 manifest
  hash closure 之外的代码；
- exact version、Registry、matrix、release manifest、DataBridge authority 不一致；
- 存在 running run、第二 writer 或 timer 无法 fence；
- 需要覆盖、删除或修改旧业务事实；
- 部分提交、insert-only 冲突、Dashboard 影响其他方案；
- 任一性能门槛不达标；
- Mac3 待晋级 archive 与 ECS 已验证 archive 不同。

## 8. 最终清理与 Migration 025

只有 26 个 Native 在对应环境完成切换、真实 one-shot 控制面模拟和回滚窗口后，才删除 Native scheme/core/config、25 个专属
回测 runner、adapter/source runner/DB 注入/ABI、Liwei cache wave、Native scheduler subprocess/artifact/gray
分支、Native Gate/onboard/policy、专属测试、`docs/native_v1/` 入口，以及本项目的临时 cutover CLI 和映射。

随后分两次发布 confidence-agnostic Blackbox release，确保 ECS/Mac3 的 current 与 previous 都不读写
confidence。得到独立生产授权并验证可恢复快照后，新增并只通过受控 migration CLI 执行
`025_drop_confidence.sql`，删除 `t_scheme_predictions.confidence` 和 `t_backtest_predictions.confidence`。
025 必须支持两列均在、只剩一列、两列均不在三种可恢复形态，其他 schema fail-closed。先 ECS 验证，再单独
授权 Mac3；DDL 后禁止回滚到 confidence-agnostic 边界以前的 release。

## 9. 完成定义

必须同时满足：W1-W3 的 21 个 successor target 全部通过合同/等价/性能并在 ECS 接管；同一 archive 晋级
Mac3；W4 九个 Mac3-only binary-bundle successor 通过 manifest/ABI/合同/等价/性能；Mac3 30 个 target 完成
切换与真实 launchd one-shot 模拟；old Registry 全 archived 且无新
Native run；Native 可执行路径与临时迁移工具已删除；全量、架构、isolated MySQL 和 migration recovery 测试
通过；025 已删除两个 confidence 列；Dashboard、Actuals、其他 Blackbox 和调度控制面无非计划变化。

当前全局状态仍为 `IN_PROGRESS`。ECS 基线已经重新只读核验；当前硬阻塞是 W3 未满足性能门槛。W4 的
Mac3-only binary-bundle 架构已经获得确认，但必须等 W1-W3 晋级 Mac3 后实施；不能用旧 adapter、未纳入
manifest 的二进制、旧水位或文档声明冒充闭环。
