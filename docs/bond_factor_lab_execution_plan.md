# Bond Factor Lab — 改造执行计划（subagent 并行派发版）

> 配套评审：[bond_factor_lab_architecture_review.md](bond_factor_lab_architecture_review.md)
> 决策：① 周度方案**彻底退役**；② 本轮 **P0 并行派发 + P1 串行子计划**。
> 编制基于对仓库的实地核验（见评审报告"review vs reality"对照表）。

---

## 0. 派发规则（务必先读）

- **文件所有权隔离**：每个任务标注"独占文件"。两个任务不得编辑同一文件，除非该文件被指定为某任务的独占文件。
- **热点文件**：`harness/cli.py`、`harness/registry.py`、`harness/config_loader.py` 只由 **任务 H1** 编辑。其它 harness 任务（H2/H3/H4）只碰各自的叶子文件，并按本计划"接口契约"写代码，由 H1 完成注册/wiring。
- **接口契约**：跨任务边界的函数签名/类名在本计划中钉死（见各任务"对外契约"段），各 agent 据此编码，合并即对齐。
- **验收**：每个任务必须带测试 + 给出 `python -m pytest <path>` 或等价复跑命令；不得把"实现一半"标记完成。
- **不在 subagent 范围**：DB 密码轮换等运维动作见 §4，由人工执行。

---

## 1. P0 并行波次（可立即全量派发）

> 8 个任务，文件互不重叠（H 系按热点规则隔离）。可一次性并行。

### 任务 PKG —— harness 一等打包
- **目标**：`harness` 作为一等模块可被 `python -m harness` 与 package 安装命中。
- **独占文件**：`pyproject.toml`
- **改动**：`[tool.setuptools.packages.find].include` 加入 `"harness*"`。顺手核对 `harness/__init__.py`、各子目录 `__init__.py` 存在。
- **验收**：`python -c "import harness"` 与 `python -m harness --help` 正常；`pip install -e .` 后 `harness` 可导入。
- **规模**：~3 行。**依赖**：无。

### 任务 DOCS —— 周度退役 + 文档/代码/产物对齐
- **目标**：消除"文档里有、代码里没有"的周度方案，达成评审 §3.2 不变量。
- **独占文件（docs）**：`docs/CODE_ARCHITECTURE.md`、`docs/TEST_MACHINE_BASELINE.md`、`docs/HISTORICAL_REPRODUCTION.md`、`docs/ARCHITECTURE.md`、`docs/HARNESS_DESIGN.md`、`docs/TEST_PLAN.md`、`docs/DEPLOYMENT.md`、`docs/DATA_LAYER_DESIGN.md`、`docs/CURRENT_STATUS.md`
- **独占目录（删除）**：`backtest_artifacts/runtime_inputs/weekly_10y_d_overlay/`、`.../weekly_5y_direct_production/`、`.../weekly_7y_cross_d_overlay/`
- **改动**：
  1. 上述 docs 中所有 `weekly_10y_d_overlay / weekly_5y_direct_production / weekly_7y_cross_d_overlay` 的"现存方案/active/paused/已注册"表述，改为**已退役/代码未实现**，或整段删除（凡声明其为可运行方案处一律去除）。保留 `t_scheme_weekly_actuals` 表本身的 schema 描述（表仍在），但标注"暂无周度方案写入"。
  2. `rm -rf` 三个 stale runtime_inputs 周度目录。
  3. 在 `docs/CURRENT_STATUS.md` 增一行说明：当前在册方案仅 `t1_daily`、`t5_daily`；周度方案如需重启按 SOP 重新入库。
- **不改**：`docs/archive/*`（归档计划，保留历史）、`docs/bond_factor_lab_architecture_review.md`、本执行计划文件。
- **验收**：`grep -rn "weekly_10y_d_overlay\|weekly_5y_direct_production\|weekly_7y_cross_d_overlay" docs/ --include=*.md` 仅在 `archive/`、review、本计划中命中；`ls backtest_artifacts/runtime_inputs/` 无 weekly_* 目录。
- **规模**：~50 行删改 + 3 个 rm。**依赖**：无。
- **注意**：`backtest_artifacts/` 已 gitignore，删除目录不进版本库，但能止住 StaticGate/discovery 的误导与本地噪音。

### 任务 EXPORT —— clean export 脚本（防泄露）
- **目标**：杜绝手工 zip 项目根导致 `.env`/`.git`/artifacts/reports 外泄（评审 §3.1）。
- **独占文件（新增）**：`scripts/export_clean_repo.sh`
- **改动**：写一个导出脚本，基于 `git archive HEAD`（天然排除 gitignore 与未跟踪文件）产出 `dist/bond-factor-lab-clean.tar.gz`，并在打包后**显式校验**产物内不含：`.env`、`.git/`、`*.pem`、`*.key`、`reports/`、`backtest_artifacts/`、`__pycache__/`、`.DS_Store`；命中即 `exit 1`。
- **验收**：`bash scripts/export_clean_repo.sh` 生成 tar 且自检通过；`tar tzf dist/...` 不含上述任何条目。
- **规模**：~40 行。**依赖**：无。
- **备注**：评审建议的 `SecretGate/PackageGate/QuarantineGate` 是 harness 内 gate，体量更大，留待 P1/P2；本轮先用脚本兜住泄露面。

### 任务 BG —— Backtest baseline 自动 bootstrap（原 B2）
- **目标**：no-persist 模式缺 baseline 时自动落地首条 baseline，而非直接 fail（当前 `backtest_gate.py` 找不到 `reports/refactor_baseline/{id}/backtest_no_persist.json` 即报错）。
- **独占文件**：`harness/gates/backtest_gate.py`、`tests/test_backtest_gate.py`（新增或追加）
- **改动**：
  1. no-persist 跑出 JSON 后，若 baseline 文件不存在：写入该 JSON 作为 baseline，gate 返回 `PASSED` 并在 evidence 标注 `baseline_bootstrapped=true`（首次确立基线）。
  2. 若 baseline 已存在：维持现有 deep-diff 比较逻辑不变。
  3. bootstrap 路径必须落在 `reports/refactor_baseline/{scheme_id}/`（已 gitignore，符合现状）。
- **对外契约**：不改 gate 构造签名、不改 `registry.py`/`cli.py`。
- **验收**：新增测试覆盖"baseline 不存在→bootstrap→PASSED"与"baseline 存在→diff"两条路径；`python -m pytest tests/test_backtest_gate.py`。
- **规模**：~30 行。**依赖**：无。

### 任务 STATIC —— StaticGate 加固（评审 §3.5 / P1#6#7，并行安全，提前到 P0）
- **目标**：堵住 `core/**` 递归、legacy 逃逸口、网络/子进程/pickle 写文件等边界漏洞。
- **独占文件**：`harness/gates/static_gate.py`、`harness/contracts/import_rules.py`、`tests/test_static_gate.py`
- **改动**：
  1. `core_dir.glob("*.py")` → `core_dir.rglob("*.py")`，覆盖 `core/**/*.py`。
  2. `legacy_*.py` 不再整体跳过：仍允许存在，但加规则——legacy 不得被 active `predict.py`/`core` import、不得访问 DB、不得写文件、不得跨方案 import（即对 legacy 施加"只读归档"约束，只是放宽其自身可包含历史实现）。
  3. 禁用集合扩充（core 与 predict）：`requests`、`urllib`、`httpx`、`socket`、`subprocess`、`os.system`、`open(...,"w"/"a"/"wb")`、`Path.write_text`、`Path.write_bytes`、`pickle.load`、`joblib.load`、`psycopg2`、`pandas.read_sql`、`shared.data_service`、`shared.db_config`、`shared.repository`、`backend`、`backtests`、跨 `schemes.*`。
  4. `predict.py` 白名单收紧为只允许：`shared.input_artifacts`、`shared.contracts`、`shared.calendar_service`/`calendar_artifacts`、`schemes.{id}.core`。
- **对外契约**：不改 gate 注册、不改 cli。仅强化判定。
- **验收**：测试覆盖递归命中、legacy 违规、新增禁用项各 1 例；`python -m pytest tests/test_static_gate.py`。
- **规模**：~80 行。**依赖**：无。
- **风险提示**：核验发现 `schemes/*/predict.py` grep 既未命中 `shared.input_artifacts` 也未命中 `shared.data_service`（可能 import 方式不同）。收紧白名单前，**先对现存 `t1_daily`/`t5_daily` 跑一遍**确认不误杀；若现状不合规，本任务需同时修正这两个 predict.py 的 import 方式（仍属本任务独占范围内的最小修补）。

### 任务 API —— GET 去写库 + trigger 鉴权 + CORS 收口（评审 §3.9 / P0#5#6）
- **目标**：GET 只读、写副作用下沉、手动触发受保护。
- **独占文件**：`backend/main.py`、`backend/services.py`、`tests/test_backend_api.py`（如有/新增）
- **改动**：
  1. 从 `list_schemes()`/`GET /api/schemes` 路径移除 `sync_registry_from_configs()` 调用。registry 同步改为：app 启动 lifespan 执行一次 + 由 scheduler 既有 `sync_scheme_registry()` 负责；另暴露 `POST /api/admin/registry/sync`（带鉴权）供人工触发。
  2. `POST /api/schemes/{scheme_id}/trigger` 增加鉴权依赖（FastAPI `Depends`，校验 admin token，从环境变量读取；缺失则 401/403）。trigger 必须写 `t_scheme_run_log` 并标注 `triggered_by`。
  3. CORS `allow_origins=["*"]` → 从环境变量读取白名单（默认本机/iframe 宿主），`allow_methods`/`allow_headers` 收敛到实际所需。
- **对外契约**：不动 DB schema（run_id/serving pointer 属 P1）；admin token 先用简单 env 校验，HMAC 升级在 P1 与 §3 token 体系统一。
- **验收**：测试覆盖"GET /api/schemes 不产生 registry 写"（mock 写函数断言未调用）、"无 token trigger 被拒"、"有 token trigger 入队并写 run_log"。
- **规模**：~90 行。**依赖**：无（与 H 系、P1 不冲突）。

### 任务 H1 —— Harness 控制面（ActivationGate 落地 + CompareGate 注册 + auth wiring）
> **唯一**可编辑 `harness/cli.py`、`harness/registry.py`、`harness/config_loader.py`、`harness/orchestrator.py` 的任务。
- **目标**：把 activate 从 fail-closed 桩升级为真 gate；把 H2 的 CompareGate 接进流水线；把 H3 的新 token 能力接到 CLI。
- **独占文件**：`harness/cli.py`、`harness/registry.py`、`harness/config_loader.py`、`harness/orchestrator.py`、`tests/test_cli_activate.py`、`tests/test_orchestrator_compare.py`
- **改动**：
  1. **ActivationGate（原 C2）**：实现 `_run_activate`/或独立 `gates/activate_gate.py`（若新建文件，该文件归 H1 独占）。流程：校验 `--authorize`（action=`activate`，经 H3 的 `verify_authorization`）→ 读取/校验 `config.yaml status` 与 cron → 将 status `paused→active` 落到 config 或激活态（DB 激活表属 P1，本轮先落 config + 写 run_log，并预留 DB 激活 hook）→ 返回 PASSED。无 token 一律 BLOCKED（fail-closed 保持）。
  2. **CompareGate 注册**：在 `registry.py` 的 gate 工厂与 stage 序列中加入 `compare`，位置按评审：`dry-run → (baseline reproduction) → compare → backtest`。`AUTO_SEQUENCE` 更新为 `["static","input","unit","dry-run","compare","backtest","api"]`（compare 缺 benchmark 时按 H2 约定 SKIP，不阻断）。
  3. **auth wiring**：`auth issue` 子命令增加 `--expires-in`（TTL 秒）、`--harness-run-id`、`--scheme-version` 透传到 H3 的 `issue_token`；保持旧参数兼容。
- **对外契约（消费 H2/H3）**：
  - H2 提供 `class CompareGate(Gate)`，stage 名 `"compare"`，构造签名 `CompareGate(context)`，无 benchmark 时其 `run()` 返回 `GateStatus.SKIPPED`（H2 若无该枚举则返回 PASSED+evidence `skipped=true`，二者择一并在 H2 卡片钉死）。
  - H3 提供 `issue_token(*, scheme_id, action, predict_date=None, scheme_version=None, harness_run_id=None, ttl_seconds=None, issued_by="harness") -> str` 与 `verify_authorization(token, *, scheme_id, action, predict_date=None) -> tuple[auth, list[str]]`，新参数全部带默认值（向后兼容）。
- **验收**：`python -m pytest tests/test_cli_activate.py tests/test_orchestrator_compare.py`；`python -m harness onboard t5_daily --predict-date <d> --stage all` 走通含 compare 段；activate 无 token BLOCKED、有 token PASSED。
- **规模**：~120 行。**依赖**：合并期需 H2 的类、H3 的函数签名（已钉死，可并行编码，最后对齐 import）。

### 任务 H2 —— CompareGate 实现（评审 Gate 9 / P0#8）
- **目标**：比较"原始方案输出 vs 平台归档后输出"，证明归档未改变算法行为。
- **独占文件（新增）**：`harness/gates/compare_gate.py`、`tests/test_compare_gate.py`
- **改动**：实现 `class CompareGate(Gate)`：
  - 读取 `schemes/{id}/benchmarks/original_predictions_sample.csv` 与 `original_backtest_summary.json`（不存在 → SKIP/PASSED+skipped）。
  - 对比维度：总条数、缺失日期/tenor、新增日期/tenor、direction 一致率、confidence max/mean 差异、核心指标差异。
  - 容差（评审 Gate 9）：direction_match_rate==100%、missing==0、extra==0、`max_confidence_abs_diff<=1e-8`、`metric_accuracy_abs_diff<=0.001`。
  - 产出 `comparison_summary.json` + `comparison_diff.csv` 到 report dir。
- **对外契约（供 H1）**：见 H1。**不得编辑** `cli.py`/`registry.py`（注册由 H1 做）。
- **验收**：测试覆盖"完全一致→PASSED"、"方向不一致→FAILED"、"无 benchmark→SKIP"；`python -m pytest tests/test_compare_gate.py`。
- **规模**：~110 行。**依赖**：与 H1 接口契约对齐。
- **备注**：现存 `t1_daily/t5_daily` 暂无 `benchmarks/` 样本，故实战默认走 SKIP；本任务先把能力补齐，benchmark 文件随方案入库时提供。

### 任务 H3 —— 授权 token 升级为 SignedAuthorizationToken（评审 §3.4.3 / P0#7）
- **目标**：HMAC 签名 + TTL + one-time + action/scheme/version/run 绑定。
- **独占文件**：`harness/authorization.py`、`tests/test_authorization.py`
- **改动**：
  1. token 载荷扩充：`action, scheme_id, scheme_version, predict_date|date_range, harness_run_id, issued_by, issued_at, expires_at, nonce`，并附 `hmac_signature`（HMAC-SHA256，密钥从环境变量 `HARNESS_AUTH_SECRET` 读取，缺失则拒绝签发/校验）。
  2. `verify_authorization` 强校验：签名一致、未过期（TTL）、未用过（沿用现有 `.used_authorization_tokens.json` one-time 机制）、action/scheme_id（及给定时 version/predict_date）绑定一致；任一不符进 errors。
  3. 保持函数名与现有调用点（`backtest_gate`/`live_gate`）兼容；新增参数带默认值。
- **对外契约（供 H1/gates）**：见 H1 段签名。
- **验收**：测试覆盖签名校验、过期拒绝、重放拒绝、action/scheme 不绑定拒绝；`python -m pytest tests/test_authorization.py`。
- **规模**：~90 行。**依赖**：H1 负责把新参数接到 CLI（H3 自身不碰 cli.py）。

---

### P0 冲突矩阵

| 文件 | 独占任务 |
|---|---|
| `pyproject.toml` | PKG |
| `docs/*.md`(非 archive) + `backtest_artifacts/runtime_inputs/weekly_*` | DOCS |
| `scripts/export_clean_repo.sh` | EXPORT |
| `harness/gates/backtest_gate.py` | BG |
| `harness/gates/static_gate.py` + `harness/contracts/import_rules.py` | STATIC |
| `backend/main.py` + `backend/services.py` | API |
| `harness/cli.py` + `registry.py` + `config_loader.py` + `orchestrator.py` | **H1（独占）** |
| `harness/gates/compare_gate.py`(新) | H2 |
| `harness/authorization.py` | H3 |

> 9 个任务可同时派发。H1 与 H2/H3 仅在合并时按钉死的接口契约对齐 import，无文件级冲突。

---

## 2. P1 串行子计划（数据模型重构 —— 单 owner，按步推进，勿 naive 并行）

> 这些步骤共享 `migrations/`、`shared/models.py`、`scheduler/repository.py`、`scheduler/executor.py`、`backtests/repository.py`、`backend/services.py`，互相撞车。**建议单个 owner（或一个 agent 串行）按 S1→S7 推进**，每步独立提交、可回滚。

- **S1 — 迁移基座（必须最先）**：新增 `migrations/005_lifecycle.sql`：建 `t_scheme_versions`、`t_harness_runs`、`t_harness_gate_results`、`t_input_artifacts`、`t_scheme_runs`、`t_scheme_serving_pointer`；为 `t_scheme_predictions` 增 `run_id`、`scheme_version`、`prediction_id`（保留旧 UK 一段过渡）；为 `t_backtest_runs` 增 `backtest_run_id`、`code_hash`、`config_hash`、`input_artifact_hash`、`run_mode`。字段以评审 §7 为准。
- **S2 — InputArtifact 指纹**：`shared/input_artifacts.py` 增 `content_hash`、`schema_hash`、`artifact_id`、`source_watermark` 等；落 `t_input_artifacts`（写入须经唯一写库点——新建 `scheduler/repository` 内 artifact 写函数或专用 repository，**不破坏写库单点不变量**）。
- **S3 — run_id + 不可变预测**：`scheduler/repository.py`/`executor.py`/`shared/models.py`：每次运行生成 `run_id`，预测按 `run_id` **insert 不覆盖**；新增 serving pointer 更新逻辑，前端读"latest approved"。
- **S4 — scheme_version 派生**：统一 `code_hash`/`config_hash`/`manifest_hash` 计算工具（`shared/` 新 util）+ `scheduler.discovery` 写 `t_scheme_versions`。
- **S5 — harness 运行留痕**：harness report writer 落 `t_harness_runs`/`t_harness_gate_results`（经批准写入点）。与 §3 token 的 `harness_run_id` 打通。
- **S6 — 回测不可变**：`backtests/repository.py` + 迁移：每次回测产生新 `backtest_run_id`，停止按 `(benchmark_id,scheme_id,data_source)` 覆盖；改为 append + pointer/最新视图。
- **S7 — backend 读切换**：`backend/services.py` 读路径切到 serving pointer / `v_latest_approved_predictions`，每个展示数字可回溯 run_id/scheme_version/input_artifact_hash。

> **与 P0 的衔接**：P0 的 API（去 GET 写库）、H3（token 带 `harness_run_id`/`scheme_version`）、BG（baseline）已为 P1 预留接口；P1 落地后回填 admin token 的 HMAC 与 §3 统一。

---

## 3. P2（实验室体验，后置，独立）

前端 ranking / 按 tenor·horizon·frequency 横向对比 / shadow vs active / confidence calibration / rolling hit ratio / drawdown·连错 / 生命周期页 / 异常告警（未出预测、actuals 未回填、输入 stale）。均在 `frontend/` + backend 只读接口，待 P1 数据模型就绪后单独排期。

---

## 4. 人工动作（不在 subagent 范围）

1. **轮换 `.env` 中已暴露过的数据库密码**（评审 §3.1 / P0#1）——运维执行，subagent 无权触碰凭据。
2. 设定 `HARNESS_AUTH_SECRET`（H3 依赖）、admin token 环境变量（API 依赖）于部署环境。
3. 确认 P1 迁移在测试库先行验证后再上生产。

---

## 5. 一页速查（派发清单）

| # | 任务 | 独占文件 | 规模 | 依赖 |
|---|---|---|---|---|
| P0 | PKG | pyproject.toml | ~3 | — |
| P0 | DOCS | docs/*.md + weekly artifact 目录 | ~50 | — |
| P0 | EXPORT | scripts/export_clean_repo.sh | ~40 | — |
| P0 | BG | backtest_gate.py(+test) | ~30 | — |
| P0 | STATIC | static_gate.py + import_rules.py(+test) | ~80 | — |
| P0 | API | backend/main.py + services.py(+test) | ~90 | — |
| P0 | H1 | cli/registry/config_loader/orchestrator(+test) | ~120 | H2/H3 接口契约 |
| P0 | H2 | gates/compare_gate.py(+test) | ~110 | H1 契约 |
| P0 | H3 | authorization.py(+test) | ~90 | H1 wiring |
| P1 | S1–S7 | migrations/shared/scheduler/backtests/backend | 大 | 串行，单 owner |
| 人工 | .env 轮换等 | — | — | — |
