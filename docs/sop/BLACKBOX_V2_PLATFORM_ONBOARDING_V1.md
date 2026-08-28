# Blackbox V2 平台入库 SOP（Contract 1.0）

**文档状态**：`CURRENT`

**适用运行时**：`blackbox_v2`

本文只保留平台操作者必须执行的最短流程。字段合同见
[共享方案契约](../architecture/SCHEME_CONTRACT.md)，日期与批量复用见
[预测日期语义](../architecture/PREDICTION_SEMANTICS.md)，生产权限见
[生产信号与调度治理](../architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 1. 边界

- 上游负责证明交付脚本可运行、算法确定性、逐 Request 截止隔离以及 predict/backtest 等价。
- 平台不再运行 Blackbox `StaticGate`、冒烟 `CompareGate` 或独立 `shadow-register`。
- 平台只验证自己拥有的边界：Intake、DataBridge 输入接入、批量 Result 合同、回测持久化和生产状态切换。
- DataBridge generation 由 producer 独立发布并一次生成 ready snapshot。任何新 publish 开始前 producer 先撤销旧 ready 指针，完成后才原子发布新指针；方案流程只读取现成 generation，不构建、修复或重新验证 DataBridge。
- 回测、激活、gray gap-fill 和调度变更仍是不同副作用；执行一个命令不授权其它动作。

## 2. Step 1：Intake（新 ID）

交付目录必须恰好包含两个普通文件：

```text
{scheme_id}.py
{scheme_id}.json
```

执行：

```bash
python -m harness intake-blackbox \
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

新交付的 Metadata 必须显式包含合法 owner。Intake 不把 owner 复制进 `config.yaml`；首次注册及后续同步
从 Metadata 写入 `t_scheme_registry.owner`。历史 canonical Metadata 为保持 exact version 可以缺少 owner，
但此时只能保留数据库已有的合法 owner；数据库缺失时必须拒绝注册或同步。Dashboard 只读取 Registry，
不在请求期回读 Metadata 或仓库映射。

同 ID 修订不创建第二个方案目录，也不重复执行会拒绝已有 ID 的 Intake。只修订 canonical
`.py/.json` 与确有必要的平台 config，然后重新执行 Step 2、3；任何字节变化都会形成新的 exact
version。回测入口会复验脚本安全边界并保存校验策略摘要；激活不重复解析 AST/Metadata，只严格加载 canonical 当前字节，并匹配同 exact version、同当前校验策略的成功持久化回测，因此 evidence 后的任何交付漂移仍会被拒绝。

## 3. Step 2：完整持久化回测

先由业务确定历史/live 分界 `gray_target_start`。平台不从 Metadata、部署日或操作日推导它。

执行：

```bash
python -m harness gate backtest \
  --scheme-id {scheme_id} \
  --predict-date {gray_target_start} \
  --persist \
  --backtest-start-date 2025-01-01 \
  --timeout-sec 1800
```

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

回测运行视图只把 producer 封存文件物化为子进程私有只读文件；不重新哈希、解析或扫描 generation CSV。
子进程结束后仍校验私有视图未被改写，这是运行隔离，不是 DataBridge 重验。

## 4. Step 3：激活

取得该方案明确的激活授权后执行：

```bash
python -m harness activate --scheme-id {scheme_id}
```

激活严格加载 canonical 当前两文件身份，不重复运行脚本安全扫描，只接受与当前 exact version 精确匹配的成功持久化回测：

```text
scheme_version + code_hash + config_hash + manifest_hash
+ script_validator_policy_digest + runtime_profile + environment_fingerprint
+ generation_id + data_snapshot_id
```

首次激活在同一个命令内：

1. 校验当前 canonical exact version 的完整持久化回测；
2. 拒绝 base/composite 身份冲突；
3. 在一个数据库事务中建立 active exact version 与全部 active Registry；
4. 写入审批人与时间并在同一事务中读回。

因此不再要求操作者先执行一次没有算法运行、没有灰度流量的 `shadow-register`。事务任一步失败即整体回滚，
不产生 draft 残留、配置覆盖层或补偿 journal。

## 5. 可选后续动作

这些动作都不属于三步入库门禁：

- 单日历史 live 缺口：取得独立授权后执行
  `python -m harness signal-gap-fill --predict-date YYYY-MM-DD --scheme-id {scheme_id}`；
- Blackbox 连续缺口：对 exact active `weekly_point/h1` 或日频 `T+5/h5` 身份，用 target 半开区间一次批量执行
  `python -m harness signal-gap-fill --scheme-id {scheme_id} --target-date-from YYYY-MM-DD --target-date-before YYYY-MM-DD`。日频日期只从权威交易日历枚举；一个区间只解析一次 DataBridge authority，读取一次 producer-ready snapshot receipt、为该方案物化一次私有运行视图并启动一个算法 batch；
- 产品读模型检查：按需执行
  `python -m harness gate dashboard --scheme-id {scheme_id}`；
- 调度安装、timer/plist 变更、服务重启、Writer 切换：必须另行授权。

Dashboard 只证明当前产品可见性，不证明 exact version。Production Observed 仍必须由真实
launchd/systemd one-shot 时钟产生成功 `scheduled_live` 证据。

## 6. 灰度区间批量物化

历史回测与灰度实盘是两个独立批次和持久化边界：历史回测只处理
`target_date < gray_target_start`；激活后，连续灰度缺口由 target 半开区间一次批量物化。这样每条灰度 Request 仍使用 live `predict_date` 与自己的 `feature_date` 截止，同时消除逐日期进程启动、generation 解析和运行视图准备。

区间执行前冻结 exact version、DataBridge authority、输入 lineage 和完整 Request 集；任一业务键已存在即整组拒绝。全部算法结果成功后，repository 在一个事务中复核 active version、Registry、run、输入 provenance 和所有业务键，再写入全部 prediction 并完成各调度日 run。不得复制数据库主键、源 run、Actuals、指标或 Harness 历史，也不得建立跨激活候选表或临时结果目录。

固定未来 `source_end`、未来 test window、跨样本 selector/calibration 或版本/输入 lineage 不一致时，禁止批量物化为 live，必须重算逐点 live-safe 结果。完整规则以[预测日期语义 5.2](../architecture/PREDICTION_SEMANTICS.md#52-历史批次与灰度区间批次)为准。

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

- [ ] 新 ID 已由 Intake 原子保存两文件并生成 `paused/draft` config；同 ID 修订已通过相同 canonical 两文件校验；
- [ ] 当前 exact version 有完整、成功、不可变的持久化回测；
- [ ] 回测的 version/code/config/manifest、环境和 generation/snapshot 证据完整；
- [ ] activate 后 exact version 与所有 composite Registry 均为 active，审批与同一数据库事务的 readback 一致；
- [ ] 如执行 gap-fill，日期重建、insert-only 和 backtest/live 零重叠通过；
- [ ] 如需要 Dashboard 或自然调度验收，分别按其独立边界完成。

运行事实保留在现场控制面；稳定摘要进入 `CURRENT_STATUS.md`，未闭环事项进入 `TODO.md`，不再建立单次重复交接文档。
