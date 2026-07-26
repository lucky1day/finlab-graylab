# 10Y T+5 四方案手工入库记录

**文档状态**：`IN_PROGRESS`

**执行日期**：2026-07-26，`Asia/Shanghai`

**当前状态**：`four-schemes-persistent-backtest-accepted-gray-waiting`

本文是本批四个 10Y T+5 Blackbox V2 方案的时点记录。批次初始状态为
`authorized/manual-onboarding-pending-revalidation`，授权逐方案 revalidation →
`controlled activate` → `persistent backtest` → `manual gray_live` →
`DB/API/frontend acceptance`。当前四个方案均已完成技术复核、激活、持久化
回测和回测 API 验收；实时灰度仍因缺少合法同日 SEALED generation 而等待。

## 1. 来源与范围

来源工作树为 `blackbox-v2-10y-t5-onboarding-20260726`，最终审计提交为 `30e2edc`；四个独立 Intake 提交依次为 `0c1daa2`、`90092d6`、`49e3e13`、`30e2edc`。本记录不修改交付 Metadata，也不将本批结论外推为其他方案或通用生产授权。

| base scheme ID | source commit | scheme version | Metadata SHA-256 | Delivery SHA-256 | description 专项状态 |
|---|---|---|---|---|---|
| `ten_y_t5_maj3_k3_ic_static_v1` | `0c1daa2` | `c54b90bcafa7` | `10c41c6d3e271e76c6c03afc4e9ff3ad998ffefb92b557d5bb868d69e077329d` | `75749f165e3ce2c5cb70f86fae1336e52e693655198b05c78e45422e165471de` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` |
| `ten_y_t5_maj4_k3_ic_static_v1` | `90092d6` | `6bdabf86b4a6` | `eee777b89f0a9138dce0454044e0569f91459c28f0f5426d8a7d5c06202da005` | `64000f9a4521dfdf8da04792e8b083bf12cd0837870b7714b27725d4dcbc955b` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` |
| `ten_y_t5_maj4_k3_ic_yearly_v1` | `49e3e13` | `af04567a19c3` | `be7b6950d329ce058726d7bff80e4069192d2724223a364b02549527ec9f720a` | `7e55fae577b3a14085fdba98af638e449c11b935db373a6176238d72afea3781` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` |
| `ten_y_t5_say_k5_sharpe_static_v1` | `30e2edc` | `e8137af4b655` | `a6fd633a49cdb7e8e52d900cf1372fbdeccb61528426d3606d30f1a141b80311` | `960a058e9f98525022d21b19034e7055d49bb56e2bba54447dc0c79870d9010c` | `TECHNICAL_GATES_PASSED_DESCRIPTION_WAIVED` |

## 2. 授权和 revalidation 边界

1. 每个方案只能使用上表 exact scheme version、Metadata SHA-256 与 Delivery SHA-256 进行手工入库和 revalidation；摘要漂移即停止并重新判定。
2. revalidation 通过后，可逐方案按顺序执行 `controlled activate`、`persistent backtest`、`manual gray_live` 和 `DB/API/frontend acceptance`；每阶段都须复核专项授权和前序证据。
3. 四个方案的精确 version 和 composite Registry 均已 active，且各自已有
   333 条历史回测明细和 17 条月度指标；四个方案截至本记录尚未执行
   `manual gray_live`，也未写入 live prediction 或 `scheduled_live`。
4. 自动 scheduler、`automatic gray scheduling` 和 `scheduled_live` 仍禁止并延后到 TODO 的独立 admission；任何阶段都不得使用 `旧 generation fallback`。
5. 缺少 `description` 的状态仅是这四个不可变既有交付的专项豁免；不得补写或重写 Metadata，不得推测算法逻辑。后续新交付仍由当前人工 fail-closed 流程要求提供 `description`，机器门禁另行 TDD。

## 3. 已验收方案

### 3.1 Pilot

`ten_y_t5_maj3_k3_ic_static_v1` 的精确验收结果如下：

- scheme version 为 `c54b90bcafa7`；Metadata SHA-256 和 Delivery SHA-256
  分别仍为
  `10c41c6d3e271e76c6c03afc4e9ff3ad998ffefb92b557d5bb868d69e077329d`
  和
  `75749f165e3ce2c5cb70f86fae1336e52e693655198b05c78e45422e165471de`。
- production all-stage run
  `hr_20260726T122333Z_b65e23488522` 为 7/7 passed；技术
  no-persist backtest 为 100/100、`persist=false`。
- composite Registry
  `ten_y_t5_maj3_k3_ic_static_v1__h5__10Y` 和精确 version 均为
  `active`；该 active 只授予灰度实验室可见性，不授予 scheduler 权限。
- 持久化回测 run `182` 为 `success`，产生 333/333 条唯一 canonical
  prediction 和 17 条月度指标；批次为 `[100, 100, 100, 33]`。
  `predict_date/feature_date` 从 `2025-01-02` 到 `2026-05-22`，
  `target_date` 从 `2025-01-09` 到 `2026-05-29`，全部严格早于
  `2026-06-01`。
- `/api/backtests/factor-lab` 返回 HTTP 200，精确命中一个 pilot，
  包含 333 条 daily rows 和 17 条 monthly metrics，因此历史回测已在
  API 可见。
- 回测 provenance 由 run summary 和 prediction extra 中的
  `scheme_version`、`harness_run_id`、`generation_id`、
  `data_snapshot_id`，以及精确 active version 行中的
  code/config/Metadata hashes 共同闭环。当前 Blackbox 持久化路径未向
  `t_backtest_runs.code_hash/config_hash/input_artifact_hash` 三个可选列
  写值，这是现行审计技术债，不影响本批跨表身份验收。
- 当前 `t_input_generations` 仍为 0，状态为
  `GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION`。未写入 `gray_live`、
  `live_write` 或 `scheduled_live`，前端实时信号尚未完成；不得使用旧
  generation 伪造实时结果。
- 本验收不是正式 21/25、08:00 SLA、自动灰度调度或正式日批准入。

### 3.2 第二个方案

`ten_y_t5_maj4_k3_ic_static_v1` 的精确验收结果如下：

- scheme version 为 `6bdabf86b4a6`；Metadata SHA-256 和 Delivery SHA-256
  分别仍为
  `eee777b89f0a9138dce0454044e0569f91459c28f0f5426d8a7d5c06202da005`
  和
  `64000f9a4521dfdf8da04792e8b083bf12cd0837870b7714b27725d4dcbc955b`。
- production all-stage run
  `hr_20260726T125225Z_b85e6e69633b` 为 7/7 passed；技术
  no-persist backtest 为 100/100、`persist=false`。
- composite Registry
  `ten_y_t5_maj4_k3_ic_static_v1__h5__10Y` 和精确 version 均为
  `active`；该 active 只授予灰度实验室可见性，不授予 scheduler 权限。
- 持久化回测 run `183` 为 `success`，产生 333/333 条唯一 canonical
  prediction 和 17 条月度指标；批次为 `[100, 100, 100, 33]`。
  `predict_date/feature_date` 从 `2025-01-02` 到 `2026-05-22`，
  `target_date` 从 `2025-01-09` 到 `2026-05-29`，全部严格早于
  `2026-06-01`。
- `/api/backtests/factor-lab` 返回 HTTP 200，精确命中一个该方案，
  包含 333 条 daily rows 和 17 条 monthly metrics，因此历史回测已在
  API 可见。
- 回测 provenance 由 run summary 和 prediction extra 中的
  `scheme_version`、`harness_run_id`、`generation_id`、
  `data_snapshot_id`，以及精确 active version 行中的
  code/config/Metadata hashes 共同闭环。现行 Blackbox 持久化路径的
  `t_backtest_runs.code_hash/config_hash/input_artifact_hash` 三个可选列
  仍为 NULL，与 pilot 使用相同的跨表身份验收口径。
- 当前 `t_input_generations` 仍为 0，状态为
  `GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION`。未写入 `gray_live`、
  `live_write` 或 `scheduled_live`，前端实时信号尚未完成；不得使用旧
  generation 伪造实时结果。
- 本验收不是正式 21/25、08:00 SLA、自动灰度调度或正式日批准入。

### 3.3 第三个方案

`ten_y_t5_maj4_k3_ic_yearly_v1` 的精确验收结果如下：

- scheme version 为 `af04567a19c3`；Metadata SHA-256 和 Delivery SHA-256
  分别仍为
  `be7b6950d329ce058726d7bff80e4069192d2724223a364b02549527ec9f720a`
  和
  `7e55fae577b3a14085fdba98af638e449c11b935db373a6176238d72afea3781`。
- production all-stage run
  `hr_20260726T131714Z_faa165a8cfa1` 为 7/7 passed；技术
  no-persist backtest 为 100/100、`persist=false`。
- composite Registry
  `ten_y_t5_maj4_k3_ic_yearly_v1__h5__10Y` 和精确 version 均为
  `active`；该 active 只授予灰度实验室可见性，不授予 scheduler 权限。
- 持久化回测 run `184` 为 `success`，产生 333/333 条唯一 canonical
  prediction 和 17 条月度指标；批次为 `[100, 100, 100, 33]`。
  `predict_date/feature_date` 从 `2025-01-02` 到 `2026-05-22`，
  `target_date` 从 `2025-01-09` 到 `2026-05-29`，全部严格早于
  `2026-06-01`。
- `/api/backtests/factor-lab` 返回 HTTP 200，精确命中一个该方案，
  包含 333 条 daily rows 和 17 条 monthly metrics，因此历史回测已在
  API 可见。
- 回测 provenance 由 run summary 和 prediction extra 中的
  `scheme_version`、`harness_run_id`、`generation_id`、
  `data_snapshot_id`，以及精确 active version 行中的
  code/config/Metadata hashes 共同闭环。现行 Blackbox 持久化路径的
  `t_backtest_runs.code_hash/config_hash/input_artifact_hash` 三个可选列
  仍为 NULL，与前两个方案使用相同的跨表身份验收口径。
- 当前 `t_input_generations` 仍为 0，状态为
  `GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION`。未写入 `gray_live`、
  `live_write` 或 `scheduled_live`，前端实时信号尚未完成；不得使用旧
  generation 伪造实时结果。
- 本验收不是正式 21/25、08:00 SLA、自动灰度调度或正式日批准入。

### 3.4 第四个方案

`ten_y_t5_say_k5_sharpe_static_v1` 的精确验收结果如下：

- scheme version 为 `e8137af4b655`；Metadata SHA-256 和 Delivery SHA-256
  分别仍为
  `a6fd633a49cdb7e8e52d900cf1372fbdeccb61528426d3606d30f1a141b80311`
  和
  `960a058e9f98525022d21b19034e7055d49bb56e2bba54447dc0c79870d9010c`。
- 首次 production all-stage run
  `hr_20260726T134616Z_eaacc3eb13d4` 在 compare Gate fail-fast：
  static、input、unit、dry-run 四个 Gate passed，compare failed，错误为
  `TypeError: zip() takes no keyword arguments`。根因是 operator wrapper
  被系统 Python 3.9.6 直接启动，而平台 contract 使用 Python 3.10+
  的 `zip(..., strict=True)`；这次失败未创建 Registry/version，也未写
  run、prediction 或 backtest 业务数据。
- 失败 run 的 `report_uri` 已恢复到 worktree 中的原路径，并验证
  `onboard_report.json` 及 static、input、unit、dry-run、compare 五个
  Gate 报告均可读。该结论只证明当前 URI 可达，不表示报告已进入长期归档。
- 显式使用服务 Python 3.12.13 重跑后的 production all-stage run
  `hr_20260726T134918Z_b71762de0a2f` 为 7/7 passed；技术 no-persist
  backtest 为 100/100、`persist=false`。该成功 run 的 `report_uri`
  仍为
  `/tmp/bfl-gray-fourth-success-20260726/onboard_report.json`，存在
  P1 耐久性风险；在迁移到受治理的审计存储前不得声称其报告已长期归档。
- composite Registry
  `ten_y_t5_say_k5_sharpe_static_v1__h5__10Y` 和精确 version 均为
  `active`；该 active 只授予灰度实验室可见性，不授予 scheduler 权限。
- 持久化回测 run `185` 为 `success`，产生 333/333 条唯一 canonical
  prediction 和 17 条月度指标；批次为 `[100, 100, 100, 33]`。
  `predict_date/feature_date` 从 `2025-01-02` 到 `2026-05-22`，
  `target_date` 从 `2025-01-09` 到 `2026-05-29`，全部严格早于
  `2026-06-01`。
- `/api/backtests/factor-lab` 返回 HTTP 200，精确命中一个该方案，
  包含 333 条 daily rows 和 17 条 monthly metrics，因此历史回测已在
  API 可见。
- 回测 provenance 由 run summary 和 prediction extra 中的
  `scheme_version`、`harness_run_id`、`generation_id`、
  `data_snapshot_id`，以及精确 active version 行中的
  code/config/Metadata hashes 共同闭环。现行 Blackbox 持久化路径的
  `t_backtest_runs.code_hash/config_hash/input_artifact_hash` 三个可选列
  仍为 NULL，与前三个方案使用相同的跨表身份验收口径。
- 当前 `t_input_generations` 仍为 0，状态为
  `GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION`。未写入 `gray_live`、
  `live_write` 或 `scheduled_live`，前端实时信号尚未完成；不得使用旧
  generation 伪造实时结果。
- 本验收不是正式 21/25、08:00 SLA、自动灰度调度或正式日批准入。

截至本记录，四个方案均为 active、各有 333 条历史回测明细和 17 条月度
指标，历史数据已在回测 API 可见；四个方案统一处于
`GRAY_LIVE_WAITING_FOR_SAME_DAY_GENERATION`。前端实时信号仍未完成，
且不存在本批 `gray_live`、`live_write` 或 `scheduled_live`。

## 4. 后续顺序

本批之后的自动灰度调度必须先完成[TODO](../../TODO.md)列出的 21/25、迁移、replay、容量、恢复和连续观察门禁，并另行获得独立 `scheduler_admission=gray|formal` 与逐方案授权。`formal` 晋级不由本记录或 `gray` admission 推导。
