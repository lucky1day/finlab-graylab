# 当前状态

**文档状态**：`CURRENT`

**治理边界更新日期**：2026-09-12；现场水位以每次只读核验为准。

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和调度状态必须从各自权威数据源
读取；待推进工作见[统一后续推进计划](TODO.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成迁移、逐次 Gate、运行 ID、
发布窗口和一次性验收证据不在工作树维护副本，通过 Git、Harness、数据库与目标机 journal 追溯。

## 双主机边界与当前进度

- 平台 confidence 退役现已独立获批：仅删除统一平台字段及两张预测表的对应列；算法内部同名计算、
  原历史其余属性及 W4 运行方式保持不变。代码、完整回归（674 passed、4 skipped、221 subtests）、
  独立审查、九个 W4 适配对照、双机备份与 26 表隔离恢复已通过；schema 024/025 下的实际仓储读写、
  激活及多目标原子提交均已验证。双机兼容 release 发布、migration 025 和运行读回已完成，详见下节。
  下述保留 confidence 的记录是前一清理窗口事实，不替代后续退役结果。confidence 独立实施窗口未操作 master；
  用户现已重新授权最终收尾同步 master，与 codex/develop 的精确交付以远端引用核验为准。
- Mac3 承载生产域名、前端、数据库和 Writer；launchd + installed plist 是其调度控制面。
  ECS 是独立灰度环境，使用本机 MySQL、DataBridge 和 systemd one-shot/timer。
- 用户此前独立授权备份、隔离恢复及引用检查后清理 ECS 精确临时身份和 Mac3 两个旧验证库。
  该清理窗口保留原 ID 历史、W4、源数据和 confidence；随后另行授权并完成了 confidence DDL。
  最终收尾确认两个共享历史来源只读保留并授权同步两分支，不重算、复制或覆盖原 ID 历史，不改公网入口。
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
- 数据库清理按最终获批范围完成：ECS 删除 11 个无共享外键依赖的临时身份，Mac3 删除两个旧验证库。
  ECS 两个 W3A 临时身份继续 archived，因为原 ID 的四条历史预测仍直接引用其回测；未解除外键、改挂或删除这些原记录。
  用户已确认这两个来源作为只读历史保留例外：Registry archived、版本 retired、无 canonical/Writer，
  不再属于待删队列，也不将其描述为已物理删除。原来源外键保持不变，不建立历史别名。
  删除前均完成备份与独立 MySQL 恢复；该窗口未改 schema 或 release，保留记录与运行验收通过；
  confidence 两列随后在独立的 025 窗口删除。
- 单一 codex/develop 集成代码线、不可变 archive；Mac3 使用 ECS 已验证的同一 archive，不能自行构建环境分支版本。

## 平台 confidence 退役验收

2026-09-12 已按 ECS → Mac3 顺序完成独立授权任务：

| 项目 | ECS | Mac3 |
|---|---|---|
| current release | `5f6c60cfd78457c2b1a6d52338e3a44f72c578ec` | 同一提交、同一 archive |
| previous release | `478850a23a4c9690f81aab41de90eccf21e61abb` | 同一提交、同一 archive |
| schema | 025 APPLIED | 025 APPLIED |
| 两张预测表 confidence | 均已删除 | 均已删除 |
| Dashboard / 可执行身份 | 88 target / 84 Blackbox | 97 target / 84 Blackbox + 9 W4 Native |

- 当前 archive SHA-256 为 `c449e0bf515ccd7024cf36ea498d450b468f1fdf3117bf368236e04c70acc3e8`。
  current/previous 都在 DDL 前发布为 confidence-agnostic，删列后不得回滚到更早依赖该列的版本。
- 统一平台模型、Native 反序列化与三类 W4 适配、共享回测结果、预测/回测/激活仓储、CompareGate 必需列及
  容差报告、前端 null 占位均已移除。Blackbox Result 仍严格为既有五字段，不新增兼容字段或转存到 extra。
- 542 个受保护算法、二进制、source package 和原始 benchmark 文件摘要、93 个 canonical exact 均不变。
  九个 W4 使用真实历史源输出、冻结 Request 与 artifact/calendar 边界重放完整新旧适配入口，除顶层
  confidence 外所有记录属性及输入参数一致；这是平台适配验证，不声称重跑二进制或完整历史回测。
- 算法内部概率、阈值、置信度、排序、投票与方向计算、仍供其他数值使用的 helper、原始 benchmark、
  001–024 SQL、旧 release 和已有 source_row/extra 审计均保留。
- MySQL 隔离验证覆盖 14 个迁移/恢复/漂移场景，以及 schema 024/025 下真实仓储写入、激活发布、多目标原子提交
  与第二个 target 写入前失败的整组回滚。独立审查发现的 unsigned 类型指纹遗漏已修复并复核。
- 双机逐表核验 26 表：除两列及新增的 025 migration 记录外，原内容、数量、身份、版本和来源不变；
  旧 001–024 migration history 逐行不变。没有历史重算、复制、预测补写、Registry 切换或源数据修改。
- 双机 Backend 健康，实际进程对应 current；Dashboard 摘要、Registry/version、派生状态与 installed 控制面
  均与发布前一致。预测任务已恢复且零在途 run；Actuals/DataBridge 的 23:45/次日 06:30 任务未暂停或遗漏。
  W4 仍仅 Mac3 Native，不修改 DNS、Nginx、认证或 SSH 隧道。
- 永久备份：ECS `/opt/bond-factor-lab/backups/confidence-retirement-20260912/ecs-v1`；Mac3
  `/Users/macstudio0/bond-factor-lab-production/backups/confidence-retirement-20260912/mac3-v1`。
  完整恢复集为 SQL gzip **与** `original-audit-json.json.gz`，已在独立 MySQL 恢复并通过全值摘要比较；
  sidecar 只修复隔离恢复时旧 JSON 科学计数浮点值的解析舍入，不修改生产审计。
  发布、DDL、行摘要和恢复回执外置保留，工作树不纳入数据库备份或大型结果。

本独立任务已完成；两个 archived W3A 身份现已按用户确认作为只读历史来源保留，不阻塞迁移完成。
迁移收尾仅同步 Markdown 与 Git，不改变已验证的双机运行代码、archive 或数据库。新的算法方案走
[统一入库入口](onboarding/README.md)，无需等待迁移历史重算、额外自然观察或进一步删除这些来源。

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
