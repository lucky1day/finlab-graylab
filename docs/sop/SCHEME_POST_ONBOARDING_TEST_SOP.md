# 方案入库后测试验证 SOP

**更新日期**: 2026-06-11
**状态**: 已定稿 v1.0（用户 review 通过 2026-06-09）
**定位**: 面向**任意一个已入库方案**的标准测试验证流程（不限于现有 5 方案）。核心是 **gatekeeping（先验入库合规）→ 双版本复现对比（入库前原始 vs 改造后，同一数据接入层）→ 数据落库与前端校验 → 挂载定时任务 → 出验证结论**。

> 与 [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md) 的关系：入库 SOP 负责"把方案合规地改造进系统"；本 SOP 负责"验证改造后的方案结果与入库前原始方案一致，并完成落库/展示/挂载"。本 SOP 的多个失败分支会**打回入库 SOP**。
>
> 新增方案入口先读 [SCHEME_ONBOARDING_T0.md](SCHEME_ONBOARDING_T0.md)，再按 [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md) 和本文执行。本文是其「验证 + 落库 + 挂载」段的人类执行手册。

---

## 0. 通用约定

**适用对象**：任意一个声称"已入库"的 `scheme_id`。

**核心判定标准（全程统一）**：
- **方向零容差**：两版本对比时，`predicted_direction`（1/-1/0）必须**逐样本完全一致**；`confidence` 等浮点允许 `1e-9` 容差。方向差一个样本即判不一致。
- **confidence 语义**：`confidence` 是原始算法置信度、概率或分数在平台里的统一承接字段；如果原始算法没有天然 confidence，baseline/current 两侧必须使用同一确定性代理值，并在 `CURRENT_STATUS.md` 说明。
- **合规判据**：入库是否合规以 `python -m harness gate static` 的 `passed/failed` 为唯一机器判据。
- **同一数据接入层**：两版本复现必须使用**同一份 `shared.data_service` 导出的同一版本数据**（同一 `data_version` / 同一周范围 / 同一日期范围），否则对比无意义。

**状态机出口**：每个方案最终落到三态之一 —— `PASS`（验证通过、已挂载）/ `REJECTED_TO_ONBOARDING`（打回入库 SOP）/ `BLOCKED`（前置不满足，无法验证）。

**失败处理总原则**：任一步失败 → 产出明确的失败原因描述 + 证据 → 按该步定义的"打回去向"流转 → **不进入后续步骤**。

---

## 流程总览（状态机）

```
              ┌──────────────────────────────────────────────┐
   START ───▶ │ S1 入库合规门禁 (StaticGate)                   │
              └───────┬──────────────────────────────┬───────┘
                 pass │                          fail │
                      ▼                               ▼
              ┌───────────────┐               REJECTED_TO_ONBOARDING
              │ S2 版本回测定义 │                （打回入库 SOP）
              │    存在性检查   │───── 无定义 ──▶ REJECTED_TO_ONBOARDING
              └───────┬───────┘
                 有定义 ▼
   S3 入库前原始方案复现 ──失败──▶ BLOCKED（基准不可复现，需作者修基准）
                      ▼ ok
   S4 改造后方案复现（同一数据接入层）──失败──▶ REJECTED_TO_ONBOARDING
                      ▼ ok
   S5 两版本对比（方向零容差）──不一致──▶ REJECTED_TO_ONBOARDING（附差异描述）
                      ▼ 一致
   S6 落库（写 t_backtest_*）──失败──▶ 修复后重试 S6
                      ▼ ok
   S7 前端刷新 + DB↔前端严格比对 ──不一致──▶ 回到 S6/刷新
                      ▼ 一致
   S8 挂载日/周/月定时预测任务
                      ▼ ok
   S9 输出验证结论 ──▶ PASS
```

> 打回 `REJECTED_TO_ONBOARDING` 的方案，必须由 [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md) 流程排查根因并改造，改造完成后**从 S1 重新开始**整个验证。

---

## S1 — 入库合规门禁（Gatekeeping）

| 项 | 定义 |
|----|------|
| **入口条件** | 收到一个待验证 `scheme_id`，其目录存在于 `schemes/{scheme_id}/` |
| **动作** | 运行 `python -m harness gate static --scheme-id {scheme_id}`（StaticGate：目录名==scheme_id、predict.run 签名+SCHEME_ID、core 零 DB/零写库、无跨方案 import、强制 shared.input_artifacts、config schema 合规） |
| **成功判定** | StaticGate `status == passed` |
| **成功→去向** | 进入 S2 |
| **失败判定** | StaticGate `status == failed`（任一规则命中） |
| **失败→去向** | `REJECTED_TO_ONBOARDING`。输出 StaticGate 的 errors/evidence，**不再进入后续验证**。由入库 SOP 修复结构问题后从 S1 重来 |

---

## S2 — 版本回测定义存在性检查

| 项 | 定义 |
|----|------|
| **入口条件** | S1 通过 |
| **动作** | 检查该方案是否存在**入库前版本回测定义**。合法形态二选一（优先级从高到低）：<br>① **可重跑原始脚本**：入库前原始算法脚本（如 `docs/legacy_sources/legacy_*0529.py` 或 scheme core 内归档的 legacy 模块），能跨历史窗口产出预测序列；<br>② **静态基准文件**：入库前固化的基准输出（如 `benchmarks/{benchmark_id}/*.csv` 或预测结果表），含逐样本 `predict_date/tenor(or target_tenor)/direction(or predicted_direction)` |
| **成功判定** | ①或②至少存在其一，且能定位到具体文件/模块路径 |
| **成功→去向** | 进入 S3（记录采用的是脚本复现还是静态基准） |
| **失败判定** | 两种形态都不存在，或存在但无法定位/不含逐样本方向 |
| **失败→去向** | `REJECTED_TO_ONBOARDING`。原因："缺少版本回测定义，无法定义正确性基准"。由作者补齐入库前基准后从 S1 重来 |

> 说明：不在本步做时间窗口划分——按版本回测定义自身覆盖的历史范围整体复现即可。

---

## S3 — 入库前原始方案复现（基准序列）

| 项 | 定义 |
|----|------|
| **入口条件** | S2 通过，已确定版本回测定义形态 |
| **动作** | **形态①（脚本）**：重跑入库前原始脚本，产出基准预测序列，存 `reports/postonboard/{scheme_id}/baseline_original.json`。<br>**形态②（静态）**：直接读入库前静态基准文件，规整为同结构 `baseline_original.json`（逐样本 `predict_date/tenor(or target_tenor)/predicted_direction/confidence`） |
| **成功判定** | 基准序列成功生成、样本数 > 0、含必需字段 |
| **成功→去向** | 进入 S4 |
| **失败判定** | 原始脚本报错跑不出、或静态文件损坏/字段缺失 |
| **失败→去向** | `BLOCKED`。原因："入库前基准本身不可复现"。这不是改造方案的问题，需方案作者修复基准定义后从 S2 重来 |

---

## S4 — 改造后方案复现（同一数据接入层）

| 项 | 定义 |
|----|------|
| **入口条件** | S3 产出 baseline_original |
| **动作** | 用改造后的方案，**经统一数据接入层**（`shared.input_artifacts → shared.data_service`，与基准复现绑定**同一 data_version / 同一数据范围**）跑历史复现：`conda run -n forecast_env python -m backtests.{scheme_id}_reproduction --no-persist`，输出存 `reports/postonboard/{scheme_id}/repro_framework.json` |
| **成功判定** | 退出码 0；复现样本数与 baseline 可对齐（同一历史范围）；输入 artifact 的 `data_version`/`source` 与基准复现一致 |
| **data_version 硬判据** | 复现 artifact 的 `data_version` 必须 `== config.yaml.input_spec.data_version`，且与后续 live adapter 产出一致。三者（baseline 复现 / backtest runner / live）口径漂移即判失败（见 [SCHEME_CONTRACT.md §1/§7](../SCHEME_CONTRACT.md#7-落库后数据完整性契约)） |
| **成功→去向** | 进入 S5 |
| **失败判定** | 复现报错、或数据版本/范围与基准不一致（违反"同一数据接入层"） |
| **失败→去向** | `REJECTED_TO_ONBOARDING`。原因："改造后复现失败或数据接入不一致"。由入库 SOP 排查 adapter/输入链路后从 S1 重来 |

---

## S5 — 两版本对比（方向零容差）

| 项 | 定义 |
|----|------|
| **入口条件** | S3 baseline_original + S4 repro_framework 均就绪 |
| **动作** | 逐样本对齐（按 `predict_date + tenor`；平台内部字段可映射为 `target_tenor`）比对两版本，可用 `scripts/compare_refactor_outputs.py`（方向严格、浮点 1e-9）。输出对比报告 `reports/postonboard/{scheme_id}/compare.json` |
| **成功判定** | **所有可对齐样本 `predicted_direction` 完全一致**；`confidence` 差异 ≤ 1e-9；无"基准有而复现缺"的样本（或缺失已有合理解释并记录） |
| **confidence 判读** | `max_confidence_abs_diff` / `mean_confidence_abs_diff` 只表示两版本同名数值字段的浮点差异；`1e-16` 量级视为舍入误差，不代表模型行为改变 |
| **成功→去向** | 进入 S6 |
| **失败判定** | 存在任一样本方向不一致，或样本集不可对齐 |
| **失败→去向** | `REJECTED_TO_ONBOARDING`。**必须输出不一致明细**：哪些 `predict_date/tenor`、基准方向 vs 复现方向、差异数量。由入库 SOP 据此排查改造引入的偏差，改造后从 S1 重来 |

---

## S6 — 落库（写历史回测结果）

| 项 | 定义 |
|----|------|
| **入口条件** | S5 判定一致 |
| **动作** | 去掉 `--no-persist` 正式落库：`conda run -n bond_factor_lab_service python -m backtests.{scheme_id}_reproduction`，写入 `t_backtest_runs / t_backtest_predictions / t_backtest_monthly_metrics`。落库前后用 `probes/table_guard` 思路核验：仅 `t_backtest_*` 该 run 相关行增加，实盘表 `t_scheme_predictions/run_log/actuals` delta==0 |
| **成功判定** | 获得 `run_id`；受保护实盘表零变化；落库样本数 == S4 复现样本数 |
| **完整性判据** | 满足 [SCHEME_CONTRACT.md §7.2 历史回测落库](../SCHEME_CONTRACT.md#72-历史回测落库)：样本数一致、仅 `t_backtest_*` 增行、实盘表 `delta==0`、落库 run 的 `data_version` 与 §1 一致。落地校验器前由本步人工核验 |
| **成功→去向** | 进入 S7（记录 run_id） |
| **失败判定** | 落库报错、或误写实盘表、或样本数不符 |
| **失败→去向** | 修复后**重试 S6**（落库是确定性写操作，非算法问题，不打回入库） |

---

## S7 — 前端刷新 + DB↔前端严格比对

| 项 | 定义 |
|----|------|
| **入口条件** | S6 落库成功，得 run_id |
| **动作** | ① 强制刷新前端读取最新静态资源和最新 run（macOS `Cmd+Shift+R`；必要时 DevTools 勾选 `Disable Cache` 后刷新）；② 请求 `/api/backtests/factor-lab`；③ **严格比对** DB 中该 run 的回测结果与前端展示：逐 `tenor × 月份` 的样本数、准确率必须与 `t_backtest_monthly_metrics` 一致；整体准确率与 DB 聚合一致 |
| **成功判定** | 前端每一个展示数值都能在 DB 找到完全相等的来源；无"前端有 DB 无"或"DB 有前端漏"的格子 |
| **成功→去向** | 进入 S8 |
| **失败判定** | 任一前端数值与 DB 不符 |
| **失败→去向** | 先排除浏览器静态资源缓存（强制刷新/Disable Cache），再回到 **S7 起点重新刷新**（必要时回 S6 重新落库）。"所有回测结果必须严格验证完毕"方可放行 |

---

## S8 — 挂载定时预测任务

| 项 | 定义 |
|----|------|
| **入口条件** | S7 DB↔前端严格一致 |
| **动作** | 按方案 `frequency` 挂载定时预测任务：① 确认 `schedule.cron` 与频率匹配（日频工作日 07:03 / 周频周六 11:30 / 月频按定义）；② 签发 activate 授权 token；③ 通过 `python -m harness activate --scheme-id {scheme_id} --authorize {TOKEN}` 激活，不得手动改 `config.yaml status` 绕过 ActivationGate；④ 重启 scheduler 使其注册该 job；⑤ 确认调度日志出现该方案 cron 注册 |
| **成功判定** | ActivationGate/activate 命令成功，registry/config 状态生效，scheduler 日志确认 `Scheduled scheme {scheme_id} at {cron}`；方案进入对应频率的定时预测队列 |
| **成功→去向** | 进入 S9 |
| **失败判定** | status 未生效 / cron 未注册 / scheduler 未识别 |
| **失败→去向** | 修复 config/调度后**重试 S8** |
| **回滚** | 若挂载后出现异常，按入库 SOP 回滚策略：改回 `paused` + 重启，保留已写数据 |

> 挂载是副作用动作，必须授权并经过 ActivationGate。不得通过直接编辑 `status: active`、手工 SQL 或无授权脚本绕过 fail-closed 流程。

---

## S9 — 输出验证结论

| 项 | 定义 |
|----|------|
| **入口条件** | S8 挂载成功 |
| **动作** | 汇总产出该方案的**验证结论报告**，含：最终状态（PASS）、入库合规结论（S1）、采用的版本回测定义形态（S2）、复现样本数与准确率（S3/S4）、对比结论（S5 一致）、落库 run_id（S6）、前端比对结论（S7）、挂载 cron（S8）。更新 [CURRENT_STATUS.md](../CURRENT_STATUS.md) |
| **成功→去向** | 终态 `PASS` |

---

## 终态定义

| 终态 | 含义 | 后续 |
|------|------|------|
| **PASS** | 双版本方向完全一致、落库与前端严格对齐、定时任务已挂载 | 方案进入实盘运行；纳入定期回归复验 |
| **REJECTED_TO_ONBOARDING** | S1/S2/S4/S5 失败 | 打回 [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md)，排查根因→改造→**从 S1 重来** |
| **BLOCKED** | S3 失败（入库前基准本身不可复现） | 非改造问题；方案作者修复基准定义→从 S2 重来 |

---

## 失败/成功流转速查

| 步骤 | 成功去向 | 失败去向 | 判定核心 |
|------|----------|----------|----------|
| S1 入库合规 | S2 | REJECTED_TO_ONBOARDING | StaticGate passed |
| S2 版本回测定义 | S3 | REJECTED_TO_ONBOARDING | 脚本或静态基准至少其一 |
| S3 原始复现 | S4 | BLOCKED | 基准序列可生成 |
| S4 改造复现 | S5 | REJECTED_TO_ONBOARDING | 同一数据接入层、复现成功 |
| S5 对比 | S6 | REJECTED_TO_ONBOARDING（附差异） | 方向零容差 |
| S6 落库 | S7 | 重试 S6 | 仅 t_backtest_* 变化 |
| S7 前端比对 | S8 | 重试 S7/S6 | DB↔前端严格相等 |
| S8 挂载 | S9 | 重试 S8 | cron 注册成功 |
| S9 结论 | PASS | — | — |

---

> **本文已定稿 v1.0（2026-06-09 用户 review 通过）。** 重写自用户 10 步流程。已确认的关键设计：① 版本回测定义支持"可重跑脚本 / 静态基准"两种形态；② S3 失败归为 BLOCKED（基准本身问题，非改造问题，不打回入库）；③ S5 方向零容差；④ S7 按 tenor×月 粒度严格比对。后续如需为每步补可执行检查脚本，另起任务。
