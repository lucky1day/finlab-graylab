# 方案入库后测试验证 SOP

**更新日期**: 2026-06-09
**状态**: 草案 v0.1（待人工 review 后定稿）
**定位**: 方案**已完成入库**（通过 [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md) 的 Normalize→Static→Input→Unit→Dry-run→Backtest→Live→Activation）之后，对其**结果正确性与前端展示**做的系统性测试验证流程。

> 与入库 SOP 的区别：
> - **入库 SOP** 回答"方案能不能合规地进系统"（结构、契约、不写错库）。
> - **本 SOP** 回答"方案进系统后，结果对不对、前端显示对不对、和基线一致不一致"。
>
> 适用对象：① 新入库方案的首次结果验收；② 已有方案的定期/回归复验（如 5 方案重新验证）；③ 数据层/框架改动后的回归。

---

## 0. 前置条件

执行本 SOP 前必须满足：

- [ ] 方案已通过 [SCHEME_ONBOARDING_SOP.md](SCHEME_ONBOARDING_SOP.md) 全部入库 Gate。
- [ ] 后端服务在 `127.0.0.1:8100` 运行（`GET /api/health` 通）。
- [ ] 双 conda 环境就绪：`forecast_env`（算法）、`bond_factor_lab_service`（服务）。
- [ ] 若做"回归复验"：已有金标准基线 `reports/refactor_baseline/{scheme_id}/`（无则先按 §1 生成）。

约定标记：✅=自动可判定（命令+期望值），👁=人工目检，🔒=需授权/写库（默认跳过，仅授权时执行）。

---

## 阶段 T0 — 锁定验证基线

| 步骤 | 命令 / 动作 | 通过标准 |
|------|-------------|----------|
| T0.1 ✅ | 对方案跑 dry-run，输出存基线：`conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id {id} --predict-date {date} > reports/refactor_baseline/{id}/dry_run.json` | 退出码 0，JSON list |
| T0.2 ✅ | 跑 `--no-persist` 回测存基线：`conda run -n forecast_env python -m backtests.{id}_reproduction --no-persist > reports/refactor_baseline/{id}/backtest_no_persist.json` | 退出码 0，含 summary |
| T0.3 ✅ | 自洽校验：`python scripts/compare_refactor_outputs.py reports/refactor_baseline reports/refactor_baseline` | `diff_count=0` |

> 复验已有方案时，基线即"上次验收通过的输出"。首次验收时，基线即"算法作者认可的预期输出"。

---

## 阶段 T1 — 结果正确性验证（核心）

### T1.1 ✅ dry-run 字段契约
跑 `python -m harness gate dry-run --scheme-id {id} --predict-date {date}`，断言（见 [SCHEME_CONTRACT.md](../SCHEME_CONTRACT.md) §3）：
- 返回条数 == 有效 tenors 数
- 每条 `scheme_id/horizon/target_tenor` 与 config 一致
- `predicted_direction ∈ {1,-1,0}`
- `extra` 含 `input_artifact_path/input_artifact_source`；周频另含 `feature_week_id/target_week_id/feature_date/target_date/target_rule`
- **写库表 delta==0**（probes/table_guard 自动核验）

### T1.2 ✅ 等价闸（与基线逐字段比对）
```bash
# 重新生成 current
conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id {id} --predict-date {date} > reports/refactor_current/{id}/dry_run.json
conda run -n forecast_env python -m backtests.{id}_reproduction --no-persist > reports/refactor_current/{id}/backtest_no_persist.json
# 比对
python scripts/compare_refactor_outputs.py reports/refactor_baseline reports/refactor_current --ignore-path '$.elapsed_sec'
```
**通过标准：`diff_count=0`。** 非 0 即结果漂移，必须定位原因后才能继续。

### T1.3 ✅ 回测 summary 合理性
从 T0.2 / T1.2 的 backtest summary 核验：
- 样本数 `sample_count` 与历史口径一致（如 5Y=503、7Y=43、10Y=45）
- 整体准确率 `accuracy` 在预期区间
- 月度分布 `monthly_distribution` 无异常空洞

### T1.4 👁 预测语义抽查（人工）
取最近 1–2 个 `predict_date`，人工核对：
- `predicted_direction` 的方向语义（1=收益率上行/价格空…）与算法意图一致
- 周频 `target_date` 是否落在正确的目标周最后交易日
- `confidence` 数值合理（非恒定、非 NaN）

---

## 阶段 T2 — 实际方向（actuals）与准确率口径验证

### T2.1 ✅ actuals 覆盖
确认对齐该方案所需的 actuals 已就绪：
- 日频：`t_scheme_actuals` 覆盖到目标日，`direction_1d`/`direction_5d` 非空
- 周频：`t_scheme_weekly_actuals` 含该 tenor，`direction_weekly`/`target_rule` 与方案一致

### T2.2 ✅ 准确率 JOIN 口径
确认后端按正确 horizon 取 actuals：horizon=1→`direction_1d`、horizon=5→`direction_5d`、horizon=6→`t_scheme_weekly_actuals.direction_weekly`。未来目标日无 actuals → 暂无准确率属正常，不算失败。

---

## 阶段 T3 — API / 前端展示验证

### T3.1 ✅ API 只读探针
```bash
curl -s http://127.0.0.1:8100/api/health
curl -s "http://127.0.0.1:8100/api/backtests/factor-lab"          # 历史排行矩阵
curl -s "http://127.0.0.1:8100/api/metrics/{id}?tenor={tenor}"    # 实盘指标（若 active）
```
通过标准：HTTP 200；factor-lab 返回含本方案 `scheme_id:tenor:data_source`；周频方案 `frequency=weekly` 或 `horizon=6`。
> 也可直接跑 `python -m harness gate api --scheme-id {id}`。

### T3.2 👁 前端矩阵目检（人工）
打开 `http://127.0.0.1:8100/`，核对：
- 方案出现在正确任务格子（如 `5Y国债活跃 · 周度`）
- 矩阵准确率数字与 T1.3 回测 summary 一致（如 `58.4%`）
- 点击方案 → 详情显示名、月度明细正确
- 同一格子多方案可并存排行；切换排行指标格子最优值同步变化
- 周频明细按 `feature_date` 所属月份归月

### T3.3 👁 展示名与口径
确认 `target_label`（来自 `t_target_registry`）显示中文名正确；历史回测显示为"当前DB对齐回测"口径。

---

## 阶段 T4 🔒 — 实盘写入复验（仅授权时）

> 默认**跳过**。仅当需要验证 active 方案的实盘写库链路时，经显式授权执行。

| 步骤 | 动作 | 通过标准 |
|------|------|----------|
| T4.1 🔒 | 发授权：`python -m harness auth issue --scheme-id {id} ...` 取 token | 得到一次性 token |
| T4.2 🔒 | 受控写库：`python -m harness gate live --scheme-id {id} --predict-date {date} --authorize <TOKEN>` | 仅该 scheme 的 `t_scheme_predictions`/`t_scheme_run_log` +N，其余受保护表 delta==0 |
| T4.3 ✅ | 写后核验 SQL：`SELECT ... FROM t_scheme_predictions WHERE scheme_id='{id}'` | 行数/字段符合预期；run_log 有 success |
| T4.4 ✅ | 审计留痕 | `reports/harness/{id}/{ts}/authorization.json` 存在 |

无 token 时 live gate 返回 BLOCKED 且不写库（fail-closed）——这本身是一条应通过的负向用例。

---

## 阶段 T5 — 验收记录

| 步骤 | 动作 |
|------|------|
| T5.1 | 汇总 T1–T3（及 T4 若执行）的证据：等价闸 diff_count、回测 summary、API 命中、前端截图/目检结论 |
| T5.2 | 更新 [CURRENT_STATUS.md](../CURRENT_STATUS.md)：该方案最新验证日期、run_id、准确率、是否 active |
| T5.3 | 若为回归复验且发现漂移：记录差异、定位根因、决定回滚或接受 |

---

## 一键串联（自动段）

入库后测试的自动段可直接用 harness 串联（不含 🔒 live）：
```bash
python -m harness onboard {id} --predict-date {date} --stage all
# static → input → unit → dry-run → backtest → api，fail-fast，退出码 0/1/2
```
本 SOP 在其之上补充了 **T0/T1.2 等价闸**（与基线比对，harness 默认不做）、**T1.4/T3.2 人工目检**、**T2 actuals 口径**、**T5 记录**——这些是"结果对不对/显示对不对"的判断，harness 自动段只保证"能合规运行且不写错库"。

---

## 验收门槛（Definition of Done）

一个方案通过本 SOP 的判定：

- ✅ T1.2 等价闸 `diff_count=0`（结果与基线一致）
- ✅ T1.1 dry-run 字段契约全过、写库表 delta==0
- ✅ T1.3 回测 summary 样本数/准确率符合预期
- ✅ T3.1 API 200 且命中本方案
- 👁 T1.4 + T3.2 人工目检通过（语义、前端展示正确）
- ✅ T2 actuals 口径正确（或未来目标日无 actuals 的合理空缺）
- 📝 T5 验收记录已更新

> 🔒 T4 实盘写入复验不是默认门槛，仅授权场景纳入。

---

## 5 方案复验执行清单（本 SOP 首次落地用）

按本 SOP 逐个验证现有 5 方案，逐格打勾：

| 方案 | T1.2 等价 | T1.3 回测 | T3.1 API | T3.2 前端👁 | T5 记录 |
|------|:--------:|:--------:|:--------:|:----------:|:------:|
| t1_daily (active) | ☐ | ☐ | ☐ | ☐ | ☐ |
| t5_daily (active) | ☐ | ☐ | ☐ | ☐ | ☐ |
| weekly_10y_d_overlay (active) | ☐ | ☐ | ☐ | ☐ | ☐ |
| weekly_5y_direct_production (paused) | ☐ | ☐ | ☐ | ☐ | ☐ |
| weekly_7y_cross_d_overlay (paused) | ☐ | ☐ | ☐ | ☐ | ☐ |

> 预期基线值（来自现有记录，复验时应不变）：10Y `68.9% (31/45)`、5Y `58.4% (294/503)`、7Y `62.8% (27/43)`；t1/t5 framework-db mismatch=0。

---

> **本文为草案 v0.1，待人工 review。** review 关注点建议：① T1.4/T3.2 人工目检项是否够具体；② 是否需要补充"性能/超时"验证；③ 5 方案预期基线值是否需按最新 DB 重新锁定。
