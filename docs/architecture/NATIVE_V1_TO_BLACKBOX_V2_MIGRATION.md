# Native → Blackbox V2 迁移：ECS 原 ID 接管计划

**文档状态**：`CURRENT — ECS 原 ID 接管完成；先清理再准备 Mac3 同包晋级；W4 保持现状`

**最新批准日期**：2026-09-12。用户已缩减本轮范围和验收，以下第 1 节为唯一当前执行主线。
第 2 节及以后保留已有实施证据和旧设计参考，不能据其恢复历史回测、结果补入、W4 改造或全量 Native 删除。
旧过程和完整证据索引通过
`git show 938d1c4:docs/architecture/NATIVE_V1_TO_BLACKBOX_V2_MIGRATION.md` 追溯；旧命令不是当前操作指令。

## 1. 第一性原理与授权范围

### 接管后的当前清理顺序

用户在 ECS 十七个原 ID 接管完成后要求：先完成 ECS 陈旧方案代码清理，验证后再一并同步 Mac3。
以下清理顺序优先于后文已完成的五方案操作步骤；不重复那些算法验证和版本切换。

**后续明确授权**：用户进一步要求删除已替换 Native 实现与闭环一次性测试。十七个原方案的
204 个附件及对应声明已删除并完成 ECS 发布，42 个 Blackbox 交付脚本/Metadata 字节不变；
旧 Liwei cache 实现和无在用消费者的 Native comparator 同步退役。W4 九方案与历史数据库不动。
T1/T5 旧 `model_muti_0529` 批次的输入 CSV、退役 SHAP `.source`、父级 manifest 和 README
共四个无运行消费者文件（19,745,668 字节）同时退出当前代码线；原件保留于 Git 与旧 release。
同目录 `weekly_average_0529` 以及日/月 0629 的完整 W4 包、manifest、参数与样本均保留。
因 config hash 真实变化，部署前必须使用固定十七方案的附件退役事务切换新 exact；
不得忽略 hash、伪改旧 Gate 或重跑历史以规避这个步骤。

最小执行顺序：冻结 source/candidate archive → 围栏 ECS Backend 与日/周 Writer →
只读 preflight → 原状态仅调整 exact 封装（payload 与原 input 不变）→ 围栏内 release 切换 →
新 preflight → 整组版本事务 → 标准准入、状态与全量事实/Dashboard 读回 → 恢复调度。
文件与数据库之间没有原子事务；中间失败保持围栏，按新 preflight 恢复旧版本及旧 release。
旧状态不覆盖，候选状态已有不同内容拒绝；不将状态封装当作新算法运行或新历史预测。
回滚分别验证旧、新状态的身份与完整性；不要求已经正常推进的新状态仍与旧 payload 相同。
先在候选 release 与维护围栏内回滚数据库，再恢复旧 release；两份状态均不覆盖。

1. 删除当前配置与迁移入口都不调用的 17 个 Native 专属回测文件（十个 Liwei runner、
   ALL-K10 公共 runner、两个 V28、三个周点和一个 T1/T5 runner）。旧 immutable release 保留原件。
2. 删除 W2/W3A 已撤销的 `preserve-live` 历史补入入口、专属 repository 实现及一次性测试。
   已完成历史事实与来源校验保持不变；prepare/cutover/rollback 及普通回测公共能力保留。
3. 首批清理不修改算法、Metadata、config 或 exact；当前附件清理批次仅 config/exact 改变，
   算法与 Metadata 不变。全量回归和独立审查后，核验全部 active 方案准入及原事实不变再发布。
4. 十六个停用临时 canonical 目录仍被同 ID 迁移校验读取，暂不删除。
   十七方案的 204 个 Native 附件已从候选删除，按上述真实版本及状态流程发布。
   T1/T5 canonical delivery 中的 `_bbv2` 文件名属于有效交付，不能误删。
5. 保留 W4 九方案、公共 Native 输入/执行/维护路径和完整 source package；保留当前回滚 release、
   数据库事实与外置证据。Mac3 晋级以本机 preflight 和 ECS 验证过的同一 archive 为前提。

本轮不把这些暂留项宣称为已清理，也不以重新回测或新增兼容框架消除依赖。

当前附件退役候选本地验收：全量 1201 passed、38 skipped、241 subtests；固定十七方案的
真实隔离 MySQL 切换/回滚及故障注入已单独通过。独立终审无 Critical/Important。
新增的附件退役事务/状态测试只服务本次受控发布验收，不作为永久算法测试。
完整 byte/hash 证据外置于 `native-attachment-retirement-20260912`；部署完成需另记现场读回，
不把本地通过当作 ECS 已升级。

#### 附件退役与一次性测试清理发布验收（2026-09-12）

`f066b8e` 与 `fb80439` 两笔清理提交合计 247 个文件变更，净删除 91,977 行。
附件退役事务所在 ECS release 为 `fb804394b1f3daa268a56f324746fb9147ee249f`，两次 archive 构建字节一致，
SHA-256 为 `a596dbfe026230431b0007f293cb7ec6aa30c39eed7de6c597f423a6ec6724d1`。
`f066b8e` 仅预安装，未切为 current。previous 保持清理前的 `5e4bc98e1796006ebd4e2d760d8c49e467b3e607`。

- 19:00 Actuals 于 19:00:03 自然启动、19:00:38 成功退出；之后才围栏 ECS 日/周 timer 与 Backend。
- 7 个增量方案仅生成新 exact 状态封装；旧状态文件、payload、原 input 完全保留，算法调用为零。
- 十七个原 ID 整组版本事务成功，新 exact 全部 active、原 exact retired，Registry 和旧版本其余证据不变。
- 84 个 active base 全部通过标准执行准入，Dashboard 88 个 target 业务内容不变；25,715 条预测、
  5,689 条 run、111 条回测 run、24,750 条回测预测及 Harness/其他事实表全量摘要不变。
- Actuals 原记录、所有原状态文件均不变；仅增加上述 17 个版本和 7 份封装，没有历史重算、复制、覆盖或删除。
- 回滚只读 preflight 通过；恢复 Backend 后实际进程 cwd 对应 current，健康检查正常，日/周 timer 已恢复。
  三个预测 timer、DataBridge 与 Actuals 均 active，installed unit 未改变。
- before 读回 SHA 为 `b6f4870713b46e5ed525c1caac698a9208c5b372bd03df62adf96d44f1943dde`，
  after 为 `fc882d0961c9f07198259e348ed5d61b5b11cd278f41ee9b1153ec020d7c1c64`；
  原件、state 转换和 cutover/rollback-preflight 回执保存在双端私有 `native-attachment-retirement-20260912`。

**退回带附件的旧 exact 边界**：不能只把 current 链接切回 `5e4bc98`；必须先围栏 Writer/Backend，在当前候选中重新生成
rollback preflight 并执行版本反向事务，再恢复 `5e4bc98` release，验证后恢复服务。两个版本状态分别校验，
不覆盖已经推进的新状态或旧状态，不删除已经发布的预测。

Mac3 current 仍为 `b736d3b21b1c57455cf36d1cdcaa22b00fdda455`，本轮未同步或操作 launchd。
W4 九方案与其 198 个 source package/manifest 文件保持不变。十六个无 Writer 临时 canonical 和
仍被版本/回滚控制使用的过渡工具尚未退役，不能据本节宣称整个仓库或双机项目已完全闭环。
本轮发布验收已完成，两份真实 MySQL/状态转换专项测试从后续清洁代码线移除；原测试与通过记录
保留于 `fb80439` 提交及其已验证 archive。公共合同和数据安全测试长期保留。
删除这两份临时测试后的完整回归为 1167 passed、36 skipped、241 subtests；相对用户提出清理时的
1253 个通过用例净减少 86 个。运行代码、全部方案 config/exact 与状态都不因测试退役改变。

最终清洁 release `1a09ec89b40154359d6225e0d2c061b603c45169` 已在 ECS 发布，previous 为
`fb804394b1f3daa268a56f324746fb9147ee249f`。确定性 archive 两次构建一致，SHA-256：
`3180b90470d2ea3d374661440b36ac9e15c56a9cfc432a7c54b0ac4bba1b4934`。
before/candidate/after 验证全部 active exact、历史事实、Registry、版本表、Actuals、状态和 Dashboard
业务摘要均不变；Backend 实际 cwd 对应 current，health 正常，五个 timer 正常。
after 读回 SHA 为 `bda94324db7684ebddbcd033d9b4c2a1ebed4fef09520b8ac6d0dbe3772fa7f0`，
完整原件外置于双端 `native-test-retirement-20260912`。此次不执行任何数据库事务或算法。
current 和 previous 均为无旧 Native 附件、同一 active exact 的源码方案 Blackbox release；
在这两个 release 之间普通回滚只需维护围栏与既有 installer，不需要上述版本反向事务。

#### 首批清理与 ECS 发布验收（2026-09-12）

清理提交 `03e63e2`、`5e4bc98` 共移除 17 个旧回测文件、历史补入控制模块、专属仓储实现和五份测试，
并删除无调用者的 source-evidence 路径 helper；净减少 11,791 行。没有删除历史数据、旧 release 或证据原件。
最终完整回归为 1253 passed、36 skipped、243 subtests passed；独立代码审查无未解决问题。

ECS 已部署 `5e4bc98e1796006ebd4e2d760d8c49e467b3e607`，两次 archive 构建字节一致，
SHA-256 为 `34088f0a1c24445f8a847a5010a2e96f2b93106269cf8f767b74c57af8b5079d`。
previous 为已经全部源码方案 Blackbox 接管的 `c74c538ffad49980fa97e51fda41e0c129e736f4`；
本批未变 scheme exact 或状态，因此不需要数据库逆向切换即可使用该 previous 恢复清理前代码。
`03e63e2` 只预安装，未切为 current。

- 84 个 active base 的 exact/批准摘要全部不变，其中包含本次已迁移的 17 原 ID；Dashboard 88 个 target 业务内容不变。
- 25,715 条预测、5,689 条 run、111 条回测 run、24,750 条回测预测，以及 Harness、Registry、版本、
  Actuals 和派生状态全量摘要均不变。发布验证未调用算法、未写业务库，零 running run。
- Backend 实际 cwd 与 current 一致，health 正常；三个预测 timer 已恢复，DataBridge/Actuals timer 保持 active。
  未改 installed unit，未影响当日 19:00 Actuals；Mac3 current 仍为 `b736d3b2…`，未同步或操作 launchd。
- 私有证据位于双端 `stale-cleanup-20260912` 工作目录；after 读回 SHA 为
  `3c716445404735b8fce3e092bd481dedfb8e1bf7f512f79f3139c515b1173f0e`。

该首批发布仅完成无在用依赖部分的清理；后续附件退役批次及 Mac3 晋级状态以上方当前主线为准。

### 已完成的原 ID 接管范围

同一个业务方案、同一份历史记录，只替换执行包装并完成 ECS 原 ID 接管。
保留 `scheme_id/base_scheme_id`、Registry ID 和全部历史，以真实新 exact version 区分实现。
本轮完成 W3C 三个和 W3D 两个 ECS 方案；此前已经接管的十二个方案不重复计算或切换。

- W4 九个加密方案不改造，继续 Mac3 现状；本轮不操作其 release、launchd 或数据库。
- 不重新回测历史区间，不重新生成、复制、覆盖或删除历史预测。W1 的 42 条历史结果补入已停止，
  未完成代码保留在私有补丁而不进入发布；此前已经完成的 W2/W3A 物化保持原状，不回删。
- 已有 Native 结果就是基线，既可来自现有正式事实，也可来自此前已完成的真实验证；
  保留其版本、日期、输入及来源，生成只读比较视图不冒充新执行。算法未改不再证明全部历史日期。
- 不再执行原计划的 333 条区间对照、内部 grid 专项证明或多遍训练；已停止的 W3D 历史进程不重启。
- 双机数据库、DataBridge、状态和调度独立；不复制数据库主键、run、Actuals、回测或 Harness 历史。
- 不改变域名、DNS、Nginx 流量指向或 SSH 隧道。不迁移其他 Blackbox 的 `legacy_v1` 输入。
- 推送 `codex/develop`、创建到 `master` 的 PR；不合并、移动或推送 `master`。
- confidence DDL 不作为本轮五方案接管前置条件；两端仍须分别独立确认。
- 不重复有效计算，不覆盖原历史，不降低 cutoff 或改变算法结果，不把文件上传当成发布完成。

剩余五方案只执行以下步骤：

1. 固定最终 Blackbox 包和原 `scheme_id`；校验包内容及真实 exact version。
2. 每方案选择一份已有 Native 对照结果，记录真实 Request 与输入来源，不新算 Native 历史。
3. Blackbox 执行一次相同 Request，三个日期与方向完全一致；不扩展为全部历史日期证明。
4. 完成基本合同与标准入口检查；复用现有 Harness 证据结构和 repository，不新建验收平台。
5. 在受影响 Writer 的维护围栏内，使用现有事务原子切换原 ID 的 active version；旧事实只读。
6. 每方案模拟执行一次，确认唯一 Writer、Dashboard、Backend、release 与 timer。
   已有业务键只读比较，不将模拟写成新的自然预测或覆盖原值。
7. 恢复调度并读回实际状态。发布仍使用 clean commit 确定性 archive，推送 develop 和更新 PR，master 不变。

算法对照不一致、标准入口失败、无法唯一 Writer 或版本/输入身份漂移时停止对应方案并报告真实原因；
不能自行扩展为重跑全历史。完成定义是 ECS 十七个原方案接管及本轮发布验收，不包含 W4 改造、
历史结果搬迁或删除仍被 Mac3 使用的 Native 框架。

### 当前五个最终包的单点结果

五包已固定原 ID，并各完成一次标准 `predict`；三个日期均为 predict 2026-08-25、feature 2026-08-24、
target 2026-08-31，方向均为 `+1`，与所选已有 Native 结果一致。没有新算 Native 或历史区间。

| 原方案 | 候选 exact version | 单次耗时 | 私有状态行为 |
|---|---|---:|---|
| `liwei_0616_5y_auc_static_all_k3_div_k10` | `811b31ffa3c3` | 8.604 秒 | 639 点复用，只计算本 Request 一个点 |
| `liwei_0616_5y_auc_yearly_all_k3_div_k10` | `daee97949294` | 8.872 秒 | 同上 |
| `liwei_0616_5y_ic_yearly_all_k3_div_k10` | `d7f10ff5bfe8` | 7.776 秒 | 同上 |
| `liwei_0616_7y01_cons_say_k3_div_k10` | `79457f27982c` | 82.076 秒 | 无持久状态 |
| `liwei_0616_7y03_cons_all_k3_div_k8` | `fdef50ae2219` | 80.584 秒 | 无持久状态 |

算法脚本与已有草稿逐字节相同，Metadata 仅改原 ID；66 个 Native 附件原字节保留且纳入摘要。
W3C 旧内部 639 点前缀不变，三份原 Native 末点证据复用；W3D 基线来自 ECS 已有
prediction 3752/3753、run 3823/3824，不伪造旧 Request ID 或当年进程凭据。
W3D 原输入与候选五文件有历史修订差异，已如实记录；全部证据只声明本次单点回归，
`same_input_equivalence=false`、`historical_equivalence=false`，不泛化为全算法同输入等价。

原件在 `outputs/final-five-single-request-20260912/<原ID>/`，已上传 ECS 同名 incoming 私有目录，
逐份 SHA 核验一致。包、打包证据与最小同 ID 控制层均已独立审查，无 Critical/Important。
控制层只接入固定的单 Request 验收原件，不再前置第二次 ECS 算法执行；W3C 私有 seed 未审核时拒绝准备。
完整回归为 1298 passed、46 skipped、243 subtests passed；新增两项真实隔离 MySQL 切换/回滚测试通过。
五方案首次围栏切换因 W3D 标准入口超时受控恢复，该次尝试没有新增接管完成的方案；后续完成情况见下文。
W3C 的 ECS 状态须从 ECS 本机已有 Native 派生缓存进行受控转换，不以本机冻结输入状态冒充 ECS ready。

三份 ECS 私有 seed 已完成受限转换及独立审核：复用本机原缓存的 649 个内部点至 2026-09-04，
当前标准模拟只计算 9/7–9/11 五个必要后缀点；状态封装各约 10 秒，零拟合、零预测调用、零生产发布。
原缓存的 unqualified/NON_PRODUCTION 来源如实保留，三份 spec 与十二份 baseline 参数及数值环境指纹
已只读复核，不将派生状态封装当作新增历史等价。原件及补充来源证明在
`outputs/w3c-ecs-state-readiness-20260912/`，接管入口只接受固定 SHA 的 ECS 原件。

### 本轮 ECS 实际执行与恢复结果

候选提交 `a3352417fa69ee7ab900e1668f32268e3e7d1df3` 已推送 develop，archive 两次构建字节一致，
SHA-256 为 `50eb5d0fe18c378c52279b05a8cf3df547b9441877b5565f3ad9b03d98129d05`。
W3C/W3D 分别完成原 ID 原子切换；prepare 未启动算法，仅新增各五条 Harness/Gate 审计。

| ECS 标准模拟 | 结果 | 实际耗时 | 后续处理 |
|---|---|---:|---|
| 5Y AUC static | 通过 | 48.240 秒 | 私有派生状态发布并保留 |
| 5Y AUC yearly | 通过 | 44.727 秒 | 同上 |
| 5Y IC yearly | 通过 | 44.513 秒 | 同上 |
| 7Y01 SAY | 120 秒超时，无 Result | 包含结束处理 122.375 秒 | 未写预测/run，无自动重试 |
| 7Y03 ALL | 未执行 | — | 本批停止后没有继续启动 |

W3C 当前 Request 为 predict 2026-09-12、feature 2026-09-11、target 2026-09-18，方向 +1；
每份状态复用 649 个内部点，仅推进五个必要后缀，实际 6 线程、峰值 RSS 约 0.71 GiB。
独立审核核实了前缀数组字节不变，不仅依赖算法日志。没有生成或修改已发布历史预测。
W3D 7Y01 在一个当前 Request 内计算多个模型族，第一组 265 configs / 2 seeds 已约需 1.1 分钟，
到后续 3 seeds 组时触发标准 120 秒上限；峰值 RSS 约 0.98 GiB、6 线程，不是完整历史回测。

停止后恢复五个原 Native canonical exact，并由受控 installer 恢复
`6c818d91bccf706ccc66e515b4ba30cee414ea61`。W3D 恢复使此前 W3C 批准摘要过期，
首次 W3C rollback 被拒绝、无部分提交；维护围栏始终保留。期间一次提前切回旧 release 后，
重新通过 installer 切回候选、刷新 W3C preflight、成功恢复版本，最后再恢复旧 release。
不绕过摘要校验，不修改 immutable release。所有失败尝试与安装审计保留。

恢复核验：25,715 条预测、5,689 条 run、111 条回测 run、24,750 条回测预测摘要不变，
Actuals、Dashboard 及原 Harness 行不变，零 running run；五原 ID 各唯一 Native active version。
Backend 与日频 timer 已恢复，月频 18:00、Actuals 19:00 等原触发未更改；Mac3 未操作。
该次恢复后 installer 的 previous 指向失败候选 a335241，当时不是已通过准入的普通回滚目标；禁止盲目切 previous。
W3C 状态及通过证据保留，不重复执行；W3D 需要先确定标准调用预算或最小提速措施，再重新安排维护窗口。
该次失败后的自动推进已暂停，不得据旧尝试自动重试算法或扩大为历史回测。当时接管为 12/17 方案、16/21 target。

完整实际原件在本机及 ECS `final-five-single-request-20260912` 私有证据目录，
包含切换、失败模拟、刷新摘要、成功回滚、恢复后数据库与控制面读回；该次失败没有被记为闭环。

### 已授权的单次预算调整

用户随后明确接受单次调用稍长，但不接受半小时、一小时级别。因此本次仅将 W3D 两份
`schedule.timeout_sec` 从 120 改为 300 秒；算法、Metadata、日期、线程数和输入语义均不变，
不修改全局 Runtime Profile，不自动继续放宽预算。新 exact 为 7Y01 `4693f5417964`、7Y03 `3ac41bdd36c6`。
原 Native 单点对照及执行回执不改写，通过明确的 timeout-only 身份转换复用，不重新跑历史或 Native。
W3C 的三份真实成功标准调用与生产派生状态须校验原摘要、当前 exact/input/environment 与状态完整性后复用，
不再启动算法。新的 prepare、切换、W3D 两次标准调用与前后读回采用独立 budget 前缀证据，旧尝试永久保留。
接管仍沿用维护围栏、逐批新 preflight、原子 repository 和失败恢复；全部准入就绪前不停止服务。
本轮修改后完整回归 1325 passed、47 skipped、243 subtests；三项真实隔离 MySQL 检查另行通过，
包含已退役 exact 受控重入及数据库原生时间类型下的旧 Gate 摘要核对。旧业务事实不参与任何改写。

### 最终 ECS 接管验收

包含独立审查修复的提交 `c74c538ffad49980fa97e51fda41e0c129e736f4` 已推送并部署 ECS，
两次构建 archive 字节一致，SHA-256 为 `15c92c38b62dab1bc07fa3885168ada59e1dd1517f02ea4eeb04fdc2baf1775c`。
运行中的 Backend cwd、current 与该提交相同，previous 为 `6c818d91bccf706ccc66e515b4ba30cee414ea61`。
此前预安装的 4641ca2 未包含最后一项审查修复，从未切为 current，不作为本轮成功发布。

- W3D 两个新 exact 原子接管后，各执行一次当前 Request：7Y01 256.532 秒、7Y03 259.269 秒，
  均通过 300 秒预算，实际 6 线程、峰值约 1.05 GiB；Result 为 predict 2026-09-12、feature 2026-09-11、
  target 2026-09-18、方向 +1。无数据库写入、无状态发布，不伪称自然预测。
- W3C 三个原 exact 原子接管，复用已通过的标准调用与原生产派生状态；本轮追加算法执行数为零。
- 两批最终回滚只读预检通过。Backend 与日频 timer 已恢复；日频下次 9/14 07:03、周频 9/19 11:30，
  月频当日 18:00、Actuals 当日 19:00、DataBridge 次日 06:30，未改 installed unit 或其他 cadence。
- 全量读回确认 17 个原 ID、21 个 active Registry target 全部为 Blackbox，各原 ID 只有一个 active exact。
  迁移临时身份没有获得 Writer；其他方案的 Registry/version 未变，Dashboard 88 个方案的业务内容不变。
- 本轮前后 25,715 条预测、5,689 条 run、111 条回测 run、24,750 条回测预测及其余事实表摘要不变，
  Actuals 不变；原 385 条 Harness/1,928 条 Gate 逐行不变，仅增加本轮五条 Harness/五条 Gate。
- `budget-verified.json` 已通过，after 读回 SHA 为
  `5be18f86851097254de35075d029e8746cfbca73eae5edbdad43f819b09ec116`，验收时零 running run。
- 最终独立实物复核无 Critical/Important；核验报告 SHA 为
  `d2d70dd498fc70b8558cb8135f9937a9fc22127139f08a78ab596e5b41199ef1`。

**本轮 ECS 17/17、21/21 接管完成。** 后续文档提交只记录验收，不伪称已替换运行 archive。
develop 已推送，PR #57 交付但不合并 master；Mac3 release、W4 九方案、域名、隧道和 DDL 均未操作。
旧自动推进保持暂停，不继续旧历史补入、临时身份删除、W4 改造或 Native 框架清理步骤。
完整输入、请求、Result、切换与恢复原件保存在本机/ECS 私有 `final-five-single-request-20260912` 目录，
本轮新证据使用 budget 前缀，旧失败和成功原件均保留。

## 2. 精确身份、目标与部署清单

下表按本轮批准时的 `deploy/native_to_blackbox_migration_v1.json` 只读核对。
“迁移临时 ID”只定位已有交付、证据和清理对象，不能继续激活为最终业务身份；尚未创建的无需创建。
最终 Registry ID 一律保留 `{原 base ID}__h{原事实 horizon}__{target_tenor}`。

| 批次 | 最终原 base ID | 任务 / target | 原事实 horizon / Metadata horizon | 迁移临时 ID（若已存在） | 部署顺序 |
|---|---|---|---|---|---|
| W1A | `t1_daily` | T+1 / 5Y | 1 / 1 | `t1_daily_5y_bbv2` | ECS → Mac3 |
| W1A | `t1_daily` | T+1 / 10Y | 1 / 1 | `t1_daily_10y_bbv2` | ECS → Mac3 |
| W1A | `t5_daily` | T+5 / 3Y | 5 / 5 | `t5_daily_3y_bbv2` | ECS → Mac3 |
| W1A | `t5_daily` | T+5 / 5Y | 5 / 5 | `t5_daily_5y_bbv2` | ECS → Mac3 |
| W1A | `t5_daily` | T+5 / 7Y | 5 / 5 | `t5_daily_7y_bbv2` | ECS → Mac3 |
| W1A | `t5_daily` | T+5 / 10Y | 5 / 5 | `t5_daily_10y_bbv2` | ECS → Mac3 |
| W1B | `weekly_5y_direct_0529` | weekly_point / 5Y | 6 / 1 | `weekly_5y_direct_0529_bbv2` | ECS → Mac3 |
| W1B | `weekly_7y_cross_d_overlay_0529` | weekly_point / 7Y | 6 / 1 | `weekly_7y_cross_d_overlay_0529_bbv2` | ECS → Mac3 |
| W1B | `weekly_10y_d_overlay_0529` | weekly_point / 10Y | 6 / 1 | `weekly_10y_d_overlay_0529_bbv2` | ECS → Mac3 |
| W2 | `daily_5y_2_v28` | T+5 / 5Y | 5 / 5 | `daily_5y_2_v28_bbv2` | ECS → Mac3 |
| W2 | `daily_7y_1_v28` | T+5 / 7Y | 5 / 5 | `daily_7y_1_v28_bbv2` | ECS → Mac3 |
| W3A | `liwei_0616_cons_sda_k3_div_k10` | T+5 / 5Y | 5 / 5 | `liwei_0616_cons_sda_k3_div_k10_bbv2` | ECS → Mac3 |
| W3A | `liwei_0616_5y01_full_oos_k3_div_k10` | T+5 / 5Y | 5 / 5 | `liwei_0616_5y01_full_oos_k3_div_k10_bbv2` | ECS → Mac3 |
| W3B | `liwei_0616_10y01_cons_say_k3_div_k10` | T+5 / 10Y | 5 / 5 | `liwei_0616_10y01_cons_say_k3_div_k10_bbv2` | ECS → Mac3 |
| W3B | `liwei_0616_10y01_full_oos_k3_div_k10` | T+5 / 10Y | 5 / 5 | `liwei_0616_10y01_full_oos_k3_div_k10_bbv2` | ECS → Mac3 |
| W3B | `liwei_0616_10y02_cons_say_k3_div_k5` | T+5 / 10Y | 5 / 5 | `liwei_0616_10y02_cons_say_k3_div_k5_bbv2` | ECS → Mac3 |
| W3C | `liwei_0616_5y_auc_static_all_k3_div_k10` | T+5 / 5Y | 5 / 5 | `liwei_0616_5y_auc_static_all_k3_div_k10_bbv2` | ECS → Mac3 |
| W3C | `liwei_0616_5y_auc_yearly_all_k3_div_k10` | T+5 / 5Y | 5 / 5 | `liwei_0616_5y_auc_yearly_all_k3_div_k10_bbv2` | ECS → Mac3 |
| W3C | `liwei_0616_5y_ic_yearly_all_k3_div_k10` | T+5 / 5Y | 5 / 5 | `liwei_0616_5y_ic_yearly_all_k3_div_k10_bbv2` | ECS → Mac3 |
| W3D | `liwei_0616_7y01_cons_say_k3_div_k10` | T+5 / 7Y | 5 / 5 | `liwei_0616_7y01_cons_say_k3_div_k10_bbv2` | ECS → Mac3 |
| W3D | `liwei_0616_7y03_cons_all_k3_div_k8` | T+5 / 7Y | 5 / 5 | `liwei_0616_7y03_cons_all_k3_div_k8_bbv2` | ECS → Mac3 |
| W4A | `daily_1y_xgb_1y13_0629` | T+1 / 1Y | 1 / 1 | `daily_1y_xgb_1y13_0629_bbv2` | Mac3 |
| W4A | `daily_5y_lgbm_5y10_0629` | T+1 / 5Y | 1 / 1 | `daily_5y_lgbm_5y10_0629_bbv2` | Mac3 |
| W4A | `daily_10y_lgbm_10y04_0629` | T+1 / 10Y | 1 / 1 | `daily_10y_lgbm_10y04_0629_bbv2` | Mac3 |
| W4B | `weekly_avg_1y_lgbm_0529` | weekly_average / 1Y | 6 / 1 | `weekly_avg_1y_lgbm_0529_bbv2` | Mac3 |
| W4B | `weekly_avg_5y_lgbm_0529` | weekly_average / 5Y | 6 / 1 | `weekly_avg_5y_lgbm_0529_bbv2` | Mac3 |
| W4B | `weekly_avg_10y_lgbm_0529` | weekly_average / 10Y | 6 / 1 | `weekly_avg_10y_lgbm_0529_bbv2` | Mac3 |
| W4C | `monthly_1y_rf_top30_0629` | monthly / 1Y | 30 / 1 | `monthly_1y_rf_top30_0629_bbv2` | Mac3 |
| W4C | `monthly_5y_knn_top20_0629` | monthly / 5Y | 30 / 1 | `monthly_5y_knn_top20_0629_bbv2` | Mac3 |
| W4C | `monthly_10y_rf_top5_0629` | monthly / 10Y | 30 / 1 | `monthly_10y_rf_top5_0629_bbv2` | Mac3 |

合计 26 个原 base / 30 个 target；非 W4 为 17 / 21。W3A、W3B 整组原子切换；
W3C、W3D 逐方案切换。W4 按日频 → 周均 → 月频推进，ECS 不执行 Darwin payload。
清单没有包含 `native` 的原 ID，不进行字符串全库改名。

原 target rule 保持不变：

- weekly point：`next_week_last_trading_day_vs_current_week_last_trading_day`。
- weekly average：`next_week_average_yield_vs_current_week_average_yield`。
- monthly：`next_month_observation_yield_vs_feature_month_observation_yield`。
- 日频保持原配置；覆盖匹配使用 task + tenor + target rule，不能只看 horizon。

## 3. 本轮基线与下一动作

以下是本轮开始时的读回/已有证据，不是未来实时状态；每次操作前重读现场。

| 对象 | 已知事实 | 下一动作 |
|---|---|---|
| 开发分支 | `codex/develop`，起点 `938d1c4`，开始时 clean；此前核实 112 个未推送提交 | 审查整体 diff、更新远端引用，不 force push |
| ECS | current `a6ffe3a6…` | 冻结 current/previous、manifest、DB、调度 |
| Mac3 | current `b736d3b2…`，launchd installed/loaded 只读审计通过 | ECS 验证后同 archive 独立晋级 |
| W1/W2/W3A | 已有跨 ID ECS 接管及算法证据 | 回原 ID、保护独有结果；不能算同 ID 闭环 |
| W3B | 三份 333 条完整等价结果通过；临时 ID backtest 282/283/284 已入库 | 复用证据并核验身份转换、原子接管 |
| W3B 原历史 | 三原 ID 各 333 历史事实、78 gray/live，gray target 2026-06-01 至 2026-09-17 | 保护原摘要，不因运行时升级补算这 234 条事实 |
| SAY | ECS 状态接纳及一次正常增量调用通过，业务写入 0 | 保留产物，不重复 |
| Full | cold 5797.250 秒、同 Request warm 9.932 秒，五字段一致，业务写入 0 | 保留产物；同 Request 命中不是新一天耗时保证 |
| K5 | 2026-09-12 01:23:59 完成；cold 5830.837 秒、同 Request warm 9.990 秒，独立只读核验通过 | 复用源状态完成原 ID 身份接纳，不重跑初始化 |
| W3C/W3D | 有草稿，静态检查不等于数值验收 | 仅补缺失的等价和性能证据 |
| W4 | 旧入口仍有 DB 上下文，binary bundle 输入边界未证明 | Mac3 隔离验证文件输入，不冒充完成 |

W3B 产物索引：

- 本地：`outputs/releases/w3b-reused-backtests-20260911.HgiZQP/`。
- ECS：`/opt/bond-factor-lab/incoming/w3b-all-candidate-20260911.7A2igD/`。
- K5 attempt：`state-init-liwei_0616_10y02_cons_say_k3_div_k5_bbv2/`，
  日志 `state-init-k5-controller.log`。只认 complete/failure receipt 及状态，不根据进程退出猜成功。
- K5 绑定 2026-09-11 Request、generation `full-20260911-063339-668c17efc6bb`；
  可跨午夜正常结束，不允许修改日期或跨日重跑冻结入口。
- 初始化脚本 SHA：
  `c88091a771e8b8bfec8b7f7c53f298eb69946aedc45aa48b091b6fb8ae7e41a7`。
- `a1b9a72…` 是废弃跨 ID 切换候选，不可作为本计划 current 激活。

每方案状态：**待处理 → 代码就绪 → 等价证据就绪 → 本机执行就绪 → 原 ID 接管 → 数据清理 → 完成**。
旧跨 ID 成功不能跳过原 ID 接管与清理。

### 本轮已验证的实现进度（2026-09-12）

- 同 ID repository 已支持整批 Native→Blackbox 版本切换、反向恢复和再次切换，
  复用普通 activation 锁，绑定全部候选版本字段、Harness 证据与现场摘要；原事实只读。
- 周/月 `fact_horizon` 投影已贯通 discovery、predict、gray batch 与 backtest，
  仅允许精确迁移清单；普通 Blackbox 默认版本摘要保持不变。
- 已实现只读交付身份转换证明与 W3B 状态 Metadata 封装转换：算法字节、数值数组不变。
  这些 helper 本身不构成激活证据；准备流程的后续接通与验证见本节最新进度。
- W3B 三原 ID 的 ECS 部署资格已在候选矩阵恢复；现场尚未切换，不能提前排除原 Writer。
- 新鲜全量回归：805 passed、9 skipped、235 subtests passed；另以本机随机隔离 MySQL
  schema 实测 3 passed，覆盖整批回滚、正常 activation 锁竞争、再切换与身份漂移。
  测试 schema 已删除；不是生产 schema inspect 或生产切换验收。
- 独立代码审查及复审通过，无未关闭的 Critical/Important。新增候选身份字段未绑定计划 SHA
  的问题已修复并有负测；状态转换证明也已要求实际 Metadata 字节，不能只信传入摘要。
- 本轮尚未改变 ECS/Mac3 current、生产 Registry、预测或回测事实；未删除迁移数据，未执行 DDL。
- K5 于北京时间 2026-09-12 01:23:59 完成，原 controller/algorithm 均退出，无 failure receipt；
  冷/温阶段凭据与最终 complete 一致，重算 plan SHA 与开始/完成凭据一致。
  Request 为 predict 2026-09-11、feature 2026-09-10、target 2026-09-17，方向 1。
  READ ONLY 复核六类数据库事实摘要与开始前完全一致；没有业务写入。
  源 envelope SHA：`482e718dac2831723b3f12b842ed683b0fcf7b88b534bf96a215f68b84ca0729`；
  payload SHA：`cb2d672a1a7eb74eb898991c91a51084bfb466c19084d7e96f6aef791012f779`；
  complete SHA：`b9e99d9fa882dde4e36a3d8f6d454e0e8b2988423b150569e9d667e04e3c1988`。
  六份执行原件和 state 已保全到本机 `outputs/ecs-k5-evidence-20260912.HvT2c0/`，
  复制前/本机副本/复制后源端摘要一致；保全回执 SHA：
  `1aa4354f498a4ef9c51630dee460f6b47c28e7e7fd00c42b7ec58a52d4ccca93`。
  这是同 Request 温调用验收，不是新一天追加训练耗时，也不是 Mac 生产状态接纳。
- 基础能力提交 `ce57ec5386a1fea9b4fea60759d4418ef8e0a41b` 已推送，远端引用已读回；
  [草稿 PR #57](https://github.com/lucky1day/finlab-graylab/pull/57) 指向 master，未合并。
  clean commit 双构建字节一致，archive SHA-256 为
  `82ce1eaf8f8170232942d22ab9b9da5d0c4e08acb3854ebc21d6c38f133500ea`，
  产物在 `outputs/releases/same-id-foundation-ce57ec5/`，尚未作为 current 部署。
- Full/K5 原 ID 私有短调用已通过：4.7746 秒 / 4.7879 秒。同输入、同日期、同 cutoff，
  Request ID 前缀按原 ID 转换，标准结果一致；两个族各八个数组逐字节保留。
  此次是同 cutoff 复用（各 651 reused / 0 trained），不等于新一天推进或 ECS 验收。
  未创建新 canonical version、未发布平台 StateSession envelope、未写 Harness 或业务事实。
  独立审查已复核脚本、源 receipt、输入/运行环境、数组与输出，未重新执行算法。
  证据根目录：`outputs/native-runtime-identity-20260912/mac-identity-7gjrs0nm/`。
  Full receipt SHA：`19e21076f9a6dc87e7ef0ea975bf0f1a588cedbaab2dfc5b1494b75b7f889457`；
  K5 receipt SHA：`4728351a904fb88033a32b3a7a1ae1dfaa6d4e3edc9e3349f8fab981403b39bf`。
  此前 Mac cold→warm→stateless 的真正下一日对照仍保留在第 3 节列出的本机私有证据目录，
  不与此次同 cutoff 身份验证混用，也不代替 ECS 本机执行身份。
- 已实现唯一迁移 CLI 的同 ID W3B 控制路由：只接受完整三方案，旧跨 ID 写入和
  `prepare-equivalence` CLI 已禁用；内部已锁定的证据复验函数保留，不重复算法。
  每次从 candidate/reference 安装树、实际执行模块、部署矩阵、systemd 围栏/有效环境、
  五文件输入及 candidate StateSession 封装重新取证；原/临时 ID 共六个激活锁统一排序持有。
  缺新原 ID canonical、已接纳的 exact 状态或三份真实 Harness Gate 时直接阻断。
  此控制层不创建 Gate、不运行算法、不自动换 release、不自动停止或恢复 timer。
- ECS 旧 `3ac4647…` immutable reference 下，三份 333 条证据及四文件 Native 源闭包
  本轮只读复验通过，仍绑定各自原输入，算法执行次数 0。
- 控制层最终全量回归：838 passed、10 skipped、235 subtests passed；另行真实隔离
  MySQL 4 passed，新增原 ID 与临时 ID 两种普通 activation 锁竞争验证。
  独立复审无未关闭 Critical/Important。全量发现的 harness→scripts 依赖越界已移除，
  未增加架构 allowlist。CLI help 与部署边界通过，不等于生产 preflight 通过。
- 原 ID 状态接纳函数已实现，复用两把既有 StateSession 锁及原子发布：源 envelope、
  actual canonical、环境和五文件同代输入必须匹配，只允许已验收 cutoff 的一次标准短调用。
  标准结果一致后首次创建新 exact envelope；已有 candidate 不覆盖、不初始化，源状态不改。
  独立审查发现的“更晚 Request 可能触发追加训练”已通过执行前 cutoff 精确限制修复。
  10 项接纳边界测试通过，新鲜全量为 848 passed、10 skipped、235 subtests passed；
  独立复审无未解决 Critical/Important。本步骤没有生产算法调用、状态发布或数据库写入。
  不能据 helper 测试通过宣称已具备生产切换条件；原 ID canonical 交付仍受下述确认边界约束。
- 三份可信源凭据加载器已完成：固定 exact ID/version/config/算法及原件 SHA，
  Full/K5 复用原始七字段 Request；SAY 绑定后续正常增量状态，按同 snapshot 的已批准
  cutoff 重建 Request 并明确标记，不冒充已保存原 Request 原件。失败凭据、状态、路径、
  身份或输入异常均拒绝；返回来源证明，不授予发布或 Gate 权限。
  独立审查及 18 项来源边界测试通过；从本机 stdin 提供只读模块，在 ECS 的 `3ac4647…`
  reference 环境直接读取三份真实原件全部通过，算法执行 0、状态/数据库/服务写入 0。
  SAY / Full / K5 的来源证明 SHA 分别为：
  `940a441961ce245cea592a34667b0235f7039db3f24b9789827136d3e6439f35`、
  `4d540df8f043fbedfca6def41d3396a84faa27e822374e8bd1c2cd167408bbf1`、
  `e9fffbbf7373674e99a8a043ab2df31c774487479163c25972fcdf714f3ac8e2`。
  合入来源加载器后的全量回归为 866 passed、10 skipped、235 subtests passed。
  生产 prepare 仍须从正式候选 immutable release 执行；这次只读来源验收不代替它。
- W3B `preflight --action prepare` / `prepare` 编排已接通：复用三份可信来源、既有等价
  证明及 StateSession 接纳函数。下述周频修订标准调用已完成，当前准备步骤直接复用这些
  经独立验证的原始状态与五字段结果，不再次调用算法。成功后通过既有
  Harness 持久化真实 `native-runtime-upgrade` Gate，不制造 backtest，不写预测或 Registry。
  六个生命周期锁内重新核对已批准计划；实际 current 必须仍是与 reference exact 身份及
  四文件源码摘要匹配的 Native release，不能提前指向 candidate。准备不停止或恢复 timer。
  输入、current 或数据库发生变化即停止；已发布状态、原始 admission 和失败记录保留，
  禁止自动重跑、覆盖或初始化。进程终止未确认时，先保留输入，再尝试记录失败。
  中断后尚无自动恢复入口，必须先核验留存产物并明确最小恢复动作，不可再次调用 prepare。
  定向验收与独立复审均为 72 passed；真实随机隔离 MySQL 为 5 passed，新增 Harness
  写入后由切换 repository 接受以及生命周期锁持续持有的验证，隔离 schema 已清理。
  新鲜全量为 882 passed、11 skipped、235 subtests passed，独立复审无 Critical/Important。
  这是控制代码完成，不是生产 prepare 完成：该阶段未运行生产算法、未发布状态、未写生产
  Harness、未切 current 或调度；当时的目录顺序待决事项现已由下述用户授权解除。
- 06:30 ECS DataBridge 自然任务于 06:33:44 成功退出，发布
  `full-20260912-063338-cefd054bbccf` / `snapshot-b67e835383c8808b26982a32`。
  新五文件均与其 manifest SHA 相符；相对三份 W3B 源状态绑定的上一代，daily、weekly、
  calendar 文件摘要变化，monthly/catalog 不变。三份源 envelope SHA 仍与已保全值一致，
  原 ID 各 411 条事实、最大 target 2026-09-17 未变，无 running run 或迁移进程。
  已有等价报告和初始化仍绑定原输入，不失效、不重跑；旧版 prepare 的同代检查因此拒绝。
  当前修订接纳分别记录旧等价/初始化输入与新标准调用输入，不改旧凭据、不改 SHA，
  不因此自动初始化或另跑完整回测。候选目录附件共存已获批准。
- 对新 generation 的只读依赖定位已完成：在源状态相同的 Python/NumPy/Pandas 及完整
  算法环境身份下，复用已批准脚本的读取、前缀摘要和特征生成逻辑；不调用模型训练、Result
  或状态发布。三个方案的 daily/monthly/calendar 已消费前缀及 catalog 均一致；weekly
  855 行中仅 `202635` 的 `HWW00001/HWW00002/HWW00003/S0114089/Y0110594` 五格由空变有值。
  对三个方案各自 ten_y/seven_y 的 3917 行特征逐行比对，均仅 `2026-09-10` 一行变化；
  每组缓存有 653 条 OOS，特征不变的前段为 652 条，候选受影响后段为最后 1 条。
  原算法在特征差异处理之前因 weekly 源前缀变化而拒绝。现已只移除该 weekly 原始前缀
  拒绝，保留 daily/monthly/calendar 保护；仍由完整特征指纹决定从最早变化点更新后缀，
  不通过平台改 generation 绕过算法，不宣称支持任意 daily 历史修订。
- 三份当前 generation 标准 CLI 验证及原算法独立后缀对照全部通过：Full 19.22 秒、
  K5 19.23 秒、SAY 20.63 秒；各族前 652 点字节不变，末 1 点的 265 组预测/概率与原算法
  独立重算逐值一致，最终 Request 和五字段 Result 一致。源生产 envelope、历史事实均未改。
  证据原件位于 ECS `incoming/w3b-weekly-revision-trial-20260912.sXv2QO`，本机保全副本为
  `outputs/w3b-weekly-revision-20260912/ecs-evidence/`；成功 execution/verification 及驱动摘要
  由临时 `harness/w3b_revision_evidence.py` 精确锁定，失败的包装尝试不作为成功凭据。
  原 ID 候选脚本采用同一已验证补丁；准备阶段校验当前五文件、本机数值环境及源状态未变，
  原样发布已验证 NPZ 到新 exact version，不再训练、改 Request ID 或制造自然预测记录。
  此处是算法/状态候选验证完成，不代表 ECS Registry 已切换；接管仍需正式准备、版本事务和读回。
  集成验收：914 passed、11 skipped、240 subtests passed；真实隔离 MySQL 事务与 Harness
  接线 5 passed；独立审查无 Critical/Important。当前候选 exact version 为 SAY
  `f8031c16ec8c`、Full `772b225fd59e`、K5 `74614164d8c1`，取代下节未含周频补丁的早期候选。
- ECS 已从 `13941fbc…` 完成正式 prepare：三份原 ID StateSession 状态及真实 Harness Gate
  均已发布，算法执行 0 次，历史 prediction/run/backtest/Registry 未改。原始准备回执保存在
  `/opt/bond-factor-lab/incoming/w3b-final-prepare-20260912.TPh9uv/`。
  首次短维护窗口的 cutover 只读预检发现旧版本表有多条 Native active/paused 历史版本，
  尚未执行版本事务；current 已补偿恢复 `a6ffe3a6…`，日频 timer 已恢复等待。
  新状态和 Gate 不重建；后续仅平台事务补丁不改变三份 exact scheme version。
  源参考目录独立验证产生的三个 `__pycache__` 已外置保全，源码及已验收数值产物未变；
  systemd 重复 EnvironmentFiles 属性解析已修复，不改变 installed unit。

迁移历史版本状态收口限定为完整 W3B 三原 ID：旧 Native 真正执行身份由已验证 canonical
config、release 和单一 one-shot 决定，不以历史版本表 `active` 行枚举算法。只在无在途 Writer、
日频围栏及旧 canonical exact 证据匹配时，允许把显式预检清单中的额外 Native active 行
与旧当前版本一起原子退役；Native paused 历史保持原样。其他 Blackbox active/pending 或
无法证明来源的执行资格仍拒绝。逆向恢复只激活先前 canonical Native exact，不恢复额外
历史 active 标记；任何版本的代码、输入来源和已发布历史事实均不改写。
该收口补丁已通过 928 passed、16 skipped、240 subtests passed；另行真实隔离 MySQL
10 passed，独立审查无 Critical/Important。未通过生产 cutover 之前仍不计为原 ID 接管。

W3B 整组 ECS cutover 已在 `d2a6fba…` 成功，三个原 ID 当前唯一 active 版本均为上述
Blackbox exact；旧当前 Native 三行及额外九行已退役，三行 paused 历史不变。反向只读
preflight 通过；准备产物与 Gate 未重跑。全库 25,682 条产品预测及相关 run/backtest、Actuals
和其他 Registry/version 摘要与切换前相同。Dashboard 的统计/明细未变，但原三方案的
回测来源 Metadata 因当前 runtime 过滤而变 null，已围住日频 timer 做最小展示修复：
历史回测来源按同一业务 ID、已支持的平台 data_source 和既有 latest-success 规则选取，
不随执行运行时升级消失；不改历史来源、不引入跨 ID 拼接或重新回测。
展示修复已在 `3a805922667de41340942edce9941c7f902bb8fb` 完成并部署 ECS；其确定性 archive
SHA-256 为 `d73ec469036f6634d147f1f132caf950e4f8333fa65a350f773f36a9f21319cf`。
全部 88 个 active Dashboard 业务 payload 与切换前完全一致，回测来源保持原值；全部事实、
Actuals、其他 Registry/version 不变。三个原 ID 的标准执行授权、严格 discovery 和状态读回均
通过，算法执行 0 次。Backend 的实际 cwd 与 current 均为该 release，health 正常；日频 timer
已恢复，next trigger 为 2026-09-14 07:03 CST。周/月、DataBridge、Actuals 控制面和 Mac3 未改。
最终全量回归 929 passed、16 skipped、240 subtests passed；临时测试随全局迁移工具收尾删除。
W3B 已完成“ECS 原 ID 接管 + 发布验收”，不再重复 prepare/cutover/初始化；Mac3 晋级、临时
身份数据清理、附件退役及全局 confidence DDL 尚未完成，不能称整个双机项目闭环。
切换后核验原件：`cutover-readback-verified.json`、`final-standard-admission.json`；本机副本
`outputs/w3b-weekly-revision-20260912/ecs-cutover-evidence/`。下一批为 W2/W3A 原 ID 收口。

### W2/W3A 原身份回收：当前实施状态

2026-09-12 只读盘点：四个原 Registry 为 archived/native，临时 `_bbv2` Registry 为
active/Blackbox；原 canonical exact 已 retired，另有 14 条 Native 历史 active 标记待收口。
不能沿用 W3B“当前 Native canonical active”的前提，也不能把 retired Native 当作实际 Writer。

| 原 ID | 原事实 / 临时事实 | 临时独有 | 原 ID 候选 exact |
|---|---:|---:|---|
| `daily_5y_2_v28` | 409 / 411 | 2 | `a884eceea071` |
| `daily_7y_1_v28` | 409 / 411 | 2 | `8ebe915daa9e` |
| `liwei_0616_cons_sda_k3_div_k10` | 409 / 411 | 4 | `f8659dab99b2` |
| `liwei_0616_5y01_full_oos_k3_div_k10` | 409 / 411 | 4 | `0a65f61a7acd` |

四份初始候选脚本与现有 Blackbox 原字节相同，Metadata 仅改 ID，Native 附件精确绑定。
W2 两份与 SDA 无状态，Full 有四数组增量状态。以上表格是初始盘点；W2 和 W3A 已完成 ECS
原 ID 接管，Full 最终 exact 为 `be34f35f233b`。Mac3 的 current、launchd 和数据库未改动。

W2 本轮实际发布 `9385292514bdd2491c13bf7b24ba0a97e1d02e60`，archive SHA-256
`a4e52b82b6ff028c6674af5357f087c5f5624b7fe9c7c17701417dae645047bc`；previous 为已验证 W3B
release `3a805922667de41340942edce9941c7f902bb8fb`。两份既有 333 条零差异报告保留自己的
09-09 输入身份；新准备仅各执行一次已到期 09-11 Request，绑定当前 09-12 五文件 generation，
耗时分别 13.418 / 11.596 秒，未写预测或状态。新的真实 Harness 凭据通过原子仓储切换，
两个原 ID 各唯一 active Blackbox exact；临时 ID archived 且移出 ECS 发现范围。
八条旧 Native 历史 active 版本标记已退役，原 paused 不动；六月遗留 compare 的 running
审计原件按精确全行摘要只读识别，真实 PID 为零，未将其改成成功或修写历史。

切换后全库 25,682 条预测、5,660 条 run、108 个 backtest 及其子表、全部原 Harness 记录、
Actuals 完整摘要不变，其他 86 个 Dashboard 方案内容不变。标准发现和只读准入确认原 ID 可执行、
临时 ID 不可执行，新版本 code/config/manifest/environment 与批准计划逐字段一致。
Backend 实际 cwd 指向新 release，health 正常；日频 timer 已恢复，周/月 timer 未更改。
反向 preflight 通过，恢复目标为原先两个临时 Blackbox Writer，不复活 Native 历史 active 标记。
全量回归 1002 passed、26 skipped、243 subtests passed；真实隔离 MySQL 20 passed，独立审查无阻断问题。
原件保存在 ECS `incoming/w2-writer-reclaim-ready-20260912.Rzepm0` 及本地
`outputs/w2-writer-reclaim-20260912/ecs-evidence/`；切换回执 SHA-256
`6d88e771c5bfd2ee11ae3c562866e4a50a51f130ede27399035393106ea8089c`，标准准入回执 SHA-256
`74be899dba6cc1dc1cc521a0d0fcfdbcd9b4127dc36beea4372b2845d63a876f`。
这是 W2 执行接管完成，不是数据清理或双机全局闭环；四条独有事实的后续保全已完成，见下文。

12 条独有结果为 8 条已到期 live 和 4 条历史 backtest 来源；来源均有本机成功执行原件，
后续按原业务键只补缺。W3A 原 ID 的两个独有日期亦必须保留。重合方向不同只属于跨输入版本
漂移信息，不覆盖原 1636 条事实，不重新训练以匹配历史。Writer 回收事务先只改 lifecycle，
原/临时事实均不变；补缺、可恢复备份和精确删除是后续独立步骤，不把回收等同于数据清理完成。

Full 的旧状态 `full-oos-a2-private-4` 已独立核验：旧输入重现全部 3917 条特征指纹；当前
weekly 修订只改变 2026-09-10 最后一行，前 652 条 OOS、标签、日/月/日历依赖及固定 IC
选择全部不变。应复用前缀，只重算最后一点的内部 265 个 grid 结果；不能直接复用变化后的
末行，也不需要全量初始化。随后一次私有标准调用已通过，耗时 8.288 秒、方向 -1，
验证前 652 点 preds/probs 不变、特征前缀不变、恰好重算最后一点。仅副本 header 的代码/Metadata
身份转换，四个源 NPY 成员字节保留；生产 envelope、数据库、current 与 timer 均未修改。
独立原算法末行数值等价已完成：未改源码直接计算当日 265 组结果，以旧前缀及独立末点构造
Expected，再与候选的 preds/probs 逐字节及完整五字段比较，全部一致；未重算历史前缀。
该私有验证当时未发布状态；后续已复用这些证据完成状态接纳与接管，见下文。
私有原件为 ECS `incoming/w3a-full-weekly-private-20260912-938529/`，不是正式 Gate 或回测成功记录。
独立原件为 `incoming/w3a-full-weekly-independent-20260912-938529/`，complete SHA-256
`6c37299e795612bff2d8b76745fe9d2dc981cb38db6e4041403f7a3c8b58f813`。
证据位于 ECS `incoming/w3a-full-weekly-analysis-20260912.wpLu6e`，`evidence.json` SHA-256
为 `d63eb3f43674466926034f11780f56f9caebe377c0f9894ec743c918d823d210`。

W2 四条 live 独有结果已完成 ECS 受控保全：

- 同一迁移 CLI 增加 `preflight --action preserve-live --wave W2` 与 `preserve-live --wave W2`；
  必须绑定原 ID 接管 release、已围栏 Writer、expected DB/UUID 和 fresh plan SHA，不接受预测值或自选键。
- 仅原两 ID 的 2026-09-16 / 17 四键；保留源 Request、日期、方向与完整原 extra，在
  `extra.migration_import` 记录源行/run/version、历史 snapshot/五文件、原件摘要、备份和恢复证明。
  新本地 run 使用 `manual`、`prediction_phase=NULL` 和实际物化时间；新 exact 表示物化归属，
  真实算法执行版本由 source 字段保留，`algorithm_executions=0`，不声称执行了新自然预测。
- 真实只读备份 3,485 行、11 张数据表和 14 份 FK closure DDL 已在本机随机隔离 MySQL 恢复，
  插入时 FK 检查开启，全部行及迁移摘要相同；演练库已删除，完整备份双份保留。
  备份 SHA-256 `61148898d72c7b6f85de9b2653c70b9406b8ed3ce8302e9776ce1390ffe51b06`，
  恢复回执 SHA-256 `93eaa9426fd0f34bb3f249fce780896b17f1564bdffed1c6fcc4d33a64f399b8`。
  仓储另核对备份源数据库身份摘要，不能将同数据克隆库误当本次同库保全。
- 四条新 run/预测一次事务；任意已有目标键、来源或输入/备份漂移均拒绝整组。
  全部旧 prediction/run/backtest/Harness、Registry/version 保持不变，尚不删除临时身份。

W2 保全时 ECS release 为 `ee92b2d62a9e04faa59e2dc1f5e16a55ce9bdfa8`，previous 为上述 W2 接管
`9385292514bdd2491c13bf7b24ba0a97e1d02e60`；同提交两次构建 archive 字节一致，SHA-256
`f0a2e7fffef1cfed5c9f8facab4b079790e1a79efcc215db4eaec42a445ae89c`。当前提交已推送 develop，master 未修改。
全量 1077 passed、35 skipped、243 subtests passed；另行真实隔离 MySQL 29 passed，独立审查无 Critical/Important。
本次在周频 one-shot 自然成功退出后，仅围栏日频 timer，未杀算法；初次只读 preflight 因操作进程
缺少显式部署 target 拒绝，保留失败原件后补齐 `aliyun-gray` 环境，再重新生成真实批准计划。
没有因此修改 production config、放宽校验或重算数据。

批准计划 SHA-256 `40f27c1f6fdc486b1dc6e10a75c6079cf2b83d48613184a2e13715bfa4d769de`；
保全回执 SHA-256 `9ff46af2b33e8c5acc706c15b21c720b47223f620c50c8da7946c4b6ebb6d744`。
前后全库预测 25,703→25,707、run 5,681→5,685；新增恰为四条 manual 物化及其预测，
算法执行 0 次。剔除这八条新记录后，全部原事实 SHA 完全不变；Registry/version、Actuals、
其他 86 个 Dashboard 方案内容不变，原两个 ID 现在各有 411 条预测。Backend cwd/health 与
current 对应，日频 timer 已恢复，周/月 next trigger 正常。正式准入回执 SHA-256
`3327a1ecbcaadaa119614beb236a8492775fb47c20e79548f77314b3ab80239d`。
原件位于 ECS `incoming/w2-live-preservation-20260912.TS7r6o/` 及本地
`outputs/w2-live-preservation-20260912/ecs-evidence/`。临时身份及其记录未删除，旧备份继续保留。

W3A 已完成 ECS 原 ID 接管与发布验收：

- 当前 release `025f153e20865137df22ea1b464a5ef02d123787`，previous 为 `ee92b2d62a9e04faa59e2dc1f5e16a55ce9bdfa8`；
  同 commit 两次构建一致，archive SHA-256 `abeb512c0ed6eda16973513b1816f1370e53a3aaf53e10707fa357211bcadb78`。
  Native 参考代码仍单独绑定 `3a8059…`，不把它当作回滚 release，避免撤销 W2 已完成的接管。
- 两份既有 333 条证据在原 locale 独立只读复验；本次执行保留实际 systemd 环境，不改旧报告输入。
  Full alias 标准 warm 为 4.617 秒，复用 653 点、训练 0 点、方向 -1；四个 NPY 成员逐字节不变。
  先发布已验证的 source 当前输入状态，再发布 candidate 状态；SDA 标准调用 63.254 秒、方向 -1。
  两次调用均未写业务预测，真实准备 Gate 已入库。
- Full `be34f35f233b`、SDA `f8659dab99b2` 已由一个 repository 事务激活；两个临时 ID archived，
  原 ID 各唯一 Writer。全库预测 25,707、run 5,685、backtest 108 及全部子表/原审计摘要不变，
  Actuals 和其他 86 个 Dashboard 方案不变。8 条临时身份独有结果仍保留源记录，待单独保全，未删除。
- 反向只读 preflight 验证当前 source/candidate 状态及回滚边界；没有为演示再次切换。
  Backend PID/cwd、health、标准执行准入均通过，日频 timer 恢复；周/月、DataBridge、Actuals 未操作。
- 本地全量 1139 passed、38 skipped、243 subtests passed；另行隔离数据库组合 39 passed，
  其中 32 项真实 MySQL、7 项 SQLite。独立审查无 Critical/Important；develop 已推送，master 未修改。
- 原件：ECS `incoming/w3a-writer-reclaim-20260912.7oD8G9/`，本地 `outputs/w3a-writer-reclaim-20260912/ecs-evidence/`。
  prepare SHA-256 `cf97378d20db02d4bf5551e2aaec4f99808b32c04b9378f5b3b6aba0cfa9ded7`；
  cutover SHA-256 `656efb09b749ebb8aa4d39fe378bf9bce2731cb5132711a20414d63fb2cadcef`；
  标准准入 SHA-256 `4ca4412f04eac79eacb84154deecc53de749bdc67d484f9d268adddc306453d4`。

W1B 三个周频方案已完成 ECS 原 ID 接管与发布验收：

- 2026-09-12 本批完成时 current 为 `0ba331f76374f27a7434f2d2732a743f7b3c66b1`，previous 为上述 W3A `025f153e…`。
  clean commit 两次构建字节一致，archive SHA-256 `064bd1a09d08e3eb0eebe01516cb1bcbdcb57123ecf8cf14d133a6172e75a857`。
- 5Y / 7Y / 10Y 各一次标准调用，分别 0.773 / 0.774 / 40.259 秒；8 线程、4 GiB、120 秒预算。
  predict/feature/target 为 2026-09-12 / 09-11 / 09-18，方向分别 +1 / -1 / -1。
  已有各 72 条等价证据按原输入复用；本次调用绑定 generation `full-20260912-063338-cefd054bbccf`，不重跑旧回测。
- 现场预检发现 Native 旧 weekly target_rule 名称与 Blackbox 标准名称不同；仅对固定 W1B 清单
  将 `next_week_last_trading_day_vs_current_week_last_trading_day` 映射为
  `target_week_end_yield_vs_feature_week_end_yield`，任意其他名称和空值仍拒绝。
  两次失败只读预检与 release 恢复记录保留；最终修复复用原三个成功 Gate，未重复算法或改写历史。
- 5Y `d910a7819747`、7Y `d5aafe9f839e`、10Y `1598b055715a` 在一个事务内成为原 ID 唯一 active exact；
  三个临时 ID archived 且被部署矩阵排除。原 Registry h6 身份不变，执行 Metadata 仍为 h1。
  25,707 条预测、5,685 条 run、108 条 backtest 及全部子表/原审计完整摘要不变，Actuals 不变，
  其他 85 个 Dashboard 方案内容完全不变。临时身份独有结果尚未补入或删除。
- 反向只读 preflight 通过；Backend cwd/health 和原/临时身份执行准入验证通过，周频 timer 已恢复，
  next trigger 为 09-19 11:30；日频 09-14 07:03、月频 09-12 18:00 保持原计划，Mac3 未操作。
- 最终 clean worktree 回归 1151 passed、76 skipped、243 subtests passed；跳过项不计为通过。
  另行 loopback 隔离 MySQL 及控制组合 40 passed，含真实整组切换/回滚与失败恢复。
  独立代码审查无未解决 Critical/Important；develop 远端已核对，master 未修改。
- 原准备：ECS `incoming/w1b-writer-reclaim-20260912.tM5hte/`；最终操作：
  `incoming/w1b-exact-rule-20260912.gb4kdt/`；本地 `outputs/w1b-writer-reclaim-20260912/ecs-evidence/`。
  prepare SHA-256 `79f7858f923b62b05abe24dfd6551bebf6d35e6c0e267a34ac4058de617be12a`；
  cutover SHA-256 `480ad62682e4fc85c937850e43b00397913b8efbad7c4a7eb2dcfb5a392e8a4e`；
  标准准入 SHA-256 `72202602b3267438448fa0f3e3349ddcddd7a39cdc94e2edd171d1d8d207857b`。

W1A 两个 base、六个 target 已完成 ECS 原 ID 接管与发布验收：

- 本批完成时 current 为 `812e8dd53aed2d81bfe5154946325c2c9dd3d164`，previous 为 W1B `0ba331f…`。
  两次 clean commit 构建字节一致，archive SHA-256 `f908e48f7e85aa81081657c78d0c0af8b6a766d529ec529b8cb3a77c628f2e3d`。
- T1 两个 target 标准调用共 7.032 秒，T5 四个 target 共 10.878 秒，每个 target 只执行一次。
  旧 T1 各 337 条、T5 各 333 条同输入等价证据复用；新调用绑定当日五文件 generation，未重跑回测或写预测。
  初次准备在写 plan 回执前遇到 datetime JSON 序列化失败，未创建 Gate、启动算法或改 DB；
  修复为已有 canonical plan 序列化后才重新准备，失败原件保留。
- 原 `t1_daily` exact `989db4dd0bfa`、`t5_daily` exact `cc81e9338231` 在一个事务中接管六个原 Registry target；
  六个临时身份 archived 并从 ECS 部署矩阵排除。整个 base 摘要覆盖所有目标交付，仍每 base 一个调度任务。
- 25,707 条预测、5,685 条 run、108 条 backtest 及全部原子表/审计摘要不变，Actuals 不变；
  其他 82 个 Dashboard 方案内容完全不变。临时身份独有事实尚未补入或删除。
- 反向只读 preflight、原 ID 标准执行准入、临时身份拒绝、Backend cwd/health 均通过；
  日频 timer 恢复，日/周/月下次触发分别 09-14 07:03、09-19 11:30、09-12 18:00。Mac3 未操作。
- 全量回归 1231 passed、42 skipped、243 subtests passed；另行 W1A/W1B 控制及真实隔离 MySQL 组合
  65 passed（不把整组都标为 MySQL）。独立审查无 Critical/Important；develop 已推送核对，master 未修改。
- 原件：ECS `incoming/w1a-canonical-plan-20260912.pKFGPN/`，本地 `outputs/w1a-writer-reclaim-20260912/ecs-evidence/`。
  prepare SHA-256 `3817cc1112dc38cbf8c37984068cee9965d86702f5a644221542139baef3b229`；
  cutover SHA-256 `f7c709d2acd661a9f08a382ba97b12e4e13c16b7ab7914c9167f40bd045138dc`；
  标准准入 SHA-256 `d39ccfdbcbe5615dfd83e575b267f47067eb73b62242fc4b3d6ae5eb928b3ebe`。

W3A 八条临时身份独有结果已完成 ECS insert-only 保全：

- 本批 current `6c818d91bccf706ccc66e515b4ba30cee414ea61`、previous `812e8dd…`；同 clean commit 双构建一致，
  archive SHA-256 `1514eb8c1fa3cecb6b0d2705f462bb9af78cae1035525c94ec78bef683c5ba7d`，算法 exact 与部署矩阵未改。
- 固定备份 3465 行、14 表 DDL、12 外键已在隔离 MySQL 保持外键启用的情况下恢复验证；
  原件 SHA `b22b235613b9e8b1094bf5dbb26b1e37505480ea06d2f5ec5410e56a9870ca0e`，
  恢复回执 SHA `7bc4e18ac867884fbaf8a0338dc96e66da7f49811cfa60592b45378d36346075`。
- Fresh preflight 绑定历史各自五文件输入、真实源 exact、Full 已审修订证据和当前围栏；一次事务新增八条预测、
  四条 manual / NULL phase run。四条回测产品保持 `backtest_run_id=278/279`、原 actual 且 run 为 NULL。
  共享父回测及明细不复制、不改写，后续清理不得删除这些共享来源，也不将历史物化称为新算法或自然执行。
- 预测 25,707→25,715，run 5,685→5,689；原两个方案各 413 条。剔除新行后全库原事实摘要完全一致，
  Registry/version、Actuals 及其他 86 个 Dashboard 方案不变，算法调用 0。临时身份数据未删除。
  Backend/current/执行准入及日周月 timer 读回通过；未操作 Mac3。
- 全量 1262 passed、44 skipped、243 subtests；另行隔离 MySQL 与 SQLite 组合 27 passed，
  包含八条成功提交、第八条故障回滚及旧 W2 防线。独立审查无 Critical/Important，develop 已推送，master 未改。
- 原件 ECS `incoming/w3a-result-preservation-20260912.IVDO74/`、本机 `outputs/w3a-fact-preservation-20260912/ecs-evidence/`。
  plan SHA `72a5711cae62dea4009a45b7382da7c00ea439562538c21be74ac9f0e2e5b07a`；
  apply SHA `b07e92e87d6b46ef94fab04900ab1c1a9fc1cdaab29900a8721c1f9894615c6b`；
  准入 SHA `e01b38c3704542b7a3b76d0e8dab7937c020c25c32835461a68fcf259382223a`。

ECS 原 ID 接管现为 12/17 个方案、16/21 个 target。下一步是 W3C/W3D 缺失验证及 W1A/W1B 结果保全；
只读盘点 W1A 六个 target 各五条、W1B 三个 target 各四条，共 42 条待保全，不能把盘点当作批准入库。
双机晋级、W4 和最终清理仍未完成。W3C/W3D 优先核实既有 Native 数值产物和真实依赖，不能因缓存的
`unqualified` 标签直接丢弃可核实结果，也不能不补证明就将其发布为合格 Blackbox 状态。

2026-09-12 后续验证进展（尚不增加接管数量）：

- W1 固定 42 个来源的备份为 14,677 行、14 表 DDL、12 外键；SHA
  `c41f7d859ebb13b08c76425cb8016631fe2784723d7ed3bbb683a12e26f0018f`。
  第三次真实隔离恢复通过，全量原始 rows/history/source 摘要完全相同，外键始终启用，自建库已删除。
  恢复回执 `outputs/w1-fact-preservation-20260912/restore-proof-3.json`，SHA
  `2c7b6dbf91253b116a04c6c58da1fb54f40fbdfb096a11b6b117d3a5ec9f4ed4`。
  前两次失败来自 MySQL JSON 文本解析将同一个旧审计 double 舍入一个 ULP；实际涉及十条审计行。
  私有恢复脚本仅在新建隔离库内通过 `JSON_SET/CAST AS DOUBLE` 精确保留原值，再比较原始完整摘要；
  无容差、无摘要改写，原备份和两次失败证据保留。独立审查无 Critical/Important；生产尚未写入这 42 条。
- W3C 三个原方案末点已实际计算并保存。AUC static 首次在后置组装失败，原数值产物完整，
  仅以零 fit 续接组装；AUC yearly、IC yearly 首次完成。原末点分别约 16.18、30.01、21.13 秒，
  不重复训练；三份证据均明确候选训练内核未执行、正式 333 Request 等价未完成。
  详见 `outputs/w3c-reuse-audit-20260912/last-point-results.md`；不得以共享 Phase A 后方向相同冒充独立内核等价。
- W3D 两个参考进程于 15:45 CST 实际启动，私有目录
  `outputs/w3d-comparison-prepare-20260912/full-sparse/{7y01,7y03}/native/`。
  使用原 selector 的真实依赖，339 个历史点全 grid 加 236 个当前点稀疏计算，不改变算法窗口或 cutoff。
  每个参考仍为 7200 秒上限，并以 17:45 为本窗口硬截止保护 18:00 任务；
  参考完成后若窗口剩余不足 3600 秒，保留完整产物并延后候选 batch，不重跑已完成参考。
  进程启动不等于验收通过，须以完整回执、333 个结果和候选逐行对照为准。

### 已批准的迁移期附件共存与历史修订边界

附件共存候选已实现并完成独立审查（无 Critical/Important）：全量回归 884 passed、
11 skipped、240 subtests passed；复用校验确认三脚本字节与已验收交付相同、Metadata 仅改 ID，
算法执行次数 0。候选 exact version 为 SAY `82df84447d51`、Full `f8ad289bc9c3`、
K5 `def88c9afd72`；三者保持 paused/draft，只有受控版本事务才可取得 Writer。
Native onboarding 清单已移除这三个候选身份，原文件及 reproduction runner 保留。
本机原编译缓存已移动至 `outputs/w3b-retained-native-bytecode.tFcHvK/` 可恢复保留，
不纳入交付摘要；生产文件、状态、Registry 与 current 尚未改变。

用户已明确授权：新 Blackbox 目录内暂时保留 Native 附件，验证接管完成后再删除；
这取代此前待确认的“提前删除三个目录附件及 reproduction runner”建议，不再等待该确认。
当前先在 W3B 三个原 ID 上实施：原配置切为 Blackbox，两文件交付放入 `delivery/`，
原 `predict.py/inference.py/core/benchmarks/__init__.py` 和 reproduction runner 保留。
配置显式记录 `native_attachments` 精确路径及 SHA-256，参与 config/exact version 摘要；
文件集合、摘要或路径不符拒绝加载。附件不加入算法导入路径，不授予 Native fallback 或第二 Writer。
普通 Blackbox 仍保持严格两文件及根目录合同；迁移清理结束后一并删除临时例外。
旧生产 current、immutable reference、archive、Git 历史及旧状态不因开发候选改动而改变。

用户同时明确：历史数据修订正常，已发布历史预测原内容、版本和来源永久保留，不因修订重算、
覆盖或强行对齐；既有等价证据继续绑定自身冻结输入。仅下一次预测必要的内部模型/缓存计算可以
更新，不将内部后缀重算写成历史预测修订。当前不再有待用户确认的普通迁移步骤；两端 confidence
DDL 仍各自保留独立确认。

针对本次 weekly 补值的代码依赖复核：daily 标签和有效样本集合未变，IC 的 2024 年前固定窗口
未变，Phase A 每个 OOS 点只依赖该点及之前的特征，故之前 652 个点可保留。
Full/K5 可补最后一个 Phase A 点并重算最终排名/信号；SAY 还保留原月内 selected-grid 训练路径，
必须如实计时，不宣称三个方案都只训练一行。不为该修订再完整重跑已验收历史。
现有 raw-prefix guard 与 exact code identity 仍需受控处理；不直接改 envelope/hash 冒充新输入验收。

## 4. 最小实现

### 4.1 原 ID 版本事务

复用现有 version、Registry、repository 锁和事务，不新增 lifecycle 表、ledger 或长期 replacement 框架。

1. 锁内核实原方案、现有 Writer、候选 exact version、批准证据与环境目标。
2. 退役原 ID 旧 Native/Blackbox version，激活原 ID 新 Blackbox version。
3. 保留 Registry ID、owner 和业务身份，仅更新必要运行属性。
4. 已跨 ID 批次同时撤销对应临时 ID 的执行资格。
5. 提交前验证 target 覆盖精确相等、唯一 Writer；失败整组回滚。

W3A/W3B 不允许多次独立 activate 冒充原子切换。
普通 Blackbox 修订检查保持严格，不能全局去掉 Native 历史校验来绕过边界。

已部署的 W3B 临时入口为 `python -m harness migrate-native-successor
{preflight,prepare,cutover,rollback} --wave W3B`。当前候选另在同一 CLI 增加 W2 的
`preflight/prepare/cutover/rollback` 原身份回收路由，不能使用旧跨 ID 命令。W2 准备入口
复用固定等价报告，在当前仍为旧临时 Writer release 时对每份执行一次无状态标准预测，
分别绑定旧等价输入和本次执行输入，创建真实 Gate；不写预测、状态、Registry 或操作调度。
可选 `--predict-date` 用于 W2/W3A prepare（包括 prepare 预检），必须是已经到期的工作日交易日，
默认选择最近已到期日期。传相应 action 的 plan SHA 和全新 work-dir；已有候选准备凭据时
拒绝重跑，失败保留实际进程、错误及已有结果，并尝试写失败 Gate。
首次 ECS 准备预检发现一条 2026-06-14 的旧 Native Compare `running` 审计记录；
它不对应当前进程、canonical 或临时 Blackbox Writer，不能改成成功或覆盖历史。
迁移期仅按固定记录身份和完整 run/Gate 摘要识别这条已核实的历史记录，并仍核对实际
Python 进程与 Writer；其他 `running`、任何摘要漂移均拒绝。该历史行及 Gate 在切换前后不变。
W3A 准备与整组事务已部署完成，必须显式传 `--rollback-project-root` 绑定实际接管前 release；
cutover/rollback 的 `--work-dir` 指向真实准备回执并只读复验状态，不在事务中执行算法或发布状态。
完成的准备只能只读恢复回执，部分失败禁止盲目重跑。Mac3 入口尚未实现。
以下表格仅描述已验证的 W3B 路径，不是其他批次的操作指令。
所有命令从 candidate immutable release 执行，传 candidate `--project-root`、
已安装旧 Native `--reference-project-root`，以及只读取得的目标 DB 名称/UUID
（`--expected-database-name` / `--expected-server-uuid`）；不提供生产凭据示例。

准备与切换的预检不能混用：

| 步骤 | current / 调度前提 | 专属参数与输出 |
|---|---|---|
| `preflight --action prepare` | current 仍为已核验 Native；不 fence、不切 release | 不传 Harness ID；输出准备 plan / SHA，不调用算法、不写状态或 Harness |
| `prepare` | 同上，在锁内重新读取准备计划 | 传准备 SHA、`--approved-by`、全新绝对 `--work-dir`；复用固定成功调用的原始状态/结果，算法执行 0 次，成功后输出三个真实 Harness ID |
| `preflight --action cutover` | 三份状态/Gate 已就绪；操作者已 fence 并在维护围栏内切到 candidate current | 传三次 `--harness-run-id 原baseID=真实runID`；输出新的切换 plan / SHA |
| `cutover` | 维持 candidate current 和 Writer 围栏 | 同一组三个 Harness ID、切换 SHA、`--approved-by`；整组版本事务，不自动切文件或恢复 timer |
| `preflight --action rollback` / `rollback` | 先 fence；仍从 candidate current 读取反向事务计划 | 三个 Harness ID；rollback 另传反向计划 SHA 和 operator；版本恢复后由操作者切回已核验旧 release，再恢复 timer |

每个写命令的 SHA 都通过 `--expected-plan-sha256` 显式提供，只能使用其对应 action
刚生成的预检结果；准备 SHA 不能代替切换或回滚 SHA。无 `--action` 的 preflight 默认为
cutover，不能当成 prepare。准备中断后先核验已保留状态与 admission，不能换目录重跑。
候选 canonical 按上文已批准的附件共存方式准备；上述命令说明不表示生产 prepare 已完成。
尚未准备完状态/Gate 前不要先 fence 或切 current。文件系统切换失败、缺证据或 DB 操作失败时，
外层操作者仍须保持 Writer 关闭并完成分层补偿，不能把此 CLI 当成自动发布脚本。

### 4.2 多目标交付与历史 horizon

T1 的 2 个 target、T5 的 4 个 target 各归属原 base 的一个 canonical 目录和 exact version。
各 target 复用独立两文件包，Metadata 使用原 base ID 和各自 tenor，配置明确目标与文件关系。
整体摘要覆盖全部交付；通用执行器归一为目标列表；每 base 仍一个调度任务，
全部输出通过后一次 repository 提交，任一目标失败不部分发布。不增加 T1/T5 专用调度分支。

周/月保留原 Registry 与事实 horizon 6/30，Metadata 使用业务桶 horizon=1。
Request 按 Blackbox task 语义生成，持久化边界显式投影原业务键；
不把 6/30 当日期步长，不全库重写历史键，其他 Blackbox 不变。

### 4.3 证据和派生状态

算法等价、执行环境、入库证据分别绑定自身真实输入，不伪装成同一次运行。

- 仅 ID/Metadata/文件位置/平台包装变化时，核验算法字节及允许差异，重新计算真实 exact version。
- 旧 backtest 不改名成新版本“重新执行成功”；临时迁移入口在现有 Harness 结构记录验收回执，
  绑定旧等价证据、包装差异、本机输入与一次标准调用，不放宽普通新算法入库。
- 状态验证结构、算法、数值环境和历史依赖；仅包装变化可受控重建身份封装，
  保存原封装摘要与来源，派生数组不变。不伪称原封装未改变。
- 优先本机已验证状态，不默认跨架构复用；无法复用先解释原因和最小重建范围，不自动全量重训。
- 历史回放不推进生产状态；平台仍负责完整性、路径安全、独占和标准 Result 后的原子发布。

### 4.4 结果保全和精确清理

只用第 2 节清单，禁止后缀模糊删除，其他 Blackbox 不受影响。

1. 每机独立导出目标行、关联键、数量、范围与摘要；备份并隔离恢复验证。
2. 原 ID 已存在的键原样保留，即使临时 ID 方向不同；原 ID 缺失且源唯一有效才补入；
   来源不完整或歧义则停止，不猜测、不自动重算。
3. 仅经 repository 创建新本地主键/导入记录，保留日期方向及真实来源；
   extra/summary 记录临时 ID、源 version、输入/结果摘要、备份引用与身份转换。
4. 导入是历史结果物化，不是新版本自然执行；验证原摘要、逐字段结果和 run/backtest XOR。
5. 原 ID 接管及回滚边界就绪后，按依赖顺序删除专属 prediction、回测子表、run、
   Harness、version、Registry。先证明无外部引用，共享对象不删除。
6. 永久备份、Git 历史和 immutable archive 保留；不存在的临时记录不要求创建。

## 5. 执行阶段与出口

### A. 冻结现场与控制器

更新本文、根规范、当前状态和入库导航；旧冲突步骤只存 Git 历史。
冻结双机 current/previous、archive、数据库身份/schema、Registry、零 running run、
prediction/backtest 数量/范围/方向、Actuals、Dashboard；冻结 generation、五文件 SHA、
business digest、catalog、snapshot、Request。UUID 只保存在受控证据，不写公开文档。
核实 installed/loaded、进程、日志、next trigger；调整既有自动推进任务以禁用旧跨 ID 路径，
每批一个控制器。K5 既有初始化已完成并保全，后续只复用；不得再次启动该初始化。

**出口**：每方案有下一动作、有效产物可追溯，没有第二 Writer/控制器或未知权威状态。

### B. 最小平台能力

实现同 ID 切换/逆向恢复、目标组合、horizon 投影、证据/状态转换、缺失结果补入和精确清理。
只保留一个临时 CLI 承载 preflight/apply/rollback/cleanup；写库仍在现有 repository。
只读 preflight 输出规范 JSON/SHA，绑定 DB 身份、release、版本、目标、输入/状态、
影响行数和备份标识；apply 锁内重读，漂移使旧 plan 失效。
隔离 MySQL 验证原子性、重复执行、恢复和引用；独立审查 Critical/Important 清零。

**出口**：相关合同、隔离事务/恢复、审查通过；文档说明不代表代码已实现。

### C. ECS 17 / 21 接管

顺序：**W3B → W2/W3A → W1A/W1B → W3C/W3D**。
已有等价/状态直接复用；W3C 保留 `platform_live_pit_variant`。
W3C/W3D 每个最终算法版本只补一次缺失完整同输入对照，产物复用入库，不重复计算。

每批：预安装 clean archive 和本机状态 → fence 相关 cadence 新触发并等在途任务退出 →
重验权威状态 → 围栏内切 release、原 ID 事务、补入核实缺口 →
标准调用/Registry/Dashboard/原事实/唯一 Writer 联合验证 → 恢复 timer 并读回 next trigger。

文件系统和数据库不是同一事务；中间失败保持 Writer 关闭，按冻结 release/版本/状态补偿恢复。
不顺带停止 DataBridge、Actuals 或其他无关服务。
**出口**：ECS 17 原方案/21 target Blackbox 接管，W4 无 ECS 执行资格。

### D. Mac3 源码方案晋级

使用 ECS 已验证的同一 archive，不另构建“Mac 版本”。
本机独立 preflight：环境、DataBridge、数据库、权限、状态；不复制 ECS 业务行。
每方案一次标准调用，已有证据不重跑，缺失状态只允许明确的一次初始化。
全部 17 个准备好才进维护窗口；核对 installed/loaded 后 fence 相关 launchd、
确认无在途 Writer，切 release、本机版本，必要时重启 Backend。
域名、认证、隧道不变；恢复后核验加载参数、日志、健康、Dashboard、唯一 Writer。
已有业务键只读比较，不提前写尚未到期日期；写库验证用隔离库或真实到期缺失键。

**出口**：Mac3 17 个原方案接管，历史展示和既有功能正常。

### E. Mac3 W4 加密方案

先隔离验证 .so ABI、导出入口、包内依赖及从五文件/Request 注入数据的真实能力。
不提供 DB 凭据，不恢复源库访问，不建模拟数据库，不调用旧 adapter/source runner。
binary bundle 包含标准 CLI、Metadata、固定 payload 清单，所有二进制及依赖进入 hash closure；
原 ID、五字段 Result、通用执行器，不加九种调度特例。
按 W4A → W4B → W4C 验证同输入日期方向与资源后接管。
同一新 archive 在 Mac3 验证 binary，在 ECS 验证平台和部署排除（不加载 Darwin .so），再双机晋级。
若只能读 DB、无法注入文件或缺包外依赖，停止对应方案并报告证据，不以薄包装冒充合同完成。

**出口**：Mac3 26 / 30 全部 Blackbox，W4 Mac-only。

### F. 清理和 confidence DDL

1. 建立原 ID Blackbox 回滚 release 边界，再备份/恢复验证、补入和删除临时身份。
2. 删除无调用者的 Native adapter/runner/cache wave/Gate/专属配置测试、source DB 注入。
3. 删除废弃候选和重复 canonical，不误删有效交付、其他 Blackbox 或 W4 payload。
4. 原 Native 历史事实/版本保留只读；历史 `native_adapter` 字符串不得成为运行身份。
5. 发布两份不读写 confidence 的 release，使双机 current/previous 都兼容。
6. 建立目标库恢复点，分别取得 ECS、Mac3 DDL 确认；仅经 `scripts/apply_migrations.py`
   执行 025，显式 expected database name/server UUID；先 ECS 验证，再 Mac3。
7. runner 支持两列存在、仅剩一列、两列已删除但 APPLYING 待恢复；其他形态拒绝。
8. 删除两预测表 confidence、模型/repository/前端空字段和 current schema 对应代码。
9. 删除临时 CLI、一次性测试，发布最终清洁 release；永久备份与旧 archive 不删。

**出口**：无 Native 执行路径、重复迁移身份或临时工具，DDL 已分别授权并验证。

## 6. 验收、预算、停止与回滚

| 对象 | 必须证明 |
|---|---|
| 算法 | 冻结 generation/五文件、Request、代码及环境绑定下，三个日期和方向逐行零差异 |
| 证据 | 包装差异明确，新 exact version 真实，旧报告不改标 |
| 事实 | 原摘要不变，仅增加核实缺失键，源结果相同，XOR 成立 |
| 状态 | 正确身份可复用，损坏/越权/不兼容失败，独占与原子发布成立 |
| 事务 | 任一步故障无半激活、半发布、部分删除；重复执行不重复写 |
| 调度 | 标准入口、唯一 Writer、已有键不覆盖、next trigger 正确 |
| 产品 | 原 ID 可见、重复 ID 消失，其他方案/Actuals/认证/输入不变 |
| 发布 | 进程、manifest、current 与批准 archive 对应，两端各自证据完整 |

不恢复三遍回测、排列矩阵、多天自然观察。区分同 Request 命中、新一天增量、历史修订重建；
已有有效证据不重跑，缺少正常增量只补一次。人工模拟不伪称自然触发。

- 日常预测 ≤120 秒、RSS ≤4 GiB、数值线程 ≤8；初始化/完整对照独立计时。
- 重型 W3 离线沿用 7200 秒预算；100 条 batch 不作为额外重复计算门槛。
- 超时先查阶段耗时，不自动重试；Mac 按资源并行独立任务，不共享状态写锁；ECS 重型计算串行。
- 维护覆盖 DataBridge、Actuals、周/月触发及早间任务；跨触发点须预先安排围栏与恢复，不默默漏任务。
- 输入/Request 无法证明、日期方向差异、第二 Writer、版本/manifest 漂移、需覆盖原事实、
  备份不可恢复、引用不明、状态复用未证明、W4 违反输入边界：立即停止本批。
- 输入代际不同只标记 vintage mismatch，绑定同代再对照，不能豁免结果差异。

回滚先 fence Writer。清理前可恢复冻结旧版本/状态，新事实不删除；
原 ID 接管后旧实现遇重复键只 skip/reject；临时身份删除后只回滚到原 ID Blackbox；
DDL 后只回滚到 confidence-agnostic release。不得用全库旧快照覆盖生产而丢失无关新事实。

## 7. Git、release 和最终完成定义

审查全部未推送提交和整体 diff，排除凭据、运行产物及无关改动，更新远端引用核实并发提交。
分阶段最小提交并推送 develop，不 force push、不改写共享历史。
测试通过的 clean commit 构建两次 archive，字节一致；记录 manifest、SHA、安装、current/previous、
数据库/状态回滚边界。算法交付进 Git，状态、备份和大型结果外置；不得把 outputs 草稿整体提交。

创建/更新一个到 master 的 PR。可本机验证问题先验证；依赖现场的变更必须现场取得证据，
否则只提供现象/问题/影响/建议报告。master 不合并。
最终两机同 commit、同 archive，差异仅环境/部署配置/依赖；包内无 .git、草稿或临时工具。
每次进度报告实际动作、算法是否运行、证据、下一步和阻塞，等待计算时推进不冲突工作。

全部满足才闭环：

- [ ] ECS 17 原方案 / 21 target 原 ID Blackbox 接管。
- [ ] Mac3 26 原方案 / 30 target 接管，W4 Mac-only。
- [ ] 原事实未改，独有结果保全，临时身份精确清理，备份可恢复。
- [ ] Native 执行路径、Phase-A 依赖、source DB 注入删除。
- [ ] 两端 confidence DDL 独立授权完成，回滚边界有效。
- [ ] 公共合同、隔离 MySQL、恢复、调度、全量回归、独立审查通过。
- [ ] Dashboard、Backend、Actuals、其他方案和调度控制面正常。
- [ ] 双机 final archive 一致，current/previous 兼容。
- [ ] develop 已推送、远端 SHA 核对、PR 已交付，master 未越权修改。
- [ ] Markdown 已更新，临时工具清理，备份与发布证据保留。
