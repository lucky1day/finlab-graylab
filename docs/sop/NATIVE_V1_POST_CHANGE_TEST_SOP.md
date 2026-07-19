# Native V1 修改后验证 SOP

**文档状态**：`LEGACY_MAINTENANCE`
**适用运行时**：`native_adapter`
**目标读者**：验证存量 Native V1 修复的工程师
**最后核验日期**：2026-07-19

本 SOP 验证现有 Native V1 维护，不用于新增方案。

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

source-backed 方案必须比较最终方向和原算法能导出的内部字段。跨 gray/live 边界时先标记 benchmark role，只比较相同执行口径；live 使用 `feature_date` 硬截止 oracle。

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

## 5. 完整自动段

```bash
python -m harness onboard {scheme_id} \
  --predict-date YYYY-MM-DD \
  --stage all
```

报告必须绑定当前 `scheme_version`，七个 Gate 全部 passed。`api-readiness` 不是 active API 验收。

## 6. 授权后核验

如本次维护需要受控写入或恢复 active 状态：

1. 保存业务表、Registry、版本和 scheduler 前置状态；
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
