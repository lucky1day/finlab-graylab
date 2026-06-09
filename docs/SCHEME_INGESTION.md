# 方案入库主线（Scheme Ingestion Mainline）

**更新日期**: 2026-06-09
**定位**: 面向 **AI 驱动新增方案** 的**单一权威端到端主线**。回答「AI 投放一个新 `.py`/文件夹 → 怎么验证 → 怎么入库」的全过程：源码放哪、怎么拆、预测怎么放怎么验、写哪张表、回测怎么入库。
**边界**: 本文是**索引 + 串联 + 缺口补强**，不重复造内容。每个环节的权威定义仍在对应文档（契约以 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 为准，步骤以 [sop/SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) / [sop/SCHEME_POST_ONBOARDING_TEST_SOP.md](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md) 为准）。

> **AI/新人第一入口**：先读本文建立全局主线，再按需深入 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md)（机器契约）、[CODE_ARCHITECTURE.md](CODE_ARCHITECTURE.md)（依赖规则）和两份 SOP（人类执行手册）。

---

## §0 一句话主线 + 投放物清单

**主线一句话**：把方案**投放进约定式目录** → 由 **harness gate 逐关验证**（结构 → 输入 → 单测 → 只读 dry-run → 双版本方向零容差对比 → 回测）→ **授权后单点写库**到 `t_scheme_predictions`（实盘）与 `t_backtest_*`（历史回测）。框架代码（L1–L3）**一行不改**。

```
AI 一次投放
  schemes/{id}/{core,predict.py,config.yaml}   ← 算法 + 适配 + 契约
  + 入库前基线（docs/legacy_sources/ 或 benchmarks/{benchmark_id}/）  ← 正确性对比基准
  + backtests/{id}_reproduction.py             ← 历史复现 runner
        │
        ▼  harness gates（fail-fast / 副作用 fail-closed）
  Static → Input → Unit → Dry-run → 双版本对比 → Backtest →（授权）Live
        │
        ▼  写库单点（方案永不直接写库）
  t_scheme_predictions（实盘，scheduler.repository）
  t_backtest_runs/_predictions/_monthly_metrics（回测，backtests.repository）
```

**投放物清单**（AI 一次需要交付什么）：

| 投放物 | 路径 | 必需 | 权威定义 |
|--------|------|:----:|----------|
| 算法纯逻辑 | `schemes/{id}/core/` | ✅ | [SCHEME_CONTRACT §4](SCHEME_CONTRACT.md#4-core-约束) |
| 适配器 | `schemes/{id}/predict.py` | ✅ | [SCHEME_CONTRACT §2](SCHEME_CONTRACT.md#2-predictpy-接口契约) |
| 方案契约 | `schemes/{id}/config.yaml` | ✅ | [SCHEME_CONTRACT §1](SCHEME_CONTRACT.md#1-configyaml-schema) |
| 入库前基线 | `docs/legacy_sources/legacy_*.py`（可重跑脚本）**或** `benchmarks/{benchmark_id}/*.csv`（静态基准） | ✅ | [POST_ONBOARDING_TEST_SOP §S2](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s2--版本回测定义存在性检查) |
| 回测 runner | `backtests/{id}_reproduction.py` | 参与历史排行时✅ | [ONBOARDING_SOP §7](sop/SCHEME_ONBOARDING_SOP.md) |

> 不需要交付：框架代码、写库代码、前端代码。普通新增方案**零框架改动**（见 [CODE_ARCHITECTURE §6 扩展模型](CODE_ARCHITECTURE.md#6-扩展模型约定式插件)）。

---

## §1 源码放哪里、怎么拆（问题 1 + 2）

拆分遵循三条不可破坏的不变量（[CODE_ARCHITECTURE §3.3](CODE_ARCHITECTURE.md#33-三条不可破坏的不变量)）：**依赖只向下、写库单点、输入单点**。

| 投放物 | 路径 | 角色与硬约束 |
|--------|------|--------------|
| **算法纯逻辑** | `schemes/{id}/core/` | DataFrame in / 结果对象 out；**零 DB、零写库、零跨方案 import**；输入由 adapter 注入（core 禁止 import `shared.input_artifacts`）。`core/legacy_*.py` 可保留旧代码，但活跃模块不得 import 它。 |
| **适配器** | `schemes/{id}/predict.py` | 暴露 `SCHEME_ID` + `run(predict_date)->list[PredictionRecord]`；**唯一**输入入口（必须 import `shared.input_artifacts`）；**禁止**写库、禁止 import `scheduler.repository/executor`。 |
| **方案契约** | `schemes/{id}/config.yaml` | 机器可校验 schema（`scheme_id`/`horizon`/`tenors`/`frequency`/`schedule`/`input_spec.*` 等）。 |

### 入库前基线（正确性对比基准）放哪

新方案的正确性靠「入库前原始版本 vs 改造后版本」**方向零容差**对比来锁定。基线二选一（[POST_ONBOARDING_TEST_SOP §S2](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s2--版本回测定义存在性检查)）：

- **形态①·可重跑脚本** → 放 `docs/legacy_sources/legacy_*.py` 或归档进 `schemes/{id}/core/legacy_*.py`，能跨历史窗口重新产出预测序列。
- **形态②·静态基准文件** → 放 `benchmarks/{benchmark_id}/*.csv`，含逐样本 `predict_date/target_tenor/predicted_direction`。

> 命名三分离：`scheme_id`（方案）/ `benchmark_id`（基准批次）/ `data_source`（数据口径）不可混用。运行期输入用 `backtest_artifacts/runtime_inputs/{scheme_id}/`，历史回测产物用 `backtest_artifacts/backtests/{benchmark_id}/`。

详细目录模板与 `config.yaml` 字段见 [ONBOARDING_SOP §3–§5](sop/SCHEME_ONBOARDING_SOP.md)。

---

## §2 预测文件怎么放、怎么验（问题 3）

`predict.py` 投放后，按 gate 链逐关验证（任一失败 fail-fast，不进入后续）：

| 关 | 验什么 | 通过判据 | 权威 |
|----|--------|----------|------|
| **Static** | 目录名==scheme_id、`run` 签名+`SCHEME_ID`、core 零 DB/零写库/无跨方案 import、强制 `shared.input_artifacts`、config schema | `harness gate static` 返回 `passed` | [CONTRACT §1/§2/§4](SCHEME_CONTRACT.md) |
| **Input** | 输入只经 `shared.input_artifacts` 产出，`extra` 记录 `input_artifact_source` | adapter/runner 不绕过公共输入层 | [ONBOARDING_SOP §3](sop/SCHEME_ONBOARDING_SOP.md) |
| **Unit** | core 输出、adapter 输出、`PredictionRecord` 字段与 config 一致 | 单测覆盖 | [ONBOARDING_SOP §5](sop/SCHEME_ONBOARDING_SOP.md) |
| **Dry-run** | `scheduler.scheme_runner` 只读返回 JSON list | 退出码 0、条数==有效 tenors 数、**`t_scheme_predictions/run_log` 行数不变** | [CONTRACT §3](SCHEME_CONTRACT.md#3-predictionrecord-运行期契约) |
| **双版本对比** | 入库前原始 vs 改造后，逐样本 `predicted_direction` | **方向零容差**（`confidence` 容差 1e-9） | [POST_ONBOARDING_TEST_SOP §S3–S5](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s3--入库前原始方案复现基准序列) |

### 🔴 周频/月频红线：week_id 必须读 DB，禁止日历公式

曾出现周频方案用「每年第一个周一」或 ISO 周公式把 `week_id` 算成日期，与 DB 实际口径不一致，导致特征周/目标周对错行、取错收益率、算错方向，且**入库时即被引入、不易察觉**。

强制规则（详见 [ONBOARDING_SOP §3a](sop/SCHEME_ONBOARDING_SOP.md)）：
- `week_id` 唯一权威来源 = `bond_db.api_wind_date.week_id`（经 `shared.calendar_service.week_id_for_date`）。
- **禁止** import 任何 `*_to_friday` / `*_to_monday` / `get_week_id_for_date` 这类**计算型**周历函数参与数据对齐。
- `target_week_id` 以 `api_wind_date` 中实际存在的下一个 week_id 为准，不是 `feature+1` 直接递增。

---

## §3 预测怎么写、写哪张表（问题 4）

### 写库单点：方案永不直接写库

方案 `run()` **只返回** `list[PredictionRecord]`，不碰数据库。统一由 `scheduler.executor` → `scheduler.repository.upsert_predictions(engine, records)` **UPSERT** 写入 `t_scheme_predictions`，并由 `write_run_log` 记 `t_scheme_run_log`。这保证运行日志与 UPSERT 口径一致（运行时调用图见 [CODE_ARCHITECTURE §5.1](CODE_ARCHITECTURE.md#51-预测路径调度--手动触发)）。

### 写哪张表：`t_scheme_predictions`（唯一键隔离多方案）

表结构以 [`migrations/001_init.sql`](../migrations/001_init.sql) 为权威，关键约束：

```sql
-- t_scheme_predictions（节选，完整见 migrations/001_init.sql）
scheme_id          VARCHAR(64)  NOT NULL   -- 多方案隔离
target_tenor       VARCHAR(16)  NOT NULL
horizon            INT          NOT NULL
predict_date       DATE         NOT NULL
target_date        DATE         NOT NULL
predicted_direction TINYINT     NOT NULL   -- 1=涨/空, -1=跌/多, 0=平
UNIQUE KEY uk_scheme_tenor_predict (scheme_id, target_tenor, predict_date)
```

**多方案隔离机制**：唯一键 `(scheme_id, target_tenor, predict_date)`。同一任务格子（如 `10Y · T+1`）下多个 `scheme_id` 可并存排行，互不覆盖。因此**一个 `scheme_id` 固定一个 `horizon`**。

### ⚠️ 关于 `t_pre_market_forecast`（参考表澄清）

> `t_pre_market_forecast` 是**旧 T1 生产系统的只读源表**，已列入 [`harness/probes/table_guard.py`](../harness/probes/table_guard.py) 的 `PROTECTED_TABLES`，**本项目代码从不写它**。新方案预测**一律写 `t_scheme_predictions`**，不要参照旧表去插数据。旧表仅作历史口径参考，受保护表行数在任何 gate 前后都必须 `delta==0`。

### actuals 口径对齐（用于实时算准确率）

后端 JOIN predictions × actuals 实时算准确率，按 horizon 取对应方向列：

| horizon | 实际方向来源 |
|---------|--------------|
| `1` | `t_scheme_actuals.direction_1d` |
| `5` | `t_scheme_actuals.direction_5d` |
| `6`（周频） | `t_scheme_weekly_actuals.direction_weekly` |

`PredictionRecord` 的 `target_date` 必须能与上述口径对齐（详见 [ONBOARDING_SOP §5 输出约束](sop/SCHEME_ONBOARDING_SOP.md)）。

---

## §4 回测脚本怎么入库（问题 5）

### 路径

```
backtests/{id}_reproduction.py
  ├─ shared.input_artifacts.build_*_input_artifact(...)   ← 唯一输入（runner 同样不得绕过）
  ├─ schemes/{id}/core/*  逐历史点跑算法
  ├─ 按 feature_date 归月生成月度指标
  └─ backtests.repository → t_backtest_runs / _predictions / _monthly_metrics   ← 回测写库单点
```

先 `--no-persist` 先验（只算不写），通过后再去掉该 flag 正式落库（[POST_ONBOARDING_TEST_SOP §S6](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s6--落库写历史回测结果)）：

```bash
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m backtests.{id}_reproduction --no-persist
PYTHONNOUSERSITE=1 conda run -n forecast_env python -m backtests.{id}_reproduction
```

### 铁律

- **回测结果绝不写 `t_scheme_predictions`**——历史排行与实盘预测分表，前端优先展示 `/api/backtests/factor-lab` 的最新 `framework_db_aligned` 回测。
- 落库前后用 `probes/table_guard` 核验：仅 `t_backtest_*` 该 run 相关行增加，实盘表 `t_scheme_predictions/run_log/actuals` `delta==0`。
- 落库样本数 == `--no-persist` 复现样本数（见 §5 与 [CONTRACT §7](SCHEME_CONTRACT.md#7-落库后数据完整性契约)）。

### 数据口径对齐（backtest ↔ live）

历史回测与实盘预测**必须使用同一数据口径版本**，否则历史排行与实盘表现不可比：

- `config.yaml.input_spec.data_version` 同时约束 live adapter 与 backtest runner 产出的 `InputArtifact.data_version`。
- 双版本对比（§2）必须绑定**同一 `data_version` / 同一数据范围**（[POST_ONBOARDING_TEST_SOP §S4](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s4--改造后方案复现同一数据接入层)）。
- DB 源数据重算（新列/重算因子）后，须重跑回测复验数据口径一致性。

---

## §5 端到端自检清单（投放即自检）

AI 投放后，按此自检（每项指向权威 gate/文档）：

- [ ] `scheme_id` == 目录名，未复用旧 ID；`name` 表达来源/预测长度/模型/版本。（[CONTRACT §1](SCHEME_CONTRACT.md#1-configyaml-schema)）
- [ ] `core/` 零 DB / 零写库 / 零跨方案 import；legacy 隔离。（[CONTRACT §4](SCHEME_CONTRACT.md#4-core-约束)）
- [ ] `predict.py` 暴露 `SCHEME_ID` + `run(predict_date)`，import `shared.input_artifacts`，不写库。（[CONTRACT §2](SCHEME_CONTRACT.md#2-predictpy-接口契约)）
- [ ] 输入只经 `shared.input_artifacts`；周频/月频 `week_id` 读 DB，无计算型周历函数。（§2 红线）
- [ ] `harness gate static` == `passed`。（[POST_ONBOARDING_TEST_SOP §S1](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s1--入库合规门禁gatekeeping)）
- [ ] dry-run 输出 JSON list，条数==有效 tenors 数，实盘表行数不变。
- [ ] 入库前基线就绪（脚本或静态文件），双版本**方向零容差**一致。（[§S3–S5](sop/SCHEME_POST_ONBOARDING_TEST_SOP.md#s3--入库前原始方案复现基准序列)）
- [ ] 回测落库仅写 `t_backtest_*`，实盘表 `delta==0`，样本数与复现一致。（[CONTRACT §7](SCHEME_CONTRACT.md#7-落库后数据完整性契约)）
- [ ] 落库后完整性：行数/值域/唯一性达标。（[CONTRACT §7](SCHEME_CONTRACT.md#7-落库后数据完整性契约)）
- [ ] live/backtest 同 `data_version`。（§4 数据口径对齐）
- [ ] 文档留痕：`CURRENT_STATUS.md` / `HISTORICAL_REPRODUCTION.md` / `TEST_PLAN.md`。

---

## §6 审计追溯（现状与已知限制）

**当前可追溯链路**：
- `t_scheme_run_log`（scheme_id + run_date + status + 时间）+ harness 报告目录 `reports/harness/{scheme_id}/{run_ts}/` 的**时间对账**，可定位「某次写库」对应的 harness 运行证据。
- 回测侧 `t_backtest_runs.benchmark_id/data_source` + `report_path` 记录口径与报告位置。

**已知限制（后续增强方向，本轮不改 schema）**：
- DB 目前**无法直接回答**「哪次 harness run 产出了这条 live 预测」——缺少 `t_scheme_run_log.harness_run_id` 强关联字段。
- 后续增强：为 `t_scheme_run_log` 增 `harness_run_id`（指向 `reports/harness/{id}/{ts}/`），使审计从「时间对账」升级为「主键直连」。**该字段待落地，本文仅记录方向。**

---

## 文档关系图

```
SCHEME_INGESTION.md（本文 · AI 第一入口 · 端到端主线）
  ├─▶ SCHEME_CONTRACT.md          机器可校验契约（config/predict/core/落库完整性）
  ├─▶ CODE_ARCHITECTURE.md        分层依赖规则与三不变量
  ├─▶ sop/SCHEME_ONBOARDING_SOP.md            人类执行手册：改造进系统
  └─▶ sop/SCHEME_POST_ONBOARDING_TEST_SOP.md  人类执行手册：验证 + 落库 + 挂载
```

> 冲突时，机器契约以 [SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 为准并回写 SOP；本文不引入新规则，只串联与补缺口标注。
