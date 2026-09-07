# 方案入库统一入口

**文档状态**：`CURRENT`

**目标读者**：平台维护人员、算法工程师、代码评审人员

本文只负责选择入库路径，不记录方案数量、运行结果或生命周期现状。动态事实查看[当前状态](../CURRENT_STATUS.md)。

## 选择流程

| 场景 | 必须使用的流程 |
|---|---|
| 新算法、新方案 ID、新目标期限或新任务类型 | Blackbox V2 |
| 现有 Native V1 的故障、数据口径或复现性修复 | Native V1 存量维护 |
| Native V1 的算法升级、替代实现或能力扩展 | 创建独立 Blackbox V2 trial |
| 查看当前方案状态或未关闭问题 | 当前状态或统一后续推进计划 |
| 查看历史规则和旧草案 | 使用 Git 历史；不得用于当前验收 |

不得通过复用旧 ID、复制 `predict.py + core/` 或修改 Native 白名单，把新算法伪装成存量维护。

## 两种运行时

| 维度 | Native V1 | Blackbox V2 |
|---|---|---|
| 机器标识 | `runtime_type: native_adapter` | `runtime_type: blackbox_v2` |
| 管理定位 | 既有身份的存量维护 | 后续新增方案唯一入口 |
| 上游形态 | 仓库内 `config + predict + core` | 一个 `.py` 和一个 `.json` |
| 输入 | `shared.input_artifacts` 从当前权威 `bond_db` 按 `feature_date` 截止构建；source-backed 身份连接同一实例和数据库，只使用现有 SELECT-only 身份 | `data_bridge_current` 五文件 Snapshot；自然运行使用当前 DataBridge generation，历史补缺严格绑定冻结 authority |
| 执行 | import adapter 子进程 | 受控 CLI 子进程 |
| 生产权限 | 保持既有方案的独立状态 | 每个方案必须单独完成生产准备和专项授权 |

两种运行时共用 Registry、版本、日期语义、`PredictionRecord`、业务表、API 和前端。运行时决定交付检查、Harness 入口、输入准备和算法执行驱动。

## 操作入口

### Blackbox V2

- 上游算法：[交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台人员：[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)
- 架构边界：[代码架构](../architecture/CODE_ARCHITECTURE.md)

### Native V1

- 文档入口：[Native V1 存量维护](../native_v1/README.md)
- 准入、改动分级、实施和验证：[存量维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)

### 共享规则

- [共享方案契约](../architecture/SCHEME_CONTRACT.md)
- [预测日期语义](../architecture/PREDICTION_SEMANTICS.md)
- [源算法保真](../architecture/SOURCE_ALGORITHM_FIDELITY.md)
- [Harness 架构](../architecture/HARNESS_ARCHITECTURE.md)

## 历史与灰度的最快正确路径

先确定方案级 `gray_target_start`。`target_date` 在起点以前的样本由一次持久化历史回测写入 immutable canonical backtest；起点及以后、正式调度以前的应有点由一次 target 区间批量执行写为 `gray_live`。区间入口按任务日历生成 live `predict_date`，一个方案只解析一次 DataBridge authority、核对一次 producer-ready receipt、物化一个私有运行视图并启动一个算法 batch，不再逐日期重复运行或重写输入快照。

两个批次绑定同一 exact version 和输入 lineage，但不跨激活保存临时候选结果，也不增加候选表、报告文件或新的 lifecycle 状态。固定未来 `source_end`、跨样本全局选择、版本或输入 lineage 不一致时必须停止批量物化，改走逐点 live-safe 计算。两侧 target 必须零重叠；灰度区间已有任一 live 键时整组拒绝，不能覆盖或删除后重写。完整规则见[预测日期语义 5.2](../architecture/PREDICTION_SEMANTICS.md#52-历史批次与灰度区间批次)和[平台入库 SOP 6](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md#6-灰度区间批量物化)。

## 当前工作流

Blackbox V2 不再进入 `harness onboard`。上游负责证明交付脚本可运行，平台只保留三个会产生新事实的步骤：

1. `intake-blackbox`：新 ID 原子接收两文件，完成 Metadata、固定 Runtime Profile/Data Schema、脚本语法与平台安全边界检查，生成 `paused/draft` canonical config；
2. `gate backtest --persist`：先对当前 canonical 脚本复验安全边界，再选择已有 producer-ready DataBridge generation，执行完整历史 Request 批次并原子写入 immutable backtest。它同时证明平台批量调用、Result 回显、数量、日期和持久化合同，不再提前做一次重复 predict；
3. `activate`：严格加载 canonical 当前字节哈希，只接受同 exact version、同当前脚本校验策略的成功持久化回测；首次激活在一个数据库事务内 insert-only 发布回测产品事实、建立 active version 与 active Registry。revision activation 只切换未来 Writer，不重写历史事实。没有独立 `shadow-register` 或 draft promotion。

同 ID 修订不重复创建方案目录，也不伪造第二次 Intake；修订 canonical `.py/.json` 后重新执行第 2、3 步。任何第三文件、symlink、危险导入或固定 Profile/Schema 漂移都会在回测前直接拒绝；回测后的任何字节漂移都会因 exact-version evidence 不匹配而阻断激活。

`gate dashboard` 是激活后的可选只读产品检查，不是入库门禁；`signal-gap-fill` 是独立授权的历史缺口操作，也不属于入库。

Native V1 存量仍使用 `onboard --stage all` 的
`static → dry-run → compare → backtest`，其中 Compare 是 source benchmark 保真证据，不能与已经删除的 Blackbox 伪 Compare 混为一谈。同一 Native 身份维护仍使用
`static → native-maintenance-admission → dry-run`。

所有人工副作用命令绑定 canonical exact version、operator 与 operation scope。Blackbox 激活从
`t_backtest_runs` 读取相同 version/code/config/manifest 与当前脚本校验策略摘要的成功回测，并复核 Runtime Profile、环境指纹、generation 与 snapshot；它不再读取 Harness `all`。首次激活和 revision 都只在本机数据库事务中切换 exact version 与 Registry，不维护 config overlay 或 lifecycle journal。

## 单维护者最快稳定路径

新 Blackbox 方案的长期最短链路是：

1. `intake-blackbox`；
2. 明确历史/live 分界后，执行一次完整 `gate backtest --persist`；
3. `activate`；
4. 仅在确有历史缺口时，单日执行 `signal-gap-fill --predict-date`，或对受支持的 Blackbox `weekly_point/h1`、日频 `T+5/h5` 方案执行一次 `--target-date-from/--target-date-before` 区间批量；
5. 如需产品验收，再运行可选的 `gate dashboard`。

这里保留的三个边界分别拥有不同的事实：不可变交付、不可变回测、生产状态切换。删除的
Static/Compare/shadow 没有提供第四种独立事实。

## 可复用测试矩阵

下列命令只列长期维护的系统合同测试。新方案不得复制一份以方案名、固定日期或固定 hash 命名的测试；方案特有但可复用的接口行为应加入现有参数化 conformance。

所有命令从仓库根目录运行，并固定服务环境：

```bash
conda activate bond_factor_lab_service
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
```

| 时机 | 验证目标 | 命令 | 通过标准 |
|---|---|---|---|
| 改动任意方案配置后 | active discovery、运行时身份和两文件入口 | `python -m pytest -q tests/test_active_scheme_contracts.py tests/test_config_schema.py tests/test_onboarding_policy.py` | 全部通过 |
| 新增或修订 Blackbox V2 平台合同后 | 通用 Contract、Intake 与 discovery | `python -m pytest -q tests/test_blackbox_v2_contracts.py tests/test_blackbox_v2_intake.py tests/test_blackbox_v2_discovery.py` | 全部通过。新 ID 通过 Intake 收包；同 ID 修订替换 canonical 两文件后，由持久化回测入口重新执行同一安全校验。具体交付不永久复制专项 pytest；截止隔离、跨批结果等价及显式增量方案的复用语义属上游义务 |
| 修改 Blackbox 可选状态能力后 | 状态身份、路径、独占、原子恢复及维护入口 | `python -m pytest -q tests/test_blackbox_state.py tests/test_blackbox_v2_harness_gates.py` | 全部通过；非法 Result/身份/路径/第二 Writer 不发布，私有回测不推进生产状态，显式重建不写业务事实 |
| 修改 Blackbox 平台适配后 | generation 读取、runner、完整回测与激活证据 | `python -m pytest -q tests/test_data_bridge_current.py tests/test_blackbox_v2_runner.py tests/test_blackbox_v2_harness_gates.py tests/test_blackbox_activation.py` | 全部通过 |
| 修改 Registry、API 或前端后 | active 方案可见性、actual join、Dashboard 基础状态与 Harness HTTP 验收 | `python -m pytest -q tests/test_repository_registry.py tests/test_backend_api.py tests/test_factor_lab_dashboard_api.py tests/test_dashboard_gate.py` | 全部通过 |
| 修改 Native 存量适配后 | 当前数据库输入、执行器和 source isolation | `python -m pytest -q tests/test_native_input_artifacts.py tests/test_native_executor.py tests/test_source_runner_database_isolation.py` | 全部通过；不得修改 Native core 算法口径 |

按实际改动选择对应行，不为单个方案入库重复执行无关的全仓测试。`tests/` 只保留跨方案复用的长期合同，
不保存单次事故、迁移实施或生产 rollout 的永久回归。Native 的 `harness onboard ... --stage all`
验收精确方案版本和真实输入证据；Blackbox 由持久化回测验收当前 exact version。pytest 只保护本次修改触及的平台代码边界，不与单次方案验收重复承担同一职责。

## 版本与政策

- `Blackbox V2`：运行时代际。
- `schema_version=1.0`：上游接口合同。
- `data-bridge-v1`：数据 Schema。
- `blackbox-v2-v1`：运行 Profile。
- `deploy/onboarding_policy_v1.json`：Native V1 存量身份的机器白名单。

机器白名单只决定身份能否进入 Native 维护 Gate，不能把算法升级变成存量修复。
