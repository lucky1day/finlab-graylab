# Bond Factor Lab — 项目规范

> 本文是项目根规范（`CLAUDE.md` 与 `AGENTS.md` 内容一致）。架构细节以 `docs/` 为准，入口见 [docs/README.md](docs/README.md)。

## 项目定位

独立的国债因子实盘测试平台，前端通过 iframe 嵌入 panda_quantflow 的 AIFin Lab Shell。

## 当前工作上下文（必须遵守）

- 当前开发分支：`codex/audit-bugfixes-20260613`。
- 生产分支：`master`。`master` 是后续生产版本的基准。
- 经过验证的开发分支只有在用户明确确认后，才能合并或覆盖到 `master` 并推送远程；agent 不得自行决定发布到 `master`。
- 不再维护第二生产分支；除非用户明确要求，验证后的更新也不得发布到其它发布分支。
- 用户口头说根目录 `agent.md` 时，优先理解为根目录 `AGENTS.md`；本项目要求 `AGENTS.md` 与 `CLAUDE.md` 内容一致，更新根规范时两者要同步。
- 分支操作、提交或暂存前必须先核对 `git status --short` 和相关分支列表，避免把未跟踪的新方案、`outputs/` 产物或其他草稿混入当前任务提交。
- 当前未跟踪的 `outputs/` 属于临时分析/导出产物；除非用户明确要求，不要纳入文档、方案或修复提交。
- 后续以 launchd + plist 作为真实生产调度控制面：任务是否生产挂载、触发时间、进程环境、重启策略和日志位置以 installed plist 与 `launchctl` 现场状态为准。仓库 `deploy/launchd/*.plist` 是受版本控制的期望配置，但文件存在不等于已经安装或生效。
- `scheduler.main`/APScheduler 等旧 Python 调度模块只是 plist 启动的子进程实现，不得被当作独立于 launchd 的第二生产控制面。后续新增或迁移调度必须先明确对应 plist、Label、`ProgramArguments`、触发和失败恢复语义，不再默认向 APScheduler 增挂任务。
- 后续明确不采用 daily ledger、occurrence 或 epoch 作为生产或过渡调度方案：不得新增、扩容、迁移或补建 ledger policy、任务或表，也不得把任何新方案的 Intake、激活、灰度可见或定时调度绑定到 ledger 建设。现存相关代码、配置和空表只作为待退役遗留物，在 launchd-only 生产链稳定后按授权删除。
- 修改生产 plist 或执行 `launchctl bootstrap/bootout/kickstart`、替换 installed plist、重启服务都属于独立生产操作；开发验证不得顺带执行，必须先只读核对仓库模板、installed plist 与 loaded state，并取得用户明确授权。

## 技术栈

- **后端**: Python 3.12 + FastAPI + SQLAlchemy + APScheduler
- **前端**: 原生 HTML/CSS/JS（从 panda_quantflow 提取的因子实验室页面）
- **数据库**: MySQL 8.0 (bond_db)
- **部署**: Mac Studio, launchd 管理进程
- **环境**: 后端/调度使用 `bond_factor_lab_service`；Native 算法使用 `forecast_env`；Blackbox 执行环境由 `blackbox-v2-v1` Runtime Profile 唯一指定

## 目录结构约定

```
bond-factor-lab/
├── shared/            # L1 统一公共层：data_service(唯一DB导出) / input_artifacts(唯一输入入口)
│                      #    / calendar_service(唯一日历) / models / db_config / artifact_paths
├── schemes/           # L2 算法层（按 runtime_type 显式发现）
│   ├── {native_id}/   # Native V1，仅维护政策清单中的存量方案
│   │   ├── config.yaml
│   │   ├── predict.py
│   │   └── core/
│   └── {blackbox_id}/ # Blackbox V2，所有后续新增方案
│       ├── config.yaml
│       └── delivery/{blackbox_id}.py + {blackbox_id}.json
├── scheduler/         # L3 预测任务层：discovery / scheme_runner / executor / repository / *_actuals_updater
├── backend/           # L4 FastAPI 后端 + 静态前端 serve
├── backtests/         # L4 历史复现 runner（写 t_backtest_*，支持 --no-persist）
├── tests/             # L4 单元/集成测试
├── harness/           # L5 强约束 harness（横切）：gates / contracts / probes / authorization / cli
├── frontend/          # 原生 HTML/CSS/JS 因子实验室页面
├── migrations/        # SQL 迁移脚本
├── scripts/           # 审计/对比/受控 admin 脚本
├── source_evidence/   # 外部来源证据归档（benchmark_batches/{benchmark_id}/）
├── backtest_artifacts/ # 运行期输入与回测产物（gitignore）
├── reports/           # 审计与 harness 报告（gitignore）
├── deploy/            # launchd plist
└── docs/              # 项目文档（入口 docs/README.md；只保留当前规范和必要设计文档）
```

## 强约束分层边界（不可破坏的四条不变量）

1. **输入单点**：算法输入只能经 `shared.input_artifacts` 产出；adapter / backtest runner 不得自拼 DB 输入。
2. **写库单点**：只有 `scheduler.repository` / `backtests.repository` / `*_actuals_updater` 能写库；其余层零写库。
3. **Native core 纯净**：Native V1 的 `schemes/*/core/`（非 legacy）零 DB、零写库、零跨方案 import；Blackbox 不向平台暴露 core。
4. **源算法保真**：Native source-backed 存量方案不得修改原始算法逻辑；时间起点、窗口、回测分组键、每组 `source_end/current_start/current_end`、特征、对齐、模型参数、投票/fallback、内部 score 映射都必须按原始脚本复现。平台只做输入/输出/日期/落库适配；若方向或内部模型数值不一致，先查输入 artifact 和 source 口径，不得用调参或改算法贴结果。原始算法能导出的 `vote_score`、baseline `*_score`/`*_vs`、`*_dir`/`*_sign`、probability/confidence 等内部字段在首次技术入库时必须进入逐方案 benchmark 和 CompareGate；只做到最终方向一致不得宣称算法逻辑完全一致。若 source-original batch 的 `source_end` 或 test window 晚于样本 `feature_date`，该 batch 只能验收 source-original backtest，不能直接当作 gray/scheduled live 逐日内部数值真值；live 必须保持 `feature_date` 硬截止并用同口径 live-safe oracle 验收。跨灰度边界的 `original_predictions_sample.csv` 必须先判定每行 benchmark role；`TOTAL_BAD=0` 只表示 live 行结构、版本和 source-compatible scope 通过，不表示 live 内部数值可与固定 source batch benchmark 混称“完全一致”。Blackbox 的算法内部保真由上游负责，平台只验收接口、确定性、截止隔离和标准结果，不反编译或改写算法脚本。

Native source benchmark 与 CompareGate 是首次技术入库的硬证据，不是全局可关闭的检查。ActivationGate 只接受两条**互斥**路径：当前精确 version 已通过完整七段 `all` 时，使用 `full_initial_onboarding_v1`，只核验该 current `all` 的七个 Gate（含当前 Compare），不要求 `native-maintenance`、prior version 或 prior snapshot；只有未走 full-`all` 且仍在 Native 政策清单的同一业务身份修订，才可走 `native-maintenance`，此时除旧 Native version 的 passed `all + compare` 外，那个 prior `all` run 的 `static.business_identity` 必须已有与当前精确匹配的持久化快照。快照只含 `scheme_id`、`runtime_type`、`horizon`、`task_type`、`frequency`、target tenors 与 composite Registry IDs，绝不含代码、config 或 version hash；该路径执行 `static → native-maintenance-admission → input → unit → dry-run → api-readiness`，并核验当前六个 Gate 与一次性授权。缺少快照仍 fail-closed，唯一已实现的狭窄例外是 `weekly_10y_d_overlay_0529`：其 maintenance 当前选择的 prior `all` run 必须有唯一 passed CompareGate、已通过且仅明确缺少 identity 字段的 StaticGate；操作员可用专用 `native-legacy-admission-attest` 命令和一次性 `native_legacy_admission_identity_attest` token（非空 issuer、精确 prior version/run、TTL 不超过 900 秒）写入唯一 receipt。receipt 只写 `t_harness_runs` 与 `t_harness_gate_results`，不含当前代码/config/version hash、不改历史记录；已存在、非 canonical、重复或身份不匹配均阻断。2026-08-04 已写入唯一 receipt `lna_hr_20260611T055610Z_8742d5bc99c9`，身份来源已被 verifier 识别为 `legacy_operator_attestation_v1`；初次 maintenance run `hr_20260804T092226Z_5d84b9d45fd9` 因预激活 API 状态混同失败，修正后 exact version `e50ad79a6c2f` 的 `hr_20260804T102103Z_91fa9e7db871` 已通过六段 Gate。随后独立 activation 已使 exact version 与 Registry 均为 active；这不授予 gap repair、其他业务写入或 launchd 权限，也不是其它方案的 waiver。满足任一已实现路径后，同一身份修订的历史 source-benchmark 输入 vintage 漂移只作归档诊断，不能单独阻断 activation、gap repair、`gray_live`、`scheduled_live` 或 API/前端读回；它不放宽输入截止、统一周历、日期语义、Registry、live-safe oracle 或 L0/L1/L2 限制，Blackbox CompareGate 也保持不变。

`native-maintenance` 的当前精确候选必须已有对应的 `t_scheme_versions` 行，且 `runtime_type='native_adapter'`、`status in {'draft','active'}`。期望的 composite Registry identity 可在激活前统一为 `paused`，或在激活后统一为 `active`；`draft` version 配 `active` Registry 必须 fail-closed。ActivationGate 是唯一可在严格 discovery、精确版本与一次性授权核验后原子建立 active 状态的操作；维护 Gate 本身不改变 version 或 Registry 生命周期。

Native source-backed 存量方案修复前必须做算法改动分级：L0 只允许平台 I/O、日期字段、extra、缓存、落库和审计适配；L1 是 source runner 明确暴露的上下文参数传递，必须逐项证明没有移动未 patch 的固定算法锚点；L2 是算法内部改动，默认禁止并 fail-closed。移动 IC screening cutoff、把 source 两段窗口改成单段窗口、把 target-date 月分组改成 feature 月或全局 `source_end`、改变特征列顺序、VT/selector/streak/fallback、内部 score 映射，都属于 L2；新算法或替代版本必须创建独立 Blackbox V2 trial。

完整依赖方向规则见 [docs/architecture/CODE_ARCHITECTURE.md](docs/architecture/CODE_ARCHITECTURE.md)；源算法保真规则见 [docs/architecture/SOURCE_ALGORITHM_FIDELITY.md](docs/architecture/SOURCE_ALGORITHM_FIDELITY.md)；边界总纲见 [docs/architecture/HARNESS_ARCHITECTURE.md](docs/architecture/HARNESS_ARCHITECTURE.md)。

## 方案接口规范

运行接口由 `runtime_type` 显式分派，不得根据目录内容猜测。

Native V1 存量方案在 `predict.py` 暴露：

```python
SCHEME_ID = "<scheme_id>"   # 必须 == 目录名 == config.scheme_id

def run(predict_date: str) -> list[PredictionRecord]:
    """
    Args:
        predict_date: 预测发出日期，格式 YYYY-MM-DD
    Returns:
        预测记录列表，每个 tenor 一条记录
    """
```

Blackbox V2 新方案只交付 `{scheme_id}.py + {scheme_id}.json`，并实现 Contract 1.0 的 `predict/backtest` CLI。共享契约见 [docs/architecture/SCHEME_CONTRACT.md](docs/architecture/SCHEME_CONTRACT.md)，Native 专属契约见 [docs/native_v1/SCHEME_CONTRACT.md](docs/native_v1/SCHEME_CONTRACT.md)，Blackbox 上游契约见 [docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md](docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)。

## 方案身份与 Registry

`config.yaml` 里的 `scheme_id`、目录名、`PredictionRecord.scheme_id` 是算法执行身份，也称 `base_scheme_id`。`t_scheme_registry` 是唯一方案注册表，每一行是一个前端/业务方案，唯一键只有 registry `scheme_id`，格式为 `{base_scheme_id}__h{horizon}__{target_tenor}`。即使原算法只预测一个标的，也必须使用这个 composite registry ID；多标的算法在 registry 中拆成多行，但 scheduler 仍按 `base_scheme_id` 只挂载一个执行任务。

前端任务格子由 `target_tenor + task_type` 定义，不再由 `frequency/horizon` 隐式推断。`task_type` 固定取值为 `T+1`、`T+5`、`weekly_point`、`weekly_average`、`monthly`，并存储在 `t_scheme_registry.task_type`；API 返回缺失或非法值必须 fail-closed。

前端、`/api/schemes`、`/api/metrics/{scheme_id}` 和 `/api/backtests/factor-lab` 只使用 `status='active'` 的 registry composite `scheme_id`，并依赖 registry `task_type` 分列；`/api/metrics/{base_scheme_id}?tenor=...` 不是合法调用。`paused` / `archived` registry 行只用于管理或审计，不进入当前前端/业务 API，不允许 trigger，也不允许 scheduler 新写入该 target。预测表、run 表和 backtest 表继续保存 base `scheme_id`，同时用 `target_tenor` 区分目标标的。

## 预测日期与实盘阶段语义

平台、业务和前端统一使用三类日期字段：

- `predict_date` — 信号发出日 / 调度运行日
- `feature_date` — 数据截止日 / 预测站位日
- `target_date` — 验证目标日，用于展示、去重、actual join 和月度统计归属

`feature_date` 是唯一标准数据截止字段；`anchor_date` 只允许作为方案内部算法变量或审计 extra，前端和业务规则不得依赖它。实盘分为 `gray_live`（灰度实盘）和 `scheduled_live`（正式 scheduler 实盘）；日频实盘满足 `predict_date=T+1`、`feature_date=T`、`target_date=T+horizon`，周频实盘先由 `predict_date` 反推上一交易日 `feature_date` 再映射周，月频 source-backed 方案若声明自然 15 号触发则 `predict_date` 保留自然月 15 号、`feature_date/target_date` 分别取当前月/目标月 15 号及以前最近交易日。历史回测必须满足 `predict_date=feature_date=T`、`target_date=T+horizon`，但已有灰度观察区时必须按方案级 `target_date` 起点截断；当前 0629 月度三方案中 `target_date >= 2026-06-01` 均为灰度实盘，不得留在 latest backtest。原始算法 benchmark 里的 `T/date/predict_date` 表达 source T / 预测站位日，进入平台后必须对齐 DB 明细的 `feature_date`，不是对齐 live `predict_date`；跨灰度边界的样本必须先按 `target_date` 和 benchmark role 分流，同执行口径才可对实盘表断言数值一致，否则用 live-safe oracle 核验。完整规则见 [docs/architecture/PREDICTION_SEMANTICS.md](docs/architecture/PREDICTION_SEMANTICS.md)。

## 方案入库流程（强约束 harness）

所有场景必须先读[统一入库导航](docs/onboarding/README.md)：

- 新算法、新方案 ID、新目标、新任务和替代版本：只走 Blackbox V2 两文件 Intake。
- 现有 Native V1 故障、数据口径或保真修复：只操作 `deploy/onboarding_policy_v1.json` 中的存量 ID。
- 已入库 Native 修订可在 prior `all` 留有匹配 `static.business_identity` 快照时走 `native-maintenance`；缺快照时仍默认只能 current exact version 的完整 `all`（含 Compare）。唯一已实现例外是 `weekly_10y_d_overlay_0529` 的专项 `native-legacy-admission-attest`：只接受 maintenance 当前选择、唯一 `all + compare=passed` 的 prior，且 StaticGate 已通过但 identity 字段明确缺失；短期一次性 token 必须绑定 issuer 与 exact prior version/run。它只在两张 Harness 控制面表留下 canonical receipt，不能激活、补数、写业务表或被任何其它 Native 身份使用；receipt 不替代已通过且完整持久化的六段 maintenance 或常规 activation。该固定 scope 的 maintenance、activation 与唯一业务键的 `gray_live` gap write 均已在相互独立的专项授权下完成；后者仅覆盖 `2026-08-01 / 2026-07-31 / 2026-08-07 / 10Y / h6`，不授予 scheduler、其他业务写入或 launchd 权限。
- Native StaticGate 与 ActivationGate 都必须拒绝清单外的新 Native 身份。

```bash
# Blackbox V2 收包
python -m harness intake-blackbox --delivery-dir <two-file-dir> --project-root . \
  --runtime-profile blackbox-v2-v1 --data-schema-version data-bridge-v1

# 两种运行时共用的自动 Gate 编排
python -m harness onboard {scheme_id} --predict-date YYYY-MM-DD --stage all
# 自动段：static → input → unit → dry-run → compare → backtest → api-readiness（fail-fast，退出码 0/1/2）
# 副作用段不在 all 内，必须显式授权且 fail-closed
```

Blackbox V2 自动 Gate 不自动授予生产运行权限；具体方案必须完成生产准备核验并取得专项授权后，才可执行 activate、持久化回测或 live，且授权不得外推到其他方案。平台操作见 [docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md](docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)，生产条件见 [docs/blackbox_v2/PRODUCTION_READINESS.md](docs/blackbox_v2/PRODUCTION_READINESS.md)。Native 存量维护见 [docs/sop/NATIVE_V1_MAINTENANCE_SOP.md](docs/sop/NATIVE_V1_MAINTENANCE_SOP.md)。Harness 边界见 [docs/architecture/HARNESS_ARCHITECTURE.md](docs/architecture/HARNESS_ARCHITECTURE.md)。

## 数据库表

源数据表只读：`api_wind_date`、`api_wind_daily/weekly/monthly`(+derivative)、`api_wind_indicators_all`、`t_trade_calendar`。

写库表：
- `t_scheme_predictions` — 统一预测结果表
- `t_scheme_actuals` / `t_scheme_weekly_actuals` / `t_scheme_monthly_actuals` — 实际方向表（日频 / 周频 / 月频）
- `t_scheme_registry` — 方案注册表
- `t_scheme_runs` — 结构化运行表（版本、阶段、输入 artifact 链接）
- `t_scheme_run_log` — 运行日志表
- `t_target_registry` — Y 标的注册与展示名
- `t_backtest_*` — 历史复现结果（独立于实盘预测）

## 数据库迁移操作边界

- `migrations.runner` 是迁移行为的唯一实现：它只接收 caller-supplied `Engine`，负责 manifest、inspect、apply 与 `APPLYING` recovery；不得把环境变量、CLI 解析或运维授权逻辑放入该库层。
- `scripts/apply_migrations.py` 是唯一受控运维包装器。生产/候选 schema 的 apply 与 recovery 只能经此 CLI，不得用 `mysql` 客户端直跑 migration SQL，也不得复制 runner 行为到 scheduler、harness 或其它脚本。
- `--apply`、`--recover-applying-017 --apply`、`--recover-applying-018 --apply` 都必须同时显式提供 `--expected-database-name` 和 `--expected-server-uuid`；CLI 在创建 Engine 前校验参数，并在首个写库动作前精确比对 `DATABASE()` 与 `@@server_uuid`。inspect 是只读操作，不需要这两个参数。
- operator 只能从只读 inspect JSON 或受控只读 identity query 取得 UUID；文档、脚本输出和提交中不得示例生产 UUID、DSN 或凭据。isolated MySQL 测试不等于已应用生产 migration；当前 apply/no-op 输出也尚未形成 durable signed operator report。

## 编码规范

- Python: 遵循 PEP 8, type hints, docstring 用中文
- 前端: 原生 JS，无构建步骤，直接由 FastAPI serve
- 数据库字段: snake_case
- API 路径: kebab-case
- registry `scheme_id`(业务方案) / `base_scheme_id`(算法执行身份) / `benchmark_id`(基准批次) / `data_source`(数据口径) 命名分离

## 关键设计决策

1. 所有方案预测结果写入同一张 MySQL 表，通过 base `scheme_id + target_tenor + horizon + target_date` 隔离；前端业务身份由 registry composite `scheme_id` 表达。
2. 准确率指标由后端实时计算（JOIN predictions 和 actuals 表）。
3. 方案按显式 `runtime_type` 发现和分派；后续新增方案只允许 Blackbox V2，Native V1 仅维护政策清单中的存量身份。
4. 前端构建为静态文件，由 FastAPI serve。
5. 强约束分层 + 横切 harness：依赖只向下，副作用（写库/激活）须授权，StaticGate 机器守护依赖规则。
6. 算法在 `forecast_env` 子进程运行，与服务环境依赖隔离（JSON stdout 解耦）。
