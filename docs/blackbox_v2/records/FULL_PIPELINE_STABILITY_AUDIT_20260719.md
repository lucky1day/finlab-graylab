# Blackbox V2 全链路稳定性认证报告

**记录类型**：隔离灰度实测审计

**适用运行时**：`blackbox_v2`

**测试日期**：2026-07-19（Asia/Shanghai）

**测试方案**：`weekly_10y_lgbm_point_v1`

**证据文件**：`FULL_PIPELINE_STABILITY_AUDIT_20260719.evidence.json`

## 1. 明确结论

```text
SHADOW_READY: PASS
PRODUCTION_READY: FAIL
OVERALL: PRODUCTION_BLOCKED
```

Blackbox V2 的两文件收包、统一 DataBridge 输入、七 Gate、单点预测、100 条 no-persist 回测和 `shadow + paused` 登记已经达到可重复使用的稳定程度。后续真实交付可以继续按 SOP 进入技术入库和 shadow 验收。

Blackbox V2 尚未达到生产稳定。当前实现不能完成回测持久化，正式 ActivationGate 与 Blackbox 版本身份不兼容，sandbox 仍能读取任意本机文件和继承环境变量，JSON Result 仍接受字符串方向。因此，任何 V2 方案当前最多进入 `shadow + paused`，不得直接 `active/live`。

本轮在独立测试库中强制激活后，`gray_live -> scheduled_live -> actual -> API -> 前端` 技术链路可以工作。这只证明下游组件兼容，不代表正式生产晋级门禁已经通过。

## 2. 测试边界与隔离

- 测试 worktree：`codex/v2-full-pipeline-audit-20260719`。
- 测试数据库：`bond_factor_lab_v2_e2e_20260719_1730`。
- 数据库迁移：`001-016` 全部执行。
- 源数据表：通过只读 View 指向 `bond_db`。
- Registry、Harness、预测、回测和 actual 业务表：全部位于独立测试 Schema。
- 测试后端：临时运行于 `127.0.0.1:18100`，测试结束后已停止。
- 生产方案始终保持 `shadow + paused`，生产方案的 run、prediction、backtest 均为 0。

历史回放 generation 是从 2026-07-19 当前全量文件按截止键截断得到的测试夹具，只用于验证平台流程和日期语义，不代表平台拥有历史修订版本回放能力。

## 3. 全链路认证矩阵

| 环节 | 状态 | 结论 |
|---|---|---|
| 上游两文件交付 SOP | `CONDITIONAL_PASS` | 真实交付可执行，文档与机器合同一致；仅覆盖一个真实方案 |
| Intake 与身份 | `PASS` | 两文件、Metadata、命名和摘要校验通过 |
| DataBridge 三频输入 | `PASS` | 当前 generation 完整，快照同代，截止隔离通过 |
| 七 Gate 技术入库 | `PASS` | 同一真实交付连续 10/10 完整通过 |
| 单点预测 | `PASS` | 重复执行一致，标准 `PredictionRecord` 可生成 |
| no-persist 回测 | `PASS` | 100/101/500/1000 条分批、顺序和结果一致 |
| 回测持久化 | `FAIL` | `--persist` 返回成功但 `t_backtest_*` 仍为 0 |
| Shadow 登记 | `PASS` | Registry=`paused`、version=`shadow` |
| 正式 ActivationGate | `FAIL` | Blackbox 版本与公共 ActivationGate 计算版本不一致 |
| gray_live | `CONDITIONAL_PASS` | 隔离库强制 active 后写入 1 run + 1 prediction |
| scheduler run-once | `CONDITIONAL_PASS` | 隔离库强制 active 后写入 1 run + 1 prediction |
| actual join 与指标 | `CONDITIONAL_PASS` | 两条 live 行完成 weekly actual 关联，准确率 50% |
| API 与前端 | `CONDITIONAL_PASS` | 强制 active 时显示正确；paused 时立即隐藏 |
| 失败零预测副作用 | `CONDITIONAL_PASS` | timeout、缺 CSV、非法方向、stale generation 均无 prediction；CLI 退出语义仍有缺口 |
| 安全隔离 | `FAIL` | 网络和 data-dir 写入被拒绝；任意文件读取和环境变量读取未隔离 |
| 五类任务合同 | `PASS` | 五组 Metadata/日期 Request 夹具通过；不等同于真实算法覆盖 |

## 4. 核心证据

### 4.1 收包、环境与数据

- 交付脚本 SHA256：`6e3ee104e9db652d0d3a291a13695c616ec605547693307bdfe61aa3fee45c72`。
- Metadata SHA256：`e60e9237f2f02d973033030e52fa8744fc62e841d5f2073ea6f273ee431901be`。
- Runtime Profile：`blackbox-v2-v1`。
- Conda 环境：`forecast_env_blackbox_v1`。
- 环境指纹：`720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd`。
- DataBridge generation：`full-20260719-055002-7876ee1e5ec9`。
- DataBridge business digest：`7876ee1e5ec975034396e3ff33c96ce294bd2f516e8497b9e54afefd5cf5867b`。
- 同代快照：`snapshot-245c54a6363ed5251475e8f5`。
- 计划基线的 174 项 V2、DataBridge、API、前端和 SOP 相关自动测试通过；提交前扩展复验 177 项通过、0 项失败。

### 4.2 十轮 Harness 稳定性

- 10/10 轮通过，每轮 7/7 Gate 通过。
- 预测方向十轮均为 `1`。
- 单轮耗时 25-26 秒，P50=26 秒，P95=26 秒。
- 代表性完整运行的最大驻留内存约 552 MiB（包含命令包装进程）。
- 十轮结束时预测、实盘 run 和回测业务表均无写入。
- 未发现残留临时快照。

### 4.3 批量回测

| Request 数 | 平台批次 | 耗时 | 结果 |
|---:|---:|---:|---|
| 100 | 1 | 3.718 秒 | 100/100 |
| 101 | 2 | 3.744 秒 | 101/101 |
| 500 | 5 | 13.045 秒 | 500/500 |
| 1000 | 10 | 26.902 秒 | 1000/1000 |

四种规模均保持 Request 顺序和前缀结果一致。真实 `harness gate backtest --persist` 虽然退出码为 0、Gate 标记 passed、生成 100 条内存结果，但证据仍为 `persist=false`，数据库 `t_backtest_runs` 和 `t_backtest_predictions` 均为 0。这是完整生产链路的确定性阻塞，不是未测试项。

### 4.4 激活、实盘与调度

真实公共 ActivationGate 被阻断：

- Blackbox StaticGate 版本：`2110193568a9`。
- 公共 ActivationGate 计算版本：`45638f28d0ea`。
- ActivationGate 无法找到匹配的 all-stage 历史，因此正式激活失败。

为测量后续组件，本轮只在测试 Schema 和测试 worktree 中标记 `FORCED_ACTIVE_TEST_ONLY`，生成测试版本 `3be8c7720410`：

| 阶段 | predict_date | feature_date | target_date | 方向 | 结果 |
|---|---|---|---|---:|---|
| `gray_live` | 2026-07-04 | 2026-07-03 | 2026-07-10 | -1 | 1 run、1 prediction、1 log |
| `scheduled_live` | 2026-07-11 | 2026-07-10 | 2026-07-17 | -1 | 1 run、1 prediction、1 log |

两条结果均包含 `runtime_type`、scheme version、Request、`data_snapshot_id` 和 `prediction_phase`。这说明 Blackbox runner 与统一写库模型兼容，但正式路径仍因 ActivationGate 失败而不可认证。

### 4.5 Actual、API 与前端

weekly actual updater 在隔离库写入实际值后：

- 2026-07-10：预测 `-1`，实际 `-1`，正确。
- 2026-07-17：预测 `-1`，实际 `0`，错误。
- API 汇总：2 个样本、1 个正确、准确率 50%。
- `/api/schemes` 在 active 测试状态返回 composite ID `weekly_10y_lgbm_point_v1__h1__10Y`。
- `/api/metrics/{registry_id}` 返回两条 live 行，字段与数据库一致。
- `/api/backtests/factor-lab` 不包含该方案，符合“回测未持久化”的实际状态。
- 前端将方案放入 `10Y国债活跃 + 周度` 格子，显示 2 条明细，无重复行，浏览器控制台无 error。
- 暂停后 `/api/schemes` 隐藏该方案，metrics 返回 404，前端不再显示。

前端截图：![隔离灰度前端验证](assets/FULL_PIPELINE_STABILITY_AUDIT_20260719.png)

### 4.6 故障注入

| 场景 | 结果 | Output / 业务副作用 |
|---|---|---|
| 缺少周/月 CSV | 正确拒绝 | 无 Output |
| 算法超时 1 秒 | 正确终止 | 无 Output |
| 网络访问 | sandbox 拒绝 | 无 Output |
| 写入 `--data-dir` | sandbox 拒绝 | 无 Output |
| 非法方向 `2` | Result 校验拒绝 | 无平台 prediction |
| stale generation | 记录 failed run | 0 prediction、0 部分结果 |
| 读取 `/etc/hosts` | **成功读取** | 安全失败 |
| 读取继承环境变量 | **成功读取** | 安全失败 |
| JSON 字符串方向 `"1"` | **被接受为整数 1** | 合同偏差 |

stale generation 运行还有一个运维缺口：数据库正确记录 failed run，但 `scheduler --run-once` 进程退出码仍为 0，外层调度可能误判任务成功。

暂停验证中，scheduler 不再新增 run 或 prediction，但会追加一条 `status=paused` 的 skipped 审计日志；这是可接受的控制面留痕。

## 5. 生产阻塞项

| 严重度 | 编号 | 阻塞项 | 必须完成的整改 |
|---|---|---|---|
| P0 | BBV2-01 | 回测 `--persist` 被静默忽略 | 实现 Blackbox 回测事务落库，并验证 API/前端回测显示和失败回滚 |
| P0 | BBV2-02 | ActivationGate 版本身份不兼容 | 统一 Blackbox Static、all-stage、shadow、activate 的版本计算和历史匹配 |
| P1 | BBV2-03 | 缺 Blackbox 专用 activate/live hard-stop | 将生产批准状态同时约束 ActivationGate、scheduler 和人工 trigger |
| P1 | BBV2-04 | sandbox 任意文件读取与环境变量继承 | 使用读取 allowlist，清理凭据和非必要环境变量 |
| P1 | BBV2-05 | JSON Result 接受字符串方向 | JSON 仅接受整数 `-1/0/1`；CSV 保持文本枚举解析 |
| P1 | BBV2-06 | failed scheduler run 仍退出 0 | `run-once` 在 failed 时返回非零，skipped 语义单独定义 |
| P1 | BBV2-07 | Shadow 多步状态和恢复未自动化 | 事务化写入或提供自动 reconciliation 与幂等重试 |
| P1 | BBV2-08 | 真实方案覆盖不足 | 在现有周频方案外至少增加日频和月频交付，使真实交付总数不少于 3 个并重复认证 |

上述问题对应并扩展 `PRODUCTION_READINESS.md` 的 PR-01 至 PR-08。当前没有任何依据可将该文档从 `BLOCKED_DRAFT` 改为生产 SOP。

## 6. SOP 稳定性结论

### 上游交付 SOP

结论为 `CONDITIONAL_PASS`。一个真实算法工程师交付包可以仅依赖 SOP 完成两文件改造，Metadata、三频快照、七字段 Request、五字段 Result、predict/backtest 和自验要求均与机器合同一致。

仍不能宣称“对所有上游算法稳定”，因为实测只覆盖 `10Y + weekly_point + LightGBM`。日频、周平均、月频和不同依赖栈目前只有机器夹具，没有第二个真实交付方证据。

### 平台入库 SOP

Intake 至 `shadow + paused` 的操作手册结论为 `PASS`。按文档可以完成收包、环境与数据 preflight、七 Gate、shadow 授权、零业务表核验和失败停止。

生产晋级仍为 `BLOCKED_DRAFT`。当前代码无法按正式门禁激活 Blackbox，也无法持久化回测，因此不能通过修改 SOP 文字把缺失能力描述成已具备。

## 7. 最终认证

允许的结论：

> Blackbox V2 技术入库与 shadow 验收已经基本稳定，可以继续用真实方案持续测试和提高入库效率。

禁止的结论：

> Blackbox V2 已经完成算法入库、回测落库、实盘预测、actual、API 和前端的生产稳定闭环。

下一次申请 `PRODUCTION_READY` 前，必须先关闭 BBV2-01 至 BBV2-07，再使用至少三个真实交付包覆盖日、周、月频率。通过后应重新执行本报告全部矩阵，而不是沿用本次强制激活证据。
