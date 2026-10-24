# 当前状态

**文档状态**：`CURRENT`

**最近运行核验**：2026-10-18（最新完成新 Mac 公网 Backend/独立隧道接管，模型调度仍停用；2026-10-09 最新完成 Mac 四个日频衍生缺口补齐，原清单源差异剩余 27 项；此前源版本追因及各批修复分别见下方记录；其他条目保留各自核验日期）。以下是已核验基线，不替代实时现场检查。

固定主机、SSH/本地转发、生产路径和只读命令见[双机部署与访问入口](operations/DEPLOYMENT_ACCESS.md)。

## 运行与部署

2026-10-18 17:20（Asia/Shanghai，最终切换），按用户授权，当前 Mac 从原 `current` release 启动正式 Backend `8101`，通过中继独立反向端口 `18101` 完成候选验收后，只将 Nginx 上游 `18100` 改为 `18101` 并 reload。公网域名、TLS 和 release 未改，旧机隧道 `18100` 保留回退；固定连接与两个新 LaunchAgent 见[部署访问](operations/DEPLOYMENT_ACCESS.md)。本地 `8100` 验收进程保留，DataBridge、Actuals 和三类预测任务未启用。Backend 增加进程级空闲防休眠，未修改全局电源设置。

切换前独立隧道验收覆盖 110 个 target 的汇总、最新月明细、首页及六个资源；切换后十张业务表全部字段按主键摘要、行数及完整产品投影与切前相同，25,222 条预测未改，无 queued/running 模型 run。最终公网 110 个 target 的汇总与最新月明细、六资源 GET/HEAD、现有公网入口及六个代表 base 的 Dashboard Gate、真实浏览器登录/展示/退出均通过；最终切换后中继已记录 184 次请求，无 5xx 或新上游错误。公网最终 HTTP/浏览器结果见本次[切换回执](/Users/macstudio0/bond-factor-lab-production/runtime/releases/public-cutover-20261018-1631/README.md)。首轮曾出现 502，慢响应后连接复用复现由 Backend 保活 5→75 秒消除；二轮 504 后定位 SSH 经过 utun91，先回退旧上游，再仅给新 SSH 加 `-B en1`，中继到新 Backend 的八次 health 降为 0.041—0.075 秒（此前 1.34—2.11 秒），全局 VPN/路由未改。两次失败与回退原件均保留，全部自身测试会话已撤销。此次完成网络和展示接管，不代替输入 ready、预测补缺或自然运行验收。

与旧公网的迁移基线对比：25,222 条共同预测的来源、三日期与方向完全一致，旧公网另有 123 条 10 月预测；共同记录中 113 项 Actual 为本机空、旧公网有值。特征日截至 9 月 30 日的 25,007 条预测一致；源表修订没有改写已有预测。本轮保留上述水位差异，后续工作见[待办](TODO.md#新-mac-公网接管后的数据与运行验收)。

2026-10-18 后续只读完整性核查：以预测应发日 9/30—10/18、当前 106 base / 110 target 的正式资格及本机交易/周/周期桶日历生成 538 个唯一业务键，本机存在 271、缺失 267；独立冻结日历枚举与新只读 SQL 交叉通过。缺口包括 9/30 季均 5、10/3 周频 28、10/8 日频 53、10/9/12/13/14 各日频 7、10/15 日频 53 + 月中收 11 + 月均 5、10/16 日频 53、10/17 周频 31。10/10 周频齐全，五个年均本期无应发；无日期偏差或日历解析未决。267 中仅 123 是此前旧公网已有、本机没有，另 144 在旧快照也未见。本轮未运行模型、补写或启用调度；当前 active 无法证明所有历史暂停区间，故该结果是业务完整性基准，不等同宿主自然触发审计。见[逐条完整清单](/Users/macstudio0/bond-factor-lab-production/runtime/reports/prediction-gap-audit-20261018/main/预测缺失完整清单.html)和[独立核验](/Users/macstudio0/bond-factor-lab-production/runtime/reports/prediction-gap-audit-20261018/scope-audit/independent-validation.json)。


源码方案执行迁移、平台 confidence 退役及展示验收衔接修复均已完成。最新特征基准日展示、区间应用修复与日历界面已发布；功能验收见[需求与验收记录](product/FEATURE_DATE_DISPLAY_ACCEPTANCE.md)。新算法仍按[标准入库流程](onboarding/README.md)处理。

| 项目 | ECS 独立灰度 | Mac3 生产 |
|---|---|---|
| 迁移结果 | 17 个原方案、21 个 target 使用 Blackbox | 同左 |
| 当前 active 执行身份 | 97 个 Blackbox base | 97 个 Blackbox base + 9 个 W4 Native base |
| Dashboard | 101 个 target | 110 个 target |
| 调度控制面 | systemd one-shot/timer | launchd + installed plist |
| current release | `1073d68aa589d4278320ff0d43661403f2a1c8bb` | `1073d68aa589d4278320ff0d43661403f2a1c8bb` |
| previous release | `759048c4eaf90cfac04067d613b4e219013472a7` | `759048c4eaf90cfac04067d613b4e219013472a7` |
| schema | 025 APPLIED | 025 APPLIED |

- 2026-09-24 Mac3 于 10:36 非正常重启后，MySQL 8.0.45 因 InnoDB redo 可扫描 LSN 落后于
  checkpoint，连续在 `log0recv.cc:3902` 断言，3306 无监听且 Backend health 返回 500。按用户授权，
  从隔离副本以 `innodb_force_recovery=6` 导出六个业务 schema，在新 datadir 重建；408 张表逐表导入
  成功且与恢复副本的精确行数一致，36 个触发器、45 个视图、9 个例程、3 个事件和业务账号权限核对通过。
  原 datadir 保留为 `/usr/local/mysql/data-corrupt-20260924`。12:24 由原 Oracle launchd daemon 启动
  普通模式 MySQL，原 server UUID、TLS 与 binlog 连续性保持；项目及源数据只读账号连接成功，本机和公网
  health 均为 200。`force_recovery=6` 跳过 redo，逐表核对不能单独证明崩溃前全部提交无损；原 binlog
  最后 row event 与恢复库 outbox 水位相符，完整业务无损仍需按上游事实核对。非正常重启的上游原因及
  恢复后的下一次自然任务待观察。见[恢复记录](/Users/macstudio0/bond-factor-lab-production/runtime/recovery/mysql-20260924/README.md)。
- 2026-09-17 按用户授权，Mac3 经正式单日 `signal-gap-fill` 补齐三套 `cgb_a4_fundseason_*_hl18`
  月频信号：特征日/预测日 9 月 15 日、目标日 10 月 15 日，3Y/5Y/10Y 均为收益率跌，run
  `5130`—`5132`。当前 exact 不变，来源保留 `gray_live`；三条均待验证且与 labchat 对应预测一致。
  精确新增三条预测和三条 run，三套方案既有预测逐字段不变，公网认证明细验证通过。未恢复已清理的
  D5Y 历史、修改其他预测或调度；9 月 15 日输入校验失败与后续自然运行观察仍独立保留，人工补缺不算
  自然成功。见[补齐回执](/Users/macstudio0/bond-factor-lab-production/runtime/reports/monthly-gap-fill-20260917/README.md)。
- 2026-09-17 区间应用修复与日历界面 release `759048c4` 已经用户本地验收，通过独立复审及四项 CI，
  按 ECS → Mac3 晋级同一 archive，SHA-256 为
  `d4abf074696613ffb92062bb94b9562d1546c7359ae8178f95c5846cd368663e`。应用时保留用户输入，
  同区间重复点击合并；手动及自动重试保留最后提交区间，默认全部历史不被固定日期替代。
  两端实际 Backend cwd、health、九方案候选 Dashboard/DataConsistency、正式区间与整月明细检查通过；
  Mac3 公网资源摘要、GET/HEAD、MIME、缓存、认证及拒绝路径检查通过。正式 Edge 普通刷新加载新日历，
  选取 2025-07-01 后连续点击应用，输入、统计范围及整月详情保持一致。
  发布前后十张业务表逐行摘要及 schema 完全一致，ECS 97 / Mac3 106 个 target、九个生产标记保持。
  未修改调度或运行算法；临时会话已撤销，候选 Backend 已关闭。首轮 CI 的代理启动测试因模拟 DOM
  缺少新控件方法失败，测试夹具补齐后最终精确提交四项 CI 全部成功。
  证据见[发布回执](/Users/macstudio0/bond-factor-lab-production/runtime/releases/range-calendar-ui-20260917/delivery-report.json)
  与[证据索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/range-calendar-ui-20260917/README.md)。
- 2026-09-17 特征基准日统一展示 release `056534b2` 已按用户授权合并到 `codex/develop`，
  通过四项 CI 后按 ECS → Mac3 晋级同一 archive，SHA-256 为
  `e6388852c1b805c0a203256c95e2590aba2e37beff06f067cadd8a919f97d3d0`。双机实际 Backend cwd、
  health、Dashboard V7 和原控制面读回正常；ECS 97 / Mac3 106 个 target、九个生产标记保持。
  两机九方案 Dashboard/DataConsistency 全部通过；从 2025-07 开始的区间汇总、跨 5—6 月的日级
  区间与整月明细、展示分类、样本计数及非法区间拒绝均通过。Mac3 公网首页、全部同源静态资源
  GET/HEAD、MIME、内容版本摘要、缓存、匿名 401、精确拒绝路径和九方案认证 Dashboard 全部通过。
  正式浏览器普通刷新后数据就绪，日级筛选、整月详情、同年日期简写和每日验证表固定表头已实测。
  发布前后十张业务表的逐行内容摘要、数量及 schema 完全一致，未重算预测或改变调度。
  首轮 `e0ce91b4` 的页面资源版本 token 未更新，在公网验收时发现并由 `056534b2` 修正；
  previous 指向该中间版本，不应直接作为通过验收的回退目标。发布前稳定 release `22f5e734`
  仍保留，恢复须按部署手册重新核验兼容与授权。验收会话已撤销、临时候选 Backend 已关闭。
  完整证据见[发布回执](/Users/macstudio0/bond-factor-lab-production/runtime/releases/feature-date-display-20260917/delivery-report.json)
  与[证据索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/feature-date-display-20260917/README.md)。
- 2026-09-17 最终 Gate 合同 release `22f5e734` 由 clean commit 构建为同一 archive，SHA-256
  `5f906fbd8304c991b60810292bd2686a5ee3d8691289abf970f8c0b7c578b2e5`；Python、Node、MySQL 8.4、
  release 四个 CI 任务均成功，独立复审无 Critical/Important。ECS 候选认证 Dashboard/DataConsistency
  对目标方案通过后，按 ECS → Mac3 原子晋级并刷新 Backend；两端实际 cwd、health、schema 025、
  当日 DataBridge ready 和控制面读回正常，前后十张业务表计数完全一致。Mac3 目标方案的公网认证
  Dashboard/DataConsistency 均通过；公网入口脚本对首页同源静态资源、GET/HEAD、MIME、版本摘要、缓存、
  匿名 401 和精确拒绝路径全部通过。已删除的旧回测不被重建；仅在既有清单冻结的 78 条 live 业务值
  完全匹配时，Gate 才接受此方案的无回测分区和旧 retired Native 缺失字段，后续 Blackbox 仍严格校验。
  另行扩选的九个 Liwei 方案并非本轮原验收清单，其中八个旧回测 run 缺样本数权威，DataConsistency
  会如实阻断；用上一版 `47df` 在同一 ECS 数据上复核也有相同八项缺口，故不将扩选失败归因于新代码，
  也不声称该扩选范围通过。自然运行和公网长期性能观察仍在 TODO。
- 2026-09-17 二次审计候选 `69c1a7046432564fe1c50a0a9d9f7beed3197671` 已完成四层 CI、独立复审、
  ECS 候选与正式九方案 Dashboard/DataConsistency 验收并晋级；archive SHA-256 为
  `8588983881a76351d61203f89671464b05332de35a01547f130ef986ed02a8f3`。ECS 晋级前后
  Registry、Version、Prediction、Run、Backtest 与四类 Actual 计数均未变化。
- 2026-09-17 19:00 Actuals 自然结束后，两机使用同一 SHA-256
  `e5e2e2c7a04a2e2eb4ed29605b0f84cd342e46342209aa4140dd1c9e81262140` 的 `47df08fb` archive
  按 ECS → Mac3 顺序晋级，仅原子切换 current 并刷新 Backend。两机 Backend 实际 cwd、health、schema 025
  与 launchd/systemd 控制面读回正常；晋级前后 Registry、Version、Prediction、Run、Backtest 和四类 Actual
  表计数均不变。该提交的 Python、Node、MySQL 8.4、release 四项 CI 均成功，ECS 晋级后九方案
  DataConsistency 的 completeness/lineage/representation 全部通过。
- 上一版 `47df08fb` 发布时，Mac3 已按用户明确的“保留最新正确结果、不以旧审计血缘阻断”范围完成发布，
  当时的 Gate 合同尚不能记为通过：
  九方案 DataConsistency 的 representation 通过，completeness 仅因已删除的 333 条历史回测不再有
  backtest 产品引用而 `BLOCKED`，lineage 仅因保留的 78 个旧 Native live run 缺输入 artifact 和三个
  retired exact 缺 `manifest_hash` 而 `BLOCKED`；expected live 键无差异。九方案 Dashboard Gate 也因
  该 live-only 方案没有 backtest 分区而失败。其余八方案的公网精确入口、静态资源、拒绝路径及认证
  Dashboard Gate 全部通过；live-only 方案单独经公网认证 Summary/Detail 200 验证，9 月 live 明细
  17 行，最新四个 target 方向 `1/0/1/1`、Actual pending。该旧 Gate 合同随后由 `22f5e734`
  按保留 live 的精确业务值范围收口；当时的 `BLOCKED`/失败未被追溯改写，自然运行与公网长期
  504 观察仍另见 TODO。
- 2026-09-17 Mac3 已备份并受控重建 `liwei_0616_5y01_full_oos_k3_div_k10` 的 Blackbox exact
  `603f1574704b` 私有状态；旧状态备份在生产 backups 下 `blackbox-state-rebuild-20260917-Jc3iyc`。
  随后经正式 `gray_live` 入口精确补齐预测日 9 月 14—17 日四个缺口，run `5126`—`5129`，目标日
  9 月 18、21、22、23 日方向依次为 `1、0、1、1`。这些是授权补缺，不能冒充自然调度成功。
- 按用户确认的“保留最新正确结果、不保留旧审计历史”范围，Mac3 已经从产品事实中删除旧回测的
  333 个精确 ID（`9433`—`9765`），并删除其独占回测 run `159` 及级联的 333 条原始回测明细；
  旧历史区间现在不再展示。78 条原 live 与四条新 live 共 82 条仍在，9 月 Dashboard Detail 的
  最新四行方向未变，5 月 backtest Detail 为 0 行。没有复制跨机事实、删除 live、修改调度或切换服务。
  生产逐行备份和恢复清单为 `backups/legacy-backtest-only-20260917-47df08fb/plan.json`，文件 SHA-256
  `f81bbd40d55687a64a0e365ccc94a91cba788bac311526a0ef9f5b3e5e7f4653`；专用清理代码的四层 CI、
  隔离 MySQL 测试和独立复审均通过，同一 archive SHA-256 为
  `e5e2e2c7a04a2e2eb4ed29605b0f84cd342e46342209aa4140dd1c9e81262140`。
- 上一份双机共同 release 为 `3a665c36425a168fa1d24559c9dc987ed2901633`，archive SHA-256 为
  `1eba1e9fbf7a8b0b944bf8de2385ce920ef647021b4ae586481cae14aae25366`。
- 2026-09-16 上线前稳定性加固 release 已按 ECS → Mac3 顺序使用同一 archive 晋级。两机 Backend
  进程实际 cwd 均为 `3a665c36425a168fa1d24559c9dc987ed2901633`，健康且 schema 025；晋级前后
  Registry、Prediction、四类 Actual、Run 和 Backtest 数量不变。Mac3 真实 MySQL Summary 为 106 个方案，
  构建约 0.35 秒；ECS 为 97 个方案，构建约 0.63 秒。
- 公网中继仅新增 `/bond-factor-lab/factor-lab-http.js` 精确静态白名单，通过 `nginx -t` 后 reload；
  公网首页和全部静态资源为 200，新脚本字节摘要与 Mac3 release 一致，未认证 Dashboard 保持 401。
  中继变更前配置备份位于
  `/etc/nginx/bfl-backups/pre-release-hardening-20260916T1827/bond-factor-lab.before`。
- ECS systemd 与 Mac3 launchd 控制面未修改；发布时所有 Writer 均不在运行，Mac3 drift audit 通过。
  本次代码与部署闭环不代替下方各方案首次自然运行观察。
- 集成分支为 `codex/develop`。开发分支的代码或文档提交不代表新的生产 release。
  实时提交以 Git 引用为准，发布以目标机 manifest、current/previous 和进程 cwd 为准。
- 2026-09-13 文档规整版本已按 ECS → Mac3 同包晋级，仅刷新两机 Backend。Registry、exact、业务条数、ready 输入、Dashboard 读模型与生产名单均未改变；两机健康与首页正常，Mac3 公网首页与 release 字节一致，控制面保持原状。未运行算法、写业务事实或跨越正式触发窗口；自然观察仍待 TODO。证据见[同步回执](/Users/macstudio0/bond-factor-lab-production/runtime/releases/docs-guidance-20260913/delivery-report.json)与[索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/docs-guidance-20260913/README.md)。
- 当前 release 包含下述平台清理与收盘候选修复；[发布验收](/Users/macstudio0/bond-factor-lab-production/runtime/releases/platform-cleanup-20260913/delivery-report.json)及[证据索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/platform-cleanup-20260913/README.md)保存双机安装、数据、输入、HTTP 和控制面读回。此前框架清理证据仍在[原核验记录](/Users/macstudio0/bond-factor-lab-production/runtime/releases/framework-cleanup-20260913/verification.json)。

## 旧增量方案缓存恢复与信号补齐

2026-09-21 已按用户授权完成先恢复缓存、再补齐信号：Mac3 六套、ECS 三套共九个组合均经正式
重建并通过正常增量路径验收，用时 7.11—15.62 秒。Mac3 原故障为运行环境摘要与 launchd 不符，
差异可由 `LC_ALL=C.UTF-8` 复现；ECS 三套 5Y AUC/IC 因旧周前缀的五个字段从空值补齐为数值而
拒绝复用。逐套身份及原因见[诊断报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/observations/20260921-old-state-analysis.md)。

Mac3 已补齐六套各六条，预测日为 9 月 14、15、16、17、18、21 日，run `5458`—`5493`；ECS 已补齐
三套各一条（预测日 9 月 21 日），run `6372`—`6374`。共 39 条均为 `gray_live`，完整业务键零缺口，
三日期、exact、成功 run、本机输入与 private state 读回通过。缓存阶段两机业务新增均为零；补缺后
各自 11 张业务表的旧事实逐行摘要、生产缓存及生产标记保持不变。19:00 Actuals 自然任务两机均
成功且新增/变更为零。两机 current 仍为 `1073d68a`，源码摘要、实际 Backend cwd、健康与原控制面
检查通过，未修改算法、release、调度或服务。

两机 Dashboard Gate 和 Summary/Detail 展示对账通过（Mac3 126 次 / 2,502 条事实，ECS 63 次 /
1,251 条事实）。本次范围的**全历史 DataConsistency Gate 未通过**：Mac3 六个、ECS 三个旧回测 run
缺 `persisted_prediction_count` 依据；分别有 1,998 / 999 条旧回测产品记录 exact 为空，并定位到
六条 / 三条 feature 2025-01-20、target 2025-01-26 的旧日期例子，现行 T+5 合同要求 target 1 月 27 日。
这些旧事实与维护前一致；未伪填证据、改写历史或放宽校验，存量问题另列 TODO。

ECS SSH 回传曾中断；读回确认前两条已成功、无在途 Writer、最后一条未启动后，仅续执行剩余一条，
没有重复计算成功部分。原状态备份、完整回执、连接恢复与验收限制见
[本机维护证据索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/state-repair-20260921/README.md)
和[结构化报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/state-repair-20260921/completion-report.json)；
ECS 原件在同批 incoming 目录的 `state-repair-20260921/`。九个组合修复后的自然调度仍待真实窗口核验；
三套 AUC/IC 的周数据成熟处理未在本次修改，复发风险仍须跟踪。补缺不计作自然成功，恢复不能用旧整库
快照覆盖持续增长事实，也不能盲目覆盖已原子发布的新状态。

## 四套 V3 新方案交付状态

2026-09-20，四套独立 Blackbox V2 新方案已按 ECS → Mac3 顺序使用同一不可变 archive 完成
完整持久化回测、正式激活、灰度区间补齐及 release 晋级。发布提交为上表 `1073d68a`，archive
SHA-256 为 `85f1dfd5459c1d9fa1321a9bddc541566d4dcdad91c69f1227226b76aa66ee8e`；
[入库 PR #58](https://github.com/lucky1day/finlab-graylab/pull/58) 已集成 codex/develop，精确提交的
[四项 CI](https://github.com/lucky1day/finlab-graylab/actions/runs/35457127763) 与独立复审通过。
四份脚本原字节保留，Metadata 仅 owner 从 `lw` 改为 `liwei`，均未启用增量状态。

| base ID | exact | ECS / Mac3 回测 run | 每机历史 / 灰度 |
|---|---|---|---|
| `daily_t5_3y_balanced_vote_cbe3_v3` | `20b906802d4e` | 294 / 265 | 333 / 83 |
| `daily_t5_10y_balanced_vote_143b_v3` | `8c4223f5bc9e` | 295 / 264 | 333 / 83 |
| `weekly_3y_full_action_vote_a7b5_v3` | `bb29fd3e64a8` | 296 / 267 | 72 / 17 |
| `weekly_10y_full_action_vote_873e_v3` | `7255ee2dea19` | 297 / 266 | 72 / 17 |

回测 predict 起点为 2025-01-01，训练输入未按展示起点裁剪；每套有 17 条月度指标。历史 target
均早于 2026-06-01；灰度授权半开区间为 `[2026-06-01, 2026-09-25)`，按已到应发时点的完整
Request 集补齐至 target 9 月 24 日。每机新增 810 条历史、200 条 `gray_live`，共 1,010 条
产品事实；三日期、完整业务键、run 引用、历史/灰度零重叠和应有点零缺口均核验通过。

ECS 使用本机 `full-20260919-063336-5a4ef586fcda` / `snapshot-eaeb49d4dd7477f34aa1d542`；Mac3 使用
`full-20260919-063120-01160051963d` / `snapshot-919d2f6fdd306129e212fe49`。两机分别计算，原有
11 张业务表旧事实逐行摘要、四类 Actual 和九个生产标记均不变，W4 仍仅保留 Mac3 既有范围。
两端实际 Backend cwd、health、schema 025、四套调度资格通过；ECS 五个 timer 保持
loaded/active/enabled，Mac3 七项 installed/loaded 配置无漂移。仅刷新 Backend，未修改调度。

两机本机 Dashboard/DataConsistency 均通过，各覆盖 84 次详情对账、1,010 条事实，10 条 Actual
pending 且无成熟 Actual 缺失。Mac3 公网入口专项检查通过：首页和全部同源静态资源的 GET/HEAD、
MIME、版本摘要、缓存、认证 Dashboard、匿名 401 与精确拒绝路径均正常，临时会话已退出。
另行通过公网执行的两轮全量 DataConsistency 展示
对账均因 TLS `UNEXPECTED_EOF_WHILE_READING` 失败；一次复核在登录握手阶段同样失败。原失败
回执保留，未修改网络/代理/认证配置；该额外全量公网对账及连接可靠性仍待复核。浏览器视觉刷新也未完成：
Computer Use 工具因当前 URL 限制终止会话，已停止交互；HTTP 检查不代替视觉验收。

2026-09-24 凌晨只读复核：两机日频在 9 月 21—23 日三个窗口累计 12 条 `scheduled_live`
run/预测均成功，较上次新增确认八条。9 月 22 日 ECS 10Y/3Y run 为 6377/6378、Mac3 为
5499/5500；9 月 23 日分别为 6423/6424、5548/5549。三日期由各机权威日历核对，exact
与交付一致，未发现本批日频缺口。两机本机 Dashboard/DataConsistency 再次通过，各对账
1,016 条事实；release、实际 Backend cwd、四套 active 资格和各机三个五文件输入快照均通过。
严格自然验收仍为 **2/8**：ECS 三个窗口均有 systemd 宿主触发、同次日志及业务记录联合证据。
Mac3 installed/loaded、执行器日志与成功业务记录一致，但对应 launchd 宿主事件仍未取回：
9 月 22、23 日窗口的统一日志查询均无匹配，现存轮转日志最早为 9 月 23 日 17:06，晚于
07:03 窗口。两项保留为“待宿主触发证据”，不当作算法失败或预测缺失；须在自然触发后日志
仍保留时取得事件证据。本轮未修改任何调度。周频四项首次窗口仍为 9 月 26 日 11:30
Asia/Shanghai，feature 9 月 24 日、target 9 月 30 日，尚未到期。三个日频窗口不能证明长期稳定。
详见[自然观察证据](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/observations/20260924/README.md)
和 [TODO](TODO.md#四套-v3-新方案自然观察与页面验收)。

2026-09-25 03:45 Asia/Shanghai 新增只读核验：9 月 24 日日频窗口 ECS 10Y/3Y run
`6469`/`6470`、Mac3 `5597`/`5598` 均为成功 `scheduled_live`，predict/feature/target 为
9 月 24 日/9 月 23 日/10 月 8 日，与本机日历、输入和 exact 一致。四个日频窗口双机累计
16 条成功记录；两机 Dashboard/DataConsistency 各对账 1,018 条事实。ECS 有 07:03:23
systemd 启动证据；Mac3 找到 07:13:23 launchd 任务结束事件，但仍缺 07:03 日历启动事件，
严格验收保持 **2/8**。周频四项尚未到 9 月 26 日窗口，9 月 25 日日频窗口本次检查时也未到。
Mac3 MySQL 已恢复后的 health 为 200；其恢复限制见上文独立条目。本轮证据见
[9 月 25 日观察](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/observations/20260925/README.md)。

2026-09-27 凌晨截止检查：9 月 26 日 11:30 周频首次窗口两机均有真实宿主日历触发。ECS systemd
于 11:30:23 启动、11:40:57 成功退出；Mac3 `UserEventAgent` 于 11:30:05 记录 `StartCalendarInterval`，
launchd 于 11:35:20 记录任务结束。ECS 10Y/3Y run 为 `6529`/`6534`，Mac3 为 `5660`/`5665`；四条均为
成功 `scheduled_live`，各有一条同 run 预测，predict/feature/target 为 9 月 26 日/9 月 24 日/9 月 30 日。
两机分别使用本机 9 月 26 日五文件快照，exact、active 资格、不可变 release/manifest、实际 Backend cwd、
health 与四套本机 Dashboard Gate 均通过。周频四项正式计入自然成功，严格验收从 2/8 增至 **6/8**。
Mac3 日频两项仍有成功业务记录，但 9 月 21—24 日 `StartCalendarInterval`/启动事件在现存统一日志中
未取回，不能以执行器日志或任务结束事件替代；9 月 25 日两机权威日历均判定非交易日，没有新日频应发点。
两项维持待宿主触发证据，不标为算法失败或已通过。按 9 月 27 日截止约定，`v3` 自动化已暂停；后续如需
关闭这两项，应在新的真实日频窗口后及时保存 `UserEventAgent` 日历触发、同次运行及产品证据。
外置证据见[9 月 27 日最终观察](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/observations/20260927/README.md)。

原件、最终摘要、输入绑定、回测/激活/补齐、产品及控制面读回、初次检查入口使用错误与恢复边界
见本机可读的[证据索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/README.md)和
[交付报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/delivery-report.json)。
ECS 原件在 `/opt/bond-factor-lab/incoming/four-v3-20260920/`，已核验回执副本位于上述本机证据的 `ecs/`。
previous 仅提供源码恢复依据；回退链接不撤销本次数据库事实，不可用旧整库快照覆盖持续增长的数据。

## 1Y T+1 跨期限日内方案交付状态

`one_y_t1_cross_tenor_intraday_v1` 已于 2026-09-16 按 ECS → Mac3 顺序使用同一不可变 release 完成首次
Blackbox V2 入库、持久化回测、激活和灰度区间补齐。平台只删除上游脚本的 100 条批量上限并同步帮助文案，
方向算法与 Metadata 未改；修正后 code hash 为 `c7cbc002a6c2f6e5494267808938d78947b22f29ed1281e33d70159878d5ab3c`，
Metadata hash 为 `f4f8e44ba028fa0372d64d4c7ab538b135a01f487f60288a1879b91e6e06e2b5`，exact 为
`4f0b95e57fdf`。

| 每机覆盖 | target 范围 | 条数 |
|---|---|---:|
| 历史回测 | 2025-01-03—2026-05-29 | 337 |
| `gray_live` | 2026-06-01—2026-09-16 | 77 |
| 产品事实合计 | 2025-01-03—2026-09-16 | 414 |

ECS backtest run 为 `293`，输入为 `full-20260916-063338-77f411b810dd` /
`snapshot-a3fa9393445761ecc4e4e355`；Mac3 backtest run 为 `263`，输入为
`full-20260916-063121-0ec9887c47b9` / `snapshot-4f96b57ebab7734843e87312`。两机分别计算，历史与灰度
target 零重叠，414 个业务键全部唯一；Registry 展示为 `BI_CROSS_TENOR_INTRADAY`、owner `fengrl`、
1Y / T+1 / h1。Dashboard 读模型已验证 active 且 `is_production=false`，未修改生产方案名单。

首次真实宿主时钟运行窗口为 2026-09-17 07:03 Asia/Shanghai；当前仅完成灰度补齐，不能把它冒充
`scheduled_live` 自然运行。双机数据库与 release 读回见[证据索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/one-y-t1-cross-tenor-20260916/README.md)
和[交付报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/one-y-t1-cross-tenor-20260916/delivery-report.json)。

## 八套新方案交付状态

两批均已完成双机交付与模拟验收，首次自然运行仍待[TODO 中的独立观察](TODO.md#八套新方案自然观察)。以下是交付时证据，不是当前生产输入水位。

| base ID | exact version | 任务 / 期限 | 批次 |
|---|---|---|---|
| `weekly_3y_full_action_rule0001_v2` | `ac06e7df4582` | weekly_point / 3Y | 五套新方案 |
| `weekly_10y_full_action_hgb0061_v2` | `e87140ab2ce2` | weekly_point / 10Y | 五套新方案 |
| `cgb_a4_fundseason_3y_hl18` | `79c08e273ecd` | monthly / 3Y | 五套新方案 |
| `cgb_a4_fundseason_5y_hl18` | `38c8fa7b0220` | monthly / 5Y | 五套新方案 |
| `cgb_a4_fundseason_10y_hl18` | `295c00e305e2` | monthly / 10Y | 五套新方案 |
| `weekly_5y_full_action_lowcorr01_v3` | `9ebe38835599` | weekly_point / 5Y | 三套 5Y 周频 |
| `weekly_5y_full_action_lowcorr02_v3` | `abd2dc14c172` | weekly_point / 5Y | 三套 5Y 周频 |
| `weekly_5y_full_action_lowcorr03_v3` | `2dccf75844e9` | weekly_point / 5Y | 三套 5Y 周频 |

任务均为 h1。八份算法脚本保持原字节；仅五份周频 Metadata 的 owner 修正为 liwei（三套 5Y 原值为 lw），月频原件未改。

| 每机覆盖 | 每套历史 target / 条数 | 每套灰度 target / 条数 | 该批每机合计 |
|---|---|---|---|
| 五套新方案中的两套周频 | 2025-01-10—2026-05-29 / 72 | 2026-06-05—2026-09-18 / 16 | 与三套月频共 192 历史、44 灰度 |
| 五套新方案中的三套月频 | 2025-02-14—2026-05-15 / 16 | 2026-06-15—2026-09-15 / 4 | 同上 |
| 三套 5Y 周频 | 2025-01-10—2026-05-29 / 72 | 2026-06-05—2026-09-18 / 16 | 216 历史、48 灰度 |

两批分别使用本机输入独立计算；exact、三日期、完整业务键和来源核验通过，历史/灰度零重叠、应有点零缺口。Dashboard、Actual join 与纯待验证月份明细通过；交付时的输入 generation、回测 ID 和逐点结果见下方证据。

两批复用既有周频和 close-period 控制面；到期/非到期、错误控制面和重复键隔离模拟通过，无生产模拟写入。原有事实与 W4 九套的 exact、文件、依赖和展示保持完整。

### 交付证据索引

| 批次 | 本机可读索引 | 目标机原件位置 |
|---|---|---|
| 五套新方案 | [证据 README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/new-five-20260912/README.md)、[最终验收](/Users/macstudio0/bond-factor-lab-production/runtime/releases/new-five-20260912/delivery-final.json) | ECS：`/opt/bond-factor-lab/incoming/new-five-20260912/` |
| 三套 5Y 周频 | [证据 README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/weekly-5y-lowcorr-20260913/README.md)、[交付报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/weekly-5y-lowcorr-20260913/delivery-report.json) | ECS：`/opt/bond-factor-lab/incoming/weekly-5y-lowcorr-20260913/` |

索引区分最终验收、历史过程、原 archive 与晋级副本；CLI 回执、输入 lineage、数据/页面/控制面、公共测试及模拟证据按主机保存。一次性 operator 脚本已归档退役，仅供追溯，不作为新操作入口。

## 保留身份与恢复证据

W4 Native 与 Blackbox 的实际范围以[部署矩阵](../deploy/scheme_deployment_matrix_v1.json)及本机 Registry/version 为准；保护原则见[根规范](../AGENTS.md)，迁移边界见[算法保真](architecture/SOURCE_ALGORITHM_FIDELITY.md)。

- ECS 的 11 个独立临时身份及 Mac3 的两个旧验证库已清理。以下两个 ECS 身份获批永久保留为只读历史来源：
  - `liwei_0616_5y01_full_oos_k3_div_k10_bbv2`
  - `liwei_0616_cons_sda_k3_div_k10_bbv2`
- 两个来源的 Registry 为 archived、版本为 retired，无 canonical、Writer 或 Dashboard 展示。
  ECS 已在完整备份、事实摘要、run lineage、raw backtest 与业务键冲突检查后，删除
  `liwei_0616_5y01_full_oos_k3_div_k10` 被 411 条 `_bbv2` 纠正事实唯一替代的 411 条旧产品事实、
  78 个旧 live run、1 个旧 backtest run 及 1 个旧 retired version；替代来源的 411 条产品事实、333 条
  raw backtest 与成功 source run 保留，Dashboard 只读投影继续使用其精确 metadata。另一个来源未在本次清理
  范围内。Mac3 不存在该 `_bbv2` 来源，因此未执行相同删除。两项来源均不是普通待删项，也不构成新算法
  入库阻塞。

### Schema 025 与恢复材料

- `t_scheme_predictions.confidence` 与 `t_backtest_predictions.confidence` 已通过 migration 025 删除。
- current/previous 均兼容 schema 025。禁止回滚到仍读写 confidence、依赖已删临时身份或要求旧 Native 接管状态的 release。
- 恢复操作按[部署手册](../deploy/README.md)核验权限、输入/状态和 Writer；以下材料是恢复依据，不是回滚授权。
- 永久备份与恢复原件：
  - ECS confidence：`/opt/bond-factor-lab/backups/confidence-retirement-20260912/ecs-v1`
  - Mac3 confidence：[/Users/macstudio0/bond-factor-lab-production/backups/confidence-retirement-20260912/mac3-v1](/Users/macstudio0/bond-factor-lab-production/backups/confidence-retirement-20260912/mac3-v1)
  - ECS 临时身份清理：`/opt/bond-factor-lab/backups/database-cleanup-20260912/ecs-eleven`
  - Mac3 旧验证库清理：[/Users/macstudio0/bond-factor-lab-production/backups/database-cleanup-20260912](/Users/macstudio0/bond-factor-lab-production/backups/database-cleanup-20260912)
- confidence 完整恢复集必须同时包含 SQL gzip 与 `original-audit-json.json.gz`；不能只恢复 SQL 而丢失原始 JSON 精度。
  先在隔离库验证恢复，再按明确授权处理精确对象；备份、原始验收回执和 immutable archive 不随文档清理删除。

## 已发布的平台清理与收盘候选修复

2026-09-13，开发工作区移除 Native onboard、专属 Gate、maintenance、新版本激活及两表验证记录写入，并退役 Native 独立历史回测、旧分步写入和未接入补平实现。新回测仅保留月度与期限汇总及必要身份、输入信息，不再生成旧固定分期或空排除摘要。W4 按[固定版本运行边界](../AGENTS.md#算法与数据不变量)保留；Blackbox 入库与公共运行校验继续维护。历史 `t_harness_runs`、`t_harness_gate_results` 及迁移定义保留，当前运行不依赖它们。

收盘入口已统一使用本机生效生命周期，解决 canonical 初始状态掩盖已激活方案的问题；现行选择规则见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。发布验收证明原有身份、事实、输入、HTTP 和控制面保留完整；详细结果由上方平台清理证据保存。

Mac3 公网首轮验收出现一次 HTTP 504，同期 Backend 有 5.37 秒慢请求；随后降频复核的 17 个请求与浏览器刷新、明细均通过。原始异常保留在上述证据目录，原因尚未定位，继续纳入[公网可靠性观察](TODO.md#其他未闭环队列)，不据此宣称长期性能问题已解决。

## Dashboard 生产标记发布

2026-09-13 双机已发布生产标记与 Registry 展示验收修复。名单规则和维护分别见
[Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#生产方案标记)与[部署手册](../deploy/README.md#生产方案名单维护)。
2026-09-13 按用户确认的完整九套生产方案名单，更新两机外置 `production_schemes.json`。当前 ECS / Mac3 各标记 9 个 target；文件摘要一致，真实 API 与 3Y、5Y、10Y 的 T+5、周收盘、月中收九个格子逐项核验通过，其他展示和统计未变。本次只更新名单，无需发布或重启；[结果与原名单备份](/Users/macstudio0/bond-factor-lab-production/runtime/releases/production-nine-marker-20260913/result.json)记录当前范围。名单标记不替代 TODO 的自然运行观察。

Registry 展示验收的 17 个迁移方案误报已解决；首次登记与后续展示的权威边界见[共享契约](architecture/SCHEME_CONTRACT.md#3-方案身份)。本次未更改 Registry 名称、描述、owner、算法或历史。

本地公共回归与独立复审通过，生产标记的非空、替换、清空、错误恢复和长名称视觉已在隔离环境验证。双机真实静态资源与验收版本一致；ECS 92 / Mac3 101 个 base 的 Gate 全部通过，认证 Summary、历史/最新 Detail 和待验证页面与本机事实一致。

发布前后 Registry、exact、历史、预测、Actual 条数及 ready 输入保持一致，两机独立核验。ECS 5 个 timer 保持 enabled/active、Persistent=false；Mac3 7 个 installed/loaded 任务配置不变，W4 九套入口与依赖可用。仅切换 current 并刷新 Backend，未运行算法、写业务事实或跨越正式触发窗口。自然观察另见 TODO。

证据：[最终报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/dashboard-registry-contract-20260913/delivery-report.json)、
[索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/dashboard-registry-contract-20260913/README.md)；
[原功能与隔离验收](/Users/macstudio0/bond-factor-lab-production/runtime/releases/dashboard-production-marker-20260913/)保留原计划、
视觉验证及 `migration-display-analysis.json` / `migration-display-findings.md` 的问题来源，不作为未完成发布状态。

## 当前输入与产品版本

双机使用五文件 DataBridge，存量 `factor_version` 已初始化为 `V1.0`；legacy 保护与新方案输入模式见[DataBridge](blackbox_v2/data_bridge_v1/README.md)。双机产品接口均为 `factor-lab-dashboard-v7`；完整读模型与表示见[Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)。

### 两项源补齐后的剩余数据差异（2026-10-08）

20:37 两端独立只读快照重新核对原 1,805 个发布输入差异位置对应源业务键：1,321 个源值已对齐，
剩 484 个（477 个衍生数值分歧：日频 396、周频 81；Mac 衍生缺口 6：日频 4、周频 2；原始数值分歧 1）。
此为原清单跟踪范围，不是全库穷尽计数。唯一原始分歧为 `P0602235` 日本10年国债收益率源日期
2026-08-25：Mac 2.893、ECS 2.894，冻结生产包输入为 2.893；尚未重新查询 Wind 或追溯观测版本，不能据包判 ECS 错。
两端当前五个发布文件摘要均与原审计文件相同，日频 648、周频 1,157 个输入差异仍在；两项源修复尚未进入发布文件。
与 9/22 冻结生产输入包共同键相比，Mac 有 290 项变化（日频 14、周频 276），这些位置当前两机一致，
属于末端观测覆盖变化；ECS 有 2,031 项差异，其中包含已修源库但尚未发布的旧空值，不能视为当前源库错误数。
共同范围月频、日历和因子目录无内容差异；新增日期/周覆盖单列统计。详见
[剩余差异报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/remaining-data-differences.html)。
本轮未写源库、查询 Wind、重算衍生/算法历史或主动发布输入；477 项尚未全部统一输入观测版本复算归因。

### ECS 两列周频衍生历史补齐（2026-10-08）

按用户授权，11:52:46 在 ECS 上游源表维护上下文中向 `api_wind_derivative_weekly` 精确 INSERT
`TB3YWI1C`、`TB7YWI1C` 各 537 个缺失键（`201603`—`202628`），合计 1,074 条；两列各由
10 条增为 547 条。值由 ECS 本机原始依赖复算，与生产参照和 Mac 精确一致到 12 位小数。
先执行第 537 条主动失败的真实事务演练，目标行与 1,074 个触发器事件回滚后均无提交残留。
正式提交后独立读回通过：原有 20 条完整字段、其他 284,340 条周频记录摘要、原始依赖和日历
不变，Mac 同范围源表、配置与日历也不变。未执行 DDL、服务/调度修改、预测/回测覆盖或主动输入发布。

ECS 双 INSERT 触发器和 outbox 默认来源仍为遗留 Mac 标签，本次实际新增 2,148 个 PENDING
事件，逐主键双事件与值验收通过；来源标签及重复触发器尚未清理。现场无 MySQL replica、outbox
消费者连接或检出的双机自动同步入口，Mac 旧手工整表导入工具不读取 outbox；这些是当时核验事实，
不能外推为未来启用旧同步链路安全。后续 DataBridge 发布与增量状态验收见 TODO。

ECS 私有原件：`/opt/bondprediction/backups/ecs-weekly-repair-20261008/`，包含精确基线、提交前后回执和回滚演练；
[本机报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/ecs-weekly-history-repair.html)、
[独立验收汇总](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/ecs-weekly-history-repair/completed-repair-summary.json)。
提交后恢复须先核对事件消费和输入引用，只能评估此次新 id/业务键精确恢复集，不能整库覆盖。

### Mac 日频原始缺口与源值追因（2026-10-08）

按用户明确授权，20:16:46 已在 Mac 生产 `api_wind_daily` 仅新增 247 条指定缺失键
（8/25 共 244 条、8/24 共 3 条），20:17:16 独立连接读回 247/247 通过，授权范围缺口清零。
本次直接使用现行生产 Wind 入口的当前查询返回；247 键的请求回显、Codes/Fields、DataValid、日期、
单值及原单位转换严格核对通过，结果与下述历史证据 247/247 一致，没有复制 ECS 主键或记录。
首条完整查询命中同轮探针缓存，已核对探针真实 Wind 调用和完全相同载荷；其余 246 项未缓存，
每个键均有本轮真实 Wind 返回依据。事务内和独立读回均确认两天原有 991 条全字段不变，
范围行数 991→1,238，日历和因子定义不变；两个既有触发器产生 494 条本地 `PENDING` INSERT
事件并逐条验收，未运行跨机同步。当前发布日频文件摘要未变，247 个对应输入位置仍为空；
未重算衍生因子或算法历史、修改生产源码或调度。见[完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-daily-247-repair-plan.html)、
[独立验收汇总](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-daily-repair-preflight/completed-repair-summary.json)。
以下为补前根因和准备证据；原采集非原子刷新及派生/输入发布验收仍待单独处理。

13:40 两端只读快照确认：Mac `api_wind_daily` 缺少 247 个指定源键（8/25 共 244 条、8/24 共 3 条），
ECS 全部有值，生产 9/22 冻结包与 Mac current 的对应输入也均为空。当前 Mac 上游采集配置与实验室
连接为同一库。双机 8/26 留存 Wind 日志覆盖这 247 键，均成功、非空、单日单值，两端最后匹配返回
数值相同；按既有单位转换与 DECIMAL 12 位精度核对，ECS 保存值 247/247 一致。先前 16 个晚间更新值
及 50 个无 Mac 源事件键的数值依据已补充确认，不再标为源值未定；此结论不外推至整个 ECS 库或今日
Wind 历史修订值。

Mac 源事件中 197 键已插入后删除，另 50 键在本次留存范围内无事件。新增日志证据发现
`insertDateList.py` 第 27 行 `invalid character '。' (U+3002)`：246 键的最后日频删除尝试打印后
紧接该异常（196 个有删除源事件、50 个仅能证明删除尝试及保存失败）；`G0003956` 为 17:12 已提交
删除与盘后同批次异常旁证，日志无逐键删除标记。写入路径先在独立连接提交删除，再导入日期模块
准备 INSERT，语法错误令 INSERT 未执行；模拟连接的隔离复现已验证此失效机制。确切历史坏文件及
编辑归属未取回；当前日期模块语法解析通过，不代表非原子刷新及历史缺口已修复。
18:37 进一步核对已安装采集 cron 使用 `get_factor` 解释器，在该解释器冷启动导入当前日期模块及
`insertDataToTable` 成功；8/24、8/25、8/26 纯日期计算均为 `202633`，与已读回的 ECS 既有源行
周键一致。日期模块、日历依赖和四个日频采集入口六份文件全部编译通过；未调用写库函数、Wind
或修改当前正常文件。见[实际环境语法验收](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-daily-production-comparison/calendar-syntax-resolution-verification.json)。

18:55 双机重新读回，247 个 Mac 缺失键及 ECS 已核实值未变；18:59 源维护补前只读检查确认
源表与 outbox 为 InnoDB、有日期＋代码唯一索引，两条 INSERT 触发器预计为 247 条补数产生
494 条本地事件。检查时无匹配采集／同步进程、可见目标源写入或 running 平台 run。
两天既有 991 条记录已留存基线；候选清单明确只新增 247 条，预计日期范围合计 1,238 条，
保留所有既有字段。见[精确补齐方案与清单](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-daily-247-repair-plan.html)。
该准备阶段零数据库写入；随后经明确授权重新核验目标、真实源 cron 和在途进程，已按上述范围完成补齐。

此前只读追因未改源表、入库程序、生产调度或发布输入；本次授权源补齐的完成情况见上文。
详细历史统计、双机响应、异常行号与复现回执见
[只读核查报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-daily-production-comparison.html)、
[根因证据汇总](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-daily-production-comparison/root-cause-summary.json)。

### Mac 原始补数影响的衍生值修订（2026-10-08）

按用户明确授权，23:07:23 在 Mac 生产源库仅 UPDATE 528 个既有衍生键（日频 453、周频 75，
94 个因子，8/25—10/7），23:07:40 独立连接验收通过。原主键及非值字段保持，8/24 之后核验
范围内另 10,221 条日/周/月衍生行全字段未变，原始数据、元数据和日历摘要未变。
两个既有 UPDATE 触发器产生 1,056 条本地 PENDING 事件并逐键验收；264 次更新后的主动回滚
演练确认目标行及事件无残留。提交前检查实际源 cron、在途任务和维护空窗；未修改生产代码、调度或 ECS。

对 165 条依赖规则的 2,767 个日期键执行 A/B 复算：A 为已补齐源，B 为同一快照仅移除先前 247
个修复原始键。两个真实解释器的 5,534 个数值精确到 DECIMAL 12 位一致；MySQL 111 条只读查询、
38 种形态的输入集一致。只覆盖旧值严格等于 B 且新值不同的 528 个既有键。另 116 个受影响键
（115 个存量值、1 个缺行）旧值不能由 B 复现，保留待核对；它们与下述剩余 116 项不是同一集合。

23:08 双端新快照确认：原 1,805 差异位置对应源清单剩余 484→116，本次消除 368 项；剩余
109 个衍生有值差异、6 个 Mac 衍生缺口、1 个原始值差异。当前五 CSV 摘要仍未变，已发布输入
仍有 1,805 项差异，未主动发布 DataBridge 或重写预测/回测。源修订不代表发布及前端全链路闭环。
见[完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-derived-repair.html)、
[完成验收汇总](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/derived-repair/completed-repair-summary.json)。
精确旧行、输入摘要、触发器和提交尝试/成功/独立读回回执在报告同目录私有证据中；已提交后的恢复
须核对精确受影响键及事件引用，不能整库覆盖或重复执行写回。

23:19—23:30 后续只读复核：两端已安装且加载每日 06:30 DataBridge 自动发布，ECS 下一触发为
10/9 06:30；10/8 两端发布均失败、ready blocked，current 仍为 9/30 generation。日频日期轴依赖的
三列 TB 收益率源都止于 9/29，当前输入在内存构建明日所需 10/8 截止后仍被 freshness 拒绝。
自动触发不保证发布成功；若此前自然采集推进所需日期轴并通过校验，正常全量导出会纳入历史修复。

剩余源清单全部 109 个非空衍生值和 6 个衍生缺口已完成当前输入隔离复算：8/26 的 85 个值当前
复算支持 ECS、Mac 旧值无法复现；另外 19 个值不匹配两端历史存值，5 个仅差 DECIMAL 第 12 位。
6 个缺口中四个 DR001 当前可复算并匹配 ECS，且不依赖 247 补数；两个英国周频仍无法解释 ECS
历史值。另 1 个日本 10 年原始值版本未决。新增九列 TB 两端各 36,576 条业务值一致，53 个当前
输出公式身份一致，两个 Mac pandas 环境复算无差异；邻日试验不支持统一错一天。历史具体取数/
运行/保存机制仍未还原，本轮没有新增生产操作。见完成报告的「明日发布」「剩余原因」与新增证据。

23:38—23:43 只读上游追因已定位发布阻断链：9/30 两端当天 Wind WSD 采集均逐项跳过三列 TB
收盘收益率；10/1 前日采集默认应请求 9/30，Mac 两次批量请求报 `Connection refused`，ECS 两次
报 `Network is unreachable`，均在取得结果及写库前中断。实际源 cron 无日期参数，现行脚本只取
当天或昨天，有同批重试但没有跨日缺口扫描；后续自然采集不主动补回 9/30。10/8 17:45 两端当天
请求也跳过三列，Mac 23:38 源轴仍止于 9/29。连接异常的具体网络原因及逐项 Wind 错误码未还原。
85 个 8/26 衍生值的数值判断已明确：当前原始输入及已核验现行公式下 ECS 存值正确，Mac 旧值
应修订；未还原历史机制不影响这一当前口径判断，也不外推整个 ECS 数据库。85 键精确候选清单
已形成但尚未写回；本轮未新增生产操作。日志、数值清单和验收见完成报告「今日失败与85条核定」，
及[本轮汇总](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/derived-repair/publication-followup/failure-and-85-verdict.json)。

### Mac 85 条核定历史衍生值修订（2026-10-09）

按用户明确要求，00:28:44 在 Mac 生产源库单事务精确 UPDATE 8/26 的 85 个既有日频衍生键，
00:28:59 独立连接验收通过。只改变数值，原主键及非值字段保留；8/24 之后三张衍生表另
10,664 条记录全字段未变，原始输入、元数据及日历摘要不变。85 键与此前 528 键无交集。
两个既有 UPDATE 触发器产生 170 条本地 PENDING 事件，逐键两条并验收；前 42 次 UPDATE
后主动回滚证明旧行与事件无残留。提交前现场无活动源 Writer 或 running scheme run，cron 未变。

重新捕获 109 个原始代码的 429,265 行输入（85 目标实际依赖 50 个代码）；Mac 两个真实解释器
复算 85/85 精确到 DECIMAL 12 位一致，全部等于此前核定及本轮 ECS 当前值，0 异常。真实
MySQL 的 52 条查询逐行对照通过，执行包及独立审查完成后才写入。当前口径的正确值修订已完成，
历史取数／运行／保存机制未完整还原，仍按既有追因边界处理。

00:29:25 双端新快照核对原 1,805 位置对应源键，剩余 116→31，本批消除 85 项；剩余为
24 个衍生有值差异、6 个 Mac 衍生缺口、1 个原始值分歧。ECS 跟踪范围所有行全字段未变，
Mac 跟踪范围 85 键之外 1,714 行全字段未变。两端五 CSV 摘要均与写前相同，发布层仍有原
1,805 项差异，未发布新 generation 或修改生产源码／服务／调度、预测及回测历史。
见[85 条完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-derived-85-repair.html)、
[本批完成回执](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/derived-repair/verified-85-repair/completed-repair-summary.json)。
精确旧行及提交尝试／提交／独立读回证据在本批独立目录；提交后不得重复执行写入或整库回滚。

### 剩余 31 项源版本与历史输入追因（2026-10-09）

00:39—00:55 两端实际 current、独立源数据库与公式现场只读核验；两端平台 release 均为
`1073d68aa589d4278320ff0d43661403f2a1c8bb`。19 个有值分歧和四个日频缺口的 16 个原始依赖
共 61,759 行业务字段相同，现行源脚本、公式与相关元数据一致。两个 Mac 实际解释器复算 23 键
及诊断场景全部精确到 DECIMAL 12 位一致，真实只读 MySQL 49/49 查询逐行一致，独立事件回放
和手算通过。该范围不代表全库验收，未恢复每次历史代码或完整历史输入摘要。

按各自生成时留存 outbox 重建输入，Mac 19/19、ECS 19/19 有值历史精确复现。在 Mac 当时输入
仅加入原 247 清单内、同时间 ECS 已有的缺键，19/19 输出变化，16/19 精确变为旧 ECS。其余三项
均在 8/26：DRS00002 另缺 K7780920/8/26；90000018 另缺 M0329501/M0329505 的 8/26；
90000012 另缺 M1001654 的 8/25、8/26。加入这些同时间缺键后也精确复现旧 ECS。
此前今天完整输入移除 247 的试验不能排除其历史因果；后来观测到齐会改变实际观测滞后截止，
周频从 Mac 两日、ECS 三日变为当前四日。90000018 还涉及 M0329501/8/26 在 8/27 晚间由
-9.6457 修订为 -9.6396，精确解释旧 ECS 与当前复算的 0.0061 差额。证据主要指向源覆盖、
采集时点和源值版本，未指向 OS 或算法不一致。

四个 DR001 相关日频缺口（DT000001、DS000001、DS000002、FS000001，8/26）当前原式复算
精确匹配 ECS，可形成 Mac 四键补齐候选，且与 247 键无直接依赖。Mac 的 M1006336/8/25 在
8/26 17:12:23 被删除，8/27 11:24:37 才重入；8/26 观测首次 11:25:07 入库，晚于衍生批次。
当时 M1006336 最新日为 8/24、S0059749 为 8/26，原式日期对齐无有限输出；批次首尾恢复均
支持跳过机制，但缺行本身不能证明当时该公式实际调用成功。

英国周 202633 的 HWM00007、HWW00003 历史 ECS 值已精确复现：lag=2 时 ECS 截止 8/24，
Mac 缺 8/24 故截止 8/21，当周为空。当前完整历史截止 8/26，复算分别为 4.537066666667、
-0.007622856690，不同于旧 ECS 的 4.569100000000、0.000138597394。恢复当时覆盖与重算
完整历史是不同口径。日本 P0602235/8/25 当前 Wind 单键只读返回 2.893，支持 Mac 源库、9/22
生产冻结包和 9/24 导出；ECS 2.894 为留存旧值，不将其外推为当前正确源值。

本轮零生产写入、零主动发布。原清单源差异仍为 31 项（19 个上述衍生有值、5 个末位精度分歧、
四个日频与两个周频缺口、1 个日本原始值）；源修订、正常 generation/ready 和历史预测分开验收。
见[核查报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/remaining-31-investigation.html)、
[逐键验收](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/remaining-31-followup/recompute/summary.json)。

### Mac 四个日频衍生缺口补齐（2026-10-09）

按用户明确授权，01:12:25 在 Mac 生产源库单事务仅 INSERT 2026-08-26 的 DT000001、
DS000001、DS000002、FS000001 四个缺失键。01:12:44 新连接独立读回 4/4 通过；本机生成新
主键，名称沿用本机目录，target_date 保持日频源默认空值。核验范围内 10,749 条既有日/周/月
衍生行全字段不变，7,124 行相关原始输入、元数据和两类日历摘要未变。八个本地 PENDING INSERT
事件逐条匹配；两条临时 INSERT 的提交前回滚演练确认目标行与事件无残留。

提交前新捕获当前原式与输入，Mac 两个实际解释器四值精确到 DECIMAL 12 位一致，真实 MySQL
唯一取数 SQL 逐行一致；独立脚本审查通过。维护窗口内源 Writer 和 running run 均为零，源 cron
及 installed/loaded 时钟不变，Backend cwd 对应实际 current；未修改生产代码、调度或服务。

01:12 后双端独立只读重查原 1,805 位置对应源键，剩余 31→27：24 个衍生有值差异（19 个
已解释历史覆盖/源版本差异、5 个末位精度差异）、两个英国周频缺口、1 个日本原始版本差异。
Mac 另 1,799 条原清单追踪既有行全字段未变，ECS 全部追踪行未变，两端五 CSV 及 manifest
摘要未变；发布输入仍有原 1,805 项差异。本次零 ECS 写入、零主动发布、零预测或回测改写。

用户本轮只要求补四个日频缺口，其余差异保留。可按已解释历史版本及源值数值容差保留，但
不外推无信号影响：本机现行 active 方案中，英国周频及部分剩余衍生因子有明确输入引用，
实际成功 run 输入 snapshot 中两个英国周频位置仍为空；静态列引用不证明模型最终采用或
改变方向，无显式引用也不证明未使用。发布新鲜度、未来真实运行及对外展示仍须独立验收。
见[四键完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/dr001-four-repair.html)、
[完成回执](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/dr001-four-repair/completed-summary.json)。

### 英国周频缺口复查与精度差异忽略（2026-10-09）

用户后续明确忽略五个既有末位精度差异，不再列为修复项，原数值保持。按 01:12 完整清单，
实际源差异仍为 27 项，其中 5 项忽略、22 项保留；本轮没有再次修改源值或全量重扫。
09:21 两端独立 READ ONLY 快照确认 Mac 的 HWM00007、HWW00003 / 周 202633 仍缺行，
ECS 两条旧值仍在，相关原始覆盖和公式／lag 配置一致。Mac 的 G0006352、G0006353 / 8/24
已于 10/8 20:16:46 随 247 条源补数进入生产库，当前原式完整历史可算，值分别为
4.537066666667、-0.007622856690。

此前这两源键有成功 Wind 返回，但 Mac 保存日志的删除尝试下一行分别报 insertDateList.py
第 27 行 U+3002 语法错误；没有此前成功插入的留存事件，不声称它们曾有行且被成功删除。
历史周频批次因缺 8/24 使 lag=2 截止退至 8/21，当周为空；源补齐不触发旧周自动重算，
此前 528／85 只 UPDATE 既有衍生行，四键 INSERT 仅为日频，英国两周键未在已提交修复范围。
本轮仅解释和记录忽略决定，零生产写入。见[英国缺口复查](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/uk-gap-followup/summary.json)。

### Mac 两个英国周频缺口补齐（2026-10-09）

按用户后续明确授权，09:44:17 在 Mac 生产源库单事务仅 INSERT HWM00007、HWW00003，
业务日期 2026-08-28、周 202633，采用完整原始输入的现行原式复算值 4.537066666667、
-0.007622856690。09:44:31 独立新连接读回 2/2 通过；本机主键 995633、995634，名称取本机
目录，target_date 保持空。核验范围内 10,943 条既有日/周/月衍生行全字段不变，8,456 行
原始依赖、因子配置、源日历及交易日历摘要不变。四个本地 PENDING INSERT 事件逐条验收。

两个实际解释器复算至 DECIMAL 12 位一致，四条真实 MySQL 查询逐行对照通过；只对查询
审计中的 week_id 字符串/整数表示作归一化。独立审查无阻断项，提交前两条临时 INSERT
主动回滚验证行与事件无残留。核验实际源 cron、installed/loaded 控制面、维护空窗与零在途
Writer/run；没有修改 ECS、生产代码、服务、调度、已发布预测或回测历史。

补后两端新快照重新核对原 1,805 位置对应源键：Mac 缺行 2→0；其余 1,803 条 Mac 追踪
既有行、ECS 全部追踪行不变。两项新值与 ECS 历史单日覆盖旧值不同，因此实际源数值差异
仍为 27 项（其他衍生历史版本 19、忽略精度 5、英国覆盖版本 2、日本原始版本 1），不能写为
27→25 或全库一致。五项精度保持用户已定忽略范围。

09:43—09:44 发布摘要读回显示两端 current 已是 10/9 06:31/06:33 generation；本次源修复前后
五 CSV/manifest 摘要不变，未主动发布。当前 CSV 差异数量、ready 与真实未来方案/前端
仍须独立验收，不继续引用 01:12 的旧 generation 状态为当前事实。
见[英国两键完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/uk-weekly-repair.html)、
[完成回执](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/uk-weekly-repair/completed-summary.json)。
精确补前基线、执行摘要、事件及提交尝试/提交/独立读回证据保留在本批目录；已提交后不重复
执行补齐，恢复须核对精确键及引用，不以整库快照覆盖持续增长的事实。

### 当前两端输入与生产冻结包对账（2026-10-09）

按用户指定 `生产九方案当前信号复现包_20260922/data/` 重新只读核验，五份 CSV 均匹配
snapshot manifest 和原 ZIP 摘要。参考日频截止 9/21、周频至 202637、月频至 202610。
09:54 两端独立 READ ONLY 快照按现行同一 release 的既有导出规则形成输入视图：对参考包
Mac 差异 981（日 584、周 353、月 44），ECS 1,001（日 598、周 359、月 44）；两端各有
542 个参考为空、当前有值位置，参考非空转当前空为零，参考业务行和因子列均未丢失，
重叠数据日历映射与 1,474 行目录一致。该核验是平台输入视图，不是全部底层源表逐记录审计。

Mac 981 项已逐键归因：247 原始补数、528 修订中的 309 个冻结覆盖差异、85 修订、四个
DR001 补缺和英国两键，共 647 项精确匹配已完成修复；另 270 项为冻结最后周空位已有覆盖，
20 项为此前已有冻结源值版本差异，44 项为最后月桶更新。后两组周／日旧差异值均与此前
捕获一致；月频差异全部在 202610，当前两端一致，仅抽样追溯源更新，不声称全量历史事件复原。

实际发布对参考：Mac 979、ECS 1,001。当前两端五份完整发布及库视图之间各有 29 个差异
（日 21、周 8），包含五项已忽略精度；其余 24 项保留。此前 27 项仅追踪原 1,805 位置，
当前全文件扫描另纳入两个已知 8/25 的 90000012／90000019 因果修订，不是新增未知故障。
Mac 发布仅与库视图的英国两项不同，因 09:44 补齐晚于 06:31 发布；ECS 五份发布与库视图
字节摘要一致。10:05 独立核验两端 10/9 generation／CSV 摘要匹配 manifest，ready gate
匹配 generation／business digest，日频截止均为 10/8。此前 1,805 发布差异是旧批次事实。

共同历史问题仍在：平台日历 9/30 为交易日，两端三列 TB 目标源当日均无行，日频轴也缺
9/30。10/8 水位推进与 ready 不证明这个旧缺口已补，9/21 截止的冻结包无法验证该日期。
本轮零生产修改、未触发发布或算法，不作为预测、回测、前端或域名迁移验收。
见[当前生产参考对账报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/current-production-comparison.html)、
[逐格清单](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/current-production-comparison/difference-details.csv)。

### 底层源表全历史对账（2026-10-11）

用户要求只看 ECS 与 Mac3 底层数据。16:09 两端独立 READ ONLY 快照全量核对六张日／周／月
原始与衍生表、因子配置、数据日历和交易日历，共九表；排除本机 ID 与入库时间，不做发布投影。
全历史差异 2,045 个业务键：Mac 单边缺 1,894、ECS 单边缺 4、双方数值不同 147；没有仅其他
业务字段不同的差异。其中 1,830 项为 ECS 独有的 TSTYIW01 原始 917 行及 TSTYIWDI 衍生
913 行，两端目录名称均标 TEST、status=1、display_flag=0，单列不等于自动接受或删除。
非 TEST 为 215 项（日频原始 96、日频衍生 109、周频衍生 10）；包含 Mac 缺 64、ECS 缺 4、
数值不同 147，扣除用户既定五个精度键为 210。另八个末位差异仍计数，不扩大忽略范围。
月频两表、因子配置及两张日历完全相同。16:14 两端新连接复核九表行数、215 个非 TEST 键
及 TEST 数量通过；清单不能全部视为尚待修复，未逐项还原新增统计项的生成原因。

247 原始补数键、四个 DR001 衍生补缺键及 TB3YWI1C／TB7YWI1C 两列源值本次无两端差异。
日频衍生差异中 77 项集中在 9/30—10/6 的 11 个未展示因子。9/30 三列 TB 共同缺口仍在；
周频原始两端各 32 个相同重复键，其中各 14 个键同端重复值不同，作为共同问题单列，不计入
两端差异。此前 29 是 10/9 输入投影口径，不能当作本次底层全历史总数。零生产修改。
见[底层对账报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/source-database-diff-20261011.html)、
[215 项非 TEST 清单](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/source-diff-20261011/non-test-differences.csv)。

16:27 快照、16:31 独立连接复查进一步确认 TEST 来源为 DataBridge 导入维护测试，metadata
注明可删除；Mac 在 8/18 11:03:03 已删除原始 917／衍生 913 行，ECS 仍保留。当前数据库运行、
方案与输入引用及部署文件检索未发现依赖；两端配置仍 status=1，建议精确清理时同时停用，
不能只删历史行而保留可被上游任务选中的测试配置。ECS 全字段备份已生成，尚未删除或停用。

68 个缺行均已逐键定位日志原因：美股联合爬虫失败 40（Mac 36、ECS 4），Mac DR007 18
（网络关闭 6、页面等待超时 6、代理 2、本机 MySQL 拒绝连接 2、页面元素失效 1、Chrome
启动失败 1），欧洲指数日期解析失败 4，Wind 响应后旧 U+3002 语法错误阻止插入 6。
Wind 六项中只有两项有实际删旧事件，其他四项不能宣称原值被删除；68 与已修 247 键交集为零。
五个当前采集文件两端摘要相同，不能将外部响应与环境故障都归为脚本版本或同步故障。
八个缺口键在周末，指数取最新报价但按前一中国工作日落库，且 9/24 键被后续多天重复改写；
另一端有行不等于该历史日期和值已验证，补数前须核实观测日期。全部仅只读归因，仍为 68 缺行。
见[TEST 与 68 缺口报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/test-and-68-followup/test-and-68-followup.html)。

用户随后精确授权 TEST 清理。16:41—16:42 已完成提交及独立读回：ECS 删除 TSTYIW01 原始
917 行、TSTYIWDI 衍生 913 行；两端各两条配置仅 status 1→0，定义保留。双端 runtime/operator
保存精确备份并有本机第二副本，SHA-256 一致；事务内恢复全字段试验、回滚读回通过。
非 TEST 周频全部字段、其他 metadata 未变，旧 outbox 保留，ECS 新增 3,660 条精确 DELETE
审计事件。重新九表全历史对账 2,045→215，非 TEST 215 项逐项与清理前一致；五项精度忽略后
210，源缺口仍 68。预测／回测记录数与操作前一致；未重跑、覆盖、改算法或服务调度。

现行爬虫因子输入审查：PC001672、PC001677、PC5EDF0C、PC001673 和 DR007001—DR007013
共 17 项均 pre_forecast_flag=0，后 13 项亦 status=0；694 条存量公开衍生公式未发现这些因子的
显式／前缀依赖或无指标限定的日频全表查询。两端当前三频发布、factor_catalog 和 Native 冻结
schema 均无这些代码。ECS 97 active base／101 任务均 Blackbox，当前供给排除这 62 个爬虫
缺口的数值影响；87 个 base 最近成功运行的可读 snapshot 也无这些列，另 10 个旧季度／年度
run 的物理 snapshot 未找到，不外推所有历史。Mac 97 Blackbox 当前供给同样排除，专属九套
Native 先调用不可读编译运行包，其平台审计输入不能代替内部取数验证；本轮未反编译或执行。
正式 Wind DR007（M1006337）及相关衍生因子仍是有效预测输入，不与这 13 个分时指标混同。
两端 current 文件摘要和 ready generation／business digest 匹配。其余数据、历史结果和域名
迁移另行验收；见[TEST 清理与预测影响报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/test-cleanup-20261011/test-cleanup-and-prediction-impact.html)。

用户随后接受忽略上述 62 个爬虫缺口，本轮不继续补缺，保留现有记录与核查证据；忽略不改变
两库实际 215 项差异。剩余六个 Wind 原始缺口均为 Mac3 缺、ECS 有：观测日 2026-08-24 一项、
2026-08-25 五项；对应采集故障发生于 8/26—8/27，不能把任务执行日当作源数据观测日。

### 源修复后的预测差异复审（2026-10-11）

16:58—17:00 双机新鲜只读事务及现场 cwd/实例核验完成。共同 97 base／101 业务任务的产品
事实同键 23,361 条，方向差 78 条（21 base）；10/7 原 76 条仍在，新增 10/9 两条。
78 条 exact 均两端相同，三日期无差；Actual 四表全业务值及覆盖相同。97 base 的 299 个
交付/配置/Metadata/配套文件逐字节相同，不据 OS 名称归因。

18 条回测在 8/23 已生成、创建/更新时间均为当日，不能全部归因后来的 8/26—27 语法故障；
两条 3Y 旧样本另有后续持久化变化证据，两条 8/26 same-snapshot 未决，52 条输入身份不同、
四条旧 exact 无 snapshot。实际冻件逐文件校验后对账：9/30 为 1,805 格，10/9 为 29 格
（21 日频＋8 周频），10/10 及当前为 30 格（新增 SBW21001/10/9 末位差，不扩大既定忽略）。
原七字段 Request 临时文件及部分早期冻件未找到；DB request_id/三日期相同不能证明三 cutoff
和当次文件/状态一致，也不能证明某一格导致方向变化。本轮未执行算法或覆盖历史事实。

10/9 Mac 七套 Liwei 增量方案、ECS 三套报 source prefix changed／explicit rebuild required。
状态封装完整性通过；Mac 七套停在 9/30，ECS 四套 cons/full 已推进 10/9、三套 AUC/IC 仍在
9/30，明确造成四条最新 Mac 单边缺预测，另三套两端共同缺口。仅只读检查公开封装，未加载
opaque payload、重建状态或补预测；状态维护需本次精确授权，不能自动 fallback 或改历史。
101 任务最新业务键 87 个对齐，其中 85 个方向同、2 个不同；另 14 个最新键不齐为十个月度
及四个新增日频覆盖差。详见[预测差异归因报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/prediction-cause-20261011/prediction-cause-report.html)。

### Mac 当前统一输入的跨机隔离复算（2026-10-11）

用户批准改用当前 Mac 输入作为唯一实验参考。18:06—18:18 双端同 release 的 20 个当前
Blackbox base／74 条已知方向差样本逐条执行标准 predict；完整 Request 由 Mac 权威入口
解析，ECS 不读取其源库构造输入。两个既有输入模式的 manifest、文件、请求、脚本、
Metadata、exact 和 Runtime Profile 摘要核验通过，所有案例 incremental_state=false。

双端 148 条首轮计算全部成功：16 条五字段结果相同，58 条仅 predicted_direction 不同，
涉及 11 个 base；4 条旧 Native exact 未运行。两条 10/9 先导样本各独立执行三次：5Y rule
始终 Mac +1／ECS 0，3Y XGB期限溢价始终双方 -1。相同平台输入仍不能消除全部差异，
不能继续把这次 58 条差归因于 ECS 输入缺失；Mac Accelerate／ECS OpenBLAS 是已观察
到的环境差，具体算法内部机制仍待上游，不能唯一归因 BLAS。

16 条一致结果中 3 条等于原 Mac 方向、13 条等于原 ECS 方向；Mac 本次 15 条方向较存量
变化，ECS 6 条变化。58 条仍不同中 54 条两端分别保持各自旧方向。本次是当前修订数据
的历史截止复算，原七字段请求／vintage 未恢复；不据新结果覆盖原历史事实。两端四表
行数、预测业务摘要、生产 state 全文件 SHA 前后不变，隔离目录保留输出；未写业务库、
重建生产 state、改算法或控制面。原数据库 78 条差仍保留，源库 215 项差亦未修改。
详见[统一 Mac 输入实际复算报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-reference-reproduction-20261011/mac-reference-reproduction.html)。


### 全区间同输入运行核验（2026-10-11—12，执行收口）

按用户授权，11 个仍有差异的当前 Blackbox exact 使用共同 Mac 冻结输入、相同完整
Request 和同一 release，隔离重新生成至 9/29 的回测及 9/30、10/8、10/9 预测。
4,372 条历史已双端完成：3,956 同、416 仅方向不同（43 相反非零、373 中性与方向）；
33 个后续请求为 19 同、3 方向不同、11 两端共同失败。Liwei 完整 418 条为 381 同、37
不同，Mac 4,415.104 秒、ECS 12,581.175 秒成功；本次耗时不是同硬件／负载容量基准。
十项全区间统一解码控制的 3,954 历史仍保留 379 方向差，48 个已观察返回表分组摘要均
一致；各主机 3,974 个有效历史／后续结果相对标准回放均未变。11 方案代表单线程环境
控制双端均成功，各自结果均未变，跨机仍 11 个不同；不能外推为全部内部线程已统一。
两项 T+1 沿用当前已入库成功回测起点 2025-07-01，其余九项为 2025-01-02；未新增同机
批量／单条结果控制比较。本轮是当前 snapshot 按截止键重放，不是原历史 vintage 复原。

十项交付拒绝超过 100 条完整请求，与上游全区间单进程合同冲突；本轮以相同 48 分组
取得区间覆盖，不把它视为完整批次能力通过。共同输入缺 9/30 日频行，10/8 请求失败
单列而非补平。已测解码、线程环境、旧 OpenMP 与 NumPy 分派控制未消除对应方向差；
版本号一致而数值构建／实际绑定不同属于线索，首次内部分歧与唯一库因果仍需上游。
公开 IO 观察未见额外业务源路径，Mac native C 层未完整覆盖。最终 151 项冻结文件跨机
SHA 相同；Mac 4,672／ECS 4,678 项已记录运行文件未变。24 阶段四项保全检查均通过：
数据库身份、四表行数、预测八字段业务摘要、生产私有 state；不等于整库逐字段核验。
本轮未调用业务写入 repository，算法结果未写生产库，未改变发布、调度或控制面。
[最终 HTML 与逐条证据](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/full-chain-runtime-audit-20261011/full-chain-runtime-audit.html)记录标准输出和失败；
执行收口不等于唯一内部根因定位或公网迁移验收通过，也不是原 78 条历史差异逐条归因。
[同输入核查阅读版](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/full-chain-runtime-audit-20261011/ecs-mac-comparison-brief.html)整理固定条件、交付一致性边界、原因证据与按预测日期的月度/方案分布；
绑定上述封存统计，不新增算法执行，原封存文件保持不变。

2026-10-12 两项 LGBM 专项内部取证已定位特征计算与筛选分歧：5Y exact `5cbbf665e081`
及 7Y exact `4f182bf23dfc` 的 188 条历史方向差异和 7Y 9/30 一条后续预测差异，均可通过
固定 Mac 选列复现。194 个历史请求（含 6 个同向对照）读取后的输入帧、标签、信号与
日期轴相同；278 次实际模型调用的历史、y、权重及参数相同。三个实际矩阵 fixture
跨机公开组件重放 834/834 次概率及 `tree_info` 摘要相同；ECS 特征改用捕获的 Mac
选列，278 原方向及 194 B8 最终方向全部恢复 Mac。仍有 12 次 5Y 概率残余，最大
绝对差 0.0165497261，本批未改变方向，不能称全部数值相同或仅末位差异。
同输入／参数／Bottleneck 分支的 32 对滚动标准差调用输出全部存在差异，零与极小非零
经符号、`sd==0` 和前向填充改变实际 Sharpe 得分；同分默认 quicksort 在固定相同分数
向量时也造成选列分歧。各端自身分数改 stable 仅 50/278 次选列相同，不能把 stable
单独视为已验证修复。该专项为用户授权的明文交付隔离取证，使用非 canonical 外部
观测 bootstrap，标准方向匹配原封存结果；不代替上游正式内部保真责任，不指认未隔离的
唯一 CPU／编译器原因。两端保全检查通过，12:31–12:32 冻结 151 文件及已记录运行依赖
摘要再次复核未变。无业务写库、交付源码修改或服务／调度变更；其余 230 条有效方向差异
尚未据此归因，生产修复尚未部署。
本专项内容已纳入[唯一维护的 ECS–Mac 差异总报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/full-chain-runtime-audit-20261011/ecs-mac-comparison-brief.html#lgbm)；原专项报告及回执保留为封存证据。

2026-10-12 汇报整合按原业务键独立重计：10/7 原 76 条方向差涉及 21 base，产品回测
21／实盘 55；10/11 完整复查保留原 76 并新增两条。原清单的 72 当前 exact 请求在共同
Mac 输入下复算为 57 不同、15 相同，仍差涉及 11 base；另 4 条为旧 Native exact
`6b8d4047dc8a` 未执行，该 base 当前 Blackbox exact `109e657e9a70` 仍 active。
原 LGBM 15 条复算为 14 不同／1 相同：3 条已有与本轮重建 Request 相同的直接控制，
10 条只有新历史 scan 的业务键／特征目标日对应但预测日期不同，1 条仍差尚无该请求
专项干预，1 条本轮相同。不能将新历史 188 条差异当作原 76 逐条因果归因数；原七字段
Request／历史 vintage 仍未完全恢复。本次仅整理已有证据、更新上述总报告，不执行算法
或访问业务库。报告内含 21 方案区间、原 76／新增 2 逐条三日期与方向、全部控制结果及
LGBM 专项；旧阶段 HTML 不再作为并行维护入口，封存证据保持。


2026-10-12 继续对其余九个仍差方案各一个完整请求做双端隔离内部取证，已排除将全部
原因统一归为两项 T5 LGBM 的解释。六项 XGB 分类固定完整矩阵／选列后的 24 对概率
均不同；关闭列采样后六例当前概率和分裂结构一致，但 12 个控制模型仍有 144 处叶权
末位差（最大约 5.6e-9），不等于完整模型或任意输入都一致。其中五项还存在 Sharpe
数值／同分选列分歧；1Y IC 代表请求选列一致，方向差落在训练。5Y 规则与 10Y 集成
实际矩阵、标签、参数相同，三个 LGBM 回归一致、三个 XGB 回归不同；固定矩阵关闭
列采样或保持比例改走等权重抽列路径后，三个回归预测与树一致。列采样分支为直接
控制结论；标准库 shuffle 探针支持更细机制，但未直接捕获真实训练内部抽列序列。

Liwei 9/21 预测（特征 9/18、目标 9/28）IC 特征选择一致，历史模型准确率同分排序
不同。补算两端各缺四个配置，加两个对照共十个固定矩阵评估，跨机概率与方向全部
一致。只交换模型排名来源，四种矩阵×排名组合均复现排名来源一端完整 36 行最终
方向；9/3 的一个 ACCWT 信号差经相同连续方向规则传递到 9/18（计数 10／11），
造成 +1／−1。此代表请求因果链已闭环，不外推全部 37 条历史差异均同因。

18 组完整标准结果证明外部观测没有改变各自结果；Liwei 初次观测失败已保留，缩减
观测量后重试成功。八组含失败阶段的只读守卫通过；151 项冻结文件及各机已记录运行
依赖摘要复核通过，均只代表所记录范围与读取时点。新增组件也经无网络／数据库的
受限 worker 执行，无生产交付修改、业务写入或调度变更。原 76／更新 78／新全区间
419 统计不变，其他九项 230 个有效方向差仅完成代表机制定位，尚未全区间逐条归因。
证据与分方案日期统一维护于[总报告第七步](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/full-chain-runtime-audit-20261011/ecs-mac-comparison-brief.html#remaining)。


## 现场核验入口

- 发布与恢复：[部署运行手册](../deploy/README.md)。
- 调度：installed plist/unit、loaded state、journal/日志、实际进程和本机 run/prediction 联合核验，见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
- 公网故障：[Dashboard 故障定位](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#故障定位与恢复)。
- 日历、DataBridge、Actuals、状态和在途任务在每次操作前重新核验；本文中的数量和摘要不能充当操作时的现场证据。
