# 实施计划

**日期**: 2026-06-05
**预计阶段**: 8个Phase
**当前状态**: 新模拟生产机器已完成服务环境、正式平台表、actuals、历史回测、周度 10Y 回测、API 和 launchd 常驻验证。`t1_daily` / `t5_daily` 均为 active，2026-06-05 09:25 已写入正式预测；`weekly_10y_d_overlay` 已按实盘 week_id 规则进入 `framework_db_aligned` 回测矩阵和前端周度格子，并已在 2026-06-06 切换为 active、注册周六 11:30 自动调度。

---

## Phase 1: 项目初始化（当前阶段 ✓）

- [x] 创建仓库和目录结构
- [x] 编写CLAUDE.md项目规范
- [x] 编写PRD需求文档
- [x] 编写架构设计文档
- [x] 整理研究资料

## Phase 2: 基础设施

- [x] 创建并执行数据库migration SQL脚本
- [x] 创建独立后端/API/调度器conda环境
- [x] 实现shared层（db_config环境变量化、data_service、公共输入文件层 input_artifacts、models）
- [x] 创建pyproject.toml和依赖管理
- [x] 编写.env.example

## Phase 3: 方案迁移

- [x] 迁移t1代码到schemes/t1_daily/，编写adapter
- [x] 迁移t5代码到schemes/t5_daily/，编写adapter
- [x] 编写两个方案的config.yaml
- [x] 本地验证方案可独立运行

## Phase 4: 调度器

- [x] 实现scheduler/discovery.py（方案扫描）
- [x] 实现scheduler/executor.py（执行+写DB）
- [x] 实现scheduler/main.py（APScheduler入口）
- [x] 实现actuals更新任务（16:00）
- [x] 测试手动触发和 launchd job 注册
- [x] 观察真实 9:25 预测定时触发
- [ ] 观察真实 16:00 actuals 定时触发

## Phase 5: 后端API

- [x] 实现FastAPI应用骨架
- [x] 实现/api/schemes端点
- [x] 实现/api/metrics/{scheme_id}端点（核心）
- [x] 实现/api/predictions和/api/actuals端点
- [x] 实现/api/schemes/{scheme_id}/trigger端点
- [x] API测试

## Phase 6: 前端

- [x] 基于现有原生HTML/CSS/JS页面替换mock数据
- [x] 实现实盘测试Dashboard布局
- [x] 实现AccuracyMatrix组件
- [x] 实现MetricSummary和TrendChart
- [x] 对接后端API
- [x] 完成 FastAPI 静态 serve 和服务侧 iframe 准备
- [ ] 接入 panda_quantflow 外层 shell（外层仓库路径待提供）

## Phase 7: 部署

- [x] Mac Studio环境配置
- [x] 创建launchd plist
- [x] 安装并启动 launchd backend/scheduler
- [x] 端到端测试（手动链路已通过）
- [x] 文档更新

备注（2026-06-05）: launchd 已安装并启动，backend 健康接口已验证；真实 9:25 预测窗口已观察通过，`t1_daily` / `t5_daily` 共写入 6 条预测和 2 条 success run_log。16:00 actuals 窗口仍需继续观察。

## Phase 8: 历史回测复现

- [x] 建立 `benchmarks/model_muti_0529` canonical 基准入口
- [x] 忽略并清理含本地 DB 密码的原始解压目录 `model-mutitest-0529/`；运行路径不再依赖 `_original_source`
- [x] 新增并执行 backtest 表 migration:
  - [x] `t_backtest_runs`
  - [x] `t_backtest_predictions`
  - [x] `t_backtest_monthly_metrics`
  - [x] `t_backtest_reproduction_checks`
- [x] 新增历史复现 runner
- [x] 完成 CSV vs DB 对齐检查；DB 侧先通过公共输入文件层按上游 `data_service.py` 生成 daily_output，Y 生成所依赖的收益率列误差全部为 0，因子列差异已记录
- [x] 当前验证口径暂排除 `target_date=2026-05-25` 至 `2026-05-29` 的 5 月最后目标周样本
- [x] 完成 t5 原始 baseline / framework-csv / framework-db 复现，原始 1328 行，有效 1312 行，framework 两组 mismatch 均为 0
- [x] 完成 t1 原始 baseline / framework-csv / framework-db 复现，原始 1011 行，有效 999 行，framework 两组 mismatch 均为 0
- [x] 新增 backtest API
- [x] 前端保持原方案结果展示，不新增历史验证结果页，并接入最新 `framework_db_aligned` 回测结果
- [x] 新增 `scripts/verify_reproduction.py` 验证脚本并通过
- [x] 新增 `weekly_10y_d_overlay` 回测 runner，最新前端展示 run_id=13，并在前端周度格子展示

## 当前剩余观察项（2026-06-06）

- [x] 人工补齐当前测试所需目标期限数据
- [x] 重新执行 actuals 刷新
- [x] 恢复并验证 `t5_daily` adapter
- [x] 等待真实 9:25 调度窗口写入 T+1/T+5 实盘预测
- [x] 生成本机 `.env` 并安装 launchd 常驻服务
- [x] Mac 重启自恢复验证（用户确认不需要，已跳过）
- [ ] 验证真实 16:00 actuals 定时触发
- [ ] 拿到 panda_quantflow 外层仓库路径后完成 iframe 菜单/路由接入
- [x] 接入周度 10Y 方案的历史回测和前端展示
- [x] 接入独立周度 actuals 表与 weekly metrics 口径
- [x] 周度 live 预测已完成 readiness、dry-run、受控写库验收，并已启用自动 scheduler 调度。
- [x] 周度 10Y 已完成单方案 scheduler 手动补跑，并修正周度 job `force=True`，避免周六被日度非交易日保护跳过。
- [x] 周度 10Y 历史回测日期转换和月度指标已修正为使用算法输出 `month_date/week_date`；`2025-07` 现在返回 4 个样本，`2025-10` 返回 5 个样本。
- [ ] 观察下一次周六 11:30 周度自动调度。
- [ ] 等待 `2026-06-12` target actual 后，确认 live 样本进入 weekly metrics。
- [x] 周度 live/backtest 已切换为 `/Users/macstudio0/Desktop/wind_export(1).py` 口径的公共周频输入链路；最新 DB 回测已写回 run_id=`13`，结果为 `68.9% (31/45)`。
- [ ] 后续专项: 修复公共周频导出与 `/Users/macstudio0/Desktop/weekly_output.csv` 的剩余逐值差异；当前导出已做到行数、列数、列顺序和关键最新周收益率列对齐，但仍有 12 个实质数值差异、101 个缺失差异和大量小数精度差异。

## Phase 9: 周度 10Y live 启用计划

详细执行 SOP 见 [WEEKLY_LIVE_ROLLOUT_PLAN.md](WEEKLY_LIVE_ROLLOUT_PLAN.md)。

当前状态（2026-06-06）:

- [x] 周度方案已从 `paused` 切换为 `active`，常驻 scheduler 已注册周六 11:30 自动调度。
- [x] 周度方案 live cron 已调整为 `30 11 * * 6`，对齐旧实盘 weekly `multi` 任务的周六 11:30 首轮预测时间；旧脚本 16:00 / 22:00 为检查和必要补跑节点。
- [x] 已新增 `scripts/check_weekly_10y_readiness.py`，用于只读检查周度 live 关键指标覆盖。
- [x] 已新增 `scripts/write_weekly_10y_live_prediction.py`，用于 readiness + dry-run 通过后的单方案受控写库。
- [x] `t_scheme_predictions` 中 `weekly_10y_d_overlay` live 记录数为 1，来自 2026-06-06 受控写库验收。
- [x] live 预测已修正为按 `feature_date` 从源表反查实际 `week_id`，`2026-06-06` 使用 `feature_week_id=202621`。
- [x] `api_wind_derivative_weekly` 关键指标代码 `TB0YWI3C/TB1YWI3C/TB5YWI3C` 最新完整到源表 `week_id=202621`。
- [x] `2026-06-06` readiness 成功返回 `ready=true`。
- [x] `2026-06-06` dry-run 成功返回 1 条 10Y 周度预测。
- [x] 周度输入已支持只读 `api_wind_daily` 现场生成缺失 weekly close，不写回源表。
- [x] 启用前已通过 `crontab -l` 复核上游 cron 仍为周六 11:30 `multi`，且 BFL 周度 cron 仍对齐该首轮预测时间。
- [x] 源数据 readiness 通过后，重新执行目标周六 dry-run。
- [x] dry-run 通过后，执行一次仅限 `weekly_10y_d_overlay` 的受控 live 写库验收。
- [x] 写库后核验: `t_scheme_predictions=7`、`t_scheme_run_log=4`、`t_scheme_actuals=13900`、`t_scheme_weekly_actuals=794`。
- [x] 验收通过后，已把 `schemes/weekly_10y_d_overlay/config.yaml` 的 `status` 从 `paused` 改为 `active`。
- [x] 已重启 scheduler 并确认注册 `Scheduled scheme weekly_10y_d_overlay at 30 11 * * 6`。
- [x] 已完成 2026-06-06 单方案 scheduler 手动补跑: `records_written=1`、`status=success`；prediction UPSERT 后仍为 1 条 weekly live prediction，run_log 新增 1 条 weekly success。
- [x] 已修正周度 scheduler job 为 `force=True`，避免周六被通用非交易日保护跳过；日度方案仍保留交易日判断。
- [ ] 观察下一次周六 11:30 自动调度。
- [ ] 等待 `2026-06-12` target actual 后，确认 `2026-06-06` live 样本进入准确率统计。
- [ ] 周频输入导出待办: `/Users/macstudio0/Desktop/wind_export(1).py` 当前只读导出与桌面 `weekly_output.csv` 结构已对齐、关键收益率列一致、最新预测输出一致；剩余逐值差异暂不作为当前 live 启用阻塞项。
