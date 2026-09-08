# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-09-05

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和调度状态必须从各自权威数据源
读取；待推进工作见[统一后续推进计划](TODO.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成迁移、逐次 Gate、运行 ID、
发布窗口和一次性验收证据不在工作树维护副本，通过 Git、Harness、数据库与目标机 journal 追溯。

## 双主机边界

- Mac3 继续承载生产域名、前端、数据库和 Writer；`launchd + installed plist` 是生产调度控制面。
- 2026-09-08 Native 迁移授权收紧为仅 ECS：本阶段不晋级或改变 Mac3 的版本、数据库、launchd、域名与流量；
  Mac3 晋级及 W4 改造暂停，恢复须另获授权。算法等价与正式入库证据各自绑定输入，细则见迁移计划。
- ECS 是独立灰度实验室，使用自己的 MySQL、DataBridge、Registry、run、prediction 和 systemd timer；
  Backend 只监听 loopback，不承载生产公网流量。
- 两端不建立持续复制、双写、共享数据库或共享 DataBridge。经明确授权的单次缺口修复可以在停止目标 Writer
  后，从同一已验证 immutable release 的源端只读导出精确业务键与核心预测结果，再由目标端 repository
  insert-only 导入；不得复制数据库主键、`run_id`、Actuals、回测或 Harness 历史。
- 两端共用唯一 `codex/develop` source release 代码线；ECS 先验证、Mac3 后晋级时允许 `current` 不同，
  不因此建立环境分支。

## 现场状态读取

- 精确 release、previous、archive 与 source-tree 摘要只从目标机 `current`、release manifest 和部署记录读取；
  尚未安装的 Git 工作区修改不视为已部署。
- Mac3 的 installed plist、`launchctl`、进程、日志和本机数据库是生产现场权威；ECS 的 installed systemd
  unit/timer、journal、进程和本机数据库是灰度现场权威。
- active 方案数量、DataBridge generation、run、prediction、Actual、Dashboard/health 与跨机差异均为运行态，
  必须现场只读查询，不在工作树维护快照。
- 人工 gap-fill、Actual 刷新或 gray live 不能冒充首次自然 `scheduled_live` 或 Production Observed。

## 当前治理边界

- Native config active；Blackbox 本机数据库 exact version active、Registry target active；再加 cadence 匹配，
  才能进入一次性 runner。自然运行写 `scheduled_live`，单日或获批 target 区间补缺只写 insert-only `gray_live`。
- 后续新方案的历史段由一次持久化 backtest batch 形成 immutable canonical backtest；首次 Blackbox
  `activate` 在激活 exact version 与 composite Registry 的同一事务中，将该回测逐点 insert-only 发布到
  `t_scheme_predictions`。激活后的连续 gray 缺口由一次 live-safe target 区间 batch 物化。区间不得早于平台 live 起点，一个方案只解析一次
  DataBridge authority、核对 producer-ready receipt、物化一个私有运行视图并启动一个算法 batch；任一既有业务键整组拒绝，全部
  prediction 在一个 repository 事务中提交。不跨激活保存候选结果，也不以性能理由放宽 cutoff、版本、
  lineage 或唯一键安全门。
- 跨主机补缺优先复用同一 immutable release 下已存在的精确预测结果；源端必须只读，目标端 Writer 必须先
  停止，release、方案版本、日期和业务键必须完全匹配，已有键整组拒绝。源端不存在的键才允许受控计算。
- 新方案只走 Blackbox V2 两文件 Intake；Native V1 只维护政策清单内存量身份。平台不反编译或改写
  Blackbox 算法逻辑，只验证平台接入和标准输出边界。
- 仅 Native→Blackbox 迁移映射中锁定的 W4 九个 Mac3 加密方案允许使用 manifest-bound Mac3-only binary
  bundle；该例外不进入 ECS、不开放给新方案，全部 `.so` payload 必须纳入 exact version hash closure，且
  仍须遵守五文件输入、五字段输出、无数据库/网络/包外路径/持久状态的边界。
- Blackbox 入库只保留“Intake → 一次完整持久化回测 → activate”；DataBridge producer 独立发布 generation，方案不构建、修复或重验 generation。Blackbox 不进入 Native `onboard`，不运行 StaticGate、CompareGate、额外 predict 冒烟或 `shadow-register`。
- Blackbox `weekly_point/h1` 与日频 `T+5/h5` 的 target 半开区间批量已经在 ECS 与 Mac3 验证；日频
  `T+1/h1` 的同类能力已完成本地候选实现与回归，尚未做现场验证。一个方案只启动
  一个 batch，全部业务键由现有 repository 原子提交，下一自然调度 target 必须保留不占用。
- Native 单日补缺复用自然调度的持久 Phase-A cache，并按 base scheme 独立提交。人工补缺只允许 hit 或
  单日 append；自然 daily one-shot 的已批准 publisher 可处理已证明且不超过 32 个交易日期的 suffix，
  consumer 只读。`full`、未知修订或 cache 身份漂移必须在训练前失败。
- Native daily/weekly/monthly one-shot 与单日补缺使用作业级临时输入：同一精确 builder identity 只构建
  一次，各方案读取只读硬链接；publisher 与其余 Native 分两阶段、每阶段最多两个 worker。one-shot 只做
  一次 discovery 并共享一个 Engine，作业结束不保留日常 `runtime_inputs`。
- Blackbox 完整历史回测对一个方案只启动一个算法进程；灰度区间直接核对 producer-ready receipt，并为每个
  方案只建立一次临时私有 runtime view。平台不再裁剪、重写或永久保存 gray replay session。
- Blackbox lifecycle 以本机数据库中的 exact version 与 composite Registry 为唯一运行权威；当前代码和
  `previous` release 都不再读取 config overlay、lifecycle journal 或 reconcile 状态。两端当前 runtime root 内
  经授权的旧 lifecycle 文件已在无读取者、无打开文件和调度 idle 的条件下删除，不影响当前执行或
  `previous` 回滚；release archive 保留。
- 当前代码线的 Dashboard 合同为 `factor-lab-dashboard-v5`。`t_backtest_runs` 与
  `t_backtest_predictions` 只保存不可变回测证据；`t_scheme_predictions` 是唯一产品逐点事实源。Dashboard
  只从产品事实表计算 Summary/Detail，回测元数据可读取 `t_backtest_runs`，但不得用回测明细参与产品逐点选择。
  `backtest/live` 只由 `target_date < 2026-06-01` 或不小于该日期推导；`gray_live/scheduled_live` 仅属于
  `t_scheme_runs` 运行审计，不进入产品事实或公开 API。无查询参数时返回月度 Summary，单方案单月逐日明细只在
  严格 detail 请求中按需读取；Dashboard 不判断调度缺口，不增加第二条 API 路径。
- `t_scheme_registry.owner` 是方案来源的唯一展示权威；新 Blackbox Intake 必须提供合法 owner，历史缺少
  Metadata owner 的方案保留数据库权威值。Dashboard 读到缺失、占位或非法 owner 时整体 fail-closed，
  前端不使用仓库映射或空值兜底。
- 数据库 Migration 024 把既有 canonical 回测结果一次性发布到 `t_scheme_predictions`，增加互斥的
  `run_id/backtest_run_id` lineage，删除事实行的 phase 与 `updated_at`。回测发布行保留 immutable
  `backtest_actual_direction`，用于周末等没有 Actual 日期的历史目标；live 行该字段必须为空并继续关联 Actual。
  两端仍使用各自独立数据库，精确主机合同以现场 `current` release、migration history 和 API payload 为准。
- `api_wind_indicators_all.factor_version` 已在两端源表完成存量 `V1.0` 初始化。ECS 当前已发布五文件
  generation `full-20260905-063321-21c5c7188fa5`，其中 factor catalog 为 1474 行；精确文件摘要、ready
  receipt 与 current/previous 兼容性仍必须从 ECS 现场权威读回。Mac3 的 current generation 不从 ECS 状态
  推断，生产操作前独立核验。
- Mac3 production 与 ECS gray 的 installed plist/unit、服务状态、数据库写入、激活、补数和 DDL 都是
  独立操作，代码或文档提交不能外推为现场授权。
- 未来把生产域名或 Writer 切到 ECS 是新的生产项目，不属于当前完成条件。
