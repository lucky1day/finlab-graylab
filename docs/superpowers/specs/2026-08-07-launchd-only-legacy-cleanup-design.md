# Launchd-only legacy cleanup design

**状态**：`COMPLETE`（2026-08-07；用户授权的仓库清理范围已完成）

## 目标

从仓库移除已经退役且不在当前 launchd 生产链上的脚本、ledger、occurrence 和协调器
闭包，同时保留仍被 active 方案、gray-gap 补齐和 DataBridge 发布链实际消费的能力。
每个阶段独立提交并可由全量 `unittest` 验证。

## 复核结论

### A 档重新分类

初步清单中四个 reproduction runner 与 `shared/weekly_average_lgbm_predict_adapter.py`
并非零引用，不能删除：

- `backtests/daily_0529_reproduction.py` 是 `t1_daily` / `t5_daily` 的 runner，也被测试和
  benchmark rebuild 脚本引用；
- `backtests/daily_0629_reproduction.py` 是三个现存 0629 日频 wrapper 的公共实现；
- `backtests/monthly_0629_reproduction.py` 是三个现存月度 wrapper 的公共实现；
- `backtests/weekly_avg_lgbm_0529_reproduction.py` 是三个现存周平均 wrapper 的公共实现；
- weekly-average adapter 被三个 active Native 方案 import，并列在 import-rule allowlist。

可删除的是 9 个无 deploy/launchd/CI/文档/测试外部入口的脚本闭包，共 2,892 行：
`build_liwei_5y_all_k10_gray_benchmarks.py`、`postonboard_common.py`、
`prewarm_liwei_0616_phase_a_cache.py`、四个 `rebuild_*_benchmarks.py`、
`refresh_liwei_cache_spec_fingerprints.py` 和 `verify_frontend_db.py`。其中唯一内部边是
`verify_frontend_db.py → postonboard_common.py`，必须一起删除。

### C 档可达性边界

当前 natural daily/weekly/monthly writer 是 launchd 的
`scheduler.launchd_prediction_runner --cadence ...`，生产安装的 daily-predictions plist 也指向
该入口；backend 环境固定 `BOND_DAILY_COORDINATOR_MODE=legacy`。`t_schedule_occurrences` 及
相关 schedule 表为 0 行，所有 `t_scheme_runs.schedule_item_id` 均为 NULL。

但不能把文字搜索命中的所有 occurrence/epoch 立即删除：

1. signal-gap fill 正在使用 `daily_coordinator` 的排它文件锁；它必须先抽成中性公共锁。
2. signal-gap fill 和 storage preflight 正在使用 daily runtime-root 解析；它必须先抽成中性
   路径 helper。
3. DataBridge 的当前发布与 strict-read 仍使用 publication identity/continuity 校验；只删除其
   ledger/occurrence 耦合，不削弱 launchd 发布闭环。
4. `t_input_generations` 有已封存的 native-source lineage，绝不能与 schedule 表混同删除。

已安装但 `Disabled`、未加载的 `daily-gray`、`scheduler` 与 `v2-preflight` plist 仅作为审计
证据；本次不修改 installed plist、launchctl、数据库表/列/FK，也不删除历史 migration。

## 先决修复

完整基线的 1,680 个 `unittest` 中有 3 个失败。根因是 active
`cgb_causal_wk_1y_v128@59415aa789c5` 已完成 gray 入库，但代码和 JSON admission 清单仍冻结
旧 `ee921f65476c`。修复只把 exact identity 同步为当前版本，维持 `mode=gray` 与空
capabilities；它不会授予 `direct_scheduled`、`launchd_one_shot` 或任何 ledger 能力。

## 分阶段设计

1. **P0 — admission parity：** 先用更新后的精确身份测试证明旧 JSON/常量不匹配，再同步
   Python admission、JSON policy 和测试期望，恢复无写 launchd simulation。
2. **A — scripts：** 删除 9-script 封闭孤岛；保留所有仍由现役方案调用的 reproduction/adapter。
3. **C1 — survival spine：** 提取中性文件锁与 runtime path，去除 executor/cache 的 coordinator
   mode 传播，并把 DataBridge publication validation 改为 launchd-only、与 occurrence 解耦的
   identity；冻结计划 schema 必须版本化并保持既有 v4 plan 的只读审计兼容。
4. **C2 — ledger closure：** 在 C1 回归通过后删除 daily ledger/runtime/replay/capacity 闭包，
   收缩 repository 的 schedule occurrence API，删掉 ledger-only policy/config/tests；保留 normal
   run、gray-gap、registry、prediction 和 input-generation 路径。
5. **验收：** 每批执行 targeted tests、`git diff --check` 与精确 `rg` 审计；最终执行全量
   `unittest`、Blackbox profile delivery tests 和 production-entry read-only 检查。

## 不变量

- 生产调度仅保留 launchd one-shot；backend manual `direct_scheduled` 仍是独立、fail-closed 的
  控制面，不得偷改为自然调度。
- CGB 当前版本保持 gray-only、零调度能力。
- 每次输入/回补仍经 `shared.input_artifacts` 与现有 repository 单点写入。
- 不新增或扩容 ledger、occurrence、epoch 表/任务/策略；也不执行 DDL 或删除历史 migration。
- 不触碰用户未提交的状态记录、报告、`.superpowers/` 或诊断脚本。

## 实施结果

- CGB 的 exact admission identity 已同步为
  `cgb_causal_wk_1y_v128@59415aa789c5`，保持 `gray`、零 scheduler capabilities；历史信号的
  批量补齐和灰度入库仍沿用既有、受控的 repository 写入路径。
- 分阶段提交已完成：P0 `2383acb`、A `00f831a`、C1a `0aba9e4`、C1b `09dc5fb`、C1c
  `757499f`、C2 `c9b5b94`。C2 删除 ledger/occurrence/epoch runtime、policy、replay 与测试
  闭包，并保留 launchd one-shot、manual direct admission、gray-gap、DataBridge publication、
  input-generation lineage 和 migration-017 recovery 测试。
- 本次没有修改 installed plist、执行 `launchctl`、执行 DDL 或写入生产/业务数据库。历史
  migration 与现有数据库对象仍只是审计/恢复证据；物理归档或 schema 退役需要独立设计和授权。
- 最终验收已通过：文档/架构测试（107 tests）、CGB Blackbox runtime delivery test（6 tests）、
  全量服务 `unittest`（exit 0）、whitespace 检查和引用审计；结果记录在同批实施计划中。
- 最终独立审查还发现 historical `direct_scheduled` admission 与 launchd-only execution fence
  的 API 边界不一致。已将同一无副作用 execution-contract 检查前移到 trigger preflight：无
  manual 实盘阶段时返回 409、不会入队；没有放开 capability、DB 写入或 natural writer。
