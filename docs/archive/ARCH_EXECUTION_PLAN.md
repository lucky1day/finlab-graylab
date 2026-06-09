# 架构执行计划（Architecture Execution Plan）

**更新日期**: 2026-06-08
**定位**: 把 [CODE_ARCHITECTURE.md](../CODE_ARCHITECTURE.md) §10 的演进路线拆成**可执行、可验收、可回滚**的分阶段计划。
**边界**: 本文是执行计划，**不含实现代码**。每个阶段交给实现时再写代码。
**与历史的区别**: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) 是 Phase 1–11 已完成工作的**状态记录**；本文是面向架构重构（数据层去重 + harness）的**前瞻计划**。

---

## 0. 最高原则：行为保持（Behavior Preservation）

当前 `t1_daily` / `t5_daily` / `weekly_10y_d_overlay` 为 **active 实盘**，`weekly_5y/7y` 已通过受控回测验收。**任何重构都不得改变这些方案的预测数值与回测结果**。

强制做法（贯穿所有数据层阶段）：

- **金标准基线（golden baseline）**：重构前，对每个方案跑一次 dry-run 与 `--no-persist` 回测，固化输出 JSON / summary 为基线文件（`reports/refactor_baseline/{scheme_id}/`）。
- **等价闸（equivalence gate）**：每个数据层步骤完成后，重新跑同一组 dry-run / 回测，与基线**逐字段比对**，数值差异必须为 0（浮点按既有容差）。差异非 0 即回滚该步。
- **小步提交**：每个阶段一个独立分支/提交，等价闸绿灯才合并；红灯只回滚当前步，不影响已合并步。

---

## 1. 阶段总览与依赖

```
S0 准备与基线         ─┐
S1 calendar_service   ─┤  数据层重构（清零 V1–V4，行为保持）
S2 周频去重           ─┤
S3 InputArtifact 强化 ─┘
        │  依赖图全合规（CODE_ARCHITECTURE §3.1 白名单 100% 成立）
        ▼
S4 harness contracts + StaticGate   ─┐
S5 InputGate/UnitGate/DryRunGate     ─┤  harness 落地
S6 BacktestGate/ApiGate              ─┤
S7 授权 + LiveGate + table_guard     ─┤
S8 orchestrator + CLI + report       ─┘
        ▼
S9 文档对齐 + 端到端纸面走查
```

依赖约束：

- **S1–S3 必须先于 S4**：StaticGate 的"core 零 DB / 无跨方案 import"规则在数据层重构前对现有方案必然 FAIL（见 V1/V2）。先清零违规，StaticGate 才能作为"持续守护"而非"一上线全红"。
- **S1 先于 S2**：周频 adapter 去掉跨方案 `read_source_week_id_for_date` 依赖 `calendar_service` 先就位。
- **S4 先于 S5–S8**：`GateResult`/`GateContext`/`contracts` 是其余 Gate 的公共底座。
- **S7 在 S5–S6 之后**：授权卡点保护的是写库动作，需先有 dry-run/backtest 能力。

---

## 2. 数据层重构阶段

### S0 — 准备与基线

| 项 | 内容 |
|----|------|
| 目标 | 固化金标准基线，建立等价闸工具 |
| 动作 | ① 对 5 个方案各跑 dry-run + `--no-persist` 回测，输出存 `reports/refactor_baseline/`；② 写一个只读比对脚本 `scripts/compare_refactor_outputs.py`（对比当前输出 vs 基线，逐字段，返回差异数） |
| 改动 | 仅新增 `scripts/compare_refactor_outputs.py` + 基线产物；不碰业务代码 |
| 验收闸 | 比对脚本对"基线 vs 基线"返回差异 0（自洽） |
| 回滚 | 删除新增脚本与产物 |
| 风险 | 低。纯只读 |

### S1 — 新建 `shared/calendar_service.py`，消除 V3/V1（日历部分）

| 项 | 内容 |
|----|------|
| 目标 | 交易日历/周历查询单点化，取代 `t5_daily` 内联 SQL 与周频 `read_source_week_id_for_date` |
| 动作 | ① 新建 `shared/calendar_service.py`（`get_calendar()` → `CalendarService.{is_trading_day,next_trading_days,nth_trading_day_after,week_id_for_date,week_id_to_last_trading_day}`），内部复用现有 `shared/weekly_calendar.py` 的纯规则；② `t5_daily/predict.py` 的内联 `text(...t_trade_calendar...)` → `calendar.nth_trading_day_after(feature_date, 5)`；③ 三个周频 `predict.py` 的 `read_source_week_id_for_date(...)` → `calendar.week_id_for_date(...)`，**删除跨方案 import**（消 V1） |
| 改动文件 | 新增 `shared/calendar_service.py`；改 `schemes/{t5_daily,weekly_10y,weekly_5y,weekly_7y}/predict.py`；新增 `tests/test_calendar_service.py` |
| 前置 | S0 |
| 验收闸 | ① 等价闸：5 方案 dry-run 输出 == 基线（`target_date`/`week_id` 不变）；② `grep "from schemes\." schemes/*/predict.py` 为空；③ `grep "t_trade_calendar" schemes/` 为空 |
| 回滚 | 还原 4 个 predict.py，删 calendar_service |
| 风险 | 中。日历语义须与旧 SQL/旧函数**逐日对齐**——用现有 `weekly_10y` live 的 `feature_week_id=202621`、`t5` 的 T+5 目标日做定点核对 |

### S2 — 周频去重，消除 V2/V1（数据部分）

> **范围修订（2026-06-08，S1 完成后复核）**：S1 把三个周频 adapter 改用 `shared.calendar_service` 后，全仓库扫描确认 `schemes/weekly_10y_d_overlay/core/weekly_data_service.py` 在**所有运行路径上已无人调用**（`schemes/backtests/scheduler/backend/shared` 零引用），唯一引用方是 `tests/test_weekly_10y_integration.py` 对其内部函数的单测。生产周频输入早已统一走 `build_weekly_input_artifact → shared.data_service.build_weekly_output_from_db`（见 `CURRENT_STATUS.md`）。
> 故 S2 **从「高风险逻辑移植」降级为「删除死代码 + 解开测试依赖」**。经决策：**砍掉 wind_export_0529 收编**——无活路径需要它，将来真需要再单开任务。不新增 `weekly_variant` 参数。

| 项 | 内容 |
|----|------|
| 目标 | 删除死代码 `core/weekly_data_service.py`（消 V2），不引入任何数值变化 |
| 前置闸（删除前必须全绿，否则停） | ① 干净工作区跑 `grep -rn "weekly_data_service" schemes backtests scheduler backend shared --include=*.py \| grep -v __pycache__` **为空**；② 唯一活引用仅剩 `tests/test_weekly_10y_integration.py` |
| 动作 | ① **先迁移/删测试**：`test_weekly_10y_integration.py` 第 33/58/95/142 行 `import_module("...core.weekly_data_service")` 及第 293 行 schema 测试，测的是 pivot / daily_close_fallback / wind_export metadata+lag / schema json——全部是被删口径的内部函数，生产路径已不用。处理：日历/周历语义类改测 `shared.calendar_service`；纯 wind_export 口径类**删除该用例**，commit message 注明「测试对象随死代码移除，生产路径改用 shared.data_service 统一 weekly」；② **删除** `schemes/weekly_10y_d_overlay/core/weekly_data_service.py`；③ **删除** `schemes/weekly_10y_d_overlay/core/weekly_output_0529_columns.json`（仅剩死引用，已确认）；④ 不改 `shared/data_service.py`，不改 3 个 adapter（S1 已就位），不加 `weekly_variant` |
| 改动文件 | 删 `core/weekly_data_service.py`、`core/weekly_output_0529_columns.json`；改 `tests/test_weekly_10y_integration.py` |
| 前置 | S1 |
| 验收闸 | ① **等价闸**：3 周频方案 dry-run + `--no-persist` 回测，`scripts/compare_refactor_outputs.py --ignore-path '$.elapsed_sec'` 对比 baseline，`diff_count == 0`（10Y `68.9%`、5Y `58.4%`、7Y `62.8%` 不变）；② **静态闸 V2 清零**：`schemes/*/core/*.py`（非 legacy）`grep -lE "sqlalchemy\|create_engine\|read_sql\|text\("` 为空；③ 分环境跑测试全绿（forecast_env 子集 + bond_factor_lab_service 子集） |
| 回滚 | 单独 commit；`git checkout` 恢复被删文件与测试。红灯即整步回退 |
| 风险 | **低**（修订后）。纯删除无人调用的死代码 + 测试解耦，不触碰任何活算法/数据路径。IMPLEMENTATION_PLAN 记录的「周频输入逐值差异」与本步无关（那是输入工件 vs 桌面 CSV 的历史项），本步不引入新差异 |
| 完成后回写 | `CODE_ARCHITECTURE.md §4` 标注 V1/V2 清零、V3 部分完成；`SCHEME_CONTRACT.md §5` weekly_10y「core 零 DB ❌→✅」 |

### S3 — 强化 `InputArtifact`，统一回测输入，消除 V4

| 项 | 内容 |
|----|------|
| 目标 | InputArtifact 带可校验元数据；回测输入统一走 input_artifacts |
| 动作 | ① `InputArtifact` 增 `data_version/row_count/column_count/columns/date_coverage/quality_flags`（生成时计算填充，不改 `dataframe` 语义）；新增 `Coverage`；② `backtests/daily_0529_reproduction.py` 改为经 `shared.input_artifacts.build_daily_input_artifact`（消 V4）；③ 移除 `build_weekly_input_artifact` 的死参数 `end_date`/`include_daily_weekly_close_fallback`；④ 各 `config.yaml` 补 `input_spec.{data_version,required_columns}` |
| 改动文件 | 改 `shared/input_artifacts.py`；改 `backtests/daily_0529_reproduction.py`；改各 `config.yaml`；改相关 `tests/` |
| 前置 | S2 |
| 验收闸 | ① 等价闸：daily 回测输出 == 基线；② `InputArtifact` 新字段非空，`quality_flags.missing_required_cols == []`；③ 死参数引用 grep 为空 |
| 回滚 | 还原 input_artifacts 字段与 daily runner |
| 风险 | 中。daily 回测换输入链路须数值对齐（Y 依赖列误差 0，沿用既有校验口径） |

**数据层出口标志**：CODE_ARCHITECTURE §4 的 V1–V4 全部清零；§3.1 import 白名单 100% 成立；5 方案输出全部 == 基线。

---

## 3. Harness 落地阶段

> 全部为新增 `harness/` 包，不改 L1–L4 业务代码（除被 Gate 调用）。设计依据 [HARNESS_DESIGN.md](../HARNESS_DESIGN.md)。

### S4 — contracts + result + StaticGate（地基）

| 项 | 内容 |
|----|------|
| 目标 | 可执行的静态强约束守护 |
| 动作 | 建 `harness/{__init__,__main__,cli}.py`、`result.py`（`GateStatus/Evidence/GateResult/OnboardReport`）、`context.py`、`contracts/{config_schema,predict_contract,import_rules}.py`、`gates/{base,static_gate}.py`、`report/writer.py`；StaticGate 复用 `scheduler.discovery.load_scheme_config` |
| 前置 | S1–S3（否则现有方案 StaticGate 全红） |
| 验收闸 | `python -m harness gate static --scheme-id <每个现有方案>` 全 PASS；故意构造一个违规样例（core 加 `import sqlalchemy`）能被判 FAIL |
| 回滚 | 删 `harness/`（无人依赖它） |
| 风险 | 低。纯静态、零副作用、不被任何层 import |

### S5 — InputGate / UnitGate / DryRunGate

| 项 | 内容 |
|----|------|
| 目标 | 只读运行类 Gate |
| 动作 | `gates/input_gate.py`（调 `shared.input_artifacts`）、`unit_gate.py`（unittest 选择器）、`dry_run_gate.py`（调 `scheduler.executor.run_scheme_subprocess` + `probes/table_guard.py` 断言写库表行数不变）；建 `probes/table_guard.py` |
| 前置 | S4 |
| 验收闸 | 对 `weekly_5y_direct_production` 跑三 Gate 全 PASS；DryRunGate 断言 `t_scheme_predictions/run_log` delta==0 |
| 回滚 | 删对应 gate 文件 |
| 风险 | 中。DryRunGate 走 conda 子进程，需确认 `forecast_env` 环境与超时配置 |

### S6 — BacktestGate / ApiGate

| 项 | 内容 |
|----|------|
| 目标 | 回测与只读 API Gate |
| 动作 | `gates/backtest_gate.py`（调 `backtests/{id}_reproduction --no-persist`；persist 留待 S7 授权）、`gates/api_gate.py` + `probes/api_probe.py`（只读 `/api/metrics` `/api/backtests/factor-lab`） |
| 前置 | S4 |
| 验收闸 | BacktestGate 对 5Y/7Y 返回 summary == 基线；ApiGate 探测格子存在 |
| 回滚 | 删对应文件 |
| 风险 | 低 |

### S7 — 授权 + LiveGate + 行数保护

| 项 | 内容 |
|----|------|
| 目标 | 副作用卡点 fail-closed |
| 动作 | `authorization.py`（`Authorization` + `verify`，绑定单 `(scheme_id,action)`、一次性 token）；`gates/live_gate.py`（授权后调 `scheduler.executor.execute_scheme` 单方案）；扩展 `table_guard` 做受保护表全集 before/after diff（只授权表 delta>0，其余 0）；readiness 收编 `scripts/check_weekly_10y_readiness.py` |
| 前置 | S5、S6 |
| 验收闸 | 无 token → LiveGate 返回 BLOCKED 且不写库；带 token → 仅目标表行数变化，其余表 delta==0；授权审计落 `reports/harness/.../authorization.json` |
| 回滚 | 删 live_gate/authorization |
| 风险 | **高**（真实写库）。强制：仅对 paused 方案演练；演练后用 SQL 核验并按 SOP §8 回滚流程清理 |

### S8 — orchestrator + CLI + report

| 项 | 内容 |
|----|------|
| 目标 | 一条命令串联，fail-fast + fail-closed |
| 动作 | `registry.py`（stage 顺序）、`orchestrator.py`（`onboard`）、`cli.py` 补全 `onboard/activate/report` 子命令、退出码 0/1/2 |
| 前置 | S4–S7 |
| 验收闸 | `python -m harness onboard weekly_5y_direct_production --predict-date 2026-06-06 --stage all` 走完全部自动段并产出聚合报告；某步注入失败→后续不执行、退出码 1 |
| 回滚 | 删 orchestrator/registry |
| 风险 | 低 |

---

## 4. 收尾阶段

### S9 — 文档对齐 + 端到端纸面走查

| 项 | 内容 |
|----|------|
| 动作 | ① SOP 各 Gate 标注实际 `python -m harness` 命令；② CODE_ARCHITECTURE §4 标注 V1–V4 已清零；③ 用一个真实 paused 方案做端到端走查并记录证据路径；④ 更新 CURRENT_STATUS |
| 验收闸 | 文档无死链；走查证据齐全；StaticGate 对全部方案持续绿 |
| 风险 | 低 |

---

## 5. 验收里程碑（Definition of Done）

| 里程碑 | 判定 |
|--------|------|
| **M1 数据层合规** | V1–V4 清零；5 方案输出 == 金标准基线；`core/*` 非 legacy 零 DB；无跨方案 import |
| **M2 StaticGate 守护** | `harness gate static` 对全部方案 PASS，注入违规可 FAIL |
| **M3 自动段贯通** | `harness onboard --stage all` 对 paused 方案全自动段绿灯并出报告 |
| **M4 授权卡点** | 写库/激活无 token 一律 BLOCKED；带 token 仅影响授权表 |
| **M5 自动化入库可用** | 新方案：塞进 `schemes/` → `harness onboard` 驱动改造-测试-验证，框架零改动 |

---

## 6. 风险登记与缓解

| 风险 | 级别 | 缓解 |
|------|------|------|
| 周频去重引入数值漂移（S2） | 高 | 逐函数移植对齐 + 等价闸；已知逐值差异列为基线，不新增 |
| 日历语义不一致（S1） | 中 | 定点核对 live 已知 week_id / T+5 目标日 |
| LiveGate 误写库（S7） | 高 | 仅 paused 方案演练；table_guard 行数保护；按 SOP 回滚 |
| conda 子进程环境差异（S5） | 中 | 沿用 executor 既有 `forecast_env` 调用与超时 |
| 重构期实盘照常运行 | 中 | 数据层改 `shared` 有影响面——每步等价闸 + 小步合并；active 方案改动后立即跑一次 dry-run 核验 |

---

## 7. 建议执行顺序（一句话）

**先用基线锁住行为（S0）→ 数据层逐步去重并每步等价校验（S1→S2→S3，清零 V1–V4）→ 再搭 harness 地基与各 Gate（S4→S8）→ 文档对齐与走查（S9）。** 数据层不合规前不让 StaticGate 上线，写库类 Gate 一律授权卡点。

> 本文为执行计划。未创建或修改任何业务代码。
