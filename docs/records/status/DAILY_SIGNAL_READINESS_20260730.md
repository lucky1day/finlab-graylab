# 2026-07-30 日频生产准备事实记录

**文档状态**：`HISTORICAL`

**冻结时间**：2026-07-30 08:20 CST

**用途**：记录 7 月 30 日 production cache、DataBridge、ledger 日批和历史补缺
authority 的实际结果。本文只保存当时时点证据，不定义未来操作规则。

## 1. Git 与服务边界

- 开发分支、远程开发分支与 `master` 在生产动作前均锁定为
  `a348d1994d70691a7426cd77986d5f84f0643da8`。
- production schema 仍停在 migration 017，rollout 仍为 `legacy`。
- backend 持续运行，`http://127.0.0.1:8100/` 与 `/api/health` 均为 HTTP 200。
- scheduler 与 v2-preflight 全程未加载。
- 未应用 migration、未发布 epoch、未修改 plist、未重启服务、未写预测表。

## 2. 7 月 30 日正式日批结果

- 06:30 没有创建真实 `daily-signals` occurrence。
- 07:55 内部目标和 08:00 硬验收均未满足。
- 结果为 0/29 `scheduled_live`，没有 item、target receipt 或 scheduled
  provenance。
- 不允许事后补造 occurrence，也不允许把手工补缺倒签为 `scheduled_live`。

## 3. Liwei production cache

唯一 operator 从 05:07 至 07:41 串行完成 7 个 family 的一次性 full bootstrap。
所有 current 均满足：

- manifest schema 3、input-state schema 3；
- `generation_acceptance=ACCEPTED`；
- exact policy spec、publisher、family 与 tenor；
- Native generation `native-8ad794ac7bfc9505f9a498f1`，
  business=2026-07-28、feature=2026-07-27；
- projection proof、payload/file hash、安全 root、parent lineage 与单调 coverage；
- root owner `macstudio0`、mode 0700、无 symlink、无 `.building-*`。

锁定 generation：

| family | generation |
|---|---|
| `liwei_0616_10y_v61` | `generation-677452165db17bbabf1178de-b0ec4e82` |
| `liwei_0616_5y_allk10_auc_static_v1` | `generation-0394490d3b7f920c59c1b821-05348e73` |
| `liwei_0616_5y_allk10_auc_yearly_v1` | `generation-d6505135454dcc5aeae61758-f9f4e23c` |
| `liwei_0616_5y_allk10_ic_yearly_v1` | `generation-fdb7c3762b5de1927f601735-be4825d9` |
| `liwei_0616_5y_v31` | `generation-7f06b100a73267e7875f55d0-7007a156` |
| `liwei_0616_7y01_v31` | `generation-c5343e9b344f2c96d41ee70c-3da20cc6` |
| `liwei_0616_7y03_v31` | `generation-3a12f3b68e5c20e3ea3c31e4-7fd9c88e` |

同 authority 第二次 operator 运行退出码 0，得到 7 个 `build_mode=hit`、27 个
reuse marker、0 个 training marker；七个 current generation、manifest SHA 和
mtime 均未变化。旧 `capacity_eligible=false` 和 `qualification_status=unqualified`
只保留为 compare-gate 审计字段，不再是 direct ledger 前置。

## 4. DataBridge 7 月 30 日 current

07:58 启动唯一 legacy operator，08:18 成功晚到发布：

- generation：`full-20260730-081804-9794ce962c1a`；
- duration：1197.458 秒；
- stable rounds：2；
- business digest：
  `9794ce962c1aa27eddb9ba43900b961a2ccf33d596d08f02cd137edbeb2c7de2`；
- daily：3886 行、774 列、max key 2026-07-29；
- weekly：848 行、575 列、max key 202629；
- monthly：201 行、123 列、max key 202701。

发布后独立 `--check-only --date 2026-07-30` 退出码 0，state、manifest、三文件
摘要与 current 完全一致。该发布晚于 06:55，只能作为后续补缺和下一交易日输入，
不能改变 7 月 30 日无 occurrence、0/29 的事实。

## 5. 历史缺口与剩余阻断

历史缺口仍为 50：T+1 为 5、T+5 为 45。DataBridge 发布后只读 plan 得到：

- 20 个 Blackbox V2 target 为 `GRAY_LIVE_GAP`；
- 30 个 Native target 为 `BLOCKED_NO_GENERATION`；
- 阻断态 plan SHA：
  `68f26dec60172d85a1fdccc453e6eef64e822edecd18dc4c2414f4d6c6d1b8c2`。

该 plan 只作审计，禁止执行。当前 Native generator/registry 只允许真实 occurrence
绑定的当天 generation，没有合法的 standalone gray-gap current-snapshot
prepare/register 路径；手工调用 repository 会绕过 provenance 和授权边界。

完整 production direct authority 还有独立 schema 阻断：

```text
t_scheme_runs.started_at
current  = datetime NOT NULL DEFAULT CURRENT_TIMESTAMP
expected = datetime(6) NULL DEFAULT CURRENT_TIMESTAMP(6)
```

因此后续顺序必须是：先完成并验证最小 Native gray-gap artifact 入口，再冻结
H1/H2 plan；migration 018、epoch 和 ledger cutover 仍须在独立维护窗口完成。

## 6. 冻结后的代码进展

冻结记录之后，Native gray-gap current-snapshot artifact prepare/register 已由
PR #16 合并到开发分支 `ff70cd78d3aed76b86be263e244543e5d6819ba1`。该入口固定
本仓库 replay store、使用专项 HMAC 绑定 canonical manifest 与私有 storage root，
只登记 `t_input_generations`，并从正式 ledger generation recovery 中隔离。

这只是代码能力进展，不改写 08:20 的现场事实：两份 production artifact 尚未生成
或登记，50 条缺口仍未写入，旧 plan SHA 仍禁止执行，migration 018 和 ledger
cutover 仍未完成。
