# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-25

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

- Mac3 在本轮仓库减负期间保持 `e93ea24518dbf26870473d9a201b310c38d5c5d2`，不提前同步中间清理版本。
- ECS 可按 `codex/develop` 的已验证 immutable cleanup release 逐次先行晋级；尚未安装的工作区修改不视为
  已部署。精确 commit、archive、manifest、source-tree 摘要和 previous 以目标机 `current`、release manifest
  与部署记录为准，不在本文复制一份容易漂移的运行态清单。
- Mac3 生产应用从 `/Users/macstudio0/bond-factor-lab-production/current` 启动，运行状态位于
  `/Users/macstudio0/bond-factor-lab-runtime`；ECS 从 `/opt/bond-factor-lab/current` 启动。两端运行时均不引用
  Git 工作区。

## 单维护者减负

- Harness 人工副作用已使用直接命令和精确 scope 审计，不再维护密钥、token、nonce、TTL 或 replay store。
- Blackbox lifecycle 只保留 `shadow-register`、统一 `activate` 与唯一 `lifecycle-reconcile`；Native
  maintenance 只接受标准 prior business identity snapshot，不保留方案级 receipt 例外。
- 自动入库只运行一次正式 `onboard` 并持久化后续 lifecycle 所需证据；已删除不能用于 activation 的重复
  `onboard --check-only`、独立 `signal-gap-plan` 和本地 `harness report` CLI。`signal-gap-fill` 内部仍先执行
  同一只读 planner，任一 blocker 都在算法或 repository 写入前终止。
- Native BacktestGate 仍执行完整 `--no-persist` 并核验受保护表零写入，但不再用首次输出自举本地 JSON
  baseline；状态、样本数和摘要只作为 Gate evidence 持久化，source benchmark/CompareGate 继续负责算法保真。
- CompareGate 不再生成 `comparison_summary.json` 或 `comparison_diff.csv`；计数、missing/extra key、字段 mismatch
  和指标差异均直接进入 `t_harness_gate_results.summary_json`，不维护本地与数据库两份审计结果。
- `signal-gap-fill` 的 planner/result 只输出到标准输出，不再创建时间戳报告目录或保存重复 plan JSON。
- Blackbox 自动入库只保留 `static -> input -> unit -> compare`；原 dry-run 已由同参数的 Compare 冒烟覆盖，
  重复的抽样 no-persist backtest 与 `--sample-size` 已删除。Blackbox `gate backtest` 只接受明确的
  `--persist`，Native dry-run/no-persist backtest 保持不变。
- 生产 release identity 只来自 launcher 注入的 `BFL_RELEASE_COMMIT`；开发态 Harness 不再额外执行 Git
  subprocess。immutable release 构建的 clean-HEAD、archive 与 manifest 校验保持不变。
- 已完成设计稿、重复架构总册和拆分的 Native T0/验证手册已从当前工作树删除，通过 Git 历史追溯。

## 每日预测 Phase-A 后缀重算

- Liwei Phase-A 已具备唯一的有界 suffix 决策：兼容 current generation 下的可定位 daily 历史修订使用
  `suffix / proven_daily_input_revision`；同时存在有效辅助输入修订时使用
  `suffix / combined_daily_effective_revision`，cutoff 从最早 daily 修订日前移五个交易日。
- 该性能问题已经完成专项验收；一次性 oracle、故障注入和迁移兼容 pytest 已删除。生产实现未修改 Native
  source-backed 算法、输入口径、07:03 调度、数据库写入或生产控制面；下一次自然 revision 的真实耗时仍以
  运行态证据为准。

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

- 15 个方案均已完成上游范围内金标、性能、Intake、四段技术 Gate、shadow、canonical backtest、
  activation、合法 gray-live 与 Dashboard 验收，并在两端保持相同业务键和值。
- canonical backtest 与 `target_date >= 2026-06-01` 的 live 区间零重叠；已到期月均、季均 Prediction/Actual
  已闭环，未到期季度和年度桶继续显示 pending，不伪造自然观察。
- 跨主机补齐只复制同一 release/版本/日期/lineage 下源端已有的精确核心结果，由目标 repository
  insert-only 写入；数据库主键、run、Actual、回测和 Harness 历史均未复制。
- 具体 exact version、Harness run、backtest/run ID 与 DataBridge snapshot 属于已完成操作证据，只从 Git、
  数据库和目标机 journal 追溯，不在当前状态文档维护第二份逐方案表。
- 它们仍需 installed 宿主控制面在权威锚点自然产生 `scheduled_live` 后，才能标记 Production Observed。

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
