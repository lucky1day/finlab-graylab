# Bond Factor Lab 框架级 Review 报告

> 历史审计快照：本报告保留 2026-06-10 当时的代码证据和评审结论，正文中的 run_id UK、serving pointer、`/api/metrics/{id}?tenor=...`、前端 deployment override、mock 兜底等描述均是当时问题证据，不代表当前实现状态。
>
> **不要把本文中的接口、表结构、前端字段或流程片段当作当前 SOP 示例复制。** 当前权威状态以 [CURRENT_STATUS.md](CURRENT_STATUS.md)、[PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md)、[SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 和 [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md) 为准。

**评审日期**: 2026-06-10
**评审对象**: 当前工作区代码（分支 `codex/p1-runner-factorlab-slim`，含未提交改动）
**评审定位**: 检查当前工程是否能支撑"国债盘前预测方案上生产前的灰度实验室"
**评审方法**: 全量代码取证（shared / schemes / backtests / scheduler / backend / frontend / harness / migrations / scripts / tests），结论均附代码证据；无法确认的明确标注"未在当前代码中确认"。

> 本报告独立于 2026-06-09 的 [bond_factor_lab_architecture_review.md](bond_factor_lab_architecture_review.md)。该报告提出的 P0/P1 改造（CompareGate、ActivationGate、run_id 不可变模型、serving pointer、回测 append、harness 留痕、GET 只读）经本次核验**确已落地**，本报告聚焦改造后的剩余缺口。

---

## 1. 总体结论

### 1.1 能否支撑生产前灰度实验室

**结论：能支撑当前规模（3 个方案）的灰度观察，核心数据模型与边界已经达标；但在"流程强制性"与"调度平台化"两个维度上是治理式而非强制式，平台化（几十个方案、多人协作）前需要补齐。**

### 1.2 已具备的能力

| 能力 | 状态 | 关键证据 |
|------|------|----------|
| 三条不变量（输入单点 / 写库单点 / core 纯净） | ✅ 已满足（事实层面） | 三个 `predict.py` 均经 `build_*_input_artifact`；core 零 sqlalchemy/网络 import；写库仅在两个 repository + actuals_updater + harness.persistence |
| 回测 / 实盘物理隔离 | ✅ 已满足 | `backtests/repository.py` 只写 `t_backtest_*`；`scheduler/repository.py` 只写 `t_scheme_*`；两模块零交叉 import；LiveGate 行数 delta 双向校验 |
| 不可变运行模型 | ✅ 已满足 | UK `(scheme_id, target_tenor, predict_date, run_id)`（006）；回测删 scope UK 改 append + `v_latest_backtest_run`（007）；serving pointer 控制展示 |
| 可追溯链 | ✅ 已满足（间接） | `t_scheme_predictions.run_id/scheme_version` 直接可查；input_artifact / harness_run 经 `t_scheme_runs` 两跳 JOIN 可达 |
| GET 只读 | ✅ 已满足 | 全部 12 个 GET 端点纯读；registry sync 移到 startup + 受保护 POST；有测试断言守护 |
| 前端数据驱动 | ✅ 基本满足 | 方案列表 / 矩阵格子由 API 驱动，新增方案无需改前端（个别硬编码例外见 §4.6） |
| harness Gate 链 | ✅ 已落地 | 9 个 gate 可运行；live/backtest-persist/activate 经 orchestrator 层 fail-closed（无 token 即 BLOCKED） |

### 1.3 不足的能力

1. **harness 是约定不是屏障**：`python -m scheduler`、`python -m backtests.*_reproduction`（不带 `--no-persist`）、手工编辑 `config.yaml` / `UPDATE t_scheme_registry` 均可完全绕过 gate 直接写库或激活方案。
2. **ActivationGate 无前置门级联**：激活只校验 token + config schema，**不查询 `t_harness_gate_results` 确认该方案已通过 onboard 全链**——可以激活一个从未通过回测复现的方案。
3. **CompareGate 缺 benchmark 时 SKIP 视同通过**：行为一致性验证（业务闭环第 3 步的核心）可能被静默跳过。
4. **调度层未平台化**：方案全串行执行、无重试、无告警通道、无按方案隔离的文件日志、同 cron 时间多方案叠加 + `misfire_grace_time=1800` 可能丢触发。
5. **回测层"新增方案零框架改动"不成立**：每个方案需要新增/修改 reproduction runner 文件（`daily_0529_reproduction.py` 内含 t1/t5 两套逻辑且直接 import 方案内部模块）。

### 1.4 最大架构风险

**"验证链与执行链脱钩"**：harness 验证链（static→…→backtest→api）与实盘执行链（scheduler→executor→repository）、激活链（activate→config.yaml）三者之间没有任何机器强制的衔接。一个方案是否经过验证，完全靠人遵守 SOP。单人单机现状下可控；一旦多人协作或方案数量上量，错误入库 / 未验证方案上实盘的概率会随操作次数线性上升。

### 1.5 最优先修复

**P0-1：给 ActivationGate 和 executor 加前置校验**——激活前必须查到该 `scheme_id` + 当前 `scheme_version` 的最近一次 `t_harness_runs(stage=all, status=passed)` 记录；executor 执行前校验 registry 状态而非仅 config.yaml。这是用现有表（005 已建好）即可完成的小改动，把"约定"升级为"机器强制"。

---

## 2. 当前架构理解

### 2.1 目录结构与模块职责

```
shared/      L1 唯一数据接入：data_service(只读 SELECT) / input_artifacts(唯一输入+hash) /
             calendar_service(唯一日历) / models(PredictionRecord) / versioning(code_hash)
schemes/     L2 约定式插件：t1_daily(日/T+1) t5_daily(日/T+5) weekly_5y_direct_0529(周/h6)
             每方案 config.yaml + predict.py(adapter) + core/(纯算法)
scheduler/   L3 discovery(glob config.yaml) / executor(conda 子进程+写库编排) /
             repository(实盘写库单点) / *_actuals_updater / main(APScheduler)
backend/     L4 FastAPI：12 GET(只读) + 2 POST(admin token) + StaticFiles serve 前端
backtests/   L4 _base_runner(模板) + repository(回测写库单点) + 每方案 reproduction runner
frontend/    原生 JS 因子实验室；postMessage 同源导航支持 iframe 嵌入
harness/     L5 横切：9 gates + contracts(AST) + probes(table_guard/api_probe) +
             authorization(一次性 token) + persistence(t_harness_runs 留痕) + CLI
migrations/  001~007；005=生命周期 6 张新表，006=predictions run_id UK，007=回测不可变
```

### 2.2 数据流（实盘预测）

```
APScheduler cron（每方案独立，来自 config.yaml schedule.cron）
  → executor.execute_scheme
      → create_scheme_run（t_scheme_runs，得 run_id）
      → conda forecast_env 子进程 scheme_runner → importlib schemes.{id}.predict.run
          → build_*_input_artifact（写 CSV + content_hash/schema_hash/artifact_id）
          → core 纯算法 → list[PredictionRecord] → JSON stdout
      → insert_run_predictions（纯 INSERT，带 run_id/scheme_version）
      → update_serving_pointer（UPSERT，approved 指向最新 run）
      → finish_scheme_run + write_run_log
```

### 2.3 回测流

```
python -m backtests.{scheme}_reproduction [--no-persist]
  → build_db_aligned_daily/weekly（经 shared.input_artifacts，按 canonical CSV 对齐日期）
  → 方案算法逐历史点预测
  → persist: create_backtest_run（纯 INSERT append）+ replace_backtest_predictions
    （旧月度汇总写入后续已删除）
  → 最新结果由 v_latest_backtest_run 视图表达
```

### 2.4 前后端交互流

```
前端启动 → GET /api/schemes + GET /api/metrics/{id}?tenor=…（实盘，经 serving pointer
           INNER JOIN，仅 approved）∥ GET /api/backtests/factor-lab（回测）
        → mergeFactorLabTasks 按 scheme_id 合并，月度行打 _source: backtest|live 标记
        → 数据源筛选器（全部/仅回测/仅实盘）+ 实盘分隔线
```

### 2.5 harness 流程

```
python -m harness onboard {id} --stage all
  static → input → unit → dry-run(行数 delta=0 守护) → compare(缺 benchmark 则 SKIP)
  → backtest(--no-persist + baseline diff) → api(只读探针)
副作用段（不在 all 内，fail-closed）：
  backtest --persist / live / activate ← 必须 --authorize 一次性 token（HMAC 可选）
留痕：t_harness_runs / t_harness_gate_results + reports/harness/{id}/{ts}/*.json
```

---

## 3. 业务流程闭环检查

### 第 1 步：原始方案进入平台

- **状态**：部分满足（流程靠文档，无机器化 intake）。
- **证据**：[SCHEME_ONBOARDING_SOP.md](sop/SCHEME_ONBOARDING_SOP.md) 定义 Intake 清单；`benchmarks/model_muti_0529/manifest.json` 记录原始基准（3843×877，日期范围、target 列）。
- **风险**：没有 IntakeGate；原始交付物（标的/期限/频率/回测区间/benchmark）的完整性检查靠人。原始 benchmark 的归档位置有两套约定：`benchmarks/{benchmark_id}/`（canonical 输入）与 `schemes/{id}/benchmarks/`（CompareGate 读的预测样本），新人易混淆。
- **建议**：在 SCHEME_CONTRACT 增加 intake manifest 必填字段清单，StaticGate 校验 `schemes/{id}/benchmarks/` 样本文件存在性（结合第 3 步建议）。
- **优先级**：P2。

### 第 2 步：方案改造进入框架

- **状态**：已满足。
- **证据**：三个 `predict.py` 均符合 `SCHEME_ID + run(predict_date)->list[PredictionRecord]` 契约（t1_daily/predict.py:30、t5_daily/predict.py:23、weekly_5y_direct_0529/predict.py:45）；输入均经 `build_*_input_artifact`；StaticGate 机器校验目录/契约/危险 import（harness/gates/static_gate.py）。
- **风险**：`predict.py` 直接调用 `data_service.create_sqlalchemy_engine()` 再传入 input_artifacts（三方案同模式），adapter 持有裸 engine，理论上可绕过 artifact 机制直接读库——当前未发生，但无硬屏障（即 CODE_ARCHITECTURE §4 V3 的遗留）。`t5_daily/latest_prediction.py` 位于方案根目录而非 core/，属于灰色层（被 backtest runner 直接 import）。
- **建议**：engine 工厂下沉到 `input_artifacts` 内部，predict.py 白名单移除 `data_service`；`latest_prediction.py` 归入 core/ 或在 SCHEME_CONTRACT 明确其地位。
- **优先级**：P2（StaticGate 已覆盖主要逃逸面）。

### 第 3 步：回测 benchmark 复现对比

- **状态**：部分满足。
- **证据**：CompareGate 严格容差（方向匹配率必须 1.0、confidence ≤1e-8、metric ≤0.001，compare_gate.py:14-17）；BacktestGate baseline diff（backtest_gate.py:72-97）；CURRENT_STATUS 记录 weekly 方案 S5 方向差异 0。
- **风险**：**CompareGate 在 `schemes/{id}/benchmarks/original_predictions_sample.csv` 缺失时 SKIPPED 且 passed=True，不阻断 `--stage all`**。即"平台改造是否保持算法行为一致"这一灰度实验室的核心验证，可以被静默跳过。BacktestGate 首跑自举 baseline（自己跟自己比），首个 baseline 的正确性无 gate 担保。
- **建议**：config.yaml 增加 `benchmark.required: true` 字段；为 true 时 CompareGate SKIP 改 FAIL。BacktestGate 自举时在 evidence 中显式标注 `bootstrap=true` 并要求人工确认。
- **优先级**：**P1**。

### 第 4 步：回测结果写库

- **状态**：已满足。
- **证据**：`persist_run_output()`（_base_runner.py:597-623）只经 `backtests.repository` 写 `t_backtest_runs/_predictions`；007 后 append-only，重跑不覆盖（create_backtest_run 纯 INSERT，repository.py:86-142）；`--persist` 经 harness 时需 `backtest_persist` token。
- **风险**：直接运行 runner（不经 harness、不带 `--no-persist`）即写库，授权可绕过；deprecated 的 `upsert_backtest_run()`（repository.py:40-83）在 scope UK 被 007 删除后**失去去重能力，已变成纯追加**，留着是误用陷阱。
- **建议**：删除 `upsert_backtest_run` 与 `scheduler.upsert_predictions` 两个 deprecated 函数；runner main() 默认 `--no-persist`、写库需显式 `--persist`（默认安全）。
- **优先级**：P1（删 deprecated 为 P1，默认安全为 P1）。

### 第 5 步：后端读取

- **状态**：已满足。
- **证据**：§4.5 路由清单；实盘读路径经 serving pointer `INNER JOIN … WHERE serving_status='approved'`（services.py:487-507）；回测读取后续已统一为 latest run 明细动态聚合。
- **风险**：无独立的 run 状态 / run_log 查询 API（仅 `/api/schemes` 透出 `last_run`），失败任务无法在前端定位（与第 10 步合并看）。
- **优先级**：P1。

### 第 6 步：前端展示（回测）

- **状态**：已满足。
- **证据**：`GET /api/backtests/factor-lab` 数据驱动矩阵；`verify_frontend_db` 三方案逐格 0 mismatch（CURRENT_STATUS 2026-06-10）。
- **风险**：见 §4.6 的少量硬编码。
- **优先级**：P2。

### 第 7 步：实盘预测调度

- **状态**：部分满足。
- **证据**：每方案独立 cron（config.yaml `schedule.cron` → main.py:143-154）；`max_instances=1`、`coalesce=True`、`misfire_grace_time=1800`；周频自动 `force=True` 跳过交易日检查；新增周频方案无需改 main.py（已被 weekly_5y_direct_0529 验证）。
- **风险**：失败**无重试、无告警**（executor.py:142-155 捕获后只写日志和 run_log）；全串行 + 单方案 600s 超时；几十个方案同 cron 时间叠加超过 30 分钟会**静默丢触发**；无健康检查端点。
- **建议**：见 §6.3 Scheduler 专项。
- **优先级**：P1。

### 第 8 步：实盘结果持久化

- **状态**：已满足。
- **证据**：`insert_run_predictions` 纯 INSERT + run_id UK，同日多 run 共存不覆盖；serving pointer UPSERT 指向最新（repository.py:264-299）；`t_scheme_runs` 记录 started/finished/records_expected/returned/written/error_message。
- **风险**：重复执行产生冗余行（设计上接受，靠 pointer 隔离展示）；serving pointer 的 `approved` 状态当前由 executor 自动写入——"approved"无独立审批动作，语义上是"最新即批准"。灰度场景可接受，但若未来需要人工审核预测再发布，需要把 approve 拆为独立操作。
- **优先级**：P2（记录语义即可）。

### 第 9 步：前端实盘对比

- **状态**：已满足。
- **证据**：数据源筛选器（index.html:48-52）+ `_source` 标记 + 实盘分隔线/趋势图虚线（aifin-shell.js:1397-1449）；待验证（actuals 未回填）显示"待验证"且不计入准确率（aifin-shell.js:449, 964-977）。
- **风险**："全部"口径下回测与实盘在同一表/图连续展示，仅靠分隔线区分（已有筛选项兜底，风险低）。
- **优先级**：P2。

### 第 10 步：日志与追溯

- **状态**：部分满足。
- **证据**：DB 三层留痕（t_scheme_runs / t_scheme_run_log / t_harness_runs+gate_results）；预测记录含 run_id/scheme_version，经 t_scheme_runs 可达 input_artifact 与 harness_run；harness 报告 JSON 落 `reports/harness/{id}/{ts}/`。
- **风险**：
  - 文件日志不按方案隔离（全部进 `logs/com.bond-factor-lab.scheduler.log`，靠 grep scheme_id）；
  - `t_scheme_run_log` 无 started_at/finished_at（在 t_scheme_runs 有，两表口径不一致）；
  - 失败任务无 API/前端入口，定位需登录机器查 DB 或日志文件。
- **建议**：增加 `GET /api/runs?scheme_id=&status=failed` 只读端点；scheduler 日志加按方案的结构化字段（或每方案独立 FileHandler）。
- **优先级**：P1（API）/ P2（日志文件隔离）。

---

## 4. 分模块 Review

### 4.1 shared/

- **职责清晰**：是。data_service 全只读（无任何 INSERT/UPDATE/DELETE，写出的是 CSV 文件）；input_artifacts 含完整 hash 链（content_hash/schema_hash/artifact_id，input_artifacts.py:82-92, 219-240）；calendar_service 单点日历；versioning 计算 code_hash→scheme_version（前 12 位）。
- **越界**：无上行依赖。
- **主要问题**：
  1. `scheme_version` 只覆盖 `predict.py + core/**/*.py`（versioning.py:7-14），**config.yaml 变更不改变 scheme_version**（config_hash 单独入 t_scheme_versions 但不参与版本号）。改 tenors/horizon 而代码不变时，预测记录的 scheme_version 不变，追溯有盲区。
  2. `create_sqlalchemy_engine` 是公开符号，被 adapter 直接调用（V3 遗留）。
- **建议**：scheme_version 改为 `hash(code_hash + config_hash)[:12]`；engine 工厂收敛。
- **优先级**：P1（版本号含 config）/ P2（engine 收敛）。

### 4.2 schemes/

- **职责清晰**：是。三方案契约一致、无跨方案 import、core 零 DB/零网络（grep + AST 双重证实）。
- **隐式副作用**：`t5_daily/core/predict_*.py` 的 `main()` 含 `to_csv` 写文件（仅 `__main__` 触发，运行时 import 安全；StaticGate 的 file_write 检查是否豁免 `__main__` 块未在当前代码中确认——若不豁免则说明这些写发生在已入库方案上未被拦截，建议复核）。
- **多方案扩展**：目录结构支持几十个方案；输入 artifact / 临时文件按 scheme_id 隔离。
- **主要问题**：`t5_daily/latest_prediction.py` 游离于 adapter/core 之外且被回测 runner 直接 import，是方案内分层的特例。
- **优先级**：P2。

### 4.3 backtests/

- **职责清晰**：是。只写 `t_backtest_*`，绝不触碰实盘表。
- **主要问题**：
  1. **每方案一个 runner 文件、且 runner 直接 import 方案内部模块**（daily_0529_reproduction.py:275-303 import `schemes.t5_daily.latest_prediction`；:433-446 import `schemes.t1_daily.core.config`）。"新增方案零框架改动"在回测层不成立——几十个方案意味着几十个手写 runner，每个都是行为一致性风险点。
  2. `daily_0529_reproduction.py` 一个文件承载 t1/t5 两个方案（约 660 行），与"每 runner 以 scheme_id 命名"的约定（HARNESS_ARCHITECTURE §2）已经偏离。
  3. deprecated `upsert_backtest_run` 在 007 后语义已变（见第 4 步）。
- **建议**：抽象 `BacktestSpec` 驱动的通用 runner 入口（`python -m backtests.run --scheme-id X`），方案侧只声明 spec + `predict_rows` 回调（可放 `schemes/{id}/backtest.py` 并纳入 StaticGate 扫描）；拆分 daily_0529 为 per-scheme 文件。
- **优先级**：P1（平台化前）。

### 4.4 scheduler/

- **职责清晰**：是。发现（glob config.yaml + status 过滤）→ 子进程执行 → 写库编排，对具体方案零静态耦合。
- **主要问题**（证据见 §3 第 7 步）：
  1. 串行执行、无重试、无告警；
  2. 生命周期单向：activate CLI 只支持 paused→active（activate_gate.py:120-134），**无 pause/retire 命令**；状态事实源是 config.yaml 文本文件，手工编辑无管控、registry 被 sync 覆盖；
  3. `t_scheme_registry.status ENUM('active','paused','archived')` 与 `t_scheme_versions.status ENUM('draft','validated','shadow','active','paused','retired')` 两套状态机不一致，含义未对齐；
  4. actuals updater 异常直接抛到 APScheduler，无重试与告警。
- **优先级**：P1。

### 4.5 backend/

- **职责清晰**：是。12 个 GET 全只读（有测试断言），2 个 POST 受 `require_admin_token` 保护，无 PUT/DELETE。
- **隐式副作用**：无（startup 时 registry sync 一次，非 GET 触发；sync 有 config 签名缓存防重复写，services.py:216-224）。
- **主要问题**：
  1. `BOND_ADMIN_TOKEN` 未配置时 POST 放行（软默认，main.py:49-62）——绑定 127.0.0.1 下单机可接受，平台化前必须改 fail-closed；
  2. 缺 run 状态查询 API；
  3. 周频判定 `_is_weekly_metric: horizon==6 or extra.frequency=='weekly'`（services.py:629-635）——**horizon==6 是魔数**，若未来出现 horizon=6 的日频方案（T+6）会被误判为周频。frequency 应作为一等字段（prediction extra 或表列）而非靠 horizon 推断。
- **优先级**：P1（1、3）/ P1（2）。

### 4.6 frontend/

- **职责清晰**：是。数据驱动，新增方案自动出现。
- **主要问题（硬编码清单）**：
  1. `factorTaskColumns` 硬编码三列 T+1/T+5/周度（aifin-shell.js:183-186）——新增 horizon（T+10、月频）需要改前端；
  2. `SCHEME_DEPLOYMENT_DATE_OVERRIDES = {weekly_5y_direct_0529: "2026/06/10"}`（aifin-shell.js:261-263）——**硬编码 scheme_id 特例**，每新增方案都可能要再加一条，违反"零前端改动"；根因是 API 不返回部署时间；
  3. 前端读取 `deploymentDate/remark` 等字段但 API 不存在（aifin-shell.js:331-348），靠 mock 占位；
  4. mock 降级数据（factorDailyBaseRows 等）在 API 失败时展示假数据——有 "实时API暂不可用" 提示兜底，但 mock 行残留是误读风险。
- **建议**：`/api/schemes` 返回 `deployed_at`（取 t_scheme_registry.created_at 或首条 live run 时间）与 `remark`，删除前端 override 与 mock 行；任务列改由 `frequency+horizon` 数据驱动生成。
- **优先级**：P1（2、3）/ P2（1、4）。

### 4.7 harness/

- **职责清晰**：是。9 gate + AST contracts + table_guard + 一次性 token + DB 留痕，链路完整。
- **主要问题**：
  1. **ActivationGate 不校验前置 gate 结果**（activate_gate.py 只验 token + config schema）——验证链与激活链脱钩（最大风险，见 §1.4）；
  2. **强制性缺失**：scheduler/backtests/config.yaml 三条绕过路径全开放（见 §6.4）；
  3. StaticGate 漏网面：`importlib.import_module`、`exec/eval`、`subprocess.run`（限定调用形式）、`ctypes` 不在黑名单（import_rules.py:10-38）；
  4. dry-run 行数守护只盯 `t_scheme_predictions/t_scheme_run_log` 两张表（dry_run_gate.py:7），dry-run 期间写 serving_pointer/t_scheme_runs/actuals 不会被发现；
  5. 一次性 token 的已用记录存 `reports/harness/.used_authorization_tokens.json`（gitignore 目录），删除该文件即可重放 token；`HARNESS_AUTH_SECRET` 未配置时 token 为明文 base64（软默认）。
- **优先级**：P0（1）/ P1（2、4、5）/ P2（3）。

### 4.8 数据库 schema / repository 层

- **区分维度**：`scheme_id + target_tenor + horizon + predict_date/target_date` 齐备；frequency 在 registry 有、predictions 表无（靠 extra JSON + horizon 推断，见 §4.5 问题 3）。
- **回测/实盘隔离**：物理隔离成立。
- **重跑覆盖风险**：实盘（run_id UK append）与回测（007 append + 视图）均已消除；唯二残留是两个 deprecated upsert 函数。
- **追溯字段**：run_id/scheme_version 直接、input_artifact/harness_run 两跳 JOIN 可达——**问题中列出的 run_id / scheme_version / input_artifact_hash / harness_run_id 四项均无需再加列**，现有模型已够用（直接列冗余为可选优化）。
- **遗留不一致**：`t_scheme_run_log` 与 `t_scheme_runs` 功能重叠（旧表缺时间戳/scheme_version，新表全有）；两套 status ENUM 不一致；`apply_migrations.py` 无 schema_migrations 版本表（幂等靠每个迁移的条件 ALTER 自觉）。
- **优先级**：P2（合并 run_log 语义或文档化双表分工）/ P2（迁移版本表）。

### 4.9 配置文件与运行脚本

- launchd 双进程（backend:8100 仅 127.0.0.1 / scheduler）+ KeepAlive，部署面清晰。
- `scripts/` 中 `apply_migrations.py` 可直接 DDL 写库、`backfill_live_predictions.sh` 未审查（**未在当前代码中确认其写库范围**）、postonboard 系列只写 reports 文件。脚本不受 harness 管控，符合"受控 admin 脚本"的文档定位，但无任何运行留痕（不写 t_harness_runs）。
- **建议**：admin 脚本统一在执行时向 `t_harness_runs(stage='admin_script')` 留一条记录；审查并文档化 backfill 脚本。
- **优先级**：P2。

---

## 5. 关键风险清单

| 编号 | 风险描述 | 影响流程 | 严重等级 | 代码位置 | 修复建议 | 优先级 |
|------|----------|----------|----------|----------|----------|--------|
| R1 | ActivationGate 不校验前置 gate 通过记录，可激活未验证方案 | 实盘挂载 | **Critical** | harness/gates/activate_gate.py:24-109 | 激活前查询 t_harness_runs/t_harness_gate_results 最近一次 stage=all 全 PASS 且 scheme_version 匹配 | **P0** |
| R2 | scheduler / backtest runner / config.yaml 可完全绕过 harness 写库与激活 | 全部 | High | scheduler/main.py:89-166；backtests/*_reproduction.py main()；schemes/*/config.yaml | executor 执行前校验 registry+versions 状态；runner 默认 --no-persist；config.status 改由 activate 流程专管并审计 | P1 |
| R3 | CompareGate 缺 benchmark 时 SKIP 视同通过，行为一致性验证可被静默跳过 | 回测复现 | High | harness/gates/compare_gate.py（SKIPPED passed=True）；orchestrator --stage all | config 增加 benchmark.required，required 时 SKIP→FAIL | P1 |
| R4 | 调度无重试/无告警/全串行；多方案同刻触发超 30 分钟静默丢触发 | 实盘调度 | High | scheduler/main.py:150-152（max_instances/coalesce/misfire）；executor.py:142-155 | 失败重试 N 次 + 告警钩子（邮件/webhook）；线程池并行；错峰 cron | P1 |
| R5 | BOND_ADMIN_TOKEN / HARNESS_AUTH_SECRET 软默认（未配置即放行/明文 token） | API 写操作、授权段 | High（多用户场景）/ Medium（单机） | backend/main.py:49-62；harness/authorization.py:36-41 | 平台化前改 fail-closed：未配置即拒绝写操作 | P1 |
| R6 | 每方案手写 backtest runner 且直接 import 方案内部，回测层无插件化 | 回测复现、扩展性 | High | backtests/daily_0529_reproduction.py:275-303, 433-446 | 通用 runner 入口 + 方案侧 spec/回调，纳入 StaticGate | P1 |
| R7 | horizon==6 魔数判定周频；frequency 不是 predictions 一等字段 | 指标计算、前端 | High | backend/services.py:629-635 | t_scheme_predictions 加 frequency 列（或强制 extra.frequency 必填并以其为准） | P1 |
| R8 | 无 run 状态/失败任务查询 API，失败定位靠登机查日志 | 日志与追溯 | Medium | backend/main.py（无 /api/runs） | 增加只读 GET /api/runs（读 t_scheme_runs） | P1 |
| R9 | 前端硬编码 SCHEME_DEPLOYMENT_DATE_OVERRIDES（按 scheme_id 特判）与 mock 降级数据 | 前端展示 | Medium | frontend/aifin-shell.js:261-263, 205-253 | API 返回 deployed_at/remark；删 override 与 mock 行 | P1 |
| R10 | deprecated upsert 函数在新 UK 下语义已变（失去去重/可产生 NULL run_id 行） | 写库 | Medium | scheduler/repository.py:122-154；backtests/repository.py:40-83 | 直接删除两个函数及引用 | P1 |
| R11 | scheme_version 不含 config_hash，改配置不改版本号 | 可追溯 | Medium | shared/versioning.py:32-34 | 版本号 = hash(code_hash+config_hash) | P1 |
| R12 | dry-run 行数守护仅覆盖 2 张表 | 入库验证 | Medium | harness/gates/dry_run_gate.py:7 | 守护表扩为 PROTECTED_TABLES 全集（允许 0 delta 断言） | P1 |
| R13 | 一次性 token 已用记录存本地可删文件；token 明文软默认 | 授权 | Medium | harness/authorization.py:183-210 | 已用 nonce 落 DB 表；与 R5 一并收紧 | P2 |
| R14 | StaticGate 漏网：importlib/exec/eval/subprocess.run/ctypes | core 纯净 | Medium | harness/contracts/import_rules.py:10-38 | 黑名单补 importlib/exec/eval/ctypes 与限定调用 | P2 |
| R15 | 文件日志不按方案隔离；t_scheme_run_log 缺时间戳与 t_scheme_runs 口径不一 | 排错效率 | Medium | scheduler/main.py:178；migrations/001:52-62 | 结构化日志/每方案 handler；文档化双表分工或合并 | P2 |
| R16 | 调度器串行 + 600s 单方案超时，几十方案的容量未压测 | 扩展性 | Medium | scheduler/executor.py:60；main.py:110-121 | 并行池 + 容量压测基准 | P2 |
| R17 | 无 schema_migrations 版本表，迁移幂等靠条件 ALTER 自觉 | 运维 | Low | scripts/apply_migrations.py | 加版本记录表 | P2 |
| R18 | 状态机双 ENUM 不一致（registry 3 态 vs versions 6 态） | 生命周期 | Low | migrations/001:46；005:12 | 统一状态机定义并文档化映射 | P2 |

---

## 6. 专项检查

### 6.1 数据库与数据流专项

- **当前是否满足**：满足。维度区分完整、回测/实盘物理隔离、不可变 append 模型、追溯链 ≤2 跳 JOIN 全通。
- **最大问题**：frequency 不是 predictions 一等字段（R7）；deprecated upsert 残留（R10）。
- **必须补齐**：predictions 表 frequency 列（或强制 extra.frequency）；删 deprecated 函数。
- **验收标准**：任取一条 t_scheme_predictions 能在一条 SQL 内 JOIN 出 run/version/artifact/harness 四元信息；代码库 grep 无 `upsert_predictions|upsert_backtest_run` 引用；horizon=6 的日频假方案在 metrics API 中不被算成周频（新增单测）。

### 6.2 前后端契约专项

- **当前是否满足**：基本满足。GET 全只读有测试守护；回测/实盘端点分离、`_source` 标记清晰；新增方案零前端改动在主路径成立。
- **最大问题**：deployment date 按 scheme_id 硬编码 override（R9）——这是"零前端改动"承诺上已经发生的第一道裂缝。
- **必须补齐**：`/api/schemes` 返回 deployed_at/remark；删除 override 与 mock 残留；失败/运行状态在前端可见（依赖 R8 的 /api/runs）。
- **验收标准**：新增第 4 个方案完整入库后，`git diff frontend/` 为空仍能完整展示（含部署时间）；关闭后端时前端不出现任何 mock 数值。

### 6.3 Scheduler 专项

- **当前是否满足**：当前 3 方案规模满足；平台化不满足。
- **最大问题**：失败无重试无告警（R4）；生命周期管理单向且事实源是可随意手改的 config.yaml（R2 一部分）。
- **必须补齐**：重试 + 告警钩子；pause/retire CLI；并行执行池；/api/runs 暴露状态。
- **验收标准**：人为让某方案抛异常后，重试 N 次并产生一条告警记录，且不影响其它方案当日执行；20 个 mock 方案同刻触发时全部在 grace time 内完成且无丢触发（压测脚本断言）。

### 6.4 Harness 与 SOP 专项

- **当前是否满足**：作为"可运行的验证工具链"满足；作为"强制流程"不满足——harness 是约定/治理层，不是屏障。三条绕过路径（直接跑 scheduler、直接跑 backtest runner --persist、手改 config.yaml/registry）全部开放，且 ActivationGate 无前置门级联（R1）。
- **最大问题**：R1（验证链与激活链脱钩）。
- **必须补齐**：
  1. ActivationGate 激活前强制查 t_harness_gate_results（当前 scheme_version 的 static…api 全 PASS）；
  2. executor 执行前校验"该 scheme_id 在 t_scheme_registry 为 active 且 t_scheme_versions 中当前 code_hash 对应版本状态为 active"——手改 config.yaml 但版本未走 activate 流程时拒绝执行；
  3. backtest runner 默认 --no-persist；
  4. dry-run 守护表扩面（R12）。
- **验收标准**：对一个从未跑过 onboard 的新方案直接执行 `python -m harness activate --authorize <token>` 必须 FAIL 并指出缺失的 gate 记录；手工把 config.yaml 改成 active（不走 activate）后，scheduler 下一次执行该方案被拒绝并写 run_log(status=skipped, reason=version_not_activated)。

---

## 7. 修改路线图

### P0（必须立刻修复）

| # | 修改目标 | 涉及模块 | 具体修改点 | 验收标准 |
|---|----------|----------|------------|----------|
| P0-1 | 激活前置门级联（消 R1） | harness/gates/activate_gate.py、harness/persistence.py | ActivationGate 增加 `_verify_gate_history()`：按 scheme_id + 当前 compute_scheme_version 查 t_harness_runs/t_harness_gate_results，要求最近一次 stage=all 的 static/input/unit/dry_run/backtest/api 全 passed（compare 允许显式 SKIP 仅当 benchmark.required=false）；查不到或版本不匹配 → FAILED | §6.4 验收标准第 1 条；新增单测覆盖"无记录 / 版本漂移 / 全通过"三分支 |

### P1（平台化前必须修复）

| # | 修改目标 | 涉及模块 | 具体修改点 | 验收标准 |
|---|----------|----------|------------|----------|
| P1-1 | 执行链状态校验（消 R2 主体） | scheduler/executor.py、scheduler/discovery.py | execute_scheme 前比对 config.status、registry.status 与 t_scheme_versions 当前版本状态三者一致且为 active，否则 skipped 并写明 reason | §6.4 验收标准第 2 条 |
| P1-2 | CompareGate 必需化（消 R3） | harness/contracts/config_schema.py、compare_gate.py | config 增加 `benchmark.required`；required 时缺样本 FAIL | required=true 且无样本时 `--stage all` 退出码非 0 |
| P1-3 | 调度健壮性（消 R4） | scheduler/executor.py、main.py | 失败重试（可配次数/间隔）+ 告警钩子（env 配 webhook/命令）；执行改线程池；模板 cron 错峰指引 | §6.3 验收标准 |
| P1-4 | 鉴权 fail-closed（消 R5/R13） | backend/main.py、harness/authorization.py | 未配置 BOND_ADMIN_TOKEN 时 POST 返回 403；HARNESS_AUTH_SECRET 必配（或生成持久本机密钥）；已用 nonce 落 DB | 未配置 token 时 trigger/sync 返回 403 的单测 |
| P1-5 | 回测 runner 插件化（消 R6） | backtests/、schemes/*/backtest.py（新约定）、harness/gates/backtest_gate.py、static_gate | 通用入口 `python -m backtests.run --scheme-id`；方案侧声明 spec+predict_rows；runner 默认 --no-persist；StaticGate 扫描 backtest.py | 新增 mock 方案跑通回测且 `git diff backtests/` 为空 |
| P1-6 | frequency 一等化（消 R7） | migrations/008、scheduler/repository.py、backend/services.py | predictions 加 frequency 列并回填；metrics 判定改读列 | horizon=6 日频假方案不再被判周频（单测） |
| P1-7 | 运行状态 API + 前端字段（消 R8/R9） | backend/main.py、services.py、frontend/aifin-shell.js | GET /api/runs（只读 t_scheme_runs）；/api/schemes 返回 deployed_at/remark；删前端 override | §6.2 验收标准 |
| P1-8 | 清理 deprecated 写库函数（消 R10） | scheduler/repository.py、backtests/repository.py | 删除 upsert_predictions / upsert_backtest_run | grep 无引用；全测试通过 |
| P1-9 | 版本号含 config（消 R11） | shared/versioning.py | scheme_version = sha256(code_hash+config_hash)[:12] | 改 config.yaml 任一字段后 discovery 产出新 scheme_version（单测） |
| P1-10 | dry-run 守护扩面（消 R12） | harness/gates/dry_run_gate.py | DRY_RUN_GUARD_TABLES → table_guard.PROTECTED_TABLES 全集 | dry-run 写 serving_pointer 的注入测试被捕获 |

### P2（后续优化）

| # | 修改目标 | 涉及模块 |
|---|----------|----------|
| P2-1 | StaticGate 黑名单补 importlib/exec/eval/ctypes/限定 subprocess 调用 | harness/contracts/import_rules.py |
| P2-2 | engine 工厂下沉，predict.py 白名单移除 data_service（消 V3 残留） | shared/input_artifacts.py、schemes/*/predict.py |
| P2-3 | 日志按方案隔离（结构化字段或独立 handler）；t_scheme_run_log 与 t_scheme_runs 分工文档化或合并 | scheduler/ |
| P2-4 | schema_migrations 版本表 | scripts/apply_migrations.py |
| P2-5 | 状态机统一（registry 3 态 vs versions 6 态映射） | migrations、docs |
| P2-6 | 前端任务列由 frequency+horizon 数据驱动；清 mock 残留 | frontend/ |
| P2-7 | admin 脚本运行留痕（t_harness_runs stage=admin_script）；审查 backfill_live_predictions.sh | scripts/ |
| P2-8 | IntakeGate / benchmark 样本存在性静态检查 | harness/、docs/SCHEME_CONTRACT.md |
| P2-9 | 调度容量压测基准（20+ mock 方案） | tests/ |

---

## 8. 最终验收清单

| 验收项 | 是否必须 | 当前状态 | 验收标准 | 备注 |
|--------|----------|----------|----------|------|
| 三不变量（输入/写库/core）静态守护 | 必须 | ✅ 已满足 | StaticGate 对违规注入样例 FAIL | 已有测试 |
| 回测/实盘表物理隔离 | 必须 | ✅ 已满足 | 两 repository 零交叉；LiveGate delta 校验通过 | — |
| 重跑不覆盖（实盘+回测） | 必须 | ✅ 已满足 | 同日双跑产生 2 个 run_id，pointer 指向最新；回测 append | scratch DB 已验证 |
| 预测记录追溯 run/version/artifact/harness | 必须 | ✅ 已满足（≤2 跳 JOIN） | 单条 SQL JOIN 全通 | 直接列冗余为可选 |
| GET 全只读 | 必须 | ✅ 已满足 | 测试断言不触发写 | — |
| **激活必须有 gate 通过记录** | **必须** | ❌ 不满足 | 无记录/版本漂移时 activate FAIL | **P0-1** |
| **执行链校验激活状态** | **必须** | ❌ 不满足 | 手改 config 未走 activate 时 executor 拒绝 | P1-1 |
| benchmark 复现强制（required 时不可 SKIP） | 必须 | ◑ 部分满足 | required=true 缺样本即 FAIL | P1-2 |
| 调度失败重试 + 告警 | 必须 | ❌ 不满足 | 注入失败触发重试与告警记录 | P1-3 |
| 写操作鉴权 fail-closed | 必须 | ◑ 部分满足（软默认） | 未配 token 即 403 | P1-4 |
| 回测层新增方案零框架改动 | 必须（平台化） | ❌ 不满足 | 新方案回测 `git diff backtests/` 为空 | P1-5 |
| frequency 一等字段 | 必须 | ◑ 部分满足（horizon 魔数） | metrics 判定不依赖 horizon==6 | P1-6 |
| 失败任务可在 API/前端定位 | 必须 | ❌ 不满足 | GET /api/runs 可筛 failed | P1-7 |
| 前端零硬编码 scheme_id | 必须 | ◑ 部分满足（1 处 override） | grep 前端无 scheme_id 字面量 | P1-7 |
| deprecated 写库函数清零 | 必须 | ❌ 不满足 | grep 无引用 | P1-8 |
| scheme_version 覆盖 config | 必须 | ❌ 不满足 | 改 config 产生新版本号 | P1-9 |
| dry-run 守护全保护表 | 必须 | ◑ 部分满足（2/14 表） | 全表 delta=0 断言 | P1-10 |
| 日志按方案可检索 | 建议 | ◑ 部分满足（grep 可用） | 结构化字段或独立文件 | P2-3 |
| StaticGate 动态逃逸面收口 | 建议 | ◑ 部分满足 | importlib/exec 注入样例 FAIL | P2-1 |
| 容量压测（几十方案） | 建议 | ❌ 未做 | 20 方案同刻无丢触发 | P2-9 |
| 迁移版本表 | 建议 | ❌ 不满足 | schema_migrations 存在 | P2-4 |

---

## 附：本次取证范围与未确认项

- 取证覆盖：shared/ schemes/ backtests/ scheduler/ backend/ frontend/ harness/ migrations/ scripts/ deploy/ tests/ 全部 Python/SQL/JS 源文件（AST/grep/逐文件阅读），docs/ 全部现行文档。
- **未在当前代码中确认**：
  1. `scripts/backfill_live_predictions.sh` 的写库范围与保护措施（shell 脚本未逐行审查）；
  2. StaticGate 的 file_write 检查是否豁免 `__main__` 块（t5_daily core 的 `to_csv` 为何通过 gate 待复核）；
  3. CompareGate 所依赖的 `schemes/{id}/benchmarks/current_predictions_sample.csv` 的生成责任方（人工放置还是 gate 自动生成）；
  4. 运行中 MySQL 实例的实际表结构与迁移文件的一致性（本次仅审查迁移 SQL 文本，未连库 DESCRIBE）。
