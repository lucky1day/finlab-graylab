# 方案入库统一入口

**文档状态**：`CURRENT`

**目标读者**：平台维护人员、算法工程师、代码评审人员

**最后核验日期**：2026-08-10

本文只负责选择入库路径，不记录方案数量、运行结果或生命周期现状。动态事实查看[当前状态](../CURRENT_STATUS.md)。

## 选择流程

| 场景 | 必须使用的流程 |
|---|---|
| 新算法、新方案 ID、新目标期限或新任务类型 | Blackbox V2 |
| 现有 Native V1 的故障、数据口径或复现性修复 | Native V1 存量维护 |
| Native V1 的算法升级、替代实现或能力扩展 | 创建独立 Blackbox V2 trial |
| 查看当前方案状态或未关闭问题 | 当前状态或全方案问题台账 |
| 查看历史规则和旧草案 | 使用 Git 历史；不得用于当前验收 |

不得通过复用旧 ID、复制 `predict.py + core/` 或修改 Native 白名单，把新算法伪装成存量维护。

## 两种运行时

| 维度 | Native V1 | Blackbox V2 |
|---|---|---|
| 机器标识 | `runtime_type: native_adapter` | `runtime_type: blackbox_v2` |
| 管理定位 | 既有身份的存量维护 | 后续新增方案唯一入口 |
| 上游形态 | 仓库内 `config + predict + core` | 一个 `.py` 和一个 `.json` |
| 输入 | `shared.input_artifacts` 从当前权威 `bond_db` 按 `feature_date` 截止构建；source-backed 身份连接同一实例和数据库，只使用现有 SELECT-only 身份 | `data_bridge_current` 三频 Snapshot；自然运行使用当前 DataBridge generation，历史补缺严格绑定冻结 authority |
| 执行 | import adapter 子进程 | 受控 CLI 子进程 |
| 生产权限 | 保持既有方案的独立状态 | 每个方案必须单独完成生产准备和专项授权 |

两种运行时共用 Registry、版本、Harness 编排、日期语义、`PredictionRecord`、业务表、API 和前端。运行时只决定交付检查、输入准备和算法执行驱动。

## 操作入口

### Blackbox V2

- 上游算法：[交付 SOP](../sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md)
- 平台人员：[平台入库 SOP](../sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)
- 生产准备：[生产准备清单](../blackbox_v2/PRODUCTION_READINESS.md)
- 架构边界：[Blackbox V2 平台架构](../architecture/BLACKBOX_V2_PLATFORM.md)

### Native V1

- 文档入口：[Native V1 存量维护](../native_v1/README.md)
- 维护判断：[存量维护 T0](../sop/NATIVE_V1_MAINTENANCE_T0.md)
- 维护实施：[存量维护 SOP](../sop/NATIVE_V1_MAINTENANCE_SOP.md)
- 修改验证：[修改后验证 SOP](../sop/NATIVE_V1_POST_CHANGE_TEST_SOP.md)

### 共享规则

- [共享方案契约](../architecture/SCHEME_CONTRACT.md)
- [预测日期语义](../architecture/PREDICTION_SEMANTICS.md)
- [源算法保真](../architecture/SOURCE_ALGORITHM_FIDELITY.md)
- [Harness 架构](../architecture/HARNESS_ARCHITECTURE.md)

## Harness 当前工作流

- `python -m harness onboard {scheme_id} --predict-date YYYY-MM-DD --stage all` 按 `runtime_type` 执行技术 Gate：
  Blackbox V2 为四段 `static → input → unit → compare`；Native V1 为六段
  `static → input → unit → dry-run → compare → backtest`。该流程不访问 Backend。
  Blackbox 的 dry-run 已并入其 CompareGate——两者的 baseline 是同一次 predict。
  `gate dry-run` 与 `gate backtest` 仍作为独立命令保留。
- CompareGate 只做两件事：校验平台输入逐字节等于声明值，以及一次冒烟 predict 证明交付在
  平台喂进去的输入下能产出合法 Result。交付自身的性质——重复执行确定性、predict/backtest
  一致、截止隔离、跨请求无状态——由上游按其交付契约保证，平台不重验。批次切分与顺序的
  回填正确性由 `load_backtest_results` 在每一次 backtest 校验（行数相等、逐行回显
  Request 字段），强于只在入库时跑一次。
- Native 同一业务身份维护固定执行五段：`static → native-maintenance-admission → input → unit → dry-run`。
- 激活后的 HTTP 验收只使用 `DashboardGate`，只验证 `/api/factor-lab/dashboard` 当前业务可见性；Dashboard 响应不携带 exact version，不能用来证明版本身份。
- 单日信号补缺使用 `python -m harness signal-gap-fill --predict-date YYYY-MM-DD`；需要限制为单个方案时增加 `--scheme-id {base_scheme_id}`。命令直接扫描并补齐真实缺口，不接收 token、operator、frozen plan、plan SHA 或日期范围。
- Native 补缺按指定日期从当前权威数据库重建输入；Blackbox 使用冻结 DataBridge replay。整批算法必须先全部成功，才按 group insert-only 写入 `gray_live`；任一算法失败则 prediction 零提交，完成后只做一次最终权威 readback。
- Blackbox lifecycle 只要存在 pending journal 就阻断后续生命周期动作，不做隐式恢复。仅显式 `blackbox_reconcile` HMAC 操作可以恢复 previous safe state；原 journal 保持不变，并新增 linked reconciliation journal。

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
| 收到或修订 Blackbox V2 交付后 | Contract、Intake、discovery，以及现役交付对**平台侧数据接入变更**的耐受（DataBridge 加列、改时间键、目标日缺日频行） | `python -m pytest -q tests/test_blackbox_v2_contracts.py tests/test_blackbox_v2_intake.py tests/test_blackbox_v2_discovery.py tests/test_active_blackbox_conformance.py` | 全部通过。交付自身的截止隔离与跨批无状态属上游义务，不在此矩阵内 |
| 修改 Blackbox 平台适配后 | 输入 cutoff、runner、六段 Gate | `python -m pytest -q tests/test_data_bridge_current.py tests/test_blackbox_v2_runner.py tests/test_blackbox_v2_harness_gates.py` | 全部通过 |
| 修改 Registry、API 或前端后 | active 方案可见性、actual join、Dashboard 基础状态与 Harness HTTP 验收 | `python -m pytest -q tests/test_repository_registry.py tests/test_backend_api.py tests/test_factor_lab_dashboard_api.py tests/test_dashboard_gate.py` | 全部通过 |
| 修改 Native 存量适配后 | 当前数据库输入、执行器和 source isolation | `python -m pytest -q tests/test_native_input_artifacts.py tests/test_native_executor.py tests/test_source_runner_database_isolation.py` | 全部通过；不得修改 Native core 算法口径 |
| 提交入库版本前 | 入库核心合同全集 | `python -m pytest -q` | 无失败；跳过项必须是已知的外部环境条件 |

`tests/` 只保留跨方案复用的入库合同，不保存单次事故、迁移实施或生产 rollout 的永久回归。`harness onboard ... --stage all` 仍是方案入库 Gate，不由上述 pytest 代替；pytest 保护平台代码合同，Harness 验收精确方案版本和真实输入证据。

## 版本与政策

- `Blackbox V2`：运行时代际。
- `schema_version=1.0`：上游接口合同。
- `data-bridge-v1`：数据 Schema。
- `blackbox-v2-v1`：运行 Profile。
- `deploy/onboarding_policy_v1.json`：Native V1 存量身份的机器白名单。

机器白名单只决定身份能否进入 Native 维护 Gate，不能把算法升级变成存量修复。
