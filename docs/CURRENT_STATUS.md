# 当前状态

**文档状态**：`CURRENT`

**最近运行核验**：2026-10-24。以下是已核验基线，不替代实时现场检查；数量与摘要不能充当操作时的现场证据。

全局目标、已定决策与阶段进度以 `/Users/macstudio0/最终提示词-20261022.md` 为唯一来源；本文只记录灰度实验室当前的部署、范围与已知限制。固定主机、SSH/转发、生产路径和只读命令见[双机部署与访问入口](operations/DEPLOYMENT_ACCESS.md)。

## 目标与阶段

- **最终目标**：灰度实验室整体迁到 ECS（`aliyun-gray`，`47.103.45.193`），ECS 成为唯一数据源头与唯一生产写入方；Mac 转为只读的算力与验证平台，产出只能经带审计的导入进入 ECS。
- **阶段 D（两端预测一致）基本完成**，待用户确认：共有方案同输入下方向 101/101 相同，其中 76 个逐位相同，其余仅 CPU 架构（Mac arm64 / ECS x86_64）末位差异，逐方案证据见[汇报](/Users/macstudio0/数据对比核查/Mac-ECS一致性攻坚-20261021/html/汇报-一致性攻坚.html)“结果”一节及其架构差异附表。
- **阶段 E（迁移）暂缓**：用户确认阶段 D 后才讨论迁移，此前不做任何迁移动作。
- 迁移判据与 Phase 1 台账见[一致性总纲](architecture/MAC_ECS_PARITY_DRAFT.md)（草案）。Phase 1 的 21 个方案于 2026-10-21 闭环；过程证据已于 2026-10-24 删除，最终报告在 `/Users/macstudio0/数据对比核查/归档-Phase1/`。

## 运行与部署

| 项目 | ECS 灰度 | Mac |
|---|---|---|
| `BFL_DEPLOYMENT_TARGET` | `aliyun-gray` | `mac3-production` |
| current / previous release | `4d362931` / `3f77bc2b` | 同左 |
| Dashboard Backend 进程 cwd | `1073d68a` | 同左 |
| schema | 025 | 025 |
| 调度控制面 | 5 个 systemd timer（DataBridge、日/周/月预测、Actuals） | launchd：同样 5 项 + 公网 Backend 与反向隧道 |
| 部署方案数（按期限计） | 101（两端共有） | 110（101 + 9 个 W4 原生方案，只在 Mac 运行） |

- `4d362931`：预测失败自动重试（日/周/月预测，最多 2 次、间隔 10 分钟、只重跑失败方案；已写入的业务键跳过，批次级失败不重试；2 次后仍失败只写日志、不通知）。`3f77bc2b`：14 个方案交付的确定性修复。
- Dashboard Backend 仍运行 `1073d68a`，与现行代码在 backend/frontend 及其依赖上无差异；按已授权计划，周一观察通过后重启到 current release。
- 产品接口为 `factor-lab-dashboard-v7`（见 [Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md)）；输入为五文件 DataBridge，存量 `factor_version` 为 `V1.0`（见 [DataBridge](blackbox_v2/data_bridge_v1/README.md)）。两端各标记 9 个生产方案 target（外置 `production_schemes.json`，规则见 [Dashboard 合同](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#生产方案标记)）。
- **访问入口**：只用公网域名（中继 Nginx → `18101` 反向隧道 → Mac Backend `8101`，自 2026-10-18 起）或 ECS Dashboard。Mac 本地 `8100` Backend 与 `18100` 回退隧道已于 2026-10-24 退役，原 LaunchAgent 备份在 `~/.config/bond-factor-lab/launchagents-retired-20261024`。切换回执见[公网切换](/Users/macstudio0/bond-factor-lab-production/runtime/releases/public-cutover-20261018-1631/README.md)。
- **数据入库**在独立仓库 `bond-prediction`（两端 `9d5ee29`）：统一入口 `bin/run_job.py`，Mac crontab 由 `config/jobs.toml` 渲染，ECS 由 22 个 systemd timer 驱动。数据问题在入库层修复，不在模型或缓存侧补偿。
- Mac 源表上写 `sync_outbox` 的 36 个触发器已于 2026-10-24 删除（下游消费者自 09-24 起已停）；两端现均无源表触发器。
- 集成分支为 `codex/develop`（代码 = `4d362931`）。开发分支提交不代表新的生产 release；发布以目标机 manifest、current/previous 和进程 cwd 为准。

## 方案范围与一致性

- 注册表 110 个 target（按期限计数）= 两端共有 101 + 9 个 W4 Native 仅 Mac。W4 只在 Mac 运行，不迁移 ECS、不参与两端比对；ECS 注册表经 10-24 对齐后同为 110 行（含 9 个 W4，ECS 不部署、不运行）。实际范围以[部署矩阵](../deploy/scheme_deployment_matrix_v1.json)及本机 Registry/version 为准。
- Phase 1：21 个（2026-10-21 闭环）。Phase 2：80 个；73 个无增量方案同输入探针为 55 逐位相同、17 架构末位差异、1 架构差异放大（`weekly_10y_d_overlay_0529`），方向 0 不一致；7 个 liwei 增量方案回测方向一致、两端状态 `preds` 逐位相同，已激活并重建缓存。
- 输入：两端 DataBridge 以五文件 SHA256 与 `business_digest` 判定一致（不以原始表全表哈希为准；实时行情不进入 DataBridge）。
- 历史账本：2026-10-24 以 Mac 为准对齐 ECS 的预测、回测、指标、版本与注册表；两端预测 25,799 行、回测 152 条 / 明细 23,713 行内容哈希相同。历史预测与回测冻结，不重跑、不回填、不覆盖；再次对齐须用户授权。
- 待验证：2026-10-26（周一）07:03 首次全量同输入实盘对照，随后 ECS 连续 5 个交易日无回归。

## 已知限制

- ECS 4 核 / 14 GB，单个重任务（回测、状态重建）已贴近 2 小时上限；目前缓存状态的行身份哈希基于特征原始比特，Mac 状态迁到 ECS 会被判为“全部行已变化”。实测与改造计划见 [TODO](TODO.md#ecs-算力卡点与新验证流程)。
- 旧回测的全历史 DataConsistency Gate 不通过（2026-09-21 记录）：部分旧回测 run 缺 `persisted_prediction_count` 依据、旧产品记录 exact 为空、2025-01-26 旧目标日期与现行 T+5 日历不符。历史冻结，不伪填字段、不放宽 Gate；逐机例子见[维护报告](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/state-repair-20260921/README.md)。
- ECS 权威交易日历止于 2026-12-31，年均方案的下一 SF 锚点须待日历扩展后计算。

## 保留身份与恢复证据

- W4 Native 与 Blackbox 的保护原则见[根规范](../AGENTS.md)，迁移边界见[算法保真](architecture/SOURCE_ALGORITHM_FIDELITY.md)。
- `t_scheme_predictions.confidence` 与 `t_backtest_predictions.confidence` 已由 migration 025 删除。禁止回滚到仍读写 confidence、依赖已删临时身份或要求旧 Native 接管状态的 release；恢复按[部署手册](../deploy/README.md)核验权限、输入/状态和 Writer。
- 2026-09-17 按用户确认的“保留最新正确结果、不保留旧审计历史”，已删除 live-only 方案 `liwei_0616_5y01_full_oos_k3_div_k10` 的 333 条旧回测（ID `9433`—`9765`）及其 run；live 事实保留。
- 2026-09-24 旧 Mac3 MySQL 非正常重启后以 `innodb_force_recovery=6` 导出重建；逐表行数一致不单独证明崩溃前全部提交无损。见[恢复记录](/Users/macstudio0/bond-factor-lab-production/runtime/recovery/mysql-20260924/README.md)。
- 已完成交付批次的外置证据（只读追溯，不作为操作入口；方案身份与 exact 以当前 Registry 为准）：

| 批次 | 本机证据索引 |
|---|---|
| 五套新方案（2026-09-12） | [README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/new-five-20260912/README.md) |
| 三套 5Y 周频（2026-09-13） | [README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/weekly-5y-lowcorr-20260913/README.md) |
| 1Y T+1 跨期限日内（2026-09-16） | [README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/one-y-t1-cross-tenor-20260916/README.md) |
| 四套 V3（2026-09-20） | [README](/Users/macstudio0/bond-factor-lab-production/runtime/releases/four-v3-20260920/README.md) |

## 现场核验入口

- 发布与恢复：[部署运行手册](../deploy/README.md)。
- 调度：installed plist/unit、loaded state、journal/日志、实际进程和本机 run/prediction 联合核验，见[调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。
- 公网故障：[Dashboard 故障定位](operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#故障定位与恢复)。
- 日历、DataBridge、Actuals、状态和在途任务在每次操作前重新核验。
