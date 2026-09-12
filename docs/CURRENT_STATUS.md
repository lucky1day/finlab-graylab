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
- 用户已授权深度清理后发布 ECS/Mac3、同步 master 并推送两分支；随后独立授权备份、隔离恢复及引用检查后，
  清理 ECS 精确临时身份和 Mac3 两个旧验证库。原 ID 历史、W4、源数据和 confidence 继续保留，
  不包含历史重算/复制/覆盖、confidence DDL 或域名/DNS/Nginx/SSH 隧道变更。
- ECS 与 Mac3 均已完成全部 17 个源码方案、21 个 target 的原 ID Blackbox 接管。
  ECS 为 84 个 active Blackbox base、88 个 Dashboard target；Mac3 为 84 个 active Blackbox base，
  另保留九个 W4 Native base，共 97 个 Dashboard target。W4 不部署 ECS、不改造。
- 本轮进一步删除旧 Liwei publisher/cache policy、16 个停用临时 canonical、旧迁移 Harness/仓储入口及一次性测试，
  恢复严格无 Native 附件的 Blackbox 合同。W4、有效算法交付与 93 个 canonical exact 摘要均未变化。
  本次 Mac 整组事务已经真实隔离 MySQL 和现场受控切换验证；接管后移除临时事务入口及其一次性测试。
  清洁版完整回归为 646 passed、4 skipped、219 subtests passed，保留跨版本公共防线。
- Mac3 的 21 个目标已完成本机标准调用，七份增量状态通过当前输入验证并受控发布。
  92 条旧 Native active version 已退休，原 paused 版本和全部历史记录保持不变；17 个新 exact 各为唯一 Writer。
  一条旧 exact 的 Compare running 审计原样保留，不冒充当前运行进程，也没有为了切换改写审计。
- 双机接管 release 已按同一个不可变 archive 验收，Backend 与预测调度已恢复，installed plist/unit 未修改。
  最终清洁版本沿用同一 exact 与状态，不再次执行算法或版本迁移；精确 current/previous、archive 与远端提交
  以[迁移验收记录](architecture/NATIVE_V1_TO_BLACKBOX_V2_MIGRATION.md)所定义的发布读回为准。
- 两端不建立复制、双写、共享数据库或共享 DataBridge；本轮不搬迁临时身份历史。
  已完成的旧历史物化保持原内容和来源，不回删。
- 独立获批的数据库清理已完成安全子集：ECS 删除 11 个无共享外键依赖的临时身份，Mac3 删除两个旧验证库。
  ECS 两个 W3A 临时身份继续 archived，因为原 ID 的四条历史预测仍直接引用其回测；未解除外键、改挂或删除这些原记录。
  因而“13 个临时身份全部物理删除”尚未完成，不能把两个共享来源算作已清理。
  删除前均完成备份与独立 MySQL 恢复；删除后保留记录、Actuals、confidence 列、Dashboard、Backend 和调度核验通过。
  本次没有算法执行、生产 schema 迁移、release 切换或服务重启。
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
- 同算法运行时迁移走受控原 ID 版本升级：全部原事实保留，不搬迁临时 `_bbv2` 历史结果，
  此前已完成的原 ID 导入也不回删；只切未来执行版本，不建立历史别名。临时身份退役按独立精确授权执行，
  原 ID 仍引用的来源不是可删除冗余；不能解除外键或改挂历史来通过清理。
  T1/T5 每 base 组合目标两文件、整体版本且原子提交；周/月 Metadata horizon=1，原事实 6/30 显式投影保留。
- W4 九个 Mac3 加密方案继续现有 Native 入口与调度，不实施此前 binary bundle 设计；
  不因 ECS 迁移而删除其依赖，也不把 W4 部署到 ECS。
- 普通 Blackbox 入库只保留“Intake → 一次完整持久化回测 → activate”；已完成迁移的受控原件只读保留，
  临时迁移入口不作为长期能力，不伪改旧 backtest 或全局放宽 activate。DataBridge producer 独立发布 generation，方案不构建、修复或重验 generation。
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
