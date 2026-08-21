# Harness 运行期状态外置设计

## 背景

不可变 release 的只读性只由 mode bits（目录 `0555`、文件 `0444`）表达，`lsattr` 无 immutable 位，
根文件系统为 `rw`。所有 one-shot 与 Backend 均以 `root` 运行，`CAP_DAC_OVERRIDE` 直接绕过这些
mode bits。因此 harness 中指向 `project_root` 的写入在生产环境**不会失败，而是成功写入 release
树**，使 `source_tree_sha256` 相对安装记录漂移，破坏可追溯、可复现与回滚依据。

该风险已有现场实证：release 激活后出现过写入 release 树的 `__pycache__`，只能人工移入
`quarantine/` 才恢复 digest 一致。

仓库中已有正确先例，本设计不发明新机制，只把同一模式补齐：

- `shared/blackbox_v2/lifecycle.lifecycle_root()` 已用
  `resolve_runtime_state_path("artifacts/blackbox-v2-lifecycle", ...)`；
- `shared/data_bridge/refresh.DataBridgeRefreshConfig.from_env()` 已用
  `resolve_runtime_state_path("data-bridge/refresh", ..., override_env="DATABRIDGE_RUNTIME_ROOT")`。

## 目标

把 harness 的运行期状态读写从源码树移到既有外置 runtime root，使不可变 release 在受控 harness
运行期间保持 digest 稳定，并使 Blackbox `all` 阶段能在生产 release 上运行。

不改变：Gate 语义、授权模型、判定结果、报告内容与结构、CLI 子命令与参数、数据库 schema、
Registry 生命周期、输入截止、算法逻辑、systemd/launchd 模板、部署矩阵。

## 问题一：DataBridge provenance 读路径（当前唯一的硬失败）

`harness/blackbox_v2/gates.py` 的 `_data_bridge_provenance()` 直接写死：

```
ctx.project_root / "backtest_artifacts" / "data_bridge_refresh" / "state.json"
```

该路径正是 `DataBridgeRefreshConfig` 的 **development_default**——它跳过了 resolver。生产 release
中 `backtest_artifacts/` 因 `.gitignore` 根本不存在，真实状态位于 runtime root 的
`data-bridge/refresh/state.json`。因此该函数抛
`Blackbox all-stage requires readable DataBridge provenance`，Blackbox `all` 阶段无法在生产
release 上完成。

修复：改为经既有 canonical 入口取得该路径（`DataBridgeRefreshConfig.from_env().state_path`），
不新增第二处路径解析，也不改变 provenance 的校验字段与判定。

## 问题二：写入 release 树的运行期状态（静默污染）

以下写点硬编码 `project_root` 且无 override：

| 位置 | 当前路径 | 触发条件 |
|---|---|---|
| `harness/authorization.py` `used_tokens_path()` | `reports/harness/.used_authorization_tokens.json` | 全部授权副作用路径（19 个调用点） |
| `harness/gates/backtest_gate.py` baseline | `reports/refactor_baseline/{scheme_id}/backtest_no_persist.json` | `--no-persist` 且基线缺失时自举写入；新方案必然缺失 |
| `harness/cli.py` `signal-gap-fill` report root | `reports/harness/signal-gap-fill/` | 唯一合规的单日人工补缺入口 |

`gate` / `onboard` / `activate` 的 `report_dir` 默认值同样指向 `project_root/reports/harness/`，
已有 `--report-dir` 可绕开；本设计一并改默认值使不传参时也安全，不移除该参数。

修复：全部改用 `resolve_runtime_state_path()`，`development_default` 保持与当前实现逐字节等价：

```
used_tokens_path        relative "reports/harness/.used_authorization_tokens.json"
backtest baseline       relative "reports/refactor_baseline/{scheme_id}/backtest_no_persist.json"
signal-gap-fill root    relative "reports/harness/signal-gap-fill"
report_dir 默认          relative "reports/harness/{scheme_id}/{timestamp}"
```

`.bfl-release.env` 已声明 `BFL_RUNTIME_ROOT`，systemd unit 与按规程加载该文件的手工命令自动落到
`<runtime_root>/reports/...`，无需新增环境变量或 unit 改动。

### 附带解决：授权重放保护跨 release 失效

`reports/**` 在 `.gitignore` 中（仅保留 `README.md`），每个 release 的 `reports/` 出厂即为空。
授权重放存储位于该目录，意味着**一次性 token 的重放保护只在单个 release 内有效**：切换 release
后同一 token 会被重新视为未使用。现场已确认全部 release 中不存在
`.used_authorization_tokens.json`。移到外置 runtime root 后该保护跨 release 保留。这是本设计的
必要组成部分，不作为可选项。

## 问题三：方案生命周期状态存放于源码树（范围外，需独立决策）

两种 runtime 都会改写 `schemes/{scheme_id}/config.yaml` 的根级 `status`：

- Native：`harness/gates/activate_gate.py` 的 `_set_status()` / `_write_expected_active_config()`；
- Blackbox V2：`shared/blackbox_v2/lifecycle.perform_lifecycle_transition()` →
  `atomic_update_config()`，调用点为 `harness/blackbox_v2/activation.py` 与
  `harness/blackbox_v2/gates.py`。

`config.yaml` 是 release 内容本身、参与 `source_tree_sha256` 与 `scheme_version` 计算。改路径无法
解决：把 status 写到别处等于改变方案身份的定义位置，涉及 `scheme_version`、StaticGate、
ActivationGate 与 Registry 的一致性。

本设计**不处理该问题**，但必须明确：问题一、二修复后，Blackbox 激活仍会写 release 内的
`config.yaml`。因此它不是"以后再说"的遗留项，而是当前方案激活链路上剩余的最后一个不可变性冲突，
需要单独设计后才能在不可变 release 上完成 activation。

## 测试与验收

新增回归，全部可在本机执行：

1. 对每个写点：设置 `BFL_RUNTIME_ROOT` 指向临时目录，断言写入落在该目录下，且 `project_root`
   调用前后**文件集合与内容摘要完全不变**。
2. 只设 `BFL_DEPLOYMENT_TARGET`、不设 `BFL_RUNTIME_ROOT` 时，断言 fail-closed 抛错且无任何文件
   系统副作用。
3. 两者皆不设时，断言路径与改动前逐字节一致（开发行为不变）。
4. 授权重放：在 runtime root 下 `mark_token_used` 后，用**不同的** `project_root` 再次校验同一
   token，断言仍被拒绝。
5. DataBridge provenance：构造 runtime root 下的 `data-bridge/refresh/state.json`，断言
   `_data_bridge_provenance` 读到它；`project_root/backtest_artifacts/` 缺失时不再抛错。
6. 既有 harness 单元与集成测试全绿。

现场验收（ECS 只读诊断，不在本设计内执行）：新 release 安装后重算 `source_tree_sha256`，在完成
一次受控 harness 运行前后均需与 `.bfl-release-install.json` 精确一致。

## 停止条件

- 需要改变 Gate 判定、授权语义、数据库 schema 或 Registry 生命周期；
- 需要引入 fallback、双读或隐式兼容才能让测试通过；
- 发现 `project_root` 仍有本设计未列出的读写点，需要先补全清单再实施。
