# Bond Factor Lab — P1 数据模型重构 任务说明（subagent 串行派发版）

> 前置：P0 已全部落地并合入 `codex/p1-runner-factorlab-slim`（一条直线，95/95 测试通过）。
> 本文件是 P1 的可派发任务说明，配套总计划见 [bond_factor_lab_execution_plan.md](bond_factor_lab_execution_plan.md) §2、评审依据见 [bond_factor_lab_architecture_review.md](bond_factor_lab_architecture_review.md) §7。

---

## 0. 派发规则（与 P0 不同，务必先读）

**P1 不能像 P0 那样并行。** S1→S7 共享 `migrations/`、`shared/models.py`、`scheduler/repository.py`、`scheduler/executor.py`、`backtests/repository.py`、`backend/services.py`，naive 并行会撞车。派发方式二选一：

- **方式 A（推荐）**：一个 subagent 串行做完 S1→S7，每步独立提交。最稳。
- **方式 B**：分步派发，但必须按依赖顺序串行——派发 S(n) 前，S(n-1) 必须已合并。每个任务卡已标注 `依赖`。

### 三条不可破坏的不变量（P0 已建立，P1 必须守住）

1. **写库单点**：新表/新列的写入只能加在 `scheduler/repository.py`、`backtests/repository.py`、`*_actuals_updater`。其它层（core/predict/backend GET/harness gate）零写库。
2. **core 纯净**：`schemes/*/core/**` 零 DB、零写库、零跨方案 import。P1 不碰 schemes。
3. **GET 只读**：backend 的 GET 接口不得写库（P0 已修，S7 切读路径时不能回退）。

### 通用验收要求

- 每步带迁移的 SQL 用 `IF NOT EXISTS`、InnoDB、`utf8mb4_0900_ai_ci`，编号续 `005`、`006`…，风格对齐 `migrations/004_weekly_actuals.sql`。
- 迁移必须**先在测试库验证**再标记完成；提供 `python scripts/apply_migrations.py`（已存在）的执行说明。
- 每步带单测，跑 `<service_env_python> -m unittest discover -s tests -p "test_*.py"` 全绿（service env：`/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python`，跑测试**不设** `HARNESS_AUTH_SECRET`/`BOND_ADMIN_TOKEN` 以验证软默认）。
- **不得**改 `harness/`、`schemes/`、`frontend/`，除非任务卡显式列出。
- subagent 不执行 git；由编排者提交（若你让单 agent 串行，可允许其每步提交，但提交信息须按下方约定并带 `Co-Authored-By`）。

### 现状基线（钉死，避免写错）

- 迁移现有：`001_init / 002_backtests / 003_target_registry / 004_weekly_actuals`。
- `t_scheme_predictions` 当前 UK = `(scheme_id, target_tenor, predict_date)`，写入走 `scheduler/repository.py: upsert_predictions()`（覆盖式 `ON DUPLICATE KEY UPDATE`）。
- `t_backtest_runs` 当前 UK = `(benchmark_id, scheme_id, data_source, start_date, end_date)`，覆盖式。
- 写库函数集中在 `scheduler/repository.py`：`sync_scheme_registry / upsert_predictions / upsert_actuals / upsert_weekly_actuals / write_run_log`；回测写库在 `backtests/repository.py`。
- `scheduler/executor.py: execute_scheme()` 调 `upsert_predictions` + `write_run_log`，是预测写库的唯一调用点。
- `shared/models.py`：`PredictionRecord / ActualRecord / WeeklyActualRecord`（frozen dataclass，非 ORM）。
- `shared/input_artifacts.py: InputArtifact`（frozen dataclass）无 `content_hash`。

---

## S1 — 迁移基座（生命周期表，必须最先）

- **目标**：一次性建好 P1 所有新表 + 给现有表加列（向后兼容，旧 UK 暂留过渡），后续步骤只填充/切换，不再频繁改 schema。
- **独占文件（新增/编辑）**：`migrations/005_lifecycle.sql`（新增）、`tests/test_migration_005_lifecycle.py`（新增，校验 SQL 可解析/幂等，可用 sqlite 或对测试库 dry-run）。
- **改动**：在 `005_lifecycle.sql` 中 `CREATE TABLE IF NOT EXISTS`（字段以评审 §7 为准，下列为必需骨架）：
  - `t_scheme_versions`：`scheme_id, scheme_version, code_hash, config_hash, manifest_hash, git_commit, status(draft/validated/shadow/active/paused/retired), created_by, created_at, approved_by, approved_at`；UK `(scheme_id, scheme_version)`。
  - `t_harness_runs`：`harness_run_id(PK), scheme_id, scheme_version, stage, status, started_at, finished_at, triggered_by, project_root, git_commit, code_hash, config_hash, report_uri`。
  - `t_harness_gate_results`：`id PK, harness_run_id, gate_name, status, started_at, finished_at, summary_json JSON, report_uri`；FK→harness_runs。
  - `t_input_artifacts`：`artifact_id(PK), scheme_id, scheme_version, predict_date, frequency, data_version, artifact_uri, content_hash, schema_hash, source_watermark, row_count, min_date, max_date, created_at`；UK `(scheme_id, predict_date, content_hash)`。
  - `t_scheme_runs`：`run_id(PK), scheme_id, scheme_version, run_type(dry_run/shadow/active/manual), predict_date, status, harness_run_id, input_artifact_id, started_at, finished_at, records_expected, records_returned, records_written, error_message`。
  - `t_scheme_serving_pointer`：`scheme_id, target_tenor, predict_date, serving_run_id, serving_status(approved/hidden/deprecated), updated_by, updated_at`；UK `(scheme_id, target_tenor, predict_date)`。
  - **ALTER 现有表（加列，不删旧 UK）**：
    - `t_scheme_predictions`：加 `run_id BIGINT NULL`、`scheme_version VARCHAR(64) NULL`、`prediction_id BIGINT`（若无独立 PK 则确认现有 `id`）。**本步只加列、不改 UK**（切换在 S3）。
    - `t_backtest_runs`：加 `backtest_run_id`、`code_hash`、`config_hash`、`input_artifact_hash`、`run_mode(no_persist/persist/reproduction/comparison)`。
  - 可选视图 `v_latest_approved_predictions`（S7 会用；本步可先建空骨架或留到 S7）。
- **不做**：不删任何旧列/旧 UK；不写入数据；不改任何 .py 业务逻辑（除测试）。
- **验收**：`python scripts/apply_migrations.py` 在测试库执行成功且**可重复执行**（幂等）；新表 `SHOW TABLES` 可见；现有表 `DESCRIBE` 含新列；全套单测绿。
- **依赖**：无（最先）。**规模**：~150 行 SQL + 测试。

---

## S2 — InputArtifact 指纹 + 落 t_input_artifacts

- **目标**：让每个输入产物有可追溯指纹，并落库 `t_input_artifacts`，回答"今天和昨天为何不同"。
- **独占文件**：`shared/input_artifacts.py`、`scheduler/repository.py`（新增 artifact 写函数，守住写库单点）、`tests/test_input_artifacts.py`（扩展）、`tests/test_repository_input_artifacts.py`（新增）。
- **改动**：
  1. `InputArtifact` 增字段：`content_hash`（对 CSV 内容 sha256）、`schema_hash`（对列名+dtype 排序后 sha256）、`artifact_id`（可由 `scheme_id+predict_date+content_hash` 派生）、`source_watermark`（源表最大 trade_date/week_id）。在 `build_*_input_artifact` 产出时计算填充。
  2. `scheduler/repository.py` 增 `upsert_input_artifact(engine, artifact) -> str`（返回 artifact_id），写 `t_input_artifacts`，UK 冲突即视为同一产物（幂等）。
  3. **不在 core/predict 里写库**——指纹计算在 `shared.input_artifacts`（纯计算 OK），落库只经 repository，调用点在 executor（S3 接）。
- **验收**：同一输入两次构造 → `content_hash` 一致、`upsert_input_artifact` 幂等不重复插；改一行数据 → hash 变化；单测覆盖 hash 稳定性 + 幂等。
- **依赖**：S1（需要 `t_input_artifacts` 表）。**规模**：~80 行。

---

## S3 — run_id + 不可变预测 + serving pointer（P1 核心）

- **目标**：每次运行生成 `run_id`，预测**按 run_id 追加、不覆盖**；前端读"latest approved"经 serving pointer。
- **独占文件**：`scheduler/executor.py`、`scheduler/repository.py`、`shared/models.py`、`tests/test_repository_registry.py`（扩展）、`tests/test_executor_run_id.py`（新增）。
- **改动**：
  1. `shared/models.py`：`PredictionRecord` 增 `run_id`、`scheme_version`（可选字段，向后兼容）。
  2. `scheduler/repository.py`：
     - 新增 `create_scheme_run(engine, ...) -> run_id`（写 `t_scheme_runs`，生成 run_id）。
     - 新增 `insert_run_predictions(engine, run_id, records)` —— **INSERT 不 ON DUPLICATE 覆盖**（按 run_id 隔离，历史不可变）。
     - 新增 `update_serving_pointer(engine, scheme_id, tenor, predict_date, run_id, status='approved')`。
     - 保留旧 `upsert_predictions` 一段过渡（标 deprecated，便于回滚）。
  3. `scheduler/executor.py: execute_scheme`：流程改为 `create_scheme_run → insert_run_predictions(run_id) → update_serving_pointer → write_run_log`，run_log 关联 run_id。
  4. **过渡策略**：`t_scheme_predictions` 旧 UK 暂留；新写入填 `run_id/scheme_version`；同一 (scheme,tenor,predict_date) 多 run 共存（这要求**放宽或调整旧 UK** —— 在本步的迁移补丁 `migrations/006_predictions_runid_uk.sql` 中把 UK 改为含 run_id，**独占该新迁移文件**）。
- **验收**：同一方案同一 predict_date 跑两次 → 两条 run、预测各自保留不互相覆盖；serving pointer 指向最新 approved run；旧覆盖路径不再被 executor 调用；单测覆盖"重跑不覆盖 + pointer 更新"。
- **依赖**：S1（`t_scheme_runs`/`t_scheme_serving_pointer`）。**规模**：~150 行 + 1 迁移。
- **风险**：这是改动面最大的一步。务必保留旧函数与旧列做回滚通道，迁移分离成独立文件。

---

## S4 — scheme_version 派生 + 落 t_scheme_versions

- **目标**：统一计算 `code_hash/config_hash/manifest_hash`，注册方案版本。
- **独占文件**：`shared/versioning.py`（新增，纯计算工具）、`scheduler/discovery.py`、`scheduler/repository.py`（增 `upsert_scheme_version`）、`tests/test_versioning.py`（新增）。
- **改动**：
  1. `shared/versioning.py`：`compute_code_hash(scheme_dir)`（对 predict.py+core/**/*.py 内容 sha256）、`compute_config_hash(config.yaml)`、`compute_manifest_hash`（若无 manifest 则返回 None）。纯计算，零 DB。
  2. `scheduler/discovery.py`：发现方案时算出 scheme_version（可用 code_hash 前 12 位或语义版本），供 registry/run 关联。
  3. `scheduler/repository.py`：`upsert_scheme_version(engine, ...)` 写 `t_scheme_versions`，幂等。
  4. S3 的 `create_scheme_run` 关联当前 scheme_version。
- **验收**：改 core 文件内容 → code_hash 变化 → 新 scheme_version；config 不变则版本稳定；单测覆盖 hash 确定性。
- **依赖**：S1（`t_scheme_versions`），与 S3 协作（run 关联 version，建议 S3 后）。**规模**：~90 行。

---

## S5 — harness 运行留痕（t_harness_runs / t_harness_gate_results）

- **目标**：harness 每次 run 与每个 gate 结果落库，可审计；与授权 token 的 `harness_run_id` 打通。
- **独占文件**：`harness/report/writer.py`、`harness/orchestrator.py`（仅在 gate 结束后调用写库 hook）、`scheduler/repository.py` 或新建 `harness/persistence.py`（**写库须经批准写入点**——若放 harness 内，需在文件头声明这是 harness 控制面的留痕写入，并仅写 `t_harness_*` 两表，不碰业务表）、`tests/test_harness_persistence.py`（新增）。
- **改动**：
  1. orchestrator 在每个 gate 跑完后，把 `GateResult` 落 `t_harness_gate_results`；run 开始/结束落 `t_harness_runs`，生成 `harness_run_id`。
  2. `harness auth issue` 的 `--harness-run-id`（P0 已加参数）与此处生成的 id 对齐：可在 onboard 流程把 harness_run_id 透传给后续 auth 签发。
  3. **边界**：harness 留痕**只写 `t_harness_runs`/`t_harness_gate_results`**，绝不写业务表（评审硬规则六）。DB 不可用时降级为只写本地 JSON（现状），不阻断 gate。
- **验收**：跑一次 `onboard --stage all`（无 DB 时降级、有 DB 时落两表）；单测覆盖"gate 结果落库 + 仅写 harness 表";确认未写任何业务表（mock 业务写函数断言未调用）。
- **依赖**：S1（`t_harness_*`）。**规模**：~110 行。
- **注意**：这是 P1 里唯一动 `harness/` 的步骤，且只在 report/orchestrator 加留痕,不改 gate 判定逻辑。

---

## S6 — 回测不可变（backtest_run_id append）

- **目标**：每次回测产新 `backtest_run_id`，停止按 `(benchmark_id,scheme_id,data_source,...)` 覆盖；改 append + 最新指针/视图。
- **独占文件**：`backtests/repository.py`、`migrations/007_backtest_immutable.sql`（新增）、`tests/test_base_runner.py`（扩展）、`tests/test_backtest_repository_immutable.py`（新增）。
- **改动**：
  1. 迁移：`t_backtest_runs` 改为以 `backtest_run_id` 为不可变主记录（S1 已加列）；新增最新指针列或视图 `v_latest_backtest_run`，把"按 benchmark+scheme 取最新"从覆盖语义改为查询语义。
  2. `backtests/repository.py`：写入改为每次 INSERT 新 backtest_run_id（含 `code_hash/config_hash/input_artifact_hash/run_mode`），不再 UPDATE 覆盖；预测/月度指标按 backtest_run_id 隔离。
  3. 保留旧覆盖路径一段过渡（标 deprecated）。
- **验收**：同区间跑两次 → 两条 backtest_run，互不覆盖；"最新"查询返回最近一条;单测覆盖 append + 最新查询;**只写 `t_backtest_*`**（断言未触碰 `t_scheme_*`）。
- **依赖**：S1（`t_backtest_runs` 新列）。可与 S3 并行（不同文件/表），但建议 S3 之后以复用 run/version 心智。**规模**：~120 行 + 1 迁移。

---

## S7 — backend 读路径切换到 serving pointer

- **目标**：前端展示的每个数字可回溯 `run_id/scheme_version/input_artifact_hash`；读"latest approved"。
- **独占文件**：`backend/services.py`、`backend/main.py`（仅必要的响应字段扩展）、`tests/test_backend_api.py`（扩展）、`tests/test_backend_serving.py`（新增）。
- **改动**：
  1. `backend/services.py`：方案预测读取从直接查 `t_scheme_predictions` 改为经 `t_scheme_serving_pointer`/`v_latest_approved_predictions` 取"当前展示版本"。
  2. 准确率 JOIN 逻辑相应改为基于 serving run。
  3. 响应中附 `run_id/scheme_version/input_artifact_hash`（供前端追溯，前端是否展示由后续 P2 决定，本步只把字段透出）。
  4. **守住 GET 只读**：S7 读路径切换绝不能引入写库副作用（P0 不变量）。
- **验收**：GET 接口返回 latest approved 且不写库（mock 写函数断言未调用）；响应含追溯字段；单测覆盖"读 pointer + 无写副作用"。
- **依赖**：S3（serving pointer 有数据）、S2（artifact hash）。**最后做**。**规模**：~90 行。

---

## 依赖关系速查（派发顺序）

```
S1 (迁移基座) ─┬─> S2 (artifact 指纹)
               ├─> S3 (run_id 不可变) ──> S4 (scheme_version) 
               ├─> S5 (harness 留痕)
               └─> S6 (回测不可变)
S3 + S2 ─────────────────────────────> S7 (backend 读切换，最后)
```

- **必须最先**：S1。
- **S1 之后可各自做**（但仍建议串行，因共享 repository.py）：S2、S5、S6。
- **核心链**：S1 → S3 → S4 → S7；S7 还依赖 S2。
- **最稳做法**：单 agent 按 S1→S2→S3→S4→S5→S6→S7 串行，每步独立提交、独立验收。

## 提交信息约定（若 agent 自行提交）

```
db: <S{n} 一句话>  (例: db: S1 lifecycle migration baseline)

<要点 1-3 行>

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

## 不在 P1 范围

- 前端 ranking / calibration / 生命周期页 / 告警 → P2。
- token HMAC 强制（已是软默认，单用户够用）。
- `.env` 轮换（已确认无需）。
