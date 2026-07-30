# cgb_causal_wk_1y 周度方案技术入库记录

**文档状态**：`CURRENT`

**目标读者**：平台入库、生产授权和审计人员

**最后核验日期**：2026-07-30

**机器证据**：[CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.evidence.json](CGB_CAUSAL_WK_1Y_ONBOARDING_20260730.evidence.json)

## 结论

```text
TECHNICAL_GATES: PASS 7/7
CHECK_ONLY: TRUE
ALGORITHM_LOGIC_REVIEWED: FALSE
CONFIG: paused + draft
BUSINESS_TABLE_WRITES: 0
PRODUCTION_ACTIVATED: FALSE
```

`cgb_causal_wk_1y@05022a0eeec7` 已完成两文件 Intake、权威业务周历组合输入
和七个自动 Gate。平台只验证 Contract、确定性、截止隔离、标准输出和
no-persist 回测，没有评审算法内部逻辑或效果。

## 身份与输入

| 项目 | 结果 |
|---|---|
| Registry ID | `cgb_causal_wk_1y__h1__1Y` |
| 任务 | `1Y / weekly_point / horizon=1` |
| Python SHA256 | `90f3abcc1501eb7173c706fc2ad5d76fda89bf9004c88ee976b98378bbb6d450` |
| Metadata SHA256 | `efc8e03c5db98c33f0b830d62cc4465f7f890b5a783de68fee58e4ae19d4162b` |
| generation | `full-20260730-081804-9794ce962c1a` |
| parent / combined snapshot | `snapshot-a0dbf1774782db2e6d2a1ec5` / `snapshot-2c964086367c6a987f193bcd` |
| 平台周历 | `api-wind-date-v1`，6064 行，SHA256 `82b32635a1b94d414fdbdcb6391210729aadd7dbb86b9c44bc692ab77bd6da91` |
| Request | `predict=2026-07-25 / feature=2026-07-24 / target=2026-07-31` |

CompareGate 首轮发现平台把回退日截止与错误的上一周截止组合成无效 Request。
平台已改为从权威 `api_wind_date.csv` 重新映射 prior daily cutoff 的业务周，
回归提交为 `5e67b09`；未修改交付脚本。

最终 Harness run `hr_20260730T090642Z_66a2646bd276` 为 7/7 PASS；
Backtest 为 100 requests / 100 records、分批与逆序一致、`persist=false`。
`check_only=true`、`control_plane_persisted=false`、
`business_tables_written=false`。

## 生产边界

- 历史回测：从 `2025-01-01` 起，且 `target_date < 2026-06-01`。
- 灰度补齐：`2026-06-01 <= target_date <= 2026-07-31`，全部为
  insert-only `gray_live`。
- 首条自然调度：`2026-08-01 / 2026-07-31 / 2026-08-07`，只有 scheduler
  真实时钟成功运行后才标记 `scheduled_live`。

当前记录不是 `SHADOW_READY` 或已上线证明。生产切换仍须先执行可持久化的
all-stage、专项 shadow/activate/backtest/gray token，并在验证分支得到用户明确
确认后合并到 `master`。
