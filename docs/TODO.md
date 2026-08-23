# 统一后续推进计划

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-23

本文是仓库当前唯一实施计划。当前稳定事实见[当前状态](CURRENT_STATUS.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成事项在验收后从本文删除，
通过 Git、Harness、数据库和目标机 journal 追溯，不在工作树长期保留关闭清单。

## 当前主任务：M0 月均、季均、年均 15 方案入库

### 1. 目标与范围

参考收包 `blackbox-v2-20schemes-m0` 包含周均、月均、季均、年均四族共 20 个 Blackbox V2 方案。
其中 `m0_weekly_avg_{1y,3y,5y,7y,10y}_v1` 五个周均方案已经在 ECS 完成 Intake、技术 Gate、
shadow、持久化回测、activation、单日 `gray_live` 和 DashboardGate，本计划不重复处理。

本轮只处理余下 15 个方案：

| 业务族 | `task_type` | 方案数 | 期限 | `horizon` | 业务桶 |
|---|---|---:|---|---:|---|
| 月均 MID | `monthly_average` | 5 | 1Y / 3Y / 5Y / 7Y / 10Y | 1 | 上月 16 日至本月 15 日 |
| 季均 CQ | `quarterly_average` | 5 | 1Y / 3Y / 5Y / 7Y / 10Y | 1 | 自然日历季 |
| 年均 SF | `annual_average` | 5 | 1Y / 3Y / 5Y / 7Y / 10Y | 1 | 春节后首个交易日至次年春节前最后交易日 |

本轮部署范围固定为 ECS 独立灰度实验室 `aliyun-gray`。不把 15 个方案同时挂载到 Mac3，不移动或推送
`master`，不修改生产域名、Nginx、DNS、Mac3 Writer，不新增 systemd timer、launchd plist、数据库表或
第二调度控制面。Mac3 后续是否接收同一份 ECS 已验证 archive 是独立晋级项目。

当前操作机的原始收包目录为
`/Users/qiyo3/Downloads/blackbox-v2-20schemes-m0`。该路径只作为本轮输入定位，不是仓库权威路径；
实施时先冻结摘要并建立工作副本，不直接改写原始收包。`_交接自验材料/` 只作复现证据，不属于正式两文件交付。

### 2. 已确认且不得重新解释的业务语义

1. 三个新增任务均为 Blackbox V2，`horizon=1` 表示下一个同类业务桶，不是一天，也不得改成
   `30/90/365`。
2. 月均 MID：交易日 `day <= 15` 归当月桶，`day >= 16` 归下一月桶；锚点是本月 15 日及以前最后一个
   交易日，`daily_cutoff_key=feature_date=锚点`。
3. 季均 CQ：使用自然日历季；锚点为季末及以前最后一个交易日，
   `daily_cutoff_key=feature_date=锚点`。
4. 年均 SF：春节只能由每年 1 月 1 日至 3 月 15 日交易日序列中的唯一最长停市间隔识别；最长间隔不足
   6 天、出现并列或日历覆盖不足时必须报错。不得回退到最早数据、自然年或人工猜测日期。
5. 算法信号继续使用交付 M0 公式：`gap = 当前桶收 - 当前桶均`，`gap >= 0` 输出 `+1`，否则输出
   `-1`。平台不重新实现或调参贴合该信号。
6. 实际方向统一为“下一桶平均收益率相对当前桶平均收益率的方向”。历史回测、gray live、Actual、指标和
   Dashboard 必须复用相同桶边界。
7. `target_date` 只使用平台既有的下一业务桶指针语义，不新增桶身份表、业务 bucket ID、季度 cutoff key
   或年度 cutoff key，也不得从 `horizon` 反推任务类型。

### 3. 实施原则

- 能在公共层一次解决的问题只实现一次，不在 15 个方案中复制平台业务逻辑。
- 正常输入下不改变 M0 方向；本轮允许的交付修正只限输入完整性、日历校验、失败语义和必要的批量性能
  优化。任何正常样本方向变化都必须停止并查明原因。
- 平台负责 Request 日期、权威日历、桶边界、输入 artifact、Actual 和历史标签；交付脚本负责消费平台输入、
  计算 M0 信号，并对自己收到的非法或残缺输入 fail-closed。
- 原始收包不修改；工作副本中的 15 个正式方案仍各自严格只有
  `{scheme_id}.py + {scheme_id}.json`。
- 所有平台代码、方案 Intake 和文档更新都必须在 `codex/develop` 形成稳定 commit 并推送
  `origin/codex/develop`。ECS 只使用精确提交构建的 immutable release，不保留 Git checkout 或现场修改。
- Gate、回测、激活、业务写入和服务操作按各自事务边界执行；技术 Gate 通过不外推为后续副作用授权。

### 4. 阶段 A：冻结交付与身份矩阵

- [ ] 核对 Git 工作区、当前分支和远程分支，避免混入其他任务、未跟踪方案或 `outputs/`。
- [ ] 计算原始收包和 15 个正式目录中 30 个文件的 SHA-256，核对目录名、脚本名、Metadata
      `scheme_id` 一致。
- [ ] 核对 `name=M0`、`owner=fengrl`、期限、任务类型、`horizon=1`、target rule、消费列和描述。
- [ ] 核对 composite Registry ID 不与现有 Registry、owner registry 或方案目录冲突。
- [ ] 记录上游自测输入范围、摘要、样本数、准确率和代表性合同测试范围；明确上游 `81/81` 不能替代
      15 个 exact version 的平台 Gate。
- [ ] 建立工作副本，后续修正不改写原始收包。

**停止条件**：文件不再是严格两文件、Metadata 组合非法、身份冲突、输入凭证无法对应选定 DataBridge
generation，或冻结后源文件发生变化。

### 5. 阶段 B：一次性公共加固与同构修正

#### 5.1 平台公共层

- [ ] 用聚焦测试证明 `shared.period_average_buckets` 已覆盖 MID/CQ/SF 边界、连续权威日历、唯一春节
      间隔、完整交易日集合和有限桶均值。
- [ ] 沿 live、history、Actual 三条调用链核对完整性检查是否都在算法执行或业务写入前发生。
- [ ] 如果 live 执行链确有缺口，只增加一个供三种周期任务复用的预执行完整性校验；若已有完整覆盖，则不为
      形式统一新增包装层、第二份桶对象或重复校验器。
- [ ] 补齐正常和异常测试，证明不影响 `monthly`、`weekly_average` 和其它存量任务。

#### 5.2 15 个正式交付

- [ ] 以月均、季均、年均三个同构模板批量修正，只允许 scheme ID、期限和消费列等声明差异。
- [ ] 15 个方案统一声明并消费 `api-wind-date-v1`，由权威交易日集合验证当前桶。
- [ ] 禁止使用 `dropna()` 后继续计算残缺桶；缺日期、重复日期、空值、非有限值或桶外日期均须失败。
- [ ] 年均检查最长停市间隔唯一且不少于 6 天，删除“没有边界则使用最早数据”的 fallback。
- [ ] 保留 Request 七字段、Result 五字段、截止日隔离、原子写 Output 和空 stdout 等 Contract 1.0 边界。
- [ ] 正常样本方向保持不变；若正式脚本字节变化，15 个 Metadata 的 `algorithm_version` 统一从
      `1.0.0` 升为 `1.0.1`，并重做与新版本绑定的复现证据。

**停止条件**：需要改变 M0 公式、方向映射、业务桶、target rule，或加固后任一正常金标方向变化。

### 6. 阶段 C：合同、金标、异常和性能验收

- [ ] 对 15 个 exact delivery 分别运行 predict、backtest、两者一致、重复执行确定性、Request 变序与
      分批不变、未来行隔离和输出字段检查。
- [ ] 使用冻结自验数据逐桶复现金标，要求方向零差异、样本数和准确率一致。
- [ ] 对比修正前后正常输入输出，要求每个 Request 的方向一致。
- [ ] 异常用例覆盖缺/重复交易日、空值、非数值、无穷值、残缺 MID/CQ 桶、春节间隔不足 6 天、最长间隔
      并列、日历窗口不完整、cutoff/feature 不一致、Request 字段错误和重复 request ID。
- [ ] 所有失败必须非零退出、stdout 无业务结果、不留下部分 Output、不写数据库。
- [ ] 批量测量 15 个方案的单条、100 条、完整历史区间耗时、峰值 RSS、输入/环境摘要和
      `fallback_used`。
- [ ] 准入线为 predict 不超过 120 秒、100 条不超过 600 秒、完整区间不超过 1800 秒、峰值 RSS 不超过
      4 GiB、`fallback_used=false`。
- [ ] 已满足准入线时不做无收益重写；超限才优化同构执行结构，并重跑全部验收。
- [ ] 每个 exact scheme 形成独立性能结论，共同环境可以复用，不能用代表方案替代其余方案。

**停止条件**：金标差异、Contract 失败、未来数据泄漏、fallback、性能超限，或证据无法绑定精确摘要。

### 7. 阶段 D：稳定提交、Intake 与推送

1. **公共加固提交（条件提交）**：只有证明平台 live 调用链有缺口时才产生；包括唯一公共实现、聚焦测试和
   必要规范。测试通过后立即推送。若无需平台代码修改则跳过，不为凑提交增加冗余代码。
2. **15 方案 Intake 提交**：逐方案执行正式 Intake，注册
   `platform_inputs: [api-wind-date-v1]`，生成 `paused/draft` 初始配置，登记 owner 和仅
   `aliyun-gray` 的部署矩阵。15 个本地 exact version 全部通过后形成一个稳定提交并推送。

提交前再次核对工作区；不得提交自验数据、临时 Request/Output、回测产物、缓存、DSN、数据库 UUID、凭据
或无关草稿。

### 8. 阶段 E：不可变 release 与 ECS 晋级

- [ ] 从 Intake 精确提交重复构建两次 archive，要求字节一致，记录 Git SHA、release ID、archive SHA-256
      和 manifest。
- [ ] 校验 archive 不携带 `.git`、临时数据或本机路径依赖。
- [ ] 切换前只读核对 ECS current/previous、installed unit/timer、运行批次、数据库身份、migration
      018–020、DataBridge current、runtime profile、环境 manifest 和资源。
- [ ] 本轮预计没有新 migration；若 schema 与已确认状态不一致，停止并单独设计，不顺带 apply。
- [ ] 解压到新 release，保留原 current 为 previous，受控切换；不在 ECS 执行 `git pull` 或现场改文件。
- [ ] 切换后核对 release、静态资源、Backend、原有 active 方案和五个 timer，确认没有存量回归。

**停止条件**：构建不确定、摘要漂移、ECS 有运行批次、环境/数据库身份漂移、需要未知 DDL、installed unit
不一致或回滚点不可用。

### 9. 阶段 F：Gate、Shadow 与持久化回测

顺序固定为月均五方案、季均五方案、年均五方案。逐 exact scheme 执行：

`StaticGate -> InputGate -> UnitGate -> CompareGate`

- [ ] Gate 绑定当前 exact version、选定 generation、combined snapshot 和正确 target tenor。
- [ ] 一个方案失败只阻断该方案及本族后续副作用，不激活、不补 gray live。
- [ ] 通过后执行独立 shadow，确认零业务写入和日期语义正常。
- [ ] 执行持久化 canonical backtest；预测、Actual、指标和月度明细使用统一周期桶。
- [ ] 核对 Request 数量、日期范围、方向分布、指标、lineage，并证明 backtest 与 gray live
      `target_date` 不重叠。

### 10. 阶段 G：激活、gray live 与 Actual

每族五方案全部通过后逐 exact scheme 激活。单人维护使用直接命令授权，不生成密钥或自签 token；仍记录
operator、scheme、version、Harness run 和 operation hash。

- [ ] 原子建立 exact version active、composite Registry active 和 ECS 生命周期 `active/active`；
      immutable `config.yaml` 保持 `paused/draft` 初始声明。
- [ ] 从 ECS 权威日历计算最近合法 MID、季度末和春节前锚点，不手填或猜测日期。
- [ ] 用单日 gap fill 补 insert-only `gray_live`；已有键拒绝覆盖，不改历史预测、不伪装
      `scheduled_live`。
- [ ] 目标桶完整时更新 Actual；尚未结束时保留 pending，不误显示为零或错误方向。
- [ ] 重跑 gap plan，要求 `present=1 / actionable=0`。

### 11. 阶段 H：DashboardGate 与页面验收

- [ ] 月均、季均、年均在五个 Y 标的下各新增一个 M0 候选，即每列五方案、方案总数增加 15。
- [ ] Dashboard API、任务格子数量之和与页面总数一致；异常数据不误显示为零。
- [ ] 排行、来源、说明、回测指标、月度明细、gray live、Actual 状态和详情正常。
- [ ] 15 个方案分别通过 DashboardGate，并抽查原有 T+1、T+5、周收盘、周平均、月中收没有回归。
- [ ] ECS loopback 页面无静态摘要漂移、接口错误或 JavaScript 异常。

### 12. 阶段 I：自然调度与后续观察

三个任务继续复用现有 close-period systemd one-shot：工作日 18:00 到期判断，不新增三个 timer，不修改
触发时间。

- [ ] installed service/timer 与 current release 一致，timer 为 `enabled/active/waiting`。
- [ ] 非到期日返回 `not_applicable / exit_code=0 / refresh_required=false`，不写 run/prediction。
- [ ] 候选只由 `active/active`、部署矩阵和 task type 选出，不由 frequency、horizon、per-scheme cron 或
      第二控制面推断。
- [ ] 记录下一次 MID、季度末和春节年前锚点；到期后读回 journal、`scheduled_live`、输入 generation、
      Actual 和 Dashboard。

入库时可声明“调度已挂载、到期选择已验证”，但真实日期前不得宣称季均或年均已完成自然运行，也不得通过
伪造日期、kickstart 或倒签信号提前取证。

### 13. 文档、完成标准与回滚

完成后更新 `CURRENT_STATUS.md`、本计划和必要 SOP，记录 exact version、Gate/backtest/DashboardGate
结论、release SHA、archive SHA-256、gray live、Actual 状态、自然观察窗口和回滚点。纯文档更新形成独立
稳定提交并推送；若分支 HEAD 晚于 ECS release，必须说明差异仅为文档。

只有同时满足以下条件，才宣布“15 个周期均值方案完整入库”：

1. 15 个 exact delivery 全部通过合同、金标、异常和性能准入；
2. 15 个方案完成 Intake、技术 Gate、shadow 和 canonical backtest；
3. exact version、Registry 和 ECS 生命周期全部 active；
4. 合法 gray live 已 insert-only 写入，或有可审计的未到期/pending 原因；
5. Dashboard 三列各五方案、总数增加 15，全部 DashboardGate 通过；
6. close-period timer、到期选择和非到期行为正确；
7. 代码和文档稳定提交均已推送，release、输入、Gate、写入和页面结果可追溯。

源码 symlink 回滚只回退代码，不回退数据库、Registry、生命周期或 insert-only prediction。激活后出现问题
时先阻止对应方案进入新调度候选，再按独立授权修复；不得删除或覆盖历史预测。

## 其他未闭环队列

1. Mac3 周期均值基础由已派发任务推进：只读取得数据库身份，受控应用 migration 020，并且只能晋级 ECS
   已验证 release `00558d175cdffd0d2aae4b51e1aea6e8adda8923` 与 archive SHA-256
   `8f408ddb960ea514a45dc8cd9613a4e42515b922f1fc6a3f8015de2ddfeddc12`，随后独立替换、加载并读回
   monthly plist。当前任务不接管该派发工作。
2. `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 等待 2026-08-24 07:03
   Asia/Shanghai 的首次真实 daily 自然触发；不得 kickstart、覆盖日期或倒签信号。
3. M0 周平均五方案及同期 ECS 周频方案等待 2026-08-29 11:30 的首次自然触发。
4. 独立诊断 Mac3 2026-08-21 daily 的 17 个 `execution_failed`；没有新业务写入授权时不得手工重跑。
5. 未来若把生产域名或 Writer 从 Mac3 切到 ECS，必须作为新生产项目设计数据库 authority、单 Writer、
   DNS/Nginx、窗口和回滚，不能从灰度验收外推授权。

ECS 继续独立灰度运行；Mac3 域名、Nginx、DNS、数据库 authority 和生产 Writer 保持不变。

## 统一停止条件

- 需要改变 M0 信号、方向映射或已确认的 MID/CQ/SF 语义；
- 需要新增 schema、timer、plist、常驻 scheduler、ledger、第二写入入口或隐式 fallback；
- 需要覆盖或删除 insert-only 历史预测，或把 gray live 冒充 scheduled live；
- 文档、代码、输入摘要、数据库身份、release manifest、installed unit 或现场状态与计划前提不一致；
- 出现无法由权威日历、精确版本、持久化 Gate 或现场读回消除的不确定点。

触发停止条件时保留现场和证据，向用户说明确定事实、影响和需要选择的事项，不猜测继续。
