# 日频 Ledger 28/32 扩容设计

**状态**：待用户复核

**日期**：2026-08-02

## 背景

当前生产仍由 launchd + plist 驱动 legacy 日频路径，
`t_schedule_occurrences`、`t_schedule_items`、`t_schedule_item_targets` 和
`t_scheduler_heartbeat` 均无真实生产记录。已经批准但尚未切换的 ledger
控制面仍冻结为 25 个 base execution、29 个业务 target，其中 17 个 Native V1、
8 个 Blackbox V2。

当前 active daily 实际集合已经增加以下三个 Blackbox V2 T+1 方案：

- `one_y_t1_quote_state_hv_v1@d6d0cb43aacd`，target `1Y`；
- `three_y_adyn_lb1_k3_v1@98233f0cb9ef`，target `3Y`；
- `three_y_adyn_lb2_k1_v1@47c7c1776db0`，target `3Y`。

因此真实集合为 28 个 execution、32 个 target，其中 17 个 Native V1、11 个
Blackbox V2。legacy `daily_gray_launchd_policy_v1.json` 已冻结为 28/32，
但 `daily_scheduler_policy_v2.json`、静态常量、容量证据、真实回放和文档仍停留在
25/29。此状态下 ledger scheduler 会 fail-closed，不能安全切换生产。

## 本批目标

把尚未投入生产、尚未产生 occurrence 的 ledger V2 policy 扩展到当前真实 active
daily 集合，并保证所有机器门禁对同一个 28/32 身份达成一致。

具体完成标准：

1. ledger policy 精确包含 28 个 active daily base identity 和 32 个 composite
   target；
2. Native/V2 数量固定为 17/11，运行时不得根据 discovery 动态放宽；
3. 三个新 V2 使用现有 `databridge_v1` 输入、`blackbox_v2` 资源类、单内部 worker
   和 120 秒 hard runtime；
4. 保留既有八个 V2 的 `0..14` 分钟 release offset，新方案只追加
   `16/18/20`，不重排已经评审的身份；
5. Native 最大并发 2、V2 最大并发 2、自动重试上限 1、08:00 SLA 和 08:30
   recovery cutoff 均保持不变；
6. scheduler startup、隔离 real replay、capacity attestation、daily health 和文档
   对 28/32 使用同一组常量和精确身份；
7. 旧 V1 policy 的字节和摘要保持不变。

## 设计决策

### Policy 身份

继续扩展 `daily-scheduler-policy-v2`，不创建 V3。原因是 V2 从未在 production
产生 occurrence，生产 rollout 仍为 `legacy`，机器全局 ledger epoch 也尚未发布；
因此不存在需要兼容的 V2 生产账本。扩展后新的 policy SHA 将作为未来首次 ledger
epoch 和 occurrence 的唯一权威。

Policy 仍必须与 `blackbox_scheduler_admission_v1.json` 的 `daily_ledger` capability
以及 strict active-daily discovery 精确相等。不能通过忽略额外 active scheme、
降低计数检查或运行时自动吸收新方案来通过校验。

### V2 release 顺序

现有八个 V2 offset 保持 `0/2/4/6/8/10/12/14`。新增身份按稳定 scheme 顺序追加：

| scheme | offset | deadline |
|---|---:|---|
| `one_y_t1_quote_state_hv_v1` | 16 | `07:25` |
| `three_y_adyn_lb1_k3_v1` | 18 | `07:25` |
| `three_y_adyn_lb2_k1_v1` | 20 | `07:25` |

三者共享 `databridge_v1` 输入但没有平台级可写 cache prerequisite。追加 offset
不会提高 V2 并发上限，也不会改变其它算法的 release 身份。

### 单一计数来源

`scheduler.daily_policy` 继续负责 policy 的规范常量。real replay、capacity
attestation、health/probe 和测试应引用这些规范常量或从已验证 policy 投影，避免
再新增裸 `25/29`。确需保留 V1 21/25 的代码必须用带 V1 名称的常量隔离。

### Fail-closed 行为

以下任一情况必须在创建 Engine、occurrence 或算法进程之前失败：

- policy 不是 28/32；
- strict discovery、admission 和 policy 身份集合不相等；
- 三个新增 scheme 的版本、runtime、task type、horizon 或 target 漂移；
- V2 offset 不是精确的 `0..20` 偶数序列；
- Native/V2 数量不是 17/11；
- 生产仍绑定旧 policy SHA 或 installed 服务 mode 不一致。

## 测试策略

实现采用测试驱动方式：

1. 先把 policy 主合同测试改为期望 28/32、17/11 和 11 个精确 V2 offset，确认在
   旧 policy 上按预期失败；
2. 增加三个新增身份的版本、target、资源类、输入兼容和 deadline 断言；
3. 更新 real replay 与 capacity attestation 的 V2 形状测试，验证旧 25/29 候选被
   拒绝、新 28/32 候选被接受；
4. 运行 daily policy、admission、runtime、scheduler main、health、capacity、
   onboarding docs 的针对性测试；
5. 最后运行完整 pytest，并确认工作区只包含本批文件。

## 明确不在本批范围

- 不修改 MySQL/DataBridge HTTP、natapp、导出客户端或刷新实现；
- 不重建或发布 DataBridge current；
- 不修改 installed plist，不执行 bootout/bootstrap/kickstart；
- 不把 `BOND_DAILY_COORDINATOR_MODE` 切为 `ledger`；
- 不应用 migration，不发布 machine-global epoch；
- 不生成真实 occurrence，不写预测或历史补数；
- 不修改任何 Native 或 Blackbox 算法逻辑；
- 不在本批实现激活日 trailing-signal 连续性门禁，该问题在 28/32 policy 收口后
  作为独立阶段处理。

## 后续生产边界

本批提交只证明仓库中的 ledger 候选与当前 28/32 active daily 集合一致，不证明
生产已经切换。生产切换仍需独立授权窗口，完成 migration 018、epoch、installed
plist 精确同步、legacy owner 退出、ledger scheduler 单实例启动和首次自然 occurrence
验收。
