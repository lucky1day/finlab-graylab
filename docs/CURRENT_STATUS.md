# 当前状态

**文档状态**：`CURRENT`

**最近运行核验**：2026-09-17。以下是已核验基线，不替代实时现场检查。

固定主机、SSH/本地转发、生产路径和只读命令见[双机部署与访问入口](operations/DEPLOYMENT_ACCESS.md)。

## 运行与部署

源码方案执行迁移、平台 confidence 退役及展示验收衔接修复均已完成。最新特征基准日展示、区间应用修复与日历界面已发布；功能验收见[需求与验收记录](product/FEATURE_DATE_DISPLAY_ACCEPTANCE.md)。新算法仍按[标准入库流程](onboarding/README.md)处理。

| 项目 | ECS 独立灰度 | Mac3 生产 |
|---|---|---|
| 迁移结果 | 17 个原方案、21 个 target 使用 Blackbox | 同左 |
| 当前 active 执行身份 | 93 个 Blackbox base | 93 个 Blackbox base + 9 个 W4 Native base |
| Dashboard | 97 个 target | 106 个 target |
| 调度控制面 | systemd one-shot/timer | launchd + installed plist |
| current release | `759048c4eaf90cfac04067d613b4e219013472a7` | `759048c4eaf90cfac04067d613b4e219013472a7` |
| previous release | `056534b2d846d0091e74495f0408cc108bc3f60c` | `056534b2d846d0091e74495f0408cc108bc3f60c` |
| schema | 025 APPLIED | 025 APPLIED |

- 2026-09-17 区间应用修复与日历界面 release `759048c4` 已经用户本地验收，通过独立复审及四项 CI，
  按 ECS → Mac3 晋级同一 archive，SHA-256 为
  `d4abf074696613ffb92062bb94b9562d1546c7359ae8178f95c5846cd368663e`。应用时保留用户输入，
  同区间重复点击合并；手动及自动重试保留最后提交区间，默认全部历史不被固定日期替代。
  两端实际 Backend cwd、health、九方案候选 Dashboard/DataConsistency、正式区间与整月明细检查通过；
  Mac3 公网资源摘要、GET/HEAD、MIME、缓存、认证及拒绝路径检查通过。正式 Edge 普通刷新加载新日历，
  选取 2025-07-01 后连续点击应用，输入、统计范围及整月详情保持一致。
  发布前后十张业务表逐行摘要及 schema 完全一致，ECS 97 / Mac3 106 个 target、九个生产标记保持。
  未修改调度或运行算法；临时会话已撤销，候选 Backend 已关闭。首轮 CI 的代理启动测试因模拟 DOM
  缺少新控件方法失败，测试夹具补齐后最终精确提交四项 CI 全部成功。
  证据见[发布回执](/Users/macstudio0/bond-factor-lab-runtime/releases/range-calendar-ui-20260917/delivery-report.json)
  与[证据索引](/Users/macstudio0/bond-factor-lab-runtime/releases/range-calendar-ui-20260917/README.md)。
- 2026-09-17 特征基准日统一展示 release `056534b2` 已按用户授权合并到 `codex/develop`，
  通过四项 CI 后按 ECS → Mac3 晋级同一 archive，SHA-256 为
  `e6388852c1b805c0a203256c95e2590aba2e37beff06f067cadd8a919f97d3d0`。双机实际 Backend cwd、
  health、Dashboard V7 和原控制面读回正常；ECS 97 / Mac3 106 个 target、九个生产标记保持。
  两机九方案 Dashboard/DataConsistency 全部通过；从 2025-07 开始的区间汇总、跨 5—6 月的日级
  区间与整月明细、展示分类、样本计数及非法区间拒绝均通过。Mac3 公网首页、全部同源静态资源
  GET/HEAD、MIME、内容版本摘要、缓存、匿名 401、精确拒绝路径和九方案认证 Dashboard 全部通过。
  正式浏览器普通刷新后数据就绪，日级筛选、整月详情、同年日期简写和每日验证表固定表头已实测。
  发布前后十张业务表的逐行内容摘要、数量及 schema 完全一致，未重算预测或改变调度。
  首轮 `e0ce91b4` 的页面资源版本 token 未更新，在公网验收时发现并由 `056534b2` 修正；
  previous 指向该中间版本，不应直接作为通过验收的回退目标。发布前稳定 release `22f5e734`
  仍保留，恢复须按部署手册重新核验兼容与授权。验收会话已撤销、临时候选 Backend 已关闭。
  完整证据见[发布回执](/Users/macstudio0/bond-factor-lab-runtime/releases/feature-date-display-20260917/delivery-report.json)
  与[证据索引](/Users/macstudio0/bond-factor-lab-runtime/releases/feature-date-display-20260917/README.md)。
- 2026-09-17 最终 Gate 合同 release `22f5e734` 由 clean commit 构建为同一 archive，SHA-256
  `5f906fbd8304c991b60810292bd2686a5ee3d8691289abf970f8c0b7c578b2e5`；Python、Node、MySQL 8.4、
  release 四个 CI 任务均成功，独立复审无 Critical/Important。ECS 候选认证 Dashboard/DataConsistency
  对目标方案通过后，按 ECS → Mac3 原子晋级并刷新 Backend；两端实际 cwd、health、schema 025、
  当日 DataBridge ready 和控制面读回正常，前后十张业务表计数完全一致。Mac3 目标方案的公网认证
  Dashboard/DataConsistency 均通过；公网入口脚本对首页同源静态资源、GET/HEAD、MIME、版本摘要、缓存、
  匿名 401 和精确拒绝路径全部通过。已删除的旧回测不被重建；仅在既有清单冻结的 78 条 live 业务值
  完全匹配时，Gate 才接受此方案的无回测分区和旧 retired Native 缺失字段，后续 Blackbox 仍严格校验。
  另行扩选的九个 Liwei 方案并非本轮原验收清单，其中八个旧回测 run 缺样本数权威，DataConsistency
  会如实阻断；用上一版 `47df` 在同一 ECS 数据上复核也有相同八项缺口，故不将扩选失败归因于新代码，
  也不声称该扩选范围通过。自然运行和公网长期性能观察仍在 TODO。
- 2026-09-17 二次审计候选 `69c1a7046432564fe1c50a0a9d9f7beed3197671` 已完成四层 CI、独立复审、
  ECS 候选与正式九方案 Dashboard/DataConsistency 验收并晋级；archive SHA-256 为
  `8588983881a76351d61203f89671464b05332de35a01547f130ef986ed02a8f3`。ECS 晋级前后
  Registry、Version、Prediction、Run、Backtest 与四类 Actual 计数均未变化。
- 2026-09-17 19:00 Actuals 自然结束后，两机使用同一 SHA-256
  `e5e2e2c7a04a2e2eb4ed29605b0f84cd342e46342209aa4140dd1c9e81262140` 的 `47df08fb` archive
  按 ECS → Mac3 顺序晋级，仅原子切换 current 并刷新 Backend。两机 Backend 实际 cwd、health、schema 025
  与 launchd/systemd 控制面读回正常；晋级前后 Registry、Version、Prediction、Run、Backtest 和四类 Actual
  表计数均不变。该提交的 Python、Node、MySQL 8.4、release 四项 CI 均成功，ECS 晋级后九方案
  DataConsistency 的 completeness/lineage/representation 全部通过。
- 上一版 `47df08fb` 发布时，Mac3 已按用户明确的“保留最新正确结果、不以旧审计血缘阻断”范围完成发布，
  当时的 Gate 合同尚不能记为通过：
  九方案 DataConsistency 的 representation 通过，completeness 仅因已删除的 333 条历史回测不再有
  backtest 产品引用而 `BLOCKED`，lineage 仅因保留的 78 个旧 Native live run 缺输入 artifact 和三个
  retired exact 缺 `manifest_hash` 而 `BLOCKED`；expected live 键无差异。九方案 Dashboard Gate 也因
  该 live-only 方案没有 backtest 分区而失败。其余八方案的公网精确入口、静态资源、拒绝路径及认证
  Dashboard Gate 全部通过；live-only 方案单独经公网认证 Summary/Detail 200 验证，9 月 live 明细
  17 行，最新四个 target 方向 `1/0/1/1`、Actual pending。该旧 Gate 合同随后由 `22f5e734`
  按保留 live 的精确业务值范围收口；当时的 `BLOCKED`/失败未被追溯改写，自然运行与公网长期
  504 观察仍另见 TODO。
- 2026-09-17 Mac3 已备份并受控重建 `liwei_0616_5y01_full_oos_k3_div_k10` 的 Blackbox exact
  `603f1574704b` 私有状态；旧状态备份在生产 backups 下 `blackbox-state-rebuild-20260917-Jc3iyc`。
  随后经正式 `gray_live` 入口精确补齐预测日 9 月 14—17 日四个缺口，run `5126`—`5129`，目标日
  9 月 18、21、22、23 日方向依次为 `1、0、1、1`。这些是授权补缺，不能冒充自然调度成功。
- 按用户确认的“保留最新正确结果、不保留旧审计历史”范围，Mac3 已经从产品事实中删除旧回测的
  333 个精确 ID（`9433`—`9765`），并删除其独占回测 run `159` 及级联的 333 条原始回测明细；
  旧历史区间现在不再展示。78 条原 live 与四条新 live 共 82 条仍在，9 月 Dashboard Detail 的
  最新四行方向未变，5 月 backtest Detail 为 0 行。没有复制跨机事实、删除 live、修改调度或切换服务。
  生产逐行备份和恢复清单为 `backups/legacy-backtest-only-20260917-47df08fb/plan.json`，文件 SHA-256
  `f81bbd40d55687a64a0e365ccc94a91cba788bac311526a0ef9f5b3e5e7f4653`；专用清理代码的四层 CI、
  隔离 MySQL 测试和独立复审均通过，同一 archive SHA-256 为
  `e5e2e2c7a04a2e2eb4ed29605b0f84cd342e46342209aa4140dd1c9e81262140`。
- 上一份双机共同 release 为 `3a665c36425a168fa1d24559c9dc987ed2901633`，archive SHA-256 为
  `1eba1e9fbf7a8b0b944bf8de2385ce920ef647021b4ae586481cae14aae25366`。
- 2026-09-16 上线前稳定性加固 release 已按 ECS → Mac3 顺序使用同一 archive 晋级。两机 Backend
  进程实际 cwd 均为 `3a665c36425a168fa1d24559c9dc987ed2901633`，健康且 schema 025；晋级前后
  Registry、Prediction、四类 Actual、Run 和 Backtest 数量不变。Mac3 真实 MySQL Summary 为 106 个方案，
  构建约 0.35 秒；ECS 为 97 个方案，构建约 0.63 秒。
- 公网中继仅新增 `/bond-factor-lab/factor-lab-http.js` 精确静态白名单，通过 `nginx -t` 后 reload；
  公网首页和全部静态资源为 200，新脚本字节摘要与 Mac3 release 一致，未认证 Dashboard 保持 401。
  中继变更前配置备份位于
  `/etc/nginx/bfl-backups/pre-release-hardening-20260916T1827/bond-factor-lab.before`。
- ECS systemd 与 Mac3 launchd 控制面未修改；发布时所有 Writer 均不在运行，Mac3 drift audit 通过。
  本次代码与部署闭环不代替下方各方案首次自然运行观察。
- 集成分支为 `codex/develop`。开发分支的代码或文档提交不代表新的生产 release。
  实时提交以 Git 引用为准，发布以目标机 manifest、current/previous 和进程 cwd 为准。
- 2026-09-13 文档规整版本已按 ECS → Mac3 同包晋级，仅刷新两机 Backend。Registry、exact、业务条数、ready 输入、Dashboard 读模型与生产名单均未改变；两机健康与首页正常，Mac3 公网首页与 release 字节一致，控制面保持原状。未运行算法、写业务事实或跨越正式触发窗口；自然观察仍待 TODO。证据见[同步回执](/Users/macstudio0/bond-factor-lab-runtime/releases/docs-guidance-20260913/delivery-report.json)与[索引](/Users/macstudio0/bond-factor-lab-runtime/releases/docs-guidance-20260913/README.md)。
- 当前 release 包含下述平台清理与收盘候选修复；[发布验收](/Users/macstudio0/bond-factor-lab-runtime/releases/platform-cleanup-20260913/delivery-report.json)及[证据索引](/Users/macstudio0/bond-factor-lab-runtime/releases/platform-cleanup-20260913/README.md)保存双机安装、数据、输入、HTTP 和控制面读回。此前框架清理证据仍在[原核验记录](/Users/macstudio0/bond-factor-lab-runtime/releases/framework-cleanup-20260913/verification.json)。

## 1Y T+1 跨期限日内方案交付状态

`one_y_t1_cross_tenor_intraday_v1` 已于 2026-09-16 按 ECS → Mac3 顺序使用同一不可变 release 完成首次
Blackbox V2 入库、持久化回测、激活和灰度区间补齐。平台只删除上游脚本的 100 条批量上限并同步帮助文案，
方向算法与 Metadata 未改；修正后 code hash 为 `c7cbc002a6c2f6e5494267808938d78947b22f29ed1281e33d70159878d5ab3c`，
Metadata hash 为 `f4f8e44ba028fa0372d64d4c7ab538b135a01f487f60288a1879b91e6e06e2b5`，exact 为
`4f0b95e57fdf`。

| 每机覆盖 | target 范围 | 条数 |
|---|---|---:|
| 历史回测 | 2025-01-03—2026-05-29 | 337 |
| `gray_live` | 2026-06-01—2026-09-16 | 77 |
| 产品事实合计 | 2025-01-03—2026-09-16 | 414 |

ECS backtest run 为 `293`，输入为 `full-20260916-063338-77f411b810dd` /
`snapshot-a3fa9393445761ecc4e4e355`；Mac3 backtest run 为 `263`，输入为
`full-20260916-063121-0ec9887c47b9` / `snapshot-4f96b57ebab7734843e87312`。两机分别计算，历史与灰度
target 零重叠，414 个业务键全部唯一；Registry 展示为 `BI_CROSS_TENOR_INTRADAY`、owner `fengrl`、
1Y / T+1 / h1。Dashboard 读模型已验证 active 且 `is_production=false`，未修改生产方案名单。

首次真实宿主时钟运行窗口为 2026-09-17 07:03 Asia/Shanghai；当前仅完成灰度补齐，不能把它冒充
`scheduled_live` 自然运行。双机数据库与 release 读回见[证据索引](/Users/macstudio0/bond-factor-lab-runtime/releases/one-y-t1-cross-tenor-20260916/README.md)
和[交付报告](/Users/macstudio0/bond-factor-lab-runtime/releases/one-y-t1-cross-tenor-20260916/delivery-report.json)。

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
  ECS 已在完整备份、事实摘要、run lineage、raw backtest 与业务键冲突检查后，删除
  `liwei_0616_5y01_full_oos_k3_div_k10` 被 411 条 `_bbv2` 纠正事实唯一替代的 411 条旧产品事实、
  78 个旧 live run、1 个旧 backtest run 及 1 个旧 retired version；替代来源的 411 条产品事实、333 条
  raw backtest 与成功 source run 保留，Dashboard 只读投影继续使用其精确 metadata。另一个来源未在本次清理
  范围内。Mac3 不存在该 `_bbv2` 来源，因此未执行相同删除。两项来源均不是普通待删项，也不构成新算法
  入库阻塞。

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
2026-09-13 按用户确认的完整九套生产方案名单，更新两机外置 `production_schemes.json`。当前 ECS / Mac3 各标记 9 个 target；文件摘要一致，真实 API 与 3Y、5Y、10Y 的 T+5、周收盘、月中收九个格子逐项核验通过，其他展示和统计未变。本次只更新名单，无需发布或重启；[结果与原名单备份](/Users/macstudio0/bond-factor-lab-runtime/releases/production-nine-marker-20260913/result.json)记录当前范围。名单标记不替代 TODO 的自然运行观察。

Registry 展示验收的 17 个迁移方案误报已解决；首次登记与后续展示的权威边界见[共享契约](architecture/SCHEME_CONTRACT.md#3-方案身份)。本次未更改 Registry 名称、描述、owner、算法或历史。

本地公共回归与独立复审通过，生产标记的非空、替换、清空、错误恢复和长名称视觉已在隔离环境验证。双机真实静态资源与验收版本一致；ECS 92 / Mac3 101 个 base 的 Gate 全部通过，认证 Summary、历史/最新 Detail 和待验证页面与本机事实一致。

发布前后 Registry、exact、历史、预测、Actual 条数及 ready 输入保持一致，两机独立核验。ECS 5 个 timer 保持 enabled/active、Persistent=false；Mac3 7 个 installed/loaded 任务配置不变，W4 九套入口与依赖可用。仅切换 current 并刷新 Backend，未运行算法、写业务事实或跨越正式触发窗口。自然观察另见 TODO。

证据：[最终报告](/Users/macstudio0/bond-factor-lab-runtime/releases/dashboard-registry-contract-20260913/delivery-report.json)、
[索引](/Users/macstudio0/bond-factor-lab-runtime/releases/dashboard-registry-contract-20260913/README.md)；
[原功能与隔离验收](/Users/macstudio0/bond-factor-lab-runtime/releases/dashboard-production-marker-20260913/)保留原计划、
视觉验证及 `migration-display-analysis.json` / `migration-display-findings.md` 的问题来源，不作为未完成发布状态。

## 当前输入与产品版本

双机使用五文件 DataBridge，存量 `factor_version` 已初始化为 `V1.0`；legacy 保护与新方案输入模式见[DataBridge](blackbox_v2/data_bridge_v1/README.md)。双机产品接口均为 `factor-lab-dashboard-v7`；完整读模型与表示见[Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)。

## 现场核验入口

- 发布与恢复：[部署运行手册](../deploy/README.md)。
- 调度：installed plist/unit、loaded state、journal/日志、实际进程和本机 run/prediction 联合核验，见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
- 公网故障：[Dashboard 故障定位](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#故障定位与恢复)。
- 日历、DataBridge、Actuals、状态和在途任务在每次操作前重新核验；本文中的数量和摘要不能充当操作时的现场证据。
