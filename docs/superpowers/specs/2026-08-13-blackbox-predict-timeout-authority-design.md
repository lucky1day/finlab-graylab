# Blackbox 预测超时预算分层设计

## 范围

本阶段只根治 GitHub #45：Blackbox V2 的方案配置声明 3600 秒、Runtime
Profile 也声明 3600 秒，但 `execute_scheme()` 默认把 600 秒当成平台上限，导致正式调度
实际最多只等待 600 秒。

本阶段不改变 39 个 Blackbox 方案的配置或精确版本，不改变 Native 超时语义、不改变
Blackbox 回测预算、不调整 launchd、不修改算法、输入、日期或写库逻辑，也不执行生产
数据库登记、Gate、激活、迁移或服务操作。

## 根因

当前实现没有区分三种含义：

- `config.yaml.schedule.timeout_sec`：方案对单次 predict 的预算申请；
- Runtime Profile `predict_timeout_sec`：平台允许的 predict 最大预算；
- executor 调用参数 `timeout_sec`：某次上层操作的剩余 deadline。

`execute_scheme()` 把调用参数默认成 600，再由 `_effective_timeout_sec()` 对 Blackbox 取
`min(config, 600)`。因此，即使调用方没有要求收紧 deadline，正常 scheduled launchd
仍被无条件截断为 600 秒。根因不是预算值本身错误，而是把“缺省的操作 deadline”错误
建模为“平台上限”。

## 设计决策

### 1. 保留既有方案配置与版本身份

39 份现有 Blackbox `config.yaml` 继续保留：

```yaml
schedule:
  timeout_sec: 3600
```

Intake 继续为新交付生成 3600 秒，Blackbox 配置校验继续要求该值为正整数，canonical
config hash 继续包含该字段，discovery 继续把它加载到 `SchemeSchedule.timeout_sec`。

为避免静默 fallback，Blackbox 正式配置缺少 `schedule.timeout_sec` 时应 fail-closed；当前
39 份配置已经满足该约束，因此不会产生任何新 config hash、`scheme_version`、Gate 或
activation 工作。

### 2. Runtime Profile 保持平台最大预算

`blackbox-v2-v1` 的 `predict_timeout_sec` 保持 3600 秒；
`backtest_timeout_sec=14400` 保持不变。Runtime Profile 是平台资源上限，方案配置不能
放宽它。

### 3. operation deadline 只在调用方显式提供时生效

`execute_scheme(..., timeout_sec=...)` 的参数改为可选值：

- `None`：调用方没有额外 deadline；
- 正整数：本次操作的剩余 deadline，只能收紧基础预算；
- 非正数：在启动子进程前 fail-closed。

最终 predict 等待预算为：

```text
min(
  scheme schedule.timeout_sec,
  Runtime Profile predict_timeout_sec,
  explicitly supplied operation deadline (if any),
)
```

这三个值不再是相互竞争的默认值，而是依次表达“方案申请”“平台上限”“本次调用剩余
时间”。

### 4. executor 不再改写 Runtime Profile

executor 先用方案预算和可选 operation deadline 计算本次请求上限，再把这个上限独立
传给 Blackbox runner。runner 使用既有 `min(profile, timeout)` 语义施加最终平台上限。
executor 不再用 `replace(..., predict_timeout_sec=...)` 把某次调用值伪装成 Runtime
Profile 配置。

正常 scheduled launchd 不传 operation deadline，因此当前配置得到：

```text
min(3600, 3600) = 3600
```

Harness、gap 或其它受控调用若显式传 600，则得到：

```text
min(3600, 3600, 600) = 600
```

显式传 7200 也不能越过方案或平台上限，最终仍为 3600。

### 5. 删除误导性的截断告警

删除 `blackbox_timeout_truncated`。旧告警把正常的层级收紧描述成配置冲突；新合同下，
显式 operation deadline 小于方案预算是合法行为，不应告警。非法或缺失的配置在校验
阶段直接失败。

### 6. Native 行为保持不变

Native 仍优先使用自己的 `schedule.timeout_sec`；未配置时仍使用既有 600 秒默认值。
本阶段不把 Blackbox 的三层预算合同外推到 Native，也不改变 Native 调用方参数的既有
优先级。

## 数据流

```text
Blackbox config request (3600)
  -> executor applies explicit operation deadline only when present
  -> Blackbox runner caps request by Runtime Profile ceiling (3600)
  -> subprocess timeout
```

方案配置、Runtime Profile 和 operation deadline 各自只有一种含义；没有旧值兼容、
版本回退或第二套 Profile。

## 生产与副作用边界

本设计不修改方案配置、canonical hash 或精确版本，因此不需要 39 个新 exact versions，
不需要重新运行 Gate、activation 或 Registry 切换，也不需要任何生产数据库写入。

代码合并到当前开发分支后，使用该工作目录的下一次自然 scheduled launchd 会读取新的
executor 语义：通常在 10 分钟内完成的方案没有行为差异；运行 10–60 分钟的方案不再被
错误地在 600 秒终止；真正卡死的方案最多可占用已声明的 3600 秒。此次不 kickstart、
不 reload、不手工触发预测。

## 错误处理

- Blackbox `schedule.timeout_sec` 缺失、非整数或非正数：配置校验失败；
- Runtime Profile 预测预算缺失、非整数或非正数：Profile 加载失败；
- operation deadline 非正数：子进程启动前失败；
- operation deadline 大于方案预算或 Profile 上限：不能放宽，按较小上限执行；
- subprocess 到达最终预算：沿用现有进程组终止与运行失败审计。

## 测试与验收

1. 配置与版本不变：
   - 39 份 Blackbox 配置继续包含正整数 `timeout_sec: 3600`；
   - Intake 继续生成该字段；
   - canonical hash 继续包含该字段；
   - 相对目标开发分支不产生任何 Blackbox config 或 version hash 变化。
2. executor 层级测试：
   - Blackbox 未提供 operation deadline 时请求 3600；
   - 显式 600 时请求 600；
   - 显式 7200 时请求不超过方案的 3600；
   - executor 不改写 Runtime Profile；
   - Native 既有超时优先级不变。
3. runner 上限测试：
   - 3600 方案请求在 3600 Profile 下得到 3600；
   - 600 operation deadline 得到 600；
   - 7200 请求不能放宽 3600 Profile；
   - backtest 14400 不变。
4. 运行相关定向测试、`compileall`、`git diff --check` 和完整
   `python -m pytest -q`。
5. 合并前再次确认共享工作区分支、upstream、tracked 状态和未跟踪路径不与改动冲突；
   只普通推送 `codex/audit-bugfixes-20260613`，不触及 `master`。

## 完成定义

- scheduled Blackbox 不再被隐式 600 秒默认值截断；
- 三层预算各自含义明确，最终值严格按最小值计算；
- 39 个方案配置、精确版本和生产生命周期状态完全不变；
- 无兼容 fallback、无新 Runtime Profile、无生产 DB/launchd/服务操作；
- Native 与 Blackbox backtest 行为没有范围外变化；
- 定向与全量测试通过，开发分支普通推送后远程 SHA 回读一致。
