# 当前状态

**文档状态**：`CURRENT`

**治理边界更新日期**：2026-09-12；现场水位以每次只读核验为准。

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和调度状态必须从各自权威数据源
读取；待推进工作见[统一后续推进计划](TODO.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成迁移、逐次 Gate、运行 ID、
发布窗口和一次性验收证据不在工作树维护副本，通过 Git、Harness、数据库与目标机 journal 追溯。

## 双主机边界与当前进度

- Mac3 承载生产域名、前端、数据库和 Writer；launchd + installed plist 是其调度控制面。
  ECS 是独立灰度环境，使用本机 MySQL、DataBridge 和 systemd one-shot/timer。
- 用户最新授权：深度清理后发布 ECS/Mac3、同步 master 并推送两分支。
  此授权不包含历史预测重算/复制/覆盖/删除、confidence DDL 或域名/DNS/Nginx/SSH 隧道变更。
- ECS 已完成全部 17 个源码方案、21 个 target 的原 ID Blackbox 接管；此前 Native 附件清理及测试收尾 release 已发布验收。
- Mac3 本轮只读核查仍有 26 个 Native base /30 个 active target，另外 67 个 Blackbox active target。
  因此 Mac3 不只是上传 release，还需完成 17 个源码方案的原 ID 版本切换；九个 W4 加密方案保持原 Native 运行。
- 本轮进一步删除旧 Liwei publisher/cache policy、16 个停用临时 canonical、旧迁移 Harness/仓储入口及一次性测试，
  恢复严格无 Native 附件的 Blackbox 合同。W4、有效算法交付与 93 个 canonical exact 摘要均未变化。
  当前完整回归为 668 passed、6 skipped、219 subtests；真实隔离 MySQL 另验证 Mac3 整组事务。
- Mac3 的七份增量状态正在核验。五份已有本机 Blackbox 私有状态已完成零训练结构/身份准备；
  W3C 原 cutoff 输入摘要匹配，Full/K5 当前 daily prefix 存在变化；W3A Full/SAY 已零训练认证本机缓存 649 行安全前缀，待标准调用。
  未通过真实输入与标准调用前不标记生产就绪、不启动全历史重跑。
- 当前尚未完成双机最终清洁 release 和 master 同步。具体阶段出口、精确范围与恢复步骤见
  [当前迁移计划](architecture/NATIVE_V1_TO_BLACKBOX_V2_MIGRATION.md)；不能把本地通过等同于现场部署完成。
- 两端不建立复制、双写、共享数据库或共享 DataBridge；本轮不搬迁临时身份历史。
  已完成的旧历史物化保持原内容和来源，不回删。
- 单一 codex/develop 集成代码线、不可变 archive；Mac3 使用 ECS 已验证的同一 archive，不能自行构建环境分支版本。

## 现场状态读取

- 精确 current/previous、archive/source-tree 摘要以目标机链接、manifest、安装记录和实际进程 cwd 为准。
- Mac3 以 installed plist、launchctl、进程和本机数据库为准；ECS 以 installed unit、systemctl、journal 和本机数据库为准。
- 每个发布窗口重新冻结 Registry/version、历史事实、Actuals、DataBridge、私有状态、Dashboard 与在途任务。
  私有快照及验收原件外置保存，文档中的已知基线不替代实时核验。
- 本轮复用已完成算法等价证据，只补本机真正缺失的标准调用和状态验证；不等待多日自然触发。
  人工模拟不冒充 scheduled_live，不提前写尚未到期业务键。
- 旧自动迁移任务保持暂停，不能继续执行已撤销的 W4 改造、历史搬迁或 DDL 计划。

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
- 同算法运行时迁移走受控原 ID 版本升级：全部原事实保留，本轮不再搬迁或删除临时 `_bbv2` 历史结果，
  此前已完成的导入也不回删；只切未来执行版本，不建立历史别名。
  T1/T5 每 base 组合目标两文件、整体版本且原子提交；周/月 Metadata horizon=1，原事实 6/30 显式投影保留。
- W4 九个 Mac3 加密方案继续现有 Native 入口与调度，不实施此前 binary bundle 设计；
  不因 ECS 迁移而删除其依赖，也不把 W4 部署到 ECS。
- 普通 Blackbox 入库只保留“Intake → 一次完整持久化回测 → activate”；本次迁移的证据转换采用专用入口，
  不伪改旧 backtest 或全局放宽 activate。DataBridge producer 独立发布 generation，方案不构建、修复或重验 generation。
  Blackbox 不进入 Native `onboard`，不运行 StaticGate、CompareGate、额外 predict 冒烟或 `shadow-register`。
- Blackbox `weekly_point/h1` 与日频 `T+5/h5` 的 target 半开区间批量已经在 ECS 与 Mac3 验证；日频
  `T+1/h1` 的同类能力已完成本地候选实现与回归，尚未做现场验证。一个方案只启动
  一个 batch，全部业务键由现有 repository 原子提交，下一自然调度 target 必须保留不占用。
- 当前代码及 ECS release 已删除十七个已接管方案的 Native 附件和旧 Liwei Phase-A/cache projection/migration
  实现，不再从当前源码重建这些 Native 缓存。旧回滚 release 与外置原状态保留。
  W4 Native 的通用作业级临时输入、子进程隔离与 one-shot 控制仍保留；不把其在用能力一并删除。
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
- `api_wind_indicators_all.factor_version` 已在两端源表完成存量 `V1.0` 初始化。ECS 本次接管验证使用五文件
  generation `full-20260912-063338-cefd054bbccf`；精确文件摘要、catalog、ready
  receipt 与 current/previous 兼容性仍必须从 ECS 现场权威读回。Mac3 的 current generation 不从 ECS 状态
  推断，生产操作前独立核验。
- Mac3 production 与 ECS gray 的 installed plist/unit、服务状态、数据库写入、激活、补数和 DDL 都是
  独立操作，代码或文档提交不能外推为现场授权。
- 未来把生产域名或 Writer 切到 ECS 是新的生产项目，不属于当前完成条件。
