# Native V1 全量迁移至 Blackbox V2

**文档状态**：`CURRENT`

**执行状态**：`A4_FULL_OOS_ECS_ALGORITHM_EQUIVALENCE_ACCEPTED_PERSISTED_BACKTEST_RUNNING; W1_ECS_NINE_TARGETS_ACTIVE_ON_INSTALLED_RELEASE; W2_ECS_OFFLINE_REVALIDATION_PENDING; W3A_CONS_SDA_CONFORMANCE_PASSED; W3B_D_PENDING; W4_MAC3_BINARY_BUNDLE_ACCEPTED`

**最新验收决定（2026-09-08）**：用户明确取消此次算法迁移的重复、倒序、乱序、子集及其他额外专项验证，
以相同冻结输入下修改前后完整回测的日期和方向逐条一致为算法验收依据，执行规则见第6.1–6.2节。
下文早期矩阵及启动/失败记录只保留历史事实，不再构成当前待办；平台写库、单Writer、回滚和发布安全边界不变。

**最新授权与证据规则（2026-09-08，覆盖此前推进范围）**：

- 本阶段只在 ECS 替换 W1-W3 的 17 个 Native base / 21 个 successor target。Mac3 当前/previous、
  数据库、launchd、对外域名、DNS/Nginx 与流量均不变；Mac3 晋级及 W4 九个加密方案暂停，恢复须另获授权。
- 算法等价证据与正式入库证据各自绑定自身 generation、snapshot、Request 和结果，不再要求二者输入同代。
  算法等价仍要求冻结输入内旧、新代码逐条零差异；正式回测仍要求当前 exact version、校验策略、运行环境、
  完整事实与原子持久化。两类证据必须覆盖相同完整 Request ID/三日期区间和同一代码身份。
- 跨输入版本不要求三个 cutoff key 或方向摘要相等；相同输入则继续交叉核验完整七字段 Request 与五字段结果。
  首次 cutover 的正式回测仍匹配现场 DataBridge，不能用旧算法验收输入替代现场输入。
- 临时 receipt 升为 `native-successor-equivalence-v2`，旧 v1 文件只留历史，不能由新工具解释为 v2 或手工改号。
  本轮不重生成已 active W1 的历史凭据；W3A 尚须补齐受控结果复用入口。部署新工具前，必须确认本批回滚
  及 re-cutover 均有新规则下可复验的证据，旧 immutable release 自身的历史工具不修改。
- 证据分离已完成本地实现：正式回测全部七字段 Request 的独立摘要进入 plan SHA；预检后任何 cutoff
  变更都使旧授权失效。定向测试先复现摘要未变化的问题，修复后验证旧摘要拒绝且 Registry/产品事实未改变。
  独立复审 Critical/Important 均为 0；全量回归 `702 passed, 6 skipped, 229 subtests passed`。
  本轮未配置 isolated MySQL URL，对应两个真实 MySQL 参数化场景 skipped，不视为通过；现场切换前仍须补验。

**最新执行核验（2026-09-08 上午）**：

- ECS Registry 的 W1 九个 successor target 已为 active，不能把下文早期“未切换”记录当作当前状态再切一次。
  最新候选校验策略下的旧回测复用检查未通过，不等于 installed release 下的九个 active 身份失效；不据此重跑或改写已发布事实。
- full-OOS successor exact `3ee3dd2334fd` 尚无成功持久化回测和产品预测事实，旧 Native Registry 仍 active；
  启动前 scheme run/backtest run 的 running 数均为 0。cons-sda successor 也尚无成功持久化回测。
- 已用严格 source-tree 校验通过的 immutable `c8d102eba7cc54327e29987707c3bae99dfbf3c3` 启动 full-OOS
  既有 `gate backtest --persist`：历史起点 `2025-01-01`、target 上界 `2026-06-01`、安全预算 7200 秒。
  该操作只在成功后原子新增回测 run/明细/月指标，不发布产品预测、不激活、不推进生产增量状态。
- 当前 producer-ready 输入为 `full-20260908-063331-69a69e87e803` / `snapshot-1d335ad33e23ca7e7c8f5b64`。
  正式 333 条完整七字段 Request 已与先前冻结 Request 逐条相等；generation 与 09-05 算法验收不同，
  不把新回测与旧 Native 参考直接宣称为同代等价。原有同代333条零差异结论继续有效，按最新授权分别记录两类输入。
- 日间任务日志位于 ECS 私有候选目录的 `full-oos-persist-20260908.log`，已启用十分钟跟进；
  尚未取得 Gate 成功结果或数据库提交读回，不将启动视为完成，不自动重试失败任务。
- 本轮未切换 current、未修改 systemd/launchd、未执行 DDL。ECS current 的 12 个历史 `.pyc` 完整性偏差
  仍未处理，部署/切换前必须恢复严格校验，不通过忽略文件放宽验证。
- 独立只读审查确认 W3A 还有临时接入缺口：现有 comparator runner 的批准目标仅含 W1/W2，
  W3A 会在启动前被拒绝；receipt producer 当前还会重新启动 Native/Blackbox，不提供已完成结果的复用入口。
  正式 Gate 不受此限制。下一步须最小化补齐 W3A 的受控凭据接入，并保持 receipt、正式回测及切换现场的
  各自的 generation/snapshot 绑定；不得手写成功凭据、混称 09-05 与 09-08 为同代，或直接运行必失败的旧 comparator。

**现场基线日期**：2026-09-05 Asia/Shanghai（后续核验日期在证据段落分别记录；计划修订不刷新现场水位）

**基线 Git 提交**：`20e98934e9d5399e3d509f486a8a650d95ad7639`

**W2 前一算法候选提交**：`f947426d969d6c3e3879e707f7a0564345788d9a`

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
- 所有算法业务输入只来自五文件 DataBridge generation 与 Request；经本计划批准的 stateful successor 可额外读取
  平台验证过的方案私有派生状态，但该状态不得成为源数据、数据库事实、跨方案接口或算法语义 fallback；
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

W3A 和 W3B 各自作为不可拆分迁移 wave，但 successor 之间不得保留 cache-family publisher/consumer 依赖。
W3C、W3D 在各自 wave 内允许逐方案切换；每个 stateful successor 必须拥有独立状态 namespace。W4A/W4B/W4C
不进入 ECS；它们只在 17 个可读源码 Native 完成 ECS 验证并以同一 immutable archive 晋级 Mac3 后，作为
Mac3-only binary bundle 单独推进。

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
能在算法验收自身冻结 generation 的同一完整 Request 集上通过，三个日期与方向必须逐行零差异；
该冻结输入可以早于正式入库输入。

同输入零差异证据使用随 immutable release 发布的临时 receipt：整批 wave 为
`deploy/native_successor_equivalence/<wave>.json`，逐方案 wave 为
`deploy/native_successor_equivalence/<wave>--<old_base_scheme_id>.json`。receipt 必须由当前 release 的
`native-successor-controlled-comparator` v2 生成，并绑定 comparator 源码 SHA-256、generation、data snapshot、
五文件 SHA-256、Native/Blackbox 环境指纹及 old/new code hash。comparator 使用同一七字段 Request CSV 直接
执行两侧程序，读取两份标准五字段 CSV 结果，逐行核对 Request 顺序、ID、三个日期和方向；任一差异直接拒绝
生成 receipt。preflight 会在
数据库只读快照中锁定 successor 的完整
持久化 backtest fact 集，并从每行 `source_row` 重新推导完整七字段 Request 摘要、Request 数量、ID/三日期摘要
和标准五字段结果摘要。receipt 的 Native/Blackbox 结果摘要必须相等，五类 mismatch count 必须全为 0。
两类输入的 Request 数量、ID/三日期覆盖必须完全一致；输入相同时还要求三个 cutoff key 和方向摘要与正式事实集
完全一致，输入不同时分别保存摘要，不将数据修订误认为算法改造差异。规范化 receipt
中的 Native 环境指纹还必须等于现场 `forecast_env` 的 conda explicit package 集指纹。receipt 进入 plan SHA，
apply 在事务内重新采集、重新推导并复验。receipt 缺失、抽样数量、手工摘要、旧 comparator 源码
或任一 identity 不匹配均 fail-closed。receipt 不保存包含自身的 Git commit，避免 tracked receipt 的不可满足
自引用；其内容由 comparator 源码 SHA、当前 release source-tree/archive、receipt 文件 SHA 和 plan SHA 共同
冻结。以下为 09-05 早期执行记录，不覆盖本文顶部最新现场核验：当时 W1A/W1B canonical receipt 仍与各自 exact code 一致，但尚未随新的 immutable release
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
backtest CSV Request。具体算法列集合以交付脚本实际读取与校验为准，迁移证据记录其摘要；当前 Metadata 不含
`required_columns`，不得通过此次优化新增方案级输入合同。Request 区间和代码 hash 在测试前不得预填。

| Wave | Successor 集合 | 输入字段摘要 | Request 区间 | script SHA-256 | 状态 |
|---|---|---|---|---|---|
| W1A | `t1_daily_{5y,10y}_bbv2` | daily date + 1Y/5Y/10Y；其余四文件做合同校验 | feature 2025-01-02..2026-05-28；各 337 条 | `126667bf95e24aeab77771d96d73cd8e43c66c39db26fd61b0ba8109d9bdc78d` | ECS_0905_FULL_COMPARATOR_PASSED |
| W1A | `t5_daily_{3y,5y,7y,10y}_bbv2` | daily date + 1Y/3Y/5Y/7Y/10Y；其余四文件做合同校验 | feature 2025-01-02..2026-05-22；各 333 条 | `126667bf95e24aeab77771d96d73cd8e43c66c39db26fd61b0ba8109d9bdc78d` | ECS_0905_FULL_COMPARATOR_PASSED |
| W1B | `weekly_5y_direct_0529_bbv2` | week_id + 1Y/5Y/7Y/10Y 周频收益率；其余文件做合同校验 | feature 2025-01-03..2026-05-22；72 条 | `59909549fee682c61a90c1394672f40b7204f67e35f692b04577cc49498a19c8` | ECS_0905_FULL_COMPARATOR_PASSED |
| W1B | `weekly_7y_cross_d_overlay_0529_bbv2` | week_id + 1Y/5Y/7Y/10Y 周频收益率；其余文件做合同校验 | feature 2025-01-03..2026-05-22；72 条 | `fb2baa38fa8b614737f0c2f87bff90626e2d8d268e5375362bf863554096e680` | ECS_0905_FULL_COMPARATOR_PASSED |
| W1B | `weekly_10y_d_overlay_0529_bbv2` | week_id + 1Y/5Y/7Y/10Y 周频收益率；其余文件做合同校验 | feature 2025-01-03..2026-05-22；72 条 | `e52e221a0046e8623107359b4fe3c2f7643e422b3ed9068c0cf317e9cdeafeed` | ECS_0905_FULL_COMPARATOR_PASSED |
| W2 | 两个 `daily_*_v28_bbv2` | 完整五文件；V28 daily/weekly/monthly 因子与真实 T+5 grid | feature 2025-01-02..2026-05-22；各 333 条 | 5Y `32aa7948d5f90bdd3de72ce46461bdc8f6dafeb24ece450c34cba84eac5480cc`；7Y `5fec87a6be3612c911f17f546ae4687e58a64b1d5a7be75a407e1fbb681861a4` | ECS_OFFLINE_REVALIDATION_PENDING; OLD_100_REQUEST_GATE_REMOVED |
| W3A | `liwei_0616_cons_sda_k3_div_k10_bbv2` | 五文件中算法所需因子；仅进程内 cutoff-keyed cache | feature 2025-01-02..2026-05-22；333 条 | `9cebc6eab7df69ed48e68163fc0cfc5229ad616fda4989924e2b34d78274c250` | ECS_CONFORMANCE_PASSED_NO_PERSISTED_BACKTEST |
| W3A | `liwei_0616_5y01_full_oos_k3_div_k10_bbv2` | 五文件 + 方案私有增量 Phase-A 状态；连续 full-OOS 排名 | 完整333条 feature 2025-01-02..2026-05-22 | `ad9bdacf5063a427ecc8b70852e045f4822ba9af1b6d8fcd171cd2d779e95103` | ECS_ALGORITHM_EQUIVALENCE_ACCEPTED; PERSISTED_BACKTEST_RUNNING |
| W3B | 三个 10Y successor | 五文件；full-OOS 方案使用独立增量状态，非 full-OOS 优先纯算法优化 | feature 2026-08-28 | 未生成 | PENDING_W3A_STATE_PILOT |
| W3C-D | 五个 `liwei_0616_*_bbv2` | 五文件；逐方案判定 stateless 或独立增量状态 | feature 2026-08-28 | 未生成 | PENDING_STATE_CLASSIFICATION |
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

随后按真实热路径继续优化，确认旧入口即使只收到一条 Request，也会把该月从月初到请求日的所有交易日全部
加入 `test_idx` 并逐日训练；2026-05-22 的 5Y/7Y 单条调用分别无效训练 13 个测试日。successor 现只对
Request 明确列出的日期训练 LightGBM 和生成预测，同时保留原月初至 cutoff 的信号选择上下文；同一次 batch
内只构建一次五文件对齐、457 个特征和 586 个信号矩阵。所有滚动、IC、训练和信号选择仍按各 Request 的历史
前缀截断。ECS 同一 Request 的 5Y 从 69.38 秒降至 13.56 秒，7Y
从 53.06 秒降至 11.97 秒，两侧 Output SHA-256 分别逐字节不变。本机当前 generation 的 333 条完整对照也
逐字节一致：5Y 从 641.35 秒降至 613.51 秒，最大常驻内存约从 3.15 GiB 降至 1.70 GiB；7Y 从 406.63 秒
降至 398.03 秒，约从 2.74 GiB 降至 1.78 GiB。第一月结果同时在包含后续 16 个月数据的预计算矩阵下保持
逐字节一致，构成未来行不影响历史前缀的直接证据。

W2 的剩余耗时不是调度或文件缓存：333 条正式区间包含 17 个月，5Y/7Y 在保留 265 个 config 和 3/2 个 seed
时分别需要训练约 264,735/176,490 个 LightGBM 模型。ECS 是两个物理核心、每核两个超线程；4-worker 已是
实测最优，2/3/5-worker 均更慢。最终代码 hash 的 ECS 复测中，5Y 的 100 条需 618.85 秒、峰值 RSS
910,740 KiB，超过 600 秒硬门槛 18.85 秒；7Y 的 100 条为 371.24 秒、峰值 RSS 918,856 KiB，已通过该项。
W2 作为同一 wave 继续 fail-closed；不得生成新 receipt、持久化回测或 cutover。

另行验证了按 `window + split_pct + min_child_samples` 把 265 个配置绑定到同一 worker、提高线程内 Dataset
cache 命中率的候选。相同 2025-01 月份 18 条 Request 下，5Y 输出逐字段一致且 RSS 从约 806 MiB 降至
约 598 MiB，但 wall time 从 93.86 秒增加到 95.97 秒；19 个粗粒度配置组的尾部负载不均抵消了 Dataset
构造收益。该负优化已完整撤销，不进入当前候选。

独立审查曾发现首版优化把信号选择锚点随 `requested_dates` 一起缩到了子集首日，存在稀疏 Request 改变
月初选信号基准的风险。当前实现已将两者分离：LightGBM 仍只训练 Request 日期，但信号选择继续使用旧路径
完整月份中的首个有效交易日及历史周期。修复后在 2025-02 与 2026-04 两个自然月分别对 5Y/7Y 执行 39 条
dense batch、6 条首中末稀疏子集和每月一个独立单点；稀疏与单点按 `request_id` 回查 dense 的五字段均
完全一致。审查中的 Important 项已修复，且没有发现 Critical 或 Minor 项。

一次 Native 性能对照暴露 `cache=True` 的 Numba 编译会在 immutable release 源目录写入 `.nbc/.nbi`，使
3162 release 的 source digest 被 preflight 正确拒绝。现场先切到已核验 f947，把受污染目录移动到独立
quarantine，再从原始、SHA-256 已核验的 3162 archive 重新预安装并激活。恢复后 preflight 已通过 release
完整性校验，只按预期拒绝 `old_still_deployed=['daily_5y_2_v28','daily_7y_1_v28']`；六个项目控制面 active，
没有遗留算法进程或部分 Output。后续禁止再从 immutable release 直接运行会触发源码旁 Numba cache 的 Native
诊断；此类比较必须在私有可写副本中执行。

W3A 的首轮冷路径试验在冻结 generation `full-20260901-063115-5636b51dacf6` 上使用
`feature_date=2026-08-28` 做了决定性性能试验。输入规模为 daily 3908×774、weekly
853×577、monthly 199×126，PIT 下界有 41 个有效测试行；三个 baseline 各需 265 个 config × 2 seeds。
完全关闭 Phase-A 持久 cache，使用最多 8 个进程内 worker、每个 LightGBM `n_jobs=1`、无子进程时，
`real 121.45s / user 469.44s / sys 31.85s`，峰值 RSS 790,052,864 bytes，仍停留在第一个 `STD`
baseline 的 `LGBMClassifier.fit`，`DIV` 与 `ACCWT` 尚未开始且没有结果输出。因此已触发单条 predict 120 秒
硬停止条件；该轮未创建 successor，未修改 Native。另一个 full-OOS 方案有 644 个有效测试行，约为该下界的
15.7 倍，不再继续无效消耗。该轮结论为继续保持 Native，直到能在不恢复跨方案持久状态、不缩减冻结 grid、
不放宽 cutoff 的前提下形成满足性能门槛的独立实现；后续 `cons_sda` 两阶段候选与剩余 blocker 见下文。

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

静态热路径复核进一步确认，W3A 的 STD/DIV/ACCWT 在所有影响 Phase-A 的字段上完全相同，包括 tenor、特征、
265 个 config、两个 seed、窗口和 IC 选择；三者仅在后续 ensemble 与 signal 组合上不同。冻结窗口含前一年同期
21 条和当前月 20 条，旧冷路径因此训练约 65,190 个模型。下一候选必须在单进程内只训练一份共享 Phase-A，
并使用两阶段精确执行：历史月仍对 265 个 config 全量训练以确定排名；当前月只训练 standard/accwt top-10 与
diverse top-5 的实际配置并集，再分别执行三套原组合逻辑。配置并集最坏 15 个，模型训练上界约降至 11,730，
减少约 82%；该缓存只存在于单次 CLI 进程，不恢复跨运行持久 cache。

2026-09-07 已按上述设计形成 `liwei_0616_cons_sda_k3_div_k10_bbv2` 严格两文件候选。它在进程内只构建一次
五文件对齐、457 个特征和 586 个信号，STD/DIV/ACCWT 共用一份 Phase-A；历史月继续运行 265 个完整配置，
当前月只训练 standard/accwt top-10 与 diverse top-5 的精确配置并集。`multiprocessing`、Numba 磁盘 cache、
跨方案 import、平台 import、DB、网络、额外 payload 和持久状态均不存在；4 个 Python worker thread 内每个
LightGBM 固定单线程。脚本/config/metadata SHA-256 分别为
`9cebc6eab7df69ed48e68163fc0cfc5229ad616fda4989924e2b34d78274c250`、
`2e4d0b2d5888a02bfa8eba0c2b83d346cd98075552c2e662edb7b50d1ed21bf8`、
`2ac8d5bae4aa7cd34e193bc880a1674ac46467038b0b01659dc8c5ad77401387`；候选基于 Git
`ef823ffd2c3aadad2a1602e90753c8a91955800f` 的干净基线构建。

最终验证绑定 snapshot `snapshot-d622a1ba1bfb27c65969aaad`。五文件 SHA-256 依次为 calendar
`f583ca3411195aa5de4bd50107646d0753edc39cb83708b89b353e5aa8c14c47`、daily
`2eabbb888f9ba9422bfa14d4d923f6e44aa21c53f7a93e30bf40fe1118fcd7f1`、weekly
`426ac86f3dff43f7b46438e6a2f76f05abe6f3db2469d498f7afff0baabbdf12`、monthly
`977ef49b5f106cff4027c2e6486aac604e915b1357b60263544af7823a436dc9`、catalog
`bb5ee17f097369ed4498744b604d51597e4814c9e31887e90aad62c2b2bd8965`。333 条正式 Request SHA-256 为
`7e27c60ff08c35622d0f04059d9ab3daba2fa25c7cae32d707ef9f5f89a2f231`，Output SHA-256 为
`87473d4e7940c216b28d64774e3cda7a2636490124e2a8e827efabd672f2a48e`；旧 Native core + 权威只读
Phase-A cache 的直接 comparator 为 333/333、方向 mismatch 0。

最终性能为：同一 JSON 单点三次 `60.91/60.41/60.59s`；正式区间首/中/末单点
`74.00/96.99/60.17s`，且三点分别与 Native 单点 mismatch 0；100 条升序/降序/确定性乱序分别
`516.18/518.46/516.49s`，规范排序 SHA-256 均为
`f7f6b29353802722da8c86155786e670a4916edcef87e71dfe508f700eb378f3`；333 条完整区间为
`1386.59s`、峰值 RSS 1,001,176 KiB。全部满足 120/600/1800 秒和 4 GiB 门槛。

独立审查首轮发现同周早期 Request 的 weekly projection 会受 batch 中更晚日期影响。最终实现改为由完整权威
calendar 固定公共 weekly 生效日，仅在已训练模型的当日 predict row 注入 Request 自身 weekly cutoff，并在
公共历史 baseline signs 上只替换目标日后重新执行原 consensus/streak。修复后，同周周初/周中/周末三条
dense 与逐条 single 完全一致，且三条分别与 Native 单点 mismatch 0；删除同周后续 Request 的 subset 不改变
早期结果；daily 文件在 cutoff 后追加 9 个未来交易日时 Output SHA-256 不变。Dataset cache 同时加入每次
Phase-A context 身份并精确校验 current selected config set。独立复审无 Critical、Important 或 Minor 发现；
最终全量回归为 `654 passed, 5 skipped, 5 warnings, 194 subtests passed`。额外字段、缺少 cutoff、重复 ID、
缺失五文件四类负向测试均非零退出、stdout 为空且没有 Output。

以上只把 `cons_sda` 推进到离线 `CONFORMANCE_PASSED`，没有生成 canonical receipt、持久化 backtest、Registry
身份或 cutover。W3A 仍是不可拆分迁移 wave；`full_oos` 从 2024-01 起对全部历史 OOS 日给 265 个配置持续累计
排名，原完整冷路径单点约有 34 万次 LightGBM 拟合；这不是所有等价实现的理论下界。2026-09-07 已批准每日
增量计算及必要的方案私有可重建状态，随后经冗余审查改为 5.2.1 的算法先行试点，先证明新增 cutoff、标签成熟
和尾部重算的最小依赖。试点通过以前 `full_oos` 继续保持 Native，W3A 不得进入持久化回测或切换。

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
超过 8。Liwei 不迁移旧 Phase-A publisher/consumer/cache wave；successor 必须独立运行。能通过共享进程内
Phase-A、精确配置并集、输入对齐复用等纯算法优化满足性能的方案保持 stateless。只有连续 full-OOS 等经证据
证明冷路径无法达到门槛的方案，才允许使用 5.2.1 定义的方案私有增量状态。三个 5Y ALL-K10 保持当前
`platform_live_pit_variant`，不得借状态改回 source-original 或缩减历史评分语义。

任何 successor 都不得恢复跨方案状态、publisher/consumer 顺序、未来窗口、模型对象持久化或数据库 cache。
增量状态只是同一算法冷路径的可重建派生结果；状态命中和从空状态重建必须产生完全相同的五字段 Result。

#### 5.2.1 Phase 2A：先做算法试点，再接入最小状态能力

##### 修订结论与适用范围

本节替代先前“先建设通用状态框架，再改造算法”的执行顺序。用户已认可每日增量计算和必要的方案私有状态；
本次收缩实现范围，先证明 full-OOS 的最小依赖和冷/热等价，再决定是否需要扩展平台。此前约 34 万次拟合是
已有完整冷路径的工作量，不是所有等价算法实现的理论下界，不能据此跳过按需计算优化。

目标是每日只处理新增预测及必要的受影响尾部，保持原 grid、seed、训练窗口、排名和控制器语义。业务输入仍为
五文件与 Request，Result 保持精确五字段。已有 stateless Blackbox 的 canonical 字节、执行方式和 exact version
不变；允许的持久状态仍只属于单个 successor，不恢复跨方案 publisher/consumer 或 Native cache wave。

##### 已有实现的借鉴与复用

| 参考代码 | 已有能力 | 本次使用方式与限制 |
|---|---|---|
| [current55 Engine](../../schemes/seven_y_current55_lgbm_001_v2/delivery/seven_y_current55_lgbm_001_v2.py) 及 002 | 按月缓存模型、按年缓存因子选择、按日期缓存原始信号；全部在进程内 | 借鉴按算法实际重算周期划分依赖；不能把 Native 每日重训擅自改成月度重训 |
| [5Y online](../../schemes/five_y_factor_rule_online_v1/delivery/five_y_factor_rule_online_v1.py) | 单点向前查询到控制器样本充分即停止，batch 按月共享训练和特征 | 借鉴按需回溯与重叠计算复用；full-OOS 的全历史排名不能直接换成它的有限样本控制器 |
| [10Y maj4](../../schemes/ten_y_t5_maj4_k3_ic_yearly_v1/delivery/ten_y_t5_maj4_k3_ic_yearly_v1.py) | 固定参数按 cutoff 训练，batch 按 cutoff 去重 | 借鉴单日入口和批内去重；不以固定单模型替换原 265 配置算法 |
| [现有文件锁](../../shared/exclusive_file_lock.py) | inode 校验、非阻塞 flock、稳定锁文件 | 平台直接复用，不新增锁实现 |
| [运行路径](../../shared/runtime_paths.py) 与现有 Blackbox runner | 外置状态路径、私有运行目录、进程预算、输入复验和 Output 清理 | 平台沿用已有边界，确有状态需求时仅增加受控快照读写 |

以上算法参考是代码审查证据，不能作为 successor 的等价或性能验收。两文件算法不得 import 其他方案或平台代码；
借鉴算法组织方式后形成自包含交付。已有 5Y online 的 batch 自检失败后逐点 fallback 不迁入本项目，迁移验收
仍要求差异直接失败。现有进程内字典不能冒充已经实现了跨日持久状态。

##### A0：先列出最小计算依赖

首个对象固定为 `liwei_0616_5y01_full_oos_k3_div_k10_bbv2`。先从 Native 代码逐项追踪实际被最终方向消费的值：

| 计算项 | 必须证明的边界 | 首选优化 |
|---|---|---|
| 因子构造与筛选 | 每个训练 cutoff 的数据前缀、筛选截止和重算周期 | 特征一次构造；完全相同的筛选上下文进程内复用 |
| Phase-A 模型结果 | config、seed、训练窗口、校准、周/月输入投影 | 同一方案内相同 baseline 共用；仅训练真正缺失的计算键 |
| 月度配置排名 | 源码使用月初之前且排除 horizon 的 OOS 前缀 | 复用已证明稳定的历史预测；保持评分分母、顺序、并列规则 |
| 信号筛选与组合 | 滚动准确率窗口、季度重平衡、最终实际使用的输出 | 按实际依赖生成，避免完整报告和未消费的诊断计算 |
| 连续方向控制器 | 零方向是否重置、同向计数、fallback 触发 | 首选调用内重算完整 OOS 控制序列，不提前保存控制状态 |
| 输入尾部与标签 | T+1/T+5 标签成熟、周内/月内投影变化 | 单独识别可稳定复用区间和必要重算尾部 |

特别注意：

- **标签成熟不等于历史数据修订。** T+5 旧预测的评分标签会在后续交易日才可用；必须在原算法允许的 cutoff
  纳入评分，不提前读取，也不把首次未知标签永久缓存为已完成统计。
- **昨天预测时的尾部不一定等于今天回看时的历史计算上下文。** 周内投影等变化已在 cons_sda 暴露；试点必须
  确定哪些历史 Phase-A 输出稳定、哪些需在尾部重新计算，不能默认“每日只新增一条，其余所有数组永远不变”。
- 对同一历史 Request，追加未来数据后的结果必须不变；对今天的新 Request，其已知标签、排名和尾部计算可以
  按原语义发生变化。两种验收分别执行。
- 无法证明的依赖保留原计算；若因此仍不满足性能，报告具体热点，不以改窗口、少 seed、少配置规避。

A0 交付物是本节内的实际依赖摘要、源代码 hash、计算次数/耗时分布和拟保存字段清单。此时不修改 Intake、
Activation、Scheduler 或 Runtime Profile。

**A0 实测与依赖结论（2026-09-07）**

基于代码提交 `4104174d53066cd13d4669e05c4ae35814b0379d`，使用已有只读冻结 snapshot
`snapshot-f42540ebc533428ca6c869e2`；五文件均重新计算 SHA-256 并与 snapshot manifest 精确一致。本次数据不同于
前述 cons_sda 的 snapshot，未拿两代输出做方向比较。五文件摘要依次为：

- daily：`8e98c84d577304b69c432abfd3bf7438133add3af542b69534d3365949a9f223`；
- weekly：`d51c65df76ab816b1d777ac7fd7377aac7d62b93b12ce30ebc4c5da26b0685bb`；
- monthly：`1ee62d0939ec73350db5b01d98343557cc11ef0a48497c93f4d31f4ba655ed27`；
- calendar：`fc42e4b6a59edcaf81ded827f82ea639a2a17a81d1763f23aa3bb386d153a4b2`；
- catalog：`bb5ee17f097369ed4498744b604d51597e4814c9e31887e90aad62c2b2bd8965`。

Native `core/v31_common.py` SHA-256 为
`dd692e4e6bad8db89290ddec3dd73edaab64cb8135561e9108b40df980da3df0`；参考 cons_sda 脚本 SHA-256 仍为
`9cebc6eab7df69ed48e68163fc0cfc5229ad616fda4989924e2b34d78274c250`。本机环境为 arm64、Python 3.13.12、
NumPy 2.3.5、pandas 2.3.3、LightGBM 4.6.0；使用 `forecast_env_blackbox_v1`，Numba 缓存外置于私有临时目录。
这不是 ECS 性能证据，也不是完整 Runtime Profile 指纹验收。

| 检查 | 实际结果与边界 |
|---|---|
| 输入准备 | 2026-08-28 截止，3908 行日频、457 个特征、586 个信号；独立 Native 构造的特征及列序、信号、T+1/T+5 标签与参考路径逐元素一致；参考准备耗时约 0.81 秒 |
| Phase-A 共用 | 源码确认三 baseline 的训练、筛选、grid、seed、horizon/purge gap 相同；差异在后续组合。644 行 × 265 config × 2 seed：单份训练上界 341320 次，原三个独立冷调用上界 1023960 次 |
| 单日训练内核 | 2026-08-28 的 265 config、两个 seed 聚合后方向与概率逐元素一致，概率最大差值 0；Native 串行约 5.72 秒，参考 4 worker 约 1.34 秒。并发不同，不解释为同并发算法加速，也不计作完整 predict SLA |
| 不被最终方向消费的路径 | 外层从 `vote_score` 即 `vs_full` 取符号后执行 consensus/streak，不读取内层季节性阈值后的 prediction。合成 Phase-A 隔离干预下 644 行外层明细完全一致；只证明依赖，可从新 successor 删除该内层计算，不证明 644 行真实方向验收 |
| 周内尾部 | 2026-08-24→25→26→27→28，旧 cutoff 当日特征每次变化；28→31 旧特征未变。必须重算受影响旧预测点，不能只算新增日；新点训练 gap 前未变，不代表旧点预测特征未变 |
| 标签与控制器 | 月度模型排名按 OOS 行位置 `pi[0]-5` 取前缀；信号准确率使用 T+1 标签。零方向不重置 streak，fallback 替换不反馈计数；每次重算可省去成熟标签和控制器的持久化修补逻辑 |
| 候选状态体积 | 644×265 的 int8 preds 与 float64 probs 共 1535940 bytes（约 1.46 MiB），不含日期、身份与输入摘要；没有证据需要对象库、分段存储或多份统计状态 |

据此 A1 首选仅保存配置顺序固定的 Phase-A 聚合结果、日期及身份/输入校验摘要；标签、排名、信号和控制器
仍在每次调用内重算。不保存模型、逐 seed 输出、累计排名或控制器状态。单点 530 次拟合仅是新增点上界；
若需重算一个旧尾点，则为最多 1060 次的条件上界，漏跑、补行或历史修订不能套用此上界。

历史源前缀修订直接拒绝状态复用，不开发任意历史修订的局部修补算法：close 修订会影响前移标签，EWM 和
streak 传播不能假定固定短窗；IC 筛选历史、列序、日历映射及缺失/补行变化也可能令全部模型失效。
正常截止推进造成的辅助频率投影尾部变化则按实际依赖定位并重算 suffix，必须与冷路径比较。

独立审查确认上述方向，无 Critical；指出并已补测独立 Native 输入准备，且明确保留旧尾点重算、T+1 标签、
历史修订拒绝及证据适用范围。A0 的局部检查不是完整 Request、冷/热或性能验收；A1 私有试点尚未晋级 A2。

##### A1：单方案最小算法试点

在开发或 ECS 私有临时目录实现自包含候选。沿用已通过的 cons_sda 优化经验，并按 A0 结论保存最小派生状态。
先验证两条内部路径：从空状态重建；从已核验状态推进到下一 cutoff。predict/backtest 使用同一计算核心，
允许调度方选择一次单点或一次批量，不能形成两套算法。

状态仅保存必要的预测数组、评分所需数据和已证明可恢复的控制信息；禁止模型对象、pickle/joblib、可执行代码、
完整源数据副本、最终 Result 或数据库事实。内部字段由算法负责解释，平台不写 Liwei 参数或排名逻辑。

试点使用一个完整快照文件，文件内封装身份摘要和派生 payload，优先验证整体读写成本。使用无可执行反序列化的
格式，数组读取禁止 object dtype/pickle；身份和 payload 必须在同一个原子替换单元内。不预先建立
objects/manifests 对象库、parent 链、current/previous 双指针、分段索引或自动回收。

快照绑定 scheme/code/config/metadata、算法状态格式、环境 fingerprint、已计算区间、标签成熟水位和实际计算所需
输入摘要。本次 Request 文件 SHA 与完整 generation/五文件 SHA 另作为验收来源证据；随机 request_id 或 batch
分割方式不应进入决定算法状态复用的计算键。新截止到来时补充的评分或尾部记录写进新快照，原业务事实不变。

初次构建与断点验证可以耗时较长，必须单列时间、CPU、RSS、读取/写入字节和模型拟合次数。不得使用旧 Native
cache 给候选初始化后声称验证了冷启动；旧 cache 只可作为经输入身份核验的 comparator 参考。

**A1 短区间试点证据（2026-09-07；不是 A2 或生产验收）**

私有两文件试点位于 `/tmp/bfl-full-oos-a0.w33Sab/delivery/`，脚本 `full_oos_trial.py` 的 SHA-256 为
`c2c113c77ae0bea4b438a2de0d2b89f0fef5490cf02d0531511b85e6d9fbd236`，配套 Metadata 为 `full_oos_trial.json`。
它没有进入 canonical、Intake、Registry 或部署矩阵；`--state-input/--state-output` 仅为私有试点参数，不能视为
当前平台已经支持的接口。全部输入仍来自 A0 同一冻结 snapshot，未读取旧 Native cache。

- 使用合法日期构造的 2024-01-19..2024-02-01 共 10 条开发 Request，连续启动 10 个独立算法进程，覆盖周内、
  跨周和月初。首次从空状态训练 14 个 OOS 日耗时 20.51 秒；后续 9 次包含进程启动、读取、校验、重算和写入的
  wall time 为 3.38–5.04 秒，每次实际训练 1 或 2 个日期。该 Request 集不是 333 条正式验收区间。
- 最后截止的完整 23 日冷计算耗时 30.01 秒，与恢复路径的 dates、feature 摘要、全部 265 config 的 preds/probs
  及规范化 header 逐元素/字段一致；最终五字段 Result 一致。同截止重试 2.14 秒且训练行数为 0。
- 独立 Native 从输入准备开始、从零训练完整 23 日 × 265 config × 2 seed，再按原三个 baseline 组合，耗时
  128.96 秒；全部配置日期上的方向、概率与试点数组精确一致，最终方向一致。此 comparator 仅共享经源码确认
  相同的三个 baseline Phase-A，不读取试点数组或旧生产 cache 来初始化 Native。
- 最终快照 902671 bytes；验证器记录的算法子进程峰值 RSS 为 815874048 bytes。本机非独占环境，以上耗时
  不能外推为 ECS、644 日成熟历史或正式 333 条区间的性能通过证据。
- 10 类负向/故障注入通过：空 feature 证明、概率摘要损坏、倒序日期、环境身份不匹配、Metadata 身份不匹配、
  Result/状态同路径、Result 覆盖输入状态、状态输出覆盖输入状态、历史源前缀修订、状态生成后 Result 写失败。
  所测场景均拒绝且没有新增 Result/状态，既有输入状态摘要不变；另已验证未来状态不能服务历史 Request。
- 独立审查提出的输出互相覆盖、状态结构校验缺失、身份信息遗漏三项 Important 已修复并重跑上述最终版本。
  语法及 import 边界检查通过，无 project shared/schemes、DB、网络或算法子进程导入；未运行平台全量回归，
  因为本轮未修改平台或 canonical 算法。

精确 Request、逐次指标及对照结果见私有 `/tmp/bfl-full-oos-a1-final.rLITH8/trial_report.json`，负向证据为同目录
`negative_report.json`；开发探针与完整数据不纳入 Git。临时目录仅是开发证据，不承担发布或恢复依赖。

A1 留给 A2 的问题为发布后的清理异常语义、可选 numba 身份读取、未来 calendar 追加与读取资源上限；
处理进度见下方 A2 实测记录。真实 generation 的历史 weekly/monthly 前缀是否稳定尚未验证；任意修订仍显式
重建。不能凭此次短区间结果接入 A3 平台或开启 cutover。

##### A2：算法等价与性能验收

**2026-09-07 本地私有验收记录：原 A2 已完成；以随后重验通过的 A2c 为当前算法候选**

- 候选为 `/tmp/bfl-full-oos-a2.mi984B/delivery/full_oos_trial.py`，SHA-256
  `01aa9371b82ad33f155f535c66a321c42d00c7edcfc937555c95df629c7ec567`；不改 canonical 或 Native 源码。
  继续绑定冻结 snapshot `snapshot-f42540ebc533428ca6c869e2` 的五文件；完整身份记录见该目录 `identity.json`。
- 修复上述 A1 边界；读取快照先验证真实文件大小、未压缩 ZIP 成员、NPY shape/dtype 与 payload 长度，
  再通过同一个只读文件句柄加载，避免路径替换绕过检查。独立代码审查未留 Critical/Important 问题。
- 20 项真实文件/CLI/故障注入检查通过，覆盖损坏数组和身份、输出路径冲突、资源声明越界、压缩/对象数组、
  历史 calendar 修订拒绝、纯未来 calendar 追加复用、Result 失败回收新 state，以及已发布 Result 的清理告警。
  证据为 `boundaries.report.json`；该集合不是完整 A2 故障矩阵或 A3 平台安全验收。
- 首条正式 Request 之前，截至 `2024-12-31` 的 242 个 OOS 点从空状态预热，耗时 **375.34 秒**，
  峰值 RSS **947,978,240 字节**，state **1,489,926 字节**；未使用 Native cache，未预装正式区间结果。
  证据为 `prewarm.report.json`。该时间是本机预热实测，不是 ECS SLA 或已批准恢复预算。
- 333 条正式 Request 覆盖 `2025-01-02` 至 `2026-05-22`，沿用已冻结七字段，仅生成 successor request_id；
  Request SHA-256 为 `dcd62088ce197adee97d95dc944dd8f4c2ab3cdb8fe2f30a7b2a4f68f9a2f340`。
  333 条推进已完成，耗时 **1185.18 秒**，峰值 RSS **876,691,456 字节**，state **2,382,699 字节**；
  100 条升序/降序/乱序分别 **339.12 / 360.91 / 362.03 秒**，规范化 Result 与最终状态完全一致。
- 已有 242 点前置历史后，十个全新进程逐日仅暴露当时可见输入，单次 **4.28–5.70 秒**；五字段结果与正式
  批次前十条完全相同。一次补齐内部十日计算仅输出末日需 **20.09 秒**；末日从空状态独立冷算需 **449.79 秒**。
  两者与十次逐日推进的末态全部数组/header、Result 精确一致；三次相同 Request 重试约 **2.66–2.72 秒**，
  结果与 state 摘要不变。冷算时间单独披露，不冒充 ready-state 每日 SLA。
- `1+332`、`100+233` 的 Result 和全部规范化状态已与 `0+333` 完全相同；100 条中的非连续乱序子集也通过。
  独立首/中/末点、`332+1` 与 Native 333 五字段比较也已通过；冷路径完整 333 已完成，耗时 **1562.98 秒**，
  Result 和全部规范化状态与恢复路径完全一致。
  实时完成/缺项清单见 `aggregate.report.json`，未执行项不能从相邻测试推导为通过。
- Native 原算法从原始五文件独立冷训练 **575 点 × 265 配置 × 双 seed**，耗时 **971.53 秒**；
  完整 dates/config 顺序/preds/probs 已与候选正式区间末态逐项精确相同。另经 Native 原函数独立训练每个
  Request 当前点，再逐 Request 重算标签、排名、信号与控制器。独立动态前缀检查覆盖 **333/333**：
  261 个仅当前末行特征变化，72 个前缀完全不变，历史训练可见标签、close、fallback 和 selected 列索引均一致；
  `reference-dependency-review/report.json` 记录重跑 **231.41 秒**与完整输入/代码/环境闭包。
  随后实际完成 333 条五字段比较，**零差异**，耗时 **1008.38 秒**，见 `native-comparison.report.json`。
- 另有 9 项当前字节的 Request/源修订/非法方向失败检查通过，见 `contract-failures.report.json`。
  并发开发负载下的本机结果不能代替 ECS 运行验收。

**真实 generation 复用边界已在 A2c 收紧并重验（不放松历史修订拒绝）**

2026-09-07 使用既有 ECS 专用 SSH 身份只读取得 current 五文件，下载前后 manifest SHA 一致，下载后五文件
全部匹配 manifest：generation `full-20260907-063337-3baeb4277bae`；只读核对 current release 为
`3162f70e67f53b7cdb65a3e8792d42c6fab32d15`，本轮未部署、写库或操作调度。
与原冻结副本比较，正式区间首/末 cutoff 的 daily/weekly/monthly/calendar 前缀全部相同；截至 `2026-09-04`
则有 daily 1 格、weekly 5 格、monthly 41 格变化，calendar 不变。证据为 `real-generation-prefix.report.json`。
原 A2 候选对整张 raw frame 做前缀摘要，会拒绝这种复用，因此即使原离线矩阵通过也不能称为每日运行已闭环。
独立动态检查已确认 daily/weekly 变化列均不在本算法必需字段中；monthly 仅 `M0317126` 属于必需列，但变化
发生在尚未消费的 `202609`，当前实际消费至 `202608`。两代 Native/trial 的完整特征、筛选特征、标签、
close、fallback 与全部信号矩阵精确相同；见 `reference-dependency-review/generation-report.json`。

据此只收紧状态证明为 `date + 28 个必需日列`、`week_id + 10 个必需周列`、
`month_id + 7 个必需月列且不晚于已消费月份`；原始五文件读取、全部算法函数、日期与 Result 不变。
第一次收紧版本 A2b 被独立审查拒绝：缺失的已消费末月补回时，仅比较旧行数前缀会误认新增月份。
反例实测四类旧前缀检查均通过，但实际四个历史日特征变化，因此没有晋级该版本。

修订为 A2c：校验月度状态时，按旧 `header.cutoff` 的完整已消费月份范围同时核对行数和摘要，不新增状态字段。
新候选在 `/tmp/bfl-full-oos-a2c.3PRPEW/delivery/full_oos_trial.py`，SHA-256
`9a32cd7e6c157a5d56a3274affd45a4431a3c3f35daa8ac88a400dab2dacb27e`，格式 `full-oos-a2-private-4`；
两文件 metadata 语义不变，独立 hash 为 `6645035fcdc1e361545f19df0bb98a8f43f70ee48485d2d87e464d157dad159f`。
独立审查的实际候选边界 10/10 通过；真实 CLI 也验证了未依赖列/未消费月变化复用、缺失末月补回拒绝、
正常跨月冷/热 Result 与全部状态相同、已消费月份修改拒绝、旧格式拒绝，见新目录 `dependency-boundary.report.json`。
已有 20 项文件/CLI 边界在新字节下重验通过；新预热与完整区间重验已独立完成，没有继承 A2 候选的通过结论。
原 Native 真值只有在五文件、完整 Request、Native code/config、环境与原证据摘要全相同后才可复用，并另行
记录新候选与独立 Native 数组/结果的实际比较，不改写旧 receipt，也不因旧 receipt 附带旧候选 hash 而重复训练 Native。

**A2c 当前字节的完整离线结果**（本机 ARM64、并发开发负载；不是 ECS executor 性能结论）：

| 验收场景 | 实测 | 结果 |
|---|---|---|
| 正式区间前从零预热 242 点 | 379.24 秒；state 1,489,926 字节 | 不预装正式区间结果 |
| 333 条正式区间增量推进 | 1212.10 秒；峰值 RSS 962,265,088 字节 | ≤1800 秒；state 2,382,699 字节 |
| 从零独立完成正式 333 条（含前置历史） | 1578.71 秒；峰值 RSS 955,285,504 字节 | 五字段及全部最终状态与增量路径一致 |
| 100 条升序 / 降序 / 乱序 | 354.69 / 369.57 / 357.91 秒 | 均 ≤600 秒；规范化结果及状态一致 |
| 十个新进程逐日推进 | 每次 4.17–5.74 秒 | 逐步暴露当时输入，五字段与正式批次一致 |
| 漏跑十日后一次补齐内部计算 | 20.72 秒；仅输出本次 Request | 与十次逐日推进及 461.90 秒独立冷算的末态一致 |
| 同一 Request 三次重试 | 2.75–2.79 秒 | Result 与状态摘要均不变 |
| 分批、子集、独立首/中/末 | `0+333`、`1+332`、`100+233`、`332+1` 全通过 | 分批末态一致；子集与单点按 ID 精确一致 |
| 当前候选对独立 Native 真值 | 333 条五字段零差异 | 完整 575×265 的 dates/config/preds/probs 逐项一致 |
| 当前候选负向检查 | 20 项文件边界 + 9 项合同失败 + 10 项依赖边界 | 全部通过；失败不发布 Result/新状态 |

独立首/中/末从前置状态复算分别为 **5.83 / 210.16 / 411.85 秒**，包含补算中间历史，不能冒充 warmed 单日
耗时。上述每日 SLA 由十个连续新进程的实测证明。状态缓存只省去重复历史模型计算，未修改原算法的日期、
模型窗口、标签成熟或最终控制器；历史修订仍显式拒绝，不自动退回冷训练。

另在私有目录以原 generation 推进到 `2026-09-04` 的 649 点状态，历史构建 **491.80 秒**；改用只读取得的
新 generation，同一 Request **3.28 秒**完成，Result 与完整状态均不变。该项证明这批无关修订不会触发重建，
不是跨 generation 的 Native 等价豁免。证据为 `real-generation-cli.report.json`，状态 SHA-256 为
`740a85ee11f5b4be12452f68342d7db3df3ccd9f08edad7e9ed0d8015f6cc55a`。

汇总为新目录 `aggregate.report.json`：`pending=[]`、`offline_matrix_complete=true`、`production_ready=false`。
完整 Result SHA-256 为 `d573f407b5486b145feacf2c3c1fa8ae7258805b0f54e9fdf6fc84223bbcb50f`；
正式末态 SHA-256 为 `eaf75d6037d67539685379efce94e802f0267f257357dc93634d1b6eaeccde49`。
`native-reuse.report.json` 复核旧 Native 证据闭包，并绑定当前 script、metadata、环境、Request、Result 及状态内部
identity/payload 摘要；独立审查发现的误用旧版本状态证据风险已修复并重验，当前无未修复 Critical/Important。
临时交付仍使用试点文件名和未验收 Metadata，不是已通过 Intake 的 canonical 包；不直接复制到生产运行。
本轮仅更新本 Markdown，未修改平台/canonical/Native 代码，未执行平台全量回归、发布、业务写库或调度操作。

以下为 A2 当时采用的历史验收清单，不作为2026-09-08后继续追加或重复运行的门槛；
用户最新决定与当前算法验收仅以第6.1–6.2节为准，已有证据保留复用。

- 同一冻结输入与完整正式 Request 集，Native、候选冷路径、候选恢复路径的 ID、三个日期、方向零差异；
- 333 条正式样本在相同前置历史准备下执行 `0+333`、`1+332`、`100+233`、`332+1`；
  比较合并后的 Result 和规范化算法状态，执行时间、批次标识等诊断不参与状态等价；
- 升序、降序、乱序、子集结果按 request_id 一致；内部需要的历史依赖仍需计算，不能把 Request 子集当训练历史；
- 独立单点复核首/中/末，并覆盖月初、季度初、年度筛选边界、同周周初/周中/周末；
- 连续模拟至少 10 个交易 cutoff，每次全新进程；逐步只暴露该时点可见的数据，覆盖 T+5 标签由未知变可用；
- 将成熟标签、周/月尾部变化与源历史修订分别注入，证明稳定区间复用和必要重算的范围与冷路径一致；
- 修改 code/config/metadata/runtime/状态格式后拒绝旧快照；同 Request 重试结果不变；
- 验证生产状态不向历史 cutoff 倒退，历史回测从合法前置点在私有状态中向前计算；乱序 Request 可内部排序后
  恢复原输出顺序，不增加未来快照反向查询能力；
- 对输入前缀未变的漏跑场景，验证同一增量核心能否在每日时间预算内补齐内部计算，并仅输出本次请求；
  未证明安全或无法满足预算时不启用该路径，明确失败并要求显式重建，不自动补写历史 prediction；
- warmed 单日每次 ≤120 秒；单进程 RSS ≤4 GiB、数值线程 ≤8，算法不创建子进程；
- 离线 backtest 每次调用以 7200 秒为安全上限，不再采用 100 条／600 秒与完整区间／1800 秒硬门槛；
  报告必须注明起始快照覆盖范围：已覆盖区间回放与
  新增区间推进分开计时，不能用预先算完被测区间的结果冒充 batch 增量性能；
- 全量预热、恢复时间与状态体积单独实测，部署前结合调度时间和可接受停机窗口确定恢复预算；
  最初六小时是开发时间窗口，不推导为恢复 SLA。恢复预算不能用于放宽每日 120 秒门槛。

本阶段仍无业务写库或控制面切换。若进程内优化已满足全部门槛，保持 stateless，A3 的状态扩展无需实施。
若有持久状态才能通过，必须先得到完整等价、状态体积和每日推进的实测证据，再冻结最小接口。

##### A3：按试点结果接入最少的平台能力

**当前入口条件**：A2c 本地离线矩阵已通过，A3 本地实现、独立审查与真实 executor 探针已通过；尚未发布。
只读源码盘点确认既有平台能够验证 generation、
文件身份、lineage 与输入完整性，但不能独立证明算法实际依赖的列和已消费月份没有变化。
用户在详细解释后已明确同意：由算法判断历史依赖、修订拒绝及复用语义，平台只负责 exact version、可信输入
lineage、状态摘要/路径/大小、独占推进和原子发布。这取代原“平台独立验证输入复用证据”的职责，不建立通用
因子依赖证明合同。算法依赖遗漏仍由冷/热等价与修订注入验收防守；平台内容摘要不冒充算法语义证明。
标准五字段 Result 验证必须先于 canonical 状态发布。

只有 A2 通过才进入本阶段。保持 Metadata 1.0、两文件交付、五字段 Result 与唯一 Blackbox executor；有状态
能力显式配置，普通方案不获得状态读写能力。上一版拟定的 mode/schema 字符串与成对 Intake 参数不作为已实现
合同，A3 按试点实际需求冻结最少字段和受控入口，不建设算法插件注册表。

只锁定受控读取、校验、独占推进、原子保存和显式重建五项必要能力。以下是候选接入点，不是必须逐项修改的
开发清单；先核对已有实现，缺什么补什么，不预定新增模块或命令数量：

1. `shared/scheme_config_schema.py`、`scheduler/discovery.py`：校验并传递状态能力。
2. `shared/blackbox_v2/versioning.py`：显式把新增配置纳入 canonical hash；验证新字段变化会改变 successor
   exact version，缺失字段的现有 Blackbox hash 保持完全一致，不能只加解析字段却漏改版本身份。
3. `shared/blackbox_v2/intake.py`、`harness/cli.py`：提供所需的受控声明方式；不允许交付后手工改 config
   绕过版本与验收。
4. `scheduler/blackbox_v2_runner.py`、必要的 executor 调用点：提供只读旧快照、私有候选输出，复验输入与状态，
   校验标准 Output 后发布新快照。算法不获得 canonical 状态根目录。
5. 复用 `shared.runtime_paths`、`shared.exclusive_file_lock.ExclusiveFileLock` 及现有进程/目录边界；
   新代码只补最小状态封装、摘要验证和发布，不重写已有锁或搬入 Native cache 模块。
6. 只读检查优先由既有执行前校验和可审计日志满足，预热/重建优先复用既有受控执行入口；仅在无法满足时
   增加最小运维入口，不另建 verify/quarantine/GC 命令。格式、磁盘与解压上限根据 A2 实测冻结。

平台只验证通用状态身份、可信输入身份、路径、大小、内容摘要和提交完整性，内部数组和历史复用语义由算法验证。
DataBridge/input_artifacts 继续提供 ready generation 与 lineage；平台对本次私有输入绑定完整文件摘要并在执行后
复验，不重建或修复 producer generation。generation 变化本身既不证明可复用，也不直接判定失效；算法必须证明
实际历史依赖仍成立，包括 catalog/calendar 的相关历史语义，不能仅凭 mtime 或 generation 名称决定复用。

每日取得稳定的 base-scheme 文件锁，覆盖不同 exact version 的本机状态操作；状态文件按 exact version 隔离。
该锁配合现有 Registry/advisory lock 与进程 fence，不能用版本级文件锁代替业务 Writer 授权。成功候选在同文件系统
内写临时快照 → fsync → 重新验证 → 原子 replace → fsync 目录。一次替换只发布一个完整快照，不要求两个文件
或双 pointer 同时原子更新；持久化验收摘要写既有 run/backtest 审计，不建立长期状态账本。

失败清理只处理本次 staging；禁止顺带递归清理 canonical 状态。崩溃恢复、磁盘满、只读目录、第二 Writer、
摘要损坏、数据库提交失败后的重试都纳入测试。文件系统与数据库不做分布式事务：状态可以先成功而业务事务失败，
重试必须从同一输入恢复相同 Result；数据库预测仍由既有 repository insert-only 提交。

生产状态只向前推进，不支持用未来快照反向服务历史 Request。普通 backtest/comparator 从合法前置点在私有
状态中向前计算，不能推进生产状态；同 cutoff、同输入的失败重试仍须返回相同 Result，不另建历史查询能力。

漏跑与状态损坏分开处理：只有输入前缀未变、状态完整且 A2 已证明安全和性能的场景，才允许同一增量核心在
既定每日预算内补齐缺失的内部计算，仅输出本次 Request，不自动补写历史 prediction。不增加恢复算法或后台
追赶任务。缺状态、状态损坏、历史修订、无法证明可复用或超出预算时明确失败，由显式预热/重建处理；
重建不写 prediction，也不自动补发历史信号。

**A3 本地实现记录（2026-09-07，未发布）**

- 新增唯一 opt-in 字段 `incremental_state: true` 和 `intake-blackbox --incremental-state`。缺省旧配置 hash 不变，
  非 true 值与 Native 声明拒绝；Metadata 1.0 和两文件合同不变。
- 现有 `SCRIPT_VALIDATOR_POLICY_DIGEST` 按整个 Intake 模块字节计算，本次受控入口修改会改变该摘要。旧 release 的
  回测证据不能直接用于新 release 的 activation/cutover（包括已记录的 W1 backtest-ready）；必须按新策略重新验证，
  不绕过此门槛。此项不改既有方案 exact version、历史事实或 Registry，不能误报为已在本轮重新回测。
- `scheduler.blackbox_state` 用一个有界平台封装保存 opaque payload，payload 上限 16 MiB，header 上限 64 KiB；
  checksum 覆盖 header 与 payload。原子保存只替换一个完整文件，无 sidecar/pointer、ledger 或状态数据库。
- 唯一 runner 在显式声明时传入 `--state-input/--state-output`，绑定 code/metadata/canonical exact version、
  Python/conda/dist-info 安装记录和实际子进程环境、五文件或存量四文件的私有输入摘要。安装记录摘要不等于
  扫描全部依赖代码字节；仍以受控冻结运行环境为前提，不支持现场手改 site-packages 或旁路加载代码。
- 自然预测使用 base 锁和 exact-version 正式状态；历史 as-of、gray batch、持久化回测使用私有状态；迁移比较只
  输出私有派生状态。缺失/损坏/无法复用不触发自动冷训练。状态 audit 写既有 PredictionRecord.extra/backtest extra。
- `rebuild-blackbox-state` 显式绑定 scheme、expected version、日期和 approved-by，复用标准输入与 executor，
  只重建状态，不创建业务 run/prediction/backtest/Actual。公共 CLI 测试验证在执行前拒绝非法范围、成功/失败释放
  只读输入 Engine、只输出状态审计；并未在 ECS/Mac3 执行该维护命令。
- 独立审查发现并修复实际环境变量漏绑、完整状态根 symlink 检查、pip 安装记录漏绑、硬链接锁及持锁期间换锁
  五项问题；复用了现有锁类并仅补强其安全不变量。当前无未修复 Critical/Important，运维 CLI 测试建议已补齐。
- 本机全量回归：**691 passed, 5 skipped, 229 subtests passed**；`git diff --check` 通过。5 项 skipped 未冒充通过，
  其中 isolated MySQL 测试未在本轮提供专用连接；本轮未改数据库事务或 migration 实现。
- 私有真实算法 executor 验证位于 `/tmp/bfl-a3-executor.9FONJP`：两文件经新 Intake 保存到该临时项目，script SHA
  仍为 A2c 的 `9a32cd7e6c157a5d56a3274affd45a4431a3c3f35daa8ac88a400dab2dacb27e`，未进入仓库 canonical。
  初次尝试因旧冻结样本有硬链接被正确拒绝；复制为独立、逐文件 SHA 相同的私有样本后已完成验证：从零预热
  **378.47 秒**；十次新算法进程逐日推进 **3.94–5.24 秒**；同 Request 三次重试 **2.65 秒**。计时包含正式 runner
  的私有输入/环境摘要、进程启动、Result 校验、状态锁与原子发布，不包含 DataBridge ready 入口或调度/数据库开销。
  本轮十日均使用同一完整冻结文件，由算法逐 Request 截止；A2 的逐日可见输入实验另行保留，不混称同一实验。
  十条五字段 Result 均与 A2c 正式区间对应行相同；最终 payload 与 A2c 独立冷算末日的 NPZ **逐字节相同**，
  三次重试 payload 和平台 envelope 均不变。末态 **1,516,736 字节**，SHA-256
  `bda0aaa6d8e4207f2ada233016d1cc838bac0711232a9181996579b2f34c5e3a`。
  `report.json` 状态为 `LOCAL_EXECUTOR_PROBE_PASSED`，SHA-256
  `a6b6819a99efc0d88e4b55377dfc2eaa4b46620f8bb2b0d6baa8286c59d985f6`；仍为 `production_ready=false`。
  验证期间 runner SHA 为 `cc254fbd4681a96a26e6f3e4cb4552d572d4511b110f915d269a98ac4e191f78`，
  state 模块 SHA 为 `2521ac3ddd2b650b27daaf78798da2e3c0ee26a01116512bcd42da6fa33a3a72`。
  未连接业务数据库、创建生产状态、执行 one-shot 或操作 ECS/Mac3；当前本地实现不能替代 A4 目标环境验收。

**A3 后续：正式候选接入与输入完整性补强（2026-09-07，未发布）**

- 经 `intake-blackbox --incremental-state` 创建仓库 canonical
  `schemes/liwei_0616_5y01_full_oos_k3_div_k10_bbv2/`，保持 `paused/draft`，部署矩阵为 `[]`，
  不进入 ECS/Mac3 方案发现集合；原有部署矩阵的所有分配完全不变。
- 新 scheme version 为 `3ee3dd2334fd`，script SHA 为
  `ad9bdacf5063a427ecc8b70852e045f4822ba9af1b6d8fcd171cd2d779e95103`，Metadata SHA 为
  `96d46ee4b2fb16f3b7da0c5485808f52f8f7ef14607721fbe00d26bc55d74982`，参与版本计算的 canonical config hash 为
  `5d0a5b3b771a02f25e472c0fa76119ef978810907f2370feb8b0eea22c0d2d61`；`config.yaml` 原始文件 SHA 为
  `c76edc19f65cb44871cfff2405b6da38a841d621a32625a75b1dc7e75eaecdfd`，两种摘要不混用。
  相比 A2c，仅整理模块首说明和 Metadata 的 name/description/algorithm_version；去模块首 docstring 的 AST
  完全一致，未改函数、常量、计算路径或算法私有状态 schema。新身份从零预热，不导入旧试点快照。
- 补上 stateful runner 的 Request 文件前后完整性检查；实际 CLI 负例先复现“修改 Request 后仍可发布状态”，
  修复后在 Result 返回、状态发布前拒绝。覆盖已有状态的 predict 与无旧状态的首次 backtest/rebuild，
  失败不覆盖旧正式状态、不创建新正式状态；stateless 调用路径不变。
- 新一轮全量回归：**693 passed, 5 skipped, 229 subtests passed**（21.75 秒）；`git diff --check` 通过。
  独立审查运行 state/runner/deployment/active 合同测试：**47 passed, 65 subtests passed**，
  两文件布局、Metadata、配置及 AST 比较通过，无 Critical/Important。5 项 skipped 不代表本轮完成 MySQL 现场验证。
- 新 canonical 经正式本机 runner 从零预热 **461.32 秒**，十次逐日推进 **4.19–6.02 秒**，
  同末日 Request 三次重试 **2.65–2.90 秒**；未加载旧试点状态。十日五字段 Result 与 A2c 完整正式区间对应行零差异。
  仍使用 `full-20260905-063321-21c5c7188fa5` / `snapshot-f42540ebc533428ca6c869e2` 的逐文件 SHA 相同私有副本，
  全量冻结文件由算法逐 Request 截止；计时包含 runner 检查/启动/发布，不含 DataBridge ready、数据库和调度入口。
  本次不宣称已重新执行新 exact version 的完整持久化回测、100 条批量或全部性能矩阵。
- 独立比对最终增量状态与 A2c `daily10-cold-last.state.npz`：dates、features、`265×252` preds/probs 数组
  逐字节一致，算法 header 除预期 code/Metadata hash 变化外完全相同。因身份已改变，不声称整个 NPZ 字节相同。
  末态 **1,516,736 字节**，payload SHA 为 `94e2fbaa11a097f0566123429daa860baf3c23cb07efa87f1eed2b3fbd906224`；
  三次重试的 payload 与平台 envelope 均不变。envelope SHA 为
  `13c8f782d3e2f9e876d02b009b9aa3c32a9b432847d3f43c1fe59e8375006f07`。
- 一次性证据位于 `/tmp/bfl-full-oos-canonical.vb1rpu`：`report.json` SHA 为
  `84320aedd0abf9d206969ddcf54a3dcafb75c8e6860bd3cb630313bb138f1710`；`state-comparison.json` SHA 为
  `28f217e78a88822d4581c71226601a5772555c1aa5d80837e32ecfb2d38ecefc`；本轮 runner SHA 为
  `9ecf354c5c706d1d2a3b2f535a128510df912de4c205a5467c35c05d1a56c6a3`，state 模块 SHA 与前一 A3 记录相同。
  两份结果均明确 `production_ready=false`；原 A2/A3 证据保留其原身份，不改写为新候选收据。
- 用户已明确授权将本次平台改动、相关文档及新候选提交到本地 `codex/develop` 并构建确定性 archive。
  archive 必须来自 clean commit；提交和两次构建的精确身份、摘要与验证结果由构建产物及随附 Markdown 记录，
  不将“获得授权”当作“已构建”。此授权不包含推送、合并 master、部署、激活、业务写库或服务切换。

**A4 只读准备记录（2026-09-07 19:28–19:29 CST）**

- ECS SSH 可读；current 为 `3162f70e67f53b7cdb65a3e8792d42c6fab32d15`，previous 为
  `f947426d969d6c3e3879e707f7a0564345788d9a`。服务 Python 3.12.13、Blackbox Python 3.13.12 可执行。
  4 CPU，约 12 GiB 可用内存，`/opt` 约 16 GiB 可用空间；这些是采集时资源，不是性能验收。
- installed daily/weekly/monthly service 均 loaded、inactive/dead、MainPID 0，timer 为 active/waiting；
  next trigger 分别为 09-08 07:03、09-12 11:30、09-08 18:00 CST。未停止、启动或替换任何 unit。
- DataBridge publication manifest generation 为 `full-20260907-063337-3baeb4277bae`，manifest SHA 为
  `d63a4df72dddae2acdc3d04f0c3e478a24837ba93f705518c309ece86a1e417f`，feature date 为 2026-09-04，
  manifest 含五文件身份与 1474 行 catalog。尚未重新哈希实际五文件或验证当前 ready receipt，不能当作冻结输入验收。
- `/opt/bond-factor-lab/incoming` 为独立于 current/release 的 root-owned 0700 目录，可供后续授权候选验证使用。
  本次没有上传、写目录、导入算法依赖、连接数据库或查人工算法进程；service 空闲不能证明所有 Writer 均不存在。
  因此只解除 SSH/基础解释器/资源可读阻塞，未获得 ECS 算法等价、性能、持久化回测或控制面验收结论。

**A4 首轮 ECS 隔离执行（2026-09-07 22:09–22:46 CST）**

用户已另行授权不可变包上传和候选目录内的隔离技术验证；未授权 current/previous、Registry、业务写库、
systemd/launchd、服务或 timer 操作。已执行的只有候选文件、私有输入视图、可重建状态及验收证据写入。

- 本地 clean commit：`d0366b9eb076dcf0d86397931cce6c6c97f42885`，两次构建 archive 完全一致；SHA 为
  `c33e5d261fc7c349353ac13beb0107db4ca3fdb7e910435865d09f26fd8a09ec`。归档 1,098 个 Git blob 与提交精确一致。
- ECS 私有根：`/opt/bond-factor-lab/incoming/a3-d0366b9.htVJZn`。使用现有 installer、不带 activate，
  安装到此根下的 `deploy/releases/<commit>`；返回 `activated=false`。未修改解包源码或现有生产 release。
  候选 version 仍为 `3ee3dd2334fd`，paused/draft，部署矩阵 `[]`。
- 现场为 x86_64、4 vCPU，服务 Python 3.12.13、Blackbox Python 3.13.12；NumPy 2.3.5、pandas 2.3.3、
  LightGBM 4.6.0、numba 0.63.1、bottleneck 1.4.2。运行前只读进程检查未见算法训练。
  算法使用既有 Profile，实际限额收紧为 4 GiB、数值库线程上限 8、冷重建 1800 秒、单点 120 秒、100 条 600 秒。
- 当前 producer-ready generation `full-20260907-063337-3baeb4277bae`、snapshot
  `snapshot-4a525546f74e5f3d003ce07f` 已通过现有 ready/producer seal 读取逻辑及私有运行视图的五文件校验。
  为保证生产文件零修改，只以 O_RDONLY 对已有锁取得共享锁，复用现有只读 helper；不创建锁、不 chmod、不重建 ready。
  当前输入入口验证不执行算法，不与旧 generation 的方向证据混用；独立复核再次在共享锁内读取身份和 SHA。
- 算法实测使用原 0905 冻结 snapshot `snapshot-f42540ebc533428ca6c869e2`；上传原五文件、manifest、
  producer receipt、ready identity 和原 Request，通过 `input_artifacts.open_blackbox_runtime_view` 重新完整校验并复制。
  不复制跨主机 inode seal、不自拼 DB 输入、不读取 Native 或本机算法缓存。formal Request SHA 仍为
  `dcd62088ce197adee97d95dc944dd8f4c2ab3cdb8fe2f30a7b2a4f68f9a2f340`。

| ECS 检查 | 实测 | 判定 |
|---|---|---|
| 从空状态预热至 2024-12-31 | 1076.14 秒；峰值 RSS 981,884,928 字节 | 显式重建成功；此为恢复成本，不是每日 SLA |
| 十日逐 Request 推进 | 8.15–11.85 秒/次；峰值 RSS 603,926,528 字节 | 单点性能通过；十条五字段与同代 ARM64 参考一致 |
| 同末日 Request 重试三次 | 4.235–4.237 秒/次；峰值 RSS 478,887,936 字节 | 结果、payload、envelope 均不变 |
| 100 条从正式区间起点推进 | 600.18 秒返回超时；算法硬限 600 秒；峰值 RSS 736,440,320 字节 | **失败，候选停止晋级** |

资源为主算法进程 `/proc` VmHWM/VmRSS 与线程采样；训练时最多观察到 6 个线程、重试 2 个线程，
采样未观察到算法子进程，五个数值库启动线程变量完整且均为 8。采样不是 syscall tracing；runner 另有进程组 RSS 限制。
单点计时包含平台身份/输入校验、进程启动、Result 校验和状态发布；batch 使用受控 CLI 与标准 Result 校验、
独立预热 payload，不声称已通过持久化回测入口。batch 超时前的一次诊断采样记录 70 个已完成 cutoff，
累计重训 125 个尾部/新增点；这不是 70 条已发布 Result，更不是 100 条完成证据。

本轮独立审查发现原始单步状态先记 PASSED 再由外层比对、base Profile 与实际限额混淆等报告问题；
未改正在运行的探针，追加 fail-closed 独立复核，逐行重验五字段、非空资源采样、实际限额、状态不变和固定 lineage SHA。
必需证据缺失、损坏或无法解析时也输出 STOPPED；本机缺失证据负例通过，独立复核无未关闭 Critical/Important。
最终权威报告为 `CANDIDATE_STOPPED_NOT_APPROVED`，不是原始单步 PASSED。

证据已保留在 ECS 私有根的 `evidence/`，本机副本为
`outputs/releases/a3-full-oos-20260907/ecs-evidence/`（不纳入 Git）：

- `independent-verification.json` SHA：`7e059e4eb6481b9fecabf3a052bb0c423955dbed38a9976e673f8e335b659e1c`；
- `identity.json` SHA：`e1b182c7f9865c3c9088d7b9bd24ef6d87efbff52688f265d4256f36145a54ea`；
- `hundred-asc.json` SHA：`17718030041c501431577a8396acc40f615aa9a0482cb19cdc6eaff60ae4c788`；
- `batch-progress.json` SHA：`2f39534aeb0d9682f4367b3cfbc8248103d7f329231dca2ec1b24686e44ab526`；
- Linux 平台运行环境摘要：`cff7095b5e6c7669b77a3335e12164d35cd59cf52b6180ee22cf6f659bbe6ce6`。

22:46 收尾读回：验证父进程和算法进程均已退出；batch 仅保留 requests.csv，无 Result/state 输出；
输入运行视图已清理；独立复核确认 batch 未改变逐日末态，预热 checkpoint 不变。ECS current/previous 仍为
`3162f70e67f53b7cdb65a3e8792d42c6fab32d15` / `f947426d969d6c3e3879e707f7a0564345788d9a`，
三项真实 `bond-factor-lab-prediction-{daily,weekly,monthly}.service` 均 loaded/inactive/dead、MainPID 0，timer 保持等待。
未查询或修改业务数据库；不以“未写库”冒充业务表数量的前后现场校验。

根据性能停止条件，未启动 100 条倒序/乱序、完整 333 条、同 Linux Native 独立对照、持久化回测或任何切换。
十日方向的跨环境一致只能作为参考证据，不能替代同 Linux 环境的 Native 完整迁移等价验收。

**失败后的最小优化方向（历史诊断，尚未实施；本轮不实施）**

缓存已复用历史 Phase-A；剩余问题是小段历史尾部变化会触发模型重新训练。独立本机纯数据诊断使用相同五文件，
未训练模型：在 2024-12-31→2025-01-02、2025-01-02→2025-01-03 两次推进中，旧末点各有 20 个周频特征变化，
其中 3 个确实进入 IC 选择的 80 列：`wk_S0114089_chg`、`wk_V0135838_val`、`wk_HWW00001_val`。
因此只缩小特征 hash 或忽略尾部变化不能消除正确重算，不能作为主要优化。

这两个旧末点的全部 265 个 config，其 fit/cal 索引、有序特征、X/y、权重均相同，seeds `[42,314]`、
轮数、early stopping 语义也不变；变化只在推断行。可考虑在单次 batch 内有界保留最近尾点的已训练模型，
训练身份完全匹配时仅重新 predict 当前真实推断行；保留相同 best_iteration 和首 seed 校准概率/阈值。
此类旧尾点理论上每次可省 530 次重复 train；新日仍正常训练，命中率与模型内存成本必须实测。

后续试点不得改平台状态合同、增加持久化模型或跨方案共享，不得仅凭 cutoff/config 判定模型可复用。
需覆盖训练/校准数据及标签、权重、有序特征、完整参数/seed/轮数/early stopping 的精确身份，限制模型缓存容量；
身份不匹配按原算法正常训练。形成新的候选代码 hash 后，先对照当前实现验证五字段零差异与失效边界，
再用新不可变包重跑 ECS 性能；两次纯数据诊断不是该优化已完成的证明。本次旧候选的超时记录永久保留。

**离线预算调整（用户确认后的执行决定）**

用户明确接受一次性完整回测耗时较长，并确认继续按每日 ≤120 秒、离线每次调用 7200 秒安全预算推进。
这取代旧 100 条／600 秒、完整区间／1800 秒门槛；旧 ECS 600 秒超时记录不改写，也不能追认为完整回测通过。
本轮不实施上述模型缓存优化，不修改算法、canonical metadata/config 或状态协议；只调整状态化离线执行器
及临时 Native/successor 对照的超时预算。调用方更短 deadline 继续优先，常规 predict 120 秒与显式
predict 重建 1800 秒上限保持不变，RSS 4 GiB、数值线程 ≤8、无子进程和五字段零差异要求不变。
先完成回归及独立审查，再以 clean commit 构建新不可变包，在 ECS 私有目录补齐完整离线计算和同输入对照。
W2 旧离线超时也需要按新预算重新验证，不自动转为通过。一次性指同 exact version 的有效证据复用；
代码、输入或校验策略变化仍按绑定规则重验，不承诺永不重新回测。
该预算调整时的授权仍不含业务写库、激活、调度服务操作、current/previous 切换、Mac3 晋级或 DDL；
后续夜间授权的新增范围及截止条件见下文，不回溯改变已执行探针的只读业务边界。

**新预算下的隔离验证（2026-09-07 23:09 CST，运行中）**

- 本地提交 `c8d102eba7cc54327e29987707c3bae99dfbf3c3` 只调整上述预算、测试和文档；
  canonical exact version 仍为 `3ee3dd2334fd`，算法、Metadata 和 config 字节未改。
- 先用新增真实 CLI 测试复现旧限制，再验证修正；全量回归为 **700 passed、5 skipped、229 subtests passed**。
  独立审查 Critical/Important 为 0，独立针对预算的 8 项测试通过；一次性 ECS 探针也经独立审查。
- 两次 clean-commit 构建 archive 及 manifest 逐字节一致：archive SHA-256 为
  `5e8065b278c525f8529686b64a8dee8977b90a8d87cedfff938f5c1becc1f02c`，manifest SHA-256 为
  `185fcf36a46b0482a3d15423e1ac65f8dd61004ca62c4c2e4267472e387608cb`。
- 安装器读回 `activated=false`；候选目录为
  `/opt/bond-factor-lab/incoming/a4-offline-c8d102e.LP4qqK`，正式 current/previous 仍为上一证据段的两份 release。
  启动前 daily/weekly/monthly service 均 inactive、MainPID 0。未查询或修改业务数据库。
- 本次通过 `run_blackbox_backtest` 从空私有状态执行 333 条正式 Request，使用相同 0905 generation、五文件和
  Request 摘要；完整状态环境指纹与上一 ECS 验证一致。安全预算 7200 秒、RSS 4 GiB、线程上限 8。
  23:09 只读确认父进程 752716、算法进程 752732 属于该精确候选；这不是完成或通过证据。
- 一次性探针及后续输出保存在本机 `outputs/releases/a4-offline-20260907/`，不纳入源码 archive。
  本次探针 SHA-256 为 `ef27cb9c380d85219aada2b2b728eb631341cf19682f8d3bc8a266e0fbd68d0a`。
  最终验收要求退出成功、`full333.json` 通过、`cleanup.json` 两项为 true 且无 `failure.json`；
  单独出现 Result 或单步报告不代表整次成功。Mac 参考比较不替代同 Linux Native 独立等价。
- 当前任务的后续检查 `native-ecs` 每 10 分钟读取本次验证；运行未变时静默、不并行启动训练。
  后续范围服从下面的最新夜间授权和硬截止。

**完整冷回测结果（2026-09-08 00:07 结束，00:13–00:15 独立复核）**

本次 `c8d102e` ECS 高层私有回测正常退出（原 session 9428 的 exit code 0），从空状态完整计算 333 条，
耗时 **3609.16 秒**（约 60 分 9 秒，包含约 1076.60 秒的首点历史准备）。五字段与冻结 Mac 参考逐条零差异。
35554 次资源采样记录峰值 RSS **1,153,753,088 字节（1.075 GiB）**、最高 6 个线程，无观察到的算法子进程；
5 项数值线程环境均为 8，采样无错误。私有状态 2,382,679 字节，不复用旧 Native cache，未推进旧日常私有状态。
临时 active/debris 视图均已清空，无 failure.json。该结果通过新离线预算，不改写旧 600 秒超时记录。

证据已下载至 `outputs/releases/a4-offline-20260907/ecs-evidence/evidence/`，本机独立解析完整 CSV 并比较
333 条记录、报告、身份与清理字段；下载后所有文件摘要与 ECS 读回一致。关键 SHA-256：

- `full333.json`：`6f3b1cea74dea0567a15e83ff24e73a4dfac4ed106230b2d65464640179c49c5`；
- `full333.result.csv`：`8a3fb06475a36eea4b5488e7fee87d13708f1d015e963c5cf822fe9126f12ef6`；
- `identity.json`：`70dc68593665f41579400fb6a3ec674c0f6a31e603cef85be4047d940e6bf50e`；
- `cleanup.json`：`7214a4be8bfe2e736373f1e04c797ce6d7addb15d84c956d4f6b4cb40a3e323f`。

输出 CSV 与 Mac 参考文件的原始摘要不同，但由解析后的五字段逐行证明业务内容相同，未把不同输入当成豁免。
复核时旧测试进程均已退出，ECS current/previous 不变，三项 prediction service 均 inactive、MainPID 0。
未访问业务数据库；不宣称业务表数量已读回。**同 Linux Native 独立等价及其他缺项仍待验收**，不标记生产就绪。

**同 Linux Native 独立对照（2026-09-08 00:29 启动，02:24 完成）**

在同一 ECS 隔离候选的 `native-evidence/` 启动一次受控原算法参考，不再执行 successor 完整回测。
先在 Linux 上运行 333 个无训练的依赖截获检查，证明每个截止点的 selector、历史特征、可见标签、close、
fallback、索引和训练参数满足前缀复用；全部通过后才独立冷训练 575 点 × 265 config × 双 seed，随后对每条
Request 独立重训 Native 当前点，再按该 Request 原始输入重算排名、信号和三个 baseline 控制器。
不把 Mac 的依赖证明直接当 Linux 证明，也不读取 successor state/模型数组作为 Native 基线。

原 Native `Pool(4)` 只用于独立参考，其每个 LightGBM `n_jobs=1`；不改 Native 源码，不开放 successor 子进程。
两边使用相同 Linux Python/依赖安装环境，分别记录 launcher 和 Native 内部数值线程设置。运行身份覆盖 Python
可执行字节与包安装元数据、实际包版本；不把安装元数据指纹表述成逐个包二进制文件的完整内容校验。
controller 通过平台 `input_artifacts` 构造私有视图，并绑定冻结五文件、Request、Native core/alignment、辅助
脚本、依赖证明和阶段数组摘要；执行后复验代码/输入/运行环境，严格校验 333 行精确五字段域。

一次性脚本独立审查无未关闭 Critical/Important，语法检查通过，上传前后 SHA-256 相同：

- `ecs_native_probe.py`：`f93cf4a092cabb567e385265dc3edff04b474125dfd4e0287b0299794ead7c29`；
- `native_reference.py`：`430d371283a020d129a52a0d3febe5f918154855e20b56caa5caf67161dd5341`；
- `native_dependency_check.py`：`5585d0da378bfd438471925e997b586f64f77a5f8c3512f501a6274213fca3f5`。

已启动父进程 769012、Native 主进程/PGID 769028（仅是此时身份，后续操作必须重新核验完整命令），
session 22637；由既有 runner 强制单次 7200 秒、进程组 RSS 4 GiB、日志 5 MiB 与工作目录 64 MiB 上限。
启动时验证两小时预算及 15 分钟清理余量均在 05:00 前；本次最迟约 02:29 超时退出。
只在退出成功、Native 333 零差异、依赖证明 333/0、源输入闭包不变、私有视图清空和状态未变后记录通过。
启动记录不作为通过证据；以下是退出后的独立复核。

02:24 进程退出 0，最终状态为 `PASSED_SAME_LINUX_NATIVE_INDEPENDENT_REFERENCE`：

- Native 独立结果 **333 条、五字段零差异**，同 Linux 候选输出原始 SHA-256 也完全一致：
  `8a3fb06475a36eea4b5488e7fee87d13708f1d015e963c5cf822fe9126f12ef6`。
- Linux 依赖截获 **333 passed / 0 failed / 0 model fits**；随后独立训练与逐 Request 比较，
  不是把依赖检查当预测等价。总耗时 **6889.16 秒**，其中比较阶段 **3756.46 秒**。
- 6812 次资源采样、峰值进程组 RSS **2,765,725,696 字节（2.576 GiB）**，无采样错误；未触及两小时/4 GiB 上限。
- 核验 generation、五文件、Request、source/helper、执行身份、依赖报告和阶段数组证据链；
  最终摘要 `e45e5f85d7afa3602276e480e5f532995b68c049e8f03d4382c8ac9055893015`，
  依赖报告 `5313f75b92d4cbfe6ab879cfeac500ad02b2f2f592dcf27f774687b25efb5844`，
  比较报告 `932d01f2cd15eefcaaad297725c4f37b6b8abc6af79a11e21b5d477b78326920`。
- 原父/子进程已退出，私有视图 active/debris 为空，旧私有日频状态未变；无业务数据库访问或 release/调度修改。
  02:25 只读确认 ECS current/previous 不变，三项 prediction service inactive / MainPID 0，daily next trigger 07:03。

完整证据已下载 `outputs/releases/a4-offline-20260907/ecs-native-evidence/` 并独立解析重验五字段域和各报告摘要。
该结论只关闭 full-OOS 的同 Linux Native 独立等价缺项，**不替代剩余 ECS 合同矩阵、持久化回测、cutover 或生产验收**。

**Linux 批量复现补验（2026-09-08 02:29 启动，02:42 控制脚本中断）**

在相同 c8d102e 私有候选、0905 冻结五文件和 exact version 上，顺序执行 100 条升序三次、倒序、
固定乱序以及包含相同末截止点的 8 条非连续子集。每次使用新输出目录、同一只读私有预热 payload
`674711eed4d0dc0730023ffaadb56bc38dd75bb97237f58784e6ff94b61e3bdf`；不重新训练已经通过的完整333条。
已在运行前精确比对预热 header 的版本/脚本/Metadata/运行环境/输入身份，并锁定已验证 Native 报告摘要。
每组以五字段按 Request 顺序逐条一致为准，并比较 NPZ 解包后的所有成员内容摘要，不能忽略 header 或数组差异。
子集最终状态一致仍是待执行的验收条件，不凭代码检查标记通过。

一次性 `ecs_batch_probe.py` 位于私有目录外层、不修改 immutable release，独立审查 Critical/Important 清零，
上传前后摘要均为 `696a6c65925c3748f933d47b598d7c187dc4b2684824c92c0878a72a025a8881`。
语法检查通过；监控器所属进程组核验通过 3 个 mock 信号边界检查，未在本机发送真实信号。
本次每组另设 **1200 秒操作安全预算**，不是恢复离线性能准入门槛；每组启动前要求余下所有组的最坏算法
耗时加 600 秒验证/清理余量可在 05:00 前完成。600 秒是控制器开销余量，不宣称文件操作也有硬超时。
资源上限仍为 4 GiB/8 数值线程；观测到子进程、线程超限或监控错误即核验父进程和组身份后终止本次测试组。
采样结果只能表述为“未观测到子进程”，并结合单进程算法代码证明边界。

证据目录为 ECS 私有候选的 `batch-evidence/`。session 49589，启动时 controller 806568、首组算法/PGID 806584；
后续组 PID 会变化，操作前必须读回对应 `*-process.json` 和完整命令。任一失败停止后续组，保留证据，不重启重训。
只有六组均通过、退出 0、无 failure、视图清空和旧状态不变后才可关闭这一补验项；仍不写业务数据库或生产状态。
02:28 读回 ECS DataBridge/daily timer 均 loaded/active，下次分别为 06:30/07:03；本次不改挂载、触发时间或 Writer。

02:47 定时只读检查发现 `failure.json`，本次矩阵已停止，未执行后续五组。不是算法超时或方向差异：

- `asc-0` 的 100 条算法调用成功，**773.07 秒**，与已验证333条参考的前100条五字段逐行零差异；
  峰值 RSS **740,831,232 字节（0.690 GiB）**、峰值线程6、未观测到子进程、数值线程环境均为8，无监控错误。
- `save('asc-0', report)` 先写完整 JSON，再 `print(..., flush=True)` 输出进度；文件已存在而
  `completed_cases` 尚为空，失败为 `[Errno 32] Broken pipe`，符合在追加内存完成清单前的进度输出中断。
  原 exec session 49589 已不可读取，无法进一步确认输出通道为什么关闭；不把推断写成确定的 SSH 根因。
- 本地下载 `outputs/releases/a4-offline-20260907/ecs-batch-evidence/`，独立复核100条 Request/Result 域、
  五字段、输出与状态文件摘要及所有 NPZ 成员摘要。`asc-0.json` 摘要为
  `becd92c99999f0201f980febc4e88f90912762654de7cc7a88cb4f785c9371ec`；失败摘要为
  `658708e02c70c3c03db286a81cb9293d3609b6239b1ee021f700abad9378def9`。
- 02:48 只读确认 controller/算法进程均已退出，没有本次目录相关进程，batch view active/debris 为空，
  原预热 payload 与旧私有日频状态摘要不变。ECS current/previous 及 DataBridge/daily timer 状态和触发时间不变。

该证据只能记 **第一组100条通过、六组矩阵未完成**；没有后续重复/倒序/乱序/子集的通过证据，不自动重启或重训。
后续需先使一次性控制器的进度记录不依赖交互输出通道，经审查后另行受控执行未完成组；已通过的第一组不重跑，
旧失败目录保留不改写。这是迁移验证脚本的问题，不改 canonical 算法、平台长期接口或生产调度作为修复。

**夜间推进授权与早间保护（2026-09-07 23:40 CST）**

用户在确认后续步骤后明确授权整个夜间继续推进，并要求不得影响早上 06:00 的定时任务。
该授权覆盖既定迁移内的 ECS 验证、满足全部前提后的持久化回测和受控切换，以及同一已验证 archive 的
Mac3 晋级与验证；不豁免证据、独立审查、单 Writer、事务、gray 区间、回滚或 W4 前置条件。
本夜不执行 `confidence` DDL、不推送或操作 master、不增加模型缓存优化，不改变早间调度时间。

23:40–23:42 只读核对：ECS installed timer 的下次 DataBridge 为 09-08 06:30、daily 为 07:03；
Mac3 installed plist 与 `launchctl print gui/501/...` 一致为 DataBridge 每天 06:30、daily 工作日 07:03，
两项均 not running、last exit code 0。Mac3 current/previous 分别为
`b736d3b21b1c57455cf36d1cdcaa22b00fdda455` / `617113ed0b2e3c059d5b8a4d1390f453938966f5`。
此处只是本轮读回，不能替代收尾时再次核验。即使已读回的时间晚于 06:00，仍按用户 06:00 边界保护。

本夜硬截止统一为 **2026-09-08 Asia/Shanghai**：

- **05:00 前**结束本次重计算及变更操作；每次启动前核对目标机时间，任务最坏执行时间、验证、清理和必要
  回滚必须全部可在窗口内完成，否则不启动。单算法仍最多 7200 秒，并进一步受距 05:00 的剩余预算限制，
  不允许临近截止仍启动完整两小时任务。已有长进程也须有独立于后续检查的进程级 timeout。
- **05:00–05:30** 只允许收尾、必要受控回滚和只读检查，不开始新的训练、回测、写库、发布或模拟触发。
  仅停止完整命令/父进程/私有目录核验属于本次的测试进程组，不按名称或过期 PID 批量 kill。
- **05:30 前**确认无本次临时重计算、悬挂事务、迁移持有锁和未恢复的 timer fence，核对两端 release、
  installed/loaded 调度、next trigger、唯一 Writer 与相关健康状态，并暂停 `native-ecs` 后续推进。
  未完成验收的批次保留原 Writer，不以半切换状态进入早间窗口；已切换批次必须有完整验收或受控回滚证据。
- 不停止或修改 DataBridge、Backend、Actuals，不依赖早间窗口补测或清理；06:00 后仅可无侵入只读观察，
  不自动恢复迁移重计算。结束时报告实际通过项、未完成项和早间就绪证据，不以整夜授权承诺全局闭环。

**本夜夜间执行收尾（2026-09-08 04:57–05:01 只读核验）**

本夜实际交付为 ECS full-OOS 冷333条、同 Linux Native 独立333条和首组100条等价/资源证据；
批量六组矩阵因一次性控制器输出通道故障未完成。未做业务写库、激活、cutover、Mac3 晋级、DDL 或调度变更。
02:42 后无本次算法重计算；不在早间窗口重启。已有有效证据与原失败目录均保留，不回写失败为成功。

| 收尾检查 | Mac3 | ECS |
|---|---|---|
| current / previous | `b736d3b...` / `617113ed...`，与本夜基线相同 | `3162f70...` / `f947426...`，与本夜基线相同 |
| release 整树摘要 | current/previous 均与安装记录一致 | previous 一致；current 有下述既有 pyc 偏差，严格校验不通过 |
| installed / loaded 调度 | 对当前生产 release 的7项 launchd 审计全通过，配置及外置环境无未批准漂移 | 11项 unit/timer 文件与当前 release 模板逐字节一致，均 loaded，FragmentPath 一致 |
| 早间触发 | DataBridge 06:30，daily 工作日07:03，当前均 not running | DataBridge 06:30，daily 07:03；timer active，service inactive/MainPID0 |
| 正在运行的 scheme run / backtest | `0 / 0` | `0 / 0` |
| full-OOS successor active Registry target | `0`，未生产切换 | `0`，未激活 |
| 数据库事务 / advisory lock | 本次只读连接以外 InnoDB事务0、granted user advisory lock0 | 当前服务账号查询权限不足，不能将不可见记为0 |
| Backend health | `ok / launchd_one_shot` | `ok / systemd_one_shot` |
| 无登录 Dashboard 请求 | HTTP401，认证边界正常响应；未验证登录后产品内容 | HTTP401，认证边界正常响应；未验证登录后产品内容 |

收尾未发现本次测试进程或相关持有的 ECS 文件锁；此前 private view 清空及旧状态不变已独立验证。
本次计算器没有数据库连接，收尾数据库检查仅执行只读事务/SELECT并显式结束连接，因此没有本次迁移遗留的
业务事务或 DB advisory lock；这不等同于对无权限查看的 ECS 全实例事务作全局保证。
Mac3 `launchctl list` 显示任务退出状态0，但 loaded print 当前显示 `(never exited)`，不能将其当成已观察到今天自然成功。
两端均无本夜建立的 timer fence，无需恢复/重启服务。

ECS 当前 release 的严格完整性差异仅定位为 **12个 `__pycache__/*.cpython-313.pyc`**，位于旧
`liwei_0616_cons_sda_k3_div_k10` 和 `shared`；mtime均为 **2026-09-06 22:32:37 +08:00**。
实际整树摘要为 `7a28fd37e8f4e7e891c55da51d49b1d54fb488f0171bc30e91ef362abed1c4ab`，安装记录要求
`69d48a34b45c69ea110890437a6e0d436e94e975acf1615386fad36cedea9849`。
只在诊断中排除这12项后，所有剩余文件内容及可执行标志所得摘要与安装记录完全一致；这不是放宽正式校验器，
也不标记 current 严格校验通过。文件时间早于本夜窗口，但创建来源未查明。本夜未删除或修改 current 中任何文件。
**后续发布/切换前须在独立受控维护中处理此项并重做严格校验**，不能在早间临时清理或顺带改旧 Native。

后续先解决一次性控制器日志依赖，再受控补验未完成五组；已通过100条不重跑。之后仍须完成当前政策下的
持久化回测、wave/单 Writer/gray/cutover验收及Mac3独立验证。当前只是本夜夜间窗口收尾，**整个迁移项目未闭环**。
夜间自动化 `native-ecs` 已在05:30前通过受控工具置为 `PAUSED` 并读回确认，不在06:00后自动恢复迁移。

**用户重新授权后的日间补验（2026-09-08 08:42 起）**

用户在获知夜间收尾、未完成五组和 release 完整性问题后明确要求继续推进。本次是新授权下的日间补验，
不是自动延长夜间窗口。08:40–08:43 只读核验 ECS DataBridge/daily/Actuals 均已成功退出、MainPID0；
Mac3 DataBridge/daily 各有一次执行记录且退出码0。只确认调度入口成功，不外推所有方案逐条业务结果。

最小修复仅涉及 ignored outputs 中的一次性验证脚本，不改算法、平台公共接口或当前生产 release：

- `ecs_batch_remaining.py` 不再向交互 stdout 输出进度，只独占新建证据文件；旧实现的 BrokenPipe 条件
  已由关闭消费者的局部回归重现，新实现通过相同检查。新目录为 `batch-remaining-20260908/`。
- 先固定摘要复核原 `asc-0` 的100条输出、Request顺序、输入/版本/环境身份和全部 NPZ 成员，再复用其
  结果及末态基线，只执行 `asc-1 / asc-2 / reverse / shuffle / subset` 五组。原失败目录全部文件最后复验，
  不改写或删除旧失败，也不重新执行已经通过的第一组。
- `launch_batch_remaining.py` 创建新 session，stdin为DEVNULL、stdout/stderr共用独占新建的普通日志文件；
  supervisor 等待 controller 的实际返回码并保存 `batch-remaining-exit.json`，不依赖SSH连接或exec session存续。
  它仅服务本次有限测试，不挂载 systemd/launchd 或新增长期调度器。
- 仍为每组1200秒、4GiB、8数值线程、出现异常停止后续组；日间操作截止 **11:30 CST**，每组前检查余下
  最坏算法时间加600秒验证/清理余量。此预算不改变算法准入标准。
- 独立审查 Critical/Important 清零，修复了新运行视图清理检查误指旧目录的问题；两个脚本语法通过，
  日志断开回归与3项launcher mock边界检查通过。资源/状态验收不证明内部没有重训，不能以缓存复用措辞替代实测。

最终脚本摘要：controller `3e44cd611a72cc560269569d1abc1bebe83234c9a664429144edc6ca4ec211c4`；
launcher `7edf9c05ad38ca4a0fdb4a5229e22b70b12bb2b4b91a3553cda62a2cb5a32eef`，上传前后相同。
隔离 c8d102e 候选的严格源码树摘要 `21d4086d822c4d3db8074c9cefc113e96001fb1cf422aed69f1a742e8e6e075b`
与安装记录一致，不使用有 pyc 偏差的线上 current 作为补验算法源。

08:47 已读回独立 supervisor 814901（PPID1、独立SID）、controller 814903、首组算法/PGID814919；
首次 SSH 启动命令已成功退出，算法仍正常运行。后续PID必须重读对应process报告和完整命令，不使用旧PID操作。
日志为隔离候选根目录的 `batch-remaining-supervisor.log`。当前只确认启动，尚无剩余五组通过结论；
最终须返回码0、summary成功、六组证据完整、旧证据不变及新运行视图清空，才关闭该项。无业务DB或生产调度变更。

**08:59 用户取消额外矩阵，按新标准关闭算法验收缺项**

用户认为本次调整只需要证明修改前后回测结果一致，明确不再执行上述重复/顺序/子集补验。
因此停止本次有限补验并暂停 `native-ecs`；这属于验收范围变更，不是方向差异、性能失败或放宽数据同代要求。
取消时 `asc-1` 已自然完成100条（772.08秒）；`asc-2` 刚开始，对实时读回的算法PGID818430验证完整命令、
父进程814903及本次私有目录后发送SIGTERM，由原runner完成清理。controller/supervisor均退出，
退出记录为1、算法终止码-15；原始failure保留，该技术退出在本记录中归类为 **CANCELLED_BY_USER_SCOPE_CHANGE**。
未开始 reverse/shuffle/subset；不将取消项标记为通过，也不把它们继续列为阻塞。
09:00读回本次三个进程均不存在，新private view active/debris为空，旧私有日频状态摘要不变。

已有ECS完整333条Blackbox回测与同Linux旧Native独立333条逐字段零差异、同一输入/Request/代码/环境证据，
满足本次full-OOS算法迁移的当前验收要求，状态改为 **算法等价验收通过，平台入库待办**。
不继续投入重复算法验证或额外缓存设计；后续工作转向正式持久化入库、受控切换和部署检查，仍保持单Writer、
历史事实不改写、事务与回滚边界。其他方案仍需各自证明同输入完整回测等价，不能外推full-OOS结论。

##### A4：ECS 验证、族扩展与晋级

A3 有代码变更后运行相关合同/原子恢复测试和全量回归，并进行独立审查；Critical/Important 必须清零。生产接入
文档、AGENTS.md 的长期约束与实际能力在同一实现阶段更新，当前计划不代表接口已可调用。

先核对 A2 证据是否来自同一候选、输入、环境和正式 Blackbox executor；符合第 6 节规则的检查直接复用，不再
整套重复。执行边界变化时必须重验受影响项目，尤其是在 ECS 私有状态目录完成正式 executor 的逐日模拟，
测量包含输入读取、快照校验、算法执行和快照发布的完整耗时。通过后完成持久化回测与现有
activation/cutover preflight；额外绑定初始/结束快照 hash、状态格式与校验策略摘要，复核与 gray 起点或下一个
live cutoff 的衔接，不增生命周期表。

W3A/W3B 仍整 wave 切换；各 successor 独立状态。W2/W3B-D 逐方案选择进程内优化或最小快照，不能为减少方案差异
强制全部有状态。Mac3 使用 ECS 验证的同一 archive，基于本机 DataBridge 独立预热并重新做恢复/控制面验证。
W4 的范围和 Mac3-only binary bundle 决定不变。

回滚保留 successor 业务事实；派生快照按原 exact version 隔离，重新切换前核验输入与状态并按需显式重建。
全部相应迁移和回滚窗口结束后删除旧 Native publisher/consumer、cache migration/projection 及专属测试。
迁移期算法诊断/性能脚本按既有原则在摘要留存后清理，长期保留公共状态合同、截止隔离、原子性与恢复测试。

##### 执行门槛与未决实测项

顺序固定为 `A0 依赖分析 → A1 算法试点 → A2 等价/性能 → 按需 A3 平台接入 → A4 ECS/Mac3`。
当前 A0/A1 与 A2c 正式区间本地离线验收已完成；A3 的复用语义职责已确认，本地实现、审查及真实 executor 探针通过，
不执行上一版 P0–P2 的框架建设，也不把本机私有 CLI 当作 ECS 正式 executor 验收。
当前没有新增生产操作授权需求；已有生产切换、Mac3 控制面、
数据库 DDL 的独立边界继续有效。

试点已提供最小派生字段、标签成熟与尾部重算、整体快照读写、预热/恢复及每日耗时的本机证据；
平台完整调用链和 ECS 实际环境的对应证据仍须 A3/A4 补齐。
仅当整体快照复制/写入实测成为主要瓶颈时，再评估分段存储并补充方案；不提前实现对象库或垃圾回收。
任一方向/日期差异、未来数据污染、错误复用、状态越权、半文件可见、Writer 冲突或运行性能失败，均阻断该
候选晋级；不能通过缩减算法语义、复制旧 Native cache 或静默 fallback 标记完成。

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
把该 archive 安装为目标机 current → 对 stateful successor 用当前 release 和 generation 完成预热与只读检查 →
fence cadence timer 并等待 one-shot 退出 → 从 current release 重做 preflight → 使用其 plan SHA 单事务切换 →
单 batch gray 区间并推进对应私有状态 → 恢复 timer → 通过真实 systemd one-shot 的人工触发验证唯一 writer、
journal、Dashboard、运行前后状态快照摘要与 next trigger。preflight 之前的安装和预热只部署代码、写可重建
派生状态，不授予 Registry 或业务事实写入权。

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

rollback 顺序固定：fence timer → 确认无进程、state lock 和 running run → 单事务 archive/pause successor、恢复
old version/Registry → 保留 successor facts 和私有派生状态 → current 切回已核验 previous release → 恢复 timer →
人工触发同一 installed one-shot，验证 old 恢复运行且 successor 不再新增 run。rollback 不回退或删除 successor
状态快照；re-cutover 前按 exact version 和当前 DataBridge digest 重新检查并显式补齐派生计算，不匹配时重建。

同 exact version 允许重新切换，但必须复用已经发布的完全相同事实；禁止重复插入、覆盖或更换版本规避冲突。

### 5.6 Phase 5：Mac3 晋级（当前暂停）

2026-09-08 用户将本阶段收紧为 ECS-only。以下只保留未来晋级设计，不是当前待执行步骤；
不得因 ECS 验证完成自动修改 Mac3 版本、数据库、launchd 或域名。W4 同时暂停，恢复均须另获授权。

Mac3 对 W1-W3 只使用 ECS 已验证的同一 immutable archive，独立重做 release、launchd、数据库、DataBridge、
backtest、cutover 和 rollback preflight，不得复制 ECS 的主键、run、prediction、backtest、Actual 或 Registry
行。stateful successor 用 Mac3 本机 generation 独立预热和检查，本次不建设跨机状态导入功能。
Mac3 使用 installed plist 的精确 ProgramArguments、
WorkingDirectory、EnvironmentVariables、运行用户和 Runtime Profile 人工触发一次 one-shot，不等待自然日历。
W4 随后形成新的 Mac3-only binary-bundle archive，该 archive 不需要也不得在 ECS 运行。30 个 target 全部完成
对应控制面模拟验证后，才允许最终删除 Native 可执行路径。

## 6. 测试与验收

A2、A4 与本节共用一份验收清单，在本 Markdown 中记录各检查的证据位置、身份摘要、结果和覆盖范围；
不新增验收服务、数据库表或证据管理框架。同一次执行可以同时满足等价、确定性、恢复和性能要求，不因多个
章节引用而重跑。按2026-09-08用户决定，取消方案级重复次数、顺序/子集矩阵和额外算法专项复测；
以完整回测同输入前后等价为算法准入依据，复用已证明适用的证据。

复用前必须核对候选代码及 exact version、输入与 Request、起始状态、运行环境、校验策略和执行边界。
代码、输入、环境或执行边界发生变化后，重新运行受影响检查并说明其余证据仍适用的依据；无法证明则重验，
不同环境的耗时不得互相替代。独立进程算法试点不能代替正式 executor 验证，本机证据不能代替 ECS/Mac3
各自的现场 preflight、持久化回测和控制面验收。按需保留一次性算法验收脚本，闭环后只留摘要与公共回归防线。

### 6.1 单 successor 算法验收（当前简化标准）

- 对同一冻结输入和完整正式回测 Request 集，比较修改前后结果；Request ID、三个日期和方向逐条一致，
  具体身份绑定见第6.2节。已有精确匹配的完整对照直接复用，不因换章节、写文档或进度脚本修订重跑。
- 不再要求单点/100条重复三次、倒序、乱序、子集、分批末态、独立首中末或专门未来追加/修订注入矩阵。
  不为每个 successor 另建或执行一套算法负例专项测试；原有平台公共合同/安全测试按平台代码改动范围运行。
- 标准执行器继续校验精确五字段 Result、Request身份和日期；这些是既有接入合同，不是新增算法实验。
- W1-W3 不 import 平台代码，不访问 DB/网络/额外代码，不启动子进程；stateless 方案不得读写持久状态，stateful
  方案只允许通过 5.2.1 的 state-input/state-output 合同读写自己 exact version 的派生状态；W4 只允许读取
  manifest 内已锁定的 binary payload，其他边界相同。

### 6.2 同输入等价

比较证据必须同时绑定 `generation_id + 五文件 SHA-256 + data_snapshot_id + 完整七字段 Request SHA-256 +
old/new code hash + runtime environment fingerprint`。Request 数量/顺序/ID、三个日期与方向必须逐行零差异，
旧、新算法必须使用相同三个 cutoff key。比较双方输入摘要不同只能标记 `data_vintage_mismatch` 并同代重跑，
不能豁免差异或调参贴历史结果。这一要求只约束算法比较双方，不再要求其 generation 等于正式回测 generation。

正式回测独立保存当前输入及每行 `source_row`，与算法等价证据通过相同 old/new code、运行环境、完整 Request
ID/三日期区间关联。只有两类证据输入也相同时，三个 cutoff key 与方向摘要才须交叉相等；不同输入则分别进入
plan SHA，不覆盖原始 generation、snapshot 或结果摘要。

### 6.3 性能

- stateless 方案及 stateful ready-state 的单条 predict ≤ 120 秒；
- 离线 backtest（100 条及完整正式区间）每次调用安全上限为 7200 秒，实际耗时单列报告；
  不再以 100 条／600 秒、完整区间／1800 秒作硬性准入，调用方更短 deadline 仍生效；
  这是迁移验收调用预算，stateless 通用 Profile 默认值不变，须由调用方显式限制；
- stateful 首次预热与恢复时间单独实测，不计入每日 predict SLA；部署前按实测与调度要求确定恢复预算，
  不将开发时间窗口作为恢复 SLA；
- 单算法进程峰值 RSS ≤ 4 GiB；
- `fallback_used=false`；
- 无子进程，数值库线程不超过 8。

性能报告必须区分 `cold_prewarm`、`warm_predict`、`warm_batch`，记录运行前后状态快照摘要、起始覆盖区间与新增
计算区间；不得把已有 Native cache 预装成 Blackbox state 后声称完成冷启动，也不得只报告 cache hit 的最好一次。
完整覆盖被测日期的快照回放和从区间起点推进必须分别披露；最终按 5.2.1 A2 验收实际新增计算的性能。

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

- 无法证明算法比较双方同 generation/Request，或无法证明各类证据自身输入、代码与运行环境身份；
- 任一日期或方向不一致；
- successor 需要数据库、网络、跨方案 import、子进程或不受 5.2.1 合同约束的持久 cache；W1-W3 需要额外代码，
  或 W4 读取 manifest hash closure 之外的代码；
- stateful successor 的输入复用证据、exact version、状态格式、快照完整性或单 Writer 任一无法证明；
- exact version、Registry、matrix、release manifest、DataBridge authority 不一致；
- 存在 running run、第二 writer 或 timer 无法 fence；
- 需要覆盖、删除或修改旧业务事实；
- 部分提交、insert-only 冲突、Dashboard 影响其他方案；
- 任一性能门槛不达标；
- Mac3 待晋级 archive 与 ECS 已验证 archive 不同。

## 8. 最终清理与 Migration 025

只有 26 个 Native 在对应环境完成切换、真实 one-shot 控制面模拟和回滚窗口后，才删除 Native scheme/core/config、25 个专属
回测 runner、adapter/source runner/DB 注入/ABI、Liwei publisher/consumer cache wave、Native scheduler
subprocess/artifact/gray 分支、Native Gate/onboard/policy、专属测试、`docs/native_v1/` 入口，以及本项目的临时
cutover CLI 和映射。通用 Blackbox incremental-state contract、executor 接口和状态恢复测试属于目标架构，不能随
Native cache 清理删除；两者不得互相 import。

随后分两次发布 confidence-agnostic Blackbox release，确保 ECS/Mac3 的 current 与 previous 都不读写
confidence。得到独立生产授权并验证可恢复快照后，新增并只通过受控 migration CLI 执行
`025_drop_confidence.sql`，删除 `t_scheme_predictions.confidence` 和 `t_backtest_predictions.confidence`。
025 必须支持两列均在、只剩一列、两列均不在三种可恢复形态，其他 schema fail-closed。先 ECS 验证，再单独
授权 Mac3；DDL 后禁止回滚到 confidence-agnostic 边界以前的 release。

## 9. 完成定义

当前 ECS-only 阶段完成条件：W1-W3 的 21 个 successor target 在 ECS 完成受控替换、灰度、真实 one-shot
模拟和回滚验收；Mac3 版本、业务库、调度及域名保持原状。以下为未来全项目完成定义，不是本阶段继续操作 Mac3
或删除其仍在使用的 Native 路径的授权。

必须同时满足：W1-W3 的 21 个 successor target 全部通过合同/等价/性能并在 ECS 接管；同一 archive 晋级
Mac3；W4 九个 Mac3-only binary-bundle successor 通过 manifest/ABI/合同/等价/性能；Mac3 30 个 target 完成
切换与真实 launchd one-shot 模拟；old Registry 全 archived 且无新
Native run；Native 可执行路径与临时迁移工具已删除；全量、架构、isolated MySQL 和 migration recovery 测试
通过；所有 stateful successor 在 ECS/Mac3 各自拥有 exact-version ready state，逐日增量、历史修订失效、崩溃
恢复和单 Writer 验收通过；旧 Liwei publisher/consumer cache wave 已删除且通用 Blackbox state 不引用 Native；
025 已删除两个 confidence 列；Dashboard、Actuals、其他 Blackbox 和调度控制面无非计划变化。

当前全局状态仍为 `IN_PROGRESS`。ECS 基线已经重新只读核验；W3A 的 `cons_sda` 已通过离线 conformance，
同 wave 的 `full_oos` 已完成 5.2.1 A0/A1 与 A2c 完整本地离线冷/热等价、每日推进及真实 generation 复用验证。
平台状态扩展已通过本地实现测试、独立审查和 full-OOS executor 探针；ECS 隔离预热、十日、重试已通过对应检查，
旧 100 条批量在 600 秒硬限超时；用户调整离线预算后，ECS 完整冷333条、Mac同输入参考比较与
同 Linux Native 独立333条等价均已通过，满足用户简化后的算法等价验收；额外矩阵已取消，仍待正式入库与切换验收。
full-OOS 两文件已提交并以不可变包安装至 ECS 私有验证目录，保持 paused/draft、部署范围为空；
尚未完成 ECS 持久化回测或生产切换，不能用单日性能或独立等价通过宣布整体闭环。
W2、W3B-D 也尚未完成新预算下各自的离线和每日验收，须逐方案分类，不能外推当前试点通过。W4 的
Mac3-only binary-bundle 架构已经获得确认，但必须等 W1-W3 晋级 Mac3 后实施；不能用旧 adapter、未纳入
manifest 的二进制、旧水位、旧 Native cache 或文档声明冒充闭环。
