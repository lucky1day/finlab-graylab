# 统一后续推进计划

**文档状态**：`CURRENT`

**整理日期**：2026-09-21；以下运营事项仍需各自现场核验，不因文档整理标记完成。

本文只保留尚未闭环的后续事项。当前稳定事实见[当前状态](CURRENT_STATUS.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成事项通过 Git、外置材料、数据库
与目标机 journal 追溯，不在本文维护副本。

## 新 Mac 公网接管后的数据与运行验收

2026-10-18 公网 Backend 和独立隧道切换已完成；见[当前状态](CURRENT_STATUS.md#运行与部署)。仍需单独推进：

- 处理预测缺口：2026-10-18 按正式 active 资格与任务日历完整核查 9/30—10/18，应发 538 条、本机已有 271 条、缺 267 条，涉及 101 base / 105 target。其中 123 条旧公网快照已有（10/15、10/16 各 46 条日频，10/17 31 条周频），另 144 条在该快照也未见；不能把 123 条迁移差额当完整缺口。逐方案/日期/业务键见[完整清单](/Users/macstudio0/bond-factor-lab-production/runtime/reports/prediction-gap-audit-20261018/main/预测缺失完整清单.html)。另 113 项 Actual 本机空而旧公网有值，独立于缺预测。按原 release/exact、输入与事实来源核验后确定补缺或成熟值更新路径，不以当前源数据重算覆盖已发布预测。
- 模型、DataBridge、Actuals 和日/周/月预测调度仍停用。恢复须先核对当前输入 ready、增量状态、唯一 Writer 与触发窗口；网站可访问不表示每日业务已恢复。停用期间的补缺和真实自然运行分别验收。
- 两个新公网 LaunchAgent 属于 `gui/501`；已加空闲防休眠。宿主重启、注销和网络恢复后的持续可用性尚无自然观察证据，不能由单轮 HTTP 验收关闭长期可靠性事项。

## 旧增量方案缓存恢复与信号补齐

2026-09-21 九个组合缓存恢复和 39 条补缺已完成，完成证据见
[当前状态](CURRENT_STATUS.md#旧增量方案缓存恢复与信号补齐)。本节仅保留后续未闭环项：

- 修复后的 Mac3 六套、ECS 三套须在真实后续日频窗口逐主机、逐方案核验 `scheduled_live`、宿主
  触发、本机输入、exact、prediction 与 Dashboard。当前 installed/loaded 的下一日频窗口为
  2026-09-22 07:03 Asia/Shanghai；人工缓存验收与 39 条 `gray_live` 不算自然成功。
- 本次范围的全历史 DataConsistency 仍未通过：旧回测缺持久化样本数依据、旧产品记录 exact 为空，
  以及 2025-01-26 旧目标日期与现行 T+5 日历合同不符。逐机旧 run、数量和例子见
  [维护报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/state-repair-20260921/README.md)。
  需先核对原始证据与迁移合同，单独形成处理方案；本轮未授权历史重写或删除，不伪填字段或放宽 Gate。
- 三套 5Y AUC/IC 的周数据成熟会改变已保存周前缀；本次重建未修改算法规则，不证明长期不再复发。
  算法修订由上游按冷/热等价合同交付后另行正式入库，不把一次重建当作此问题关闭。

## 四套 V3 新方案自然观察与页面验收

- 四套身份、exact 与部署验收见[当前状态](CURRENT_STATUS.md#四套-v3-新方案交付状态)。截至
  2026-09-27 截止检查，严格自然验收为 **6/8**：ECS 日频两项与双机周频四项通过。
  Mac3 日频两项 9 月 21—24 日已有成功 `scheduled_live` run、预测、exact、本机输入和 Dashboard，
  但现存统一日志无法取回对应 `StartCalendarInterval`/宿主启动事件，不能仅凭执行器输出或结束事件关闭。
  9 月 25 日两机权威日历均判为非交易日，没有新的日频应发点。后续须在真实日频触发后、日志尚保留时
  及时取回 `UserEventAgent` 日历事件，并与同次 run、预测和产品事实关联。
- 周频四项首次窗口为 2026-09-26 11:30 Asia/Shanghai，feature 2026-09-24、target 2026-09-30，
  已在两机取得宿主触发、成功 `scheduled_live` run/预测、exact、release、本机输入及 Dashboard 联合证据。
  ECS 10Y/3Y run `6529`/`6534`，Mac3 `5660`/`5665`。`gray_live` 未计入自然成功。
- 八个组合全部取得证据后才关闭自然观察项；当前只剩 Mac3 日频两项。按截止约定，跟进 `v3` 已暂停，
  后续观察需另行安排。不授权重跑、补缺、业务写库、DDL、服务重启或调度/网络/认证配置变更。
  本次证据见[9 月 27 日观察](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/observations/20260927/README.md)。
- Mac3 公网连接可靠性及额外全量 DataConsistency 复核仍待完成：两轮展示对账及一次登录前复核遭遇 TLS EOF，
  本机同一合同与公网入口专项检查全部通过；尚未确定连接断开的根因。
  保留外置失败回执，只读定位可以继续，代理/网络配置修复须另行明确授权。
- 浏览器视觉刷新仍待验收。Computer Use 已因当前 URL 限制终止交互，不重试受限路径；HTTP Gate
  与公网资源检查不代表视觉刷新通过，该待办独立于自然运行观察。

## 1Y T+1 跨期限日内方案自然观察

- 2026-09-17 07:03 Asia/Shanghai 后，只读核验 `one_y_t1_cross_tenor_intraday_v1` 在 ECS 和 Mac3
  分别产生 exact `4f0b95e57fdf` 的真实 `scheduled_live` run 与 2026-09-17 target 预测；同时核对两机
  installed/loaded 控制面、日志、本机 DataBridge generation 和 Dashboard。2026-09-16 完成的 77 条
  `gray_live` 补齐不计作自然运行。
- 观察不授权手工运行算法、补缺、业务写库、服务重启或调度修改。双机全部通过后，将证据更新到
  [当前状态](CURRENT_STATUS.md#1y-t1-跨期限日内方案交付状态)和本机
  [外置证据索引](/Users/macstudio0/bond-factor-lab-production/runtime/releases/one-y-t1-cross-tenor-20260916/README.md)，
  再从本文移除该待办。

## 八套新方案自然观察

- 分别只读观察三套月频 2026-09-15 18:00、五套周频 2026-09-19 11:30 Asia/Shanghai 的首次自然运行。
  逐 ID 范围由[当前状态的八套身份表](CURRENT_STATUS.md#八套新方案交付状态)确定；按[自然运行证据链](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)核对 installed/loaded、日志、真实 `scheduled_live` run、prediction、exact、本机输入和 Dashboard。模拟与灰度补缺不计作自然运行。
- 2026-09-13 记录的“八套新方案双机自然运行观察”只读 follow-up（automation ID `automation`）每日 19:30 检查；实际启停状态以自动化配置读回为准。
  未到窗口或无新变化时保持安静；全部十六个主机/方案通过后暂停，最迟 2026-09-21 报告未通过项后暂停。
  不授权自动重跑、补缺、业务写库、服务/调度修改或 Git 修改。

## M0 周期均值自然观察

1. 15 个新方案继续复用 ECS 已安装的 close-period systemd one-shot/timer；不新增或修改 timer，不伪造日期、
   kickstart 或倒签信号。
2. 权威日历当前给出的下一月均 MID 锚点为 2026-09-15，下一季均 CQ 锚点为 2026-09-30。到期后读回
   journal、run、`scheduled_live`、scheme version、DataBridge generation、Actual 和 Dashboard；全部成功后才
   把对应方案标记为 Production Observed。
3. ECS 当前权威交易日历止于 2026-12-31，尚不能权威推导下一年均 SF 锚点。日历自然扩展后重新计算，不按
   历史节假日或自然年猜测日期。
4. 未完成目标桶继续显示 pending；人工补缺和 Actual 刷新不能代替第 2 项的自然运行证据。

## 其他未闭环队列

1. 只读核验 `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 在 2026-08-24 07:03
   Asia/Shanghai 的首次真实 daily 自然触发结果；在 journal、run、`scheduled_live`、输入 generation 和
   Dashboard 证据完整前，不得标记 Production Observed，也不得 kickstart、覆盖日期或倒签信号。
2. 只读核验 M0 周平均五方案及同期 ECS 周频方案在 2026-08-29 11:30 的首次自然触发证据；
   该时间已过去，不能继续描述为等待未来触发，也不能未经核验标记完成。
3. 公网刷新可靠性的 Actuals/预测窗口观察仍未闭环。需定位[当前状态所记公网 504](CURRENT_STATUS.md#已发布的平台清理与收盘候选修复)的慢请求原因并完成窗口观察；单轮降频复核不代表可靠性问题解决，不重复注入生产故障。
4. Mac3 MySQL 2026-09-24 恢复后的首次真实调度窗口须只读核对宿主触发、数据库稳定性、run/输入与
   Dashboard；恢复服务本身不算自然业务运行。另查 10:36 非正常重启的上游原因，并以原 binlog 和外部
   上游事实核对关键业务水位；`force_recovery=6` 导出的逐表行数一致不单独证明崩溃前全部提交无损。
   原件与恢复边界见[恢复记录](/Users/macstudio0/bond-factor-lab-production/runtime/recovery/mysql-20260924/README.md)。

## 尚缺现场验证的能力

- Blackbox `T+1/h1` target 半开区间批量已有本地候选与回归，尚缺现场验证；
  不据此宣称该路径已生产验收，也不为了验证向生产写入虚构或重复预测。

以上观察和验证队列不阻塞新 Blackbox 方案开始 Intake；入库仍按自身前置条件执行。

## ECS 周频历史补齐后的独立验收

2026-10-08 两列源表历史补齐及数据库读回已完成，见[当前状态](CURRENT_STATUS.md#ecs-两列周频衍生历史补齐2026-10-08)。
尚未主动发布新 DataBridge generation，也未重建或推进算法私有增量状态；后续按现有 producer-ready
合同核对发布输入中的两列覆盖及历史前缀变化，再分别核验未来预测和前端，不能把数据库修复当作全链路一致。
ECS 双 INSERT 触发器及 outbox 遗留 Mac 来源标签仍待上游受控处理，旧同步不得直接恢复；本次
补齐授权不包括触发器 DDL、历史队列重写/删除、整表复制或算法历史重算。

## Mac 日频原始缺口与源刷新完整性

247 个指定源键的 ECS 值已由双机当日 Wind 返回核实；Mac 日期模块历史语法错误及先删后失败的
保存机制已定位，见[当前状态](CURRENT_STATUS.md#mac-日频原始缺口与源值追因2026-10-08)。247 个源缺失键
已按授权从当前 Wind 返回补齐，20:17 独立读回通过；991 条既有行全字段不变，见[完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-daily-247-repair-plan.html)。
剩余：评估并受控修复原采集的事务完整性与写库失败验收，不能把当前模块语法通过或一次精确补数
视为日常源采集稳定性已通过。
23:07 已按后续独立授权修订 528 个原始补数因果证据通过的衍生存量键，独立读回通过，见
[衍生修复完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-derived-repair.html)。
另 116 个受影响复算键（115 个存量值、1 个缺行）的旧值无法由单独移除 247 补数复现，仍待核对
生成时的原始观测版本及历史输入；不得把它们全部归为本次缺数后果直接覆盖。
源层之后分别验证 DataBridge manifest/ready 与未来方案运行；不重写已发布预测/回测历史。
当前发布日频输入文件未变，对应 247 个位置仍空；两次修复均未发布新输入或修改生产代码、调度或服务。

20:37 只读核对后，原 1,805 项对应源键仍有 484 项差异（477 个衍生值、6 个 Mac 衍生缺口、1 个原始值），
见[最新差异报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/remaining-data-differences.html)。
23:08 双端复核已将上述历史源差异降至 116 项（109 个衍生有值差异、6 个缺口、1 个原始值），
具体清单见衍生完成报告；该剩余集合与前述 116 个受影响复算键不同。后续核实 `P0602235` 源日期
8/25 的观测版本、4 个无本次原始依赖的 DR001 日频缺口及 2 个英国周频缺口，以及剩余衍生分歧
的生成输入。两端原清单外有 2 个原本相等但均能由缺数输入复现的 8/25 旧值，Mac 已按原式修正，
ECS 的历史输入和计算时点仍待核对。当前发布文件仍有 1,805 项输入差异；源问题闭环后须按既有
发布与 ready 合同独立验收。不能整表复制或将冻结包空值当作完整真值。

23:30 后续只读核查已将剩余 109 个衍生有值和 6 个缺口全部按当前输入复算分类，见上述完成报告。
85 个 8/26 值当前支持 ECS、Mac 存量不可复现；19 个值不匹配两端历史，5 个仅差第 12 位。
四个 DR001 缺口当前复算支持 ECS且无 247 补数依赖，两个英国周频和日本原始版本继续追溯。
后续须形成不同于已执行 528 键的精确历史维护范围，不把当前复算等价当作已还原历史运行原因。
自动发布任务两端均已加载、每日 06:30；当前收益率日期轴止于 9/29，10/8 发布失败。
10/9 所需截止按平台日历为 10/8，须核验先行采集是否推进三列 TB 源日期及真实新 generation/ready；
当前输入只读构建已复现 freshness 拒绝，不能宣称明天触发后必然成功。

23:43 上游追因已确认 9/30 三列 TB 逐项失败、10/1 补采传输中断及固定当天/昨天取数缺少跨日
补偿机制；10/8 盘后三列仍失败。下一步核验 Wind 原请求，形成两端独立精确源补数及必要衍生
重算范围，再验收正常 generation/ready。85 个 8/26 存值已按用户后续要求于 10/9 00:28 精确
修订完成，独立读回及双端新快照通过；见[85 条完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-derived-85-repair.html)。
原清单源差异已降至 31 项（24 个衍生有值、6 个缺口、1 个原始值）。00:55 后续只读追因已确认：
19 个有值差异的两端 38 个旧值均由生成时输入精确复现；历史仅补 247 缺键影响 19/19，其中
16/19 直接匹配旧 ECS，另三项还涉及其他历史缺观测。一条净买入原始值晚间修订另解释当前复算差额。
四个日频缺口当前支持 ECS；英国两个周频旧 ECS 值有历史覆盖依据，完整历史重算值不同；日本源值
当前 Wind 支持 Mac 2.893。详见[最新核查报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/remaining-31-investigation.html)。
10/9 01:12 已按用户后续授权仅补齐四个 Mac 日频缺口，独立读回通过，原清单源差异 31→27；
见[四键完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/dr001-four-repair.html)。
其余 27 项在本轮保留，不继续修订。保留差异不等于已证无信号影响：英国两项及部分衍生因子
被现行 active 方案显式引用；输入 snapshot、发布新鲜度与未来方案结果仍按既有合同验收。
用户于 10/9 后续明确将五个已核定源值末位精度项忽略，不再列为修复项，保持原数值。
01:12 全量对账实际差异 27 项，其中忽略 5 项、其余 22 项保留；这不是再次修复了 5 条。
09:21 两端英国两键只读复查后，用户明确要求补齐。09:44 已按完整原始输入口径仅新增 Mac
HWM00007、HWW00003 / 2026-08-28 / 周 202633，独立读回 2/2 通过；见
[英国两键完成报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/uk-weekly-repair.html)。
原清单 Mac 缺行 2→0，英国两项现在是完整输入新值与 ECS 单日旧值的覆盖版本差异；实际源差异
仍为 27 项，其中五项精度忽略，其余数值版本保留，不再把英国两项列为缺口待补。
后续若确需维护保留差异，须明确历史覆盖口径与精确旧/新值及恢复范围，不从本批四键授权外推。
09:43—09:44 现场已读到两端 10/9 新 generation；本次插入前后发布摘要未变。后续需重新核验
当前发布输入差异、generation/ready 与未来真实结果，不把 01:12 的原 1,805 项 CSV 差异外推为
当前发布统计；历史原因追溯与数值修正分开验收。

10/9 09:54 全文件只读对账已完成，10:05 发布摘要再读通过：当前两端发布差异为 29 项
（五项已定精度忽略，其余 24 项保留），并非旧批次的 1,805 项。此前 27 项是原差异清单追踪，
本次另包含 8/25 两个已知清单外因果修订。两端今天真实发布均 ready、截止 10/8；Mac 仅有
09:44 补齐的英国两项尚未入 06:31 发布，下一次既有发布后独立读回。对 9/22 生产冻结包的
当前库视图差异为 Mac 981、ECS 1,001，参考非空转空／业务键丢失均为零，详见
[当前生产参考对账报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/current-production-comparison.html)。
9/30 三列 TB 原始缺口和日频轴缺日仍在，两端一致不消除此共同问题；后续仍须获取 Wind 证据，
评估日期轴与衍生影响，按独立精确授权处理。结果／前端／迁移验收仍待进行。

10/11 底层九表全历史只读对账及新连接复核完成：全量差异 2,045，单列 ECS 独有两项 TEST
1,830 后为 215（68 个单边缺行、147 个值不同），扣除既定五个精度键为 210。未作生产修改。
16:31 进一步复查完成：TEST 是已在 Mac 清理、ECS 尚留存的导入维护测试；随后用户精确授权，
16:41—16:42 ECS 的 1,830 行删除及双端四条 TEST 配置停用已提交、独立核验完成。备份、
全字段事务恢复试验及原审计保留通过，定义仍在；重新九表对账非 TEST 215 项逐项未变。
68 缺行逐项故障
原因已定位（美股 40、DR007 18、欧洲指数 4、Wind 语法故障 6），不能再全部列作未归因；
六个 Wind 源键按历史观测日独立核实再申请补缺：2026-08-24 一项、2026-08-25 五项，均为
Mac3 缺、ECS 有。用户随后接受忽略 62 个爬虫缺口，不继续补缺；保留记录和证据，不将忽略
记作已修复。此前八个周末键及指数日期映射疑点随爬虫缺口单列保留。
本轮未补数或改采集器；见[原因与清理范围](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/test-and-68-followup/test-and-68-followup.html)。
已核查其中 62 个爬虫缺口对应的 17 因子：当前不进入 ECS 97 个 Blackbox base 的平台输入，
无已发现的公开公式衍生依赖；正式 Wind DR007 及其有效衍生链另列，不能一并称为未用。
源数据质量可另处理，但不能用这 62 缺口解释现行 ECS 数值信号差异。10 个旧季度／年度运行
的物理 snapshot 未找到，Mac 专属九套 Native 编译包内部取数未核验；当前输入排除不外推
所有历史或全部 Native。其余六项 Wind 缺口、147 个数值差异和共同数据问题仍按原范围评估。
见[清理完成与影响核查](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/test-cleanup-20261011/test-cleanup-and-prediction-impact.html)。
详见[底层对账报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/source-database-diff-20261011.html)。
后续按底层清单区分已接受历史版本、爬虫源覆盖及新增统计范围，不能将全部差异直接覆盖；
9/30 三列 TB 共同缺口和周频原始共同重复键仍为独立数据质量事项。

10/11 源修复后预测只读复审完成：共同同键 23,361，方向差 78（原 76＋10/9 新两条），
18 条 8/23 回测为“全部由 8/24—25 缺口导致”的反例；实际输入 9/30 为 1,805 格，10/9 为
29 格，10/10 及当前为 30 格。共同 97 base 的 299 文件字节相同，不能在输入/Request/状态
未等价时归因 OS。后续优先准备 Mac 七套／ECS 三套增量状态的精确维护方案并另取授权：
10/9 源前缀变化被拒绝，四套 ECS 成功而 Mac 失败，造成四条最新单边缺信号；三套双方失败
亦须单列。状态重建不补历史事实。两条新方向差需完整原 Request、冻结输入和交付方标准
Result 因果对照；旧同 snapshot、后续存量变化及无输入身份案例分别追溯，不能覆盖历史贴值。
见[本轮完整报告](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/prediction-cause-20261011/prediction-cause-report.html)。

用户批准以当前 Mac 输入为唯一参考，替代此前双输入交叉提案。18:18 已完成 20 个当前
Blackbox base／74 个差异样本的双主机标准 predict：16 一致、58 仅方向不同（11 base），
执行失败为零。两条最新样本各三次独立计算保持相同结果；相同输入仍不同的 58 条需上游
跨平台计算定位，不能再一概归因 ECS 源缺失，也不能仅凭 Accelerate／OpenBLAS 差异认定
内部根因。优先保存共同输入／完整请求／exact／双方 Result 的复现证据，由上游核验。
四条旧 Native exact 仅追溯；原请求／vintage 缺失，当前历史截止复算不覆盖已发布事实。
双端生产行数／预测业务摘要／state SHA 前后不变，状态维护与预测补缺仍属另定范围。
见[实际复算结果与逐条证据](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/mac-reference-reproduction-20261011/mac-reference-reproduction.html)。

本轮 11 exact 全区间同输入回放已于 2026-10-12 收口：4,372 历史为 3,956 同、416
仅方向不同；33 后续请求为 19 同、3 方向不同、11 共同输入失败。十项全历史统一解码
控制仍保留 379 方向差；Liwei 完整区间双端成功、381 同／37 不同。11 方案代表单线程
环境控制各主机均未变，跨机仍 11 不同。24 阶段四项保全检查通过，最终冻结输入／请求／
交付和已记录运行文件复核通过；结果未写生产库，不能扩展为整库全字段未变证明。
待上游解决十项 >100 完整批次合同。两项 LGBM 专项内部取证已定位滚动标准差的零边界
及默认同分排序造成的特征／选列分歧，覆盖 188 历史方向差及 7Y 9/30 一条后续差异；
见[唯一维护的差异总报告及修复建议](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/full-chain-runtime-audit-20261011/ecs-mac-comparison-brief.html#lgbm)。
后续由上游明确数值、补值和同分选择语义并交付新 exact；stable 单独不足，12 次概率残余
还需纳入验收。候选在同输入／Request 下核对特征、选列、实际矩阵、概率和最终方向；
新规则的业务认可及未来唯一 Writer 切换须另行授权，不覆盖历史事实。其余 230 条有效
方向差异已新增九个代表请求内部控制，但尚需全区间逐条归因；不能外推两项 LGBM 原因。
八项含 XGB 的代表训练分歧收敛到列采样分支，六分类仍有叶权末位残余；五项还须处理
Sharpe 数值与同分选列。Liwei 代表请求的同分选模型→9/3 信号→9/18 连续修正规则已
由真实补模型、交换排名与信号控制闭环。后续扩展各类控制到剩余差异日期；采样开关
仅作诊断，不作为已认可修复。滚动计算及真实抽列序列的底层实现机制仍有未隔离项。
共同输入缺 9/30 日频行，10/8 预测失败属输入完整性问题，不是跨机方向差。
见[本轮最终统计及证据](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/full-chain-runtime-audit-20261011/full-chain-runtime-audit.html)。

后续统一在上述总报告维护原 76／更新 78／新全区间 419 的不同口径，不再生成并行汇报
HTML。原 76 中的 57 条同输入仍差涉及 11 当前方案，已定位两项 LGBM 特征／筛选机制，
另九项代表请求机制已定位，详见[第七步](/Users/macstudio0/bond-factor-lab/outputs/cross-platform-audit-20261007/full-chain-runtime-audit-20261011/ecs-mac-comparison-brief.html#remaining)，尚需全区间归因。LGBM 原预测 9/23、特征 9/22、目标 9/30 的请求仍差且尚无
该请求专项选列控制；历史 scan 的对应业务键不能替代完整 Request／vintage 因果证明。
15 条本轮同输入相同与四条未运行旧 exact 保留来源证据，不据此改写历史。

## 候选 release 手工启动入口

手工 Harness 的前置条件和现有能力限制见[部署手册](../deploy/README.md#手工-harness-的目标环境绑定)。后续若统一入口，应复用现有可信配置读取能力，在隔离环境验证两机路径、错误身份/覆盖值拒绝和零意外写入；不通过恢复批次脚本或修改生产调度解决。

所有后续工作遵守[根规范](../AGENTS.md)与[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)的授权、停止与恢复边界；发现现场与计划不一致时保留证据，先确认再操作。
