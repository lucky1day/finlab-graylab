# 5Y / 10Y 日频 T+1 两方案 Mac3 入库设计

**日期：** 2026-08-23  
**目标方案：** `five_y_factor_rule_online_v1`、`ten_y_factor_level_ensemble_v1`

## 目标

在不复制 ECS 数据库、Registry、Harness receipt 或运行期状态的前提下，把已在 ECS 完成
Onboarding Complete 的两个 Blackbox V2 日频 T+1 方案，在 Mac3 重新完成独立、完整、可审计的
技术入库。双主机最终使用同一份由最新 `codex/develop` 精确提交构建的不可变 source archive。

## 已确认前提

- 远程最新提交为 `ee80cf502e55e4342a0b8fe5d2dc1236660343eb`，其中包含两个方案以及后续前端概览改动。
- ECS 当前 release 为 `990fd4b96fcd7cfb9fad533179c645bac723b817`；两个 exact version 分别为
  `55f0e753b18e` 和 `997af2e57ad9`，version、Registry 与 host overlay 均为 active。
- ECS 每方案最新 `all` 的 `static/input/unit/compare` 全部 passed；每方案已有 278 条回测、14 个月指标
  和一条 `2026-08-21 / 2026-08-20 / 2026-08-21` gray-live 预测。
- Mac3 当前 release 为 `59afc857c37eebb956adb200d9b1a05ed6a123f0`；本机数据库中两个身份的
  version、Registry、backtest 和 prediction 数量均为 0。

## 决策

1. 只修改 `deploy/scheme_deployment_matrix_v1.json`，把两个方案从 `aliyun-gray` 扩展到
   `mac3-production + aliyun-gray`；两份 canonical config 保持 `paused/draft`，算法交付字节不变。
2. 增加精确 deployment-scope 回归测试，保护两个方案的双目标资格。不得用方案总数断言。
3. 从最终 clean HEAD 构建一份确定性 archive，并做两次独立构建与 SHA-256/字节一致性校验。
4. 先在 ECS 预安装、严格发现并以 `expected-current=990fd4b...` 做 CAS；只重启 Backend，复验两个
   exact active overlay、数据库、Dashboard 与五个 timer。不得重跑 Gate、回测、activation 或 gap fill。
5. Mac3 使用同一 archive 预安装，先在候选 release 中验证 osx-arm64 环境、DataBridge 和严格 discovery，
   再逐方案重新执行 `all → shadow-register → persistent backtest`。
6. Mac3 以 `expected-current=59afc857...` 做 source CAS，只 kickstart Backend；随后逐方案执行
   `activate → signal-gap-fill → dashboard`。所有数据库与 lifecycle 状态只在 Mac3 本机建立。
7. 任务闭环后更新 CURRENT/TODO 的现场事实，并删除本次 `docs/superpowers` 设计与实施计划；追溯使用
   Git、Harness、run/prediction、数据库与 immutable release 记录。

## 数据与日期语义

- 两方案均为 `task_type=T+1`、`horizon=1`、日频，目标分别为 5Y 与 10Y。
- canonical backtest 从上游正式起点 `2025-07-01` 开始，target 必须止于 gray-live 边界前的
  `2026-08-20`；每方案期望 278 条结果和 14 个月度指标。
- Mac3 单日 gray gap 固定为 `predict_date=2026-08-21`、`feature_date=2026-08-20`、
  `target_date=2026-08-21`。预期方向由本机冻结 DataBridge authority 与 exact delivery 运行得出，
  再与 ECS 同口径结果 `5Y=-1`、`10Y=0` 比较。
- 写入永久 insert-only；重复计划必须变为 `present=1/actionable=0`，部分冲突必须整组失败。

## 失败边界

下列任一情况立即停止尚未发生的副作用：

- `origin/codex/develop` 在候选构建后出现新提交；
- archive 两次构建不一致，或 manifest/source tree/commit 不匹配；
- 任一主机 `current` 不等于 CAS 预期值，或 Writer 正在运行；
- ECS exact version、active overlay、Registry、既有回测/gray-live 或 timer 状态漂移；
- Mac3 候选环境、DataBridge、四 Gate、历史截止、Registry 或 Dashboard 任一失败；
- signal gap 不是唯一 actionable business key，或已有部分业务键；
- 需要修改算法、数据库 schema、installed plist/unit、调度时间、DNS/Nginx 或跨主机 authority。

## 验收标准

- 最终 Git 提交、远程分支、manifest、archive SHA-256 和双主机 `current` 可精确关联。
- ECS 晋级后两个方案仍为 exact `active/active`，既有 278 条回测与一条 gray-live 不变；五个 timer
  保持 enabled/active，daily 下一自然触发不被 kickstart。
- Mac3 两个 exact version、host overlay 和 composite Registry 均为 active；最新 `all` 四 Gate passed。
- Mac3 每方案存在 278 条 canonical backtest、14 个月度指标、一条 insert-only gray-live，并且历史与
  live target 零重叠。
- 两个 DashboardGate passed；API 和真实浏览器分别在 5Y/10Y T+1 格子展示方案、回测和 live；控制台
  无 error/warning。
- launchd drift audit `ok=true`；installed plist 和触发时间未改变，weekly/monthly/daily Writer 未被手工运行。

