# 方案生命周期状态的存放位置设计

## 背景

`schemes/{scheme_id}/config.yaml` 的 `status`（paused/active）与 `version_status`（draft/active）
由激活链路改写：Native 经 `activate_gate._set_status`，Blackbox 经
`shared/blackbox_v2/lifecycle.perform_lifecycle_transition → atomic_update_config`。

而 `config.yaml` 位于 immutable release 内、参与 `source_tree_sha256`。因此每次激活必然造成
release digest 相对安装记录漂移。

2026-08-22 现场实测（`weekly_1y_causal_v1_31_0_standalone`）：

```
激活前  source_tree_sha256 = 381ad1e7…6674   （与安装记录一致）
激活后  source_tree_sha256 = 800afc2c…f450   *** MISMATCH ***
```

逐文件核验确认漂移**仅由该 config.yaml 引起**：把两行 status 还原后整树 digest 与安装记录逐字节
相同，release 中没有其它内容被改动。复位方式是把状态变更提交进源码线、再发一版 release——这正是
仓库中既有方案全部以 `status: active` 提交的原因。

**成本**：每入库一个方案要多付一个完整 release 周期（构建 → SCP → 三重校验 → 预安装 → CAS →
重启），且该周期唯一的目的是擦除平台自己造成的漂移。按后续约 20 个待入库方案计，这是 20 次纯开销。

## 第一性判断

`status` / `version_status` 回答的是「**这个方案此刻在这台主机上是否活跃**」——是**主机级运行状态**，
生命周期是这台主机；而 release 的生命周期是一次发布。两者生命周期不同，不应共用同一个存储。

这与今日已确立的规则同源：*代码不得从自身所在位置派生运行期状态*。

**平台其实已经画出了这条界线，只是没有贯彻到存储上。** `shared/blackbox_v2/versioning.py` 的
`canonical_platform_config` 只把以下字段纳入版本计算：

```
runtime_type / input_source / runtime_profile / data_schema_version
schedule{cron,timezone,timeout_sec} / delivery{script,metadata} / platform_inputs
```

`status` 与 `version_status` 被**刻意排除**——这正是激活改写 config 之后 `scheme_version` 仍为
`c93f76489d5b` 不变的原因。即：平台在概念上已认定它们不属于方案身份，只是物理上仍与身份同处一个文件。

因此本设计不是重新定义方案身份，而是**把两个已被排除在身份之外的字段搬到与其生命周期匹配的位置**。

### 第二个独立理由：单一 `status` 无法表达双主机

同一份 release 同时运行在 Mac3 与 ECS 上。`weekly_1y_causal_v1_31_0_standalone` 当前在 release 内
声明 `status: active`，但它在部署矩阵中是 `["aliyun-gray"]`，在 Mac3 上不会运行——**该字段在 Mac3
上是失真的**，靠部署矩阵兜住。矩阵表达的是「资格」，不是「激活」；若要在 ECS 激活而 Mac3 保持
paused（灰度工作流的常态），单一 `status` 字段根本无法表达。

## 目标

使激活不再写入 release 树，`source_tree_sha256` 在方案生命周期变更前后保持一致；同时让状态成为
真正的主机级事实。

不改变：`scheme_version` 的定义与计算；三重资格（config / exact version / registry）的判定强度；
Registry 生命周期；授权要求；`t_scheme_versions` 与 `t_scheme_registry` 的既有语义；上游契约。

## 最小设计

### 1. 引入主机级生命周期覆盖层

```
<BFL_RUNTIME_ROOT>/lifecycle/{scheme_id}.json
{
  "scheme_id":      "...",
  "scheme_version": "c93f76489d5b",
  "status":         "active",
  "version_status": "active",
  "updated_at":     "2026-08-22T10:11:00+00:00",
  "harness_run_id": "hr_..."
}
```

路径经既有 `shared.runtime_paths.resolve_runtime_state_path` 派生，与 DataBridge、cache、harness
报告同一机制，不新增第二套路径解析。

### 2. 读取顺序与 fail-closed

`scheduler/discovery.load_scheme_config` 解析 `config.yaml` 得到身份与**初始声明**状态，随后按
以下顺序确定生效状态：

1. 覆盖层存在，且其 `scheme_version` **精确等于**本次计算出的 `scheme_version` → 采用覆盖层的
   `status` / `version_status`；
2. 覆盖层不存在 → 采用 `config.yaml` 中的初始声明（新 intake 方案即 `paused` / `draft`）；
3. 覆盖层存在但 `scheme_version` 不匹配、文件不可读、字段缺失或非法 → **视为未激活**并记录原因。

第 3 条是关键：release 一旦更换使方案代码或平台配置变化，`scheme_version` 随之改变，旧覆盖层
自动失效，方案回落为未激活。**代码变了就必须重新激活**，不会被过期状态带上线。

### 3. 写入点改向

- Native：`activate_gate._set_status` / `_write_expected_active_config` 改写覆盖层；
- Blackbox：`lifecycle.atomic_update_config` 改写覆盖层。

两者的原子写入、`_fsync_directory`、journal 记录与补偿语义原样保留，只更换目标路径。
`config.yaml` 从此**只读**，激活链路不再触碰 release 树。

### 4. 三重资格不变

`config active + exact version active + registry target active + cadence 匹配` 四项判定逻辑
完全不动，只是第一项的取值来源从「release 内的文件」变为「主机级覆盖层，回落到 release 内的初始
声明」。discovery 仍为纯文件系统操作，不引入对数据库的新依赖，不改变分层。

## 迁移

现存 65 个方案的 `config.yaml` 已提交为 `status: active`。首次部署本设计时，各主机需要为其**在本机
实际激活**的方案生成覆盖层。生成方式只允许从该主机数据库的既有 `t_scheme_versions` +
`t_scheme_registry` 状态推导，不得凭 `config.yaml` 推断——因为后者在 Mac3 上对 gray-only 方案是
失真的。

迁移是一次性的受控运维操作，需独立授权，不由本设计推断。迁移完成前，覆盖层缺失即回落到
`config.yaml`，行为与现状完全一致，因此可以先部署代码、后迁移。

## 测试与验收

1. 激活前后 `source_tree_sha256` 与安装记录**保持一致**（本设计的核心验收）。
2. 覆盖层 `scheme_version` 与当前不匹配时，方案判定为未激活，且 discovery 记录原因。
3. 覆盖层缺失时回落到 `config.yaml` 初始声明，与现状逐字段一致。
4. 覆盖层不可读、字段缺失或非法时 fail-closed 为未激活，且无文件系统副作用。
5. 三重资格的既有拒绝用例逐条保持（paused config / draft version / paused registry / cadence 不符）。
6. Native 与 Blackbox 两条写入路径都不再修改 `schemes/*/config.yaml`；构造一次激活后断言
   `config.yaml` 字节不变。
7. lifecycle journal 的记录、补偿与 reconcile 语义不变。
8. 全量回归对照纯净 HEAD 无新增失败。

## 停止条件

- 需要改变 `scheme_version` 的定义或计算；
- 需要削弱三重资格中任一项；
- 需要 discovery 依赖数据库；
- 迁移无法从数据库既有状态确定性推导某方案在本机的真实状态。
