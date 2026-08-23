# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-23

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

- Mac3 与 ECS 的 `current` 均为
  `71a31b0d14bb88d383ea2dc5b495cdc984001461`，`previous` 均为
  `eca1f0fa9fb6eb8c0d0e1b933634d7940df7ba25`。
- 当前 source archive SHA-256 为
  `41917526cb2613bd3538dc9673099107c74cf8bcef795f624971a795fe218f17`，manifest SHA-256 为
  `b57208ebdc5c653497bafa165e7c2ebf5fb8215851011a8c3543121e4d57dcd7`，安装后 source-tree SHA-256 为
  `73897aaef6fb0ff68965280638001974ba22ec749fcd6bd91646b957e7b45be0`。
- 该 release 包含 immutable Native 子进程数据库身份修复和 Liwei 私有缓存完整输出比较回调修复；完整测试为
  `1142 passed + 500 subtests passed`，Liwei 聚焦测试为 `144 passed + 26 subtests passed`。
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
- 参考包中的周均五方案已在 ECS 完成入库；余下月均、季均、年均 15 个方案仍未 Intake，继续由
  [统一后续推进计划](TODO.md)管理，不属于本次 Mac3 基础晋级闭环。

## Mac3 调度与 Dashboard 终态

- 七个 installed/loaded plist 的只读 drift audit 为 `ok=true`；Backend 与 SSH tunnel 正在运行，其余
  one-shot 当前空闲。daily 保持工作日 07:03，weekly 保持既有触发，monthly 每日 18:00。
- 2026-08-21 日频缺口终态为 `expected=48 / present=48 / actionable=0 / blocked=0`；2026-08-22 周频缺口
  终态为 `expected=21 / present=21 / actionable=0 / blocked=0`。
- 前一轮新增 27 个唯一 insert-only `gray_live` 业务键，其中 19 条由 ECS 同 release 的既有
  `scheduled_live` 核心结果精确复制，3 条 ECS 不存在的周均结果在 Mac3 受控计算，另 5 条在此前聚焦补缺
  中完成；随后对 8 个近期 Blackbox 方案完成 `2026-06-01` target 边界重分区。Mac3 直接复用各自旧
  canonical 回测结果，新增 188 条 insert-only `gray_live`，其中两个日频方案各 58 条、六个周频方案各
  12 条；`t_scheme_predictions` 当前为 3281 条。迁移记录的方向、置信度、`feature_date`、`target_date`
  和 actual/准确率事实与 Mac3 源回测逐条一致，未重新执行算法。
- 8 个新 canonical backtest run 为 `227–234`，每个 run 的最大 `target_date` 均为 `2026-05-29`；旧 run
  保持不可变且不再被 latest-success 规则选中。新 canonical 与 live 的 target 零重叠，受影响方案的回测
  月份止于 2026-05、gray live 从 2026-06 开始，`2026-08` 只保留一条 live 月度展示。
- Mac3 当前有 73 个 active base scheme、77 个 active composite Registry。单一 Dashboard 快照 HTTP 200，
  77 行中 `present=69 / not_due=8 / missing=0`；重分区后 73/73 base DashboardGate、77/77 composite target
  全部通过。
- Backend `/api/health` 正常；本机与公网
  `https://bond.finailab.cn/bond-factor-lab/` 页面和 Dashboard 均 HTTP 200，公网 HTML/JS/CSS 摘要与当前
  release 字节一致。

## ECS 灰度状态

- DataBridge、daily、weekly、monthly、Actuals 五个 timer 均保持 `enabled/active/waiting`；Backend
  active，loopback 页面和 Dashboard 均 HTTP 200。
- installed DataBridge、daily、weekly、monthly service 均不读取历史
  `/run/bond-factor-lab/manual-run.env`；monthly timer 每日 18:00 运行 close-period 到期判断。
- ECS 同样完成上述 8 个方案的本地结果重分区：新增 188 条 insert-only `gray_live`，
  `t_scheme_predictions` 当前为 3259 条；新 canonical backtest run 为 `229–236`，最大 target 均为
  `2026-05-29`，与 live target 零重叠。ECS 逐条保留自己的源方向和准确率事实，不用 Mac3 结果覆盖；
  两端历史 snapshot 原有的 8 个方向差异继续保持。ECS 当前 64/64 base DashboardGate、68/68 composite
  target 全部通过，loopback HTML/JS/CSS 与 Dashboard 均 HTTP 200。

## 新方案入库状态

- `weekly_1y_causal_v1_31_0_standalone` 已完成技术 Gate、shadow、持久化回测、activation、单日
  `gray_live` 和 DashboardGate，Registry 为 active。
- `m0_weekly_avg_{1y,3y,5y,7y,10y}_v1` 已逐方案完成 Blackbox Intake、技术 Gate、shadow、持久化回测、
  activation、单日 `gray_live` 和 DashboardGate；五个 composite Registry 均为
  `active + weekly_average`，部署范围仅为 `aliyun-gray`。
- `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 已在 ECS、Mac3 分别完成 Blackbox
  技术 Gate、shadow、完整持久化回测、activation、单日 `gray_live` 和 DashboardGate；两端仍等待首次真实
  `scheduled_live` 自然触发，不能由 gray live 或人工运行预先宣称 Production Observed。

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
