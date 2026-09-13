# Bond Factor Lab — 项目规范

本文件维护全局约束、操作权限与任务导航；专题合同的权威归属见[文档中心](docs/README.md#权威来源与职责)。[CLAUDE.md](CLAUDE.md) 仅引导读取。用户说根目录 `agent.md` 时，按本文件理解。
项目是独立的国债因子实盘测试平台，前端由本项目 FastAPI 提供。

## 开始任务前

本规范适用于整个仓库。从[文档中心的任务表](docs/README.md#按任务查找)选择当前任务，实际打开对应权威正文；不要求依次读取全部专题。普通 Markdown 链接是导航，不代表内容已经加载。不能从旧会话、历史计划或文件名推断当前操作规则。

涉及现场操作时，另读[当前状态](docs/CURRENT_STATUS.md)、[待办](docs/TODO.md)和[部署访问](docs/operations/DEPLOYMENT_ACCESS.md)，重新核验目标机；文档基线不代替现场。

## 开发与操作权限

- 集成分支为 `codex/develop`，不得建立 ECS/Mac3 长期环境分支。`master` 受保护，只有用户明确确认后才能合并、移动或推送。
- 分支操作、暂存或提交前核对工作区与相关分支；`outputs/`、未跟踪新方案和草稿未经明确要求不得纳入提交。
- 技术验证不授予业务写库、激活、DDL、服务重启或调度修改权限。生产操作先核实影响范围、在途任务、触发窗口和可恢复边界，在用户授权范围内执行。已明确授权的步骤连续完成，不重复索取同一授权；未决业务选择和新增操作范围仍须确认。
- DNS、Nginx、认证、SSH 隧道或跨机 Writer 切换不得从算法交付授权外推。历史文档、代码示例和已完成计划不构成新授权。
- 发现身份冲突、合同失败、输入证据不匹配、需要改变算法或扩大操作范围时，停止对应操作，保留证据并向用户确认，不猜测继续。
- 默认主 agent 直接执行。只有可安全拆成独立并行工作、且能显著节省时间时才使用 subagent；明确文件范围、验收条件和禁止事项，避免共享写入。主 agent 负责整合与最终验证；委派不扩大任何操作权限。
- 提 PR 前判断能否在本机取得决定性验证。本机可验证的直接修复并附证据；只能结合生产确认的，不夹带未经验证的代码改动，只提交「现象 / 问题点 / 造成的影响 / 推荐解决方案」报告 PR，交由能核验现场的同事处理。

## 环境与发布

- 坚持单一代码线、不可变 release、独立环境。release 由精确提交生成确定性 archive，包内无 `.git`，身份来自已校验 manifest；生产端不得现场修改源码。环境差异只允许放在部署配置、环境变量、部署矩阵和依赖清单。
- 开发工作区与生产 runtime 分离，生产使用 `current -> releases/<release_id>`，状态和日志外置。ECS 先验证，Mac3 只能晋级同一份已验证 archive；阶段性 current 不同是允许的。
- ECS 是独立灰度实验室，Mac3 承载生产；各自使用 MySQL、DataBridge、Actuals、调度和前端，不建立持续复制、双写或跨机共享输入。
- 跨 release 的输入、缓存和派生状态必须通过既有 manifest、lineage、business digest、input state 与 ready gate 才能复用。源码链接回滚不等于数据库、解释器或运行状态回滚；不得用旧整库快照覆盖持续增长的事实。
- 跨机精确结果补缺须单独授权：只允许同一已验证 release、exact、日期、输入摘要和 lineage 的结果，先隔离目标 Writer，再经目标 repository insert-only 导入；不得复制数据库主键、run_id、Actuals、回测或 Harness 历史。身份不匹配时禁止复用，计算另按授权执行。
- 环境方案范围只由 `BFL_DEPLOYMENT_TARGET` 与[部署矩阵](deploy/scheme_deployment_matrix_v1.json)表达，不能修改两端 canonical status 制造不同 exact version。
- Mac3 只用 launchd/plist，ECS 只用 systemd one-shot/timer；仓库模板不证明 installed/loaded。不得恢复常驻 Python scheduler，或新增 ledger、occurrence、epoch、runtime/config 闭包。新增调度须明确触发、时区、漏触发和失败恢复语义。
- 生产 plist/unit 替换、bootstrap/bootout/kickstart 和服务重启属于生产操作；先核对模板、installed 与 loaded 状态并取得明确授权。自然运行必须由真实宿主时钟与本机 run/prediction 证明，模拟或补缺不能冒充。

## 算法与数据不变量

1. **输入单点**：算法输入只能经 `shared.input_artifacts` 产出；adapter/backtest runner 不得自拼 DB 输入，Wind、指标与日历源表只读。
2. **写库单点**：业务写入仅经 `scheduler.repository`、`backtests.repository`、`*_actuals_updater`；认证写入仅经 `backend.auth.repository` 的三张认证表。DDL 仅经受控迁移入口，不是上述业务写权限的扩展。
3. **Native core 纯净**：非 legacy 的 `schemes/*/core/` 零 DB、零写库、零跨方案 import；Blackbox 不向平台暴露 core。
4. **源算法保真**：source-original 与 live-safe 真值分开，禁止调参贴结果。L0/L1/L2 仅解释存量适配与历史改动，不授予 Native 新版本维护入口。Blackbox 内部保真由上游负责，平台验证接入与标准输出边界。

- 所有新算法、新 ID、新目标、新任务、修订与替代版本只允许 Blackbox V2。[存量清单](deploy/onboarding_policy_v1.json)中的 W4 九套固定现有 Native 版本，仅保障 Mac3 既有日频、周频和月频调度及运行依赖，不改造、不部署 ECS。不再提供 Native 入库、maintenance、新版本激活或独立历史回测，也不为已迁移方案维护或扩展 Native Harness。运行故障按现有部署与调度边界处理，不借此重开 Native 版本修订。
- 同算法运行时升级保留原 base/Registry ID，以真实新 exact 区分版本，只切未来唯一 Writer，不重算、覆盖或搬删既有历史。包装迁移、临时身份退役和旧附件清理必须遵守[保真与迁移边界](docs/architecture/SOURCE_ALGORITHM_FIDELITY.md)，不能借清理跳过验收或删除被历史引用的来源。
- 已发布业务键永久 insert-only；重复、部分重复与授权区间补缺按[预测语义](docs/architecture/PREDICTION_SEMANTICS.md)处理。源数据修订不触发已发布预测重算，任何历史删除须独立精确授权与恢复证据。
- 平台不要求、提取、传递、存储或展示统一 `confidence`。算法内部概率、阈值、排序、投票、模型选择及方向计算必须保留；原始 benchmark、旧迁移/release、已有 source_row/extra 审计不按关键词改写，仍服务其他数值字段的 helper 不得删除。
- 私有增量状态仅供显式 `incremental_state: true` 的方案使用，不成为源数据或业务事实、不跨方案共享；回测不推进生产状态，失败不自动 fallback。合同与显式重建步骤见[上游合同](docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)及[平台 SOP](docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)。
- 运行时按显式 `runtime_type` 分派，身份与任务不得按目录内容或 frequency/horizon 猜测。产品只读 `/api/factor-lab/dashboard`，不得恢复分散展示 API 或浏览器第二套聚合；具体字段、可见性及日期规则由上述权威合同定义。

## 数据库迁移

只允许通过 [scripts/apply_migrations.py](scripts/apply_migrations.py) 调用 `migrations.runner`，不得用 mysql 客户端直跑 migration SQL，也不得在其他模块复制迁移逻辑。目标库身份围栏、inspect/recover、state digest、备份和版本兼容要求统一见[部署手册](deploy/README.md)。只读 inspect 与隔离测试不等于生产 apply；生产 UUID、DSN 和凭据不得进入文档、脚本示例或提交。

## 编码与测试

- Python 遵循 PEP 8、type hints，docstring 用中文；前端为原生 HTML/CSS/JS，无构建步骤。
- 数据库字段使用 snake_case，API 路径使用 kebab-case；执行身份、Registry 身份、benchmark 与 data_source 分开命名。
- 长期测试只保留公共合同、数据不变量、安全、事务原子性、迁移恢复及调度控制面等跨版本防线。同一行为优先由最高层、最稳定合同覆盖，不保留多层重复断言。
- 单次故障、实现调用次数、源码字符串、一次性视觉/现场数据和方案内部验收测试，闭环后不沉淀为永久回归；已退役实现对应的一次性测试随之清理。上游算法内部测试由交付方负责。

## 完成与文档维护

- 先确定本次交付的可验证结果，再按[现有验证矩阵](docs/onboarding/README.md#可复用测试矩阵)选择相关检查；报告实际结果、跳过项和未解决问题，不以文件生成或命令退出成功代替业务验收。纯文档变更按[文档维护与验收](docs/README.md#维护与验收)检查，不触发算法或生产操作。
- 规则随对应实现或业务决定同步更新；新增、迁移与删除规则都按文档中心的维护标准执行，不在根规范继续追加单次问题流水。
