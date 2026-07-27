# FengRL 五个月度方案技术入库记录

**批次开始日期**：2026-07-26

**最后更新日期**：2026-07-27

**分支**：`codex/blackbox-v2-monthly-fengrl-review-20260726`

**工作树**：`/Users/macstudio0/.config/superpowers/worktrees/bond-factor-lab/blackbox-v2-monthly-fengrl-review-20260726`

**批次状态**：`IN_PROGRESS_4_OF_5`

## 固定边界

本批次只做 Blackbox V2 技术入库准备。所有 Gate 使用
`onboard --stage all --check-only`；不激活 Registry，不运行
gray/scheduled live，不执行持久化 backtest，不修改 rollout/admission、
BondProjectPro 或日频 coordinator，不合并、不推送、不部署。

正式交付仅接收每个方案同名的 `.py + .json`。上游随包
`api_wind_date.csv`、样例、开发检查和交接文档均不进入方案目录；
平台日历由声明的 `api-wind-date-v1` provider 从权威
`api_wind_date` 捕获。

## 串行状态

| 顺序 | 方案 | Intake | 七 Gate | 技术状态 |
|---:|---|---|---|---|
| 1 | `cgb_a4_fundseason_1y` | passed | 7/7 passed | `TECHNICAL_GATES_PASSED_CHECK_ONLY` |
| 2 | `cgb_a4_fundseason_3y` | passed | 7/7 passed | `TECHNICAL_GATES_PASSED_CHECK_ONLY` |
| 3 | `cgb_a4_fundseason_5y` | passed | 7/7 passed | `TECHNICAL_GATES_PASSED_CHECK_ONLY` |
| 4 | `cgb_a4_fundseason_7y` | passed | 7/7 passed | `TECHNICAL_GATES_PASSED_CHECK_ONLY` |
| 5 | `cgb_a4_fundseason_10y` | 未执行 | 未执行 | `PENDING_NOT_INTAKEN` |

## 1Y 技术证据

- Intake 的私有临时目录恰好包含两个普通文件；入库后的 delivery
  字节与上游完全一致。
- Metadata 的 `description` 非空，`scheme_id`、`target_tenor=1Y`、
  `task_type=monthly` 和 `horizon=1` 均通过 StaticGate。
- 配置保持 `status: paused`、`version_status: draft`、
  `runtime_type: blackbox_v2`，并声明
  `platform_inputs: [api-wind-date-v1]`。
- 成功 check-only run：
  `hr_20260727T012116Z_4dd70ba51581`。
- static、input、unit、dry-run、compare、backtest 和 api-readiness
  按固定顺序 7/7 passed。
- 报告明确记录 `check_only=true`、
  `control_plane_persisted=false`、
  `business_tables_written=false` 和 `persist_backtest=false`。
- DataBridge 父快照仅含三个业务 CSV；组合输入 ID 为
  `snapshot-b24bd78475f367ed858c2658`，父 ID 为
  `snapshot-46ff3231de2c4a080c46ba56`。
- `api-wind-date-v1` 规范化为 6064 行，SHA-256 为
  `816d44d062af74c40fe6b7c8a7a0369a0813c1bfd46250b4c344d26206a92ef8`；
  同一组合 ID、父 ID 和日历 SHA 贯穿 Input 后六 Gate。
- `weekly_cutoff_key=202628` 被权威日历覆盖。
- CompareGate 的重复确定性、predict/backtest 一致、分批、逆序、
  未来业务行隔离和平台输入哈希不变均通过。
- no-persist backtest 为 100 requests / 100 records，
  `persist=false`。
- structural API readiness 保持 `paused`、scheduler 不可执行、
  API 不可见，没有执行真实 Registry 或 HTTP/scheduler 验收。
- 运行前后，该方案在 Registry、scheme version、input artifact、
  prediction、scheme run、run log、serving pointer、backtest 和
  Harness 控制面各表的只读计数均为 0，增量均为 0。

成功报告保留在忽略目录：
`reports/harness/cgb_a4_fundseason_1y/20260727T012116Z/`。
结构化证据见本 records 目录
`MONTHLY_ONBOARDING_FENGRL_5SCHEMES_20260726.evidence.json`。

## 3Y 技术证据

- Delivery 的 Python/JSON SHA-256 分别为
  `fdc314eb323055efdd8b11ab34666373d6cdc7d226a3385d079c59a72843802e`
  和
  `e0fe7c6b230472d9d6f7aa67ddb5d39f2572b3678348507993764e7220d8ed61`，
  与上游逐字节一致。
- Metadata 的 description 非空并明确 3Y；配置保持
  `paused/draft/blackbox_v2` 和
  `platform_inputs: [api-wind-date-v1]`。
- 成功 check-only run：
  `hr_20260727T013903Z_a429d69eb4dd`；七 Gate 按固定顺序
  7/7 passed。
- 组合 ID、父 ID、三频文件和日历 SHA 与本批次冻结输入一致；
  `weekly_cutoff_key=202628` 被权威日历覆盖。
- CompareGate 六项隔离/确定性断言全部通过；no-persist backtest 为
  100 requests / 100 records；structural API readiness 保持 paused、
  scheduler 不可执行、API 不可见。
- 运行前后，该方案在 Registry、版本、输入、预测、运行、serving
  pointer、backtest 和 Harness 控制面各表均为 0，增量均为 0。

成功报告：
`reports/harness/cgb_a4_fundseason_3y/20260727T013903Z/`。

## 5Y 技术证据

- Delivery 的 Python/JSON SHA-256 分别为
  `5c3d48908db898491637f51edf9f04d5969f6d64ef41529300c02c42fb02b080`
  和
  `a7eb84763b7b9f713facdcbd2a3efe717ac56636ac617f08905a3be4f858c7e5`，
  与上游逐字节一致。
- Metadata 的 description 非空并明确 5Y；配置保持
  `paused/draft/blackbox_v2` 和
  `platform_inputs: [api-wind-date-v1]`。
- 成功 check-only run：
  `hr_20260727T014817Z_e4e42e9ba3d8`；七 Gate 按固定顺序
  7/7 passed。
- 组合 ID、父 ID、三频文件和日历 SHA 与本批次冻结输入一致；
  `weekly_cutoff_key=202628` 被权威日历覆盖。
- CompareGate 六项隔离/确定性断言全部通过；no-persist backtest 为
  100 requests / 100 records；structural API readiness 保持 paused、
  scheduler 不可执行、API 不可见。
- 运行前后，该方案 12 项方案级数据库计数均为 0，增量均为 0。

成功报告：
`reports/harness/cgb_a4_fundseason_5y/20260727T014817Z/`。

## 7Y 技术证据

- Delivery 的 Python/JSON SHA-256 分别为
  `86c8c5d98120d872d3d5d15e303b3ee2b921266363cb774f785544625fd6b87d`
  和
  `dbeb9133734e43feb23aa6d201ed42d3ea9822e7ed465ffb78e54ccc030161ed`，
  与上游逐字节一致。
- Metadata 的 description 非空并明确 7Y；配置保持
  `paused/draft/blackbox_v2` 和
  `platform_inputs: [api-wind-date-v1]`。
- 成功 check-only run：
  `hr_20260727T015710Z_e737d5c08926`；七 Gate 按固定顺序
  7/7 passed。
- 组合 ID、父 ID、三频文件和日历 SHA 与本批次冻结输入一致；
  `weekly_cutoff_key=202628` 被权威日历覆盖。
- CompareGate 六项隔离/确定性断言全部通过；no-persist backtest 为
  100 requests / 100 records；structural API readiness 保持 paused、
  scheduler 不可执行、API 不可见。
- 运行前后，该方案 12 项方案级数据库计数均为 0，增量均为 0。

成功报告：
`reports/harness/cgb_a4_fundseason_7y/20260727T015709Z/`。

## 恢复与失败历史

1. `hr_20260726T191201Z_fe2d9a05f052` 在 InputGate 因 fresh worktree
   的 `data/data_bridge` 为 `0755` 而 fail-closed。之后从既有 sealed
   DataBridge publication 物化普通文件私有副本，并用平台官方
   `check_current_dataset` 复验 generation、manifest、state 与三 CSV。
2. `hr_20260727T005811Z_ed989409cfef` 的 InputGate passed，但
   UnitGate 暴露 `_read_input_state` 未恢复父 snapshot
   `generation_id/refresh_date` 的平台回读缺陷。该缺陷按 TDD 修复，
   规格与质量评审通过，并单独提交为 `0d97e72`。
3. 修复后从 static 开始重新运行完整七 Gate，最终 run
   `hr_20260727T012116Z_4dd70ba51581` 7/7 passed。

上述失败均发生在算法技术通过声明之前；没有跳过 Gate，也没有修改
上游算法贴合平台结果。

## 下一步

只有在 7Y 方案状态/evidence 与三个方案文件完成独立提交、工作树恢复
clean 后，才允许 Intake 10Y。
