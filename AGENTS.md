# Bond Factor Lab — 项目规范

> 本文是项目根规范（`CLAUDE.md` 与 `AGENTS.md` 内容一致）。架构细节以 `docs/` 为准，入口见 [docs/README.md](docs/README.md)。

## 项目定位

独立的国债因子实盘测试平台，前端由本项目的 FastAPI 直接提供，不依赖或嵌入其它前端系统。

## 当前工作上下文（必须遵守）

- 根规范只保存长期约束。当前 release、主机状态、方案数量、数据库水位和自然调度观察必须从 [当前状态](docs/CURRENT_STATUS.md) 与现场权威控制面读取，不在本文维护副本。
- 当前活动集成分支是 `codex/develop`；不得建立 Mac3/ECS 长期环境分支。`master` 受保护，只有用户明确确认后才能合并、移动或推送。
- 发布坚持“单一代码线 + 不可变 release + 独立部署环境”：每个版本由精确提交生成确定性 archive，生产包不含 `.git`，身份来自已校验 manifest。环境差异只允许出现在部署配置、环境变量、部署矩阵和依赖清单；ECS 不保留 Git checkout 或现场修改 release。
- 开发工作区与生产 runtime 必须分离；生产使用 `current -> releases/<release_id>`，状态和日志外置。ECS 先验证、Mac3 后晋级时可暂时指向不同 release，但 Mac3 只能使用同一份已验证 archive。
- 跨 release 状态不得无条件共用：日志和部署记录可永久外置；DataBridge、缓存和输入 artifact 只有通过既有 manifest、lineage、business digest、input state 与 ready gate 才能复用；源码 symlink 回滚不等于数据库、Python 环境或运行期状态回滚。
- 跨主机补缺只允许在明确授权后复用同一已验证 release、方案版本、日期、输入摘要和 lineage 的精确结果；先停止目标 Writer，再由目标端 repository insert-only 导入。不得复制数据库主键、`run_id`、Actuals、回测或 Harness 历史，不得建立持续复制或双写；任一身份不匹配都必须重算。
- 环境方案范围只由 `BFL_DEPLOYMENT_TARGET` 与 `deploy/scheme_deployment_matrix_v1.json` 表达；不得再通过为不同主机修改 canonical `config.yaml` 的 `status` 制造两个 `scheme_version`。
- 用户口头说根目录 `agent.md` 时，优先理解为根目录 `AGENTS.md`；本项目要求 `AGENTS.md` 与 `CLAUDE.md` 内容一致，更新根规范时两者要同步。
- 分支操作、提交或暂存前必须先核对工作区与相关分支；`outputs/`、未跟踪新方案和草稿未经明确要求不得纳入提交。
- 默认执行方式为“主 agent 直接执行（inline-first）”：日常实现由主 agent 在当前会话完成。只有工作可安全拆成相互独立、可并行的子任务且预期能显著节省时间时，才使用 subagent；不得仅因任务复杂或可拆分就启用。该长期偏好只授权任务拆分与代码审查，不外推为生产切换、数据库写入、服务重启、破坏性操作、受保护分支发布或未决业务选择的授权。
- 使用 subagent 时，只分派无共享写入冲突的独立工作流，并明确任务范围、相关文件、验收条件和不可触碰的边界。subagent 必须报告实际改动、验证结果与阻塞；主 agent 负责整合结果并对整体变更运行相关验证。
- 提 PR 前先判断该问题能否在本机测试验证。本机可验证的，直接在 PR 中修复并附复现与验证证据；需要结合生产环境才能确认的（launchd 现场状态、生产 DataBridge、真实调度时钟、跨环境数据漂移等），不得夹带未经验证的代码改动，只提交「现象 / 问题点 / 造成的影响 / 推荐解决方案」四段式报告 PR，由能验证该环节的同事结合建议自行处理。判断依据是能否在本机跑出决定性证据，不是主观把握程度。
- ECS 是独立灰度实验室，不直接替换 Mac3 生产。两端使用各自的 MySQL、DataBridge、算法调度、Actuals 和前端，不建立复制、双写、跨主机共享数据库或共享 DataBridge。生产域名、Nginx、DNS 或 Writer 切换必须作为新的生产项目单独授权。
- Mac3 继续以 launchd + plist 作为真实生产调度控制面：任务是否生产挂载、触发时间、进程环境、重启策略和日志位置以 installed plist 与 `launchctl` 现场状态为准。仓库 `deploy/launchd/*.plist` 是受版本控制的期望配置，但文件存在不等于已经安装或生效。ECS 独立灰度实验室只允许使用仓库 `deploy/systemd/` 的 one-shot service/timer，现场状态以 installed unit 与 `systemctl` 读回为准。
- 调度只允许由 launchd/systemd 承载一次性入口，不得恢复常驻 Python 调度控制面，也不得新增或重建 ledger、occurrence、epoch 或 runtime/config 闭包。新增调度必须先明确触发、时区、错过触发和失败恢复语义。
- 修改生产 plist 或执行 `launchctl bootstrap/bootout/kickstart`、替换 installed plist、重启服务都属于独立生产操作；开发验证不得顺带执行，必须先只读核对仓库模板、installed plist 与 loaded state，并取得用户明确授权。

## 技术栈

- **后端**: Python 3.12 + FastAPI + SQLAlchemy
- **前端**: 本项目独立维护的原生 HTML/CSS/JS，无构建步骤
- **数据库**: MySQL 8.0 (bond_db)
- **部署**: Mac3 生产使用 launchd；阿里云 ECS 独立灰度实验室使用 systemd one-shot/timer
- **环境**: 后端/调度使用 `bond_factor_lab_service`；Native 算法使用 `forecast_env`；Blackbox 执行环境由 `blackbox-v2-v1` Runtime Profile 唯一指定

## 目标架构：统一到 Blackbox V2

- 长期目标是把仍在运行的 Native V1 存量方案逐批迁移为 Blackbox V2，最终只维护 Blackbox V2 Contract、Intake、Gate、执行与 lifecycle 一套入库范式。
- 所有新算法、新方案、新目标、新任务和替代版本立即只允许 Blackbox V2；Native V1 在完成迁移前仅作存量维护，不再扩展身份或能力。
- 迁移前先让 Blackbox 结果合同向后兼容地承载 Native 需要保留的 confidence 与必要审计字段；不得通过静默丢字段缩小“结果不变”的口径。
- Native 迁移使用新的 Blackbox successor base ID，不原地修改 `runtime_type`，也不把平台 adapter 冒充 Blackbox。每批必须形成合法两文件交付，并证明输入截止、日期、结果字段、回测结果和性能与迁移前基线一致。
- 迁移期间旧 Native 与新 Blackbox 的身份、Registry 切换、历史数据和回滚边界必须显式设计；不得双写、覆盖历史预测或让两个 Writer 同时拥有同一业务键。
- 只有在等价验证、灰度观察、受控切换和回滚窗口闭环后，才能删除对应 Native adapter、source runner、回测 runner、专属 Gate 与测试；不得先删旧路径再验证新路径。

## 强约束分层边界（不可破坏的四条不变量）

1. **输入单点**：算法输入只能经 `shared.input_artifacts` 产出；adapter / backtest runner 不得自拼 DB 输入。
2. **写库单点**：只有 `scheduler.repository` / `backtests.repository` / `*_actuals_updater` / `backend.auth.repository` 能写库；认证仓储只允许写认证三张表，其余层零写库。
3. **Native core 纯净**：Native V1 的 `schemes/*/core/`（非 legacy）零 DB、零写库、零跨方案 import；Blackbox 不向平台暴露 core。
4. **源算法保真**：Native 存量只允许有证据的 L0 平台适配和 L1 source runner 上下文传递；L2 算法内部改动必须停止并创建独立 Blackbox V2 trial。source-original backtest 与 live-safe 真值必须分开验收，平台不得用调参贴结果。Blackbox 内部保真由上游负责，平台只验证自身接入与标准输出边界。

Native 首次入库必须保留 source benchmark 与 CompareGate；同一身份维护只允许走文档定义的互斥 full-`all` 或 `native-maintenance` 路径，任一身份快照、version、Registry 或 Gate 前提不满足都 fail-closed。Native 不运行按名称扫描测试的 UnitGate；Blackbox 不再单设 UnitGate 或 predict 冒烟，平台通过完整持久化回测验证批量调用与标准输出，非法 Request 和失败无 Output 行为由上游交付契约负责。

完整依赖方向规则见 [docs/architecture/CODE_ARCHITECTURE.md](docs/architecture/CODE_ARCHITECTURE.md)；源算法保真规则见 [docs/architecture/SOURCE_ALGORITHM_FIDELITY.md](docs/architecture/SOURCE_ALGORITHM_FIDELITY.md)；边界总纲见 [docs/architecture/HARNESS_ARCHITECTURE.md](docs/architecture/HARNESS_ARCHITECTURE.md)。

## 方案接口规范

运行接口只按显式 `runtime_type` 分派，不得根据目录内容猜测。Native 存量接口、Blackbox V2 两文件交付和标准结果分别以 [共享契约](docs/architecture/SCHEME_CONTRACT.md)、[Native 契约](docs/native_v1/SCHEME_CONTRACT.md)和 [Blackbox 上游契约](docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)为准，根规范不维护副本。

## 方案身份与 Registry

`config.yaml` 里的 `scheme_id`、目录名、`PredictionRecord.scheme_id` 是算法执行身份，也称 `base_scheme_id`。`t_scheme_registry` 是唯一方案注册表，每一行是一个前端/业务方案，唯一键只有 registry `scheme_id`，格式为 `{base_scheme_id}__h{horizon}__{target_tenor}`。即使原算法只预测一个标的，也必须使用这个 composite registry ID；多标的算法在 registry 中拆成多行，但 scheduler 仍按 `base_scheme_id` 只挂载一个执行任务。

前端任务格子由 `target_tenor + task_type` 定义，不再由 `frequency/horizon` 隐式推断。`task_type` 固定取值为 `T+1`、`T+5`、`weekly_point`、`weekly_average`、`monthly`、`monthly_average`、`quarterly_average`、`annual_average`，并存储在 `t_scheme_registry.task_type`；API 返回缺失或非法值必须 fail-closed。Blackbox V2 周均和三种周期均值的 `horizon=1` 都是一个业务桶步长；周期均值的 `target_date` 是由桶锚点直接计算的日期指针，绝不得按一天或由 horizon 推断。

前端与业务展示只读取 `/api/factor-lab/dashboard`；不得恢复 `/api/schemes`、`/api/metrics/{scheme_id}`、`/api/backtests/factor-lab` 或浏览器侧第二套聚合。`paused` / `archived` registry 行只用于管理或审计，不进入 Dashboard，不允许 trigger，也不允许 scheduler 新写入该 target。预测表、run 表和 backtest 表继续保存 base `scheme_id`，同时用 `target_tenor` 区分目标标的。

## 预测日期与实盘阶段语义

平台、业务和前端统一使用三类日期字段：

- `predict_date` — 信号发出日 / 调度运行日
- `feature_date` — 数据截止日 / 预测站位日
- `target_date` — 验证目标日，用于展示、去重、actual join 和月度统计归属

`feature_date` 是唯一标准数据截止字段；`anchor_date` 只允许作为方案内部算法变量或审计 extra，前端和业务规则不得依赖它。`gray_live` 与 `scheduled_live` 只属于 run 审计；产品事实不保存 phase，公开回测/实盘只按 `target_date=2026-06-01` 分界。日频、周频、月频和周期均值必须按统一日历与任务语义生成三个日期。完整规则见 [docs/architecture/PREDICTION_SEMANTICS.md](docs/architecture/PREDICTION_SEMANTICS.md)。

后续方案的历史回测与灰度实盘保持两个清晰批次：`target_date < gray_target_start` 由一次持久化历史回测写入新的 immutable canonical backtest；起点及以后、正式调度以前的应有点由一次 target 区间批量执行，按受控 repository insert-only 物化为 `gray_live`。灰度区间不得逐日期重复启动算法、重复解析 generation 或重建输入；平台必须按任务日历生成 live `predict_date`，并保留 `feature_date`、`target_date`、方向、置信度、exact scheme version 和必要算法 `extra`。两个批次严格绑定同一 exact version 和输入 lineage，但不为了少一次进程启动引入跨激活候选表、临时结果文件或新的生命周期状态。任一灰度业务键已存在即拒绝整个授权区间，不得更新、覆盖或先删除再导入；历史 run 保持不可变。若 batch 使用晚于样本 `feature_date` 的固定 `source_end`、未来 test window、全局 selector/calibration，或 exact version、输入 digest、lineage 任一不匹配，则禁止批量物化为 live，必须走逐点 live-safe 计算。完整操作和验收规则见统一入库导航与预测语义文档。

## 方案入库流程（强约束 harness）

所有场景必须先读[统一入库导航](docs/onboarding/README.md)：

- 新算法、新方案 ID、新目标、新任务和替代版本：只走 Blackbox V2 两文件 Intake。
- 现有 Native V1 故障、数据口径或保真修复：只操作 `deploy/onboarding_policy_v1.json` 中的存量 ID。
- 已入库 Native 修订只在 prior `all` 留有匹配 `static.business_identity` 快照时走 `native-maintenance`；缺少、重复、损坏或不匹配时只能让 current exact version 重新走完整 `all`（含 Compare），不再读取方案级历史 receipt。
- Native StaticGate 与 ActivationGate 都必须拒绝清单外的新 Native 身份。

```bash
# Blackbox V2：最短链路是收包、完整回测、激活
python -m harness intake-blackbox --delivery-dir <two-file-dir> --project-root .
python -m harness gate backtest --scheme-id {scheme_id} \
  --predict-date YYYY-MM-DD --persist --backtest-start-date 2025-01-01
python -m harness activate --scheme-id {scheme_id}

# onboard 只服务 Native V1 存量维护
python -m harness onboard {scheme_id} --predict-date YYYY-MM-DD --stage all
# Native all：static → dry-run（含实际输入合同）→ compare → backtest
```

平台不再对 Blackbox 运行 `StaticGate`、冒烟 `CompareGate` 或 `shadow-register`：Intake 定义两文件、Metadata、固定 Profile/Schema 和安全静态边界；持久化回测在真实执行前复验脚本安全边界，并持久化当前校验策略摘要。activate 不再重复解析 AST 或 Metadata，而是严格加载 canonical 当前字节，只接受同 exact version 且校验策略摘要与当前 release 一致的成功持久化回测证据。上游负责交付可运行性；平台的完整持久化回测同时验证真实批量执行和标准输出。首次 activate 在同一命令内 insert-only 建立 draft 身份并原子激活，不再要求操作者先做一次不可观察的 shadow 转换。人工副作用仍绑定 canonical exact version、operator 与 operation scope。技术验证不授予生产写库、激活、服务或调度权限。详细流程见 [Blackbox 平台入库 SOP](docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)、[生产准备](docs/blackbox_v2/PRODUCTION_READINESS.md)、[Native 维护 SOP](docs/sop/NATIVE_V1_MAINTENANCE_SOP.md)和 [Harness 架构](docs/architecture/HARNESS_ARCHITECTURE.md)。

## 数据库边界

- Wind、指标与交易日历源表只读；写入只能经过第二条分层不变量列出的 repository/updater。
- 所有已发布 live 业务键永久 insert-only：完整重复可记 `skipped`，部分重复整批失败；授权 gap-fill 只要已有任一键就整组拒绝，任何修订都不得覆盖历史预测。
- Registry、run、prediction、Actual 与 backtest 的表身份和字段合同以迁移、模型和架构文档为准，根规范不维护枚举副本。

## 数据库迁移操作边界

- `migrations.runner` 是迁移行为的唯一实现：它只接收 caller-supplied `Engine`，负责 manifest、inspect、apply 与 `APPLYING` recovery；不得把环境变量、CLI 解析或运维授权逻辑放入该库层。
- `scripts/apply_migrations.py` 是唯一受控运维包装器。生产/候选 schema 的 apply 与 recovery 只能经此 CLI，不得用 `mysql` 客户端直跑 migration SQL，也不得复制 runner 行为到 scheduler、harness 或其它脚本。
- `--apply`、`--recover-applying-017 --apply`、`--recover-applying-018 --apply`、`--recover-applying-019 --apply`、`--recover-applying-021 --apply`、`--recover-applying-022 --apply`、`--recover-applying-023 --apply`、`--recover-applying-024 --apply` 都必须同时显式提供 `--expected-database-name` 和 `--expected-server-uuid`；CLI 在创建 Engine 前校验参数，并在首个写库动作前精确比对 `DATABASE()` 与 `@@server_uuid`。inspect 是只读操作，不需要这两个参数。
- operator 只能从只读 inspect JSON 或受控只读 identity query 取得 UUID；文档、脚本输出和提交中不得示例生产 UUID、DSN 或凭据。isolated MySQL 测试不等于已应用生产 migration。

## 编码规范

- Python: 遵循 PEP 8, type hints, docstring 用中文
- 前端: 原生 JS，无构建步骤，直接由 FastAPI serve
- 数据库字段: snake_case
- API 路径: kebab-case
- registry `scheme_id`(业务方案) / `base_scheme_id`(算法执行身份) / `benchmark_id`(基准批次) / `data_source`(数据口径) 命名分离

## 测试代码保留原则

- 长期测试只保留公共合同、数据不变量、安全边界、事务原子性、迁移恢复和调度控制面等跨版本防线。
- 针对单次故障、具体实现调用次数、源码字符串位置、一次性视觉或现场数据的测试，只用于当前开发验收；问题闭环后删除，不沉淀为永久回归。
- 同一行为优先由最高层、最稳定的合同测试覆盖；不得同时保留源码扫描、内部 helper、HTTP 和端到端四套重复断言。
- 上游算法内部行为由交付方负责；平台测试只验证 Blackbox/Native 合同与标准输出边界，不为单个方案长期复制算法内部测试。
