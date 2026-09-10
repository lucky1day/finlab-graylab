# Native V1 全量迁移至 Blackbox V2

**文档状态**：`CURRENT`

**执行状态**：`W3A_ECS_BATCH_CLOSED; W1_ECS_NINE_TARGETS_ACTIVE_ON_INSTALLED_RELEASE; W2_ECS_BATCH_CLOSED; W3B_FIRST_FIXED_FULL333_AND_INCREMENTAL_STATE_PASSED; W3B_NATIVE_REFERENCE_PASSED_FULL_CANDIDATE_RUNNING; W4_MAC3_PAUSED`

**最新执行读回（2026-09-11 02:21 CST；覆盖下文旧运行状态）**：

- `native(final)` 于02:06:20成功，2926.256324秒（约48分46秒），两个Native结果各333行、精确五字段；
  controller1235659与算法1235698已退出，execution为returncode0、process_group_clean=true，无failure。
  report SHA256 `3a188412a1a30d3bc1c9d3da55c1878cc84ec9205326c23dfad4514905181a27`；
  full原始CSV SHA256 `8296155397cc542c3a8a2b188f91e679a9080274ecc94dee38b740736c2d46d1`；
  k5原始CSV SHA256 `09d8f666aef41e59f10c7e101b64b12328856da136ab312150a48a678441b1f9`。
  三个Native阶段累计10326.568719秒，不作完整两小时通过声明；此后复用原件，不重跑Native。
- 02:20复验三个阶段15/21/28个artifact摘要全部一致；现场五one-shot正常inactive、两run表running0、
  无竞争算法，current仍a6ffe，可用13284MiB、磁盘15GiB。早间next仍06:30 DataBridge、07:03 daily。
- 02:20:57实际派发同一driver `--stage full --finish-before 2026-09-11T05:00:00+08:00`，
  controller1249732、算法PGID1249774；02:21:15确认候选在Blackbox运行环境中执行一次333条backtest，
  stderr已进入Phase A。日志为 `hejFGE/full-controller.log` 与 `full-execution/`。
  尚无full候选成功或五字段等价结论；成功且预算充足才执行k5，不为赶窗口截短预算启动重任务。
- 三个已完成Native阶段原始目录均备份于本地 `outputs/releases/w3b-staged-reference-20260911/`。
  单一重计算、每次7200秒/4GiB、失败保留不重试及凌晨05:00保护不变；未修改生产状态或Mac3。

**最新执行读回（2026-09-11 01:18 CST；覆盖下文旧运行状态）**：

- `master-seven` 于01:01:35成功，2980.613238秒（约49分41秒），完整265配置、575日、2seed；
  controller1223547与算法1223584已退出，execution为returncode0、process_group_clean=true，无failure。
  report SHA256 `31b564634243d46641b76338201e9047614c1392bad4ee119059d98b1f35bb2c`；
  `seven_y-master.npz` SHA256 `0d7d3000d1e4951934ba43f7d3e8e44ca12dc8f676db0178acad4a7ff9a47873`。
  原始阶段目录备份到本地 `outputs/releases/w3b-staged-reference-20260911/`，不纳入Git。
- 01:17两master的15/21个artifact摘要复验通过，累计7400.312395秒；各次7200秒边界通过，
  不将累计耗时宣称为完整两小时通过。fresh现场五one-shot正常inactive、两run表running0，
  无竞争重算法，current仍a6ffe，可用内存13286MiB、磁盘15GiB，早间next仍06:30/07:03。
- 01:17:24已实际派发同包 `--stage native`，controller1235659、算法PGID1235698；01:17:44确认
  算法正在执行 `--stage final --master-ten ... --master-seven ...`，日志为 `hejFGE/native-controller.log`
  与 `native-execution/`。先完成333 Request双族依赖证明，再读取自产master计算selected correction和原final。
  两master不重跑；此阶段尚无两个完整CSV或候选等价结论。成功后才串行进入full/k5候选比较。
- 既有自动推进交接已更新。凌晨05:00截止、单次7200秒/4GiB、单一重计算、失败停止后续不重试不变；
  未做Intake、业务写库、Registry切换、release或timer变更，Mac3不动。

**最新执行读回（2026-09-11 00:12 CST；覆盖下文旧运行状态）**：

- 同一 `hejFGE` 包的 `master-ten` 已于09-10 23:56:25成功完成，耗时4419.699157秒（约73分40秒），
  完整265配置、575日、3seed；controller1205258与算法1205293均退出。
  00:11复验15个artifact摘要全部一致，execution为returncode0、process_group_clean=true，无failure。
  `master-ten-execution/comparison-report.json` SHA256
  `c13c4d1464b22f71c8d15674737c856b331e44160098d1bdf3ff3f9438caa61f`；
  `results/ten_y-master.npz` SHA256
  `7720aa7b82f8e2629fef963d64a8e3b229d49199952150ca3d14994a5d036d3a`。
  原始阶段目录已备份到本地 `outputs/releases/w3b-staged-reference-20260911/`，不纳入Git，不重新计算。
- 00:11 fresh preflight确认current仍a6ffe；daily/weekly/monthly/data-bridge/actuals五个真实unit均
  loaded/inactive/MainPID0/Resultsuccess，两run表running0，无竞争重算法，可用内存13272MiB、磁盘15GiB。
  23:45 Actuals已自然成功结束，墙钟34.064秒、CPU4.680秒；没有停止或修改timer/service。
- 用户要求继续后，00:11:45实际派发同一已审查driver的 `--stage master-seven`，controller1223547、
  算法PGID1223584；00:11:59日志确认进入seven_y master，265配置、575日、2seed。
  日志为 `hejFGE/master-seven-controller.log` 与 `master-seven-execution/stderr.log`。
  该阶段尚未完成，禁止重复派发。既有十分钟自动推进任务已更新为跟进此阶段并启用。
- 三个Native阶段共同09-11 05:00截止、单调用7200秒/4GiB和单一重计算边界不变。
  下一步仅在7Y成功、摘要链一致、无残留与现场预检通过后执行 `native(final)`，再串行完成full/k5比较。
  master成功不等于两个候选五字段等价通过；W3B尚未整批切换。首SAY已通过证据继续复用，Mac3不动。

**用户要求立即推进后的现场执行（2026-09-10 22:42 CST；覆盖下文23:45等待条件）**：

- 现场确认今晚无后续算法预测任务；23:45 Actuals timer仍挂载，不能称不存在任务。
  installed入口为`python -m scheduler.actuals_runner`，只更新Actuals，不训练模型；最近一次30.154秒墙钟、
  3.070秒CPU、83.5MiB，Nice=0。与本次Nice=10、私有冻结文件、不写业务数据库的离线参考不存在业务写入冲突。
  内存可用约13GiB，故取消一律等待这个轻量任务的保守条件；不停止、不修改Actuals timer/service。
- 启动前五one-shot均inactive/MainPID0、两run表只读running0、无其他重算法、磁盘15GiB；current仍a6ffe。
  22:42:36启动`hejFGE/run_family_comparison.py --stage master-ten`，controller1205258、算法PGID1205293。
  `master-ten-execution/started.json`时间22:42:43，stderr已进入ten_y master：265配置、575日、3seed。
  新完整计算确已开始，尚无成功报告；禁止再次启动该阶段。
- 每次7200秒/4GiB及三个Native阶段统一09-11 05:00截止不变。23:45后核验Actuals自然结果，但不再将其
  作为当前长计算的开跑门槛；如出现资源或任务异常，停止启动后续阶段并定位，保持早间任务保护。
  已将既有自动推进任务更新为跟进实际PID/日志与新窗口边界，保持单一计算，成功原件不重跑。

**参考效率优化（2026-09-10 22:17 CST；覆盖下文“仅诊断”的后续动作限制）**：

- 用户已授权继续优化。新临时参考
  `outputs/native-migration-w3b-full-reference-draft/native_reference_v2.py`
  SHA256 `e9140f0941b1f2da6c2b58396ac1f6d14289b2258a6a8711d6c9bda6e9539ea2`：
  保持完整575日、265配置、seed/window和原Native worker，仅将末日correction缩减为实际Phase C消费集合，
  并增加阶段开始、每25配置、完成和失败日志。排名仍使用各Request cutoff标签，早期不足5行仍全grid。
  作者56个baseline纯内存场景及独立reviewer128组原Native Phase C对照均通过；完整前缀ensemble、概率、
  `ml_base`一致，master不变；乱序恢复与失败检查通过，Critical/Important为0。尚无新完整CSV或全量性能结论。
- 已结束执行器探索，不采用spawn草稿v3。ECS同冻结输入、原Native、265配置及原seed下：

  | 有界诊断 | 原四线程 | spawn四进程 | 比较结果 |
  |---|---:|---:|---|
  | 最近1日，两family | 15.803秒 | 20.002秒 | 全部原始pred/prob摘要相同 |
  | 最近5日，两family | 66.551秒 | 67.856秒 | 全部原始pred/prob摘要相同 |

  五日进程组峰值RSS分别510222336与2021437440字节；均在180秒/4GiB监督内完成且组内无残留。
  5日原始证据在ECS `incoming/w3b-five-day-profile-20260910.Q13M3e/{thread,spawn}/diagnostic.json`，
  benchmark SHA256 `a43b75bfee8ab3bc4ed98263fb3ad116d748ade3eac8dd99bcb65e70dd177e9d`。
  直接fork曾被父进程已有数值库线程的检查拒绝，未绕过。spawn没有测得收益、内存更高，不进入正式参考包。
- 同一冻结输入零拟合统计显示，重复训练eligible索引仅ten_y14/575日、seven_y9/575日；
  理论最多减少15900/761875（约2.1%）次seed-fit。尚未实现模型复用，不为该小比例收益修改独立原worker。
- 优化针对一次性独立迁移参考，不是已通过的首SAY每日增量路径。首SAY完整333条及16.88秒增量证据继续复用，
  不重跑；另两successor字节未变。没有Intake、Registry/DB写入、release/current/timer或Mac3变化。
  不能由小样本或correction工作量下降宣称完整7200秒预算已通过；不得原样重启已失败的`5CqPOn`阶段。

**一次性参考的分阶段执行调整（2026-09-10 夜间）**：

- 仅临时参考拆为`master-ten → master-seven → native(final)`，之后两个未完成候选依次`full → k5`。
  两个master分别调用原Native worker，完整265配置/历史日期不减少；final复用本次原Native自产数组，
  不复制首SAY或候选计算结果。已成功阶段不能重新执行，不建立长期平台cache、调度或生命周期框架。
- 每个master只验证完整Request配对/window及最新cutoff的双源shared context；完整333 Request的双族
  依赖证明只在final做一次，必须全部通过后才载入master、执行selected correction和原Final。
  两份master本身不代表逐Request等价验收，最终两个333行CSV才进入候选比较。
- 中间结果是本次私有incoming目录的两个小型NPZ，不是生产状态。固定metadata/configs/dates/preds/probs，
  `preds=int32`、`probs=float64`保留原字节；绑定完整源码、七个输入文件、自身参考代码及family。
  wrapper另外锁定精确driver、运行环境、release和前序成功执行报告链；错身份、错轴、额外字段、pickle、
  软链、损坏或缺少成功执行证据均拒绝，不自动重训或fallback。文件原子insert-only发布。
- 三个Native阶段共用同一总维护截止，并分别受`min(7200, 距截止剩余秒数)`及进程组4GiB上限监督。
  这是按现行“离线每次调用7200秒”组织一次性对照，不是提高单次上限；必须另报阶段及累计耗时，
  不把累计超过两小时标成完整两小时通过。候选完整backtest仍各一次调用、各自7200秒上限，可在后续独立安全窗口消费原件。
- 本次窗口仅在23:45 Actuals正常结束、现场零竞争计算且只读preflight一致后开启；三个Native阶段统一
  `finish-before=2026-09-11T05:00:00+08:00`。不抢在Actuals前启动、不改timer，并保护06:30 DataBridge和07:03 daily。
  每个阶段都须重新核验现场；失败停止后续，不删除原件，不自动重复失败阶段。
- 本地分阶段参考草稿SHA256 `578d60b8e1c1da8630eb472cd701ad6e5e9da12eb766d9aa4796fa6121b677f8`，
  driver SHA256 `b56aa2f4de32888d1a885aa136ba43da636441e743e04a662eadf81f0a6f8572`。
  wrapper独立审查的串行前置和失败记录两个Important已修复；参考NPZ读写/证明顺序亦独立审查通过，
  Critical/Important为0。原始NPZ roundtrip与错误身份/shape/dtype/链接/压缩拒绝测试通过，
  wrapper各阶段成功及失败证据保留检查通过；未添加长期平台回归测试。
- 六文件包已校验并只读封存在ECS
  `/opt/bond-factor-lab/incoming/w3b-staged-reference-20260910.hejFGE/`；参考文件名为`native_reference.py`，
  driver为`run_family_comparison.py`，另有两候选各自原两文件。已使用过去截止时间做无拟合preflight：
  完整release/输入/源码/候选/运行环境只读核验通过，在`budget()`按预期拒绝启动；所有阶段目录仍不存在。
  该拒绝是维护窗口边界测试，不是算法失败或一次已尝试训练。真正执行仍须在23:45 Actuals后重新preflight。
- 已恢复既有`native-ecs`十分钟线程推进任务为`ACTIVE`，提示更新为上述新包与串行顺序；
  旧失败进程提醒及“仅诊断不可优化”的旧限制已移除。任务在安全窗口前不启动长计算，未变化保持静默，
  只在重要结果/失败/需决策时通知。实际运行与完成情况仍以各阶段原始报告为准。

**另两Native参考超时与只读定位（2026-09-10 夜间）**：

- `5CqPOn/native-execution/`于21:06:28失败，实际7200.174846秒，命中完整7200秒预算，
  不是23:30维护窗口裁剪。controller1173565/算法1173600已退出，没有完整或部分Result CSV，
  仅两Request CSV；`post_failure_integrity_error=null`。不重启原阶段、不启动依赖它的full/k5。
- 原stderr仅98字节一条dependency证明日志：333 Request，ten_y修正257行、seven_y修正261行。
  stdout创建19:06:27.583931，唯一stderr最后写入19:13:59.143170；据原文件mtime，初始化/依赖证明约451.56秒，
  其后至超时约6749.18秒。参考在两个master、首Request修正和两个final之后才打印首结果，
  因此不能从现有日志确定超时发生于哪个master或首Request阶段；不能把后续修正直接认定为本次全部耗时。
- 主agent以同一冻结五文件、已锁8源文件执行零拟合capture，并按原worker条件逐grid/date统计：
  两family各575个OOS日（2024-01-02至2026-05-22）、265配置、80特征，全部满足fit条件，无fallback。
  ten_y三seed需要457125次fit，seven_y两seed需要304750次fit；仅master就761875次模型拟合，
  不是333次预测或530次训练。265配置包含256常规与9慢速配置。
- 另做限定诊断：每family取配置0/132/264和首末两个OOS日期，共30次原Native fit，未改参数或生成验收结果。
  10Y的18次fit耗时0.7391秒（run_config总0.8989秒、predict调用0.0154秒），
  7Y的12次fit耗时0.2811秒（总0.2942秒、predict调用0.0100秒）。小样本支持训练为主要开销，
  不据此外推精确全量完成时间，也不作正式性能或等价验收。
- 原Phase C按当月之前可用历史对265配置排名；当前末行不参与该排名。独立只读审查确认当前日
  10Y的STD/ACCWT top10加DIV最多5个，7Y top15，故末行修正每family最多15配置。
  现参考仍对257/261个修正日各重训265配置，若全满足fit条件将额外342645次seed-fit；
  仅按原排名消费集合修正，上界19395次。没有master结果时不能伪造具体入选集合。
  历史master仍为各月排名与连续controller提供依赖，不能只保留今天topK或从2025截断历史。
- 此轮仅定位，未修改参考/候选/平台、未重新运行完整批次、未Intake/切换。后续建议先给必要master
  做有界分段计时，再复用既有按消费集合修正思路；不原样重跑、不靠延长超时或减少seed/窗口绕过问题。

**另两方案Native双参考已启动（2026-09-10 19:06 CST）**：

- Actuals19:00:30自然启动、19:01:00退出0；19:05五one-shot均inactive/MainPID0，
  两run表只读查询running0，无其他手工算法进程，磁盘15GiB，current仍a6ffe、Backend仍1141752。
  下一Actuals23:45、DataBridge次日06:30、daily07:03；未修改timer/service。
- `w3b-family-20260910.5CqPOn/run_family_comparison.py --stage native`已启动，
  controller1173565、算法PGID1173600（外层shell1173564不是算法）；原已审六文件保持不变。
  `native-execution/started.json`及两套Request已生成，实际解释器为forecast_env/python3.13；
  仅一次Native双参考，不重复首SAY，不启动两个候选。单段7200秒/4GiB，维护截止23:30。
  这仍是计算中，尚无Native完成或full/k5等价结论；完成后复用原件，不自动重跑。

**首SAY新状态与增量性能通过（2026-09-10 18:44 CST）**：

- `fZmImh/fixed-state-execution/`于18:31:47三阶段完成：cold_init 5784.759095秒、
  warm_append 16.875757秒、同日stateless 192.952751秒，全部退出0、stdout为空、在各自预算内。
  warm真实新增feature_date 09-08：10Y与7Y都复用650行、仅新增训练1行，总行数650→651；
  warm与stateless五字段Output逐字节一致，方向0，目标09-15。不是同日cache hit。
- 主agent已取回完整11MiB原件至`outputs/releases/w3b-weeklyfix-20260910/fixed-state-execution/`，
  独立复核原Result、执行耗时/退出、NPZ原字节hash与header、cutoff、候选身份、cold→warm状态链和新增行诊断。
  报告SHA `aa0afd1bcedda33340cd5b1adcd4423e46500732ba34ecaa961def716508a80f`；
  cold execution `35aacad2e7e99ecefe0519b692eb3e98f31d20762ba232fd3e1e1f9e9925b3e1`，
  warm execution `fc4799c4a785e28207e2747444b151670d6f4dcf76bd20d4e061155209953d2c`，
  stateless execution `d059d812ecba21ac2b7aca8a6ee8d4d82c87b1d4051d81d89cff3061cd316c09`。
- 原controller1148410/算法1148432已退出，不重跑首SAY任何已通过阶段。未发布生产state、Intake或写库；
  首SAY通过不等于W3B整批切换。下一重计算在19:00 Actuals自然完成及fresh preflight后启动另两方案Native双参考。
- 另两scheme的显式state监督器已完成独立审查，发现私有Request解析后取hash的双读窗口；
  已调整为先锁摘要、解析比较、立即复验，修复SHA `b10be92e7e926a6f29efeab0202cc86aefba6d61249d0ad6897f55035a496e62`，
  定点独立复核已通过：稳定输入接受、cold/live读取后替换均拒绝，撤销补丁精确恢复旧稿hash，无其余变化；
  Critical/Important清零。尚无两方案family通过报告，两个批准hash仍None，不上传或启动state阶段。

**另两方案执行包已审并预置（2026-09-10 17:34 CST）**：

- `run_family_comparison.py`独立审查完成，无Critical/Important；服务Python3.12实际纯内存验证三阶段路由、
  333条Request仅替换base ID、Native原件复用拒绝条件与实际forecast_env身份。脚本SHA
  `416c6a9b665d3a6512fae18b96f134bdf682072f47a043152a8412c3f6a85db7`，原独立参考7c50及两候选字节不变。
- 六个已审文件已上传新独占`/opt/bond-factor-lab/incoming/w3b-family-20260910.5CqPOn/`，
  远端逐文件SHA与本地批准值一致；没有启动Native/full/k5阶段，没有Intake、业务写库或release变更。
  后续显式串行执行一次Native双参考、一次full、一次k5；每段拒绝重跑既有stage，不重复首SAY对照。
- 17:32首SAY新状态controller1148410/算法1148432仍在cold_init，运行40分钟、RSS约963MiB，
  尚无完成或失败报告。继续使用原18:45硬截止，不启动竞争算法；下一轮计算前重新核验Actuals与维护窗口。
- 18:01只读读回：monthly于18:00:30自然启动、18:00:31状态0退出，结果`not_applicable`、
  `refresh_required=false`，与事前判定一致；首SAY仍cold_init，未启动第二算法。

**完整等价通过，衔接新状态验证（2026-09-10 16:53 CST）**：

- 修复candidate完整333于02:02:13通过，实际6770.990417秒；此前22条方向差异全部消除。
  16:50主agent取回全部原始证据，独立确认candidate与Native CSV逐字节相同，均为SHA
  `8e998c224af3891890f5035ae5e59199897608e4e23a6bd61fe951f69d916acf`；行数333、退出0、stdout空，
  未超7200秒。报告SHA `a96b3191868c0d2141d15be6a6cb47a8c23bb4e44c9d1d1b675f39899e84cb4e`，
  execution SHA `ce758a0a2915d7a2150da1b6d265ea7b3160b342659ae06daec3b45e90d571c6`。
  本地原件`outputs/releases/w3b-weeklyfix-20260910/full-comparison-execution/`，该对照不再重跑。
- 已审state监督器只锁上述通过报告hash并规范化末尾空行，无其他差异；最终SHA
  `051c843ea1237122b25eb4048f847bf9cbf4b21e8e24219fd7d1fccc783d90ea`，远端逐字节核验一致。
  controller1148410已启动`fZmImh/fixed-state-execution/`：只做新身份从零初始化、一次真实新增日、
  同日stateless五字段验证，不重跑Native/333，不读取旧NPZ，不发布生产state或写业务事实。
- 启动前五one-shot均inactive/MainPID0、两run表running0。为减少无意义等待，按已部署入口的真实
  discovery/calendar判定09-10：monthly候选5、day非15故不due，period候选0且无invalid类型；
  18:00入口没有刷新/算法工作，历史journal也为not_applicable。保留该timer自然触发，维护硬截止设18:45，
  保护19:00 Actuals，不改变任何控制面。每段仍按剩余窗口裁剪7200/120/7200秒，4GiB/8线程。
- 两个尚未验收的W3B full-OOS草稿继续并行做本地执行接线与入库证据复用盘点，不并行启动第二算法。
  三方案整体验收完成前不做半批激活；W3A/W2已闭环事实保持，Mac3/域名不操作。

**修复版首差异通过，完整333对照已启动（2026-09-10 00:10 CST）**：

- Jan2单点于09-09 23:56:56通过，进程耗时281.995447秒，方向0及五字段与保留Native首行完全一致。
  这是离线重算，不是每日warm性能验收；主agent取回原始Output/报告/started/execution并独立对照Native CSV，
  确认stdout为空、退出0、预算及输入身份正确。单点报告SHA
  `a80ab80183404cd1a417237bc531a63aaff9767cb9e452da301a32beb20f2618`，Output SHA
  `f4d171c91fc37bd737a7a0e40ec61d699eb91a62fd99377ba62dd9704e44ab76`，execution SHA
  `d932c3917e16a8e46b38079589cad8017c1eaef0069342188820656981e68262`。
- 已审full监督器仅锁定上述单点报告hash，无其他差异；最终SHA
  `c5ffed5d0c0be57e4fa71f2d3715ab1f0fc477057cdf6c26fd6b127c96da48c2`，上传后远端hash相同。
  再次只读确认五one-shot inactive/MainPID0、两run表running0、current仍a6ffe、单点进程已退出。
- `fZmImh/full-comparison-execution/started.json`于00:09:21生成，controller1111437，
  只执行修复candidate一次333 backtest，复用原Native333，不执行predict/state/Native或业务写入。
  单次7200秒/4GiB/8线程及06:00维护截止保持。此时尚无全量结果，不能宣称22条差异全部消除。

**修复版首差异单点已启动（2026-09-09 23:53 CST）**：

- Actuals自然23:45:24启动、23:45:54状态0退出；23:51五one-shot均inactive/MainPID0，
  两张run表只读查询running均0，Backend仍PID643451、current仍a6ffe。
  下次DataBridge06:30、daily07:03、Actuals08:30，未变更任何service/timer。
- 已审`fZmImh/run_first_difference.py`于23:52:12生成started证据并开始唯一修复candidate
  Jan2单点验证；controller1109669、算法PGID1109691。绑定原五文件、Request及Native333，
  无Native重训、状态推进或业务写入。维护硬截止09-10 06:00，单算法仍7200秒/4GiB/8线程。
- 后续`run_fixed_full_comparison.py`独立审查完成；唯一started证据读取竞态已按同bytes解析/hash
  修复并复核，SHA `813eaebe2520e5e55e5297cf8e65836a73b7479eaab04efcc08f2b9a24ff29f0`。
  当前单点报告批准SHA仍为None，故不能启动333；须真实单点通过后再锁定，不自动放行。

**周投影修复审查通过，单点验证包就绪（2026-09-09 23:25 CST）**：

- 新两文件Python SHA `93798b4183fab8e02f974c72a4cf7602df9b410227fbc909608c74fe6ba1e9ce`，
  Metadata SHA `ceba9f3a222decadafcdc7375aba94f4e160fe84c20ffd4ae9028fa42bf4fe49`。
  独立审查确认只删除错误周末过滤及传递，保留算法参数、逐Request修正与状态后缀重算；
  19个实际函数前缀/override样本、8个状态后缀样本通过，无Critical/Important。未执行模型拟合。
- 一次性`run_first_difference.py`只运行修复版首差异Jan2单点，无Native重训/333/状态调用。
  审查发现的退出后耗时检查缺失已补回，实际预算边界复核通过；候选hash已锁定，最终脚本SHA
  `6567323a66b6e8b63a03c7a305a8409e348e24330660ecbf8e71c088d68840be`，独立复核无遗留C/I。
- 三文件已上传新独占`/opt/bond-factor-lab/incoming/w3b-weeklyfix-20260909.fZmImh/`，
  远端逐文件SHA与已审原件相同。只是离线验证包，不是release/Intake/激活，尚未启动算法。
- 23:23现场只读确认current仍a6ffe、前轮三个验证进程均已退出、磁盘可用15GiB；
  Actuals.service为inactive/MainPID0/上次状态0，timer仍active、下次23:45。
  等其自然结束后重新preflight再启动，维护硬截止次日06:00，保护06:30 DataBridge及07:03 daily。
  单点通过后只重算修复candidate完整333并对照已保留Native；新身份状态/性能仍待后续验收。

**冻结输入证实周投影错误，最小修复准备（2026-09-09 23:00 CST）**：

- 主agent只读提取原Native/candidate的真实alignment函数，在原五文件上执行零拟合比较：
  feature2025-01-02共3507个daily行、10个周因子；Native有效单元22789，candidate基础投影有效单元0，
  其中22779个差异位于历史行。日历包含周日（202451末日12-29、202501末日01-05），而实际daily
  每周末行通常为周五。candidate要求两者日期相等，错误丢弃了历史周数据；逐Request末行override不能补回历史。
- 最小修复恢复Native“传入daily的每周最后一行”语义，删除`week_end_by_id`过滤/参数及两处calendar
  最大日期构造；保留原周ID映射、逐Request末行override和状态特征后缀重算。不增加另一份未来daily
  周末映射或封周机制，不改变模型参数/训练/最终控制器。改动仅在新的ignored weeklyfix草稿，原件不覆盖。
- 同一冻结输入的修复替身零拟合验证：Jan2 Request所在batch截止Jan17，恢复源alignment后，
  batch前3507行20个周特征加该Request单行override，与原Native Jan2完整周特征矩阵逐值一致。
  这已证明输入构造缺陷及其定点修复，不代表所有22个方向差异已解释或完整算法等价通过。
- 候选脚本身份与训练输入均会改变，旧6db状态不可复用；旧5473秒初始化/17秒追加/163秒无状态及
  6538秒完整回测只保留为旧错误字节记录，不能给修复版本验收。必要的新版本初始化/追加验证与重复旧版
  验证有别；不手改state header。独立Native333原件继续有效，无需重算。
- 后续顺序：新两文件最小差异独立审查→首差异Request的输入/结果定点验证→修复版一次完整333对照，
  仍比较已保留Native原件。确认等价后才进行修复版本的状态/性能验收；23:45 Actuals及Mac3不操作。

**完整333条出现22处方向差异，停止本批（2026-09-09 22:36 CST）**：

- 两个算法进程均正常退出：Native5735.966928秒、successor6537.703946秒，均低于7200秒，
  4GiB监督未触发。wrapper于22:27:36.902在最终五字段比较时失败，完整性复核无异常；
  这次不是超时或输入代际不匹配，也不能解释为迁移已通过。
- 主agent独立读取两份原始CSV：各333行，Request ID/顺序及三个日期全部一致，22行仅方向不同。
  首差异feature2025-01-02：Native0、successor1；其余分布于2025年1/4/5/6/7/11月，
  不以调参贴结果，不重算已完整留存的Native参考。
- successor.csv SHA `0ff86b846d7d37f24a706b5ca83088ece6f98fcc2170a903775ff9e625efa8bb`；
  successor-execution SHA `a348ed6c347e3c6279bc5fe9d9ca6400a8234dd09f5ad3184cb9c04e2292c7ef`；
  failure SHA `d6dba75439f1c744994f9686b7f57a7a5d93eb4767d1e9767c5d26c8b8701aab`。
  原件仍在Aj6bv8；本地只读取回`outputs/releases/w3b-remaining-20260909/comparison-failed/`。
- wrapper及successor已退出，未Intake/切换/发布state或业务写入，current仍a6ffe。
  后续先做只读源码与无拟合最小差异定位：区分候选批量路径、独立参考、输入构造与最终控制器，
  不修改运行原件，不自动重跑整套或重开已通过cold/warm/stateless。两个环境的LightGBM、NumPy、
  pandas、scikit-learn、SciPy、Numba、Bottleneck版本读回一致；尚未确定根因。

**Native参考333条完成，新版回测接续（2026-09-09 20:39 CST）**：

- 同一夜间wrapper内Native v2正常退出，监督耗时5735.966928秒，低于7200秒；原监督器4GiB限制未触发。
  原生算法日志完整输出333条，主agent独立只读校验五字段集合、Request顺序/ID及三个日期全部匹配、方向合法。
  `native.csv` SHA `8e998c224af3891890f5035ae5e59199897608e4e23a6bd61fe951f69d916acf`，
  `native-execution.json` SHA `f3e9a9cc6a7b8c315df8b3270139e8899024ab7b69260bac8f599aa957b90156`。
- 原件仍在Aj6bv8目录；本地只读取回`outputs/releases/w3b-remaining-20260909/native-complete/`。
  不再重算此Native参考；该阶段成功尚不代表新旧方向一致。
- wrapper PID1056244已自动串行启动唯一successor333，算法PGID1081519（20:39读回）；
  保留相同冻结输入和候选字节，无state参数。维护截止仍23:30，尚无完整新旧等价结论。

**剩余两阶段夜间启动（2026-09-09 19:03 CST）**：

- Actuals于19:00:30自然启动、19:01:02状态0退出；五个one-shot全部inactive/MainPID0，
  只读数据库确认scheme/backtest running均0。current仍a6ffe、Backend PID643451未变。
  现场Actuals下次为23:45，因此本次维护硬截止收紧为09-09 23:30，未变更任何timer/service。
- 两份已审脚本上传至新独占`/opt/bond-factor-lab/incoming/w3b-remaining-20260909.Aj6bv8/`，
  远端SHA与18:18批准值完全相同。19:02:53启动唯一wrapper，PID1056244；19:03读回已完成
  原输入/Request/三段通过证据及状态身份复核，生成`remaining-execution/started.json`并进入Native阶段。
  后续只串行Native v2→successor333无状态回测，每进程7200秒/4GiB，原监督器再按维护剩余时间裁剪。
- 未重跑cold/warm/stateless，未生成新输入或推进生产状态，没有Intake、激活或业务写库。
  原失败incoming与已通过证据未修改；此时尚无完整333条对照结果，不标记W3B批次闭环。

**剩余验证收缩、审查通过与调度读回（2026-09-09 18:18 CST）**：

- 新独立参考草稿`outputs/native-migration-w3b-reference-draft/native_reference_v2.py`已完成，
  SHA `70c226026303323ca9520eecf00b1410a163d34bfe801a68a96adde68bbfda73`，独立审查通过，未上传或拟合。
  所有被任何Request用作prior的日期保留完整265配置；current-only及末行修正仅计算原Native排名真正
  消费的配置，保留整月前缀、原训练/窗口/最终控制器。稀疏消费掩码与真实Native Phase C纯内存对照通过，
  不能据此宣称真实333条等价或性能通过；候选与原参考字节未变，不使用候选结果作旧版真值。
- 新`run_remaining_comparison.py`仅复用原冻结输入、333 Request及三段通过证据，执行Native v2和
  successor无状态完整回测；不重跑初始化/新增日/当日无状态预测。所有新输出将进入独立incoming。
  审查发现的旧pyc加载身份缺口已改为校验源码bytes后直接compile/exec，纯内存helper加载通过；
  修复经独立复核通过，已锁定上述批准参考hash；最终wrapper SHA
  `e79c64d6acced3fa8fcde2b629bb8354c3a78a1bdda4b07936af17ee8ec281ef`。
  reviewer独立执行真实Native Phase C纯内存28组对照、selected correction、缺失单元拒绝及
  P/C重叠需求检查，未发现Critical/Important；主agent复核语法、hash锁定及原driver/reference未变。
  此结论只准入剩余离线计算，不代表真实333条等价、性能通过或可上线。
- 18:02现场只读确认monthly.service于18:00:30自然启动、18:00:31退出，状态0/MainPID0；
  monthly下一次为09-10 18:00，Actuals下一次仍09-09 19:00，DataBridge/daily仍09-10 06:30/07:03。
  current仍a6ffe，无新手工计算、调度操作或生产写入。新计算须等19:00 Actuals正常退出后安排，
  两脚本保持本地待执行，不提前启动；每进程仍7200秒/4GiB，维护硬截止保护次日原任务。

**17:45维护截止生效，保留已通过阶段（2026-09-09 17:47 CST）**：

- Native参考被原监督器于17:45:00.778终止；本阶段开始时离维护截止仅余6272.503秒，实际6272.695秒。
  这是维护窗口收紧后的超时，不能写成7200秒算法性能失败，更不能写成日期/方向不一致。
  controller1005560和算法组1028846均已退出，失败后的绑定完整性复核无异常。
- `native-execution.json` SHA `403e014173301f1f0c994dceff658f731aee4889f79b75a0de9e43088df8f12a`，
  `failure.json` SHA `450c3d000dfa783d6ca21bfd89ead471e4c615689653f5844037ede7e1dcb461`。
  Native只留下333依赖证明日志，未生成完整或部分native.csv；successor333尚未启动，无全历史等价结论。
  原件保留于原incoming，取回本地`outputs/releases/w3b-state-20260909/native-reference-stopped/`。
- 已通过的cold_init、warm_append17.159秒及今日stateless五字段真值不重跑，不重新初始化状态。
  后续只处理未完成Native参考及successor333，先只读检查全grid/末行修正中的冗余计算，
  在充足且避开原调度的窗口执行；不改失败原件、不加自动重试、不用候选结果充当Native真值。
- current仍a6ffe、Backend仍PID643451；18:00月频timer保持active，月频service尚未触发。
  本轮未Intake/激活/业务写库/发布生产state，Mac3与域名未操作；此时不启动新的重计算。

**W3B 首方案实际新增日17.159秒、当天冷/热一致（2026-09-09 16:08 CST）**：

- 同一受控调用中cold_init正常退出：5473.591秒（约91分14秒），两族各650历史行从零计算，低于7200秒。
  私有状态5,163,110 bytes，SHA `7ed977884c197f4c576c93c2a199df6572abf229994dd729e31cbb1b8745cc2d`。
  该时间属于一次性初始化，不是每日预测耗时；没有发布生产state或写业务事实。
- 从feature09-07真实推进到09-08的warm_append正常退出：**17.158577秒**，低于120秒；不是相同Request重试。
  新状态5,168,472 bytes，SHA `79cc6cc24baf0c2eb93666e1386650de1301787163dcb02024c54b32d1d7d68a`。
- 同09-09 Request的原stateless路径正常退出：162.788452秒。两者五字段完全相同：feature09-08、
  target09-15、方向0；两份result JSON SHA均为
  `61f513cc099618646c18543ea284ed420988ef6d0631b632189cd471b712ada3`。本次新增日约提速9.49倍。
  各进程均由原监督器约束4GiB/8线程，未触发内存、时间、输出或输入完整性拒绝。
- 三段原件位于同`w3b-state-20260909.HFFFwd`的execution目录，本地只读取回
  `outputs/releases/w3b-state-20260909/`。主driver已继续唯一Native参考，算法PGID1028846；
  333个Request依赖证明通过，需独立计算的末行修正为10Y257、7Y261。尚无完整Native/new等价结论。
  不重跑已通过的三段，不把单点一致外推为全历史一致；W3B另外两个方案仍需验证，三者整批切换条件未满足。

**W3B 增量候选独立审查通过、开始私有实测（2026-09-09 14:26 CST）**：

- 首方案Python SHA `6db81c1d01c86a1bb9de9e46776e1e8979f9162e60ebb16aa86a13bf75dd9833`，
  Metadata SHA `4d13cdf08ca795fec593bbe93de9187bb00aae4c24b47b7af80977307e9b5373`；Metadata仅说明及draft版本
  更新，业务身份/五字段合同不变。只读两文件校验通过，原训练、参数、final/streak及stateless路径AST不变。
- 复用已有双族NPZ状态及原月度窗口，未新建平台模块或月度重建调度。纯内存真实日期函数检查通过日期追加、
  跨月、原始修订拒绝、特征后缀重算、7Y缺行及有界非可执行状态；独立审查无Critical/Important。
  本次只准入2026年初始化与实际新增日、历史333无状态对照，不推广到2024年以前状态域。
- 临时driver SHA `d15ff4e9927a12d55092eb7968ec0dda2f5def4615d822f6bf46e2df26c12bda`，原独立Native参考
  SHA `d3fe92f44fe18ec58cccb8a4660aaca3d3a76f106f52f2a1adf18aedc578d54b`；三段请求/状态路由独立复审通过。
  前一交易日冷初始化→今天实际增量120秒→今天原无状态路径五字段真值，再运行一次Native/新333对照。
  初始化与无状态参考使用离线7200秒预算，不计入增量日频SLA；任何失败停止后续阶段，不自动重试。
- 新独占执行目录 `/opt/bond-factor-lab/incoming/w3b-state-20260909.HFFFwd/`，维护PID1005560。
  上传四文件SHA均读回一致，截止仍为09-09 17:45 CST。状态仅写入本次私有execution，不发布到生产state，
  不Intake/切换/写业务事实；current a6ffe、Backend原PID643451及18:00月频timer不变。尚无真实性能或等价通过结论。

**W3B 首个日频性能实测停止、定位重复历史训练（2026-09-09 14:04 CST）**：

- 13:59:59监督器按120秒上限终止首方案，实际120.088秒；算法及driver均已退出。
  `daily-execution.json`和`failure.json`保留于下述unitfix执行目录，失败后的输入、代码、环境及release
  完整性复核无异常。没有启动Native参考或完整历史对照，没有Intake、业务写库或发布生产状态。
- 日志显示10Y历史同期265配置、3 seeds约1.5分钟；当前月11个入选配置很快完成，随后进入7Y历史同期
  265配置、2 seeds时达到上限。瓶颈是每天重复计算不变历史窗口，而不是调度脚本本身。
- 不放宽每日120秒门槛、不重复运行未修改候选。仅在ignored两文件草稿内复用已有双族NPZ私有状态，
  按日期保存/追加可证明历史依赖不变的Phase A派生结果；月度排名按原窗口切片，当期仍按每条Request
  精确计算。未新增只能同月复用的JSON状态格式，也不新增每月自动重建；历史修订/无法复用明确失败。
  实测使用前一交易日Request显式私有冷初始化→今天Request实际增量预测（120秒）→今天无状态原路径
  作为五字段真值，再进行一次完整旧/新历史对照。不把同Request cache hit当新增日期的性能证明。
  不新增平台状态框架，不触碰已闭环W2、其他Registry、Mac3或调度配置。
- 原八份草稿的静态审查通过不等于性能或等价通过；首方案状态改动后须重新审查相关差异。

**W2 ECS 闭环、W3B 首个真实算法启动（2026-09-09 13:58 CST）**：

- W2两份gray均通过，分别一次batch写入75条；独立`gray-readback.json` SHA
  `7b3b54dc8dd2c2f71146b81ca98f621878e26c484eacc860b822615199d63f09`，确认各333历史+75灰度、
  旧事实/回测/Actuals及非本批active Registry不变、Dashboard一致、零running。
- daily.timer恢复active，next trigger仍09-10 07:03；13:26:19人工触发一次installed daily.service，
  invocation `81cb53c90add4a848acd1d194b047c78`，13:49:04正常退出。47个方案中W2各1success
  （5Y run5552、7Y run5553），其他45个跳过写入；本次不是自然timer触发，不混淆证据。
- 最终只读验收`installed-daily-final-readback.json` SHA
  `0a77e346c2e3ce5c1b24feac22fb8f64526b6452f88ba6010bcfd8235eddfabf`，状态`W2_ECS_BATCH_CLOSED`。
  两新事实feature09-08/target09-15、方向0/1，与当天模拟五字段完全一致；各方案409条独立事实，
  旧prediction/run/backtest、Actual、Registry、Dashboard的88个active身份及五个timer全部通过。
  current仍a6ffe、previous8eb2，Backend原PID643451未重启，Mac3及域名未操作。
- 整套入口约22分45秒，现场已确认先串行六个Native publisher、再并行两个consumer、最后Blackbox；
  原executor在计算后才做repository去重，所以最终skipped不等于没有计算。W2单点17.658/12.420秒
  与整套入口耗时必须区分。后续不改变执行边界的ECS批次按5.4节复用控制面证据，不重复触发全部47方案。
- W3B首个临时driver的namespace及systemctl重复`EnvironmentFiles`行解析问题均在算法启动前暴露，
  已最小修复并独立复审；原`w3b-first-20260909.9xYk9e`目录及失败日志完整保留，零算法/业务写入。
  当前唯一执行目录为`/opt/bond-factor-lab/incoming/w3b-first-unitfix-20260909.ltZM8N/`，
  driver SHA `1c0076da1a3ba66d14661dddac381b98aec7ecd92f82a62a968d89dcdf7331aa`，原独立Native参考SHA不变。
- 13:57:36启动当前driver，13:58已通过平台、unit、冻结输入与环境身份检查，实际daily算法PGID1003914、
  维护PID1003883；先120秒stateless当日predict，再独立Native/候选完整333对照，4GiB/最多8线程。
  `--finish-before 2026-09-09T17:45:00+08:00`仅通过原进程监督器收紧各次剩余执行预算，保留15分钟给
  18:00月频前的清理/收尾；不放宽等价或性能标准。已有execution标记不可重启，尚无等价/性能通过结论。
- 另两个W3B full-OOS Native独立参考草稿已完成并复审通过：
  `outputs/native-migration-w3b-full-reference-draft/native_reference.py` SHA
  `7c50b2381d78bfb8f437099aee638ea510296eb19e9c556002af9f44f2e18fd3`。两原件逐Request与双源完整依赖
  证明后只共享同次Native自己的两族Phase A，分别保留streak10/5；不读取successor或生产cache。
  尚未上传、拟合或证明性能，不把静态审查当作模型验证。

**W2 两正式通过、ECS 原子切换完成（2026-09-09 13:10 CST）**：

- 7Y正式Gate于13:03:29返回passed，13:03:36收尾完成：run281、333条预测、17条月度指标，
  exact version `bc06323c0fc6`；原件 `formal-daily_7y_1_v28_bbv2/complete.json` 确认旧事实摘要不变。
  两方案正式证据均已由a6ffe候选的当前验证策略复核，不再重复运行算法或Gate。
- 当天模拟13:06:41通过，5Y/7Y单点分别17.658335/12.419984秒，方向分别0/1；
  feature09-08、target09-15，绑定09-09五文件generation；只读Engine复核11表全部不变。
  这才是每天单点耗时，一次性完整历史耗时不作为每日任务耗时报告。
- 已仅暂停ECS daily.timer，并由原installer把已验证archive激活为current `a6ffe3a6b477e2fee67489c43c79ab767433acbb`，
  previous为`8eb2df2e239dec6c3de529b99764d7be3f7316e2`。Backend原PID643451、其他timer、Mac3与域名未动。
- 原迁移CLI fresh preflight SHA `cf755f6874ecabf1737f3cd0f7708155cbfad18038d24bc9468c8271fa72480c`，
  两业务格子各333日期覆盖完全一致、零running；随后原子cutover成功，旧Registry archived、新exact
  version/Registry active，各自insert-only发布333历史事实。原件在同W2 incoming的
  `cutover-preflight.json` 与 `cutover-result.json`；不得重复切换或改写事实。
- 13:10启动5Y唯一gray batch，维护PID995682；区间`[2026-06-01,2026-09-15)`，各Request独立cutoff，
  原repository原子写入。已审临时`run_gray.py` SHA
  `7b786af3f8533cb07528b8fcb16eae9b4bdcffef1490ceac58a262ac84fddd60`，只在本次维护进程通过
  原profile参数明确7200秒/4GiB/8线程，不改平台默认值。完成后再串行7Y；两项完成才恢复daily.timer，
  执行一次installed daily.service并验收09-15保留键。此时尚未宣布W2批次闭环。
- W3B首方案离线driver两个Important已修复并定点复审通过：失败保留原Output及完整日志；每次验证重枚举
  环境安装记录集合与hash，拒绝运行中新增包。文件`outputs/native-migration-w3b-reference-draft/run_comparison.py`
  SHA `c83fb450f2a8f087d4080de0d77a4cf50481dab1a762fb37809888a0e651eae1`，参考源码SHA仍为`d3fe92f4…`。
  只准入首次受控离线验证，尚未上传/拟合/证明等价，不与W2切换窗口争抢资源。

**W2 首份正式证据完成、第二份开始（2026-09-09 12:46 CST）**：

- 5Y正式Gate于12:43:37返回passed，收尾验证于12:43:44完成：run280、333条预测明细、17条月度指标，
  exact version `67018ae8f703`，绑定09-09 generation/snapshot；仅一次算法子进程，完整Gate约29分12秒。
  wrapper复核旧事实摘要不变；原件 `formal-daily_5y_2_v28_bbv2/complete.json` SHA
  `0706adcdb48168d84fd3ef7633c7c8170f54f782cb1348b6f031ae0335074c6c`，已取回本机ignored目录。
- 主agent从预安装a6ffe候选重新只读加载当前canonical和`verify_passed_blackbox_backtest`通过，数据库
  读回333条、产品预测仍0、全库running0，7Y三张回测表均无旧证据。这不是激活或产品事实发布。
- 12:45:03以字节未变的原`run_formal.py`启动7Y一次正式Gate；维护PID989771、算法PGID989846，nice10。
  12:46实际读回正常训练、已保存started，无complete/failure。输出在同W2 incoming的`formal-7y.log`和
  `formal-daily_7y_1_v28_bbv2/`；不得重启或覆盖。仍由ca39运行，current8eb2不变。
- 当天模拟临时入口已完成独立审查并上传，尚未执行；本机
  `outputs/releases/w2-cutover-20260909/simulate_today.py`，SHA
  `b0124685efc5011c2ef195b68496320cf887acaff6bb1b8e2554f33f03371be3`。
  两正式Gate都成功后才可执行一次：a6ffe候选、真实installed locale、各一次stateless predict，
  120秒/4GiB/8线程、feature09-08/target09-15，由只读Engine核对11张业务表不变；不写状态、事实或切换。
  后续gray区间为这次已授权installed入口保留09-15业务键，不提前占用当天验收点。

**切换包预安装与参考工具复审（2026-09-09 12:25 CST）**：

- W2真实receipt与W3A原件重绑定已独立审查通过，随`7ce0412`提交；没有重跑算法或改写输入身份。
- `a6ffe3a6b477e2fee67489c43c79ab767433acbb`只进一步移除两个旧V28的ECS部署target，保留全部Mac3
  成员及其他方案；同步既有部署合同中两个Mac3-only预期，不新增测试。相关23测试、87 subtests通过，
  矩阵独立审查与原W2原子映射校验通过。方案代码、config、环境与校验策略不变，不因此重做正式Gate。
- clean commit确定性archive连续构建两次，archive与manifest字节相同；archive SHA
  `4775c8070b74d18d099e0496e2375a0627819647c77eb34e0370834fc2ce12c5`。
  已经原installer预安装至ECS，明确`activated=false`。current8eb2/previous3162及Backend原PID643451
  读回未变，尚未具备切换结论；必须先完成两方案正式Gate、当天模拟及fresh控制面前置条件。
- 12:25读回5Y正式算法PGID982127运行10分52秒、RSS约893MiB，仍无complete/failure；7Y正式Gate未开始。
  不重复dispatch、不抢训练资源。原timer保持运行，最近触发仍为18:00 monthly。
- W3B专用参考草稿唯一审查问题已修复：直接编译并执行hash校验过的同一份源码bytes，私有package不搜索
  磁盘子模块，避免旧pyc绕过源码身份。定点复审通过，其余计算与证明代码未变；Python SHA
  `d3fe92f44fe18ec58cccb8a4660aaca3d3a76f106f52f2a1adf18aedc578d54b`。
  该草稿可以进入受控离线验证，尚未实际Native导入、拟合或等价/性能验证；不纳入当前W2候选。

**W2 完整算法对照通过、正式回测启动（2026-09-09 12:15 CST）**：

- 唯一四段对照于12:12:30正常完成，`complete.json`确认两个333条，五字段全部零差异；不再重跑。
  W2原始receipt SHA `337c6d9483e00721cefb1c43cedf647a4def04bc1313584371cbdc143dd7df1e`，
  绑定ca39候选、当前工具 `652d901d0678deb85e7c70d22cb02ccc3ea071fa383090568b7eb313d366949d`、
  `full-20260909-063339-4688e3c69f8d` 五文件与各自完整Request。5Y两方2432.991/1743.958秒，
  7Y两方1573.871/1084.233秒；7Y两份原始CSV SHA均为
  `47a6f9b8037a271f34ffa5406d37f629d55736196329a9607fe65c56d8dd3b6b`。
  原始CSV字节hash与receipt规范JSON结果hash不同是编码层次不同，不混为一个摘要。
- 原始结果与执行日志已取回ignored `outputs/releases/w2-offline-20260909/four-worker-evidence/`。
  当前canonical W2 receipt取自上述真实输出；W3A只更新已通过原件的工具绑定与生成时间，target事实不变。
  主agent已用现有标准结果解析器独立复核两方333条按序相同、规范结果hash、当前old/new codehash；
  不执行算法、不更改任何receipt输入身份。候选凭据变更另做独立审查。
- 12:14启动已审查且字节未变的 `run_formal.py --scheme-id daily_5y_2_v28_bbv2`，控制Python
  PID982053、算法PGID982127，nice10。完整前置检查通过，12:15已产生`started.json`并实际训练；
  使用原Harness持久化Gate，单次7200秒/4GiB/最多8线程。7Y正式Gate尚未开始，必须串行。
  日志及结果在原W2 incoming的`formal-5y.log`和`formal-daily_5y_2_v28_bbv2/`，已有标记不得自动重试。
  这一步只建立正式回测证据，不等于Registry激活或产品事实发布；完成后须读回正式证据和旧事实摘要。
- 当前全部one-shot空闲后才启动维护，最近原timer触发为18:00 monthly，窗口充足；current8eb2、
  previous3162、Backend及installed timer均不改变。算法对照与正式入库证据分别绑定各自实际输入。
- W3B首个专用Native参考草稿已在`outputs/native-migration-w3b-reference-draft/`完成，正在独立审查。
  它不是新Blackbox交付或长期框架；尚未运行真实Native导入、依赖证明或拟合，不能报告等价/性能通过。

**八个 Liwei 转换草稿完成、W2 首方案完整等价通过（2026-09-09 11:51 CST）**：

- W2 5Y Native 与 successor 均正常退出，各333条五字段完全一致；两份 CSV SHA 均为
  `99f1ed8ad3da3ed0d481bad37638c7018f15a81fd616504ffaafc418cb314ffc`。
  Native 用时2432.991秒，successor1743.958秒，属于一次性历史批量，不是每日预测耗时。
  原控制PID953168自动进入7Y Native PGID970906，11:51读回运行23分钟；仍无整体complete/failure，
  7Y successor及W2正式Gate尚未执行。不重启driver、不重跑已成功5Y。
- W3B三个、W3C三个、W3D两个合法两文件草稿全部完成，并分别通过独立审查，无未修复Critical/Important。
  五个full-OOS草稿复用既有私有增量状态协议；三个monthly-PIT草稿只用调用内缓存，不新增平台框架。
  所有稿件仍在ignored outputs，尚无实际全量等价/性能结论，未Intake、入库、部署或切换。
  W3C保持active `platform_live_pit_variant`，不改回source-original；W3D分别保留三方/四方投票及
  streak10不重置、streak8反向回退重置的源差异。路径及Python摘要如下，完整Metadata与源hash见各自
  `MIGRATION_NOTES.md`（位于两文件交付子目录之外）：

  | 草稿目录（均在 `outputs/`） | Python SHA-256 |
  |---|---|
  | `native-migration-w3b-10y01-cons-draft/` | `8de00b60b3a64d471d4803bb7fc55e2bffbee064aa56138c519e7c8179aeed1c` |
  | `native-migration-w3b-10y02-cons-draft/` | `a1fafbfe8c11866cc32a9b446874810bcc0fa0d0084115fa6a8ea8e9dbbf4992` |
  | `native-migration-w3b-10y01-full-draft/` | `a15cb1ee13a20eed06402be3f4491a1a8938245f09b1e68818d7c049f841e0a0` |
  | `native-migration-w3c-auc-static-draft/` | `171224803329b8cb25ebb601ef866fbc6c8ce797b6f841211f66703270e16215` |
  | `native-migration-w3c-auc-yearly-draft/` | `8ac8392ea5749f73756e4ac8845d958b4024613c8ffa83944d38410fec2b50ef` |
  | `native-migration-w3c-ic-yearly-draft/` | `6e452c5e1860448b559b30919ea724edf23d4e0b704401361c63ed6bf82fa561` |
  | `native-migration-w3d-7y01-draft/` | `10985741c4240f3aee4f11deb23b02c7956db9ea0d39d1a4fc266131f90ba98c` |
  | `native-migration-w3d-7y03-draft/` | `4e3441b45361cf7a684a988519ae3573943b608a7b34b217a1df527717d261d1` |

- 上游SOP第6.1节补充明确引用本迁移第6.1–6.2节的已批准简化标准，避免将通用的首中末冷复算及
  重复/顺序/子集矩阵再次引入本次迁移。逐Request截止、完整结果等价及失败无Output要求不变。
- W3后续真实参考入口仍需最小接入，现有受控comparator并不已经支持这八稿。先在树外准备首个W3B
  的专用Native参考：由原Native证明逐Request训练依赖，再复用其自己计算的Phase A；月度窗口为子序列，
  10Y/7Y各自保持日期和seed，不能盲套W3A连续前缀或借用successor输出。证明失败即停止复用。
  暂不修改正在执行的W2候选或工具hash，不与W2争抢ECS训练资源。
- ECS current8eb2/previous3162未切换；W3A保持已闭环，Mac3、域名、W4与confidence DDL未操作。

**参考计算完成首段、后续转换与证据准备（2026-09-09 11:12 CST）**：

- W2 首个 5Y Native 完整参考成功退出，耗时 2432.991 秒，333 条结果已保留，CSV SHA
  `99f1ed8ad3da3ed0d481bad37638c7018f15a81fd616504ffaafc418cb314ffc`。原控制 PID953168 自动进入
  5Y successor 计算，算法 PGID963365；并未重新启动 driver。7Y 两段及完整 W2 凭据仍待当前流程完成。
- W3B 10Y02 私有状态草稿独立审查通过，无 Critical/Important。随后按已核实的源差异完成第三个
  `liwei_0616_10y01_full_oos_k3_div_k10_bbv2`，仅改变身份、model1/streak10、函数名与独立状态 schema，
  不改连续 2024 年起的 full-OOS 语义。第三稿独立差异复审也通过；Python SHA
  `a15cb1ee13a20eed06402be3f4491a1a8938245f09b1e68818d7c049f841e0a0`，Metadata SHA
  `c5440c0eda7a02ce2456539eec80f26750f5bce8963eb89e3db9fc99bca7a69a`。
  三稿均可进入受控离线验证，但仍未证明真实等价、恢复或性能；未 Intake 或纳入部署。
- W3C 第一个 AUC static 方案开始独立转换，只写 ignored outputs。已核对 active 的
  `platform_live_pit_variant`、2024 年前带 horizon gap 的 AUC 筛选、STD/DIV/ACCWT 加独立 CROSS_7Y
  两族、实际 K=10/5/10/15，以及月度排名 `usable_end=max(0,pi[0]-horizon)`；不得按 ALL_K10 名称
  把四个 K 统一，也不得改回 source-original。尚未完成交付或任何真实模型验证。
- 用既有、字节未变的 `verify_candidate.py` 在 ca39 候选上完成 W3A 原件只读重绑定。输出位于本轮
  W2 incoming 的 `w3a-rebound.json`，SHA
  `7ca902aa648556f24243557394ce32069cdbf3698a2774358be8451ad1bb0f97`。两组各333条证据和原正式
  run278/279复验通过；target 证据及环境指纹与原 canonical receipt 完全一致，只更新工具绑定/生成时间。
  算法执行0、数据库写入0、current不变。原件核验沿用原对照环境，不发布或更改生产状态。
  该输出先保留树外，等待 W2 成功后统一更新开发线凭据；没有重跑已闭环 W3A。

**并行转换与下一步执行准备（2026-09-09 10:52 CST）**：

- W2 唯一四 worker 对照仍在计算首个 5Y Native 参考：控制 PID953168、PGID953180 存活，四个 worker
  各接近一个 CPU 核，进程组 RSS 约 2.49 GiB。尚无完整 Output、successor execution、失败或成功凭据。
  这是一次性全历史拟合，不是每日单点耗时；不因 stdout 暂无刷新重启，也不增加重复验证矩阵。
- 后续正式 Gate 的单方案包装已完成独立审查并上传本轮 incoming，尚未执行。文件 `run_formal.py` SHA
  `2927cc63306d47dceebc59dfe97aa8f55a7643521f7faf5e7d5834512dea82dd`；仅在独立维护进程内收紧现有
  Harness Profile 至 4 GiB/最多 8 线程，原 Gate 负责算法、校验、操作审计和 repository 持久化。
  必须先取得两个方案完整零差异凭据，再逐方案执行一次正式 Gate；正式证据绑定自身实际输入，不强求与
  算法对照 generation 相同。已有任何目标 backtest 或执行标记先人工核查，不自动重试。
- W3B 10Y01 月度 PIT 两文件草稿已修复辅助 7Y 缺当前行、prior 短历史两项边界，独立复审通过。
  Python SHA `8de00b60b3a64d471d4803bb7fc55e2bffbee064aa56138c519e7c8179aeed1c`，Metadata SHA
  `84b6f16828b0d4ab9d8ea7812f2d40f8d114af820ec79ad743f03b4281bd2361`。只读交付校验及无拟合样本通过，
  尚未证明真实等价/性能，未 Intake、部署或入库。
- W3B 10Y02 源码实际为连续 2024 年至 cutoff 的 full-OOS，不能套用前一方案的月度 PIT 优化。
  已沿用 W3A 私有增量状态协议形成独立两文件草稿，分别维护 10Y/7Y 日期、特征摘要和 Phase-A 结果，
  各族只重算变化后缀；无新增平台缓存框架、持久模型或跨方案依赖。Python SHA
  `a1fafbfe8c11866cc32a9b446874810bcc0fa0d0084115fa6a8ea8e9dbbf4992`，Metadata SHA
  `f9a04607e978c386312bf351c5443144e170fb2b0abb31d288239e1286094b6f`。合成状态读回/推进检查通过，
  已交独立审查；实际模型拟合为零，不能据此宣布冷/热等价或每日性能通过。
- 两份草稿仅在 ignored `outputs/native-migration-w3b-10y01-cons-draft/` 与
  `outputs/native-migration-w3b-10y02-cons-draft/`；第三个 W3B 方案等待状态实现审查结论后复用。
  ECS current/previous、Backend、所有原 timer 不变；Mac3、W4 和 DDL 没有操作。

**W2 四 worker 对照进行中（2026-09-09 10:19 CST）**：

- 候选 `ca39a044c6403ab874ea22d49c154b02de95f7e9` 仅预安装，archive SHA
  `a5e677f7b365707324f2cbda08930118a2a6a9afb3b5731ea78e935a381c2f95`，current8eb2/previous3162 不变。
  唯一目录 `/opt/bond-factor-lab/incoming/w2-four-worker-20260909.rpZUXS`，控制 PID953168、首 Native
  PGID953180、nice10；started.json 已生成，5Y Native 正在计算。不是 W2 等价通过或切换完成。
- 独立复审确认 worker 8→4 仅改变参考并发：ordered pool.map、每模型 n_jobs=1 和固定 seed 保持不变。
  私有复制/缓存隔离函数 AST 与已经真实 JIT 验证的实现相同，不重复该探针。相关 85 测试再次通过。
- 先前两个目录的失败记录保留；不得再启动旧 driver。完成/失败按本目录 complete.json/failure.json 和
  真实进程判断，不凭短暂没有 stdout 或过期“运行中”文案判断。后续正式回测和切换等待当前全量零差异结果。
- W3B 两文件草稿独立审查已开始，发现“辅助7Y当月全缺行”的潜在保真差异，正在用无模型拟合样本确认并
  修复；草稿尚未批准入库，不把静态检查通过当作等价通过。

**W2 参考并发修正与 W3B 草稿（2026-09-09 10:13 CST）**：

- 私有源码隔离生效，但 8-worker Native 参考在第三个月训练时被 4 GiB 进程组 RSS 保护终止：
  10:12:06 采样 `4344676352 > 4294967296` bytes。控制进程和 PGID951373 已退出，failure.json 保留。
  未产生完整 Native Output、successor 计算、receipt 或业务事实；不把此状态报告为仍在运行。
- 只把临时 V28 comparator 的参考 worker 从 8 降至 ECS 的 4 vCPU；原 core、265 配置、seed、月份窗口、
  Request 和 Result 比较不变，保留 7200 秒/4 GiB 上限。原 core 使用有序 pool.map、每模型 n_jobs=1 和
  显式 seed；经独立审查后才运行新的候选，不盲目重启旧 driver、不放宽资源限制。
- W3B 10Y01 cons-SAY 两文件草稿已经完成，在 `outputs/native-migration-w3b-10y01-cons-draft/`；
  Metadata/Intake 只读检查、目标 Python 语法/import、源关键配置及无拟合控制器检查通过。
  10Y 与 V55_7Y 两个 Phase-A 族、不同 seed、streak 和训练不足分支分别保留；尚未宣称等价/性能通过，
  未进入 canonical Intake、部署矩阵或数据库。完整说明及 hash 见该目录 MIGRATION_NOTES.md。

**W2 修正后离线对照已启动（2026-09-09 10:07 CST）**：

- 候选 `8e173c8f5083c37791028c9f5343f0f5a333b7e2` 仅预安装，archive SHA
  `bc738b2366d9058bfc4afb67e69a9c856168ae0281bafc09aba2ea687cfec1a1`；current8eb2/previous3162 不变。
  独立复审通过；相关迁移、结果复用与 runner 测试共 85 passed。未扩大算法验收矩阵。
- 实际旧 core 微型 JIT 通过：模块从私有 source 加载，`.nbc/.nbi` 只在私有 numba-cache 中产生，候选源码
  digest 前后均为 `64d9050c8d9a15cf6dcccb058a98386b4c59f037ad5784c3f140725440698886`。
- 控制进程 PID951361、nice10；10:07:53 开始完整 W2 对照，首个 Native PGID951373。
  证据根 `/opt/bond-factor-lab/incoming/w2-isolated-20260909.nVpeWy`，使用 09-09 ready generation 的
  五文件私有冻结副本及两个各333条的平台 Request。查询确认两 successor 尚无持久化 backtest。
  `started.json` 记录 exact version、文件 hash、Request hash、实际 locale 与 driver hash；成功才写 `W2.json`
  和 `complete.json`。失败写 `failure.json`，不重启该 driver、不伪造 receipt，不执行切换。
- 10:09 读回确认 Native 正在计算、缓存位于私有路径、候选源码无 Numba cache，current 未变。
  当前仍是运行中，不是等价通过或 W2 闭环。正式 Gate、当日模拟及原子切换尚待本次完整结果。
- 为避免串行等待，另一个无 ECS 操作的独立工作流仅在 ignored outputs 中转换 W3B 的 10Y01 cons-SAY
  两文件草稿；未 Intake、未激活、不宣称结果或性能通过。W3C/D 与其余 W3B 仍待推进。

**W2 最短路径执行修正（2026-09-09）**：

- 09:55 首次离线参考启动后发现旧 core 的 `cache=True` 仍向候选 844e1 源码写 Numba cache；09:58 已停止
  精确 Native 进程组，未启动 successor，无完整结果、receipt 或业务写入。current8eb2/previous3162 未变。
  问题是执行隔离遗漏，不是算法差异；旧候选保留在本次 incoming 的 quarantine，原失败记录不删除。
  修正为仅复制 harness/shared/当前 old scheme 到受配额约束的私有源码目录，显式外置 Numba/Matplotlib
  cache，运行前后验证源码字节一致；必须先用旧 core 的真实微型 JIT 验证缓存位置和 immutable digest，
  再启动一次修正后的对照，不通过重复完整训练验证该隔离修复。
- 算法转换已完成，当前缺口是新 exact code 的 ECS 完整同输入对照与正式入库；不再执行旧 100 条重复矩阵。
  此前全历史 1800 秒中断不能外推为每日预测慢：既有同输入单点实测 5Y 13.56 秒、7Y 11.97 秒。
- 临时 comparator 增加可选树外、新建且不可覆盖的证据目录，保留成功 Native/successor 的原始 Output、
  日志、耗时和输入 hash；successor 失败不再删除已完成 Native 原件。不接受外部成功结果注入，不自动重试。
  Native 参考复用现有进程组/RSS/超时控制，各 worker 数值线程为 1；successor 单次 Profile 收紧至
  4 GiB、最多 8 数值线程、7200 秒调用上限，不修改全局 Runtime Profile。各算法退出后复验输入未变。
- 本次只在不可变候选目录离线执行，不激活候选、不改 Registry/current/timer。新 W2 receipt 写在树外，
  不调用会在计算结束后因已有 W2.json 而失败的 CLI 包装器。正式 Gate 和原子切换仍为后续步骤。
- comparator 源码 hash 随执行工具修正变化；候选晋级前须用已批准的 W3A 原件只读重生成其工具绑定凭据，
  不重新运行 W3A 算法、正式回测、初始化或灰度。旧 immutable release 与历史 receipt 不改写。

**W3A ECS 本批闭环（2026-09-09 09:27 CST）**：

- 唯一installed daily验收Invocation `bc96e8f427fc4885856adc49992832a5`于09:20:38退出0。
  全47个日频方案中，W3A full run5361、cons run5362分别success且各写1；其他45个正常skipped，
  failed/blocked/denied为空。此前08:56仍运行的状态已结束，不得再次触发同日验收。
- 最终只读验收通过：两条scheduled_live的Request ID、predict09-09、feature09-08、target09-15、direction0
  与当天模拟逐字段一致，exact version正确，各successor累计409条，业务键唯一且run/backtest XOR成立。
  排除本次47条新审计run及2条新预测后，原全部run/prediction行摘要匹配调用前基线；旧Native无新run，
  Registry、Actual、backtest完整摘要不变，Dashboard读模型88个active身份正确。
- full生产state完整envelope、实际runtime身份及当天generation匹配；状态锁空闲，全库running0。
  immutable current8eb2/previous3162源码校验通过，标准算法临时目录/runtime views已清空，无残留执行进程。
  Backend原PID643451和health200正常，所有原timer active，daily next09-10 07:03；Mac3、域名和DDL未动。
- 证据`installed-daily-final-readback.json`保存在本轮incoming及本机ignored目录，SHA
  `ce5507db96ca128d11921eb0012a931753676807e175244185d5a5c36603b8b7`。
  结论仅为W3A两个方案在ECS的已授权模拟、切换与真实入口验收闭环，不冒充多次自然观察或Mac3晋级。
  不再重跑W3A初始化、等价、正式回测、gray和同日验收；下一批继续W2，随后W3B-D，整个ECS项目尚未闭环。

**唯一补缺验收完成，真实日频入口运行中（2026-09-09 08:56 CST）**：

- full唯一09-14补缺于08:42:16退出0，耗时2359.070秒，existing gap-fill PASSED且remaining为空。
  仅新增gray_live run5346及1条预测；full/cons现在均408条。原333历史和74条gray未重算。
- 08:55独立只读验收确认日期/目标/版本、唯一键、run/backtest XOR；排除唯一新增fact/run后，全部原预测、
  run和backtest完整行摘要匹配当天模拟基线，旧Native及其他方案事实未改变。生产state完整envelope仍为
  `f2162a6a689edb1ecc646ccf71afbacf387c12ddc253b46b6498d61ff831bc67`，锁空闲，算法PGID和两个私有目录已清理。
  immutable源码、88个Dashboard active身份和Backend原PID643451/health200均通过。
- Actuals保持原调度，08:30:30启动、08:31:07退出0，Invocation
  `f8e4f63658454dd1ac2768bc2c1ce3a0`；其自然刷新journal已与本次读回保存，Actual新基线单独记录，
  未把自然变化当作迁移写库或强制回退。`one-gap-readback.json` SHA为
  `a8c606aff9d325208099b9376a9071f1c05509bac0d14bcd53c1fcbbb9ace8e9`。
- 08:56:27恢复daily.timer，next为09-10 07:03，随后仅start一次原installed daily.service，Invocation
  `bc96e8f427fc4885856adc49992832a5`、初始MainPID947709。当前activating，尚无最终退出结果，禁止重复启动。
  调用前max run_id=5346，全表事实/Registry/Actual基线位于本次`one-gap-readback.json`；预期W3A各1条
  scheduled_live成功、09-15目标/方向0与当天模拟一致，其他45允许skipped审计但不改变事实。
  必须等全47最终摘要、DB、进程、state和控制面验证通过后才关闭W3A本阶段；失败仍按整组回滚顺序收尾。

**早间恢复、当天验证与重切完成，唯一补缺进行中（2026-09-09 08:03 CST）**：

- DataBridge installed service于06:30:30启动、06:33:45正常退出，Invocation
  `e30f27a8872b4424905b3f5ef69556c1`；新generation `full-20260909-063339-4688e3c69f8d`、
  snapshot `snapshot-c8655eab5d799e1c7004e63c`已通过平台当天ready只读校验。
- 原3162 installed daily于07:03:23自然启动、07:50:26退出0，Invocation
  `c9f7c37b866741198e94a6c88c0c36b4`，47个方案全部success、各写1，无failed/blocked/denied/skipped。
  已逐条读回journal关联的47条DB run及预测，确认scheduled_live和09-09日期；旧full run5300、cons run5310
  均成功，回滚后旧Writer实际恢复验收通过，不再依赖reset-failed后的Result字段。
- 同一8eb2候选、真实installed locale（LANG=en_US.UTF-8、无LC_ALL）下，各一次09-09无业务写入模拟通过：
  full-OOS正常增量11.592秒，cons-sda stateless63.542秒，均在120秒/4GiB/8线程边界内。
  两者predict09-09、feature09-08、target09-15、direction0，也与当天旧Native实际结果一致。
  九张Registry/run/prediction/backtest/Actual表完整摘要前后相同。
  full生产state正常推进，没有初始化；payload SHA变为
  `37dcd8cb4be86541a706500601b648805512d4526c682b6bdb7106e9202104df`，envelope SHA为
  `f2162a6a689edb1ecc646ccf71afbacf387c12ddc253b46b6498d61ff831bc67`。
- 独立执行审查无Critical/Important问题；标准installer核验原archive后激活8eb2，previous3162。
  仅fence daily.timer，在零running和fresh preflight下执行整W3A原子cutover；plan SHA
  `4aadb0c3adcc0b9ff5c5ccf12d1b6ccf2eb862a6635ef54e88ae58e1935a7931`。
  08:02独立只读验收通过：新Registry/exact version active、旧Registry archived，正式278/279各333事实原样复用，
  full407/cons408事实及所有非Registry业务表完整摘要不变，其他86个active身份与Dashboard读模型88项一致。
- 权威日历枚举确认只缺full target09-14（predict09-08、feature09-07）一条；cons无缺口，09-15均未被占用。
  08:03确认唯一 `fill_one_gap.py` driver PID937747、既有CLI PID937750已启动，实际跟进须复核完整命令。
  使用半开区间 `[2026-09-14, 2026-09-15)`、7200秒，nice10让未改变的08:30 Actuals保持原优先级。
  日频timer仍fenced；这是私有冷状态历史补缺，不推进生产state、不重跑原74条gray或正式回测。
  当前无完成结果，不得重复启动。成功后读回唯一事实、私有进程/目录清理、生产state不变，再恢复timer并
  人工调用一次原installed daily完成真实新Writer验收；失败按既定整组rollback顺序收尾。
- 本轮证据沿用incoming `w3a-locale-20260909.qqTZuf`与本机ignored目录：
  `current-day-simulation.json` SHA `feb584c97b5d525b1ba9f1d076cdf34f27a9b75194855927358a2161809d2968`；
  `recutover-readback.json` SHA `05ce55ecb2d7015a1c250f28f4961a09bd6dae8bd0334a5899f170489323942e`。
  后续08:30 Actual自然变更须按其真实Invocation单独核验，不能机械套用08:02摘要，也不得停止Actuals。
  Backend原PID643451及Mac3/域名/DDL保持不变。W3A收尾后继续W2与W3B-D，尚未整个ECS闭环。

**环境修复与正常增量验收完成（2026-09-09 02:48 CST）**：

- 初始化CLI于02:32:32退出0，耗时2314.066秒；完整Registry/run/prediction/backtest摘要前后一致。
  state payload为2,583,754字节，SHA仍为
  `0ef5a019ab3a8ec41d7ddf37af30d52c002074040524b4af8c44398d8ed5e9b2`，与备份原状态逐字节相同。
  header唯一identity差异为runtime SHA改为真实日频环境的
  `6e817a9b694d6c46d4414d8b4449e57cc21cba4bc194ba37c62db3f869cd993b`；新envelope SHA为
  `903c623070dad66e4a41f281d703187a5c951480dce424de5f8f6156e8db1588`。
- 独立读回复验代码/Metadata/manifest、五文件/snapshot、完整state、互斥锁、immutable源码和维护私有目录清理。
  随后通过标准executor执行唯一一次正常增量predict（rebuild=false、120秒），02:47:27验收完成，
  耗时8.910秒；Request ID、predict09-08、feature09-07、target09-14及direction1与原模拟逐字段一致。
  此次标准路径确实发布派生state，但前后完整envelope与payload相同；全业务表摘要仍未变。
- 首个验收driver遗漏必填algo_env，在Python参数绑定时失败，未进入executor、未启动算法；原错误报告保留。
  修正调用参数后使用新v2脚本和独立输出文件完成上述唯一算法调用，没有重新初始化、失败算法重跑或覆盖证据。
- 02:48核验无残留维护/算法进程，state锁已释放；current3162、previous8eb2、Backend原PID643451正常，
  全部timer active。下一DataBridge06:30、daily07:03；本轮资源已在05:00前清理。
  环境问题已在正常预测路径消除，不等于已完成09-09当前输入或真实installed日频验收；继续按早间顺序推进，
  不重复初始化/09-08模拟，不伪造当前ready、不暂停整项目。Mac3和所有业务Writer身份未改。
- 本次incoming及本机ignored证据目录保留 `rebuild-completion.json`（SHA
  `a419e09eba7b0839c9e8c53b29d9fad3b190ac762941a46473151dd664b1f25b`）与
  `incremental-v2-verification.json`（SHA `927abc5ac659a7b07d8711f889b76c7f2835fe7f232a7efb181cd68e3b49605c`）。

**继续执行授权与最小环境修复（2026-09-09 01:56 CST）**：

- 用户要求继续推进直至闭环，本阶段仍仅ECS，不改变Mac3、域名、DDL或master。当前凌晨，09-09早间任务
  尚未触发；current3162、previous8eb2、所有业务one-shot idle、daily.timer next07:03、DataBridge next06:30。
  上次reset-failed后service的Result显示success不代表旧Writer已经复跑，必须用新Invocation及DB核验。
- 最小修复选择不改算法、平台Runtime Profile或installed systemd环境：维护调用改用已核验真实日频环境
  `LANG=en_US.UTF-8`，移除SSH继承的LC_ALL，TZ继续采用既有Profile默认Asia/Shanghai。
  旧state环境身份不同，不能修header或跳过校验；本次授权下保留其完整字节证据，再执行一次现有显式初始化。
  独立运维审查无Critical/Important阻塞，确认archived successor可维护、CLI不写业务事实。
- 01:53:50启动唯一driver，初始PID921287；CLI PID921311，算法PID/PGID921327。
  这些仅为定位提示，跟进须复核完整命令。使用同一immutable8eb2、exact3ee3dd2334fd、明确predict09-08及
  尚未更新的09-08 ready generation；这是派生状态维护，不是09-09 scheduled_live或伪造当天输入。
  operator `user-authorized-ecs-locale-recovery-20260909`，现有7200秒/4GiB/8线程限制不变，预算含清理早于05:00。
  01:56已从实际算法/proc/environ读回LANG与TZ正确、LC_ALL不存在、五项数值线程均8，stderr0字节；尚未完成。
- 证据根 `/opt/bond-factor-lab/incoming/w3a-locale-20260909.qqTZuf`，本机ignored
  `outputs/releases/w3a-locale-20260909/`。`before.state.evidence`保存原状态字节；`rebuild-start.json`保存
  全Registry/run/prediction/backtest行摘要；driver调用现有CLI，结束后保存completion及前后摘要，不重试。
  current/Registry/timer均未切换。原始算法等价、正式回测278/279和已发布407/408条事实不重算。
- 完成后先验收状态完整性、真实环境身份、进程/锁清理、业务事实不变，随后正常predict09-08验证日期和方向，
  并如实记录其状态发布。等待09-09 DataBridge ready且旧07:03自然任务退出，先确认回滚后旧Writer恢复；
  再对当前输入做120秒增量验证，通过后按fresh preflight/plan SHA执行整W3A re-cutover。
  新generation不自动要求重建；算法自行判断历史依赖是否可复用。已发布旧正式回测在同exact version重切时
  可复用，不因09-09 generation变化重跑278/279。已存在74条gray不得重复跑；只处理full-OOS缺少的09-14键。
  单条补缺仍走现有gray batch私有状态，不读取或推进生产state；显式使用
  `signal-gap-fill --scheme-id liwei_0616_5y01_full_oos_k3_div_k10_bbv2 --target-date-from 2026-09-14 --target-date-before 2026-09-15 --timeout-sec 7200`。
  该区间只含缺少的一个target，执行前必须按权威日历/事实确认；不含已存在的74条，也不占用09-15当天目标。
  单条历史回放可能需要冷计算，不能用正常增量8.910秒估算耗时或沿用默认600秒；补缺期间仍保持timer fence，
  但不得停止08:30 Actuals。其私有state不得晋级或覆盖生产state；只经已有repository提交授权缺口。
  失败不自动重试初始化、改预算或放宽校验；安全保留旧Writer并报告。W3A闭环后继续W2、W3B-D，不扩大Mac3权限。

**W2后续执行准备（2026-09-09，只读库存核验，尚未启动算法）**：

- 当前两文件script hash与台账相符，exact version为5Y `67018ae8f703`、7Y `bc06323c0fc6`；
  旧Native exact为 `fce0d1126dc5`／`febc16e47919`。旧W2 receipt绑定旧successor字节与comparator v1，
  不能用于当前代码。未找到可复核的本地W2原始完整Request/Native结果；不把历史“参考完成”视为原件仍在。
- 最短比较复用现有 `build_native_successor_equivalence_receipt` 普通W2入口：平台生成完整历史七字段Request，
  冻结五文件与环境，每target各一次Native及successor完整比较，共四次算法调用；不跑额外100条矩阵。
  W3A专属reviewed-result reuse例外不得扩展到W2。成功后每scheme一次正式Gate，先查已有证据避免重复写入。
- 当前CLI `prepare-equivalence` 在计算结束后才用 `open("x")` 创建canonical receipt，而旧W2.json已存在；
  不得盲跑至最后报FileExistsError，更不得删除immutable release内文件。一次性受控driver调用现有builder，
  将新receipt保存至树外，验证后在开发线明确替换过期receipt并审查，再构建新的immutable archive。
- 离线调用显式7200秒，正式Gate使用历史target排他上界 `--predict-date 2026-06-01`、
  `--backtest-start-date 2025-01-01 --algo-env forecast_env_blackbox_v1 --timeout-sec 7200 --persist`；
  该predict参数不是主机当天日期，输入仍绑定所选ready generation。默认stateless Profile内存是64GiB，
  不能误称CLI已执行4GiB约束；启动前须在单次调用层收紧现有Profile至4GiB/8线程/7200秒，不改全局Profile。
  Native参考为带timeout的普通subprocess、可有子进程，另需复用既有进程组回收与资源观测，不能把其证明
  外推为successor允许子进程。先完成执行边界审查；凌晨不启动会越过05:00的额外ECS训练。

**真实日频失败、整组回滚及只读定位（2026-09-08 18:41 CST，覆盖以下运行中状态）**：

- 唯一 installed daily 调用于18:32:42退出，Result `exit-code`、exit1；同一Invocation的最终摘要为
  47个方案：45 skipped、1 success、1 failed，无blocked/denied。full-OOS run5266因
  `Blackbox state exact identity mismatch; rebuild required` 在计算前失败、写入0；cons-sda run5267
  scheduled_live成功、写入1，predict09-08、feature09-07、target09-14、direction1。
  不能把模拟通过、gray成功或cons单方案成功视作W3A整组真实入口验收通过。
- 已保存完整journal和47条run证据，fence daily.timer，核验进程退出、MainPID0、全库running run0、
  state锁可独占后，仅reset-failed该daily.service。8eb2仍为current时fresh preflight确定rollback动作，
  plan SHA `5d9b66dbf193475bae8a77466548f04f8257d7f28e171201828420f699b00c03`，现有CLI原子回滚成功。
  随后标准installer验证并激活3162，current为3162、previous为8eb2，未直接改symlink或release源码。
- 18:39独立只读验收通过：两个旧Native Registry及对应exact version恢复active，新Registry archived、
  新exact version retired；旧prediction/run/backtest完整摘要不变；successor各333历史＋74gray完整保留，
  cons额外1条scheduled_live也保留，因此full407条、cons408条，未删除或覆盖任何事实。
  其他86个active身份及所有非successor预测不变，Dashboard读模型恢复原88个active集合；Actual摘要不变。
  Backend原PID643451、health200；state校验和不变、锁空闲、无残留算法或runner进程。
  daily.timer已active，next09-09 07:03；DataBridge next06:30及weekly/monthly/19:00 Actuals timer均保持正常。
  没有再次启动旧daily，旧Writer的回滚后实际调用验证仍pending，不冒充已完成自然恢复验收。
- 只读身份复算定位到入口locale差异：SSH初始化/模拟继承 `LANG=en_US.UTF-8, LC_ALL=C.UTF-8`；
  按installed unit、manager环境和两份EnvironmentFile重建的systemd环境只有 `LANG=en_US.UTF-8`，
  没有LC_ALL。相同script、metadata、五文件与snapshot，SSH复算runtime SHA精确匹配已存状态
  `cff7095b5e6c7669b77a3335e12164d35cd59cf52b6180ee22cf6f659bbe6ce6`；仅切换这组allowlist环境后变为
  `6e817a9b694d6c46d4414d8b4449e57cc21cba4bc194ba37c62db3f869cd993b`。
  这解释了模拟通过而真实入口拒绝；复算未执行算法、未改状态。已退出进程的environ不能直接重读，
  systemd环境结论来自已核验控制面重建，不能称作保存了该进程的原始环境快照。
- 下一步只处理初始化与installed日频的locale身份一致性；不删除环境校验、不重写state header、
  不因错误含rebuild字样就自动再次初始化，不重跑已通过等价和正式回测。任何环境调整须先明确
  对其他Blackbox的影响及是否改变已绑定runtime身份，再按既定边界批准执行。
  此轮失败安全收尾已完成，监测 `native-ecs` 已暂停；未自动重试切换，Mac3完全不变。
- 小体积证据保存在本次incoming与本机ignored `outputs/releases/w3a-prewarm2-20260908/`：
  `installed-daily-runs.json`、`installed-daily-journal.json`、`rollback-preflight.json`、`rollback-result.json`、
  `rollback-readback.json`（SHA `7940736bdd8dcf134703ba1c6e4ec83a8c02b807169deec99bb1ac91b5ed6016`）、
  `state-identity-diagnosis.json`（SHA `780bbc0996d1914b585333d0b269c98f1da645413b3232752c9588ee43448e8d`）。

**灰度完成与真实日频验收启动（2026-09-08 18:09 CST）**：

- 两个gray CLI均PASSED、exit0、无remaining：full-OOS于17:54:12完成，耗时2721.894秒；
  cons-sda于17:59:54完成，耗时342.375秒。每方案74条预测及74个gray_live success run全部入库，
  当前各407条产品事实（333 historical＋74 gray），保留09-14目标键，未重新初始化或重跑正式回测。
- `gray-readback.json`独立只读验收通过：旧完整事实/旧run/Actual摘要不变、86个其他active身份不变，
  唯一键、run/backtest XOR、日期区间、exact version、run关联及状态校验和通过；算法组已退出、
  两个私有Request/runtime view已清理、状态锁空闲，current/previous严格源码摘要一致。
  Dashboard读模型88个active target；真实Backend `/api/health`返回200。真实Dashboard HTTP未带会话返回401，
  没有绕过认证，不能将读模型验证表述为已取得认证后的HTTP Dashboard payload。
- 18:00月频service已于18:00:31正常退出。18:08:54恢复daily.timer，next trigger为09-09 07:03；随后
  仅人工start现有installed daily.service，原unit/参数/环境不变，InvocationID
  `8e279275e67b400ea097041e34eab3aa`，初始MainPID916573。18:09仍activating，尚无最终退出结论，
  禁止重复start；灰度监测转为该真实调用的完成验收。
- 全日频调用前基线：全产品事实24719条、非W3A successor事实23905条，最大scheme run_id为5251，
  摘要均保存在 `gray-readback.json`。调用后预期W3A各增加1条scheduled_live，不得据“service已启动”宣布成功。
  若跨19:00，Actuals自然执行导致的变化须独立溯源，不要求其机械匹配18:08快照。

**按用户最新指示立即模拟并切换（2026-09-08 17:09 CST）**：

- 用户明确要求“现在直接模拟测试之后，直接真实切换”，覆盖下面原19:10等待安排。原晚间监测已先暂停，
  确认现场无计算进程、one-shot与run后立即执行；不改变Mac3或对外域名，不重跑等价/正式回测/初始化。
- 同一8eb2候选通过标准平台Request/输入/runner对09-08执行各一次日常predict模拟，不写业务事实；
  明确使用120秒/4GiB/8线程边界。full-OOS耗时7.849秒、cons-sda耗时62.071秒，均通过五字段合同，
  feature-date09-07、target-date09-14、direction均为1。full-OOS生产state输入/输出payload SHA完全相同，
  没有重新训练或改变状态内容。`today-simulation.json` SHA：
  `2dcdf9d4435b4eefd1625ddc66c8fa5826142e0c9571534e7ed6dec7be249ec2`。
- 重新取得即时基线 `immediate-baseline.json`（SHA
  `f5ec6e045de782eaccd30cca7e93a5dd8d3823382fcc0b1b2738424c314df236`），随后标准installer激活8eb2，
  previous精确为3162。仅fence daily.timer并确认service idle；current下真实preflight通过，授权plan SHA为
  `8301ecc29ed2b0d6e3570eda18663c80dcf4d79c3fd731925b325b8baa5ccb8c`。
- 现有CLI原子cutover已成功提交，operator为 `user-approved-ecs-w3a-immediate-20260908`。
  17:08:23独立只读读回：两个successor exact version/Registry active，各发布333条历史facts；两个旧Native
  Registry archived，旧prediction/run/backtest的完整行摘要不变，Actual摘要不变，其他86个active身份不变。
  Dashboard共88个active target，旧W3A消失、新W3A出现。原件均保留在本次incoming与本机ignored证据目录。
- 17:08:51左右启动唯一一次串行gray调用：full-OOS后cons-sda，各74条、每条独立cutoff，每方案一个batch、
  最多7200秒；临时driver为 `run_gray_once.py`，初始PID903027，仅用于定位且必须复核完整命令。
  以nice10运行离线补齐，让未变更的18:00 monthly/19:00 Actuals保持默认优先级，不改installed控制面。
  17:09确认full-OOS算法已启动、stderr为空；尚无gray最终结果，不能把进程启动当成入库完成。
- daily.timer仍fenced，其他timer及Backend active；灰度通过后恢复timer并验收实际入口，失败按5.5回滚。
  本轮模拟已证明当天预测性能，但不冒充已产生scheduled_live事实；W3A当前是“切换提交成功、收尾进行中”，
  不是整个17方案迁移闭环。跟进改为本次已运行gray的结果核验，禁止重复启动或再次cutover。

**ECS 切换准备与原晚间执行窗口（2026-09-08 16:58 CST，等待安排已被用户覆盖）**：

- 用户在初始化验收后授权继续推进。已将同一经过验证的 `8eb2df2` archive 标准预安装至
  `/opt/bond-factor-lab/releases/8eb2df2e239dec6c3de529b99764d7be3f7316e2`，返回 `activated=false`。
  current仍为3162；未切Registry、未写预测、未fence或人工触发timer/service。
- 只读准备通过：正式run278/279与09-08generation匹配，初始化state hash不变；旧W3A Registry active、
  新Registry/run/live facts为0。已取得旧prediction/run/backtest、Actual与Dashboard摘要；必须在实际切换前
  再取即时基线，不能将本次摘要用于跨越19:00 Actuals写入的“不变”断言。
  临时 `pre-cutover-readiness-1655.json` SHA为
  `d2e357b7e8df36db9a5b42fd53f0f738e1beaa8068cc01b0609fc100b3cd02b7`，位于本次incoming根及本机ignored证据目录。
- 权威日历确认每方案74条gray：target半开区间 `[2026-06-01, 2026-09-14)`，最后predict-date为09-07。
  留出真实09-08调用的feature-date09-07、target-date09-14；不提前占用模拟业务键。
  gray使用私有冷状态，不读取或推进已预热生产state；日常predict才使用生产state，不重复正式回测。
- 3162回滚archive已找到并保留到本次incoming，SHA为
  `0b8bd380940c8986df23c10928ac708af14921a4e09c1c826efa7017a2ad568a`。严格installer初检发现三个空的可写
  `__pycache__`目录，仅用rmdir移除这三个空目录后复验通过；源码字节/hash不变，没有删除代码或业务文件。
- ECS为4vCPU/15GiB；18:00 monthly、19:00 Actuals不改变。正式切换最早19:10，在两项真实任务成功退出且
  全部one-shot与run空闲后继续；两方案gray串行，各最多7200秒，失败不重跑，按5.5回滚。真实installed daily
  会执行所有active日频算法后再去重，预计产生其他方案skipped审计run；不能误称只运行两个算法或其他run不变。
  今日同一installed daily耗时32分44秒、exit0，仅作耗时参考，不替代新版本验收。
- 独立运维审查未发现新Critical代码问题；两项Important执行前提已纳入：真实daily不得在23:30后首次启动，
  更不得跨到09-09后沿用09-08 ready gate。若剩余日内窗口不足，正常收尾bounded batch并rollback，不能伪造日期。
  daily失败时先保存journal与进程证据，确认进程/锁释放后精确reset-failed该service并读回inactive；若有确切
  abandoned running run，必须确认其进程退出、无已发布事实，再经既有repository失败入口处理，禁止直接SQL改状态。
  rollback必须先在8eb2 current下fresh preflight及DB事务，再切回3162，不能反序。05:00前恢复控制面并清空
  本次计算/锁，保护早间任务；任何无法安全完成的异常暂停并报告，不扩大权限或自动重试。
  复审确认：午夜前启动的daily固定入口predict_date，可以跨午夜结束；不是要求整次运行午夜前结束。
  若午夜后回滚，只恢复旧Writer/current/timer，不启动缺次日ready gate的旧daily，恢复验证保留pending。
  `fail_scheme_run_atomic`是既有仓储API，不是已具备OS进程核验的事故恢复CLI，不能跳过外层现场确认。

**最新验收决定（2026-09-08）**：用户明确取消此次算法迁移的重复、倒序、乱序、子集及其他额外专项验证，
以相同冻结输入下修改前后完整回测的日期和方向逐条一致为算法验收依据，执行规则见第6.1–6.2节。
下文早期矩阵及启动/失败记录只保留历史事实，不再构成当前待办；平台写库、单Writer、回滚和发布安全边界不变。

**最新授权与证据规则（2026-09-08，覆盖此前推进范围）**：

- 用户已明确批准把显式首次预热/故障重建上限调整为 7200 秒，并再执行一次受控初始化；日常增量
  predict 仍为 120 秒，4 GiB/8线程和算法/结果合同不变。复用现有离线 Profile 预算，较短调用方 deadline
  仍生效，不新增运行参数、调度或自动重试。下文首次1800秒失败与“待确认”条目只保留当时事实。
  本次只修改维护调用和 runner 的预算选择：预算隔离先以失败测试复现，再经实际小型 CLI 验证修复。
  定向测试 `52 passed, 17 subtests passed`，全量 `741 passed, 6 skipped, 229 subtests passed`；
  独立审查无 Critical/Important。算法两文件、exact version、状态语义和校验策略不变，不重跑已接受回测。

- 本阶段只在 ECS 替换 W1-W3 的 17 个 Native base / 21 个 successor target。Mac3 当前/previous、
  数据库、launchd、对外域名、DNS/Nginx 与流量均不变；Mac3 晋级及 W4 九个加密方案暂停，恢复须另获授权。
- 算法等价证据与正式入库证据各自绑定自身 generation、snapshot、Request 和结果，不再要求二者输入同代。
  算法等价仍要求冻结输入内旧、新代码逐条零差异；正式回测仍要求当前 exact version、校验策略、运行环境、
  完整事实与原子持久化。两类证据必须覆盖相同完整 Request ID/三日期区间和同一代码身份。
- 跨输入版本不要求三个 cutoff key 或方向摘要相等；相同输入则继续交叉核验完整七字段 Request 与五字段结果。
  首次 cutover 的正式回测仍匹配现场 DataBridge，不能用旧算法验收输入替代现场输入。
- 临时 receipt 升为 `native-successor-equivalence-v2`，旧 v1 文件只留历史，不能由新工具解释为 v2 或手工改号。
  本轮不重生成已 active W1 的历史凭据；W3A 受控结果复用已完成下述 ECS 候选验证。部署新工具前，必须确认本批回滚
  及 re-cutover 均有新规则下可复验的证据，旧 immutable release 自身的历史工具不修改。
- 证据分离已完成本地实现：正式回测全部七字段 Request 的独立摘要进入 plan SHA；预检后任何 cutoff
  变更都使旧授权失效。定向测试先复现摘要未变化的问题，修复后验证旧摘要拒绝且 Registry/产品事实未改变。
  独立复审 Critical/Important 均为 0；全量回归 `702 passed, 6 skipped, 229 subtests passed`。
  本地未配置 isolated MySQL URL，对应两个参数化场景在该次全量回归 skipped；随后已在 ECS 本机 MySQL
  8.4.11 的随机隔离 schema 中实际执行，结果为 `2 passed in 1.91s`。覆盖同输入与独立算法输入两种情况下的
  中途失败整事务回滚、cutover、rollback 和 re-cutover；测试前后隔离 schema 数均为 0，未访问 `bond_db`。

**初始化完成验收（2026-09-08 16:23 CST）**：

- 用户批准的第二次、7200秒预算初始化于15:32:33启动、16:10:50成功退出，实际2296.752秒
  （38分17秒），exit0；不是再次运行已接受的完整回测，也不是日常预测耗时。
- 只读验收通过：状态封装与payload校验和、exact `3ee3dd2334fd`、脚本/metadata/manifest、实际Python
  环境和09-08 generation五文件身份全部匹配；payload为2,583,754字节。
  envelope SHA为 `f20cc30c34a5696353c2d2226b9d59afc4b01ab1a6d73286841c2a6a619607e2`，
  payload SHA为 `0ef5a019ab3a8ec41d7ddf37af30d52c002074040524b4af8c44398d8ed5e9b2`。
- 状态锁已释放、算法进程组已退出、临时Request及runtime view已清理；原始日志与完整状态保留。
  CLI返回 `prediction_written=false`；旧W3A Registry仍active，新Registry/run/live facts仍为0，
  scheme/backtest running均为0。current仍为 `3162f70e67f53b7cdb65a3e8792d42c6fab32d15`，
  current与私有候选源码完整性复验通过；Backend active，五个installed one-shot loaded/inactive、
  timer active且模板字节一致。未操作Mac3、Registry切换、gray或systemd人工触发。
- 完成记录与stdout/stderr已取回 ignored `outputs/releases/w3a-prewarm2-20260908/`，SHA分别为
  `eeabe2f8e1b8553f59c7a1adfefdef17f3f1423389f1fa631390c20b01cc8d41`、
  `9114f470cf7427501e2770e85465ffaf7526365d7288603f5bc57b8d383b2d40`、
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`（stderr为空）。
- 初始化跟进已暂停，不再自动重跑。本次只关闭初始化；W3A原子切换、gray与真实one-shot验证仍待执行，
  不能据此宣布W3A或17个方案全部闭环。日常增量仍受120秒硬限约束，实际耗时须在后续调用中验收。

**用户批准后的单次初始化启动记录（2026-09-08 15:33 CST，历史）**：

- 预算修订 commit `8eb2df2e239dec6c3de529b99764d7be3f7316e2` 两次 archive SHA 一致：
  `7603115913fb66e0183a71523936f7e75fce778ed983c8495417f6bdafc1ae3f`。仅安装至 ECS 私有根
  `/opt/bond-factor-lab/incoming/w3a-prewarm2-20260908.STT2MG/deploy/releases/`，`activated=false`。
- ECS 在该候选实际执行公共状态/维护测试：`52 passed, 17 subtests passed`；重新只读核验原始等价输入、
  实际环境、两文件版本与正式 run278/279，各333条的证据仍通过，算法执行0、业务写入0。
  `verified-identity-and-admission.json` SHA 为
  `ffe0579c71d80d98d7c8af9b67972d23f3fd5afe476ba9b892598a7dff7a1875`，本机副本在 ignored
  `outputs/releases/w3a-prewarm2-20260908/`。
- 15:32 启动前：五个 installed one-shot 均 loaded/inactive，scheme/backtest running为0；旧W3A Registry
  active，新Registry/run/facts为0；上次算法已退出，状态目录只有空闲 `state.lock`。没有复用失败中间状态。
- 15:32:33 左右仅启动一次既有 `rebuild-blackbox-state`，full-OOS exact `3ee3dd2334fd`、
  predict-date `2026-09-08`，operator `user-approved-7200s-rebuild-20260908`。启动前检查完整7200秒预算加
  900秒保护余量早于18:00；最长运行预计17:33左右结束，不为该初始化停用任何 timer。
  初始 wrapper/CLI/algo PID为 `891093/891094/891111`，15:33读回仍运行；须按完整命令复核，不能依赖旧PID。
- 日志和结果文件在新私有根：`full-oos-prewarm.stdout/.stderr`、`full-oos-prewarm-completion.json`。
  原十分钟跟进已指向本次执行，无变化静默、失败不自动重试；成功后验证完整状态与输入身份，不把文件存在
  当成验收。此刻尚未取得最终退出或完整状态发布结论。current仍为 `3162f70e67f53b7cdb65a3e8792d42c6fab32d15`，
  未切换Registry、未写预测、未操作Mac3；上一轮1800秒失败原件保留。

**W3A 受控原件复用（2026-09-08，本地候选）**：

- receipt v2 的 `input_identity` 移到每个 target 内，包含该方案算法对照的 generation、snapshot 和五文件
  SHA-256。full-OOS 的 09-05 原件与 cons-sda 的 09-08 原件分别绑定，不因同属 W3A 而合并输入身份。
  两方案正式入库证据仍分别绑定 09-08 输入；同输入方案继续交叉校验完整七字段 Request 和五字段 Result。
- 复用输入采用临时 `native-successor-reviewed-input-v1`：顶层只有 `schema_version/wave/comparisons`，
  每项只有 `old_base_scheme_id/new_base_scheme_id/target_tenor/evidence_dir/data_dir`。
  不接受操作者提供的 Result、成功摘要或哈希。仅开放已锁定的完整 W3A 两方案，原始 identity/summary 的
  已核验 SHA 固定在临时工具中，再逐层读取 Request、两方 Result、输入、源码和实际运行环境闭包。
- 每个 target 显式记录 `native_runtime_profile` 与环境指纹。上述两份 Native 参考实际使用
  `blackbox-v2-v1` Python/依赖，不能改称另一 Native 环境；原有受控比较仍记录其真实 `native` 环境。
  路径仅用于寻找原件，不进入业务事实；原件损坏、代码/输入/环境不同都拒绝复用，不启动算法兜底。
- 独立审查发现仓储层参考运行环境例外范围过宽，已收紧为完整锁定 W3A family；复审无 Critical/Important。
  修复后本地全量回归为 `739 passed, 6 skipped, 229 subtests passed`，其中 MySQL 场景尚须用此次候选在 ECS
  随机隔离 schema 实测。该结果不等于 ECS 原件读取、receipt 生成或 cutover 已完成。
- 14:09 CST ECS 只读核验 current 仍为 `3162f70e67f53b7cdb65a3e8792d42c6fab32d15`，Backend active，
  无遗留参考计算进程，下一 installed timer 为 18:00 月频。本轮没有改动 current、systemd、Mac3 或业务库。

**W3A ECS 原件与正式事实闭环（2026-09-08 14:14–14:17 CST）**：

- clean commit `39b68ee68d40bd9e47860583176fae7c3cf29394` 两次 archive SHA 完全相同：
  `5b30289aabb040cbb445b4eef4198936f6f65322d790a494555c804d5a24f151`。仅私有安装至
  `/opt/bond-factor-lab/incoming/w3a-reuse-20260908.0TiPGj/deploy/releases/`，`activated=false`。
- ECS 实际读取两份已锁定原件并复验实际 Python/依赖、两文件版本、Native 源码和两组五文件输入，生成
  W3A receipt。每方案 333 条、五类差异均为 0；算法执行数为 0，业务库写入数为 0。
  原始工具输出 `verified-reuse.json` SHA 为
  `21f3587ab5a592a651223c4bd9d8cbc108d78fdad790849550fbcf629477c0e3`；canonical
  `deploy/native_successor_equivalence/W3A.json` 从其 receipt 对象直接提取，没有人工填成功字段。
- 同一候选对正式 run 278/279 复验当前策略/环境/版本证据，并经只读 repository 重新加载各 333 条事实。
  与 receipt 的 Request ID/三日期覆盖一致；cons-sda 同输入时额外七字段 Request 和五字段 Result 摘要一致。
  正式回测仍精确匹配现场 09-08 generation/snapshot。核验输出为同目录 `verified-admission.json`；
  本机原件副本保存在 ignored `outputs/releases/w3a-reuse-20260908/`。
- 两个旧 Native 已发布历史各 333 条，与新事实日期 grid 摘要不同，按既定规则记录
  `data_vintage_drift=true`，不改写旧事实、不把旧持久化日期当作本次同输入算法比较；两份同输入对照仍零差异。
- 本次候选在 ECS MySQL 的随机隔离 schema 中实测 `2 passed in 1.18s`；测试前后隔离 schema 均为 0，
  事务测试未访问业务库。候选与 current 严格源码树在原件读取前后保持一致，无 running scheme/backtest run。
- 此项只关闭证据复用与正式事实关联，不等于 installed 控制面 preflight、生产增量预热或 W3A cutover。
  随后先为尚无自然 Writer 的 full-OOS successor 显式预热 exact-version 派生状态；cons-sda 当前为
  stateless，不新增状态或预热。使用已验证私有候选的
  标准维护入口，不改 current，不复制正式回测的私有状态。完成后才安装切换候选、fence 与原子切换。
  这只调整预热到 current 安装之前，以缩短切换窗口；状态代码/版本/输入复验、无业务写入及停止条件不变。

**W3A 预热启动与部署准备（2026-09-08 14:23 CST 核验）**：

- 14:22:46 左右从已验证私有候选调用现有 `harness rebuild-blackbox-state`，绑定 full-OOS exact
  `3ee3dd2334fd`、`predict-date=2026-09-08`。启动前该 successor 状态不存在、Registry/run/产品事实均为 0，
  旧两方案 Registry active，全部 scheme/backtest running 数为 0；cons-sda 不声明 incremental_state。
- 维护入口使用标准五文件输入、4 GiB/8线程/1800秒冷重建限额。仅写
  `/var/lib/bond-factor-lab/state/blackbox-state/liwei_0616_5y01_full_oos_k3_div_k10_bbv2/`
  下的锁与派生状态；不创建业务 run 或 prediction。启动 wrapper/CLI/algo 的初始 PID 为
  `882347/882348/882365`，仅供核对命令，不作为未来身份；14:23 尚在运行，未取得成功退出或完整发布证明。
  日志和完成记录在上述私有 incoming 根，文件为 `full-oos-prewarm.stdout/.stderr` 与
  `full-oos-prewarm-completion.json`。已更新原有十分钟跟进，失败不自动重试。
- 切换候选仅调整 ECS W3A 部署成员：移除两个旧 Native，加入 full-OOS successor，cons-sda successor 已在
  ECS matrix 中，保持原状。逐项核对全部 106 个成员，Mac3 成员集合与所有无关项不变；独立范围审查通过。
  同步两个新 Mac-only predecessor 的既有测试期望后，全量回归再次为 `739 passed, 6 skipped, 229 subtests passed`。
  此矩阵是未激活候选，不是现场 Registry 切换；ECS current/systemd 与 Mac3 不变。

**W3A 首次预热超时停止（2026-09-08 15:03–15:05 CST 只读核验）**：

- 该次预热已于 14:52:53 CST 结束：CLI exit 1，墙钟 1807.786 秒；明确异常为算法进程达到 1800 秒
  timeout，不是等价差异或业务写库失败。完整状态未发布，stdout 为空；没有自动重试、调大预算或继续 cutover。
- 只读核验确认算法 PGID 已无成员，Request 临时目录和本次 runtime view 已由原执行器清理。
  successor 状态目录仅保留 `state.lock`，不存在 `3ee3dd2334fd.state`；保留锁文件，不手工拼接中间状态。
  两个旧 W3A Registry 仍 active；两个 successor 的 Registry、run 和产品事实均为 0；scheme/backtest
  running 数均为 0。current 仍为 `3162f70e67f53b7cdb65a3e8792d42c6fab32d15`。
- current、已预安装但未激活的 `a3d55fef70b0290ec97d18adfaea1773a8fa8b43` 和私有 `39b68ee` 三棵源码树
  严格 hash 均匹配安装记录。Backend active，18:00/月频、19:00/Actuals、翌日06:30/DataBridge及07:03/daily
  timer 仍按现场计划等待；没有改动 current、Registry、unit 或 Mac3。
- 日志显示冷路径仍按月训练 265 个配置，每批记录约 1–2 分钟。源码冷启动从 `2024-01-01` 构造截至当前
  Request 的所有 OOS 月，而先前 1076 秒的 ECS 冷启动只覆盖到 `2024-12-31`，不能用该实测保证本次更长
  初始化在 1800 秒内完成。stderr 由既有 runner 有界截断，不能从它宣称准确已完成月份或预计剩余时间。
  本次尚未进入有状态的每日增量路径，不推翻已完成的 333 条算法等价与正式回测证据。
- 完成记录 SHA：`341299801aedc12f96519fa74c024772b4aac9392313b496ca99e152ecdbe1b0`；
  stderr SHA：`b55960f831794a04c3e4a1f09bc81c3535c53777ee8db56b041690f428566364`。
  原件仍在 ECS incoming 根，逐字节核对的本机副本位于 `outputs/releases/w3a-reuse-20260908/`。
- 已暂停 `native-ecs` 跟进。待确认的最小下一步是仅把显式首次预热/故障重建作为离线维护采用独立预算
  （建议最多 7200 秒），每日增量预测仍保持 120 秒、4 GiB/8线程，算法与事实合同不变。
  该预算尚未修改或获准；不得通过本次超时自动放宽，也不重跑已接受的完整回测。确认后需修改现有维护调用
  和 runner 的重建限额、验证预算隔离，再安排一次受控重建；没有 ready state 前不切换 W3A。

**最新执行核验（2026-09-08 上午）**：

以下启动前记录保留原时间语义；10:23–10:26 的完成读回及下一项启动见后文，不把旧状态当作当前待办。

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
- 本轮未切换 current、未修改 systemd/launchd、未对业务 schema 执行 DDL。ECS current 的 12 个历史 `.pyc`
  已精确移至可恢复隔离目录；完整源码树严格 SHA 已恢复安装记录中的预期值，没有放宽校验或改写源码。
- 独立只读审查确认 W3A 还有临时接入缺口：现有 comparator runner 的批准目标仅含 W1/W2，
  W3A 会在启动前被拒绝；receipt producer 当前还会重新启动 Native/Blackbox，不提供已完成结果的复用入口。
  正式 Gate 不受此限制。下一步须最小化补齐 W3A 的受控凭据接入，并保持 receipt、正式回测及切换现场的
  各自的 generation/snapshot 绑定；不得手写成功凭据、混称 09-05 与 09-08 为同代，或直接运行必失败的旧 comparator。

**ECS 平台准备推进（2026-09-08 09:48 核验）**：

- clean commit `cd314dcac631885b2447154a2cca955fa74a66a1` 的 archive SHA 为
  `791c07a9f115fd91a805fb5cb4a2f7909fc511b5f6e3c20ae22a7b4283c92bbd`，仅安装到
  `/opt/bond-factor-lab/incoming/evidence-v2-cd314dc.rwlLlJ/deploy/releases/` 做上述隔离 MySQL 验证，`activated=false`；
  测试后候选源码完整性仍与安装记录一致。不能将此私有候选安装视为 ECS current 升级。
- current 仍为 `3162f70e67f53b7cdb65a3e8792d42c6fab32d15`。只移动前后精确核对的 12 个 `.cpython-313.pyc`，
  未移动或改写任何源码/配置/安装记录；严格 source-tree SHA 恢复为
  `69d48a34b45c69ea110890437a6e0d436e94e975acf1615386fad36cedea9849`。
  备份目录为 `/opt/bond-factor-lab/incoming/evidence-v2-cd314dc.rwlLlJ/pyc-quarantine-uhjclk5d`，
  保留原相对路径与逐文件字节，可恢复；若后来重新生成缓存，应重新核验，不能假定持续完整。
- 真实 installed unit 已按 `list-unit-files` 核对，daily 为 `bond-factor-lab-prediction-daily.service`，
  `LoadState=loaded/ActiveState=inactive/MainPID=0/Result=success`；Backend 仍 active，PID 未变，health 为 `ok`。
  不使用不存在的简写 service 名称返回的缺省 `Result=success` 作为任务健康证据。
- full-OOS 正式回测在 09:48 仍运行，尚无最终 Gate 结果。它结束且完成读回后，串行推进同批 cons-sda 的
  一次正式持久化回测；任何失败或证据身份不符都先停止，不自动重复启动。两者成功之前不执行 W3A cutover。
- 为后续受控复用核对了 ECS `forecast_env` 与 `forecast_env_blackbox_v1`：去掉环境路径注释后的完整
  conda explicit package URL 集完全相同，两侧独有成员数均为 0；这不是已生成 W3A receipt 的声明。

**正式回测推进（2026-09-08 10:23–10:26 核验）**：

- full-OOS Gate 于 10:16:54 CST 返回 `passed`，09:16:30 开始，总计 3624 秒（60 分 24 秒）。
  成功 run 为 `278`，benchmark 为 `bbv2-liwei_0616_5y01_full_oos_k3_div_k10_bbv2-c9fdfd34d9854343b84e1d260356753f`，
  exact version `3ee3dd2334fd`，输入为 `full-20260908-063331-69a69e87e803` /
  `snapshot-1d335ad33e23ca7e7c8f5b64`。标准持久化证据校验通过 code/config/manifest/当前策略与输入身份；
  数据库实际读回 333 条明细、17 个月度指标，指标样本总数 333，完整七字段 Request 与已核验 Request 集逐条相等。
- 全部回测行的状态审计均为 `state_scope=private/state_input_sha256=null`，生产 full-OOS 状态目录不存在。
  successor Registry、scheme run、产品预测事实均为 0，旧 Native Registry 仍 active；本次没有 activation、
  cutover、gray 或生产增量预热。Harness 及其算法子进程均已退出，无其他运行中 scheme/backtest run。
- 完整 Gate 日志保存于 ECS 原候选目录，已取回本地 ignored `outputs/releases/a4-offline-20260907/`；
  日志 SHA 为 `c86643bf70a02d66f1b93ca12b9c094dfd4b20f9693258b7fb54dad09995f793`。09-08 正式结果不冒充
  09-05 同代算法对照，二者继续各自绑定输入。
- cons-sda exact `b5db363bbe17` 两文件、候选源码完整性及正式 333 条 Request 已通过启动前检查；数据库无
  可复用成功正式回测，也无 successor Registry/run/产品事实。真实五个 one-shot service 均 loaded/inactive，
  无其他训练，下一 timer 为 18:00。按既有授权使用同一 c8 immutable 候选启动一次 `gate backtest --persist`，
  日期边界及 7200 秒预算不变，日志为候选根目录的 `cons-sda-persist-20260908.log`。
  初始 Harness/算法 PID 为 `836127/836145`，仅用于定位，后续必须复核命令和父子关系；尚未返回最终结果。
- ECS current 仍为 `3162f70e67f53b7cdb65a3e8792d42c6fab32d15`；Mac3 未操作。cons-sda 完成后仍须补齐
  W3A 受控结果复用和整批切换准备，两个正式回测通过不等于 W3A 已接管。

**现场基线日期**：2026-09-05 Asia/Shanghai（后续核验日期在证据段落分别记录；计划修订不刷新现场水位）

**W3A 正式回测收尾（2026-09-08 10:59 后只读核验）**：

- cons-sda 于 10:25:59–10:49:42 CST 完成，耗时 1423 秒（23 分 43 秒），Gate `passed`。
  成功 run `279`，benchmark `bbv2-liwei_0616_cons_sda_k3_div_k10_bbv2-51934d55c8aa46639d53283dd9b9ae18`，
  exact version `b5db363bbe17`；正式输入与 full-OOS 同为 `full-20260908-063331-69a69e87e803` /
  `snapshot-1d335ad33e23ca7e7c8f5b64`。只读复验当前校验策略、代码/配置/Metadata、环境及 snapshot 证据通过，
  实际读回 333 条明细、17 个月度指标、样本总数 333；完整 Request 与按正式日历重建的 333 条逐条一致。
- 两个 successor 均无 Registry、scheme run、产品预测事实和生产状态目录；旧 Native Registry 仍 active，
  running scheme/backtest run 均为 0。c8 私有候选与 installed current 的完整源码 SHA 均通过严格校验；
  ECS current 未切换，Mac3 未操作。
- cons-sda 完整 Gate 日志取回本地 ignored `outputs/releases/a4-offline-20260907/cons-sda-persist-20260908.log`，
  SHA 为 `990ac3e1498521d60dcf6672ad327836c763ef33d71e9c8ab89d0ca31e1fdbac`。
  本轮十分钟回测监测已暂停，禁止重复启动两个已完成回测。下一步仅补齐 W3A 已有算法证据的受控复用及整批
  ECS 切换准备；正式回测通过不等于 activation、gray、状态预热或接管已完成。

**cons-sda 等价原始证据补齐（2026-09-08）**：

- 已核对本任务原始执行记录和 ECS 精确路径：09-07 的 `/tmp/bfl-w2-final-verify.hE16Jp` 曾在前轮清理中删除，
  当前不存在，也未找到本地或 incoming 原始归档。历史日志保留 333/333、mismatch 0 的结论，但不能从摘要
  还原 Request/Result 原件或生成 canonical receipt。full-OOS 原始证据仍完整，不能代替 cons-sda。
- 从历史恢复的旧 comparator 直接加载生产 Phase-A pickle，未在 probe 中核验 cache 与输入的身份闭包，
  且按月末 cutoff 生成整月参考。因此不原样复用该脚本，也不把历史 conformance 声明提升为新受控凭据。
- 最小补齐方式为只读导出今日成功 run279 的完整 333 条 Request 和五字段 Result，只计算未改 Native core 的
  独立参考。Blackbox 不再执行，正式回测不再写入；Native 只从同一 09-08 冻结五文件建立私有 Phase-A，
  每条 Request 的末点单独重训、原 `liwei_0616_pit_window` 排名及 consensus/streak 单独重算。
  所有前缀复用必须先通过该输入下的 Native 训练依赖检查，不读取旧生产 cache、不调整算法或方向。
- 只读准备已核验 run279 完整输入与标准结果；导出 Request SHA 为
  `7e27c60ff08c35622d0f04059d9ab3daba2fa25c7cae32d707ef9f5f89a2f231`，Result SHA 为
  `83543e00ff040d4e6270537b9f70ee37363a08e9d782fed1eca2183baf9644c6`。
  后者是 09-08 正式结果，不与 09-07 不同输入的旧 Output hash 直接判等。
  首次准备曾因误用 full-OOS 的 alignment 字节 hash 被安全拒绝；已分别核对 Git 与 ECS immutable 文件，
  cons-sda 的正确 hash 为 `217d25154d80aa3e0cfd61c771d13adbabbb377ed7118736e307c17f979f5805`，
  两个 alignment 源文件仅末尾空行不同。修正的是临时 probe 身份锁定，不是算法源码，也未放宽完整性检查。
- 独立参考是迁移期一次性补证，不增加长期算法测试或接入框架。结果、身份和逐 Request 日志保留在 ECS
  私有 incoming 与本地 ignored outputs，至少保留到受控凭据与切换验收闭环；失败不自动重试，不清理成功原件。
- 11:19 CST 在 `/opt/bond-factor-lab/incoming/cons-native-reference-20260908.a0Jdr4` 启动一次独立参考，
  11:21 核对 supervisor `843417` 和 Native PID/PGID `843488`，实际日志已输出首条依赖检查通过；
  尚未完成全部依赖检查或最终等价。启动前五个真实 one-shot service 均 loaded/inactive、MainPID=0、
  Result=success；没有其他训练，下一 timer 为 18:00。参考预算 7200 秒、进程组 RSS 上限 4 GiB，
  Native 原有 Pool(4) 只用于独立基线，successor 不执行。十分钟监测已改为跟进本次参考，失败不重试。
- 三个临时脚本的 AST 检查通过；独立审查已修复 alignment hash 与资源终止时日志丢失两项 Important，
  fresh read 复审 Critical/Important/Minor 均为 0。逐 Request stdout/stderr 在参考进程内 exclusive 创建并
  逐行落盘，超时或内存终止后仍保留，已有日志拒绝重启。实际 ECS 字节与审查版本一致：
  probe SHA `96b4aa7682cf1a08174458b7a3bf00d4fc56bb02f09500f56a3239428953cce8`；
  Native reference SHA `9d2f69351888289cec6d5548a83618170e94e7a4dfd047088b3bccb3deeeb957`；
  dependency check SHA `bf36f63feac8786fb3f7bdeb84cf88623f57824dee6ad2e3e270a08e851b23ef`。
  ECS current 仍为 3162，Mac3 未操作；本次补证不是 activation 或整批切换。

**cons-sda 独立参考完成（2026-09-08 13:17 后只读验收）**：

- 一次参考实际返回 `PASSED_CONS_SDA_INDEPENDENT_NATIVE_AGAINST_RUN279`，总计 6763.74 秒
  （约 1 小时 52 分 44 秒），进程组峰值 RSS 2,423,361,536 字节（约 2.26 GiB），资源采样 6687 次、无采样错误。
  Native 训练依赖检查 333/333 通过，最终完整 Request ID、三个日期与方向逐行 333/333 一致、mismatch 0。
  本次 Blackbox 执行次数与业务写库次数均为 0；不是新增一次正式回测。
- 成功后独立只读复验了三脚本和 Native source closure、实际运行环境、冻结五文件、Request、Native 私有
  Phase-A 与 dependency/comparison identity、完整标准 CSV，以及数据库 run279 的当前 Gate 证据与全部333行。
  Native Result 与正式导出 CSV 的 SHA 同为
  `83543e00ff040d4e6270537b9f70ee37363a08e9d782fed1eca2183baf9644c6`；结果没有依赖不同输入的旧摘要。
- supervisor、Native 及整个进程组均已退出，私有 runtime view 的 active/debris 均为空；ECS current 仍为
  `3162f70e67f53b7cdb65a3e8792d42c6fab32d15`，current/c8 候选源码完整性均与安装记录一致。两旧 Native
  Registry 仍 active，两 successor Registry、scheme run、产品预测事实仍为 0，生产状态目录均不存在；
  running scheme/backtest run 均为 0。未 activation、gray、预热、切换或修改 systemd/Mac3。
- 原始 CSV、身份、依赖和比较报告及完整日志已从 ECS 取回
  `outputs/releases/cons-reference-20260908/evidence/`，保持 ignored；未复制 Numba cache 或大体积运行视图，
  远端原始证据与私有 Phase-A 均保留，未删除。摘要 SHA
  `84cc36e8948527509481daa1416bd1cb163ca6bad9d29f191d6e465083c2f7d2`；identity SHA
  `61af2028b5222be9b1aa4ce60798c003d68334fef4b257c8a0acab3c78986a31`；dependency report SHA
  `0bb0b992556e6a409554d7f52f6364c0aae0a5d0c6967f788355e9d5fdf0cdd3`；comparison report SHA
  `f88ded981d8a919ebf3dc8447d94810b4c8c991ad3bbfa6a4cbfea0bfbbb2db6`。
- 本轮参考监测已暂停。下一步是复用 full-OOS 的 09-05 原始等价与 cons-sda 的 09-08 原始等价，
  各自保留真实输入身份，补齐受控 receipt 入口，再准备 W3A 原子 ECS 切换；不得为迁就 wave 顶层单一
  generation 结构重跑已经通过的算法，也不得把两份等价证据伪写为同代。当前仍未接管，不能宣布全局闭环。

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
五文件 SHA-256、Native/Blackbox 环境指纹及 old/new code hash，各方案输入放在自身 target 的 `input_identity`。
普通 comparator 使用同一七字段 Request CSV 直接执行两侧程序；已审查 W3A 则只读复用哈希锁定原件。
两种入口都读取两份标准五字段 CSV 结果，逐行核对 Request 顺序、ID、三个日期和方向；任一差异直接拒绝
生成 receipt。preflight 会在
数据库只读快照中锁定 successor 的完整
持久化 backtest fact 集，并从每行 `source_row` 重新推导完整七字段 Request 摘要、Request 数量、ID/三日期摘要
和标准五字段结果摘要。receipt 的 Native/Blackbox 结果摘要必须相等，五类 mismatch count 必须全为 0。
两类输入的 Request 数量、ID/三日期覆盖必须完全一致；输入相同时还要求三个 cutoff key 和方向摘要与正式事实集
完全一致，输入不同时分别保存摘要，不将数据修订误认为算法改造差异。规范化 receipt
中的 `native` 参考环境指纹还必须等于现场 `forecast_env` 的 conda explicit package 集指纹；仅完整锁定 W3A
允许记录实际使用的 `blackbox-v2-v1` 参考环境，此时必须匹配 successor 环境，其他 wave/身份拒绝该例外。receipt 进入 plan SHA，
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
| W3A | `liwei_0616_cons_sda_k3_div_k10_bbv2` | 五文件中算法所需因子；仅进程内 cutoff-keyed cache | feature 2025-01-02..2026-05-22；333 条 | `9cebc6eab7df69ed48e68163fc0cfc5229ad616fda4989924e2b34d78274c250` | PERSISTED_BACKTEST_VERIFIED; INDEPENDENT_REFERENCE_VERIFIED |
| W3A | `liwei_0616_5y01_full_oos_k3_div_k10_bbv2` | 五文件 + 方案私有增量 Phase-A 状态；连续 full-OOS 排名 | 完整333条 feature 2025-01-02..2026-05-22 | `ad9bdacf5063a427ecc8b70852e045f4822ba9af1b6d8fcd171cd2d779e95103` | ECS_ALGORITHM_EQUIVALENCE_ACCEPTED; PERSISTED_BACKTEST_VERIFIED |
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
当时 predict 重建上限仍为 1800 秒；2026-09-08 后续明确授权改为最多 7200 秒的离线维护预算。
RSS 4 GiB、数值线程 ≤8、无子进程和五字段零差异要求不变，日常增量 predict 仍最多120秒。
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

普通 `prepare-equivalence` 的 `native-successor-comparison-input-v1` bundle 顶层只允许
`schema_version/wave/generation_id/data_snapshot_id/data_dir/comparisons`；`data_dir` 必须包含精确五文件，工具现场
计算其 SHA-256。每个 comparison 只允许
`old_base_scheme_id/new_base_scheme_id/target_tenor/requests_path`，Request CSV 必须是该 successor 的完整正式
Request 集。工具不接受 operator 提供的 Native 或 successor 结果文件：它在 `forecast_env` 中调用映射锁定的旧
Native core runner，在 Blackbox frozen profile 环境中调用 canonical successor delivery，两边共用同一 Request
artifact 和五文件目录；输出只存在于私有临时目录，逐行比较后立即销毁。receipt 的 comparator source SHA 同时
覆盖主控程序和 Native runner，old/new code hash 由 canonical config 重新计算。per-scheme wave 还必须传
`--old-scheme-id <old_base_scheme_id>`。工具独占创建 canonical receipt，已有文件时拒绝覆盖；如需重做，必须先由
operator 明确撤销旧候选证据并形成新的 clean commit，不能静默替换。

W3A 已完成结果仅使用本文顶部定义的 `native-successor-reviewed-input-v1`，不再执行上述算法启动路径。
从不可变私有候选验证时调用同一个 receipt builder，把其生成对象直接带回开发树，核对来源后随下一 clean
archive 发布；禁止为了生成 receipt 现场修改不可变 release，也不以调整序列化格式为由重跑算法。

每批顺序固定：在候选树生成受控 receipt 并更新 deployment matrix → 形成 clean commit 和 deterministic archive →
对尚无自然 Writer 的 stateful successor 使用同代码/版本的已验证候选及目标机 generation 预热并只读检查 →
把该 archive 安装为目标机 current 并复核状态身份 →
fence cadence timer 并等待 one-shot 退出 → 从 current release 重做 preflight → 使用其 plan SHA 单事务切换 →
单 batch gray 区间并推进对应私有状态 → 恢复 timer → 核对唯一 writer、Dashboard、运行前后状态快照与
next trigger；按下述控制面复用边界决定是否需要再次人工触发真实 systemd one-shot。
preflight 之前的安装和预热只部署代码、写可重建
派生状态，不授予 Registry 或业务事实写入权。

cutover 单事务必须：

1. 按稳定顺序取得 old/new advisory lock 和 Registry/version/fact 行锁；
2. 验证 old/new 的 task type、target tenor、target rule 业务格子覆盖精确相等，并把跨数据 vintage 的历史日期
   grid 数量、摘要和 drift 标记纳入计划；
3. 验证 successor exact version 的成功持久化回测及 evidence；
4. insert-only 发布或验证复用同 exact version 的 successor backtest facts；
5. 激活 successor version/Registry，archive old Registry，retire old exact version；
6. 同事务权威读回；任一步失败全部 rollback。

gray 区间从 `target_date >= 2026-06-01` 到首个真实 one-shot 验收 Request 的 target 前，单 successor 只启动一个 batch，每条 Request
独立 cutoff；任一业务键已存在则整组拒绝，一次 repository 事务提交。失败立即反向 cutover，不开放 timer。
用户改为人工触发真实 one-shot 后，上界必须按实际调用日的权威 Request 计算，不能机械沿用下一 timer 日期而
提前用 gray 占掉模拟运行的业务键；若不人工触发才使用下一自然目标。不得伪造调用日或把 skipped 当作成功新运行。

不等待日频、周频或月频的自然触发次数。每个 cadence 的真实 installed systemd one-shot 控制面至少验收一次；
命令、WorkingDirectory、EnvironmentFiles、运行用户和 Runtime Profile 必须与 timer 触发完全相同。该次
service 成功退出、唯一 `scheduled_live` run、业务键唯一、old 无新 run、Dashboard 与 journal 相互证明。

**ECS 后续纯方案批次复用规则（2026-09-09，沿用用户“模拟验证即可”的授权）**：已验证 cadence 的执行代码、
日期/输入/状态协议、repository、Runtime Profile 与 installed unit/环境参数均未改变时，复用已经通过的
控制面证据，不为每个新算法再次启动整套日频方案。每批仍独立完成完整等价、正式回测、真实环境下的当日
predict 性能/状态验证、fresh preflight、原子切换、全区间 gray 与数据库/Dashboard/唯一 writer 读回。
不再人工触发时，gray 上界用下一自然目标；不把模拟或 gray 记作该 successor 已发生 `scheduled_live`。
复用依据及未变执行边界只记入本迁移 Markdown 和现有证据，不增加表、长期框架或第二套调度入口。
若相关执行边界改变，或此前控制面证据不成立，则重新做受影响 cadence 的真实入口验收；不能套用复用规则。
W2 已启动的整套入口仍须正常完成并验收，不中途取消。此规则不放宽 Mac3 的独立晋级授权或验收。

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
successor 唯一键、`run_id/backtest_run_id` XOR、gray/backtest 区间、Actual 水位、其他 active 集合、Dashboard
和 timer；真实 one-shot run/journal 按5.4节完成本次验证或核对可复用证据。任何非计划变化都使 wave 失败。

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

当前 ECS-only 阶段完成条件：W1-W3 的 21 个 successor target 在 ECS 完成受控替换、灰度、当日预测模拟、
5.4节定义的控制面验证或证据复用，以及回滚验收；Mac3 版本、业务库、调度及域名保持原状。以下为未来全项目完成定义，不是本阶段继续操作 Mac3
或删除其仍在使用的 Native 路径的授权。

必须同时满足：W1-W3 的 21 个 successor target 全部通过合同/等价/性能并在 ECS 接管；同一 archive 晋级
Mac3；W4 九个 Mac3-only binary-bundle successor 通过 manifest/ABI/合同/等价/性能；Mac3 30 个 target 完成
切换与真实 launchd one-shot 模拟；old Registry 全 archived 且无新
Native run；Native 可执行路径与临时迁移工具已删除；全量、架构、isolated MySQL 和 migration recovery 测试
通过；所有 stateful successor 在 ECS/Mac3 各自拥有 exact-version ready state，逐日增量、历史修订失效、崩溃
恢复和单 Writer 验收通过；旧 Liwei publisher/consumer cache wave 已删除且通用 Blackbox state 不引用 Native；
025 已删除两个 confidence 列；Dashboard、Actuals、其他 Blackbox 和调度控制面无非计划变化。

当前全局状态仍为 `IN_PROGRESS`，各批实际进度及证据以本文顶部按时间记录的执行状态为准。
ECS 已闭环批次不因后续方案的差异诊断而重复执行；未通过等价与性能的候选不得外推为已完成。
W4 的 Mac3-only binary-bundle 架构已获确认，但当前 ECS-only 阶段不操作 Mac3；不能用旧 adapter、
未纳入 manifest 的二进制、旧水位、旧 Native cache 或文档声明冒充闭环。
