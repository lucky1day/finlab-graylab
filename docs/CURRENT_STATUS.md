# 当前状态

**文档状态**：`CURRENT`

**最近运行核验**：2026-09-13。以下是已核验基线，不替代实时现场检查。

固定主机、SSH/本地转发、生产路径和只读命令见[双机部署与访问入口](operations/DEPLOYMENT_ACCESS.md)。

## 运行与部署

源码方案的执行迁移及平台 confidence 退役已有完成证据；迁移展示保留规则与 Dashboard Gate 的衔接缺口见下方[生产标记发布](#dashboard-生产标记发布)。新算法仍按[标准入库流程](onboarding/README.md)处理。

| 项目 | ECS 独立灰度 | Mac3 生产 |
|---|---|---|
| 迁移结果 | 17 个原方案、21 个 target 使用 Blackbox | 同左 |
| 当前 active 执行身份 | 92 个 Blackbox base | 92 个 Blackbox base + 9 个 W4 Native base |
| Dashboard | 96 个 target | 105 个 target |
| 调度控制面 | systemd one-shot/timer | launchd + installed plist |
| current release | `892f2b3e1af5ff247eae1f0455f32a065772c8e6` | `6fff43e2b16855a7a97fb501c2b2425e8b9784f7` |
| previous release | `6fff43e2b16855a7a97fb501c2b2425e8b9784f7` | `b3479c62e06f2dcf25abcb8b1645bc3a31692ccd` |
| schema | 025 APPLIED | 025 APPLIED |

- ECS archive SHA-256：`3a1fb71a4d9849a48795fc6817cb0ed5dde8fe3fe7c2a934169f63f2b1654a7f`。
- Mac3 archive SHA-256：`3a102a16e0b756ab93a22ee2915e5e3a4f41da3b0cc71a11a5c205c7a12724cc`。
- 两机 Backend cwd、健康及 immutable 源码树已核验；本轮新功能已发布 ECS，Mac3 晋级暂停，原因见下方生产标记状态。
  域名仍由 Mac3 服务；DNS、Nginx、认证和既有 SSH 隧道未改变。
- 集成分支为 `codex/develop`。开发分支的代码或文档提交不代表新的生产 release。
  实时提交以 Git 引用为准，发布以目标机 manifest、current/previous 和进程 cwd 为准。
- 当前 release 包含下述平台清理与收盘候选修复；[发布验收](/Users/macstudio0/bond-factor-lab-runtime/releases/platform-cleanup-20260913/delivery-report.json)及[证据索引](/Users/macstudio0/bond-factor-lab-runtime/releases/platform-cleanup-20260913/README.md)保存双机安装、数据、输入、HTTP 和控制面读回。此前框架清理证据仍在[原核验记录](/Users/macstudio0/bond-factor-lab-runtime/releases/framework-cleanup-20260913/verification.json)。

## 八套新方案交付状态

两批均已完成双机交付与模拟验收，首次自然运行仍待[TODO 中的独立观察](TODO.md#八套新方案自然观察)。以下是交付时证据，不是当前生产输入水位。

| base ID | exact version | 任务 / 期限 | 批次 |
|---|---|---|---|
| `weekly_3y_full_action_rule0001_v2` | `ac06e7df4582` | weekly_point / 3Y | 五套新方案 |
| `weekly_10y_full_action_hgb0061_v2` | `e87140ab2ce2` | weekly_point / 10Y | 五套新方案 |
| `cgb_a4_fundseason_3y_hl18` | `79c08e273ecd` | monthly / 3Y | 五套新方案 |
| `cgb_a4_fundseason_5y_hl18` | `38c8fa7b0220` | monthly / 5Y | 五套新方案 |
| `cgb_a4_fundseason_10y_hl18` | `295c00e305e2` | monthly / 10Y | 五套新方案 |
| `weekly_5y_full_action_lowcorr01_v3` | `9ebe38835599` | weekly_point / 5Y | 三套 5Y 周频 |
| `weekly_5y_full_action_lowcorr02_v3` | `abd2dc14c172` | weekly_point / 5Y | 三套 5Y 周频 |
| `weekly_5y_full_action_lowcorr03_v3` | `2dccf75844e9` | weekly_point / 5Y | 三套 5Y 周频 |

任务均为 h1。八份算法脚本保持原字节；仅五份周频 Metadata 的 owner 修正为 liwei（三套 5Y 原值为 lw），月频原件未改。

| 每机覆盖 | 每套历史 target / 条数 | 每套灰度 target / 条数 | 该批每机合计 |
|---|---|---|---|
| 五套新方案中的两套周频 | 2025-01-10—2026-05-29 / 72 | 2026-06-05—2026-09-18 / 16 | 与三套月频共 192 历史、44 灰度 |
| 五套新方案中的三套月频 | 2025-02-14—2026-05-15 / 16 | 2026-06-15—2026-09-15 / 4 | 同上 |
| 三套 5Y 周频 | 2025-01-10—2026-05-29 / 72 | 2026-06-05—2026-09-18 / 16 | 216 历史、48 灰度 |

三套 5Y 的灰度 predict 覆盖 2026-05-30—2026-09-12，ECS 回测 ID 为 290—292、Mac3 为 260—262。
两批各机历史与灰度均绑定本机同一输入：ECS `full-20260912-063338-cefd054bbccf`，Mac3
`full-20260912-063159-944bc5a2c67a`；business digest 不同，独立计算，未复制业务记录。

验收已证明 exact、三日期、完整业务键和本机来源一致，历史/灰度零重叠、应有点零缺口。两批每机分别核对
236、264 条 HTTP 明细及 Actual join，并完成 DashboardGate 和浏览器期限、名称、owner、历史月份与待验证展示。
三套 5Y 每套的 2026-09-18 目标在交付时仍待验证；纯待验证月份可以打开明细，不进入已验证统计。

两批复用既有周频和 close-period 控制面，installed unit/plist 未变，ECS `Persistent=false`；到期/非到期、错误控制面和重复键隔离模拟通过，无生产模拟写入。原 Registry/预测与 W4 九套 Native exact、文件、依赖和展示保持完整。

### 交付证据索引

| 批次 | 本机可读索引 | 目标机原件位置 |
|---|---|---|
| 五套新方案 | [证据 README](/Users/macstudio0/bond-factor-lab-runtime/releases/new-five-20260912/README.md)、[最终验收](/Users/macstudio0/bond-factor-lab-runtime/releases/new-five-20260912/delivery-final.json) | ECS：`/opt/bond-factor-lab/incoming/new-five-20260912/` |
| 三套 5Y 周频 | [证据 README](/Users/macstudio0/bond-factor-lab-runtime/releases/weekly-5y-lowcorr-20260913/README.md)、[交付报告](/Users/macstudio0/bond-factor-lab-runtime/releases/weekly-5y-lowcorr-20260913/delivery-report.json) | ECS：`/opt/bond-factor-lab/incoming/weekly-5y-lowcorr-20260913/` |

索引区分最终验收、历史过程、原 archive 与晋级副本；CLI 回执、输入 lineage、数据/页面/控制面、公共测试及模拟证据按主机保存。一次性 operator 脚本已归档退役，仅供追溯，不作为新操作入口。

## 保留身份与恢复证据

W4 Native 与 Blackbox 的实际范围以[部署矩阵](../deploy/scheme_deployment_matrix_v1.json)及本机 Registry/version 为准；保护原则见[根规范](../AGENTS.md)，迁移边界见[算法保真](architecture/SOURCE_ALGORITHM_FIDELITY.md)。

- ECS 的 11 个独立临时身份及 Mac3 的两个旧验证库已清理。以下两个 ECS 身份获批永久保留为只读历史来源：
  - `liwei_0616_5y01_full_oos_k3_div_k10_bbv2`
  - `liwei_0616_cons_sda_k3_div_k10_bbv2`
- 两个来源的 Registry 为 archived、版本为 retired，无 canonical、Writer 或 Dashboard 展示。
  原 ID 四条预测仍引用其回测；不得删除来源、解除外键或改挂历史。它们不是待删项，也不构成新算法入库阻塞。

### Schema 025 与恢复材料

- `t_scheme_predictions.confidence` 与 `t_backtest_predictions.confidence` 已通过 migration 025 删除。
- current/previous 均兼容 schema 025。禁止回滚到仍读写 confidence、依赖已删临时身份或要求旧 Native 接管状态的 release。
- 恢复操作按[部署手册](../deploy/README.md)核验权限、输入/状态和 Writer；以下材料是恢复依据，不是回滚授权。
- 永久备份与恢复原件：
  - ECS confidence：`/opt/bond-factor-lab/backups/confidence-retirement-20260912/ecs-v1`
  - Mac3 confidence：[/Users/macstudio0/bond-factor-lab-production/backups/confidence-retirement-20260912/mac3-v1](/Users/macstudio0/bond-factor-lab-production/backups/confidence-retirement-20260912/mac3-v1)
  - ECS 临时身份清理：`/opt/bond-factor-lab/backups/database-cleanup-20260912/ecs-eleven`
  - Mac3 旧验证库清理：[/Users/macstudio0/bond-factor-lab-production/backups/database-cleanup-20260912](/Users/macstudio0/bond-factor-lab-production/backups/database-cleanup-20260912)
- confidence 完整恢复集必须同时包含 SQL gzip 与 `original-audit-json.json.gz`；不能只恢复 SQL 而丢失原始 JSON 精度。
  先在隔离库验证恢复，再按明确授权处理精确对象；备份、原始验收回执和 immutable archive 不随文档清理删除。

## 已发布的平台清理与收盘候选修复

2026-09-13，开发工作区移除 Native onboard、专属 Gate、maintenance、新版本激活及两表验证记录写入，并退役 Native 独立历史回测、旧分步写入和未接入补平实现。新回测仅保留月度与期限汇总及必要身份、输入信息，不再生成旧固定分期或空排除摘要。W4 按[固定版本运行边界](../AGENTS.md#算法与数据不变量)保留；Blackbox 入库与公共运行校验继续维护。历史 `t_harness_runs`、`t_harness_gate_results` 及迁移定义保留，当前运行不依赖它们。

收盘入口在预规划前解析本机生效生命周期，与 one-shot 使用同一规则，防止 canonical 初始状态掩盖已激活方案。清理后的全仓回归为 638 passed、4 skipped；跳过项为未配置隔离库的认证 MySQL 集成。方案与上游原件、迁移和部署配置字节未变。

双机已发布同包，仅刷新 Backend。发布前后本机 Registry、exact、历史与预测条数、输入身份及 Dashboard 内容一致；认证 HTTP Summary 全量对照、八套新方案 Gate 与九月 Detail 对照通过。ECS 的 5 个 timer 保持 enabled/active、Persistent=false；Mac3 的 7 个 launchd 任务配置检查通过，W4 九套 adapter、来源包、动态入口、解释器 ABI 与依赖可用。本次未运行算法或写业务事实，未跨越正式触发窗口；自然运行仍按 TODO 独立观察。

Mac3 公网首轮验收出现一次 HTTP 504，同期 Backend 有 5.37 秒慢请求；随后降频复核的 17 个请求与浏览器刷新、明细均通过。原始异常保留在上述证据目录，原因尚未定位，继续纳入[公网可靠性观察](TODO.md#其他未闭环队列)，不据此宣称长期性能问题已解决。

## Dashboard 生产标记发布

2026-09-13，功能与前序测试清理已提交并推送 `codex/develop`；生产标记规则和名单维护分别见
[Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#生产方案标记)与[部署手册](../deploy/README.md#生产方案名单维护)。
本地验证 578 passed、171 subtests passed；隔离浏览器覆盖非空名单、替换、清空、错误恢复和长名称，独立审查无阻塞缺陷。

ECS 已切换上表新包并刷新 Backend，本机初始名单 `{}`，96 target 全部 `is_production=false`。
真实 HTTP Summary 全量与 DB 一致，4 个历史/最新 Detail 对照通过，浏览器 fresh、候选排行及待验证明细正常；
发布前后 Registry/exact、业务条数、输入 ready 和调度配置保持一致。算法、W4 执行依赖与部署配置未改。

扩大 Gate 检查时，75 个 base 通过，17 个已迁移原方案（21 target）失败；八套新方案均通过。
两机 2026-09-13 13:25 的独立快照中差异相同：名称 15 处、描述 21 处、owner 12 处（2 个 rl、10 个 lw，对应 canonical liwei）。
ECS 本次发布前后上述字段完全不变；Mac3 的数量为保存快照的离线核对，未宣称已完成本轮新版 HTTP 验收。

追溯旧迁移方案和事务发现：当时明确保留原 Registry 展示信息，并以逐字段不变作为验收条件；
Gate 却把所有带 description 的 Blackbox 都按新入库展示信息检查，没有区分原 ID 迁移。
Gate 自迁移收尾提交 `87e9c4c7` 后未改动；运行接管已完成，但这项合同衔接遗漏，不能称整体完全闭环。
原实现、迁移验收范围及双机逐字段清单见外置 `migration-display-analysis.json` 和 `migration-display-findings.md`。

验收职责修复已获确认：首次登记在激活事务核对 Metadata → Registry；通用 Gate 对照 API → 本机 Registry。
保留既有业务展示，修复通过验证后重新构建 release，按 ECS → Mac3 顺序验收；Mac3 尚未创建初始名单或切换本轮 release。

证据：[本轮外置目录](/Users/macstudio0/bond-factor-lab-runtime/releases/dashboard-production-marker-20260913/)，
其中 `ecs-before/candidate/after.json`、`ecs-http.json`、`existing-gate-differences.json` 分别定位本机数据、HTTP 和存量差异；
安装回执、控制面、日志、原计划与隔离验证同目录保存。

## 当前输入与产品版本

双机使用五文件 DataBridge，存量 `factor_version` 已初始化为 `V1.0`；legacy 保护与新方案输入模式见[DataBridge](blackbox_v2/data_bridge_v1/README.md)。ECS 产品接口为 `factor-lab-dashboard-v6`，Mac3 暂为 V5；完整读模型与表示见[Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)。

## 现场核验入口

- 发布与恢复：[部署运行手册](../deploy/README.md)。
- 调度：installed plist/unit、loaded state、journal/日志、实际进程和本机 run/prediction 联合核验，见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
- 公网故障：[Dashboard 故障定位](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#故障定位与恢复)。
- 日历、DataBridge、Actuals、状态和在途任务在每次操作前重新核验；本文中的数量和摘要不能充当操作时的现场证据。
