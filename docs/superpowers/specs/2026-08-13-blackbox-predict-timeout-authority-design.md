# Blackbox 预测超时单一权威设计

## 范围

本阶段只根治 GitHub #45：Blackbox V2 的预测超时目前同时由方案配置、Runtime
Profile 和 executor 调用参数表达，导致配置声明 3600 秒、正式调度实际执行 600 秒。

本阶段不改变 Native 超时语义、不改变 Blackbox 回测预算、不调整 launchd 触发时刻、
不修改算法、输入、日期或写库逻辑，也不执行生产数据库登记、Gate、激活或服务发布。

## 根因

当前 39 份 Blackbox `config.yaml` 和 Intake 生成器都写入
`schedule.timeout_sec: 3600`，`blackbox-v2-v1` Runtime Profile 的
`predict_timeout_sec` 也为 3600；但 `execute_scheme()` 的调用方默认预算为 600，
`_effective_timeout_sec()` 再对 Blackbox 取两者最小值。随后 executor 用该结果覆盖
Runtime Profile，正式调度因此始终只得到 600 秒。

这不是单纯缺少告警，而是一个资源限制拥有三个权威来源。平台接受并版本化一个它在
正式调度中不会兑现的方案值，方案作者、Gate、运维容量评估和真实子进程因而无法从
同一合同得到同一答案。现有 `blackbox_timeout_truncated` 告警只暴露冲突，没有消除
冲突。

## 设计决策

### 1. Runtime Profile 是 Blackbox 预测预算的唯一权威

`deploy/blackbox_v2/runtime_profile_v1.json` 中的
`predict_timeout_sec` 改为 600。所有 Blackbox predict 路径均从加载后的
`DEFAULT_RUNTIME_PROFILE.predict_timeout_sec` 取得基础预算。

继续只支持当前 `blackbox-v2-v1`，不新增第二个 Runtime Profile，也不保留 3600 秒
兼容分支。原因是正式调度的有效预算本来就是 600 秒；本次将合同修正为既有生产事实，
而不是扩大或缩小正式调度窗口。此前直接调用 Runtime Profile、未经过 executor 的
predict 路径会由 3600 秒收敛为 600 秒，从而与生产调度一致。Blackbox
`backtest_timeout_sec=14400` 保持不变。

### 2. 方案配置不再拥有 Blackbox 超时字段

- 从全部 Blackbox `config.yaml` 删除 `schedule.timeout_sec`。
- Blackbox Intake 不再生成该字段。
- Blackbox 配置校验只要发现该字段就 fail-closed，不接受旧字段、不忽略旧字段，也不
  做默认值兼容。
- Blackbox canonical config hash 不再包含 timeout；`schedule` 只保留 `cron` 和
  `timezone`。
- `SchemeSchedule.timeout_sec` 继续为 Native 所用；Blackbox discovery 得到的值固定为
  `None`，executor 不读取它。

这会使仓库内 39 个 Blackbox 配置产生新的 canonical config hash 和精确
`scheme_version`。不伪造旧哈希，也不让新合同继续映射到旧版本。

### 3. 调用方只可收紧一次执行的 deadline

executor、Harness 或 gap 工具传入的 `timeout_sec` 改为“本次操作剩余 deadline”，
不是第二个基础预算。Blackbox 实际预测等待时间为：

```text
min(Runtime Profile predict_timeout_sec, caller operation deadline)
```

调用方未提供 deadline 时直接使用 Runtime Profile；提供 300 秒时可收紧为 300 秒，
提供 1800 秒时仍为 600 秒，不能放宽 Runtime Profile。非正数在进程启动前
fail-closed。

executor 不再用 `replace(..., predict_timeout_sec=...)` 改写 Profile，而是把可选的
operation deadline 独立传给已经具备 `min(profile, deadline)` 语义的 Blackbox CLI
执行层。这样审计时可以明确区分“发布资源合同”和“某次操作剩余时间”。

Native 继续按既有优先级使用其方案级 `schedule.timeout_sec`，否则使用 executor 默认
600 秒；本次不改变 Native 行为。Blackbox gray replay/backtest 继续使用独立的
backtest budget，本次不把预测预算外推到回测。

### 4. 删除过渡告警

删除 `blackbox_timeout_truncated` 及只验证截断告警的测试。新合同下不存在合法的
Blackbox 方案级 timeout，因此没有需要截断的配置值；若重新出现该字段，应在
StaticGate/discovery 阶段直接失败，而不是运行时告警后继续。

## 数据流

```text
blackbox-v2-v1 Runtime Profile (predict_timeout_sec=600)
  -> discovery 验证 config.schedule 不含 timeout_sec
  -> executor 取得 Profile 基础预算
  -> 可选 caller deadline 只做 min() 收紧
  -> Blackbox CLI 子进程使用最终预算
```

Intake 只生成调度时刻和时区；方案交付者不能声明、扩大或覆盖平台资源预算。

## 版本与生产边界

移除 39 份配置中的 timeout 会改变精确方案版本。仓库开发分支可以原子提交代码、配置、
文档与测试，但该提交不是生产激活授权，也不能直接写生产数据库。

后续若发布到生产，必须在发布前按现有 Blackbox revision 流程为每个新精确版本取得
相应 Gate 证据并受控激活，使 `t_scheme_versions`、Registry 与仓库配置精确一致。未完成
该步骤时不得部署本变更；不得增加“接受旧 timeout”“沿用旧版本 hash”或“找不到新版
就运行旧版”的 fallback 来绕过版本闭包。

## 错误处理

- Blackbox 配置含 `schedule.timeout_sec`：配置校验失败，方案不进入 discovery/执行。
- Runtime Profile 的预测预算缺失、非整数或非正数：Profile 加载失败，fail-closed。
- caller deadline 非正数：子进程启动前失败。
- caller deadline 大于 600：仍执行 600 秒，不告警，因为收紧规则本身就是公开合同。
- caller deadline 小于 600：按较小 deadline 执行，并由现有运行审计记录失败结果。

## 测试与验收

1. 先用失败测试锁定新合同：
   - Blackbox 配置出现 `schedule.timeout_sec` 必须被拒绝；
   - Intake 产物不得包含该字段；
   - canonical Blackbox config 不包含 timeout；
   - 仓库全部 Blackbox 配置均无该字段；
   - Runtime Profile 的预测预算为 600、回测预算仍为 14400。
2. executor/runner 定向测试证明：
   - 未提供 operation deadline 时使用 Profile 的 600；
   - 300 可收紧，1800 不可放宽；
   - executor 不再改写 Profile；
   - Native 的方案级 3600 仍然生效；
   - 旧截断告警路径已删除。
3. Intake、discovery、StaticGate、Blackbox runner、launchd runner、signal-gap 相关测试
   全部通过。
4. `compileall`、`git diff --check` 和完整 `python -m pytest -q` 全部通过。
5. 只在目标开发分支完成普通 merge/push 与远程 SHA 回读；不触及 `master`，不执行任何
   生产 DB、launchd 或服务操作。

## 完成定义

- 仓库中 Blackbox 预测预算只有 Runtime Profile 一个基础权威；
- 配置、Intake、版本计算、executor 和文档不再表达第二个 Blackbox 基础预算；
- 全部预测调用路径对 600 秒合同一致，调用方只能收紧；
- Native 与 Blackbox backtest 行为未发生范围外变化；
- 新精确版本的生产迁移边界被明确记录，没有兼容 fallback。
