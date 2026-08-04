# Native V1 修改后验证 SOP

**文档状态**：`LEGACY_MAINTENANCE`
**适用运行时**：`native_adapter`
**目标读者**：验证存量 Native V1 修复的工程师
**最后核验日期**：2026-08-04

本 SOP 验证现有 Native V1 维护，不用于新增方案。

首次技术入库仍使用完整七段 `all`，其中 source benchmark/CompareGate 是硬证据。ActivationGate 的两条 profile 互斥：current exact version 的 `all` 通过时使用 `full_initial_onboarding_v1`，不要求 `native-maintenance` 或 prior snapshot；只有未走该 profile、仍在政策清单且 prior `all` 的 `static.business_identity` 已持久化并与当前 `runtime_type`、`task_type`、`frequency`、`horizon`、target tenors 和全部 active composite Registry IDs 精确匹配的同一业务身份修订，才可使用 `native-maintenance`；快照不含代码、config 或 version hash。legacy admission 缺快照时一律 fail-closed，当前唯一已实现路径是完整 `all`（含当前 Compare）。`legacy admission identity attestation` 尚未设计或实现，不得自动生成、推断或作为当前选择。满足任一已实现 profile 时，历史 source-benchmark 输入 vintage 漂移才只归档，不能单独阻断 activation、gap repair、`gray_live`、`scheduled_live` 或 API；它不放宽 L0/L1/L2、输入截止、统一周历、日期语义、live-safe oracle 或授权。

## 1. 静态和单元验证

```bash
python -m harness gate static --scheme-id {scheme_id}
python -m harness gate unit --scheme-id {scheme_id}
```

确认：

- 方案在 Native 白名单；
- config、目录和 `SCHEME_ID` 一致；
- adapter 走统一输入；
- core 无 DB、写文件和跨方案依赖；
- 修改覆盖了原问题和失败路径。

## 2. 输入和单点 Dry Run

```bash
python -m harness gate input \
  --scheme-id {scheme_id} \
  --predict-date YYYY-MM-DD

python -m harness gate dry-run \
  --scheme-id {scheme_id} \
  --predict-date YYYY-MM-DD
```

确认输入 source、data version、列、时间范围、`feature_date` 截止和输出日期关系。Dry Run 前后业务表行数必须不变。

## 3. 保真对比

```bash
python -m harness gate compare --scheme-id {scheme_id}
```

source-backed 方案在首次技术入库必须比较最终方向和原算法能导出的内部字段。跨 gray/live 边界时先标记 benchmark role，只比较相同执行口径；live 使用 `feature_date` 硬截止 oracle。已入库同一身份修订不得手工跳过 CompareGate：只有匹配 prior `static.business_identity` 的 maintenance profile 才不运行当前 historical compare/backtest，并以 prior admission、业务快照与 live-safe oracle 留证。缺 legacy snapshot 时当前回到完整 `all`；attestation 尚未实现，不是当前 bypass。

## 4. 回测 No Persist

```bash
python -m harness gate backtest --scheme-id {scheme_id}
```

确认：

- 默认 `persist=false`；
- 样本数、日期范围和方向分布符合预期；
- historical target 不与 gray/live 重叠；
- 回测执行前后实盘表、Registry 和 actuals 不变；
- 相同输入重复执行结果一致。

## 5. 自动验证路径

首次技术入库使用：

```bash
python -m harness onboard {scheme_id} \
  --predict-date YYYY-MM-DD \
  --stage all
```

报告必须绑定当前 `scheme_version`，七个 Gate 全部 passed。`api-readiness` 不是 active API 验收。

已入库、且有匹配 prior `static.business_identity` 快照的同一身份修订使用：

```bash
python -m harness onboard {scheme_id} \
  --predict-date YYYY-MM-DD \
  --stage native-maintenance
```

该阶段固定为 `static -> native-maintenance-admission -> input -> unit -> dry-run -> api-readiness`，不支持 `--check-only`。它必须持久化当前六个 Gate，并只读证明不同 prior Native active version 的 passed `all + compare`、该 prior `static.business_identity` 与当前 active composite Registry identity 精确匹配；快照只读业务字段，不含代码/config/version hash。不运行当前 historical `compare/backtest`，不写业务表。legacy admission 缺快照时 fail-closed，不能把当前 Registry 或代码版本反推为旧身份；当前只能完整 `all`。`legacy admission identity attestation` 尚未设计或实现，不能作为当前操作。

## 6. 授权后核验

如本次维护需要受控写入或恢复 active 状态：

1. 保存业务表、Registry、版本和 scheduler 前置状态，并记录 validation profile、Harness run 和 prior admission（若适用）；
2. 使用绑定 exact scheme/version/action 的短期 token；
3. 执行授权操作；
4. 独立核对数据库、scheduler 和 API；
5. 失败时执行 reconciliation，不覆盖历史版本。

## 7. 验收结论

结论必须写明：

- 修复级别和问题根因；
- 验证使用的 source role 或 live-safe oracle；
- Harness run 和 scheme version；
- 业务表副作用；
- 当前 Registry、scheduler 和 API 状态；
- 未解决风险。

不得把“Gate 通过”扩大表述为算法效果提升或新方案正式入库。
