# Blackbox V2 历史灰度补齐 Gate 设计

**文档状态**：`APPROVED_DESIGN`

**决策日期**：2026-07-20，`Asia/Shanghai`

## 1. 背景与目标

`1Y国债活跃 × T+5` 四个 Blackbox V2 方案的生产灰度分界已经固定为：

```text
historical backtest: target_date < 2026-06-01
gray live:           target_date >= 2026-06-01
```

四个最新持久化回测 run `178..181` 已正确收口为 333 条、17 个目标月份，最后一条 `target_date=2026-05-29`。但每个方案的正式预测表目前只有部署日 `predict_date=2026-07-20` 的一条 `gray_live`，缺少从 `target_date=2026-06-01` 起到部署时点的灰度观察序列。

普通 LiveGate 不能直接补历史日期。它要求 `DataBridge refresh_date == predict_date`；当前 DataBridge 是 `refresh_date=2026-07-20`，所以历史 `predict_date=2026-05-26` 会被正确拒绝。此次失败只产生一条失败 run 和一条失败日志，预测表零新增。

本设计新增 Blackbox V2 专用、显式授权的 `gray-backfill` Gate。它允许在方案部署后，以当前 DataBridge 快照对历史 `feature_date` 做严格 as-of 重放，并把结果写为 `gray_live`。它不改变普通 LiveGate、scheduler 或上游交付契约。

## 2. 口径声明

历史灰度补齐是“部署后的 live-safe 重放”，不是“当时已经自然运行”的证明：

- 数据来源是当前、已校验的 DataBridge generation；
- 每次 Request 的三频 cutoff 必须由该行 `feature_date` 解析；
- 算法只允许使用 `feature_date` 及以前的数据；
- 结果记录 `replay_semantics=current_snapshot_as_of_not_historical_vintage`；
- 结果记录补齐模式和实际补齐时间，不能伪称为历史调度观测；
- `prediction_phase=gray_live` 表达业务灰度区间；只有未来自然 scheduler 产生的记录才可标为 `scheduled_live`。

前端继续把 `target_date` 作为历史/实盘分界与月份归属字段，把 `predict_date` 作为信号发出日，把 `feature_date` 作为唯一数据截止日。

## 3. 方案比较与决策

采用方案 A：专用逐日 `gray-backfill` Gate。

- 新增独立授权动作 `gray_backfill_write`，不能与 `live_write` token 互换。
- 每次调用只处理一个 base scheme 和一个 `predict_date`，并精确新增一条 prediction、run、run log。
- 复用 Blackbox `predict` CLI、active exact-version 校验、最新 all-stage 绑定、运行时 sandbox、Result 校验、日期校验和原子写入路径。
- 仅在该 Gate 内允许读取当前 DataBridge 快照执行历史 as-of 重放；普通 LiveGate 仍要求快照日期与运行日一致。
- 已存在相同业务唯一键时 fail-closed，不覆盖已有 prediction。

未采用方案 B：用 Blackbox `backtest` 批量生成后直接提升为实盘。该方案虽然更快，但混用了回测和实盘执行入口，并需要新的批量 promotion 事务和 run 归属语义。

未采用方案 C：只在前端声明 6 月灰度起点、不写正式预测。该方案会造成展示阶段与数据库事实不一致，无法形成 actual、metrics 和审计闭环。

## 4. 命令与授权边界

新增命令：

```bash
python -m harness gate gray-backfill \
  --scheme-id <base_scheme_id> \
  --predict-date YYYY-MM-DD \
  --project-root /Users/macstudio0/bond-factor-lab \
  --algo-env forecast_env_blackbox_v1 \
  --timeout-sec 600 \
  --prediction-phase gray_live \
  --authorize <token>
```

授权 token 必须满足：

- `action=gray_backfill_write`；
- HMAC 签名已启用；
- TTL 不超过 900 秒；
- `issued_by` 非空；
- 绑定 exact `scheme_id + scheme_version + harness_run_id + predict_date`；
- 一次性使用；
- `harness_run_id` 必须等于该 exact version 最新通过的 all-stage run。

Gate 只支持 `runtime_type=blackbox_v2`，只接受 `prediction_phase=gray_live`，不进入 `harness onboard --stage all`，也不允许 scheduler 调用。`live_write` token 调用本 Gate、`gray_backfill_write` token 调用普通 LiveGate 均必须被拒绝。

## 5. 执行数据流

```text
gray_backfill_write token
        │
        ▼
GrayBackfillGate preflight
  ├─ active + active exact version
  ├─ latest all-stage binding
  ├─ lifecycle clear
  ├─ predict_date 是交易日且早于当前 refresh_date
  ├─ target key 尚不存在
  └─ phase 必须为 gray_live
        │
        ▼
scheduler.executor（仅本次启用 historical-as-of snapshot mode）
  ├─ 打开并完整校验 current DataBridge，不要求 refresh_date==历史 predict_date
  ├─ 按历史 predict_date 推导 feature_date/target_date
  ├─ 按 feature_date 解析 daily/weekly/monthly cutoff
  ├─ 调用原交付脚本 predict CLI
  └─ 追加 replay/backfill provenance
        │
        ▼
repository 原子事务
  ├─ t_scheme_predictions +1
  ├─ t_scheme_runs +1
  ├─ t_scheme_run_log +1
  ├─ 其它受保护表 0
  └─ precommit delta 不符合时整体回滚
```

普通 scheduler 路径不传历史 snapshot mode，仍使用 `require_fresh=True`。这样新增能力不会削弱自然实盘的 freshness 约束。

## 6. 结果字段和日期约束

每日 T+5 补齐必须满足：

- `predict_date` 是历史信号发出日；
- `feature_date` 是其上一交易日；
- `target_date` 是 `feature_date` 后第 5 个交易日；
- `prediction_phase=gray_live`；
- `target_date >= 2026-06-01`；
- `target_date` 不与 canonical latest historical backtest 重叠；
- `scheme_version` 等于 active exact version；
- `extra.feature_date` 与物理列相等；
- `extra.prediction_phase=gray_live`；
- `extra.replay_semantics=current_snapshot_as_of_not_historical_vintage`；
- `extra.backfill_mode=post_deployment_live_safe_replay`；
- `extra.backfilled_at` 是实际执行时刻；
- `extra.data_snapshot_id`、generation 和 cutoff provenance 非空。

已有 `predict_date=2026-07-20` 的部署日 `gray_live` 不重跑、不覆盖。四个方案各补 38 个缺失信号日，最终各有 39 条 `gray_live`：

```text
predict_date: 2026-05-26 .. 2026-07-20
feature_date: 2026-05-25 .. 2026-07-17
target_date:  2026-06-01 .. 2026-07-24
```

## 7. 失败和恢复

- 授权、active 状态、version、latest all-stage、快照校验、cutoff、Result、日期或表增量任一不合格，Gate fail-closed。
- token 在开始业务执行前完成校验；进入业务执行时消费，不得复用。
- 重放失败允许留下不可变失败 run/log，但 prediction 必须为零新增。
- prediction 唯一键已存在时不得 UPSERT 覆盖；预检和原子提交前增量校验都要拒绝。
- 四个方案串行执行；任一方案失败，只停止该方案和后续补齐，已通过方案不回滚。
- 不直接修改数据库、不删除旧 run、不修改上游脚本来贴结果。
- 当天不重启 scheduler；新增 Gate 不触发 scheduler catchup。

## 8. 测试策略

测试驱动实现至少覆盖：

1. CLI 能发现 `gray-backfill`，且强制 `--predict-date` 和 `gray_live`。
2. Gate 拒绝 Native V1、paused/draft、非 exact version、非最新 all-stage 和生命周期未收口状态。
3. Gate 拒绝未签名、过期、长 TTL、空 `issued_by`、错误 action、错误 scheme/version/run/date 和重放 token。
4. 普通 `live_write` 与 `gray_backfill_write` token 不能跨 Gate 使用。
5. 普通 Blackbox live 仍要求 `refresh_date==predict_date`。
6. 历史模式只在 GrayBackfillGate 显式开启，使用 current snapshot，并按历史 `feature_date` 解析 cutoff。
7. 输出三日期、phase、version、snapshot 和 replay provenance 完整。
8. 成功精确新增 prediction/run/log 各一条，其它受保护表零增量。
9. 重复 target、算法失败、非法 Result 和 precommit delta 异常时预测零写入。
10. 现有 Blackbox V2 Gate、scheduler、backend API 和前端回归全部通过。

## 9. 本批生产执行与验收

执行顺序固定为：

1. 在测试环境对首个方案、首个缺失日期做 Gate Canary。
2. 生产 Canary 使用 `one_y_t5_liq_excess_a_v1 + 2026-05-26`，核对精确 `+1/+1/+1` 和三日期。
3. 补齐该方案剩余缺失日期，再串行补齐其余三个方案；每次签发独立 token。
4. 数据库核对每个方案 333 条 canonical history、39 条 `gray_live`、历史/live target 重叠为 0、`scheduled_live=0`。
5. API 核对每个方案 gray phase range 为 `predict 2026-05-26..2026-07-20`、`target 2026-06-01..2026-07-24`。
6. 前端核对 `1Y国债活跃 × T+5` 仍只有四个选中候选，名称保持短名，历史区结束于 2026-05，灰度区从 2026-06 开始，actual 未到显示 pending，控制台错误为 0。
7. 核对 scheduler 进程未在当天重启，且没有意外 `scheduled_live`。

生产证据必须记录四个 scheme version、all-stage run、DataBridge generation/snapshot、回测 run `178..181`、补齐成功/失败 run、授权审计摘要、最终 DB/API/前端结果以及普通 LiveGate freshness 仍然有效的测试证据。

## 10. 文档范围

本次只更新：

- `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`；
- `docs/blackbox_v2/PRODUCTION_READINESS.md`；
- `docs/CURRENT_STATUS.md`；
- 本批生产记录、机器证据和必要的文档索引/守护测试。

明确不修改 `docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md`。上游算法仍只负责 Contract 1.0 两文件交付；历史灰度补齐是平台生命周期与生产写入能力。
