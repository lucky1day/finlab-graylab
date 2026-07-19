# 当前状态

**文档状态**：`CURRENT`
**适用运行时**：`native_adapter`、`blackbox_v2`
**目标读者**：项目负责人、平台运维和审计人员
**最后核验日期**：2026-07-19

## 2026-07-19 双运行时入库政策

- 当前仓库共有 29 个 `native_adapter` 方案和 1 个 `blackbox_v2` 试验方案；版本化清单见 `deploy/onboarding_policy_v1.json`。
- 29 个 Native V1 方案保持现有 Registry、数据库、scheduler 和历史结果，只允许故障、数据口径、复现性和经批准的保真维护。
- 后续新算法、新方案 ID、新目标、新任务和替代版本一律通过 Blackbox V2 两文件交付；StaticGate 与 ActivationGate 均阻断清单外 Native ID。
- 当前 Blackbox 试验方案 `weekly_10y_lgbm_point_v1` 仍为 `shadow + paused`，未激活、未进入生产 scheduler、未写入预测或回测业务表。
- `weekly_10y_lgbm_point_v1` 已增加文档标记 `GRAY_LAB_READY`：可以安排灰度实验室内的 no-persist 预测、回测和对照实验；该标记不等于 `gray_live`，不改变配置、Registry 或数据库状态。
- 首轮持续回归基线 `hr_20260719T090240Z_64d1d64d4def` 已通过七个 Gate，100/100 回测成功，Harness 自动段约 26 秒；独立查询确认该 trial 的预测、运行和回测业务记录仍全部为 0。
- Blackbox V2 生产晋级仍受[生产准备清单](blackbox_v2/PRODUCTION_READINESS.md)阻断；本轮文档和政策门禁整理不改变任何方案运行状态。

所有入库场景从[统一入库导航](onboarding/README.md)进入。下方带日期记录是历史时点事实；其中旧“新增 Native 方案”链接或操作方式不再代表当前政策。

## 2026-07-14 投票方案无信号转平

平台已实现统一的 `no_signal_to_flat_v1` 输出规则：对当前 active registry 范围内的投票、共识及投票叠加方案，确认输入 artifact 包含当前 feature key 且必要字段有效、日期上下文和 core 计算均成功后，如果非空合法 core 结果中明确缺少当前 feature key 的最终方向，则 adapter/backtest 输出 `predicted_direction=0,confidence=0.0`，并写入 `signal_state=no_signal`、`signal_policy_applied=true`、固定 reason 与具体 `source_component`。算法原生平保持原样；输入水位不足、当前周关键值缺失、日历异常、整体空/非法 core 输出、模型异常、超时和代码错误继续 fail-closed。当前范围按已批准清单为 19 个 base 方案、23 个 active target；没有批量修改 19 份 `config.yaml`，也没有修改任何 `schemes/*/core/`、投票、fallback、阈值或原始 benchmark。

代码侧新增 `shared.signal_policy`，三个周度点位 adapter 已接入并前置校验当前周输入；executor 已把成功条件收紧为 active target 集合完全相等，并要求 `records_expected == records_returned == records_written`。周频公共回测增加默认 `skip`、按方案显式 `flat` 的策略，5Y/7Y/10Y 点位方案均显式启用；整批 core 空输出或非法 `week_id` 会在公共索引层失败，不能批量生成平。平台补平行进入 live/backtest 明细与样本总数，但不进入 `metric_samples` 和准确率分母。SQL 导出和 runner compact benchmark 均过滤 `signal_policy_applied=true` 的平台行；payload 用 `row_count` 表示平台明细数、`benchmark_row_count` 表示过滤后的 benchmark 行数，source-original/current CSV 与 CompareGate 仍只比较原算法实际输出。

真实 DB 输入下的 no-persist 验证已完成：5Y 为 `row_count=72,benchmark_row_count=71`，其中 `feature_week_id=202534` 生成 1 条平，原始 benchmark 71/71 matched，`samples=72,metric_samples=71`；7Y 为 `row_count=72,benchmark_row_count=68`，其中 `202529/202534/202547/202608` 生成 4 条平，原始 benchmark 42/42 matched，`samples=72,metric_samples=68`。10Y 当前无法完成 no-persist：历史输入含 `week_id=200901`，但 `api_wind_date/t_trade_calendar` 的可用交易周从 `201001` 开始；同期 live 的 `week_id=202625` 也仍有已知周历/输入异常。两类 10Y 问题均须先修数据再按算法真实输出重跑，禁止直接补平。

用户一次性授权后，5Y/7Y 已完成受控发布。ActivationGate 分别激活版本 `c3526a721528` 与 `e0f46b070b0e`；四次 LiveGate 以 `scheduled_live` 回补 `predict_date=2026-07-04/2026-07-11`，run_id=`874/875/876/877`，对应 `feature_date=2026-07-03/2026-07-10`、`target_date=2026-07-10/2026-07-17`。四条记录均为 `predicted_direction=0,confidence=0.0`，且带 `signal_state=no_signal`、`signal_policy=no_signal_to_flat_v1`、`signal_policy_applied=true`、固定 reason 和 `rule_vote/cross_d_overlay` source component。每次授权写入严格只有 `t_scheme_runs +1`、`t_scheme_predictions +1`、`t_scheme_run_log +1`。

5Y/7Y latest backtest 已通过单次 `backtest_persist` token 重建为 run_id=`163/164`，各 72 行。5Y 为 `samples=72,metric_samples=71,predicted_dist.flat=1`，政策平 feature key 为 `202534`；7Y 为 `samples=72,metric_samples=68,predicted_dist.flat=4`，政策平 feature key 为 `202529/202534/202547/202608`。两次 BacktestGate 各只增加 `t_backtest_runs +1,t_backtest_predictions +72`，未改源表、actual、live 和 run_log。发布后业务表计数为 `t_scheme_predictions=775`、`t_scheme_runs=877`、`t_scheme_run_log=899`、`t_backtest_runs=56`、`t_backtest_predictions=15138`。

10Y 保持 fail-closed。本轮没有为 10Y 签发或消费 `activate/live_write/backtest_persist` token，没有回补 live，也未重建 run_id=`108` 的 72 行 latest。需要区分的是，5Y/7Y ActivationGate 的全量 registry sync 已把当前 10Y config 版本 `23ed6b85cf08` 登记为 active；该登记不代表 10Y 自身通过 Gate 或获得发布授权，实际运行仍因数据故障失败且不写预测。根因进一步确认：当前 `api_wind_date` 已把 `2026-06-29..2026-07-05` 归为 `202626`，但 `api_wind_weekly` 同期仍同时保留 `202625/202626` 重叠周数据，live artifact 无法建立可信的唯一周映射；当前历史 artifact 相比 2026-06-11 已验证 artifact 有 88 列、128 个单元格漂移，并在 `feature_week_id=202533` 出现 confidence `0.8618307845278366` vs benchmark `0.90256711825722`。旧 artifact 仍可 45/45 通过 source benchmark，证明阻塞来自受治理源数据，不是本轮算法或补平代码。按源表只读和算法保真约束，不得删除 ghost week、改 benchmark 或直接补平；须由上游数据链修复后重跑全 Gate。

验收结果：新增边界聚焦回归 103/103、最终全量测试 761/761 通过；StaticGate 白名单已加入新的公共 `shared.signal_policy`，同时保留对 `shared.data_service` 等越层 import 的拦截。5Y 完整无副作用 harness `hr_20260714T090518Z_5ebd89a11f5a` 与 7Y `hr_20260714T090543Z_1becdebd10f2` 均通过；发布后 active-only ApiGate 也均通过。scheduler 已通过 `launchctl kickstart -k` 重启，运行 PID 更新为 `91407`，日志确认全部 active 任务重新挂载。浏览器前端验收确认 5Y/7Y 的 `2026-07` 行均显示样本 `2`、预测分布 `0/1/1`、准确率分母 `1`；验证表中 `07/04 -> 07/10` 和 `07/11 -> 07/17` 均显示“平”，结果列为 `-`，后者实际方向为“待验证”，页面控制台无错误。

## 2026-07-13 10Y01 / 5Y01 原脚本 Full-OOS 灰度方案

新增两个独立方案 `liwei_0616_10y01_full_oos_k3_div_k10` 与 `liwei_0616_5y01_full_oos_k3_div_k10`，旧的 10Y01 / 5Y01 active 方案保持不变。新方案只把测试序列改为原脚本的 `2024-01-01..source_end` 单段连续 Full-OOS；baseline、共识、seasonal VT、streak-break fallback 和 core 参数均保留原脚本。StaticGate 要求方案 code hash 自包含，因此两个新目录各保存一份与已验证 source core 字节一致的本地副本，adapter 只负责 Full-OOS 窗口和平台日期语义转换。

两方案均已完成 Static/Input/Unit/DryRun/Compare/Backtest/ApiReadiness 全门禁，最终通过记录分别为 `hr_20260712T160937Z_909ea359ac86` 与 `hr_20260712T160942Z_6ed605b92de5`。CompareGate 的 2026-04 target-month 样本均为 21/21 方向一致、内部 score/sign 0 mismatch；真实 runner 与归档 benchmark 的方向、label 也全部一致，浮点差仅约 `1e-16`。授权持久化回测生成 run_id=`158/159`，各 333 条，feature date `2025-01-02..2026-05-22`、target date `2025-01-09..2026-05-29`；灰度 target month 从 2026-06 起，不与历史回测重叠。

ActivationGate 已将两方案激活，active scheme version 分别为 `fefcef733cad` 与 `db1680bc4741`。灰度 LiveGate 使用 `predict_date=2026-07-10` 写入 run_id=`669/670`，两条记录均为 `feature_date=2026-07-09`、`target_date=2026-07-16`、`prediction_phase=gray_live`，与 7Y 日频 T+5 的“运行日 -> 上一交易日特征 -> 第 5 个交易日目标”口径一致。`/api/schemes`、`/api/backtests/factor-lab` 和两个 composite `/api/metrics` 均已返回新方案；回测摘要分别为 `63.9% (177/277)` 与 `64.3% (169/263)`。

2026-07-13 已按 7Y 日频 T+5 网格补齐两方案的 2026-06/07 灰度观察数据。每个方案现有 33 条 `gray_live`，predict date `2026-05-26..2026-07-10`、target date `2026-06-01..2026-07-16`；另有当天自然调度产生的 1 条 `scheduled_live`，predict date `2026-07-13`、target date `2026-07-17`。两个方案均与 7Y 参考网格的 34 个 target date 完全一致，`missing=[]`、`extra=[]`；其中 6 月 21 行、7 月 13 行。当前 actual 水位可验证到 7 月前 8 行：10Y 六月准确率 `53.3% (8/15)`、七月 `50.0% (4/8)`；5Y 六月 `52.9% (9/17)`、七月 `100.0% (7/7)`。10Y 首次冷建 run_id=`687` 因 3600 秒超时未写预测；重试后完成。10Y `predict_date=2026-07-07` run_id=`749` 模型成功并写入 1 条预测，LiveGate 仅因同期外部 DataBridge 写入 `api_wind_daily +1` 报源表并发 delta，授权方案表增量与预测输出均正确。

业务 cron 均保持 `3 7 * * 1-5`、`Asia/Shanghai`、`timeout_sec=3600`。scheduler 已重启并加载新配置；在当前 `2` 分钟错峰和单并发设置下，业务 cron 仍归属 07:03 组，物理执行槽位为 10Y01 Full-OOS `07:15`、5Y01 Full-OOS `07:19`。截至本次记录，两方案均已有真实时钟自然触发的 `scheduled_live`，达到 **Production Observed**。

## 2026-07-12 liwei_0616 T+5 日频增量 Phase A 缓存

本轮只修改五个 `liwei_0616` 日频 T+5 方案。实盘 adapter 首次运行会按 baseline 生成 `.pkl` Phase A 缓存，后续日期在锁内读取上一水位，只对 `missing_dates` 执行原始滚动训练，再把完整缓存交给未改动的 Phase B/C、seasonal VT、consensus 和 fallback 路径。历史 backtest/benchmark 默认仍走原路径，不强制使用该实盘缓存。

缓存按期限共享：5Y 家族 3 个 baseline；7Y_01/7Y_03 共用 4 个 baseline；10Y_01/10Y_02 共用 4 个 baseline。默认目录为 `backtest_artifacts/runtime_cache/liwei_0616/{5y|7y|10y}/{baseline}.pkl`。缓存 identity 覆盖 canonical baseline 配置、IC 起点、horizon/purge gap、ABI 以及 Python/NumPy/pandas/LightGBM 版本；已完成的 daily/weekly/monthly 历史前缀发生修订时冷重建。当前未结束周/月的聚合值允许随新日变化，待下一周/月出现后再纳入受保护前缀，避免正常日更被误判为历史修订。写入使用逐 baseline `flock`、临时文件、`fsync` 和 `os.replace`；损坏文件隔离，失败扩展保留旧缓存，较早截断请求不会降低较新持久水位。

真实隔离 DryRunGate 使用 `predict_date=2026-06-11/2026-06-12` 验证：5Y、7Y_01、10Y_01 均得到 `cold_build → extended`，扩展时 `missing_dates` 只有 `2026-06-11`；同日重跑为 `hit`。7Y_03 直接复用 7Y_01 的缓存并返回 `hit`。5Y 另以“完整冷建到 2026-06-11”与“从 2026-06-10 增量追加 2026-06-11”两份独立缓存逐值比较，最终方向、置信度、target date、`vote_score`、全部 baseline score/sign 完全一致。所有 DryRunGate 受保护表 delta 为 0。10Y_02 的完整 OOS 日期集合是 10Y_01 两段窗口的超集，因此预热 CLI 选择 10Y_02 作为 10Y 家族代表，确保首建后两个 10Y 方案均可复用。

验证结果：缓存/五方案 adapter/预热聚焦单测 89/89 通过；五个回测模块 82/82 通过；五方案 Static/Input/Unit/Compare Gate 共 20/20 通过；`forecast_env` 下预热 CLI `--help` 通过。未执行正式缓存预热、scheduler 重启、业务写库、`master` 合并或远程推送。

## 2026-07-11 T+1 最新验证与周末 actual 补刷

本轮复核确认：T+1 预测本身已更新到最新交易日。5 个 active T+1 业务方案均已有 `predict_date=2026-07-10`、`feature_date=2026-07-09`、`target_date=2026-07-10` 的 `scheduled_live` 明细；`scripts/check_production_daily_health.py --predict-date 2026-07-10 --strict-runs` 返回 `status=ok`、`findings=[]`。

前端当时看不到最新验证结果的原因不是预测缺失，也不是前端缓存，而是 actual 表晚于源表：`api_wind_daily` 的 `1Y/3Y/5Y/7Y/10Y` 已到 `2026-07-10`，但 `t_scheme_actuals` 仍停在 `2026-07-09`。日志显示 `2026-07-10 08:30/19:00/23:45` actuals job 均执行过；结合当前水位判断，`2026-07-10` 源 actual 是在最后一次 `23:45` 后才补入的。`2026-07-11` 是非交易日，旧 `run_actuals_job()` 在非交易日跳过 daily/weekly actuals，导致周六没有自动补刷上一交易日。

已通过官方 updater 补齐：`python -m scheduler.daily_actuals_updater --start-date 2026-07-10 --end-date 2026-07-10` 写入 5 条 daily actual。复核后 `api_wind_daily` 与 `t_scheme_actuals` 对 `1Y/3Y/5Y/7Y/10Y` 均到 `2026-07-10`；`/api/metrics/t1_daily__h1__5Y?start_month=2026-07&end_month=2026-07` 已返回 `target_date=2026-07-10` 行，`actual_direction=1,is_correct=true`。

平台侧已修复周末补刷缺口：`scheduler.main.run_actuals_job()` 在非交易日不再完全跳过 daily/weekly actuals，而是把 daily/weekly 的 `end_date` 设为上一交易日，monthly actuals 仍按自然 run date 刷新。这样周五源数据若晚于 `23:45` 才到，周六 `08:30/19:00/23:45` 会自动补齐上一交易日 actual。回归测试 `tests.test_scheduler_main tests.test_production_daily_health` 通过，scheduler 已 `launchctl kickstart -k`，运行态为 `running`。

## 2026-07-09 调度自启动、启动追跑与 23:45 actual 刷新

本轮复核确认：`2026-07-09` 的日频预测不是没跑，也不是前端刷新问题。`t_scheme_predictions` 中 `predict_date=2026-07-09` 已有 16 条 `scheduled_live` 明细，覆盖 12 个 active 日频 base scheme，`feature_date=2026-07-08`，日期语义检查为 `status=ok`。`2026-07-06..2026-07-09` 每个交易日均有 16 条日频预测，近期 `status='running'` 的 run 为 0。

仍待验证的原因是 actual/source 水位：`api_wind_daily` 与 `t_scheme_actuals` 对 `1Y/3Y/5Y/7Y/10Y` 当前均只到 `2026-07-08`，尚无 `2026-07-09` 的验证 actual。BondPrediction 当天日志显示 17:45 盘后任务请求了 `2026-07-09` 的中国国债到期收益率 `1Y/5Y/10Y` 等指标，但实际仍未写入 07/09；05:05 的 `daily_lastday` 对活跃券 `TB1YWI0C/TB5YWI0C/TB7YWI0C/TB0YWI0C` 多数返回空值并保留旧数据。因此前端对 `target_date=2026-07-09` 或更晚目标日显示“待验证”是数据水位未到，不是预测缺失。

为避免后续反复出现“更新后无人启动/错过早间 cron”的生产缺口，已完成两项平台级修复：

- launchd 安装态已同步到仓库新版 `deploy/launchd/com.bond-factor-lab.scheduler.plist`，`RunAtLoad=true`、`KeepAlive=true`，运行态环境包含 `BOND_SCHEDULER_STARTUP_CATCHUP=1`、`BOND_SCHEDULER_STAGGER_MINUTES=2`、`BOND_SCHEDULER_PREDICTION_MAX_CONCURRENCY=1`。
- `scheduler.main` 新增 startup catch-up：scheduler 启动后会对当天业务 cron 已过、且 `t_scheme_runs` 尚无 `success/partial/failed/skipped` 终态记录的 active 方案自动补跑；已有终态 run 的方案跳过，避免重启后重复覆盖。
- actual 刷新从 `08:30/19:00` 扩展为 `08:30/19:00/23:45`。新增 `23:45` 用于承接 BondPrediction `23:25` 左右的 Wind 日频导入，避免源表夜间补齐后平台 actual 仍等到次日早上才更新。

运行态验收：`launchctl print gui/$(id -u)/com.bond-factor-lab.scheduler` 显示 scheduler 为 `running`，日志确认 active 日频、周频、月频方案均已注册，并出现 `Scheduled actuals refresh at 08:30, 19:00, 23:45 Asia/Shanghai`。启动追跑已执行并对 `2026-07-09` 的 12 个 active 日频方案全部识别为已有终态 run 后跳过，没有重复写库。验证命令 `conda run --no-capture-output -n bond_factor_lab_service python -m unittest tests.test_scheduler_main tests.test_executor_run_id tests.test_production_daily_health` 通过 31/31；`scripts/check_production_daily_health.py --predict-date 2026-07-09 --strict-runs` 返回 `status=ok`、`findings=[]`；`/api/health` 返回 `{"status":"ok"}`。

## 2026-07-07 日频生产复审与最终排查汇总

2026-07-06 的直接原因已确认：生产 scheduler 当天 14:17 才启动，07:03-07:25 的日频任务超过 APScheduler 30 分钟 grace 后被判定 missed，初始 `t_scheme_runs/t_scheme_predictions` 均为 0 行。授权修复后，daily actual tail stale 已清理，`t1_daily`、`t5_daily`、`liwei_0616_10y01_cons_say_k3_div_k10`、`liwei_0616_10y02_cons_say_k3_div_k5` 已按 `scheduled_live` 补写 2026-07-06；其余 8 个 active 日频方案因源 runner/算法在 `feature_date=2026-07-03` 无有效输出而 fail-closed，未人工伪造预测。

2026-07-07 追加发现一个平台护栏缺口：`t1_daily/t5_daily` 在源水位不足时仍输出旧 `feature_date=2026-07-03`。其中 `t5_daily` 因预测表业务唯一键为 `(scheme_id,target_tenor,horizon,target_date)`，把 07/06 `run_id=568` 的 `target_date=2026-07-10` 明细覆盖成 07/07 `run_id=580`。已修复 executor：daily scheduled_live 统一校验 `predict_date`、`feature_date=previous_trading_day(predict_date)`、`target_date=feature_date+horizon trading days`，不匹配即 fail-closed 且不写库。只读巡检 `scripts/check_production_daily_health.py` 也已新增 run/prediction 明细一致性和 stale live 日期语义检查。

生产数据已受控修复：`t5_daily` 07/06 的 4 条明细已归还 `run_id=568`；07/07 stale 的 `t1_daily` 2 条明细已删除；`run_id=579/580` 已标记为 failed。当前只读健康检查结果：`2026-07-06` 为 warning（8 个 active 日频方案被源水位阻塞，无 error），`2026-07-07` 为 warning（12 个 active 日频方案全部被源水位阻塞，`predictions_count=0`，无 error）。运行中 FastAPI 指标确认 t1/t5 前端最新行回到 2026-07-06，07/07 stale 行不再进入 `daily_rows`。

剩余状态不是前端问题，也不是后端刷新问题：截至本次复审，源水位仍为 `1Y/3Y/5Y/7Y=2026-07-01`、`10Y=2026-07-03`，早于 2026-07-07 日频 expected feature date `2026-07-06`。因此 2026-07-07 没有有效日频预测、部分 07/06 target 仍待验证，均属于上游源数据未覆盖或目标日未到，不应人工补写。完整证据和修复清单见 [OPS_AUDIT_2026-07-06.md](OPS_AUDIT_2026-07-06.md)。

## 2026-07-06 周度 07/03 与全量前端复扫

用户继续复核 `2026-07-03` 目标周。深排结论分三类：

- 任务不是没启动：scheduler 日志显示 actuals 任务在 `2026-07-03 08:30/19:00` 均成功执行；`2026-07-04` 周频预测任务也自然触发。
- 前端不是缓存问题：API 是前端事实源，修复后直接访问 `/api/metrics/...` 已能看到更新结果。
- 数据源不是所有 tenor 都已覆盖：`api_wind_daily` 中 `TB0YWI0C(10Y)` 已到 `2026-07-03`，但 `TB1YWI0C/TB5YWI0C/TB7YWI0C` 只到 `2026-07-01`，因此 1Y/5Y/7Y 的 `target_date=2026-07-03` 仍待验证是正确状态。

根因一是源周历存在孤立异常：`api_wind_date` 把 `2026-07-03` 标为 `week_id=202626`，但 `2026-07-04/2026-07-05` 又回到 `202625`。旧 `weekly_actuals_updater` 逐字使用该异常周历，导致 10Y actual 只能落到 `2026-07-02`，无法匹配前端预测的 `target_date=2026-07-03`。已新增 `shared.week_calendar_normalizer.normalize_week_calendar_rows()`，只修正“交易日提前跳到下一周、随后非交易日又回到上一周”的孤立 forward jump；不改源表。`shared.calendar_service` 与 `scheduler.weekly_actuals_updater` 共享该归一化逻辑。随后通过官方 updater 执行 `python -m scheduler.weekly_actuals_updater --start-date 2026-07-03 --end-date 2026-07-03`，写入 10Y point/average actual 2 条。

根因二是该周历异常已影响 `2026-07-04` 三条周平均预测的 `feature_date`：旧行写成 `2026-07-02`，修正后应为 `2026-07-03`，`target_date` 仍为 `2026-07-10`。三条周平均方案 dry-run 均通过，已用正式 executor 重写：`weekly_avg_1y_lgbm_0529` run_id=`553`、`weekly_avg_10y_lgbm_0529` run_id=`554`、`weekly_avg_5y_lgbm_0529` run_id=`555`。当时三条 weekly point 方案均因当前周缺输出而 fail-closed，且旧周信号不得复用；这条旧处理结论已被 2026-07-14 的精细分类取代：5Y/7Y 的算法无信号转为政策平，10Y 的 `week_id=202625` 数据异常仍失败。

全量前端复扫还发现两类历史脏数据并已处理：`weekly_5y_direct_0529` 旧 run_id=`31` 有 `predict_date=2026-06-06,target_date=2026-06-05` 的反向时点异常，已加入 `scripts.delete_bad_live_predictions` 审计白名单并删除 prediction 明细（保留 run/run_log）；随后用 executor 补齐正确的 `predict_date=2026-05-30,target_date=2026-06-05`，run_id=`564`。`t1_daily` 早期 8 个灰度交易日仍沿用旧偏移语义，已按当前 `predict_date=target_date, feature_date=previous_trading_day(predict_date)` 用 executor 重写 `2026-06-01/02/03/04/05/08/09/10`，run_id=`556..563`。

最终验收：`/api/metrics/weekly_avg_1y_lgbm_0529__h6__1Y?start_month=2026-06&end_month=2026-06` 返回 4 条已验证明细，`06/26` 行为 `actual_direction=-1,is_correct=true`，月度统计 `4/4=100%`。`/api/metrics/weekly_avg_10y_lgbm_0529__h6__10Y?start_month=2026-07&end_month=2026-07` 中 `target_date=2026-07-03` 已验证，actual 为 `-1` 且预测正确；`2026-07-10` 因未来目标周仍待验证。全量 active API 扫描覆盖 25 个前端方案、466 条明细：`semantic_mismatch_count=0`、`unexpected_issue_count=0`；剩余 65 条待验证全部归类为源数据未覆盖或未来目标日。回归测试 `conda run -n bond_factor_lab_service python -m unittest tests.test_calendar_service tests.test_weekly_actuals tests.test_weekly_metrics tests.test_backend_serving tests.test_frontend_factor_lab tests.test_delete_backtest_runs tests.test_prediction_context tests.test_scheduler_main` 通过，84/84 OK；`git diff --check` 通过。backend/scheduler 已 `launchctl kickstart -k`，当前 backend PID=`76305`、scheduler PID=`76308`，scheduler 日志确认 active 方案和 actuals 任务已重新注册。入口 README、docs 索引、系统/代码架构、预测语义、方案契约、harness 和 SOP 已同步本次 07/03 周度复审结论。

## 2026-07-06 周度 06/26 待验证修复

用户复核前端 `2026-06` 周平均验证表时发现 `weekly_avg_1y_lgbm_0529__h6__1Y` 的 `predict_date=2026-06-20,target_date=2026-06-26` 仍显示待验证。排查结论：不是前端缓存，也不是 actual 缺失；`t_scheme_weekly_actuals` 已有该目标周 actual，周平均规则下实际方向为 `-1`。根因是后端 `/api/metrics` 对周频 actual 的 JOIN 同时要求 `wa.predict_date = p.predict_date`，而 2026-06-19 是非交易日、源周历在该周把 feature week 的 `week_predict_date` 写为 `2026-06-19`，预测表则按业务周六调度写 `predict_date=2026-06-20`。两边 `target_date=2026-06-26`、`target_rule=next_week_average_yield_vs_current_week_average_yield`、`tenor=1Y` 均一致，但因审计字段 `predict_date` 不一致导致未匹配。

已修复：`backend.services.scheme_metrics()` 的周频 actual join 改为按事实键 `target_tenor + target_date + target_rule` 匹配，不再要求 `predict_date` 相等。新增回归测试 `tests.test_weekly_metrics.WeeklyMetricsTests.test_scheme_metrics_matches_weekly_actual_when_holiday_week_predict_date_differs` 覆盖节假日周调度日与 actual 审计 predict_date 不一致的场景。后端重启后，`/api/metrics/weekly_avg_1y_lgbm_0529__h6__1Y?start_month=2026-06&end_month=2026-06` 返回 4 条已验证明细，06/26 行为 `predicted_direction=-1, actual_direction=-1, is_correct=true`，2026-06 月度统计更新为 `4/4=100%`。

## 2026-07-05 生产水位复审与运维修复

本轮按运维口径复审所有 active 方案的最新水位、调度进程、actual 水位、API 与前端月度展示。结论：平台链路缺陷已修复，近期日频 active 方案连续性已补齐；仍存在的未验证行来自上游数据或源算法信号水位，不得误判为前端刷新失败。

已修复问题：

- 调度器曾运行旧进程，未加载部分 0629 日频方案。已 `launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler`，当前 launchd `com.bond-factor-lab.scheduler` 运行中，PID=`52590`；日志确认 active 日频、周频、周平均与月频方案全部注册。
- 0629 日频三方案缺 `2026-07-02/2026-07-03` 实盘点，已通过正式 executor 补齐，run_id=`538..543`。当前日频 T+1/T+5 active 方案最新 `predict_date=2026-07-03`、`feature_date=2026-07-02`。
- `liwei_0616_10y02_cons_say_k3_div_k5` 原因不是缺输入，而是全局 600 秒执行 timeout；该方案单次实测约 2266-2306 秒。已新增方案级 `schedule.timeout_sec=3600`，并通过正式 executor 补齐旧缺口：`2026-06-26` run_id=`548`、`2026-06-29` run_id=`549`、`2026-06-30` run_id=`550`、`2026-07-01` run_id=`551`、`2026-07-02` run_id=`552`，另有最新点 `2026-07-03` run_id=`547`。当前 10Y02 `/api/metrics` 返回 `daily_rows=28`，`scheduled_live` 覆盖 `predict_date=2026-06-23..2026-07-03` 共 9 行。
- 周频 live context 在源周历提前切周且下一周尚未进入 DB 日历时会 fail；已在 `shared.prediction_context.build_weekly_live_context()` 增加受限 fallback：若上一交易日所在源周无下一周，且触发日所在源周已有完整上一交易日，则回退到触发日源周对应的完整输入周，不移动 source-backed core 算法。
- 前端 live 方案 `latestRun` 曾固定为 `"--"`，导致即使 API 已有新行也不展示最新运行。已改为从 `/api/metrics` 的 `daily_rows` 计算最大 `predict_date`，并刷新静态资源到 `aifin-shell.js?v=20260705a` / `aifin-shell.css?v=20260705a`。

复审后仍保留的外部水位限制：

- 活跃目标日频源指标 `TB0YWI0C/TB1YWI0C/TB3YWI0C/TB5YWI0C/TB7YWI0C` 当前最大 `rdate=2026-07-02`，`t_scheme_actuals` 各 tenor 最大 `trade_date=2026-07-02`。因此 `target_date>=2026-07-03` 的 actual 暂为空是上游数据未到，不是后端或前端 bug。
- 周点值方案的规则已于 2026-07-14 更新：5Y/7Y 在 core 正常但当前 feature key 缺输出时生成带政策审计字段的平信号，旧信号仍不得复用；`2026-07-04/2026-07-11` live 缺口已通过受控授权回补。10Y 同期缺口属于 `week_id=202625/202626` 周历/输入冲突，继续 fail-closed，必须修数据后按算法真实输出重跑。
- 月频 0629 三方案当前只有 `target_date=2026-06-15` 已验证，`target_date=2026-07-15` 尚未到期。API `monthly_metrics` 只统计 `2026-06`，前端最新页显示 `2026-07` 样本数 0、准确率 `--（0/0）` 是正确语义。

验证证据：

- 近期日频 active 方案按交易日 `2026-06-23..2026-07-03` 做连续性检查，无缺口输出；10Y02 明细连续覆盖 `predict_date=2026-06-23..2026-07-03`。
- `/api/metrics/liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y` 返回 `daily_rows=28`，尾部 `predict_date=2026-06-26..2026-07-03` 均存在，`target_date=2026-07-03` 及以后因 actual 水位暂为空。
- `/api/metrics/monthly_1y_rf_top30_0629__h30__1Y` 返回 2 条 gray_live 明细；`monthly_metrics` 只有 `2026-06` 一行，`2026-07-15` 仍为待验证。
- 浏览器前端 `http://127.0.0.1:8100/` DOM 验收确认任务格子为“回测+实盘”口径；月度 1Y/5Y/10Y 最新页均显示 `2026-06` 已验证、`2026-07` 待验证。
- 回归测试 `conda run -n bond_factor_lab_service python -m unittest tests.test_config_schema.ConfigSchemaScheduleTests tests.test_executor_run_id tests.test_scheduler_main tests.test_prediction_context tests.test_frontend_factor_lab tests.test_frontend_static_cache tests.test_liwei_0616_10y02_cons_say_k3_div_k5.Liwei061610Y02ConfigTests` 通过，59/59 OK。
- 活文档已同步本轮复审结论：入口 README、系统/代码架构、预测语义、方案契约、harness、部署 README、T0/SOP/入库后测试 SOP 均已补充 scheduler 重载、方案级 timeout、actual 水位、周频 fail-closed 与前端静态缓存验收规则；历史计划和 source evidence 归档保持原样。

## 2026-07-01 日度 0629 三方案 SOP 收口

本轮补齐 `daily_1y_xgb_1y13_0629`、`daily_5y_lgbm_5y10_0629`、`daily_10y_lgbm_10y04_0629` 的 SOP 尾段。算法仍使用 0629 source-original encrypted daily binary runner；平台只修正 historical output 起点过滤、benchmark/current backtest、authorized latest backtest、gray_live 回补、live 顶层 `model_version` 落库长度适配和审计文档，不修改原始算法逻辑。

日度 T+1 语义固定为：`predict_date=target_date`，`feature_date` 为 `predict_date` 前一交易日，`target_date` 为 T+1 目标交易日。历史 backtest 只保留 `feature_date >= 2025-01-01` 且 `target_date < 2026-06-01` 的样本；灰度实盘补齐 `target_date=2026-06-01..2026-07-01` 的 22 个交易日。

| 方案 | Registry ID | latest backtest | benchmark / CompareGate | API / live 状态 |
|------|-------------|-----------------|--------------------------|-----------------|
| `daily_1y_xgb_1y13_0629` | `daily_1y_xgb_1y13_0629__h1__1Y` | run_id=`155`，337 行，`framework_db_aligned`，accuracy=`138/337=40.9%` | original/current 337/337，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；gray_live 22 行，run_id=`447..468`，覆盖 `target_date=2026-06-01..2026-07-01` |
| `daily_5y_lgbm_5y10_0629` | `daily_5y_lgbm_5y10_0629__h1__5Y` | run_id=`156`，337 行，`framework_db_aligned`，accuracy=`130/337=38.6%` | original/current 337/337，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；gray_live 22 行，run_id=`470..491`，覆盖 `target_date=2026-06-01..2026-07-01` |
| `daily_10y_lgbm_10y04_0629` | `daily_10y_lgbm_10y04_0629__h1__10Y` | run_id=`157`，337 行，`framework_db_aligned`，accuracy=`203/337=60.2%` | original/current 337/337，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；gray_live 22 行，run_id=`492..513`，覆盖 `target_date=2026-06-01..2026-07-01` |

验证证据：先补回归测试确认旧 1Y benchmark 含 `feature_date=2024-12-31` 的历史起点污染；修复 `backtests/daily_0629_reproduction.py` 后重建三方案 benchmark，最终均为 337 行。`tests.test_daily_0629_schemes` 与 `tests.test_daily_0629_source_runner` 通过；CompareGate 三套均 `direction_match_rate=1.0`、`internal_mismatch_count=0`；修复 5Y live `model_version` 长度适配后，最终 `stage=all` no-persist 全部通过，报告分别为 `reports/harness/daily_1y_xgb_1y13_0629/20260701T093840Z/onboard_report.json`、`reports/harness/daily_5y_lgbm_5y10_0629/20260701T093911Z/onboard_report.json`、`reports/harness/daily_10y_lgbm_10y04_0629/20260701T093943Z/onboard_report.json`，harness_run_id 分别为 `hr_20260701T093840Z_10f6f1b2591f`、`hr_20260701T093911Z_b28d6f4f6d81`、`hr_20260701T093943Z_a6a300cb390b`。最终回归 `tests.test_daily_0629_source_runner`、`tests.test_daily_0629_schemes`、`tests.test_benchmark_paradigm`、`tests.test_config_schema`、`tests.test_harness_static_gate`、`tests.test_backend_api` 共 80 项通过；active-only ApiGate 三套均通过。授权 backtest persist 每方案只追加 `t_backtest_runs +1` 与 `t_backtest_predictions +337`，源表、actuals、实盘表和 run_log delta 均为 0；逐行 DB latest vs `current_predictions_sample.csv` 按 `feature_date+target_date+target_tenor+horizon` 对齐，direction、confidence、label、is_correct 与 required internal fields 全部 0 diff。

gray_live 回填前对 66 个 `(scheme, predict_date)` 组合执行 DryRunGate。最终 66/66 均有通过记录；首轮 1Y/`2026-06-17` 曾因外部 `/Users/macstudio0/Documents/DataBridge/manage.py wind_backfill_worker --loop --interval 2` 在 gate 运行期间向 `api_wind_daily` 写入 1 行而 fail-closed，确认不是方案代码写库，源表稳定后已从同一日期重跑通过。授权 live 回填最终写齐 66 条 `gray_live` prediction；所有成功 prediction 行的 run 均为 `status=success`、`records_written=1`，`prediction_phase=gray_live`，`predict_date=target_date`，`feature_date` 为上一交易日。回填过程中另有两次 LiveGate 因外部 `api_wind_daily +4` fail-closed，但对应 `run_result.status=success`、`records_written=1`，并且 allowed live 表各 `+1`；DB 覆盖验收已确认这些行有效。5Y 首次 live 写入曾暴露平台适配 bug：source `model_id` 长度 83，超过 `t_scheme_predictions.model_version varchar(64)`，导致 run_id=`469` 失败且未写 prediction；已按 TDD 修复为日度 0629 live 顶层 `model_version=final_select_id`（5Y 为 `5Y10`，10Y 为 `10Y04`），完整 source model/candidate ID 保留在 `extra.source_model_id` 与 `extra.candidate_id`，不改变算法方向、置信度或 internal fields。修复后 focused DryRunGate 5Y/`2026-06-01` 通过，后续成功回填 5Y 22 行。

DB/API/前端验收：`/api/metrics/{composite_id}` 三套均返回 22 条 `daily_rows`，phase range 均为 `gray_live`、`start_target_date=2026-06-01`、`end_target_date=2026-07-01`；1Y 顶层 `model_version` 保持既有短于 64 的 source ID，5Y/10Y 分别为 `5Y10`/`10Y04`。`scripts.verify_scheduler_mount` 三套均 `status=pass`，registry/config 均为 active，cron=`3 7 * * 1-5`，scheduler 正在运行。浏览器前端 `http://127.0.0.1:8100` 验收确认任务格子最优指标显示 `1Y T+1=54.4%`、`5Y T+1=58.8%`、`10Y T+1=59.5%`；候选排行分别出现 `0629日度1Y XGB 1Y13`、`0629日度5Y LGBM 5Y10`、`0629日度10Y LGBM 10Y04`；详情页均显示 `实盘发出起点 2026-06-01 · 灰度实盘 2026-06-01 至 2026-07-01`。

剩余观察项：等待下一次真实 scheduler 自然触发成功后升级为 Production Observed。当前文档中保留两类过程异常用于审计：外部 DataBridge 源表同步导致的 fail-closed gate，以及 5Y run_id=`469` 的修复前失败 run；二者均未造成最终 prediction 缺口。

## 2026-06-29 月度 0629 三方案入库完成

用户确认 `/Users/macstudio0/Desktop/方案/0629/forecast_project/monthly_project/` 是月度三方案原始包；原始运行核心为加密 `.so`，已归档到 `source_evidence/benchmark_batches/monthly_0629/source_package/forecast_project/`，manifest 记录 source package tree hash=`2ab82437de8fe96a556b0cf492d36739442497f861e87d2a58be8cc320088e47`。平台未改原始算法逻辑，只做 source package 解封装、输入/输出/日期语义、extra、benchmark、CompareGate、落库和 API 适配。

月度预测语义固定为 **每个自然月 15 号预测一次，无论 15 号是否交易日**；若 15 号不是交易日，`predict_date` 仍保留自然 15 号，`feature_date` 和 `target_date` 分别取当前月/目标月 15 号及以前最近交易日。月度 actual 方向固定为 **目标月观测收益率 vs 当前 feature 月观测收益率**，即 `next_month_observation_yield_vs_feature_month_observation_yield`。`t_scheme_monthly_actuals` 已通过 `migrations/015_monthly_actuals.sql` 建表并刷新，当前 `1Y/5Y/10Y` 合计 422 行；`2026-06-15` 灰度预测的 `target_date=2026-07-15` 尚未到期，因此 API 明细中的 `actual_direction/is_correct` 暂为空是正常状态。

2026-06-30 修复月度三方案前端“任务格子最优指标”缺失/样本边界问题：根因先是默认 `/api/backtests/factor-lab` 只读取 `data_source=framework_db_aligned`，而首轮月度 backtest latest 仅写在 `source_original_monthly_binary_runner` 口径下；随后发现 current/display runner 只默认重放 `2026-04-15` 单点；最后按 SOP 复核并按用户确认固定边界：**凡 `target_date >= 2026-06-01` 的月度样本均属于实盘预测，应在前端虚线下方以 `gray_live` 展示，不得进入 backtest latest**。修复后 3 个 scheme 的 current/display backtest 统一追加为 `framework_db_aligned`，并按 `2025-01-15..2026-04-15` 的 16 个自然月 15 号样本重建 benchmark 与 latest backtest，末尾 target 为 `2026-05-15`；run summary、prediction extra 与 benchmark provenance 仍保留 `source_original_data_source=source_original_monthly_binary_runner` 和 source package hash。旧 source-original run_id=`140/141/142`、旧 single-sample current run_id=`143/144/145`、旧 17 行 current run_id=`146/147/148` 均不删除、不覆盖，仅作为历史审计记录保留。授权 backtest persist 只追加 `t_backtest_runs +3`、`t_backtest_predictions +48`，源表、actuals、实盘表与 run_log delta 均为 0。随后按灰度 target 起点补齐 `predict_date=2026-05-15,target_date=2026-06-15` 三条 `gray_live` 行；原有 `predict_date=2026-06-15,target_date=2026-07-15` 三条 `gray_live` 行继续保留，未来 `2026-07-15` live actual 回填前 actual 为空是正常状态。前端静态资源版本已刷新到 `aifin-shell.js?v=20260630c`，合并视图按 live 明细的 `target_date` 月份切分 backtest/live：`2026-05` 仍是回测，`2026-06` 与 `2026-07` 均在虚线下方作为灰度实盘。

| 方案 | Registry ID | latest backtest | benchmark / CompareGate | API / live 状态 |
|------|-------------|-----------------|--------------------------|-----------------|
| `monthly_1y_rf_top30_0629` | `monthly_1y_rf_top30_0629__h30__1Y` | current run_id=`149`，`framework_db_aligned`，16 行，历史 backtest accuracy=`10/16=62.5%`；旧 run_id=`140/143/146` 保留 | source-original/current 16/16，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；default factor-lab 可见 16 backtest samples；gray_live run_id=`435`(`2026-06-15` target，方向 `1`) 与 `423`(`2026-07-15` target，方向 `-1`) |
| `monthly_5y_knn_top20_0629` | `monthly_5y_knn_top20_0629__h30__5Y` | current run_id=`150`，`framework_db_aligned`，16 行，历史 backtest accuracy=`11/16=68.8%`；旧 run_id=`141/144/147` 保留 | source-original/current 16/16，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；default factor-lab 可见 16 backtest samples；gray_live run_id=`436`(`2026-06-15` target，方向 `-1`) 与 `424`(`2026-07-15` target，方向 `1`) |
| `monthly_10y_rf_top5_0629` | `monthly_10y_rf_top5_0629__h30__10Y` | current run_id=`151`，`framework_db_aligned`，16 行，历史 backtest accuracy=`9/16=56.3%`；旧 run_id=`142/145/148` 保留 | source-original/current 16/16，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；default factor-lab 可见 16 backtest samples；gray_live run_id=`437`(`2026-06-15` target，方向 `-1`) 与 `425`(`2026-07-15` target，方向 `-1`) |

验证证据：三方案 paused 版本 `stage=all` 均通过，harness_run_id 分别为 `hr_20260629T141528Z_6d83a6938104`、`hr_20260629T141729Z_421b31de4ed4`、`hr_20260629T141926Z_1a4a0028823b`。2026-06-30 自然 15 号与单点/17 样本网格先后修复后，最终严格 SOP 版本 `stage=all` no-persist 全部通过，报告目录分别为 `reports/harness/monthly_1y_rf_top30_0629/20260630T030025Z/`、`reports/harness/monthly_5y_knn_top20_0629/20260630T030025Z/`、`reports/harness/monthly_10y_rf_top5_0629/20260630T030025Z/`；授权 backtest persist 审计目录分别为 `reports/harness/monthly_1y_rf_top30_0629/20260630T030132Z/backtest_authorization/`、`reports/harness/monthly_5y_knn_top20_0629/20260630T030140Z/backtest_authorization/`、`reports/harness/monthly_10y_rf_top5_0629/20260630T030148Z/backtest_authorization/`。逐行 DB latest vs `current_predictions_sample.csv` 按 `feature_month_id+feature_date+target_month_id+target_date+target_tenor+horizon+target_rule` 对齐，direction、confidence、label、is_correct 与 required internal fields 均为 0 diff；active-only ApiGate 三套均通过且 backtest `monthly_rows=16`。灰度实盘补齐审计目录分别为 `reports/harness/monthly_1y_rf_top30_0629/20260630T031528Z/`、`reports/harness/monthly_5y_knn_top20_0629/20260630T031532Z/`、`reports/harness/monthly_10y_rf_top5_0629/20260630T031536Z/`。`/api/backtests/factor-lab` 当前返回 run_id=`149/150/151`、每方案 16 backtest samples、末尾 `target_date=2026-05-15`；`/api/metrics/{composite_id}` 每方案返回 2 条 `gray_live` 明细，覆盖 `target_date=2026-06-15` 与 `2026-07-15`。浏览器前端验收确认静态版本 `aifin-shell.js?v=20260630c`，任务格子分别显示 `1Y|monthly=58.8%`、`5Y|monthly=64.7%`、`10Y|monthly=58.8%`；1Y 详情页为 `58.8%（10/17）`，`2026-05` 仍在回测区，虚线下方有 `2026-06` gray_live 已验证行和 `2026-07` gray_live 待验证行。ActivationGate 仍为 2026-06-29 已完成状态，激活版本分别为 `f9278202690a`、`77a1c6f983a5`、`46aa34bd1ac9`，DB registry row 均为 `status=active`、`task_type=monthly`、`deployed_at=2026-06-29`、cron=`0 18 15 * *`。

2026-06-29 已按用户授权完成最近一期 `gray_live` 回填：三套方案均使用 `predict_date=2026-06-15`、`feature_date=2026-06-15`、`target_date=2026-07-15`。2026-06-30 按用户确认的 `target_date >= 2026-06-01` 实盘边界继续补齐首个灰度 target 月：三套方案追加 `predict_date=2026-05-15`、`feature_date=2026-05-15`、`target_date=2026-06-15` 的 `gray_live` 行，run_id 分别为 `435/436/437`。两轮回填前 no-write DryRunGate 均通过，所有受保护表 delta 为 0；回填时逐条 LiveGate 使用一次性 `live_write` token，每条只写 `t_scheme_runs +1`、`t_scheme_predictions +1`、`t_scheme_run_log +1`，源表、actuals、backtest 表均为 0 delta。激活后 active-only ApiGate 三套均通过，`/api/metrics/{composite_id}` 三套均返回 2 条 `gray_live` 明细与 phase range，scheduler 已 `kickstart` 并通过 `scripts.verify_scheduler_mount`，日志确认三套 active cron 均为 `0 18 15 * *`。

## 2026-06-29 周平均 0529 独立算法入库完成

用户确认 `/Users/macstudio0/Desktop/方案/0629/forecast_project/` 就是周平均原始方案，且原始周平均只包含 `1Y/5Y/10Y`，没有 `7Y`。当前有效入库方案因此改为 `weekly_avg_1y_lgbm_0529`、`weekly_avg_5y_lgbm_0529`、`weekly_avg_10y_lgbm_0529`；旧 `weekly_avg_5y_direct_0529`、`weekly_avg_7y_cross_d_overlay_0529`、`weekly_avg_10y_d_overlay_0529` 为 point-backed 错误口径历史审计，已停用为 `paused`，不得再作为周平均 strict benchmark。

原始证据归档在 `source_evidence/benchmark_batches/model_muti_0529/weekly_average_0529/`，manifest 指向归档 source package，并固定 runner 为 `weekly.run_backtest` / `weekly.run_weekly`。benchmark 由重跑 source-original 周平均包生成，不复用任何 `weekly_*` 周度单点方案的 label、Score、Model2、D-overlay 或 point runner。actual 方向仍固定为 **目标周平均收益率 vs 当前周平均收益率**，即 `next_week_average_yield_vs_current_week_average_yield`；周平均 oracle 只负责 label/actual 验证，不生成 original prediction。

| 方案 | Registry ID | latest backtest | benchmark / CompareGate | API / live 状态 |
|------|-------------|-----------------|--------------------------|-----------------|
| `weekly_avg_1y_lgbm_0529` | `weekly_avg_1y_lgbm_0529__h6__1Y` | run_id=`137`，72 行 | original/current 72/72，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；gray_live 5 行 run_id=`408/411/414/417/420` |
| `weekly_avg_5y_lgbm_0529` | `weekly_avg_5y_lgbm_0529__h6__5Y` | run_id=`138`，72 行 | original/current 72/72，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；gray_live 5 行 run_id=`409/412/415/418/421` |
| `weekly_avg_10y_lgbm_0529` | `weekly_avg_10y_lgbm_0529__h6__10Y` | run_id=`139`，72 行 | original/current 72/72，direction=1.0，internal mismatch=0，DB latest diff=0 | active，ApiGate 通过；gray_live 5 行 run_id=`410/413/416/419/422` |

source package 回测明细每个期限有 73 行，其中 `effective_week_id=202607` 在 `source_output_date=2026-02-21/2026-02-28` 重复且内容完全一致；平台 benchmark/backtest 层按同一 weekly strict key 折叠为 72 个唯一有效周，并在 summary 中记录 `source_duplicate_effective_week_count=1`、`source_duplicate_effective_week_rows_collapsed=1`。若未来同一有效周重复行的预测或内部字段不一致，当前 runner 会 fail-closed。

验证证据：三方案 paused 版本 `stage=all` 均通过，harness_run_id 分别为 `hr_20260629T105051Z_4be24633bf28`、`hr_20260629T105150Z_513d7991b13b`、`hr_20260629T105250Z_c3adef7c1a81`；随后 ActivationGate 将三套 config/registry/version 切为 active，激活版本分别为 `a6990418e39c`、`0ee7a9d223f7`、`246062333936`。授权 backtest persist 只写 `t_backtest_runs +1` 与 `t_backtest_predictions +72`（每方案各一次），源表、actuals、实盘表和 run_log delta 均为 0。激活后 active-only ApiGate 三套均通过，scheduler 已 `kickstart` 并通过 `scripts.verify_scheduler_mount`，日志确认三套 active cron 均为 `30 11 * * 6`。

2026-06-29 已按用户授权完成 `gray_live` 回填：覆盖 `predict_date=2026-05-30/2026-06-06/2026-06-13/2026-06-20/2026-06-27`，对应 `feature_date=2026-05-29..2026-06-26`、`target_date=2026-06-05..2026-07-03`。回填前 no-write DryRunGate 全部通过，所有受保护表 delta 为 0；回填时逐条 LiveGate 使用一次性 `live_write` token，15 条均通过，每条只写 `t_scheme_runs +1`、`t_scheme_predictions +1`、`t_scheme_run_log +1`，源表、actuals、backtest 表均为 0 delta。`/api/metrics/{composite_id}` 三套均返回 5 条 `gray_live`，phase range 为 `start_predict_date=2026-05-30`、`end_predict_date=2026-06-27`、`start_target_date=2026-06-05`、`end_target_date=2026-07-03`。

## 2026-06-27 Liwei Source/Live 口径裁决

本节是当前有效状态；下方 2026-06-25/26 的“尚未刷新 / 尚未修复 / 不能把 live/API 实盘段称为 source-original 闭环”等记录保留为过程审计，已被本节取代。后续讨论 10Y01、10Y02、7Y01、7Y03、5Y01 时，必须先区分两类验收口径：

- **source-original historical backtest**：复现原始算法 batch/window 的真实输出；用于和逐方案 `original_predictions_sample.csv` / 用户 10Y02 4 月 target-date benchmark 做同口径对齐。
- **live-safe / PIT 实盘行**：保持 `predict_date=T+1`、`feature_date=T`、`target_date=T+horizon`，并以 `model_source_end=feature_date` 硬截止；只能和同口径 live-safe oracle 对齐，不能拿固定 `source_end=2026-06-10` 的 source batch 内部数值直接验收逐日 live。

本轮只读复核确认：5 个 Liwei source 方案的 source-original **历史段**已与 latest backtest 对齐；DB live/scheduled_live 行的结构、版本和 source-compatible scope 已修正为当前口径，`TOTAL_BAD=0`。但不得据此说“所有 live 预测数值都和原始 batch benchmark 完全一致”：对 7Y01/7Y03/10Y01/5Y01，`original_predictions_sample.csv` 中 `target_date >= 2026-06-01` 的 8 条跨灰度样本来自固定 `source_end=2026-06-10` 的 source batch，只能作为 source evidence，不能作为逐日 live 内部数值真值。

| 方案 | latest backtest source-original 对齐 | live / scheduled_live 当前结构核验 |
|------|-------------------------------------|-----------------------------------|
| `liwei_0616_7y01_cons_say_k3_div_k10` | run_id=`125`，历史段 13 行 direction/label/correct/confidence/internal 全部 0 diff，最大浮点差 `9.8879238130678e-17` | 23 行，`bad=0`，version=`b408a012dbcc`，scope=`liwei_0616_7y01_source_compatible_context`，phase=`gray_live` 18 / `scheduled_live` 5 |
| `liwei_0616_7y03_cons_all_k3_div_k8` | run_id=`126`，历史段 13 行全部 0 diff，最大浮点差 `4.884981308350689e-15` | 23 行，`bad=0`，version=`3314ca85442c`，scope=`liwei_0616_7y03_source_compatible_context`，phase=`gray_live` 18 / `scheduled_live` 5 |
| `liwei_0616_10y01_cons_say_k3_div_k10` | run_id=`127`，历史段 13 行全部 0 diff，最大浮点差 `4.218847493575595e-15` | 23 行，`bad=0`，version=`b91bab383ca9`，scope=`liwei_0616_10y01_source_compatible_context`，phase=`gray_live` 18 / `scheduled_live` 5 |
| `liwei_0616_10y02_cons_say_k3_div_k5` | run_id=`128`，4 月 target-date benchmark 21 行全部 0 diff，最大浮点差 `2.886579864025407e-15` | 22 行，`bad=0`，version=`8c225d56a6a1`，scope=`liwei_0616_10y02_source_compatible_context`，phase=`gray_live` 19 / `scheduled_live` 3 |
| `liwei_0616_cons_sda_k3_div_k10` | run_id=`130`，历史段 13 行全部 0 diff，最大浮点差 `8.326672684688674e-17` | 23 行，`bad=0`，version=`d5dddc032775`，scope=`liwei_0616_5y01_source_compatible_context`，phase=`gray_live` 18 / `scheduled_live` 5 |

10Y02 4 月 target-date 复核结论固定为：用户新旧数据 CSV 的最终预测、实际、是否正确完全一致；平台 latest backtest run_id=`128` 已按 `target_date` 月分组、`source_end=2026-04-30` 的 source-original 口径与 21 行 benchmark 对齐。后续不得再把 10Y02 改回逐日局部 PIT 或 `latest_oos` 局部窗口去贴结果。

当前 live 修复证据：10Y02 剩余 12 条旧 live/scheduled_live 行已通过 LiveGate + scheduler executor 重建，审计报告为 `reports/harness/liwei_0616_10y02_cons_say_k3_div_k5/20260627_live_remaining_repair_retry_with_source_monitor/repair_summary.json`，`remaining_old_like_count=0`；5Y01 版本 metadata 尾行已通过 LiveGate 重建，审计报告为 `reports/harness/liwei_0616_cons_sda_k3_div_k10/20260627_live_version_metadata_repair/repair_summary.json`，`verify_ok=true`。DataBridge `watch_triggers` / `wind_backfill_worker` 已恢复运行，crontab 未被改动。

本轮文档裁决同时固定一个回答边界：若问题是“5 个方案是否都完成修复”，可回答“source-original 历史回测对齐已完成，live 行结构/版本已修到 `TOTAL_BAD=0`”；若问题是“所有预测结果是否都和原始 benchmark 完全一致”，只能回答“同口径 historical benchmark 完全一致，live 逐日内部数值不能和固定 source_end 的原始 batch 混为一谈，必须按 live-safe oracle 验收”。

2026-06-27 分支策略更新：`master` 作为生产分支和远程默认分支；验证完成的开发分支只有在用户明确确认后，才能合并或覆盖到 `master` 并推送 GitHub，agent 不得自行决定发布到 `master`。后续不再维护第二生产分支，也不再默认同步其它发布分支。

2026-06-27 二次文档规范收口：不再新增“五方案一次性对比报告”作为主证据，而是把本轮犯错点固化为入库流程约束。根规范、Source Fidelity、T0/SOP、Scheme Contract 和 Harness 文档已要求所有 source-backed 改动先做 L0/L1/L2 分级：L0 为平台外壳适配，L1 为原始 runner 明确 patch 的上下文传递，L2 为算法内部改动并默认禁止。本轮已记录为 L2 反例的误改包括：10Y02 `IC screening` 固定锚点误移、10Y01/7Y03 source 两段窗口误替换、10Y02 target-date 月分组误改为全局 `source_end`、5Y01/V31 特征/VT/score 映射风险，以及 raw source batch 与 live-safe 口径混用。后续入库不得再用补充历史事故文档替代 SOP 约束；若发现 L2，必须停止原方案入库/修复，恢复 source 口径或另立经批准的新实验方案。

> 2026-06-25 追加 source-backed 硬约束：后续新增或修复原始算法方案，必须先读 [SOURCE_ALGORITHM_FIDELITY.md](SOURCE_ALGORITHM_FIDELITY.md)。平台日期语义、PIT helper、monthly fast path 或 gray/live 适配都不得静默改变原始算法逻辑；时间起点、窗口、周/月频对齐、特征/信号、模型参数、投票/fallback 和内部 score 映射必须按 source 口径复现。若最终方向一致但内部模型数值仍有残差，只能记录为“方向一致、内部数值待归因”，不得宣称算法逻辑完全一致。下方 2026-06-21/22 历史段落中的 strict PIT 记录按当时已登记的 `platform_live_pit_variant` 理解；后续不得把该类平台变体冒充 source-original reproduction。
> 2026-06-25 liwei source 方案改为逐个闭环复核：不得再把 10Y01 / 10Y02 / 7Y03 的历史 `strict PIT` 变体直接宣称为 `source_original_reproduction`。当前 `liwei_0616_7y01_cons_say_k3_div_k10` 已完成 source-original benchmark 与 DB/API latest backtest 闭环：source latest_oos `feature_date=2026-05-06..2026-06-03`、`backtest_input_end/source_end=2026-06-10`、`batch_mode=monthly`，最终方向、label/actual、target_date、内部 `vote_score` 与 `STD/ACCWT/CROSS_5Y/DIV` 的 `vs_full` 投票分数全部 0 diff；CompareGate 返回 `internal_mismatch_count=0`、`max_internal_abs_diff=0.0`；BacktestGate no-persist 在 Phase A cache 优化后通过，授权 persist 生成 latest backtest run_id=`125`，只写 `t_backtest_runs +1` 与 `t_backtest_predictions +333`。`liwei_0616_10y02_cons_say_k3_div_k5` 已按 2026-04 target-date source-original 口径刷新 benchmark、内部 score 列与 DB latest backtest run_id=`128`：CompareGate 返回 `internal_mismatch_count=0`、`max_internal_abs_diff=0.0`；DB latest 与 4 月 benchmark 21 行方向、label、confidence 0 diff，内部 `vote_score` 与 `STD/ACCWT/V55_7Y/DIV` score/dir 最大浮点差 `2.886579864025407e-15`。`liwei_0616_10y01_cons_say_k3_div_k10` 已改回 source `latest_oos_20260616` 两段窗口：`test_ranges=(2025-05-01..2025-06-30, 2026-05-01..2026-06-10)`，21 行 final `pred/true/is_trade/is_correct` 对 source predictions.csv 全部 0 diff，内部 `vote_score` 与 `STD/ACCWT/V55_7Y/DIV` 的 `vs_full` score/dir 对 source pkl 全部 0 diff；CompareGate 返回 `internal_mismatch_count=0`、`max_internal_abs_diff=0.0`；2026-06-26 已刷新 DB latest backtest run_id=`127`，但截至当日 gray_live/scheduled_live 实盘行仍处于待 live-safe 结构刷新状态，当前状态以 2026-06-27 顶部裁决为准。`liwei_0616_7y03_cons_all_k3_div_k8` 已同样改回 source `latest_oos_20260616` 两段窗口，21 行 final 与 source predictions.csv 0 diff，内部 `vote_score` 与 `STD/DIV/ACCWT/CROSS_5Y` score/dir 对 source pkl 0 diff；CompareGate 返回 `internal_mismatch_count=0`、`max_internal_abs_diff=0.0`；截至当日 DB/API latest backtest/gray_live 仍处于待刷新状态，当前状态以 2026-06-27 顶部裁决为准。`liwei_0616_cons_sda_k3_div_k10` 已补齐 source `latest_oos_20260616` benchmark 内部列，21 行 final 与 source predictions.csv 0 diff，内部 `vote_score` 与 `STD/DIV/ACCWT` score/dir 对 source pkl 0 diff。后续实盘调度只允许增加外层 `predict_date=T+1` 调度任务和平台写库/审计适配，不得再改变原始算法计算逻辑。
> 2026-06-26 7Y03 单方案闭环更新：`liwei_0616_7y03_cons_all_k3_div_k8` 已按 source `latest_oos_20260616` 固定 `source_end/backtest_input_end=2026-06-10`、`batch_mode=monthly`、Phase A cache 执行外壳刷新 latest backtest。CompareGate 仍为 21 行 final/internal 0 diff；BacktestGate no-persist 333 行通过，授权 persist 生成 latest backtest run_id=`126`，只写 `t_backtest_runs +1` 与 `t_backtest_predictions +333`。DB latest 与 benchmark 历史段 `feature_date=2026-05-06..2026-05-22` 共 13 行全部匹配，方向/label/confidence 0 diff，内部 `vote_score` 与 `STD/DIV/ACCWT/CROSS_5Y` score/dir 仅有 1e-15 量级浮点舍入。截至 2026-06-26 当日，7Y03 灰度/实盘行仍处于待 live-safe 结构刷新状态，当前状态以 2026-06-27 顶部裁决为准。
> 2026-06-26 10Y01 单方案闭环更新：`liwei_0616_10y01_cons_say_k3_div_k10` 已按 source `latest_oos_20260616` 固定 `source_end/backtest_input_end=2026-06-10`、`batch_mode=monthly`、Phase A cache 执行外壳刷新 latest backtest。CompareGate 仍为 21 行 final/internal 0 diff；BacktestGate no-persist 333 行通过，授权 persist 生成 latest backtest run_id=`127`，只写 `t_backtest_runs +1` 与 `t_backtest_predictions +333`。DB latest 与 benchmark 历史段 `feature_date=2026-05-06..2026-05-22` 共 13 行全部匹配，方向/label/confidence 0 diff，内部 `vote_score` 与 `STD/ACCWT/V55_7Y/DIV` score/dir 仅有 1e-15 量级浮点舍入。截至 2026-06-26 当日，10Y01 灰度/实盘行仍处于待 live-safe 结构刷新状态，当前状态以 2026-06-27 顶部裁决为准。
> 2026-06-26 10Y02 单方案闭环更新：`liwei_0616_10y02_cons_say_k3_div_k5` 已按 source-original target-date 月度口径刷新 latest backtest。根因是旧 full historical monthly runner 将所有 feature dates 合为一个全局窗口并用全局 `source_end=2026-05-29`，而 source 2026-04 target-date 回测必须按 `target_date` 月份分组：`feature_date=2026-03-25..2026-04-23`、`target_date=2026-04-01..2026-04-30`、`source_end=2026-04-30`。修复后 full runner 按 target 月拆分，每组用该月最大 target_date 作为 `source_end`，Phase A cache 只缓存 LGBM 逐日输出，Phase B/C、seasonal VT、投票/fallback 仍按各 source 窗口原逻辑重算。CompareGate 21 行 final/internal 0 diff；BacktestGate no-persist 333 行通过，`diff_count=0`、protected table delta 全 0；授权 persist 生成 latest backtest run_id=`128`，只写 `t_backtest_runs +1` 与 `t_backtest_predictions +333`。DB latest 与 4 月 benchmark 21 行全部匹配，方向/label/confidence 0 diff，内部 `vote_score` 与 `STD/ACCWT/V55_7Y/DIV` score/dir 最大浮点差 `2.886579864025407e-15`。旧 run_id=`124` 和既有 gray_live/scheduled_live 行仅作为旧平台 strict/local PIT 审计历史保留；截至 2026-06-26 当日，10Y02 实盘行仍处于待 live-safe 结构刷新状态，当前状态以 2026-06-27 顶部裁决为准。
> 2026-06-26 5Y01 单方案闭环更新：`liwei_0616_cons_sda_k3_div_k10` 已按 source `latest_oos_20260616` 固定 `source_end/backtest_input_end=2026-06-10`、`batch_mode=monthly`、Phase A cache 执行外壳刷新 latest backtest。CompareGate 21 行 final/internal 0 diff；BacktestGate no-persist 333 行通过，`diff_count=0`、protected table delta 全 0；第一次授权 persist 已写入 run_id=`129`，但 gate 被实时源表 `api_wind_daily` 的 `DR007_14:00` 并发插入触发 fail-closed；第二次授权 persist 生成 clean latest backtest run_id=`130`，只写 `t_backtest_runs +1` 与 `t_backtest_predictions +333`，所有源表/实盘表/actuals/run_log delta 均为 0。DB latest 与 benchmark 历史段 `feature_date=2026-05-06..2026-05-22` 共 13 行全部匹配，方向/label/confidence 0 diff，内部 `vote_score` 与 `STD/DIV/ACCWT` score/dir 最大浮点差 `8.326672684688674e-17`。截至 2026-06-26 当日，5Y01 既有 gray_live/scheduled_live 行仍处于待 source-compatible 内部 score 结构刷新状态，当前状态以 2026-06-27 顶部裁决为准。
> 2026-06-26 live/PIT 专项审计：5 个 liwei source 方案的 source-original 历史 latest backtest 已全部闭环，但 live 段仍不能宣称全部修复。当前 `t_scheme_predictions` 中，7Y01/7Y03/10Y01/5Y01 各有 18 条 `gray_live` 与 4 条较早 `scheduled_live` 仍是旧 `*_pit_window` extra 结构，缺 `baseline_scores`，只有 `2026-06-26` 产生的 scheduled_live 行已写入 `*_source_compatible_context`；10Y02 live 共 22 条仍全部是旧 `liwei_0616_10y02_pit_window` 结构。与 source benchmark 边界 8 行对比：7Y01、7Y03、5Y01 最终方向一致但内部 score 全部不一致或缺字段；10Y01 的 8 条 gray_live 最终方向也全部不一致；10Y02 本次 4 月 target-date benchmark 全部落在历史 backtest，无 live 边界样本。进一步用 `forecast_env` 只读跑 10Y01 `predict_date=2026-05-26` 证明当前 live adapter 已按 `feature_date=2026-05-25` 硬截止输出方向 `0` 且带 `baseline_scores`，但内部值不等于 source latest_oos benchmark，因为 source benchmark 固定 `source_end=2026-06-10`。结论：PIT 逐日问题点是把原始 batch/window 的 `source_end/test_ranges/current_start/current_end` 改成了逐日 live 截止，导致 selector/streak/fallback/内部 score 状态变化；source-original backtest 必须按原始 batch 对齐，live 调度必须另用 live-safe oracle 验收，二者不能混作“完全一致”证据。
> 2026-06-26 10Y01 live 边界纠偏：已按受控 delete-and-rewrite 流程重建 `target_date=2026-06-01..2026-06-10` 的 8 条 `gray_live` 行。旧行已归档到 `reports/harness/liwei_0616_10y01_cons_say_k3_div_k10/20260626_live_safe_oracle/repair_archive/`，逐条通过 LiveGate `live_write` 重建；其中 `predict_date=2026-05-28` 首次模型执行成功但被并发 `api_wind_daily +1` 触发 fail-closed，随后删除新行并重跑取得 clean gate。最终 DB run_id=`261,262,269,264,265,266,267,268`，全部写入 `model_scope=liwei_0616_10y01_source_compatible_context`、`baseline_scores` 与 `model_source_end=feature_date`；DB 与 live-safe oracle 在方向、confidence、`vote_score`、`STD/ACCWT/V55_7Y/DIV` score/dir 上 0 diff（最大浮点差 `1.1102230246251565e-16`），方向与 source benchmark 8/8 对齐。source batch 与 live-safe oracle 的 `vote_score` 最大差仍为 `0.06873378484423387`，原因是 source benchmark 固定 `source_end=2026-06-10`，live-safe 逐日行固定 `source_end=feature_date`；此差异不得再被当成算法 core 需要修改的证据。
> 2026-06-26 10Y02 live 边界处理状态：当前代码侧 10Y02 live adapter 已能输出 `model_scope=liwei_0616_10y02_source_compatible_context`、`baseline_scores` 与 `model_source_end=feature_date`；`python -m unittest tests.test_liwei_0616_10y02_cons_say_k3_div_k5` 14 项通过，`harness onboard ... --stage all` 的 dry-run sample_record 也证明了该结构。但当前 config 计算版本 `8c225d56a6a1` 尚未登记到 `t_scheme_versions`，DB 最新 active version 仍是 `6622023dd006`，因此直接 LiveGate 会被 executor fail-closed 为 `skipped`。已尝试重建 `target_date=2026-06-01..2026-06-10` 的 8 条 gray_live，因版本未登记全部跳过；随后已从 `reports/harness/liwei_0616_10y02_cons_say_k3_div_k5/20260626_live_boundary_repair/old_gray_live_rows_20260601_20260610.json` 恢复旧行，当前 API 不缺行。当前版本补跑 full gates 时 dry-run 被外部 `api_wind_daily +2` 并发写入触发 fail-closed，harness_run_id=`hr_20260626T080227Z_4d4d4b8c991f` 未通过；因此截至 2026-06-26 当日，10Y02 live 边界仍处于版本登记阻断状态；该过程状态已在 2026-06-27 修复记录中被取代。
> 2026-06-25 10Y02 target-date 202604 专项复查更新：`/Users/macstudio0/Downloads/pred_target_202604_config2_with_signals.csv` 与 `pred_target_202604_config2_with_signals_data2.csv` 两份文件行数、预测日、目标日、最终预测、实际、是否正确均完全一致；出手 20 条、正确 15 条、准确率 75.0%，差异只在内部 `STD/ACCWT/V55_7Y` 数值。平台旧 strict/local PIT 口径的问题已定位：10Y02 source-original 不是逐日局部 PIT，也不是 `latest_oos` 的“去年同期 + 最新窗口”局部测试集，而是从 `2024-01-01` 到 `source_end` 的 single full-OOS test sequence，之后再按 current window 抽取目标 feature_date；改变该测试序列会改变 monthly ensemble top-K、signal selection、seasonal VT 和 streak 状态。按 `sample_dates=2026-03-25..2026-04-23`、`source_end=2026-04-30`、`batch_mode=monthly` 重跑修复后 runner，最终预测已与 `data2` 21/21 对齐；`STD_vs` 最大差 `0.0006387866`、`ACCWT_vs` 最大差 `0.0006780978`、`DIV_vs` 最大差 `3.333e-7`，均不改方向；`V55_7Y_vs` 对用户 `data2` 仍有最大差 `0.0144886351`、21/21 数值残差但方向不变。进一步用 `/Users/macstudio0/Desktop/models-liwei-0616/bond_predict_merged/10y/bond_common.py` 加同一 0608/data2 输入重跑 V55，结果与平台 full-OOS 完全同口径，而不是用户 `data2` CSV；`latest_oos` 局部窗口 probe 也不能复现用户 CSV。因此当前不得通过调参或修改 core 去贴 `V55_7Y` 数值；需要补充该 CSV 对应的实际生成脚本、LightGBM/依赖版本或完整输入包后，才能把“内部模型输出完全一致”闭环。
> 2026-06-25/26 10Y01 source-original 复查更新：旧平台 runner 使用单段 full-OOS，导致 2026-05-19/20 被错误出手、2026-05-26/28 被错误置平；source `latest_oos_20260616` 实际是 prior-year + latest 两段测试集，并且 `latest_start=2026-05-01`。修正 10Y01 `inference.py` 与 backtest runner 的 source window 后，`/tmp/10y01_latest_oos_source_window.json` 与 source `predictions.csv` 在 21 行 final 输出上 0 diff，与 source pkl 内部 `vs_full` score/dir 也 0 diff。2026-06-26 已用固定 `input_end=2026-06-10`、monthly source window、Phase A cache 执行外壳刷新 DB latest backtest run_id=`127`；13 条 benchmark 历史段与 DB latest 方向、label、confidence、内部 score/dir 全部对齐。灰度/实盘行仍需后续按 target_date 分流受控刷新后，才能作为 live/API source-original 证据。
> 2026-06-25/26 7Y03 source-original 复查更新：旧平台 runner 使用单段 full-OOS，导致 2026-05-18/19/21/25 被错误做多、2026-05-22 被错误出手；source `latest_oos_20260616` 实际也是 prior-year + latest 两段测试集。修正 7Y03 `inference.py` 与 backtest runner 的 source window 后，`/tmp/7y03_latest_oos_source_window.json` 与 source `predictions.csv` 在 21 行 final 输出上 0 diff，与 source pkl 内部 `vs_full` score/dir 也 0 diff。2026-06-26 已用固定 `input_end=2026-06-10`、monthly source window、Phase A cache 执行外壳刷新 DB latest backtest run_id=`126`；13 条 benchmark 历史段与 DB latest 方向、label、confidence、内部 score/dir 全部对齐。灰度/实盘行仍需后续按 target_date 分流受控刷新后，才能作为 live/API source-original 证据。
> 2026-06-23 文档已按当前 DB、代码目录、live 语义修复和 latest 回测重建状态刷新。新增方案入口统一为 [PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md) 与 [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md)；平台统一使用 `predict_date`（信号发出日）、`feature_date`（数据截止日/预测站位日）、`target_date`（验证目标日）。日频与周频月度统计、明细日期均按 `target_date` 归属；灰度实盘与正式实盘需通过 `prediction_phase=gray_live/scheduled_live` 区分。回测起点 `2025-01-01` 是输出样本起点，不是训练历史裁剪点。
> 2026-06-14 追加 registry 单表 per-tenor 语义：`t_scheme_registry` 每一行就是前端/业务定义的一个方案，唯一身份为 composite `scheme_id = {base_scheme_id}__h{horizon}__{target_tenor}`。算法目录、scheduler、`PredictionRecord` 和 backtest 表继续使用 base `scheme_id`；前端、`/api/schemes`、`/api/metrics/{scheme_id}`、`/api/predictions?scheme_id=...` 和 `/api/backtests/factor-lab` 只使用 `status='active'` 的 registry composite `scheme_id`。`/api/metrics/{base_scheme_id}?tenor=...` 已废弃且不兼容；`paused` / `archived` registry 行只用于管理或审计，不进入当前前端/业务 API，也不允许 trigger 或 scheduler 新写入该 target。
> 2026-06-15 前端任务格子契约升级为显式 `task_type`：任务格子由 `target_tenor + task_type` 定义，`task_type ∈ {T+1, T+5, weekly_point, weekly_average, monthly}`。`frequency` 和 `horizon` 仍用于输入频率、目标日计算和 actual join，但前端不再用它们推断列；API 返回缺失或非法 `task_type` 必须 fail-closed。
> 2026-06-14 指标口径已统一：预测为“平”的样本计入 `samples` / `sample_count` 和方向分布，但不进入整体准确率、上涨/下跌准确率、上涨/下跌召回率等任何指标分母；平台新增 `metric_samples` / `metric_sample_count` 表达真实指标分母，前端准确率括号展示 `correct/metric_samples`，样本数列仍展示总样本数；每日/周度验证明细中预测为“平”的行结果列展示 `-`。
> 2026-06-14 回测前端指标事实源已收敛：`/api/backtests/factor-lab` 只从 latest run 的 `t_backtest_predictions` 明细动态聚合月度指标和 summary；正常 backtest runner 不再写独立月度指标汇总，后端也不再暴露旧月度汇总读入口。latest run 缺少回测明细时接口 fail-closed。
> 2026-06-14 benchmark 对齐口径已明确（2026-06-27 更新 live 前提）：原始算法 benchmark 中的 `T/date/predict_date` 表示 source T / 预测站位日，进入平台后必须对齐数据库明细的 `feature_date`，不是对齐实盘语义下的 `predict_date`。若 benchmark 样本的 `target_date` 已进入灰度/实盘观察区，必须先判定 benchmark 与 live 是否同执行口径；同口径时才与 `t_scheme_predictions.feature_date` 对齐，否则用 live-safe oracle。仍在历史回测区间的样本与 `t_backtest_predictions.feature_date` 对齐。
> 2026-06-14 `t1_daily` / `t5_daily` 已重建严格逐方案 benchmark baseline：批次级外部来源证据归档位于 `source_evidence/benchmark_batches/model_muti_0529/`，真正供 CompareGate 使用的 original/current 文件位于 `schemes/{scheme_id}/benchmarks/`；字段固定为 `feature_date,target_date,target_tenor,horizon,direction,confidence,label,is_correct`。`t1_daily` 为 674 行、仅含 `5Y/10Y`；`t5_daily` 为 1332 行、含 `3Y/5Y/7Y/10Y`；两者已从旧的 `PASS_WITH_LEGACY_SAMPLE_LIMITATIONS` 升级为严格 `PASS`。0529 source-evidence CSV 截至 `2026-05-28`，逐方案 benchmark 通过 DB target completion 补齐 `2026-05-29` 目标验证日：T1 最后一条为 `feature_date=2026-05-28 -> target_date=2026-05-29`，T5 最后一周为 `feature_date=2026-05-18..2026-05-22 -> target_date=2026-05-25..2026-05-29`。
> 2026-06-14 `daily_5y_2_v28` 的 predict/backtest inference 已收敛到同一个共享入口。该方案属于 test-window 敏感算法，核心窗口固定为 `feature_date` 所在月月初到 `feature_date`；旧连续窗口口径写入的 `run_id=42` 灰度预测明细已删除并由 `run_id=58` 重跑修复，`feature_date=2026-05-28` 当前为 `predicted_direction=1`、`confidence=1.0`。新的 V28 回测 no-persist 对比已完成，但最新 backtest run 仍等待人工确认后再落库覆盖 canonical latest。

## 总览

当前 registry 保留二十六个 active 可调度 base 方案：

| 方案 | 频率 | Horizon | Task Type | 目标 | 状态 |
|------|------|---------|-----------|------|------|
| `t1_daily` | `daily` | 1 | `T+1` | `5Y/10Y` | `active` |
| `t5_daily` | `daily` | 5 | `T+5` | `3Y/5Y/7Y/10Y` | `active` |
| `weekly_5y_direct_0529` | `weekly` | 6 | `weekly_point` | `5Y` | `active` |
| `weekly_7y_cross_d_overlay_0529` | `weekly` | 6 | `weekly_point` | `7Y` | `active` |
| `weekly_10y_d_overlay_0529` | `weekly` | 6 | `weekly_point` | `10Y` | `active` |
| `weekly_avg_1y_lgbm_0529` | `weekly` | 6 | `weekly_average` | `1Y` | `active / source-original weekly average aligned; gray_live rows current` |
| `weekly_avg_5y_lgbm_0529` | `weekly` | 6 | `weekly_average` | `5Y` | `active / source-original weekly average aligned; gray_live rows current` |
| `weekly_avg_10y_lgbm_0529` | `weekly` | 6 | `weekly_average` | `10Y` | `active / source-original weekly average aligned; gray_live rows current` |
| `daily_5y_2_v28` | `daily` | 5 | `T+5` | `5Y` | `active` |
| `daily_7y_1_v28` | `daily` | 5 | `T+5` | `7Y` | `active` |
| `daily_1y_xgb_1y13_0629` | `daily` | 1 | `T+1` | `1Y` | `active / source-original daily aligned` |
| `daily_5y_lgbm_5y10_0629` | `daily` | 1 | `T+1` | `5Y` | `active / source-original daily aligned` |
| `daily_10y_lgbm_10y04_0629` | `daily` | 1 | `T+1` | `10Y` | `active / source-original daily aligned` |
| `liwei_0616_cons_sda_k3_div_k10` | `daily` | 5 | `T+5` | `5Y` | `active / source-original backtest aligned; live-safe rows current` |
| `liwei_0616_7y01_cons_say_k3_div_k10` | `daily` | 5 | `T+5` | `7Y` | `active / source-original backtest aligned; live-safe rows current` |
| `liwei_0616_7y03_cons_all_k3_div_k8` | `daily` | 5 | `T+5` | `7Y` | `active / source-original backtest aligned; live-safe rows current` |
| `liwei_0616_10y01_cons_say_k3_div_k10` | `daily` | 5 | `T+5` | `10Y` | `active / source-original backtest aligned; live-safe rows current` |
| `liwei_0616_10y01_full_oos_k3_div_k10` | `daily` | 5 | `T+5` | `10Y` | `active / original continuous Full-OOS; gray_live current` |
| `liwei_0616_10y02_cons_say_k3_div_k5` | `daily` | 5 | `T+5` | `10Y` | `active / source-original backtest aligned; live-safe rows current` |
| `liwei_0616_5y01_full_oos_k3_div_k10` | `daily` | 5 | `T+5` | `5Y` | `active / original continuous Full-OOS; gray_live current` |
| `liwei_0616_5y_auc_static_all_k3_div_k10` | `daily` | 5 | `T+5` | `5Y` | `active / ALL consensus gray experiment` |
| `liwei_0616_5y_auc_yearly_all_k3_div_k10` | `daily` | 5 | `T+5` | `5Y` | `active / ALL consensus gray experiment` |
| `liwei_0616_5y_ic_yearly_all_k3_div_k10` | `daily` | 5 | `T+5` | `5Y` | `active / ALL consensus gray experiment` |
| `monthly_1y_rf_top30_0629` | `monthly` | 30 | `monthly` | `1Y` | `active / source-original monthly aligned; gray_live target months current` |
| `monthly_5y_knn_top20_0629` | `monthly` | 30 | `monthly` | `5Y` | `active / source-original monthly aligned; gray_live target months current` |
| `monthly_10y_rf_top5_0629` | `monthly` | 30 | `monthly` | `10Y` | `active / source-original monthly aligned; gray_live target months current` |

2026-06-21 入库 `liwei_0616_7y01_cons_say_k3_div_k10`，前端中文名为 `liwei_0616 7Y_01 SAY共识-DIV回退`，对应外部源模型 `7Y_01_cons_SAY_k_3_DIV_K_10`，registry composite ID 为 `liwei_0616_7y01_cons_say_k3_div_k10__h5__7Y`。该方案覆盖 `7Y`、horizon=5、task_type=`T+5`，使用 `STD/ACCWT/CROSS_5Y` 三基线 `k=3` 共识，并以 `DIV_K=10` 做 streak-break fallback；`CROSS_5Y` 只作为算法结构记录，不进入 base scheme_id。已完成：方案代码、legacy 快照、source-original benchmark 四件套、单元测试、Static/Input/Unit/Dry-run/Compare、source-compatible no-persist Backtest、registry sync、最新 persisted backtest run_id=`125`、`api-readiness`、ActivationGate、active-only ApiGate、gray_live 回补。激活证据：`harness_run_id=hr_20260621T055223Z_6bab3a72f056`，ActivationGate 将 `config.yaml.status` 翻为 `active`，activated_scheme_version=`054ec7bb5d74`，DB registry row `status=active` 且 `deployed_at=2026-06-21`；后续修正 live 输入历史起点后通过 registry sync 登记 scheme_version=`b071ae4b1bc0`。灰度 live 已按 `target_date >= 2026-06-01` 补齐 predict_date `2026-05-26..2026-06-18` 共 18 条，当前有效 run_id=`128..145`，feature_date `2026-05-25..2026-06-17`，target_date `2026-06-01..2026-06-25`，`/api/metrics/liwei_0616_7y01_cons_say_k3_div_k10__h5__7Y` 返回 18 条 live rows 和对应 `phase_ranges`。2026-06-25 source-original 复查已完成第一轮闭环：`original_predictions_sample.csv` / `current_predictions_sample.csv` 已从 8 列扩展到 17 列，新增 `vote_score`、`STD/ACCWT/CROSS_5Y/DIV` 的 `*_score` 与投票 `*_dir`；`*_score` 固定为 source `vs_full`，`*_dir` 固定为 7Y01 共识实际使用的 `np.sign(vs_full)`，不得误用 source pickle baseline `final`。targeted no-persist 输出 `/tmp/bond_factor_lab_7y01_latest_oos_sample_with_internal.json` 与 source latest_oos/pkl 在 21 条样本上最终方向、label、target_date、内部 score/dir 全部 0 diff；真实 CompareGate 报告 `reports/harness/liwei_0616_7y01_cons_say_k3_div_k10/20260625T112536Z/comparison_summary.json` 返回 `internal_mismatch_count=0`、`max_internal_abs_diff=0.0`。full historical runner 采用同一 source benchmark 上下文 `--batch-mode monthly --input-end 2026-06-10 --n-workers 20 --phase-a-cache`：Phase A cache 只缓存逐 test day 的 LGBM 输出，后续 Phase B/C、seasonal VT、投票/fallback 仍按每个 source window 原逻辑重算；13 条 historical benchmark 样本与 full output 的方向、confidence、label、`vote_score`、`STD/ACCWT/CROSS_5Y/DIV` score/dir 全部 0 diff，最大浮点差 `2.220446049250313e-16`。BacktestGate no-persist 通过，`diff_count=0`，protected table delta 全 0；授权 persist 通过，授权审计路径 `reports/harness/liwei_0616_7y01_cons_say_k3_div_k10/20260625T110402Z/backtest_authorization/authorization.json`，只写 `t_backtest_runs +1` 与 `t_backtest_predictions +333`，源表、实盘表、actuals、run_log delta 均为 0。DB/API latest 已切到 run_id=`125`，关键行 `feature_date=2026-05-18,target_date=2026-05-25` 为 direction=`0`、confidence=`0.0`、`vote_score=-0.6944595854757836`、`CROSS_5Y_score=0.008396337253219598`、`CROSS_5Y_dir=1`，并在 `extra.baseline_scores` / `extra.baseline_signs` 保留完整内部输出；旧 run_id=`121` 仅作为旧口径审计历史保留。

2026-06-21 入库并完成 `liwei_0616_7y03_cons_all_k3_div_k8`，前端中文名为 `liwei_0616 7Y_03 ALL共识-DIV回退`，对应外部源模型 `7Y_03_cons_ALL_k_3_DIV_K_8`，registry composite ID 为 `liwei_0616_7y03_cons_all_k3_div_k8__h5__7Y`。该方案覆盖 `7Y`、horizon=5、task_type=`T+5`，使用 `STD/DIV/ACCWT/CROSS_5Y` 四基线 `k=3` 共识，并以 `DIV_K=8` 做 streak-break fallback。已完成：方案代码、legacy 快照、strict PIT benchmark 四件套、单元测试、Static/Input/Unit/Dry-run/Compare、BacktestGate no-persist、授权 backtest persist、api-readiness、ActivationGate、激活后 ApiGate、gray_live 回补、前端/API 逐行对齐和 scheduler 挂载。BacktestGate 采用严格 PIT 月度快路径，保留 daily strict 参考路径并以 targeted sample 证明 `daily` 与 `monthly` 在敏感日期一致；latest persisted backtest run_id=`122`，333 条明细，历史回测截断到 `target_date < 2026-06-01`，`start_date=2025-01-02`、`end_date=2026-05-22`。激活证据：`harness_run_id=hr_20260621T134836Z_097bb5fb46cb`，ActivationGate 将 `config.yaml.status` 翻为 `active`，activated_scheme_version=`968525c48b4e`，DB registry row `status=active` 且 `deployed_at=2026-06-21`。benchmark 审计注意：source latest_oos batch 的 21 行原始证据为 `15/20=75.0%`，但 source batch 使用较晚窗口；平台 canonical benchmark 必须使用 strict PIT，已将 `2026-05-19/2026-05-20/2026-05-21` 三个 source-batch 差异行修正为 strict PIT 口径，并在 summary JSON 的 `source_batch_differences` 记录差异。最终对齐结果：`original_predictions_sample.csv` 的 13 条 historical 样本与 DB latest backtest run_id=`122` 和 `/api/backtests/factor-lab?benchmark_id=liwei_0616_7y_03&data_source=framework_db_aligned` 均为 0 diff；source benchmark 中 8 条 gray/live 边界样本已与 `/api/metrics/liwei_0616_7y03_cons_all_k3_div_k8__h5__7Y` 逐行 0 diff。2026-06-21 复查发现首轮 gray_live 只补到 source latest_oos 边界 `target_date=2026-06-10`，随后按 SOP Step 10b 补齐完整灰度观察网格：当前 gray_live 共 18 条，run_id=`146..163`，predict_date `2026-05-26..2026-06-18`、feature_date `2026-05-25..2026-06-17`、target_date `2026-06-01..2026-06-25`。补齐过程中 run_id=`156` 对应 predict_date=`2026-06-09` 的 LiveGate 因外部 DataBridge 在 gate 窗口写入 `api_wind_weekly +4` 而 fail-closed，但模型执行成功且预测行已写入；剩余补齐在临时 `SIGSTOP` DataBridge PID `3814/3816` 的静止源库窗口内完成并已自动恢复，source table deltas 均为 0。2026-06-22 已观察到 scheduler 自然触发的首条 `scheduled_live` run_id=`189`，`predict_date=2026-06-22`、`feature_date=2026-06-18`、`target_date=2026-06-26`；2026-06-23 自然调度 run_id=`215` 因当日源数据缺失失败，补齐数据后受控补跑 run_id=`231` 成功写入 1 条 `scheduled_live`。当前 `/api/metrics` 返回 18 条 `gray_live` 加 2 条 `scheduled_live`，本方案已达到 **Production Observed**。

2026-06-22 入库并激活 `liwei_0616_10y01_cons_say_k3_div_k10`，前端中文名为 `liwei_0616 10Y_01 SAY共识-DIV回退`，对应外部源模型 `10Y_01_cons_SAY_k_3_DIV_K_10`，registry composite ID 为 `liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y`。该方案覆盖 `10Y`、horizon=5、task_type=`T+5`，使用 `STD/ACCWT/V55_7Y` 三基线 `k=3` 共识，并以 `DIV_K=10` 做 streak-break fallback；`V55_7Y` 只作为算法结构记录，不进入 base scheme_id。2026-06-25/26 source-original 复核确认：旧平台 strict PIT runner 使用单段 full-OOS，不能作为 source-original 证据；source `latest_oos_20260616` 必须固定 prior-year + latest 两段测试集 `test_ranges=(2025-05-01..2025-06-30, 2026-05-01..2026-06-10)`，并固定 `backtest_input_end/source_end=2026-06-10`。当前 CompareGate 21 行 final/internal 0 diff；full historical runner 采用 `--batch-mode monthly --input-end 2026-06-10 --n-workers 20 --phase-a-cache`，Phase A cache 只缓存逐 test day 的 LGBM 输出，后续 Phase B/C、seasonal VT、投票/fallback 仍按每个 source window 原逻辑重算。BacktestGate no-persist 333 行通过，`diff_count=0`、protected table delta 全 0；授权 persist 生成 latest backtest run_id=`127`，只写 `t_backtest_runs +1` 与 `t_backtest_predictions +333`。DB latest 与 benchmark 历史段 13 行 `feature_date=2026-05-06..2026-05-22` 全部匹配：方向、label、confidence 0 diff，内部 `vote_score` 与 `STD/ACCWT/V55_7Y/DIV` score/dir 最大浮点差 `4.218847493575595e-15`。旧 run_id=`123` 和既有 gray_live/scheduled_live 行仅作为旧平台口径审计历史保留；本段 live 状态为 2026-06-26 过程判断，当前状态以 2026-06-27 顶部 live-safe 表为准。

下方 2026-06-22 的 10Y02 onboarding 段落保留旧 strict/local PIT 历史审计；10Y02 source-original backtest 当前状态以 2026-06-26 run_id=`128` 更新为准。

2026-06-22 入库并完成 `liwei_0616_10y02_cons_say_k3_div_k5`，前端中文名为 `liwei_0616 10Y_02 SAY共识-DIV回退`，对应外部源模型 `10Y_02_cons_SAY_k_3_DIV_K_5`，registry composite ID 为 `liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y`。该方案覆盖 `10Y`、horizon=5、task_type=`T+5`，使用 `STD/ACCWT/V55_7Y` 三基线 `k=3` 共识，并以 `DIV_K=5` 做 streak-break fallback；`V55_7Y` 只作为算法结构记录，不进入 base scheme_id。已完成：方案代码、legacy 快照、strict PIT benchmark 四件套、单元测试、Static/Input/Unit/Dry-run/Compare、BacktestGate no-persist、授权 backtest persist、api-readiness、ActivationGate、激活后 ApiGate、gray_live 回补、前端/API 逐行对齐和 scheduler 挂载。BacktestGate 采用严格 PIT 月度快路径，保留 daily strict 参考路径并用 targeted sample 证明 `daily` 与 `monthly` 在 `2026-05-06/2026-05-13/2026-05-19/2026-05-20/2026-05-21/2026-05-22/2026-05-25/2026-05-26/2026-05-27/2026-05-28/2026-05-29/2026-06-01/2026-06-02/2026-06-03` 上逐行一致，`diff_count=0`；本次 14 日期 proof 的 daily strict 耗时 `1978.344s`，monthly fast path 耗时 `178.541s`，两者均使用 `backtest_input_end=2026-06-10` 以覆盖灰度边界 label。latest persisted backtest run_id=`124`，333 条明细，历史回测截断到 `target_date < 2026-06-01`，`start_date=2025-01-02`、`end_date=2026-05-22`，full historical summary 为 `metric_samples=259`、`correct=135`、accuracy=`52.1%`。激活证据：`harness_run_id=hr_20260622T035557Z_f80f7f315190`，ActivationGate 将 `config.yaml.status` 翻为 `active`，validation_scheme_version=`3b67a8b6cf25`，activated_scheme_version=`7f1328fecb70`，DB registry row `status=active` 且 `deployed_at=2026-06-22`。benchmark 审计注意：source latest_oos batch 的 21 行原始证据为 20 个 traded、`14/20=70.0%`、1 个 no-trade；平台 canonical benchmark 使用 strict PIT 后为 `metric_samples=19`、`correct=15`、accuracy=`78.95%`、2 个 no-trade，`source_batch_differences` 记录 9 个 source-batch 差异行。授权 backtest persist 只写 `t_backtest_runs +1` 和 `t_backtest_predictions +333`，源表、实盘表、actuals 和 run_log delta 均为 0。激活后 `python -m harness gate api --scheme-id liwei_0616_10y02_cons_say_k3_div_k5 --api-base-url http://127.0.0.1:8100` 通过，`/api/backtests/factor-lab` 展示该 composite ID，`/api/metrics/liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y` 返回 200，旧式 `/api/metrics/liwei_0616_10y02_cons_say_k3_div_k5?tenor=10Y` 返回 400。gray_live 已按 SOP Step 10b 补齐完整平台 target-date 网格，共 19 条，run_id=`190..208`，prediction_phase 全部为 `gray_live`，predict_date `2026-05-26..2026-06-22`、feature_date `2026-05-25..2026-06-18`、target_date `2026-06-01..2026-06-26`。2026-06-23 首次真实时钟自然触发的 `scheduled_live` run_id=`210` 因当日源数据缺失失败，保留 failed run 作为审计证据；补齐 2026-06-22 特征数据后，受控补跑 run_id=`233` 成功写入 1 条 `scheduled_live`，`predict_date=2026-06-23`、`feature_date=2026-06-22`、`target_date=2026-06-29`。`/api/metrics` 当前返回 `daily_rows=20`，`phase_ranges` 包含 19 条 `gray_live`（predict_date `2026-05-26..2026-06-22` / target `2026-06-01..2026-06-26`）与 1 条 `scheduled_live`（predict_date `2026-06-23` / target `2026-06-29`）。最终 strict benchmark API 对齐结果：`original_predictions_sample.csv` 的 13 条 historical 样本与 `/api/backtests/factor-lab?benchmark_id=liwei_0616_10y_02&data_source=framework_db_aligned` latest run_id=`124` 为 0 diff，8 条 gray/live 边界样本与 `/api/metrics/liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y` 为 0 diff；逐样本主键为 `feature_date+target_date+target_tenor+horizon`，direction、label/actual、confidence、is_correct 全部一致。scheduler 已重启并通过 `scripts.verify_scheduler_mount`，日志包含 `Scheduled scheme liwei_0616_10y02_cons_say_k3_div_k5 at 3 7 * * 1-5`；本方案当前达到 **Onboarding Complete**，手动补跑已补齐 `scheduled_live` 业务结果，但由于首个自然调度 run_id=`210` 失败，**Production Observed** 仍等待下一次真实时钟自然触发成功后升级。

下方 2026-06-20 的 5Y01 onboarding 段落保留旧入库与 live 观察审计；5Y01 source-original backtest 当前状态以 2026-06-26 run_id=`130` 更新为准。

2026-06-20 新增并激活日频方案 `liwei_0616_cons_sda_k3_div_k10`，前端中文名为 `liwei_0616 5Y_01 SDA共识-DIV回退`，对应外部源模型 `5Y_01_cons_SDA_k_3_DIV_K_10`。该方案覆盖 `5Y`、horizon=5、task_type=`T+5`，使用 `STD/DIV/ACCWT` 三基线 `k=3` 共识，并以 `DIV_K=10` 做 streak-break fallback。代码侧按平台 PIT 口径入库：adapter 使用 `predict_date=T+1`、`feature_date=T`、`target_date=T+5`；backtest 使用 `predict_date=feature_date=T` 并排除 `target_date >= 2026-06-01` 的灰度/实盘区。strict benchmark 四件套已放入 `schemes/liwei_0616_cons_sda_k3_div_k10/benchmarks/`；confidence 采用确定性代理值（非零预测为 `1.0`，平为 `0.0`）。benchmark 已按源 `latest_oos_20260616` 完整覆盖 21 条 `2026-05-06..2026-06-03` 样本，original/current 主键为 `feature_date+target_date+target_tenor+horizon`，方向、label、confidence 全部一致；其中 `target_date >= 2026-06-01` 的行仅作为跨灰度边界 benchmark，不进入历史回测落库。2026-06-25 复查补齐 source-internal benchmark：`original_predictions_sample.csv` / `current_predictions_sample.csv` 已扩展为 15 列，新增 `vote_score`、`STD/DIV/ACCWT` 的 `*_score` 与 `*_dir`；`*_score` 固定为 source pkl `vs_full` / 平台 baseline `vote_score`，`*_dir` 固定为 `np.sign(vs_full)`，不得误用 VT 后 baseline `final`。`/tmp/5y01_latest_oos_source_window_internal.json` 与 source latest_oos/pkl 在 21 条样本上最终方向、label、is_correct、内部 score/dir 全部 0 diff，`source_pkl_alignment.internal_scores_exact=true`；CompareGate 报告 `reports/harness/liwei_0616_cons_sda_k3_div_k10/20260625T162306Z/comparison_summary.json` 返回 `internal_mismatch_count=0`、`max_internal_abs_diff=0.0`。最新无副作用验证：UnitGate 12/12、StaticGate、InputGate、Dry-run Gate、CompareGate、BacktestGate 均通过；Dry-run 样本为 `predict_date=2026-06-11`、`feature_date=2026-06-10`、`target_date=2026-06-17`、方向 `-1`，所有受保护表 delta 为 0。BacktestGate no-persist 输出 333 条、17 个月度格、`metric_samples=292`、`correct=168`、accuracy=57.5%，相对 baseline `diff_count=0`，所有受保护表 delta 为 0。审计注意：前两次 BacktestGate 曾因外部 `/Users/macstudio0/Documents/DataBridge/manage.py wind_backfill_worker --loop --interval 2` 在 gate 运行期间向 `api_wind_daily` 写入 `2026-06-20` 源数据而 fail-closed（两轮分别触发 `api_wind_daily` delta `+91`、`+1`），确认不是方案代码写库；最终通过是在临时 `SIGSTOP` DataBridge PID `3814/3816`、gate 结束后自动 `SIGCONT` 恢复的静止源库窗口内完成。2026-06-20 已通过受控 `POST /api/admin/registry/sync` 同步 registry，并用一次性 `backtest_persist` token 授权持久化回测，BacktestGate `--persist` 通过并落库当时 latest run_id=`120`、333 条明细，只写 `t_backtest_runs +1` 和 `t_backtest_predictions +333`，`t_backtest_monthly_metrics` 与所有源表/实盘表 delta 均为 0，授权审计路径为 `reports/harness/liwei_0616_cons_sda_k3_div_k10/20260620T112709Z/backtest_authorization/authorization.json`。2026-06-20 修正 harness 生命周期死锁：`stage=all` 末段改为 pre-activation `api-readiness`，active-only `api` 保留为激活后验收。`python -m harness onboard liwei_0616_cons_sda_k3_div_k10 --predict-date 2026-06-11 --stage all --timeout-sec 3600` 已通过，harness_run_id=`hr_20260620T160644Z_d2a15067a18f`；ApiReadinessGate 证据为 registry composite `liwei_0616_cons_sda_k3_div_k10__h5__5Y` 存在、当时 latest backtest run_id=`120` 且 333 条明细、paused 时 public factor-lab/metrics 均不可见。随后 ActivationGate 使用一次性 `activate` token 通过，validation_scheme_version=`d9954b4056e8`，activated_scheme_version=`66b9a7c6f5fa`，`config.yaml.status=active`，DB registry row `status=active` 且 `deployed_at` 非空，`t_scheme_versions` 已登记 active 版本；授权审计路径为 `reports/harness/liwei_0616_cons_sda_k3_div_k10/20260620T164036Z/activation_authorization/authorization.json`。激活后 `python -m harness gate api --scheme-id liwei_0616_cons_sda_k3_div_k10 --api-base-url http://127.0.0.1:8100` 通过，`/api/backtests/factor-lab` 已展示该 composite ID，`/api/metrics/liwei_0616_cons_sda_k3_div_k10__h5__5Y` 返回 200，不再 404；已 `launchctl kickstart -k gui/$(id -u)/com.bond-factor-lab.scheduler` 重启 scheduler，日志确认 `Scheduled scheme liwei_0616_cons_sda_k3_div_k10 at 3 7 * * 1-5` 并 `Scheduler started`。2026-06-22 已观察到 scheduler 自然触发的首条 `scheduled_live` run_id=`184`，`predict_date=2026-06-22`、`feature_date=2026-06-18`、`target_date=2026-06-26`；2026-06-23 自然调度 run_id=`213` 因当日源数据缺失失败，补齐数据后受控补跑 run_id=`229` 成功写入 1 条 `scheduled_live`。本方案已达到 **Production Observed**。

2026-06-21 按 SOP Step 10b 完成 `liwei_0616_cons_sda_k3_div_k10` 灰度实盘回补：使用 LiveGate + 一次性 `live_write` token 逐日写入 `prediction_phase=gray_live`，补齐 predict_date `2026-05-26..2026-06-18` 共 18 条，feature_date `2026-05-25..2026-06-17`，target_date 覆盖 `2026-06-01..2026-06-25`，run_id=`92..109`，每次 gate 只允许 `t_scheme_runs/t_scheme_predictions/t_scheme_run_log` 增量各 +1，源表与回测表 delta=0；DataBridge worker 在 gate 期间临时 `SIGSTOP` 并已恢复。当前 `/api/metrics/liwei_0616_cons_sda_k3_div_k10__h5__5Y` 返回 18 条 `gray_live` 加 2 条 `scheduled_live`，`phase_ranges` 覆盖 gray target `2026-06-01..2026-06-25` 与 scheduled target `2026-06-26..2026-06-29`。对 `original_predictions_sample.csv` 的 21 条 source OOS 样本做最终平台对齐：13 条命中 `/api/backtests/factor-lab` 历史 backtest daily_rows，8 条命中 `/api/metrics` 灰度 live daily_rows，按 `feature_date+target_date+target_tenor+horizon` 逐样本匹配，direction、label/actual、confidence、is_correct 全部 0 diff。注意 source md 的月度表按 `feature_date`/source date 归属，前端历史与 live metrics 按 `target_date` 归属，月度行不能跨口径直接比较。

2026-06-10 新增周度方案 `weekly_5y_direct_0529`，按 [SCHEME_ONBOARDING_SOP](sop/SCHEME_ONBOARDING_SOP.md) 完整通过了 Intake → Normalize → Input/Static/Unit/Dry-run Gate → Live Gate → API 验证 → Activation 全流程。方案采用 3 规则加权投票算法（7Y-10Y 利差动量 + 5Y-10Y 利差反转 + 1Y 动量），所有 week_id↔日期 映射只读 `api_wind_date.week_id`，禁止日历公式计算。2026-06-13 删除严格 PIT 旧 run_id=`104` 后，按 source-original batch reproduction 重建 latest run_id=`109`，历史回测仍按灰度实盘起点截断到 `target_date < 2026-06-01`，monthly_rows=17，original benchmark 覆盖区间 71/71 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。

2026-06-10 新增周度方案 `weekly_7y_cross_d_overlay_0529`，覆盖 `7Y`、horizon=6、周六 11:30 调度。原始脚本归档为 `schemes/weekly_7y_cross_d_overlay_0529/core/legacy_weekly_7y_cross_d_overlay_0529.py.txt`，活跃 core 为纯 DataFrame 算法；adapter 与回测 runner 均通过 `shared.input_artifacts.build_weekly_input_artifact()` 取数，`week_id` / `target_week_id` / `target_date` 均经 `shared.calendar_service` 读取 DB 日历。源文件原始回测（`/Users/macstudio0/Desktop/weekly_7y_cross_d_overlay_0529 (1).py` + `/Users/macstudio0/Desktop/weekly_output.csv`）与入库后回测在历史 benchmark 覆盖窗口内 42 条样本逐 `feature_week_id` 对齐：方向差异 0、label 差异 0、confidence 最大差异 0、`target_date` 月度 accuracy 差异 0；这里的 confidence 为原始算法 `cross_d_prob_up` 概率输出映射到平台统一字段，不是平台另造指标。四份 benchmark 文件已落在 `schemes/weekly_7y_cross_d_overlay_0529/benchmarks/`，且 `backtest.benchmark_required=true`。2026-06-13 删除严格 PIT 旧 run_id=`105` 后，按 source-original batch reproduction 重建 latest run_id=`110`，历史回测仍按灰度实盘起点截断到 `target_date < 2026-06-01`，monthly_rows=17，original benchmark 覆盖区间 42/42 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。ActivationGate 已用一次性 `activate` token 登记当前 active 版本，scheme_version=`27ece22d45f4`，并已重启 scheduler。

2026-06-11 新增周度方案 `weekly_10y_d_overlay_0529`，覆盖 `10Y`、horizon=6、周六 11:30 调度。原始脚本归档为 `schemes/weekly_10y_d_overlay_0529/core/legacy_weekly_10y_d_overlay_0529.py.txt`，活跃 core 为纯 DataFrame 算法；adapter 与回测 runner 均通过 `shared.input_artifacts.build_weekly_input_artifact()` 取数，`week_id` / `target_week_id` / `target_date` 均经 `shared.calendar_service` 读取 DB 日历。源文件原始回测（`/Users/macstudio0/Desktop/weekly_10y_d_overlay_0529 (1).py`）与入库后回测在已有 benchmark 覆盖窗口内 45 条样本逐 `feature_week_id` 对齐：方向差异 0、`target_date` 差异 0、`label/is_correct` 差异 0、confidence 最大差异在 `1e-12` 容差内；这里的 confidence 为原始算法 `d_prob_up`（score/model2 overlay 后概率）映射到平台统一字段。四份 benchmark 文件已落在 `schemes/weekly_10y_d_overlay_0529/benchmarks/`，且 `backtest.benchmark_required=true`。`python -m harness onboard weekly_10y_d_overlay_0529 --predict-date 2026-06-06 --stage all` 已通过，CompareGate 为 `passed`；API gate 确认 `/api/backtests/factor-lab` 中 `10Y + weekly_point` 任务格子存在。2026-06-13 删除严格 PIT 旧 run_id=`106` 后，按 source-original batch reproduction 重建 latest run_id=`108`，历史回测仍按灰度实盘起点截断到 `target_date < 2026-06-01`，monthly_rows=17；2026-06 只由 gray/live metrics 展示。ActivationGate 已用一次性 `activate` token 登记当前 active 版本，已通过 registry 同步进入 `t_scheme_registry`，并已重启 scheduler。

2026-06-14 复查确认的 source 原始输出行数仍是：`weekly_5y_direct_0529` 71 条，缺 `feature_week_id=202534`；`weekly_7y_cross_d_overlay_0529` 68 条，缺 `202529/202534/202547/202608`；`weekly_10y_d_overlay_0529` 72 条。旧 `manual_flat_completion` run_id=`118/119` 已被 2026-07-14 的统一政策回测 run_id=`163/164` 取代，当前平台 latest 为 72/72/72。source-original/current benchmark 继续保持 71/68/72 的原始输出集合，平台补平行仅进入 live/backtest 明细和政策审计。

2026-06-12 完成日频 V28 方案 `daily_5y_2_v28`（对应外部 `5y_2`，因 scheme_id 规范采用平台名）入库。adapter 实盘语义为 `predict_date=signal_date=T+1`、`feature_date=T`（上一交易日），`target_date=T+5`；`anchor_date` 仅作为方案内部/审计字段保留，业务和前端统一使用 `feature_date`。回测 runner 语义为 `predict_date=feature_date=T`，并排除 `target_date >= 2026-06-01` 的灰度/实盘 target 区间。方案声明 `input_spec.auxiliary_inputs`，InputGate 会构建 daily/weekly/monthly 三类 artifact；active core 只消费原 V28 代码白名单中的 `WEEKLY_COLS` / `MONTHLY_COLS`，避免把 artifact 全量列误引入特征空间。模型更新语义保留原 V28 设计：每个预测锚点滚动重训 LGBM，训练 mask 使用 `all_idx < idx - horizon` 防泄漏；IC screening 固定在 `2024-07-01` 前；LGBM top-K 按月用此前 test-window 表现更新；信号组合按该方案 `rebal=monthly` 更新。2026-06-14 修复确认，V28 是 test-window 敏感算法，Phase C 的 monthly ensemble / signal selection 会因 test window 改变而改变输出；`predict.py`、benchmark current 生成和 backtest runner 现统一调用 `schemes.daily_5y_2_v28.inference`，核心窗口为 `feature_date` 所在月月初到 `feature_date`，不再使用连续窗口。CompareGate 基准采用外部 May 2026 patched auxiliary source，original/current 各 18 条锚点 `2026-05-06` 至 `2026-05-29`，这些锚点语义是 source T / 平台 `feature_date`，不是实盘 `predict_date`；missing/extra=0、direction_match_rate=`1.0`、max_confidence_abs_diff=`0.0`。初始 BacktestGate 曾落库 run_id=`92`；2026-06-13 按最终预测语义重建 latest 基线 run_id=`107`，`t_backtest_predictions` 333 条；2026-06-14 新口径 no-persist 已完成，候选输出仍为 333 条、17 个月度格，是否落库为新的 canonical latest 等待人工确认。ActivationGate 已用 `activate` token 将 `config.yaml.status` 从 `paused` 翻为 `active`，当前 active scheme_version=`c3d222781648`，并已重启 scheduler。

旧周度方案（`weekly_10y_d_overlay` / `weekly_5y_direct_production` / `weekly_7y_cross_d_overlay`）已于 2026-06-09 退役。

2026-06-09 已清理旧周频写库记录：`t_scheme_weekly_actuals` 整表清空，`t_scheme_registry`、`t_scheme_predictions`、`t_scheme_run_log`、`t_backtest_runs`、`t_backtest_predictions` 中旧周频 `scheme_id` 记录已删除；源表未修改。同日已修复 `scheduler/weekly_actuals_updater.py`，新生成的周频 actuals 只读 `api_wind_date.week_id` 与 `t_trade_calendar.trade_flag`，不再使用计算型周历公式。

## 架构与 Harness

强约束 harness 工程已从"文档设计"推进到"代码落地并可运行"。架构演进路线见 [CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) §10，执行阶段 S0-S8 已完成：

- `shared.calendar_service` 提供交易日历/周历查询单点入口，交易日判断只读 `t_trade_calendar.trade_flag`，`week_id_for_date` 只读 `api_wind_date.week_id`。
- `shared.input_artifacts` 是所有预测 adapter 的输入文件生成入口；日频、周频、月频底层统一由 `shared.data_service` 生成输出宽表，artifact 层负责写出输入 CSV 并读回给算法。
- `harness/` 包已实现 StaticGate、InputGate、UnitGate、DryRunGate、CompareGate、BacktestGate、ApiReadinessGate、ApiGate、LiveGate、ActivationGate，以及 contracts、table_guard、authorization、orchestrator、persistence、CLI。
- `python -m harness onboard {scheme_id} --stage all` 可串联 static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness；新增方案必须准备 benchmark 并让 CompareGate `passed`，不能把 `skipped` 当作完成证据。active-only `api`、live/activate 不在 `all` 内，必须显式运行或授权。

周频共享基础设施保留：

- `shared/calendar_service.py`：`t_trade_calendar` 交易日查询与 `api_wind_date` 周编号查询。
- `shared/input_artifacts.py`：`build_weekly_input_artifact()`。
- `shared/data_service.py`：`build_weekly_output_from_db()`。
- `scheduler/weekly_actuals_updater.py`：周频 actuals 刷新基础设施，周编号只读 `api_wind_date`，周内最后交易日只读 `t_trade_calendar`。
- 前端任务格子列支持：按 registry `task_type` 分列，当前展示 `T+1` / `T+5` / `周度(weekly_point)` / `周平均(weekly_average)` / `月度(monthly)`，scheme-agnostic；不再按 `frequency=weekly` 或 `horizon=6` 推断“周度”列。

已删除的旧周频专属内容包括方案目录、回测 runner、方案专属测试/脚本、计算型周历模块和 5Y/7Y legacy 源。

## 平台改造里程碑（P0 / P1）

控制面加固与灰度实验室数据模型两阶段改造已全部落地并通过独立验证。

### P0 — 控制面加固与安全边界（已完成）

- `harness` 作为一等模块纳入 `pyproject.toml`（`python -m harness` 可用、可 `pip install -e .`）。
- **CompareGate**：比较算法原始输出 vs 平台归档输出（缺 benchmark 时 SKIP）；已接入 `--stage all`（dry-run 与 backtest 之间）。
- **ActivationGate**：由 fail-closed 桩升级为真实激活（凭 `activate` token 把 `status: paused -> active`）。
- **StaticGate 加固**：递归扫描 `core/**/*.py`；`legacy_*` 不再是逃逸口；禁网络/子进程/pickle/写文件/跨方案 import；收紧 `predict.py` 导入白名单。
- **BacktestGate**：首次 no-persist 运行自动落地 baseline。
- **授权 token 软默认**：配置 `HARNESS_AUTH_SECRET` 时附 HMAC + TTL；未配置时退化为明文一次性确认闸（单用户本机无需配置，写库/激活仍需显式 token）。
- **backend GET 只读**：`GET /api/schemes` 不再触发 registry 写库（同步移到启动时 + 受保护的 `POST /api/admin/registry/sync`）；trigger/admin 写接口必须配置 `BOND_ADMIN_TOKEN`，未配置时 fail-closed 返回 503；CORS 由 `BOND_CORS_ORIGINS` 白名单替代通配符。
- **clean export 脚本** `scripts/export_clean_repo.sh`：基于 `git archive` 并自检产物不含 `.env`/`.git`/密钥/artifacts/reports。
- 旧周度方案在文档/产物中退役；当前 active 周度方案为 `weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529`。

### P1 — 灰度实验室数据模型（已完成，独立验证通过）

S1→S7 串行落地，新增迁移 `005_lifecycle.sql` / `006_predictions_runid_uk.sql` / `007_backtest_immutable.sql`；2026-06-12 追加 `009_backtest_latest_view.sql` 和 `010_prediction_semantics.sql`，分别统一历史回测 latest 语义与实盘预测日期/阶段字段。

- **S1 生命周期表**：新增 `t_scheme_versions`、`t_harness_runs`、`t_harness_gate_results`、`t_input_artifacts`、`t_scheme_runs`；历史迁移中存在的 serving pointer 不再作为读取依赖。
- **S2 输入产物指纹**：`InputArtifact` 增 `content_hash`/`schema_hash`/`artifact_id`/`source_watermark`，经 `scheduler.repository.upsert_input_artifact` 幂等落 `t_input_artifacts`。
- **S3 实盘预测唯一口径**：每次运行生成 `run_id` 留痕，但业务唯一键按 `(scheme_id, target_tenor, horizon, target_date)` + UPSERT 保持同一目标点唯一；`predict_date` 只记录调度发出日，`feature_date` 记录输入截止日，`prediction_phase` 区分 `gray_live/scheduled_live`。
- **S4 方案版本**：`shared.versioning` 计算 `code_hash`/`config_hash`/`manifest_hash`，落 `t_scheme_versions`。
- **S5 harness 留痕**：`harness.persistence` 把每次 run 与每个 gate 结果落 `t_harness_runs`/`t_harness_gate_results`（仅写这两表，DB 不可用时降级本地 JSON）。
- **S6 回测不可变**：每次回测 append 新 `backtest_run_id`，`v_latest_backtest_run` 只返回同一 `benchmark_id + scheme_id + data_source` 下最新的 `status='success'` run（按 `updated_at DESC, id DESC`），`start_date/end_date` 仅作为 run 属性，不参与 latest 分组。
- **S7 backend 读切换**：GET 预测直接读 `t_scheme_predictions` 并按 `target_date` 去重/分组；响应透出 `run_id`/`scheme_version`/`input_artifact_hash`/`feature_date`/`prediction_phase`/`phase_ranges` 可追溯字段，保持只读。

**三条不变量经独立核验全部守住**：core 纯净（`schemes/*/core` 未被污染）、写库单点（新增写库仅在 `scheduler.repository` / `backtests.repository` / `harness.persistence`）、GET 只读。**全套单测 249/249 通过**（服务环境 `bond_factor_lab_service`），并经真实 MySQL 验证迁移幂等、实盘语义回填、同一业务键 UPSERT、回测 append/latest view、留痕与 trace 字段。

## 已完成

- 本机 `.env` 已生成并保持 Git 忽略；服务环境和算法环境均可读取。
- MySQL `bond_db` 已创建正式平台表：
  - `t_scheme_predictions`
  - `t_scheme_actuals`
  - `t_scheme_weekly_actuals`
  - `t_scheme_registry`
  - `t_scheme_run_log`
  - `t_target_registry`
- 历史回测表已创建：
  - `t_backtest_runs`
  - `t_backtest_predictions`
  - `t_backtest_reproduction_checks`
- 服务环境已分离：
  - 算法预测：conda `forecast_env`
  - 后端/API/调度器：conda `bond_factor_lab_service`
- `t1_daily` 已完成 adapter，当前 active，预测目标为 `5Y/10Y`。
- `t5_daily` 已完成 adapter，当前 active，预测目标为 `3Y/5Y/7Y/10Y`。
- `weekly_5y_direct_0529` 已完成 adapter + harness 全流程入库，当前 active，预测目标 `5Y`，horizon=6，`task_type=weekly_point`（周度单点）。
- `weekly_7y_cross_d_overlay_0529` 已完成 adapter + harness 全流程入库，当前 active，预测目标 `7Y`，horizon=6，`task_type=weekly_point`（周度单点）；dry-run `predict_date=2026-06-06` 产出 `feature_date=2026-06-05`、`target_date=2026-06-12`，证明周六预测指向下一周最后交易日。
- `weekly_10y_d_overlay_0529` 已完成 adapter + harness 全流程入库，当前 active，预测目标 `10Y`，horizon=6，`task_type=weekly_point`（周度单点）；dry-run `predict_date=2026-06-06` 产出 `feature_date=2026-06-05`、`target_date=2026-06-12`，证明周六预测指向下一周最后交易日。
- 强约束 harness 已落地：[HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) 是方案入库总纲，[CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md) 是代码架构主蓝图，[SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 是现行设计规范。
- artifact 命名已统一：`backtests/` 只放回测代码，`source_evidence/benchmark_batches/{benchmark_id}/` 只放外部来源证据归档，不能作为 active runner 的默认输入真源；运行期输入统一由 `shared.input_artifacts` 生成并落在 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测和数据差异报告在 `backtest_artifacts/backtests/{benchmark_id}/`。
- `t_target_registry` 当前展示 `1Y/3Y/5Y/7Y/10Y` 五个国债活跃目标，排序为 `1Y` 在前；`1Y` 已从因子/审计输入升级为前端和平台业务可见目标。
- launchd 已安装并启动：
  - `com.bond-factor-lab.backend`
  - `com.bond-factor-lab.scheduler`
- 2026-06-09 周频方案删除后已重启 scheduler；`launchctl print` 显示 `com.bond-factor-lab.scheduler` 为 `running`。2026-06-11 起日频 actuals/真实方向刷新在每日 `08:30` 和 `19:00` 各触发一次；非交易日由交易日检查跳过，旧的“每日16:00”文案已废弃。
- 前端已从真实 API 读取目标注册表和回测数据；任务格子入口按 `task_type` 展示为 `T+1` / `T+5` / `周度` / `周平均` / `月度`。日频 T+N 与周度明细、月度指标均按 `target_date` 作为目标交易日归属；真实方向仍分别匹配 `t_scheme_actuals.trade_date = target_date` 与 `t_scheme_weekly_actuals.target_date = target_date`。`feature_date` 是前端和业务统一的数据截止字段，`predict_date` 是信号发出/调度运行日；同一 `target_date` 下同方案同标的同 horizon 由 UK + UPSERT 保持唯一。
- 周频 actuals 代码口径已修复并重刷入库；当前 `t_scheme_weekly_actuals` 2,927 条，按 `feature_date/target_date -> api_wind_date.week_id` 复核无错配。

## 当前数据库快照

主体快照核验时间：`2026-06-13`，数据库 `bond_db`，MySQL `8.0.45`。`t_target_registry` 的 `1Y` 增量行已于 `2026-06-15` 通过迁移 `013_add_1y_target_registry.sql` 验证。

| 项 | 当前值 |
|----|--------|
| `api_wind_date` | 6,017 行 |
| `api_wind_daily` | 2,237,901 行，日期 `2010-01-01` 到 `2026-06-11` |
| `api_wind_derivative_daily` | 938,450 行，日期 `2010-01-01` 到 `2026-06-10` |
| `api_wind_weekly` | 121,630 行，周 `200901` 到 `202622` |
| `api_wind_derivative_weekly` | 277,064 行，周 `200901` 到 `202621` |
| `api_wind_indicators_all` | 1,637 |
| `t_trade_calendar` | 6,209 |
| `t_pre_market_forecast` | 930 |
| `t_shap` | 28,147 |
| `t_scheme_predictions` | 94 |
| `t_scheme_actuals` | 13,927 |
| `t_scheme_weekly_actuals` | 2,927 |
| `t_scheme_registry` | 11（011 迁移后按 target_tenor 拆分；012 迁移后补齐 `task_type`） |
| `t_scheme_run_log` | 69 |
| `t_target_registry` | 5（`1Y` 为 2026-06-15 迁移 013 后新增） |
| `t_backtest_runs` | 48 |
| `t_backtest_predictions` | 38,512 |
| `t_backtest_reproduction_checks` | 8 |
| `bfl_probe_*` 影子表 | 0 |

actuals 覆盖：

| 期限 | 记录数 | 日期范围 |
|------|--------|----------|
| `1Y` | 2,521 | `2016-01-18` 到 `2026-06-08` |
| `3Y` | 2,522 | `2016-01-18` 到 `2026-06-09` |
| `5Y` | 2,521 | `2016-01-18` 到 `2026-06-09` |
| `7Y` | 2,522 | `2016-01-18` 到 `2026-06-09` |
| `10Y` | 3,833 | `2010-07-27` 到 `2026-06-09` |

weekly actuals 覆盖：

| 期限 | 记录数 | target_date 范围 |
|------|--------|------------------|
| `1Y` | 529 | `2016-01-29` 到 `2026-06-05` |
| `3Y` | 529 | `2016-01-29` 到 `2026-06-05` |
| `5Y` | 529 | `2016-01-29` 到 `2026-06-05` |
| `7Y` | 529 | `2016-01-29` 到 `2026-06-05` |
| `10Y` | 811 | `2010-08-06` 到 `2026-06-05` |

## 历史回测状态

历史复现结果写入独立 backtest 表，不混入 `t_scheme_predictions`。当前代码侧保留的历史回测方案如下：

| 方案 | 数据源 | 状态 | 日期范围 |
|------|--------|------|----------|
| `t1_daily` | `baseline_original_csv` | `success`，run_id=`114` | `2025-01-01` 到 `2026-05-29` |
| `t1_daily` | `framework_original_csv` | `success`，run_id=`115` | `2025-01-01` 到 `2026-05-29` |
| `t1_daily` | `framework_db_aligned` | `success`，run_id=`116` | `2025-01-01` 到 `2026-05-29` |
| `t5_daily` | `baseline_original_csv` | `success`，run_id=`111` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_original_csv` | `success`，run_id=`112` | `2025-01-01` 到 `2026-05-31` |
| `t5_daily` | `framework_db_aligned` | `success`，run_id=`113` | `2025-01-01` 到 `2026-05-31` |
| `weekly_5y_direct_0529` | `framework_db_aligned` | `success`，run_id=`163` | `2025-01-03` 到 `2026-05-22`，统一政策平补齐 72 行 |
| `weekly_7y_cross_d_overlay_0529` | `framework_db_aligned` | `success`，run_id=`164` | `2025-01-03` 到 `2026-05-22`，统一政策平补齐 72 行 |
| `weekly_10y_d_overlay_0529` | `framework_db_aligned` | `success`，run_id=`108` | `2025-01-03` 到 `2026-05-22` |
| `weekly_avg_5y_direct_0529` | `framework_db_aligned` | `superseded point-backed run`，run_id=`131` | `2025-01-03` 到 `2026-05-22` |
| `weekly_avg_7y_cross_d_overlay_0529` | `framework_db_aligned` | `superseded point-backed run`，run_id=`132` | `2025-01-03` 到 `2026-05-22` |
| `weekly_avg_10y_d_overlay_0529` | `framework_db_aligned` | `superseded point-backed run`，run_id=`133` | `2025-01-03` 到 `2026-05-22` |
| `weekly_avg_1y_lgbm_0529` | `framework_db_aligned` | `success`，run_id=`137` | `2025-01-03` 到 `2026-05-22` |
| `weekly_avg_5y_lgbm_0529` | `framework_db_aligned` | `success`，run_id=`138` | `2025-01-03` 到 `2026-05-22` |
| `weekly_avg_10y_lgbm_0529` | `framework_db_aligned` | `success`，run_id=`139` | `2025-01-03` 到 `2026-05-22` |
| `monthly_1y_rf_top30_0629` | `framework_db_aligned` | `success`，run_id=`149` | `2025-01-15` 到 `2026-04-15`，target 截止 `2026-05-15` |
| `monthly_5y_knn_top20_0629` | `framework_db_aligned` | `success`，run_id=`150` | `2025-01-15` 到 `2026-04-15`，target 截止 `2026-05-15` |
| `monthly_10y_rf_top5_0629` | `framework_db_aligned` | `success`，run_id=`151` | `2025-01-15` 到 `2026-04-15`，target 截止 `2026-05-15` |
| `daily_5y_2_v28` | `framework_db_aligned` | `success`，run_id=`107` | `2025-01-02` 到 `2026-05-22` |

已确认错误口径并受控删除的周频回测 run 包括 `weekly_5y_direct_0529` run_id=`104`、`weekly_7y_cross_d_overlay_0529` run_id=`105`、`weekly_10y_d_overlay_0529` run_id=`106`；旧的正确审计 run 继续保留，`v_latest_backtest_run` 只选当前 latest success。`weekly_5y_direct_0529` 的 source-original run_id=`109` 保留 71 条原算法输出，当前 latest run_id=`163` 以 `no_signal_to_flat_v1` 增加 `feature_week_id=202534`，共 72 条；方向指标仍为 41/71=57.7%，source benchmark 71/71 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。

`weekly_7y_cross_d_overlay_0529` 的 source-original run_id=`110` 保留 68 条原算法输出，当前 latest run_id=`164` 以 `no_signal_to_flat_v1` 增加 `feature_week_id=202529/202534/202547/202608`，共 72 条；方向指标仍为 51/68=75.0%。`/api/backtests/factor-lab` 返回 composite `scheme_id=weekly_7y_cross_d_overlay_0529__h6__7Y`。原始 benchmark 仍为 42 条，CompareGate direction_match_rate=1.0、missing/extra=0，source benchmark 42/42 matched。

`weekly_10y_d_overlay_0529` 已按 DB 周历完成历史回测落库，最新 `framework_db_aligned` run_id=`108`，`t_backtest_predictions` 72 条；整体样本 72、正确 47、accuracy=65.3%，`evaluation_filter.date_field=target_date`。`/api/backtests/factor-lab` 返回该业务方案 composite `scheme_id=weekly_10y_d_overlay_0529__h6__10Y`、`base_scheme_id=weekly_10y_d_overlay_0529`、`task_type=weekly_point`、`frequency=weekly`、`horizon=6`、`target_tenor=10Y`，历史回测月份为 2025-01 到 2026-05，2026-06 不再出现在 backtest monthly rows。该方案历史回测是批准的 source-original batch reproduction 例外：旧严格 PIT run_id=`106` 已删除；原因是 10Y D-overlay 的 Model2 固定未来分段与逐周 PIT 切片冲突，会导致 2025H1 无有效当前周信号。新 run summary 标记 `backtest_mode=original_batch_reproduction`、`backtest_point_in_time=false`，且 `original_benchmark_validation` 为 45/45 matched。实盘/灰度 adapter 仍严格使用 `feature_date`、`end_week=feature_week_id`、`as_of_date=feature_date`。

`daily_5y_2_v28` 当前 canonical latest 仍为 `framework_db_aligned` run_id=`107`，`t_backtest_predictions` 333 条；整体样本 333、正确 163、accuracy=48.9%，`evaluation_filter.date_field=target_date`。历史回测已截断到 `target_date < 2026-06-01`，predict_date 范围为 `2025-01-02` 到 `2026-05-22`，target_date 范围为 `2025-01-09` 到 `2026-05-29`；`/api/backtests/factor-lab?benchmark_id=v28_daily_5y_2&data_source=framework_db_aligned` 返回 active registry 业务方案 `scheme_id=daily_5y_2_v28__h5__5Y`、`base_scheme_id=daily_5y_2_v28`、run_id=`107`，DB 明细动态聚合↔API 月度格 17/17 一致。其中 2026-05 目标月 13 个样本、正确 10 个、accuracy=76.9%。旧 run_id=`92/93/103` 仍保留为审计历史，但不再被 `v_latest_backtest_run` 或前端 latest 查询选中。2026-06-14 用共享 inference helper 运行的 no-persist 候选回测为 333 条、17 个月度格，metric_samples=261、correct=164、accuracy=62.8%；该候选尚未落库，避免未经人工确认直接改变前端 canonical latest。

2026-06-14 已移除 `t1_daily` / `t5_daily` 的 2026-05 最后一周 target 临时排除，并重跑落库 latest 回测。最新 run_id：`t5_daily` baseline/framework-csv/framework-db 分别为 `111/112/113`，`t1_daily` baseline/framework-csv/framework-db 分别为 `114/115/116`，`weekly_5y_direct_0529` framework-db 为 `109`，`weekly_7y_cross_d_overlay_0529` framework-db 为 `110`，`weekly_10y_d_overlay_0529` framework-db 为 `108`，`daily_5y_2_v28` framework-db 为 `107`。SQL 复核所有 latest rows 均满足 `predict_date = feature_date`、`predict_date >= 2025-01-01`、`target_date < 2026-06-01`，且同一 `benchmark_id + scheme_id + data_source` 不存在多行 latest。`/api/backtests/factor-lab?data_source=framework_db_aligned` 中 `t1_daily__h1__5Y/10Y` 的 2026-05 目标月已包含 `target_date=2026-05-29`，对应 `feature_date=2026-05-28`；`t5_daily__h5__5Y` 的 2026-05 目标月已有 18 条样本，并包含 `target_date=2026-05-25..2026-05-29` 五条每日明细。

## 实盘预测状态

active 方案的 live 预测事实表当前状态：

| 方案 | 预测日期 | 记录数 | 状态 |
|------|----------|--------|------|
| `t1_daily` | `2026-05-29` 到 `2026-06-12` | 20 条（2/日） | `gray_live` run_id=`22` 到 `29`；`scheduled_live` run_id=`34/53` |
| `t5_daily` | `2026-05-26` 到 `2026-06-12` | 52 条（4/日） | `gray_live` run_id=`2/4/6/8/11/16` 到 `21`；`scheduled_live` run_id=`33/54` |
| `weekly_5y_direct_0529` | `2026-06-06` | 2 条 | `gray_live` run_id=`31/32`；旧错误 `scheduled_live` run_id=`57` 的预测明细已删除 |
| `weekly_7y_cross_d_overlay_0529` | `2026-05-30` 到 `2026-06-06` | 2 条 | `gray_live` run_id=`36/35`；旧错误 `scheduled_live` run_id=`56` 的预测明细已删除 |
| `weekly_10y_d_overlay_0529` | `2026-05-30`、`2026-06-06`（回补） | 1/次 | `gray_live`，run_id=`37/38` |
| `daily_5y_2_v28` | `2026-05-26` 到 `2026-06-12` | 14 条（1/日） | `gray_live` run_id=`39`-`41`、`43`-`51`、`58`；旧 `run_id=42` 预测明细已删除，仅保留 run/log 审计；`scheduled_live` run_id=`52` |

2026-06-13 审计确认 `weekly_5y_direct_0529` 旧 live run_id=`31` 曾出现 predict_date=2026-06-06 → target_date=2026-06-05 的反向时点异常；该记录保留为历史审计，不作为合规语义范例。当前修复分支已去掉 5Y 周频 adapter 的 stale signal fallback，后续重整 live 回补时必须按 Step 10b 以 `target_date` 覆盖为准，并满足 `predict_date=T+1`、`feature_date=T`、`target_date=T+horizon`。

2026-06-10 周度方案 `weekly_5y_direct_0529` post-onboarding SOP 已完成：原始基准按 DB 周历归一化后 503 条，改造后 framework 复现 503 条，S5 方向差异 0；当时按 `target_date` 月度口径落库的 run_id=`81` 保留为审计历史。2026-06-13 source-original batch reproduction run_id=`109` 为 71 条；当前 latest run_id=`163` 以统一政策审计口径补齐 72 条。旧严格 PIT run_id=`104` 已受控删除。scheduler 已确认注册周六 11:30 任务（`30 11 * * 6`）。

2026-06-11 按 SOP Step 10b 回补 `weekly_7y_cross_d_overlay_0529` 灰度实盘预测：先补 `execute_scheme(cfg, '2026-06-06')`，run_id=`35`，predict_date=2026-06-06 → target_date=2026-06-12，方向=1；随后发现 2026-06 周度表还缺第一条目标周，又补 `execute_scheme(cfg, '2026-05-30')`，run_id=`36`，predict_date=2026-05-30 → target_date=2026-06-05，方向=1。该经验已写入 SOP：周度回补必须按 `target_date >= 2026-06-01` 枚举目标周并反推 predict_date，不能只按 `predict_date >= 2026-06-01` 枚举。API 已返回两条 live row：06/05 已验证（actual=-1，correct=False），06/12 尚无 actual（待验证）；`t_scheme_run_log` id=`49/48` 留痕。迁移 010 回填后，run_id=`35/36` 均标识为 `prediction_phase=gray_live`。

2026-06-11 按 SOP Step 10b 回补 `weekly_10y_d_overlay_0529` 灰度实盘预测：registry 同步前两次 live gate 因 `scheme ... not found in t_scheme_registry` 跳过并留痕（run_log id=`50/51`），随后通过 `sync_scheme_registry` 同步 active 配置，再补 `2026-05-30` 与 `2026-06-06` 两个周六，run_id=`37/38`，分别写入 predict_date=2026-05-30 → target_date=2026-06-05、predict_date=2026-06-06 → target_date=2026-06-12，方向均为 -1。API 已返回两条 live row：06/05 已验证（actual=1，correct=False），06/12 尚无 actual（待验证）；011 后 `/api/backtests/factor-lab` 返回业务方案 `weekly_10y_d_overlay_0529__h6__10Y`，对应 base `weekly_10y_d_overlay_0529`、run_id=`108`。本次还修复了 10Y 周度详情出现两个 2026-06 的问题：历史回测截断为 `target_date < 2026-06-01`，灰度实盘月份只由 live metrics 展示。迁移 010 回填后，run_id=`37/38` 均标识为 `prediction_phase=gray_live`。

2026-06-11 已重刷日频源数据至 `api_wind_daily.rdate=2026-06-11`，实盘预测表中 `t1_daily` 覆盖 target_date=2026-06-01 到 2026-06-11，`t5_daily` 覆盖 target_date=2026-06-01 到 2026-06-17。当前 6 月日频明细按目标日展示；对应目标日有 actuals 时直接计结果，目标日尚无 actuals 时保持 `待验证`，不会显示为“平”。

2026-06-12 按 SOP Step 10b 回补 `daily_5y_2_v28` 灰度实盘预测：按 `target_date >= 2026-06-01` 反推 signal `predict_date`，补齐 `2026-05-26` 到 `2026-06-11` 共 13 条 gray_live row，target_date 覆盖 `2026-06-01` 到 `2026-06-17`，原始 run_id=`39` 到 `51`。2026-06-14 复查发现 `run_id=42` 的 `feature_date=2026-05-28` 明细使用了错误的连续 test window，已受控删除该预测明细并保留 run/log 审计；随后用共享月度窗口 inference 重跑为 `run_id=58`，`predict_date=2026-05-29`、`feature_date=2026-05-28`、`target_date=2026-06-04`、`prediction_phase=gray_live`、`predicted_direction=1`、`confidence=1.0`。`predict_date=2026-06-12`、run_id=`52` 是当前第一条 scheduler 自然触发的正式实盘记录，`prediction_phase=scheduled_live`，feature_date=`2026-06-11`、target_date=`2026-06-18`。011 后 `/api/metrics/daily_5y_2_v28__h5__5Y` 返回 live row；旧 `/api/metrics/daily_5y_2_v28?tenor=5Y` 不再是合法调用。

2026-06-10 已验证前端/DB 一致性：`python -m scripts.verify_frontend_db --scheme-id t5_daily --run-id 76` 检查 68 格、0 mismatch；`t1_daily --run-id 79` 检查 36 格、0 mismatch（当时 `1Y` 尚未升级为前端可见目标）；`weekly_5y_direct_0529 --run-id 80` 检查 124 格、0 mismatch。2026-06-13 对 `daily_5y_2_v28` 使用显式 `benchmark_id=v28_daily_5y_2` 验证 `/api/backtests/factor-lab` 与 DB 月度格：最终 latest run_id=`107` 为 17/17 一致、0 mismatch。同日修复默认 `/api/backtests/factor-lab` 只读取 `model_muti_0529` 的问题：未传 `benchmark_id` 时现在返回所有 benchmark 下各方案最新成功回测，因此前端可同时合并 `daily_5y_2_v28` 的 17 条回测月度行与实盘 2026-06 行；前端展示 `实盘发出起点 2026-05-26`，并通过 `phase_ranges` 显示灰度实盘区间与正式调度起点 `2026-06-12`。2026-06-12 scheduler 配置复核：`daily_5y_2_v28` / `t1_daily` / `t5_daily` 注册工作日 07:03，`weekly_5y_direct_0529` / `weekly_7y_cross_d_overlay_0529` / `weekly_10y_d_overlay_0529` 注册周六 11:30，日频 actuals 注册每日 08:30 与 19:00；已重启 `com.bond-factor-lab.scheduler`，日志确认 `Scheduled scheme daily_5y_2_v28 at 3 7 * * 1-5` 与 `Scheduled actuals refresh at 08:30 and 19:00 Asia/Shanghai`。2026-06-13 修复分支已移除前端部署日期 override，候选方案部署/灰度/正式调度展示统一依赖后端 live rows 与 `phase_ranges`。用户侧强制刷新后确认页面正确，后续遇到“静态前端已改但页面仍旧”需先提醒强制刷新/禁用缓存。

旧周频 live prediction/run_log 记录已清理。当前 scheduler 重启后会按 active config 注册全部日频、周频、周平均与月频方案；运行态还必须确认 startup catch-up 已注册、actuals 刷新为 `08:30/19:00/23:45` 三档。

## API 与安全边界

- 已只读验证：
  - `GET /api/health`
  - `GET /api/targets`
  - `GET /api/predictions?scheme_id=t5_daily__h5__5Y&limit=1`
- `GET /api/backtests/factor-lab` 已改为只读获取 active registry metadata，不再触发 registry sync，也不再从 config fallback 临时拼业务 `scheme_id`。P0 后 `GET /api/schemes` 也已去除 registry 写副作用：registry 同步改为后端启动时执行一次，外加受保护的 `POST /api/admin/registry/sync`（必须配置 `BOND_ADMIN_TOKEN`，请求需携带匹配的 `X-Admin-Token`；未配置时返回 503）。当前所有 GET 接口均为只读。
- `GET /api/predictions` 已收敛为 active registry composite `scheme_id` 入口；base scheme id、无 `scheme_id` 和 `?tenor=...` 都不是合法业务查询。
- 正式运行需要写库时，只应通过明确的调度器或运维命令写入 `t_scheme_predictions`、`t_scheme_run_log`、`t_scheme_actuals`、`t_scheme_weekly_actuals` 或 `t_backtest_*`，不要改动源数据表。

## 剩余观察项

1. 下一次 scheduler 运行后，继续确认 active 日频、周频、周平均与月频方案均按配置注册并自然触发；同时确认 launchd `RunAtLoad/KeepAlive`、`BOND_SCHEDULER_STARTUP_CATCHUP=1` 和 actuals jobs `actuals:0830` / `actuals:1900` / `actuals:2345` 均存在。
2. 后续新周频方案进入时，继续按 [SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) 流程，并证明 `week_id` 来自 `api_wind_date` 而非公式计算。
3. 2026-06-10 修复了 `backend/services.py` 中周度 metrics 的 JOIN 条件：去掉 `wa.predict_date = p.predict_date`（周度 predict_date 语义在 prediction 和 actuals 间不一致），仅按 `tenor + target_date` 匹配。
4. 2026-06-12 固化日频 T+N 与周频前端/API/回测口径：月度指标与明细均按 `target_date` 归属，真实方向按 `target_date` join；`feature_date` 是前端和业务统一的数据截止字段，`predict_date` 是信号发出/调度日；灰度实盘和正式实盘需通过 `prediction_phase=gray_live/scheduled_live` 区分；未来目标日可先显示为 `待验证`。
5. 拿到 panda_quantflow 外层仓库路径后完成菜单/路由接入并验证 iframe。
