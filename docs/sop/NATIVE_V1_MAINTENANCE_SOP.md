# Native V1 存量维护 SOP

**文档状态**：`LEGACY_MAINTENANCE`
**适用运行时**：`native_adapter`
**目标读者**：平台维护人员
**最后核验日期**：2026-07-19

本 SOP 只维护已登记的 Native V1 方案，不接受新增方案。新算法和替代版本使用 [Blackbox V2 平台 SOP](BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md)。

## 1. 接收维护任务

记录：

- `scheme_id`、当前 `scheme_version`、Registry composite ID 和状态；
- 问题现象、影响日期、目标期限和任务类型；
- 修改分级 L0/L1 及证据来源；
- 当前代码、配置、benchmark 和关键数据库只读快照；
- 明确禁止变化的算法锚点。

先确认方案位于 `deploy/onboarding_policy_v1.json`。不在白名单时立即停止，不能补写白名单后继续。

## 2. 限定修改范围

允许修改现有方案目录、对应回测 runner、方案专属测试和状态记录。除非任务已拆为独立平台改造，不得修改：

- `shared` 公共输入、日历和模型契约；
- scheduler、backend、frontend、Harness 公共 Gate；
- migrations 和数据库 Schema；
- 其他方案目录；
- Registry 身份、target 或 task type。

维护期间不得把外部文件、历史 benchmark 或 source evidence 变成生产运行输入。

## 3. 实现和自验

1. 保持 `config.yaml`、目录名和 `predict.py::SCHEME_ID` 一致。
2. 保持 `runtime_type=native_adapter` 和 `input_source=legacy_db`。
3. adapter 只负责输入、日期、调用 core 和构造 `PredictionRecord`。
4. core 不接触数据库、平台写库和其它方案。
5. backtest 与 live 使用同一输入口径和算法核心。
6. source-backed 方案同时比较方向和可导出的内部字段。
7. future `source_end` benchmark 只能验证 source-original backtest，不能作为 live 真值。

## 4. 自动 Gate

先执行单项 Gate 定位问题，再执行完整自动段：

```bash
python -m harness onboard {scheme_id} \
  --predict-date YYYY-MM-DD \
  --stage all
```

固定顺序：

```text
static -> input -> unit -> dry-run -> compare -> backtest -> api-readiness
```

必须确认：

- StaticGate 同时通过 Native 白名单、配置、adapter、core 和 import 边界；
- InputGate 使用 `shared.input_artifacts`；
- DryRunGate 不写业务表；
- CompareGate 按 source role 比较正确证据；
- BacktestGate 默认 `no-persist`；
- ApiReadiness 只表示激活前结构准备，不是 active API 验收。

任一 Gate 失败时从 static 重跑完整自动段，不跳过失败项。

## 5. 数据与结果核验

至少验证：

- `predict_date / feature_date / target_date` 符合对应频率语义；
- 输入严格截止到 `feature_date`；
- 方向值仅为 `-1/0/1`；
- 方向和内部 score 与正确角色的 benchmark 或 live-safe oracle 一致；
- 相同输入和日期重复执行结果一致；
- 回测不包含 gray/live target 区间；
- `predicted_direction=0` 不进入准确率分母。

## 6. 授权副作用

自动段通过后，任何 persist、live 写库或状态切换仍使用既有一次性授权流程。操作前后独立查询：

- Registry 和版本状态；
- `t_scheme_runs`、预测表和 run log；
- `t_backtest_*`；
- scheduler active job；
- `/api/schemes`、metrics 和 factor-lab backtest。

仅维护当前方案，不得改变其它方案记录。失败时保持或恢复原状态，保存审计证据，不手工删除历史版本。

## 7. 完成条件

- [ ] 身份仍在 Native 白名单且未改变。
- [ ] 改动保持 L0/L1，没有 L2 算法升级。
- [ ] 七个自动 Gate 全部通过。
- [ ] no-persist、重复和日期截止验证通过。
- [ ] 授权写入只影响允许的当前方案记录。
- [ ] API、scheduler 和 Registry 与预期一致。
- [ ] `CURRENT_STATUS` 记录修复事实和剩余风险。

不满足任一项时不得宣称维护完成。
