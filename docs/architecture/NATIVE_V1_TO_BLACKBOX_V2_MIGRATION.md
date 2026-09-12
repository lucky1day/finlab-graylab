# Native → Blackbox V2：迁移与双机发布验收

**状态：17 个源码方案双机接管已完成；W4 九方案按批准边界保留 Native。更新：2026-09-12。**

本文记录本轮有效范围、完成证据和恢复边界，不再提供一次性迁移 CLI。精确部署版本以 current/previous、安装 manifest、运行进程和远端 Git 引用为准；原始验收记录外置保存。

## 1. 最终目标与边界

同一个业务方案保留原 scheme_id、Registry ID 和历史事实，只替换未来执行实现。

- ECS 与 Mac3 均由 Blackbox 执行 17 个源码方案、21 个 target。
- W4 九个加密方案仅在 Mac3 继续原 Native 方式，不改造、不部署 ECS。
- 不创建第二业务身份、不建立历史别名或第二 Writer。
- 本轮不重跑历史区间，不生成、复制、搬迁、覆盖或删除原 ID 预测/run/backtest。
- 临时 _bbv2 历史默认只读保留；用户后续独立授权精确临时身份退役，须完成备份、隔离恢复与引用检查。
  原 ID 仍引用的来源不删除；此前已完成的原 ID 物化不回删，停用 canonical 不再具备执行资格。
- 用户已授权双机发布、同步 master 和推送两分支；不 force push，不覆盖并发工作。
- 未更改域名、DNS、Nginx、认证或 SSH 隧道。confidence DDL 未执行，仍需单独确认，不属于此次调整后的完成范围。

## 2. 精确执行范围

| 批次 | 原方案 ID | target 数 | ECS | Mac3 |
|---|---|---:|---|---|
| W1A | t1_daily、t5_daily | 6 | Blackbox | Blackbox |
| W1B | weekly_5y_direct_0529、weekly_7y_cross_d_overlay_0529、weekly_10y_d_overlay_0529 | 3 | Blackbox | Blackbox |
| W2 | daily_5y_2_v28、daily_7y_1_v28 | 2 | Blackbox | Blackbox |
| W3A | liwei_0616_cons_sda_k3_div_k10、liwei_0616_5y01_full_oos_k3_div_k10 | 2 | Blackbox | Blackbox |
| W3B | liwei_0616_10y01_cons_say_k3_div_k10、liwei_0616_10y01_full_oos_k3_div_k10、liwei_0616_10y02_cons_say_k3_div_k5 | 3 | Blackbox | Blackbox |
| W3C | liwei_0616_5y_auc_static_all_k3_div_k10、liwei_0616_5y_auc_yearly_all_k3_div_k10、liwei_0616_5y_ic_yearly_all_k3_div_k10 | 3 | Blackbox | Blackbox |
| W3D | liwei_0616_7y01_cons_say_k3_div_k10、liwei_0616_7y03_cons_all_k3_div_k8 | 2 | Blackbox | Blackbox |
| W4 日 | daily_10y_lgbm_10y04_0629、daily_1y_xgb_1y13_0629、daily_5y_lgbm_5y10_0629 | 3 | 排除 | 原 Native |
| W4 周均 | weekly_avg_10y_lgbm_0529、weekly_avg_1y_lgbm_0529、weekly_avg_5y_lgbm_0529 | 3 | 排除 | 原 Native |
| W4 月 | monthly_10y_rf_top5_0629、monthly_1y_rf_top30_0629、monthly_5y_knn_top20_0629 | 3 | 排除 | 原 Native |

T1/T5 各保留一个 base/version，目标包在同一次任务中全部验证、原子提交。有效文件名的 _bbv2 后缀不代表临时业务身份，不按后缀批量删除。周频原 h6 业务键保持，Metadata h1 决定执行语义。

## 3. 已完成的接管与验收

### 算法与状态

- 复用已接受的 ECS 算法等价证据；Mac 原 Native core 相对源基线字节一致，不重新证明全部历史日期。
- Mac3 使用本机五文件、Request、数值环境与私有状态完成 17 base /21 target 的标准调用。
- 10 个非增量 base /14 target 全部通过；七个增量 base 的调用耗时约 10–52 秒。
- 非增量最长成功回执约 171 秒（包含调用后只读核查），峰值 RSS 约 1.5 GiB；未放宽 300 秒、4 GiB 的本轮调用限制。
- 数值库线程上限为 8；总 OS 线程另作观测，不将空闲/控制线程总和冒充数值并行度。
- 两次辅助监测误判的失败原件保留；经明确修正后分别产生新的成功回执，没有覆盖失败记录。
- 七份状态经安全历史前缀与必要后缀计算验证后，由已有 StateSession 原子发布；没有修改算法数组来贴合 Native 结果。
- 历史 Native 与当前输入代次不同的比较只作诊断；算法等价、标准执行和状态输入分别保留真实身份，不伪称相同输入。
- 后续仅修改迁移 guard、文档和删除临时工具，算法/Metadata/config/Profile 字节未变；通过显式 release 重绑定复用真实产物，没有再训练。

### Mac3 原 ID 事务

- 已在维护围栏内一次取得 17 个方案锁，重新核验 source/candidate、数据库、Registry、状态与标准结果。
- 92 条旧 Native active version 退休；11 条原 paused version 及全部旧版本其他内容保留。
- 17 个新 Blackbox exact 激活；21 行 Registry 保留原 ID/业务键，只更新运行时与数据库自动维护时间。
- 每个迁移方案只有一个 active Blackbox exact。其余 67 个 Blackbox 与九个 W4 Native 不变。
- 已完成反向事务的只读 preflight，随后恢复原 installed launchd 配置；未触发额外自然预测。
- 一条 6 月旧 exact 的 Compare running 审计没有对应进程，完整保留；迁移检查仅将当前 source/candidate 的 Harness 运行视为阻断，预测/回测在途与真实进程围栏未放宽。

### 实际读回

- ECS：84 个 active Blackbox base，88 个 Dashboard target。
- Mac3：84 个 active Blackbox base、九个 W4 Native base，97 个 Dashboard target。
- 接管窗口 Mac3 22727 条预测、4747 条 run、93 条 backtest run、18483 条 backtest prediction 的数量与内容摘要完全不变。
- Actuals、无关 Registry/version、Dashboard 业务内容、installed plist/unit 均未发生非计划改变。
- Backend 健康与真实 cwd 对应当前 release；预测调度重新加载，DataBridge/Actuals 未被停止或重配。
- 人工标准调用只证明模拟验收，不冒充 scheduled_live；本轮不等待多天自然观察。

上述数量只是本轮冻结证据，不能替代后续自然运行后的权威现场。

## 4. 深度清理结果

此前已删除 17 个源码方案的 Native 附件、专属回测 runner、旧 Liwei cache/projection/migration 和无用 benchmark。本轮进一步删除：

1. 旧 Liwei cache policy/ABI、publisher-first wave 和专用调度预算。
2. 16 个停用临时 canonical、16 个旧迁移 Harness 模块、23 份一次性测试和五份临时 mapping/equivalence 文件。
3. 跨 ID、same-ID reclaim、附件迁移仓储的无调用闭包。
4. native_attachments 合同例外；Blackbox 不再允许 Native 附件。
5. Mac 接管完成后，最后十个临时晋级函数、固定迁移清单、失效 import 与 Mac 专项测试。

当前 Native canonical、predict/inference 与 source package 仅属于 W4。保留九个 W4 方案的 source package、必要 adapter/runner、只读源数据上下文、ABI/输入隔离、Native 维护 Gate 与公共测试。这些是有效功能依赖，不作为冗余删除。

有效算法包和 W4 闭包字节不变；所有 93 个 canonical 的 exact/code/config/manifest 摘要保持不变。旧二进制/代码、已执行 migrations、数据库历史与不可变 release 仍可追溯。

长期测试保留公共合同、输入/日期不变量、安全边界、事务原子性、迁移恢复和调度控制面；不以减少到某个数字为目标，也不保留已经完成任务的专项迁移测试。

## 5. 清洁 release 与 Git 交付

清洁 release 只删除已经无消费者的临时实现、同步文档，不修改算法 exact、业务数据或生产状态：

1. 完整回归、W4/有效交付摘要比较、独立审查。
2. clean commit 构建两次相同 archive，推送 codex/develop。
3. ECS 先安装/切换/读回，Mac3 使用同一 archive 晋级；不是重新构建一个 Mac 分支版本。
4. 每机 release 切换前后比较事实、Registry/version、Actuals、状态、Dashboard 和调度；不再执行版本迁移或算法。
5. current 与 previous 均进入“原 ID Blackbox + W4 Native”兼容边界；前一份已验证接管 release 保留。
6. 已授权的 master 同步使用正常快进/合并并核对远端，不 force push；PR 同步真实状态。

精确 commit/archive SHA 与最后发布读回由目标机 manifest、current/previous、进程 cwd、Git 远端引用和外置回执共同证明。不在文档伪造未来 commit，也不把文件上传当作发布完成。

## 6. 恢复与原件

- 原 ID 接管前的 Native release 不能仅靠 current 链接回滚：必须先关闭对应 Writer，按已验证版本事务恢复身份与状态，再恢复调度。
- 清洁版回滚优先使用已接管的前一份 Blackbox release，不恢复已删临时 CLI或创建第二控制面。
- 新写入事实不删除；相同业务键仍严格跳过或拒绝，不能覆盖。
- confidence DDL 未执行，现有 schema 024 与 W4 使用字段继续保留。
- 完整私有回执目录：开发机 outputs/dual-host-finalization-20260912；ECS 对应 incoming 发布证据目录。
- 关键原件包括算法标准结果、状态来源/封装、切换与 rollback-preflight、双机前后快照、发布验收、文件清单和 archive SHA。
- 历史阶段方案保留在 Git，不再作为执行指令；不删除回滚 archive 或外置状态来追求表面“无 Native 字符串”。

**完成定义按最新范围收口：17 个源码方案在双机 Blackbox 接管，九个 W4 继续正常 Native；清洁 release、远端两分支和上述现场验收一致。不包含已撤销的 W4 改造、历史搬迁或未经单独授权的 confidence DDL。**

## 7. 后续独立授权的数据库清理

用户随后授权在备份、隔离恢复和引用检查通过后删除 ECS 的 13 个迁移临时身份，以及 Mac3 两个旧验证库；原 ID 历史、W4、源数据和 confidence 继续保留。此授权不允许为完成删除而解除外键、改挂来源或覆盖历史。

实际完成：

- ECS 的 11 个独立临时身份已在同一 repository 事务删除：3,549 条预测、679 个 run、22 个 backtest run、5,922 条回测明细、382 条月度指标、679 条日志和各 11 条 Registry/version。
- 另外两个 W3A 临时身份整组保留 archived：`liwei_0616_5y01_full_oos_k3_div_k10_bbv2`、`liwei_0616_cons_sda_k3_div_k10_bbv2`。原 ID 下四条历史预测直接通过外键引用其两个 backtest；它们仍是历史来源依赖，不是空壳。全部 13 个身份物理删除的目标尚未完成。
- Mac3 的 `bond_factor_lab_bbv2_cert_20260720` 与 `bond_factor_lab_v2_e2e_20260719_1730` 已删除；生产库未改写。
- ECS 完成 24 表、87,382 行的真实独立 MySQL 恢复、失败回滚和成功删除演练；Mac3 完成 34 个基础表与 23 个视图恢复验证。独立审查通过后执行，备份永久保留。
- 提交后全部保留表行摘要与 schema 一致，原 ID 来源外键无孤儿；ECS 84 Blackbox/88 Dashboard、Mac3 84 Blackbox + 九 W4/97 Dashboard、Backend 和 installed 调度均核验通过。
- 四条 W2 已物化原 ID 记录的来源 extra 保持原样，旧备份与新的永久备份均绑定实际源行摘要；没有复制或重写业务历史。

外置证据入口：开发机 `outputs/database-cleanup-20260912/COMPLETION.md`；ECS 永久备份位于 `/opt/bond-factor-lab/backups/database-cleanup-20260912/ecs-eleven`，Mac3 永久备份位于 `/Users/macstudio0/bond-factor-lab-production/backups/database-cleanup-20260912`。恢复时先在隔离库还原验证，再按缺失精确主键受控恢复，不把旧整库快照覆盖生产。

该数据库清理窗口仅为数据退役与文档同步，当时生产 schema 为 024、confidence 两列保留，运行代码及 release 未改变；清理临时入口不作为长期平台功能保留。

后续平台 confidence 退役已经作为独立任务获批，不改变上述清理历史事实；其当前部署/schema 状态以
[当前状态](../CURRENT_STATUS.md)为准。算法内部同名逻辑、W4 二进制、原始 benchmark 与审计仍受保护。
