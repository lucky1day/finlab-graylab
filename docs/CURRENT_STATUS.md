# 当前状态

**文档状态**：`CURRENT`

**最近运行核验**：2026-09-13。以下是已核验基线，不替代实时现场检查。

## 运行与部署

源码方案迁移及平台 confidence 退役已闭环；新算法可直接进入[标准入库流程](onboarding/README.md)。

| 项目 | ECS 独立灰度 | Mac3 生产 |
|---|---|---|
| 迁移结果 | 17 个原方案、21 个 target 使用 Blackbox | 同左 |
| 当前 active 执行身份 | 89 个 Blackbox base | 84 个 Blackbox base + 9 个 W4 Native base |
| Dashboard | 93 个 target；新月频待验证月份入口验收阻塞 | 97 个 target |
| 调度控制面 | systemd one-shot/timer | launchd + installed plist |
| current release | `699b986c00be100a42f9e173a87f0f446b566cc8` | `5f6c60cfd78457c2b1a6d52338e3a44f72c578ec` |
| previous release | `5f6c60cfd78457c2b1a6d52338e3a44f72c578ec` | `478850a23a4c9690f81aab41de90eccf21e61abb` |
| schema | 025 APPLIED | 025 APPLIED |

- ECS archive SHA-256：`b6f0cae7fe18d008bed0468ad5aeff990f1a451c50c55d43f07aec031e0667d4`；Mac3 仍为 `c449e0bf515ccd7024cf36ea498d450b468f1fdf3117bf368236e04c70acc3e8`。
- ECS 新 release 的 Backend cwd、健康、五套 exact、完整数据及调度模拟已验证；前端验收未闭环，Mac3 尚未晋级。
  域名仍由 Mac3 服务；DNS、Nginx、认证和既有 SSH 隧道未改变。验收使用用户授权的临时 localhost 转发。
- 两端独立使用自己的 MySQL、DataBridge 和派生状态，不复制数据库、不双写、不跨机共享输入。
- 集成分支为 `codex/develop`。迁移收尾时两条远端分支已同步；其后文档提交不代表新的生产 release。
  实时提交以 Git 引用为准，发布以目标机 manifest、current/previous 和进程 cwd 为准。

## 五套新方案交付状态

- ECS 已激活两套周频 `weekly_3y_full_action_rule0001_v2@ac06e7df4582`、
  `weekly_10y_full_action_hgb0061_v2@e87140ab2ce2`，以及三套月频
  `cgb_a4_fundseason_3y_hl18@79c08e273ecd`、`cgb_a4_fundseason_5y_hl18@38c8fa7b0220`、
  `cgb_a4_fundseason_10y_hl18@295c00e305e2`。上游脚本原字节保留，仅两份周频 Metadata owner 修正为 liwei。
- ECS 每套周频历史 72 条（target 2025-01-10—2026-05-29）、灰度 16 条（2026-06-05—2026-09-18）；
  每套月频历史 16 条（2025-02-14—2026-05-15）、灰度 4 条（2026-06-15—2026-09-15）。
  合计 192 条历史、44 条灰度；业务键完整，来源、exact、三日期及存量记录保护通过。
- ECS 五套 DashboardGate 及 236 条 HTTP 明细与数据库核对通过，但月度汇总过滤纯待验证月份，
  三套月频 2026-09 无页面明细入口。按合同失败停止条件暂停 Mac3 晋级，待确认公共展示修复与新包发布。
  Mac3 尚未执行本批新身份的回测、激活、补缺或 release 切换。
- 证据：开发机 runtime 下 `releases/new-five-20260912/`，其中 `ecs/data-validation.json`、
  `ecs/schedule-simulation.json`、`ecs-ui/dashboard-details.json`、`frontend-blocker.json`；
  ECS 原件在 `/opt/bond-factor-lab/incoming/new-five-20260912/`。

## 保留范围与历史保护

- W4 九个加密方案仅在 Mac3 按原 Native 方式运行，保留其算法、二进制、adapter、runner、输入隔离和维护 Gate；不部署 ECS。
- 其他 canonical 只保留 Blackbox 交付，不保留 Native 附件、Liwei Phase-A 调度或临时迁移入口。
  精确部署身份以 [部署矩阵](../deploy/scheme_deployment_matrix_v1.json)和本机 Registry/version 为准。
- T1/T5 保留原 base ID、整体 exact version 与多目标原子提交。周/月执行 horizon 与原事实 horizon 的映射见[共享契约](architecture/SCHEME_CONTRACT.md)。
- 原 ID 的预测、run、回测及来源保持不变；源数据修订不触发已发布预测重算或覆盖。
- ECS 的 11 个独立临时身份及 Mac3 的两个旧验证库已清理。以下两个 ECS 身份获批永久保留为只读历史来源：
  - `liwei_0616_5y01_full_oos_k3_div_k10_bbv2`
  - `liwei_0616_cons_sda_k3_div_k10_bbv2`
- 两个来源的 Registry 为 archived、版本为 retired，无 canonical、Writer 或 Dashboard 展示。
  原 ID 四条预测仍引用其回测；不得删除来源、解除外键或改挂历史。它们不是待删项，也不构成新算法入库阻塞。

## 平台字段与恢复边界

- `t_scheme_predictions.confidence` 与 `t_backtest_predictions.confidence` 已通过 migration 025 删除。
  平台不再要求、传递、存储或展示该统一字段；Blackbox Result 仍精确为五字段。
- 算法内部概率、阈值、排序、投票、模型选择和方向计算不变。原始 benchmark、旧 SQL/release、
  已有 source_row/extra 审计内容保留，不按关键词清理。
- current/previous 均兼容 schema 025。禁止回滚到仍读写 confidence、依赖已删临时身份或要求旧 Native 接管状态的 release。
- 回滚须先隔离受影响 Writer，核验数据库、exact version、输入和状态兼容，再恢复调度。
  不能只切 current 链接，也不能用旧整库快照覆盖仍在增长的生产数据。
- 永久备份与恢复原件：
  - ECS confidence：`/opt/bond-factor-lab/backups/confidence-retirement-20260912/ecs-v1`
  - Mac3 confidence：`/Users/macstudio0/bond-factor-lab-production/backups/confidence-retirement-20260912/mac3-v1`
  - ECS 临时身份清理：`/opt/bond-factor-lab/backups/database-cleanup-20260912/ecs-eleven`
  - Mac3 旧验证库清理：`/Users/macstudio0/bond-factor-lab-production/backups/database-cleanup-20260912`
- confidence 完整恢复集必须同时包含 SQL gzip 与 `original-audit-json.json.gz`；不能只恢复 SQL 而丢失原始 JSON 精度。
  先在隔离库验证恢复，再按明确授权处理精确对象；备份、原始验收回执和 immutable archive 不随文档清理删除。

## 输入、产品与入库

- 双机使用五文件 DataBridge；`factor_version` 存量已初始化为 `V1.0`。
  各机 generation、catalog、ready receipt 与输入摘要必须独立读取，不能从另一主机推断。
  存量 `legacy_v1` 保护和新方案 `algorithm_managed` 规则见[输入契约](blackbox_v2/data_bridge_v1/README.md)。
- 新方案只走两文件 Intake → 一次完整持久化回测 → 独立授权 activate；迁移例外不放宽新算法验收。
  原历史不因包装升级重算，人工模拟不冒充自然 `scheduled_live`。
- 平台只读 `/api/factor-lab/dashboard`，合同为 `factor-lab-dashboard-v5`。
  `t_scheme_predictions` 是唯一产品逐点事实源，回测表仅作不可变证据及元数据；
  `run_id/backtest_run_id` 保持互斥来源。
- `backtest/live` 由 `target_date=2026-06-01` 分界；`gray_live/scheduled_live` 只属于 run 审计。
  owner 以 Registry 为展示权威，缺失或非法值 fail-closed。
- 生产写入保持 insert-only 与唯一 Writer；灰度补缺、激活、发布、DDL 和调度变更均需各自授权。
  未闭环运营事项只记录于 [TODO](TODO.md)，不与迁移完成状态混写。

## 现场核验入口

- 发布与恢复：[部署运行手册](../deploy/README.md)。
- 调度：installed plist/unit、loaded state、journal/日志、实际进程和本机 run/prediction 联合核验，见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
- 公网故障：[刷新与链路可靠性](operations/PUBLIC_FACTOR_LAB_REFRESH_RELIABILITY.md)。
- 日历、DataBridge、Actuals、状态和在途任务在每次操作前重新核验；本文中的数量和摘要不能充当操作时的现场证据。
