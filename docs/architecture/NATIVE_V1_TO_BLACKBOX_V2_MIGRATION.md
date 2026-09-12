# Native → Blackbox V2：当前迁移与双机发布计划

**状态：执行中；本文件只保留当前有效决策。更新：2026-09-12。**

此前的新 successor 身份、历史补入/删除、W4 binary bundle、多遍历史回测及多日自然观察方案均已撤销，不再作为执行指令。历史过程从 Git 和外置验收原件追溯。

## 目标与授权

同一个业务方案保留原 scheme_id、Registry ID 和历史事实，仅替换未来执行实现。

- ECS：17 个有源码方案、21 个 target 使用 Blackbox。
- Mac3：上述 17 个方案晋级 Blackbox；九个 W4 加密方案继续原 Native 运行方式。
- 只保留一个有效 Writer；不新建业务别名，不拼接历史，不迁移临时身份历史。
- 不重跑历史回测，不重新生成、复制、覆盖或删除已有预测、run、backtest。
- 用户已授权本轮清理后发布 ECS/Mac3，并同步 master、推送两分支。不得 force push 或改写远端历史。
- 不变更域名、DNS、Nginx、认证或 SSH 隧道。confidence DDL 仍须单独确认，本轮不执行。
- 算法未改时复用已有 Native 对照与 Blackbox 等价结果；本机准备仅补真实缺失的环境、输入、状态和标准调用证据。

## 精确范围

| 批次 | 原方案 ID | target 数 | ECS | Mac3 |
|---|---|---:|---|---|
| W1A | t1_daily、t5_daily | 6 | Blackbox 已接管 | 待晋级 |
| W1B | weekly_5y_direct_0529、weekly_7y_cross_d_overlay_0529、weekly_10y_d_overlay_0529 | 3 | Blackbox 已接管 | 待晋级 |
| W2 | daily_5y_2_v28、daily_7y_1_v28 | 2 | Blackbox 已接管 | 待晋级 |
| W3A | liwei_0616_cons_sda_k3_div_k10、liwei_0616_5y01_full_oos_k3_div_k10 | 2 | Blackbox 已接管 | 待晋级 |
| W3B | liwei_0616_10y01_cons_say_k3_div_k10、liwei_0616_10y01_full_oos_k3_div_k10、liwei_0616_10y02_cons_say_k3_div_k5 | 3 | Blackbox 已接管 | 待晋级 |
| W3C | liwei_0616_5y_auc_static_all_k3_div_k10、liwei_0616_5y_auc_yearly_all_k3_div_k10、liwei_0616_5y_ic_yearly_all_k3_div_k10 | 3 | Blackbox 已接管 | 待晋级 |
| W3D | liwei_0616_7y01_cons_say_k3_div_k10、liwei_0616_7y03_cons_all_k3_div_k8 | 2 | Blackbox 已接管 | 待晋级 |
| W4 日 | daily_10y_lgbm_10y04_0629、daily_1y_xgb_1y13_0629、daily_5y_lgbm_5y10_0629 | 3 | 排除 | 保持 Native |
| W4 周均 | weekly_avg_10y_lgbm_0529、weekly_avg_1y_lgbm_0529、weekly_avg_5y_lgbm_0529 | 3 | 排除 | 保持 Native |
| W4 月 | monthly_10y_rf_top5_0629、monthly_1y_rf_top30_0629、monthly_5y_knn_top20_0629 | 3 | 排除 | 保持 Native |

T1/T5 各为一个 base/version，目标包在同一次任务内全部验证、原子提交。有效交付文件名中的 _bbv2 不代表临时业务身份，不按后缀批量删除。周频原 h6 业务键保持，Metadata h1 只决定执行语义。

## 本轮现场基线

本次只读核验发现两端并未完成同包晋级：

- ECS current：1a09ec89b40154359d6225e0d2c061b603c45169；previous：fb804394b1f3daa268a56f324746fb9147ee249f。
- Mac3 current：b736d3b21b1c57455cf36d1cdcaa22b00fdda455，仍为 26 个 Native base /30 个 active target，另有 67 个 active Blackbox target。
- Mac3 待迁移范围内有 103 条 Native version：92 active、11 paused。不能只退休各方案最新一行，必须在受控事务内撤销所有旧 active Writer。
- Mac3 17 个方案的 Native core 相对已验证 ECS 源基线字节未变，已有算法等价证据可复用，不重新证明所有历史日期。
- 双端输入 generation 各自独立；不得把 ECS 状态或输入身份直接标记成 Mac 产物。
- 精确数据库身份、全部事实/版本/Registry、installed 调度、状态和 Dashboard 快照保存在私有 dual-host-finalization-20260912 目录。执行前重新核验，旧快照不能替代现场。

## 已完成的代码清理

此前已删除 17 个源码方案的 204 个 Native 附件、专属回测 runner、旧 Liwei cache/projection/migration 和无用 benchmark；ECS 已发布验收。

本轮进一步：

1. 删除旧 Liwei cache policy/ABI 合同、publisher-first wave 和专属调度预算，只保留 W4 的通用 Native 执行路径。
2. 删除 16 个停用临时 canonical 目录、16 个旧迁移 Harness 模块、23 份一次性测试、五份临时 mapping/equivalence 文件。
3. 删除旧跨 ID、same-ID reclaim、附件迁移仓储无调用闭包。
4. 删除 native_attachments 临时合同例外；现有 Blackbox 不再允许附带 Native 文件。
5. 保留 W4 的九个 canonical、完整 source package、ABI/只读数据适配、通用子进程、输入临时目录、Native 维护 Gate 与其公共安全测试。

删除前的 92 个精确文件 SHA、455 个保留文件 SHA 和 93 个 canonical exact 摘要均已外置。清理后保留文件与有效方案 exact 不变。旧代码可从清理前 Git 和不可变 archive 恢复，不删除旧 release 或外置证据。

当前清理候选完整回归：668 passed、6 skipped、219 subtests passed；Mac3 临时晋级事务另有真实隔离 MySQL 验证。测试数量不是验收目标，长期保留公共合同、安全与事务边界。

## 当前待完成动作

### A. Mac3 本机准备

- 从 ECS 验证过的同一 archive 预安装候选，核实 manifest、依赖、环境矩阵和 W4 字节。
- 七个增量方案已逐一绑定本机状态来源、数值环境、数组完整性和候选身份，21 个目标标准调用全部通过。
- 五份既有 Mac Blackbox 私有状态已完成零训练的结构/身份准备；仅 Full/K5 重封装已审查的周频保护变化，其余算法字节不变。
- 当前输入的只读检查显示：W3C 三份既有状态的原 cutoff 输入摘要匹配；Full/K5 的 daily prefix 已有差异，尚不能直接标记可用于当前生产。
- W3A Full、W3B SAY 已从本机缓存分别认证并生成截止 9/4 的 649 行私有安全前缀，均零训练，待标准调用补必要后缀；Full/K5 既有 Blackbox 状态的安全前缀仍在检查。不得仅修改头部把未认证缓存伪装为已通过状态，也不自动启动全历史初始化。
- 只补本机真实缺失的一次标准调用；结果保持精确五字段，日期正确。人工调用不写历史、不冒充 scheduled_live。

七份状态已在维护围栏内发布，但版本事务尚未执行；原 release 和调度已恢复。
一次 preflight 识别出 2026-06-14 旧 exact 的遗留 Compare running 审计，不能把它改写为 failed。
迁移检查仅对当前 source/candidate exact 的 Harness 运行态阻断，预测/回测在途检查与进程围栏保持严格。
重新发布修正后的同包候选后复用现有标准调用和状态，不重新训练。

### B. ECS 清理 release 验证

1. clean commit 构建两次 archive，验证字节一致并记录 SHA。
2. 核实服务/触发点/零在途 Writer，预安装候选。
3. 围栏受影响预测 timer；等待 one-shot 自然退出。不影响 DataBridge、Actuals。
4. 切换 release、按需重启 Backend，验证全部 active exact 准入、事实/状态/Registry/Dashboard 不变。
5. 恢复 timer，读回 installed/loaded/next trigger 与实际进程 cwd。

仅平台清理且 exact 不变时，不运行版本迁移或算法。

### C. Mac3 原 ID 接管

使用一次性的固定 17 方案事务，不建立第二套长期迁移控制面：

- preflight 只读输出规范计划及 SHA，绑定旧 current、新 archive、数据库身份、原版本/Registry preimage、状态和标准调用回执。
- apply 取得 17 把既有生命周期锁后，重读全部权威状态；漂移即拒绝。
- 原子退休范围内所有旧 active Native version，保留 paused/历史证据；激活 17 个真实 Blackbox exact；21 行 Registry 保留原 ID 与业务键，仅更新运行时。
- 全库历史事实、Actual、无关版本和 Registry 不变；全部目标只能有唯一 Writer。
- rollback 恢复冻结的原 Native 生命周期，候选只 retired、不删除，任何已发布事实保持不变。

只在本机状态和标准调用均就绪后暂停日/周/月预测 launchd，确认无进程/在途 run，再切换 release 与版本。中间失败保持围栏，恢复旧版本及旧 release 后验证，再恢复调度。Backend、Dashboard 和 installed plist 必须实际读回；不改 tunnel、域名或源数据库配置（W4 仍需要）。

### D. 最终清洁版本与远端交付

双端接管验证后删除本次 Mac 临时迁移入口及专项测试，保留可恢复的外置证据；发布最终同一个 archive。回滚前一份 release 也必须能运行原 ID Blackbox，不能仅靠 symlink 恢复数据库版本。

更新受影响 Markdown，完整回归、独立审查后推送 codex/develop，按用户授权将已验证提交同步 master 并推送。不 force push；出现远端并发提交先核验差异，不覆盖他人工作。当前 PR 同步真实发布状态，不再沿用“Mac3 不操作”的旧描述。

## 最终验收

- ECS 17 个 base /21 target、Mac3 同样 17 个源码 base 均为原 ID Blackbox。
- Mac3 仅九个 W4 方案保留 Native 实现；W4 功能、原 source package、installed 调度与数据库历史无非计划变化。
- 无停用 alias canonical、旧 Liwei 调度、Native 附件、旧迁移工具的可执行残留。
- 旧预测/run/backtest/Actual 不重算、不搬迁、不覆盖、不删除；临时身份历史仍只读保留。
- 双机 current 对应同一已验证 archive，previous 具备清楚且经过验证的恢复边界。
- Backend/健康检查、Dashboard、唯一 Writer、调度加载与 next trigger 正常。
- 文档、两分支远端 commit 和 PR 状态与现场一致。

**当前尚未达到完整闭环：清理候选已验证，Mac3 状态准备、双机最终发布及 master 同步仍在推进。**
