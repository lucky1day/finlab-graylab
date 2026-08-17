# Liwei Phase A 缓存结果复用与 Linux input_state 重绑定设计

**日期：** 2026-08-17
**范围：** 阿里云 ECS 候选部署中的 7 个 Liwei Phase A cache family
**决策：** 复用现有缓存结果，只建立 Linux `input_state` lineage；不重训、不跑跨平台 CompareGate、不启用 timer

## 1. 背景与目标

阿里云 ECS 已挂载并通过安全加载检查的 Liwei Phase A 缓存覆盖本轮所需日期，但缓存 generation 记录的是 macOS 解析后生成的 `input_state`。Linux 从同一份 DataBridge CSV 读取数据时，少量浮点值出现 1 ULP 的解析差异，导致 `input_content_id` 改变；现有 publisher 因此把它识别成历史输入修订并要求全量重建，而不是 cache hit。

已确认的事实：

- ECS 与 Mac 使用的 daily、weekly、monthly DataBridge CSV 字节和 SHA256 一致。
- 7 个 cache family 的规格 fingerprint、覆盖日期和缓存完整性校验均通过。
- 差异来自相同 CSV 在不同 CPU 架构上的浮点解析结果，不是数据文件变化。
- 现有缓存契约明确拒绝 `rebind`，直接编辑 `manifest.json` 或 `current.json` 会被安全加载器拒绝。
- 7 个现有 generation 都没有绑定旧 `input_state` 的 Compare qualification，因此重绑定不需要重跑 CompareGate；仍需按新 `input_state` 机械重建完整性证据。

本设计的目标是：在不运行任何 Phase A 训练的前提下，为 7 份现有缓存创建不可变的 Linux 子 generation，让正常任务随后命中缓存并继续执行、写库。

## 2. 本轮边界

### 2.1 包含

1. 增加一个窄范围的 `migration_rebind` cache build mode。
2. 用一份精确 migration receipt 把授权限定为本次 7 个旧 generation 与对应 Linux `input_state`。
3. 在 ECS 已隔离复制的缓存根中创建 7 个不可变子 generation，并原子切换各自 `current.json`。
4. 使用正常消费者入口验证 7/7 cache hit。
5. 随后继续原定的手工调度任务和数据库读回验收。

### 2.2 不包含

- 不训练、续训或重算任何 Phase A baseline。
- 不修改缓存中的 `test_dates`、`config`、`preds` 或 `probs`。
- 不修改 Native 算法、模型参数、特征、窗口、投票或输出映射。
- 不声称 Linux 重算结果与 macOS 缓存数值等价。
- 不运行 macOS/Linux CompareGate。
- 不允许通用、长期或自动的输入 lineage waiver。
- 不原地改写源缓存 bundle，不删除父 generation。
- 不启用任何 systemd timer。

本次变更属于缓存与输入 lineage 的 L0 迁移适配，不改变算法逻辑。用户接受的生产边界是“复用已验证缓存结果，验证 Linux 任务可执行并能写库”，而不是跨平台数值复算验收。

## 3. 方案选择

采用“不可变子 generation + 精确 receipt”：

```text
已验证 Mac generation
        │  缓存语义内容逐字段哈希保持不变
        ▼
Linux migration_rebind generation
        │  input_state = Linux 当前真实输入状态
        ▼
正常 prepare_phase_a_caches 调用返回 hit
```

不采用以下方案：

- **全量 Linux 重建：** 能建立原生 lineage，但会重复数小时训练，不符合本轮部署目标。
- **直接编辑 manifest/current：** 会破坏摘要与 acceptance lineage，安全加载器应继续拒绝。
- **永久忽略 input mismatch：** 会把未来真实数据修订也错误当成命中，禁止。
- **把复用伪装成 `full`：** 会虚构“Linux 已完成 authoritative training”，审计语义不真实，禁止。

## 4. 最小实现

### 4.1 精确 migration receipt

新增一份版本化 JSON receipt。它只记录非秘密标识，并逐 family 固定：

- migration schema 与唯一 migration ID；
- `cache_family`、`tenor`、spec fingerprint；
- 父 generation ID、父 manifest SHA256、父 generation content ID；
- 父 `input_content_id`；
- 目标 Linux `input_content_id`；
- baseline 集合及每个 baseline 的 cache content SHA256；
- receipt 自身 canonical SHA256。

receipt 不包含数据库 DSN、凭据、预测值或授权 token。运行时只有在全部字段与现场精确匹配时才允许重绑定；任一 generation、输入、规格或缓存摘要不同都 fail closed。

### 4.2 一次性重绑定入口

增加一个一次性 operator CLI。它复用正常输入构建和 cache 安全加载逻辑，但不进入 `train_missing`：

1. 只读加载 Linux 当前 DataBridge 输入，生成真实目标 `input_state`。
2. 安全加载 receipt 指定的父 generation，并核对 current 指针。
3. 核对 family、tenor、spec、父摘要、目标 input ID、baseline 集合和缓存内容摘要。
4. 以父缓存内容创建 `migration_rebind` 子 generation；affected scope 为空，全部父日期属于 preserved scope。
5. 重新生成与 Linux `input_state` 一致的未绑定缓存完整性证据。
6. 完整执行 staged generation 与 lineage 校验后，才原子替换 `current.json`。

CLI 只处理 receipt 中明确列出的 7 个 family。它不提供通配 family、任意目标 input ID、跳过摘要或强制覆盖参数。

### 4.3 安全加载契约

现有 `full`、`append`、`suffix`、`qualification` 行为保持不变。新增的 `migration_rebind` 只有同时满足以下条件才可被 secure lineage 接受：

- 存在真实父 generation，且 acceptance parent binding 与父文件精确匹配；
- manifest 内嵌的 migration evidence 与 receipt entry 摘要一致；
- 父子 spec fingerprint 和 baseline 集合完全一致；
- 父子每个 baseline 的 cache content SHA256、逐字段 SHA256、日期数和配置数完全一致；
- `affected_dates=[]`，`preserved_dates` 精确等于父缓存全部日期；
- 父子 preserved scope SHA256 完全一致；
- 子 generation 的 `input_state.content_id` 精确等于 receipt 中的 Linux 目标 ID；
- `native_generation` 继续为 `null`，acceptance 继续为 `NON_PRODUCTION`。

普通 publisher/consumer 不接收 rebind 开关，也不能自行生成该模式。迁移完成后删除服务器上的临时 receipt 引用；后续调用只按标准 lineage 命中或按既有规则 append/full rebuild。

## 5. 原子性与回滚

每个 family 独立执行：先写 staging、完整校验、原子落 generation，最后原子更新 `current.json`。因此单个 family 失败不会留下半成品 current。

7 个 family 全部完成前不启动手工预测写库。若中途失败：

1. 停止重绑定，不启动预测服务；
2. 保留失败日志和已生成的不可变子 generation；
3. 将已切换 family 的 `current.json` 原子恢复为执行前记录的父指针；
4. 读回 7 个 current 全部恢复后再定位问题。

源 bundle 和执行前的隔离副本始终保留，因此回滚不需要删除文件，也不涉及数据库操作。

## 6. 测试与服务器验收

### 6.1 本地自动测试

必须覆盖：

- 未提供 receipt 时，`migration_rebind` 仍被拒绝；原来的裸 `rebind` 始终被拒绝。
- 父 generation、manifest SHA、spec、目标 input ID 或任一 baseline 摘要不符时 fail closed。
- 子 generation 的任一 `preds`、`probs`、`config` 或日期被修改时 fail closed。
- 合法重绑定不调用训练 callback，并生成可 secure-load 的子 generation。
- 合法子 generation 再次走正常 `prepare_phase_a_caches` 时返回 `build_mode=hit`。
- 原有 full/append/suffix/qualification 与安全路径测试继续通过。

### 6.2 ECS 重绑定验收

执行前记录 7 个 current 指针和 manifest SHA。执行后必须满足：

- receipt 精确列出的 7 个 family 全部生成一个新子 generation；
- 7 个父 generation 仍存在且未修改；
- 7 个子 generation 的目标 `input_content_id` 均为 Linux 现场计算值；
- 父子所有 cache semantic/field hashes 完全一致；
- 7 个 current 均指向对应子 generation；
- 7/7 secure lineage 校验通过；
- 用正常消费者入口复查时 7/7 返回 `status=hit`、`build_mode=hit`，训练 callback 调用数为 0；
- 无残留构建进程，无 cache lock，无 OOM；全部 timers 仍为 disabled/inactive。

### 6.3 手工任务与写库验收

缓存命中闭环后，继续既定部署顺序，手工启动一次性预测服务。成功标准保持为：

- due active scheme 均产生本次 `t_scheme_runs` 与 `t_scheme_run_log`；
- 成功 run 的 predictions 可按 run ID、exact version、日期与 tenor 精确读回；
- 9 个 paused Native 不执行、不写 prediction；
- 无失败或悬挂 run、无残留算法子进程、无 OOM；
- 所有 systemd timers 仍为 disabled/inactive。

若缓存已 7/7 hit 但某个方案仍执行失败，按该方案的独立运行故障处理，不再回退为全量缓存重建。

## 7. 完成定义

只有同时满足以下条件，缓存迁移才闭环：

1. 7 个缓存结果未改变，并已建立可审计的 Linux `input_state` 子 lineage。
2. 正常运行路径对 7 个 family 全部真实 cache hit，且没有触发训练。
3. 手工调度任务能够执行并留下数据库 run/log/prediction 证据。
4. timers 全程保持关闭。

本设计只授权本次 receipt 固定的 7 个 generation 重绑定，不授权未来缓存迁移、算法变更、跨平台等价声明、timer 启用或生产流量切换。
