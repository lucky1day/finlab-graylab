# 当前状态

**文档状态**：`CURRENT`

**最近代码与控制面核验**：2026-11-09；下列未注明新日期的业务统计仍为各自历史基线。以下是已核验基线，不替代实时现场检查；数量与摘要不能充当操作时的现场证据。

全局目标、已定决策与阶段进度以 `/Users/macstudio0/最终提示词-20261022.md` 为唯一来源；本文只记录灰度实验室当前的部署、范围与已知限制。固定主机、SSH/转发、生产路径和只读命令见[双机部署与访问入口](operations/DEPLOYMENT_ACCESS.md)。

## 目标与阶段

- **最终目标**：灰度实验室整体迁到 ECS（`aliyun-gray`，`47.103.45.193`），ECS 成为唯一数据源头与唯一生产写入方；Mac 转为只读的算力与验证平台，产出只能经带审计的导入进入 ECS。
- **阶段 D（两端预测一致）基本完成**，待用户确认：共有方案同输入下方向 101/101 相同，其中 76 个逐位相同，其余仅 CPU 架构（Mac arm64 / ECS x86_64）末位差异，逐方案证据见[汇报](/Users/macstudio0/数据对比核查/Mac-ECS一致性攻坚-20261021/html/汇报-一致性攻坚.html)“结果”一节及其架构差异附表。
- **阶段 E（迁移）暂缓**：用户确认阶段 D 后才讨论迁移，此前不做任何迁移动作。
- 迁移判据与 Phase 1 台账见[一致性总纲](architecture/MAC_ECS_PARITY_DRAFT.md)（草案）。Phase 1 的 21 个方案于 2026-10-21 闭环；过程证据已于 2026-10-24 删除，最终报告在 `/Users/macstudio0/数据对比核查/归档-Phase1/`。

## 运行与部署

| 项目 | ECS 灰度 | Mac |
|---|---|---|
| `BFL_DEPLOYMENT_TARGET` | `aliyun-gray` | `mac3-production` |
| current / previous release（2026-11-09三套周频入库后） | `a8cec20d` / `3f174520` | 同左 |
| Dashboard Backend 进程 cwd（2026-11-09三套周频入库后） | `a8cec20d` | 同左 |
| schema | 025 | 025 |
| 调度控制面 | 5 个 systemd timer（DataBridge、日/周/月预测、Actuals） | launchd：同样 5 项 + 公网 Backend 与反向隧道 |
| 部署矩阵配置范围（按期限计，含暂停项） | 150（含1个暂停7Y候选） | 159（150 + 9个W4；含1个暂停7Y候选） |

- `11aaf21a`：新增 lw 5 套日频 T+5 方案（2026-10-24 两端激活并补齐 2026-06-01 起预测，两端逐行一致；三年期方案为已确认的 pandas FMA 末位差，见[一致性总纲](architecture/MAC_ECS_PARITY_DRAFT.md) 6.3）。交付 Metadata 的 owner 为“国债收益率预测项目”/“guozhai”，经用户确认于 2026-10-25 在两端 Registry 更正为 `lw`（业务展示更正，交付两文件与 exact 不变）。
- `4d362931`：预测失败自动重试（日/周/月预测，最多 2 次、间隔 10 分钟、只重跑失败方案；已写入的业务键跳过，批次级失败不重试；2 次后仍失败只写日志、不通知）。`3f77bc2b`：14 个方案交付的确定性修复。
- 2026-11-08框架全面复核发布：ECS先验证、Mac晋级同一份`0b74428c`源码包，SHA256为`83925e030dcb5629e98342413269dc2810cf7aec55e6b54c63ed6d1ec57c2737`，previous为`c1b3cd1c`。修复整组进程清理及批次启动围栏、Native IPC整数校验、周缓存滞后模式身份、审计取消释放、非法FIFO读取阻塞和UI身份/自动退避边界；公共端点、111套方案身份、619个受保护文件及migration SQL保持。两端各1136项公共测试与186项子测试、Mac 53项Node和24项MySQL8.4隔离集成、四项远程CI通过。发布前后ECS 31表151,368行、Mac 57表1,569,085行全字段多重集及保护范围内1732/3176个运行文件相等；输入/ready、模型状态和Dashboard读模型不变；两端认证Summary及各11组Detail覆盖8类任务且相等，ECS五项timer和Mac七项漂移检查通过。公网正向验收与串行复测通过，另有一次中继504，详见已知限制。回执见[全面复核验收](/Users/qiyo3/国债期货报告/outputs/framework_recheck_20261108/验收结果.json)；前轮重复实现/私有转发清理证据保留在[前轮回执](/Users/qiyo3/国债期货报告/outputs/framework_final_cleanup_20261108/验收结果.json)。即时验收不替代新版本未来宿主自然运行样本。
- 2026-11-08 V8六套已按Blackbox V2 SOP在双端激活：3Y/5Y/10Y各一套日频T+5和周频点预测，Metadata、Registry及认证看板owner均为`lw`。两端同包`7943052e`，SHA256为`ffa7fe3c4c80fe5faac0395028769eeb9b7472d9d34706244418ac0e2b41fc3a`，previous为`0b74428c`。原84条跨端方向差异经均匀列采样及日频局部数值运算顺序修订后为0；因子池、学习参数、随机种子、阈值与日期保护保持，旧研究run和引用它们的release保留。
- V8两端各发布1608条已完成target的历史结果及15条已发出日期的待到期gray补缺，1623条业务结果及138条月度指标逐项一致。历史物理来源为`backtest/current_snapshot_as_of_not_historical_vintage`，前端继续按`feature_date=2026-06-01`分区。两端各1136项公共测试/192项子测试、远程53项Node/24项disposable MySQL8.4集成及四项CI通过；原111身份/619受保护文件、业务历史、输入和模型状态保持。两端认证Summary/各12组Detail、静态入口、ECS五timer与Mac七项漂移检查通过；公网一次504后串行复测通过，失败证据保留。原范围585个旧disposable测试资产的另行移除单列记录，其余2591/1732文件未变。本机可读回执见[V8入库验收](/Users/qiyo3/国债期货报告/outputs/v8_onboarding_20261108/当前状态.json)。
- 2026-11-08公网偶发504已定位并修复：旧反向SSH隧道经本机SOCKS代理，Backend已200但中继等待超时；同Backend的直连对照及旧代理复测确认链路差异。现用隧道显式`ProxyCommand=none`，绑定`en1`直连既有中继，Nginx超时及应用代码未改。双端同包`2f438267`，SHA256为`0e9675e234f47e5c114a1fea382e463b8ff66c84e0e38c509263e3f8d4eb03e4`；修复前认证Summary 60次有7次504，修复后同条件60次及绕过客户端HTTP代理的60次均0失败，最慢分别835/1035ms；中继与公网健康各100次0失败。两端各1136项公共测试/192项子测试、远程53项Node/24项disposable MySQL8.4/59项release测试通过；117身份、652受保护源码、Mac 57表1,577,044行/2591文件及ECS 31表159,327行/1732文件保持，两端Summary和各11组Detail相等。公网入口认证Gate、Mac七项配置漂移、ECS五timer及进程cwd回读通过。临时诊断隧道与会话已清理，旧plist及源码包保留可恢复；详细因果对照、请求记录与验收边界见[504闭环报告](/Users/qiyo3/国债期货报告/outputs/public_504_20261108/公网504闭环报告.md)。这次有限窗口验收不代替未来网络可用性保证。
- V8首轮自然运行尚未到期：日频2026-11-09 07:03、周频2026-11-14 11:30（上海）。本次15条`gray_live`不计为`scheduled_live`，自然证据按[待办](TODO.md#v8-首轮自然运行观察)读取。
- 2026-11-08新交付32套5Y日频T+5完成准入核验，用户确认Metadata/Registry/前端owner统一`lw`，并选择先入库通过的23套、9套待修订。双端同包`16429d5d`，SHA256为`7efedd9c172399719c9f1f32d38ebb441601a9ada021c99e2ee74bc27caa5bfb`；两端23套均已激活，各发布10,143条历史结果及92条已应发、尚未到期的`gray_live`，10,235条产品事实及529条月度指标逐项一致。历史物理来源保持`backtest/current_snapshot_as_of_not_historical_vintage`，前端仍按`feature_date=2026-06-01`分区。32套算法脚本原字节不变；旧117身份/652受保护源码、Mac 57表1,577,044行/2591运行文件及ECS 31表159,327行/1732运行文件的保护范围不变。两端各1136项公共测试/215项子测试、53项Node/24项disposable MySQL8.4/59项release合同及四项CI通过；认证Summary各144个target，原121条目不变，各69组Detail（含23组待验证明细）一致，Mac七项配置漂移与ECS五timer读回通过。9套因3个因子缺值导致78个方案×Request不可计算，保持paused/draft、无Registry/版本/产品事实；矩阵配置不等于Writer资格。结果与证据见[本批入库验收](/Users/qiyo3/国债期货报告/outputs/5y_t5_32_onboarding_20261108/验收报告.md)，9套清单见[修订反馈](/Users/qiyo3/国债期货报告/outputs/5y_t5_32_onboarding_20261108/待修订9套/交付修订反馈.md)。23套首次自然窗口为2026-11-09 07:03（上海），不将本次补缺标为自然运行。
- 2026-11-08新增7Y/10Y两套日频T+1：`yield_7y_t1_c2b223e761b00964efc7`、`yield_10y_t1_fcaf9b9391df63ce0fd7`，Metadata、Registry及前端owner均为`lw`。双端同包`c82c2bbe`，SHA256为`f0544ed4ab939638ac7dba5b4146f6cb1a02278c8cba7a3bae5f76ac4eb197c8`；每端完整发布890条历史结果、46条月度指标，日期、方向、Actual和指标逐项一致。T+1本批没有已应发而未到期的gray缺口；最近已到期日2026-11-06的只读规划为`SKIP_PRESENT`，下一自然发出日为2026-11-09 07:03（上海），未提前写入未来信号。两套算法脚本原字节保留，仅按用户授权更正Metadata owner；已有149身份/748受保护源码、Mac 57表1,598,204行/2591文件及ECS 31表180,487行/1732文件保护范围不变。两端各1136项公共测试/217项子测试、Node/MySQL8.4/release四项远程CI、认证Summary/各4组Detail、控制面和旧9套未注册状态通过核验。两端看板各146个target，原144条目不变，前端仍按2026-06-01分区；物理历史保留当前快照回放来源。完整证据见[T+1双方案验收](/Users/qiyo3/国债期货报告/outputs/t1_pair_onboarding_20261108/验收报告.md)。该时点9套仍为paused/draft；其后修订入库结果见2026-11-09记录。
- 2026-11-09，原待修订9套5Y T+5按原始因子前向填充修订完成双端正式回测、同输入一致性、激活、灰度补齐及认证前端回读，原ID保留，Metadata/Registry/前端owner均为`lw`，算法版本`20261108.fullspace.3`，各按新exact登记。两端同包`3f174520`，SHA256 `bed0fb09baca6515742d7797e6e5de25119cd464e65709df2d5462e07c45eab8`。正式`gray_target_start=2026-06-01`：每端发布2997条分界日前历史、1008条`gray_live`及153条月度指标；完整日期、方向、Actual、指标及业务键零差异，每套4条待验证的Actual保持null。最初误用全区间回放已在激活前纠正；原未发布immutable研究证据和ECS 7Y错误区间回放受控取消回执保留，正式产品仅引用正确分界证据。7Y生命周期回退是候选包到发布包的唯一差异，9套四项身份、输入及环境保持，已完成门禁按显式证据复用，未重复回测。其余142身份/727受保护文件、Mac57表1,600,036行/2591文件、ECS31表182,319行/1732文件原保护范围不变；生产源数据、封存输入、schema025、宿主调度和入口网络未改。两端各1136项公共测试/226项子测试，Node53/MySQL8.4独立24/release59及四项源码CI通过；看板各155个target，旧146条目不变，各27组Detail含待验证明细一致。Mac七项漂移、ECS六服务/五timer及实际PID/cwd回读通过。逐项exact及原件/回执见[十套修订验收](/Users/qiyo3/国债期货报告/outputs/ten_revision_onboarding_20261108/验收报告.md)；首次自然窗口2026-11-09 07:03（上海）仍待观察。3套因TB5YWI3C缺失受阻的5Y周频不在本次范围。
- 本批7Y `seven_y_t5_macro_logit_b213_v1`（owner `lc`、算法版本`1.0.1`、exact `9854cfd0a93b`）未通过一致性门禁，未激活、无Registry/version/产品预测；canonical为paused/draft，交付算法和Metadata原字节未改。两端正式333条历史中1条方向不同：feature/predict `2025-10-27`、target `2025-11-03`，Mac -1、ECS +1。原始五CSV及实际float32训练/测试矩阵、标签完全相同；单线程未消除分歧，实际求解迭代/系数不同。只定位到数值求解阶段，未宣称具体CPU/BLAS原因。按根规范停止该方案，数值实现修订待用户确认；原回测和最小复现保留，见[7Y阻断报告](/Users/qiyo3/国债期货报告/outputs/ten_revision_onboarding_20261108/blocked_7y/7Y一致性阻断报告.md)。本批10套中9套完成，不能标为整批闭环。
- 2026-11-09，三套5Y周频修订`3.1.2`完成Blackbox V2双端入库：`weekly_5y_lowcorr03_sop_v3`（lw、exact `87061ec7172d`）、`weekly_5y_vote270f45_sop_v3`（guozhai、`7c738e6aeab4`）、`weekly_5y_vote2808b6_sop_v3`（guozhai、`dfe481234123`），Metadata/Registry/前端owner一致，原交付六文件字节保留。操作前两端均无三个ID的canonical/Registry/version/回测/预测，分别Intake一次。此前TB5YWI3C门槛导致的上游周端点问题由获准修订解决；平台没有填数、改日历或改原模型。两端同包`a8cec20d`，SHA256 `e443d7a907097019e733ca5d9d5d06c76b710cf1c5a388b3283bd7eb591a36d0`，只含本批方案及部署矩阵/审计绑定新增，不含框架清理。正式分界2026-06-01：每套72历史+24gray_live；每端216历史、72灰度、51月指标、288产品事实，日期/方向/Actual/指标及完整键集一致，复核上游全部285个成熟对照无方向变化，每套1条目标11月13日待验证。两端看板158个target、原155条目不变，新增三套完整Summary业务字段及各9组Detail相同，独立本机benchmark编号保留。Mac1136公共/229子测试、Node53及源码CI四项（含MySQL8.4独立24、release59）通过；ECS完成真实算法回测及完整灰度验收，未额外在生产解释器安装测试依赖。757文件/152原身份、Mac57表1,614,235旧行/2591运行文件、ECS31表196,053旧行/1732运行文件的保护范围不变。两端Backend PID/cwd、Mac七项漂移及ECS六服务/五timer通过；独立入库层24项调度、source开关和`52afa0e5`源码提交均未改动，ECS已有三处未跟踪配置资产保持。证据见[三套周频验收](/Users/qiyo3/国债期货报告/outputs/weekly_three_onboarding_20261109/验收报告.md)。首个周频自然窗口2026-11-14 11:30（上海）另待观察；本次补齐不计scheduled_live。用户要求2026-11-10 06:00入库、06:30 DataBridge、07:03正式日频的真实自然验收尚未到期，框架清理改动在该验收成功前禁止发布。
- 产品接口为 `factor-lab-dashboard-v7`（见 [Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)）；输入为五文件 DataBridge，存量 `factor_version` 为 `V1.0`（见 [DataBridge](blackbox_v2/data_bridge_v1/README.md)）。两端各标记 9 个生产方案 target（外置 `production_schemes.json`，规则见 [Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#生产方案标记)）。
- **访问入口**：只用公网域名（中继 Nginx → `18101` 反向隧道 → Mac Backend `8101`，自 2026-10-18 起）或 ECS Dashboard。Mac 本地 `8100` Backend 与 `18100` 回退隧道已于 2026-10-24 退役，原 LaunchAgent 备份在 `~/.config/bond-factor-lab/launchagents-retired-20261024`。切换回执见[公网切换](/Users/macstudio0/bond-factor-lab-production/runtime/releases/public-cutover-20261018-1631/README.md)。
- **数据入库**在独立仓库 `bond-prediction`（2026-11-09两端只读核验 `52afa0e5`；DR007 分时与中债曲线只在中国工作日运行，非工作日记为 `not_workday`）：统一入口 `bin/run_job.py`，Mac crontab 由 `config/jobs.toml` 渲染，ECS 由 24 个 systemd timer 驱动（2026-11-09 installed/loaded读回）。数据问题在入库层修复，不在模型或缓存侧补偿。
- Mac 源表上写 `sync_outbox` 的 36 个触发器已于 2026-10-24 删除（下游消费者自 09-24 起已停）；两端现均无源表触发器。
- 集成分支为 `codex/develop`；框架清理在隔离分支整合`2af297e9`最终态并验证。开发分支提交不代表新的生产 release；发布以目标机 manifest、current/previous 和进程 cwd 为准。

## 方案范围与一致性

- 2026-11-09三套周频入库后，两端看板各158个target，含9个仅在Mac运行的W4历史展示身份。部署矩阵覆盖Mac155个base/159个target、ECS146个base/150个target，含1个未注册的paused/draft 7Y候选。新三套周频均已按本机exact激活；配置范围仍不等同Writer资格，以canonical lifecycle、[部署矩阵](../deploy/scheme_deployment_matrix_v1.json)及Registry/version共同核定。W4不部署ECS、不参与跨端预测比较；旧110/121/144/146/155条目均为带日期历史基线。
- Phase 1：21 个（2026-10-21 闭环）。Phase 2：80 个；73 个无增量方案同输入探针为 55 逐位相同、17 架构末位差异、1 架构差异放大（`weekly_10y_d_overlay_0529`），方向 0 不一致；7 个 liwei 增量方案回测方向一致、两端状态 `preds` 逐位相同，已激活并重建缓存。
- 输入：两端 DataBridge 以五文件 SHA256 与 `business_digest` 判定一致（不以原始表全表哈希为准；实时行情不进入 DataBridge）。
- 历史账本：2026-10-24 以 Mac 为准对齐 ECS 的预测、回测、指标、版本与注册表；两端预测 25,799 行、回测 152 条 / 明细 23,713 行内容哈希相同。历史预测与回测冻结，不重跑、不回填、不覆盖；再次对齐须用户授权。
- 2026-10-26 实盘对照通过：06:36 两端 `business_digest` 相同（`eddae1880ea8`）；07:25 当日预测 Mac 58 / ECS 55，共有 55 条逐字段 0 差异（含 lw 5 套首日），Mac 多出 3 条为 W4 日频，两端无失败 run；继续观察至 10-30。

## 已知限制

- ECS 4 核 / 14 GB，单个重任务（回测、状态重建）已贴近 2 小时上限；目前缓存状态的行身份哈希基于特征原始比特，Mac 状态迁到 ECS 会被判为“全部行已变化”。实测与改造计划见 [TODO](TODO.md#ecs-算力卡点与新验证流程)。
- 旧回测的全历史 DataConsistency Gate 不通过（2026-09-21 记录）：部分旧回测 run 缺 `persisted_prediction_count` 依据、旧产品记录 exact 为空、2025-01-26 旧目标日期与现行 T+5 日历不符。历史冻结，不伪填字段、不放宽 Gate；逐机例子见[维护报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/state-repair-20260921/README.md)。
- ECS 权威交易日历止于 2026-12-31，年均方案的下一 SF 锚点须待日历扩展后计算。

## 保留身份与恢复证据

- W4 Native 与 Blackbox 的保护原则见[根规范](../AGENTS.md)，迁移边界见[算法保真](architecture/SOURCE_ALGORITHM_FIDELITY.md)。
- `t_scheme_predictions.confidence` 与 `t_backtest_predictions.confidence` 已由 migration 025 删除。禁止回滚到仍读写 confidence、依赖已删临时身份或要求旧 Native 接管状态的 release；恢复按[部署手册](../deploy/README.md)核验权限、输入/状态和 Writer。
- 2026-09-17 按用户确认的“保留最新正确结果、不保留旧审计历史”，已删除 live-only 方案 `liwei_0616_5y01_full_oos_k3_div_k10` 的 333 条旧回测（ID `9433`—`9765`）及其 run；live 事实保留。
- 2026-09-24 旧 Mac3 MySQL 非正常重启后以 `innodb_force_recovery=6` 导出重建；逐表行数一致不单独证明崩溃前全部提交无损。见[恢复记录](/Users/macstudio0/bond-factor-lab-production/runtime/recovery/mysql-20260924/README.md)。
- 已完成交付批次的外置证据（只读追溯，不作为操作入口；方案身份与 exact 以当前 Registry 为准）：

| 批次 | 本机证据索引 |
|---|---|
| 五套新方案（2026-09-12） | [README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/new-five-20260912/README.md) |
| 三套 5Y 周频（2026-09-13） | [README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/weekly-5y-lowcorr-20260913/README.md) |
| 1Y T+1 跨期限日内（2026-09-16） | [README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/one-y-t1-cross-tenor-20260916/README.md) |
| 四套 V3（2026-09-20） | [README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/README.md) |

## 现场核验入口

- 发布与恢复：[部署运行手册](../deploy/README.md)。
- 调度：installed plist/unit、loaded state、journal/日志、实际进程和本机 run/prediction 联合核验，见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
- 公网故障：[Dashboard 故障定位](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#故障定位与恢复)。
- 日历、DataBridge、Actuals、状态和在途任务在每次操作前重新核验。
