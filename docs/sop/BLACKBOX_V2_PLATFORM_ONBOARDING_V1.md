# Blackbox V2 平台入库 SOP（Contract 1.0）

**文档状态**：`CURRENT`

**适用运行时**：`blackbox_v2`

本文只保留平台操作者必须执行的最短流程。字段合同见
[共享方案契约](../architecture/SCHEME_CONTRACT.md)，日期与批量复用见
[预测日期语义](../architecture/PREDICTION_SEMANTICS.md)，生产权限见
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 1. 边界

- 上游负责证明交付脚本可运行、算法确定性、逐 Request 截止隔离以及 predict/backtest 等价。
- 平台只验证自己拥有的边界：Intake、DataBridge 输入接入、批量 Result 合同、回测持久化和生产状态切换。
- 方案流程只消费已有 producer-ready 输入，不构建或修复 DataBridge；发布与兼容边界见[DataBridge 契约](../blackbox_v2/data_bridge_v1/README.md)。
- 回测、激活、gray gap-fill 和调度变更仍是不同副作用；执行一个命令不授权其它动作。

## 2. Step 1：Intake（新 ID）

交付目录必须恰好包含两个普通文件：

```text
{scheme_id}.py
{scheme_id}.json
```

执行：

```bash
python -B -m harness intake-blackbox \
  --delivery-dir <delivery-dir> \
  --project-root .
```

Intake 一次完成：

- 两文件、文件名、Metadata 与 `scheme_id` 合同；
- `name`、`owner`、`description`、任务和期限字段；
- 固定 Runtime Profile 与 Data Schema；
- 脚本语法和平台安全静态边界（危险导入/调用、绝对路径与路径穿越）；
- 原子保存原始交付字节；
- 生成 `blackbox_v2 + paused + draft` canonical config；
- 已有 base ID 拒绝覆盖。

成功后只生成：

```text
schemes/{scheme_id}/
├── config.yaml
└── delivery/
    ├── {scheme_id}.py
    └── {scheme_id}.json
```

Intake 不读取 DataBridge、不运行算法、不写数据库业务表，也不激活 Registry。

仅对已完成增量算法验收的交付，可显式增加 `--incremental-state`；Intake 在平台 config 中写入
`incremental_state: true` 并纳入 exact version，Metadata 和两文件字节不变。缺省字段的既有版本哈希保持不变；
配置出现 `false/null/字符串/数字` 或 Native 声明均拒绝，不设置全局自动启用。

新交付 Metadata 必须显式包含合法 owner；Intake 不将其复制进 config。注册与历史兼容按
[共享身份契约](../architecture/SCHEME_CONTRACT.md#3-方案身份)处理。

同 ID 修订直接修改获准的 canonical 两文件及必要 config，然后执行第 3、4 节，不重复 Intake。任何交付字节变化都会改变 exact；回测后的漂移必须重新验证，不能复用旧激活证据。

## 3. Step 2：完整持久化回测

执行前完成以下准备：

1. 由业务确定历史/live 分界 `gray_target_start`，不得从 Metadata、部署日或操作日推导。
2. 在开发工作区将本次拟激活的 canonical 定稿为 `status: active`、`version_status: active`，并准备部署矩阵；通过标准 discovery 固定最终 exact version。Intake 的 paused/draft 只是初始文件状态。配置定稿不建立数据库身份、不授予激活权。
3. 按[部署运行手册](../../deploy/README.md)冻结候选 release；目标环境从该不可变 release 执行回测、激活和补缺。回测后不得再改配置或两文件来取得调度资格，`activate` 不修改 canonical。
4. 按[手工 Harness 环境绑定](../../deploy/README.md#手工-harness-的目标环境绑定)核对本次进程，再检查输入 ready receipt、完整 Request 清单及回测授权；以下命令不自动加载目标服务环境。训练历史不按展示起点裁剪。

执行：

```bash
python -B -m harness gate backtest \
  --scheme-id {scheme_id} \
  --predict-date {gray_target_start} \
  --persist \
  --backtest-start-date 2025-01-01 \
  --timeout-sec 7200
```

7200 秒是示例显式指定的完整离线回测预算，不修改 CLI 默认 timeout 或每日预测限制。
同 exact version 的有效完整证据可复用；算法版本、输入或校验策略变化需要按证据绑定规则重新验证，
“只做一次”不表示未来任何变更都不必重跑。

已完成运行时升级的证据按[运行时迁移边界](../architecture/SOURCE_ALGORITHM_FIDELITY.md#7-同算法-native--blackbox-迁移)只读保留，不走本节重新计算，也不得恢复已退役的原件复用入口。

回测只做一次完整计算：

1. 复验当前 canonical 目录仍是安全的精确两文件交付；
2. 读取已有 producer-ready generation receipt；
3. 构造完整 HistoricalCase；
4. 把完整 HistoricalCase 写成一份 Request CSV，按 Runtime Profile 只调用一次交付的 backtest 入口；
5. 逐行校验 Result 数量、顺序、Request 回显、日期、方向与输出合同；
6. 在一个事务中写入一个 immutable success run、完整 prediction 明细和非空 monthly metrics；
7. 在 durable summary 中保存 exact version、code/config/manifest、脚本校验策略摘要、Runtime Profile、环境指纹、generation 和 snapshot。

它已经包含真实批量执行，因此平台不再提前额外运行一次 predict 冒烟。失败重试必须产生新的
immutable run；不得更新、删除或补写旧 run。

私有视图按[DataBridge 契约](../blackbox_v2/data_bridge_v1/README.md#generation-与运行视图)执行输入隔离，不重验 generation。显式增量方案另核验私有输入前后内容摘要；完整回测与 gray replay 使用本次私有状态，不读取或推进生产状态。

## 4. Step 3：激活

取得该方案明确的激活授权后执行：

```bash
python -B -m harness activate --scheme-id {scheme_id}
```

激活严格加载 canonical 当前两文件身份，不重复运行脚本安全扫描，只接受与当前 exact version 精确匹配的成功持久化回测：

```text
scheme_version + code_hash + config_hash + manifest_hash
+ script_validator_policy_digest + runtime_profile + environment_fingerprint
+ generation_id + data_snapshot_id
```

首次激活在一个事务内：

1. 锁定并复核唯一成功的 exact-version 持久化回测，拒绝 base/composite 身份冲突；
2. insert-only 发布缺失的 `t_scheme_predictions` 产品事实，以 `backtest_run_id` 指向证据 run；
3. 建立 active exact version 与全部 active composite Registry，写入审批人与时间；
4. 锁内读回事实和 lifecycle 后提交，任一步失败整体回滚，不留下 draft 身份。

同 ID revision 激活不重写历史产品事实。无需 `shadow-register`，也不生成配置覆盖层或补偿 journal。

## 5. 可选后续动作

这些动作都不属于三步入库门禁：

- 单日历史 live 缺口：取得独立授权后执行
  `python -B -m harness signal-gap-fill --predict-date YYYY-MM-DD --scheme-id {scheme_id}`；
- Blackbox 连续缺口：先按[第 6 节](#6-灰度区间批量物化)确认区间支持范围与 live-safe 条件；
- 产品读模型检查：按[Dashboard 认证验收](../operations/PUBLIC_FACTOR_LAB_PERFORMANCE.md#认证响应与合同验收)取得真实授权响应并验证；默认裸 CLI 不携带会话；
- 调度安装、timer/plist 变更、服务重启、Writer 切换：必须另行授权。

单日入口省略 `--scheme-id` 时扫描当日全部应运行 active 方案，只有授权覆盖完整范围时才可使用；指定 ID 缩小范围。`SKIP_NOT_DUE`、`SKIP_PRESENT` 是无写入的正常结果，仅 `GRAY_LIVE_GAP` 执行。每个 base 独立提交、多 target 组内原子；一个方案失败不回滚其他方案的成功事实。

Native 单日按权威日期推导 feature_date 并构造本机截止输入；Blackbox 使用计划绑定的冻结 DataBridge authority。单日/区间命令都内部规划，不接收外部 plan、operator、HMAC token 或 plan SHA；任一 blocker 在执行前停止，不自动重试、fallback 或切旧版本。失败先读回已发生副作用，再按实际缺口处理。

Dashboard 只证明当前产品可见性，不证明 exact version。Production Observed 仍必须由真实
launchd/systemd one-shot 时钟产生成功 `scheduled_live` 证据。

### 5.1 增量方案的显式预热/重建

仅适用于显式启用增量能力、已完成目标环境验证并取得本次状态维护授权的 exact version。

执行前按[手工 Harness 环境绑定](../../deploy/README.md#手工-harness-的目标环境绑定)核验本次进程，并从 installed/loaded 控制面及环境文件读取真实 Runtime Profile allowlist（当前 `LANG/LC_ALL/TZ`）。保存有效值和环境摘要：真实入口缺少的变量须从维护进程移除，不能设为空值，也不能继承 SSH locale。已有状态与真实环境不匹配时，保留原证据后受控重建，不改 header、全局调度或其他方案来迁就旧状态。

```bash
python -B -m harness rebuild-blackbox-state \
  --scheme-id <scheme-id> --predict-date YYYY-MM-DD \
  --expected-scheme-version <exact-version> --approved-by <operator> \
  --project-root <immutable-release>
```

该入口复用 ready 输入、标准 Request 和唯一 executor，从空状态计算并发布新派生状态；不创建 run/prediction/backtest/Actual，不激活或补发历史信号。它会写状态文件，不能称作只读验证。

| 核验项 | 通过条件 |
|---|---|
| 位置与独占 | `BFL_RUNTIME_ROOT/blackbox-state` 按 base/exact 隔离；可信根内无穿越或 symlink；同 base 重建与日常推进共用非阻塞锁，拒绝第二 Writer |
| 执行预算 | 重建不超过 `min(Profile.backtest_timeout_sec, 7200)` 秒，日常增量 predict 不超过 `min(Profile.predict_timeout_sec, 120)` 秒；更短 caller deadline 仍生效；最多 4 GiB、8 数值线程，不改 stateless 默认 Profile |
| 状态身份 | payload 不超过 16 MiB；封装绑定 exact、generation/snapshot、私有输入摘要、实际代码/Metadata/环境及完整性校验 |
| 发布与证据 | Result、输入和状态全部复验成功后，同目录 fsync/replace/fsync 原子发布；记录 operator、版本、日期和状态摘要；用正常增量路径验收重建结果 |

缺状态、损坏、版本/环境不符或算法拒绝复用均明确失败，不自动 fallback、重建或重试。发布后的目录 fsync 失败可能已留下完整新状态：报告失败，按相同 Request 受控重试，不覆盖回旧状态；业务事实仍经 repository insert-only 提交。文件锁不替代 Writer 操作授权。

发布前必须通过[公共状态合同与执行链验证](../onboarding/README.md#可复用测试矩阵)，覆盖崩溃恢复和双 Writer；本地实现或私有试点通过不能代替目标环境验收。

## 6. 灰度区间批量物化

本节是 target 区间入口支持范围与操作步骤的权威来源。仅支持 exact active Blackbox：

| task_type | horizon |
|---|---:|
| `weekly_point` | 1 |
| `T+1` | 1 |
| `T+5` | 5 |

代码支持不等于目标环境已验收，尚未闭环的现场验证见[后续计划](../TODO.md)。其它任务使用第 5 节的既有单日入口，不扩展区间支持。

1. 按[预测语义 5.2](../architecture/PREDICTION_SEMANTICS.md#52-历史批次与灰度区间批次)确认逐 Request 截止与 predict/backtest 等价；无法证明 live-safe 时改用逐点计算，不复用未来 `source_end`、test window 或跨样本校准结果。
2. 固定本机 exact version、DataBridge authority、输入 lineage、完整 Request 集和授权 target 半开区间；与历史回测的绑定一致。日频日期由权威交易日历枚举。
3. 执行：

   ```bash
   python -B -m harness signal-gap-fill --scheme-id {scheme_id} \
     --target-date-from YYYY-MM-DD --target-date-before YYYY-MM-DD
   ```

4. 一个授权区间只解析一次输入 authority、读取一次 ready receipt、物化一次私有视图并启动一个算法 batch；任一业务键已有即整组拒绝。
5. 核验 repository 原子提交的全部日期 run 与 prediction，确认历史/灰度零重叠、应有点零缺口，以及 exact、来源和 Actual join。事务及失败不变量由预测语义定义，不通过删旧结果重试。

## 7. 失败处理

| 场景 | 处理 |
|---|---|
| 新 ID Intake 失败 | 修正交付后重新 Intake；目标目录不得有部分写入 |
| 同 ID 修订两文件校验失败 | 修正 canonical 交付后重新完整回测；不得带旧 evidence 激活 |
| 无 ready generation | 停止；由 DataBridge producer 独立发布，方案流程不代建 |
| 回测失败 | 保留失败现场；修复后创建新的完整回测 run |
| exact version/环境/输入证据不匹配 | 禁止激活，重新回测当前 exact version |
| 身份冲突 | 禁止覆盖、删除或手工改 Registry |
| activation 事务失败 | 整体回滚；修复原因后重新执行 `activate` |
| backtest/live 重叠 | 保留旧 run，创建新的正确 canonical run |
| DataBridge 或日历失败 | 当前运行 fail-closed，不 fallback 到旧 generation |

## 8. 完成条件

入库完成以第 3 节成功持久化回测和第 4 节激活读回为证据：二者对应同一最终 canonical exact，首次激活的产品事实齐全，修订未改写历史。若任务包含补缺，按第 5 或第 6 节所选入口完成完整键集核验；生产接管按[准备清单](../blackbox_v2/PRODUCTION_READINESS.md)验收，自然运行单独观察。

运行证据保留在本机控制面和外置目录，包括 exact、输入、回测、激活与授权摘要；直接操作仅保存 operation hash，不把原始内部 operation id、凭据或完整输入提交到仓库。文档仅按[文档中心](../README.md)保留当前摘要、未闭环事项和证据索引。
