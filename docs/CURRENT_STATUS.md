# 当前状态

**文档状态**：`CURRENT`

**最近运行核验**：2026-09-13。以下是已核验基线，不替代实时现场检查。

固定主机、SSH/本地转发、生产路径和只读命令见[双机部署与访问入口](operations/DEPLOYMENT_ACCESS.md)。

## 运行与部署

源码方案执行迁移、平台 confidence 退役及展示验收衔接修复均已完成。最近同步为下方文档 release；功能验收见[生产标记发布](#dashboard-生产标记发布)。新算法仍按[标准入库流程](onboarding/README.md)处理。

| 项目 | ECS 独立灰度 | Mac3 生产 |
|---|---|---|
| 迁移结果 | 17 个原方案、21 个 target 使用 Blackbox | 同左 |
| 当前 active 执行身份 | 92 个 Blackbox base | 92 个 Blackbox base + 9 个 W4 Native base |
| Dashboard | 96 个 target | 105 个 target |
| 调度控制面 | systemd one-shot/timer | launchd + installed plist |
| current release | `b43fc1911b0f7bf3ada05edca7ad8b483ac38e02` | 同左 |
| previous release | `accde117545ceca7f579692d4123df421f8b9d8c` | 同左 |
| schema | 025 APPLIED | 025 APPLIED |

- 双机同一 archive SHA-256：`f0c77cfeb296b8f930b98b528758c574c038eb2c968ee76374e31f3c3b97df91`。
- 两机 Backend cwd、健康及 immutable 源码树已核验；ECS 验收后晋级同一 archive 至 Mac3，本轮发布已闭环。
  域名仍由 Mac3 服务；DNS、Nginx、认证和既有 SSH 隧道未改变。
- 集成分支为 `codex/develop`。开发分支的代码或文档提交不代表新的生产 release。
  实时提交以 Git 引用为准，发布以目标机 manifest、current/previous 和进程 cwd 为准。
- 2026-09-13 文档规整版本已按 ECS → Mac3 同包晋级，仅刷新两机 Backend。Registry、exact、业务条数、ready 输入、Dashboard 读模型与生产名单均未改变；两机健康与首页正常，Mac3 公网首页与 release 字节一致，控制面保持原状。未运行算法、写业务事实或跨越正式触发窗口；自然观察仍待 TODO。证据见[同步回执](/Users/macstudio0/bond-factor-lab-runtime/releases/docs-guidance-20260913/delivery-report.json)与[索引](/Users/macstudio0/bond-factor-lab-runtime/releases/docs-guidance-20260913/README.md)。
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

两批分别使用本机输入独立计算；exact、三日期、完整业务键和来源核验通过，历史/灰度零重叠、应有点零缺口。Dashboard、Actual join 与纯待验证月份明细通过；交付时的输入 generation、回测 ID 和逐点结果见下方证据。

两批复用既有周频和 close-period 控制面；到期/非到期、错误控制面和重复键隔离模拟通过，无生产模拟写入。原有事实与 W4 九套的 exact、文件、依赖和展示保持完整。

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

收盘入口已统一使用本机生效生命周期，解决 canonical 初始状态掩盖已激活方案的问题；现行选择规则见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。发布验收证明原有身份、事实、输入、HTTP 和控制面保留完整；详细结果由上方平台清理证据保存。

Mac3 公网首轮验收出现一次 HTTP 504，同期 Backend 有 5.37 秒慢请求；随后降频复核的 17 个请求与浏览器刷新、明细均通过。原始异常保留在上述证据目录，原因尚未定位，继续纳入[公网可靠性观察](TODO.md#其他未闭环队列)，不据此宣称长期性能问题已解决。

## Dashboard 生产标记发布

2026-09-13 双机已发布生产标记与 Registry 展示验收修复。名单规则和维护分别见
[Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#生产方案标记)与[部署手册](../deploy/README.md#生产方案名单维护)。
2026-09-13 按用户确认，将“五套新方案”批次的完整 Registry ID 加入两机外置 `production_schemes.json`。当前 ECS / Mac3 各标记 5 个 target；文件摘要一致，真实 API 与五个任务格子的生产菱形逐项核验通过，其他展示和统计未变。本次只更新名单，无需发布或重启；[结果与原名单备份](/Users/macstudio0/bond-factor-lab-runtime/releases/production-five-marker-20260913/result.json)记录此次范围。名单标记不替代 TODO 的自然运行观察。

Registry 展示验收的 17 个迁移方案误报已解决；首次登记与后续展示的权威边界见[共享契约](architecture/SCHEME_CONTRACT.md#3-方案身份)。本次未更改 Registry 名称、描述、owner、算法或历史。

本地公共回归与独立复审通过，生产标记的非空、替换、清空、错误恢复和长名称视觉已在隔离环境验证。双机真实静态资源与验收版本一致；ECS 92 / Mac3 101 个 base 的 Gate 全部通过，认证 Summary、历史/最新 Detail 和待验证页面与本机事实一致。

发布前后 Registry、exact、历史、预测、Actual 条数及 ready 输入保持一致，两机独立核验。ECS 5 个 timer 保持 enabled/active、Persistent=false；Mac3 7 个 installed/loaded 任务配置不变，W4 九套入口与依赖可用。仅切换 current 并刷新 Backend，未运行算法、写业务事实或跨越正式触发窗口。自然观察另见 TODO。

证据：[最终报告](/Users/macstudio0/bond-factor-lab-runtime/releases/dashboard-registry-contract-20260913/delivery-report.json)、
[索引](/Users/macstudio0/bond-factor-lab-runtime/releases/dashboard-registry-contract-20260913/README.md)；
[原功能与隔离验收](/Users/macstudio0/bond-factor-lab-runtime/releases/dashboard-production-marker-20260913/)保留原计划、
视觉验证及 `migration-display-analysis.json` / `migration-display-findings.md` 的问题来源，不作为未完成发布状态。

## 当前输入与产品版本

双机使用五文件 DataBridge，存量 `factor_version` 已初始化为 `V1.0`；legacy 保护与新方案输入模式见[DataBridge](blackbox_v2/data_bridge_v1/README.md)。双机产品接口均为 `factor-lab-dashboard-v6`；完整读模型与表示见[Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)。

## 现场核验入口

- 发布与恢复：[部署运行手册](../deploy/README.md)。
- 调度：installed plist/unit、loaded state、journal/日志、实际进程和本机 run/prediction 联合核验，见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
- 公网故障：[Dashboard 故障定位](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#故障定位与恢复)。
- 日历、DataBridge、Actuals、状态和在途任务在每次操作前重新核验；本文中的数量和摘要不能充当操作时的现场证据。
