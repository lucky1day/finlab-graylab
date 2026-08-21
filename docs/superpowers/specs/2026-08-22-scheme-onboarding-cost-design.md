# 方案入库成本设计

## 背景

Blackbox V2 的立项前提是：**不修改算法内部，尽快把方案送进灰度实验室观察**。后续会有持续的方案
流入。当前每入库一个方案都要在两台机器上各做一次完整的不可变发布，这与该前提冲突。

本设计只处理**入库成本**，不处理算法保真、Gate 判定或授权模型。当前阶段尚未进入生产级维护，
因此设计原则是：**优先用既有规则和既有机制解决，不新建子系统。**

## 现状成本拆解

新增一个 Blackbox 方案，今天实际发生的动作：

| # | 动作 | 机器 |
|---|---|---|
| 1 | 收两文件交付、intake、写入 `schemes/{id}/` | Mac3 |
| 2 | 跑 `all` 六段 Gate | Mac3 |
| 3 | 改 `deploy/scheme_deployment_matrix_v1.json`、`deploy/scheme_owner_v1.json` | Mac3 |
| 4 | 改测试里写死的方案计数 | Mac3 |
| 5 | commit、tag、确定性构建 archive | Mac3 |
| 6 | SCP、三重 digest 校验、预安装、CAS、重启 Backend | ECS |
| 7 | 再跑一次 `all` 六段 Gate | ECS |
| 8 | draft-register、activate、Registry | ECS |
| 9 | **重复 5–6 到 Mac3** | Mac3 |

其中 4、7、9 是可以消除的；5–6 是这套设计的核心价值，不应消除。

## 第一性：两种变更被焊在一起

仓库里存在两类生命周期完全不同的变更：

| | 平台代码变更 | 方案内容新增 |
|---|---|---|
| 频率 | 低 | 持续 |
| 影响面 | 全部方案、两台机器 | 单个方案、单台机器 |
| 需要的保证 | 确定性、可回滚、双环境一致 | 该方案的内容可追溯、可独立回滚 |
| 当前载体 | source release | **同一个 source release** |

因为方案内容存放在源码树内，新增内容等同于新增代码版本，于是继承了平台代码的全部发布代价。

### 证据：平台自己已经不把 Blackbox 交付当代码

`shared/versioning.compute_code_hash()` 只覆盖 `predict.py` 与 `core/**/*.py`——那是 Native 布局。
Blackbox 方案目录只有 `config.yaml` 与 `delivery/{id}.{py,json}`，因此实测：

```
weekly_1y_causal_v1_31_0_standalone   code_hash = e3b0c44298fc1c14...   (空字符串的 SHA-256)
cgb_causal_wk_1y_v128                 code_hash = e3b0c44298fc1c14...   (同上)
liwei_0616_cons_sda_k3_div_k10        code_hash = 2ffdcfac682fe51f...   (Native，有实际内容)
```

`manifest_hash` 也为 `None`（只在方案根找 `manifest.*`，Blackbox 的清单在 `delivery/` 下）。

**结论：Blackbox 方案的 `scheme_version` 只由 `config.yaml` 派生。3213 行的交付算法不参与
`code_hash`、不参与 `manifest_hash`、不参与 `scheme_version`。**

也就是说平台在语义上已经把 Blackbox 交付当作**不透明内容**（隔离子进程执行、内部保真由上游负责、
不定义方案身份），却在物理上把它当作**代码**存放（进 release、进 `source_tree_sha256`、按代码节奏发布）。
入库成本正来自这个错配。

### 附带暴露的真实风险

当前安排下，改动 Blackbox 交付算法而不改 `config.yaml`，`scheme_version` **不会变化**。现阶段这被
接受为频繁修正期的权衡，但它意味着交付内容本身没有独立的身份记录。任何把交付移出源码树的方案
必须为其建立独立 digest——因此该方向不是"放松追溯"，而是**补上当前缺失的追溯**。

## 方案

### A. 立即可用：不把灰度方案晋级到 Mac3（零成本，不需要任何代码或规则改动）

项目规范原文是「ECS 先晋级验证，Mac3 **后续如需**晋级只能使用该份已验证 archive」，并明确
「分阶段晋级期间两端 `current` 可以不同，这不构成环境分支」。

因此消除动作 9 不需要任何新设计——**它已经是被允许的，只是没有被使用。**

配套：新方案在部署矩阵中登记为 `["aliyun-gray"]`。这样 Mac3 停留在旧 release 是自洽的——
discovery 校验的是「该 release 内被发现的方案」与「该 release 内的矩阵」一一覆盖，两台机器各自成立。

同时消除动作 7 的重复：方案只在 ECS 生效时，Gate 只需在实际运行它的环境跑一次。

**止损规则**：**平台变更即晋级两端；方案内容不晋级。**

该规则精确限定了漂移维度：平台代码的 delta 恒为 0，两台始终运行同一套平台逻辑；累积的只有方案
内容，而这些方案在矩阵中排除了 Mac3，因此在 Mac3 上是惰性的——discovery 按 target 过滤，永不加载；
矩阵覆盖校验仍然满足（它们被列出，只是目标不含 `mac3-production`）。落后的部分恰好是对 Mac3
无影响的部分。这不是容忍漂移，是让漂移只发生在无害维度上。

**适用边界与真实成本**：方案从灰度进入 Mac3 真实使用时，**不只是增加一条矩阵记录**。
`deploy/blackbox_v2/` 下有两份分平台冻结清单（`environment_manifest.json` 为 `linux-64`，
`environment_manifest.osx-arm64.json` 为 `osx-arm64`），由
`shared/blackbox_v2/environment_manifest.py` 按 `platform.system()/machine()` 选择。只在 ECS
验证过的方案跑的是 `linux-64` 那份，**从未被验证过 Mac 环境**。因此晋级到 `mac3-production`
必须在 Mac3 上以 `osx-arm64` 环境重跑 Gate。

即 A **推迟**而非消除 Mac3 的验证成本，且只对真正被晋级的方案付这笔钱。考虑到多数灰度方案最终
不会进入 Mac3，该交易成立。

**Mac3 当前风险敞口**：生产 runner 不 import harness（`launchd_prediction_runner`、
`actuals_runner`、`executor` 均无 harness 依赖），因此 Mac3 的五个 launchd one-shot 不暴露在
harness 路径问题下，可以从容停留在旧 release。唯一例外是 `signal-gap-fill`——它会把 report root
写入 release 树，并使用 `$HOME/Library/Application Support/...` 作为锁目录（落在声明的 runtime
root 之外）。**平台修复晋级前，不应从 Mac3 生产 release 执行该命令。**

### B. 批量入库（明确不采用）

把 N 个方案合成一次发布可以把 ceremony 摊薄 N 倍，但项目规范明确要求「每个新方案各自接收独立的
Blackbox V2 两文件交付，分别走 Intake/Gate/ECS-only activation，不把多个方案绑成一个提交、发布或
回滚单元」。批量会牺牲独立回滚，代价高于收益。**不采用。**

### C. 方案内容与平台代码分离（结构性，本阶段不实施）

把 Blackbox 交付从源码树移到独立的内容存储，按方案粒度安装与回滚，各自携带 digest 并记入
`t_scheme_versions`。新增方案不再改变 `source_tree_sha256`，因此完全不触发平台发布。

这能永久消除耦合，并顺带补上上文那个身份缺口。但它要改 intake、discovery、Gate 的输入解析和
安装工具，是一个真实的子系统。

**本阶段不实施。** 理由：A 已经拿走了大部分成本且为零成本；在没有到生产级维护之前，为此新建
子系统违背「不要沉重设计」的原则。若 A 实施后入库成本仍是瓶颈，再以独立设计推进 C。

### D. 解除测试与方案清单的耦合（小改动，立即做）

当前测试写死方案数量：

```python
self.assertEqual(len(blackbox_paths), 39)   →  40
assert len(configs) == 65                    →  66
def test_aliyun_target_keeps_56_and_excludes_exact_nine()  →  ..._57_...
assert len(aliyun_ids) == 56                 →  57
```

这些断言检查的是**清单**而非**行为**，每入库一个方案必然失败一次，且修复方式只是改数字——
没有任何安全价值，纯摩擦。

改为断言不变量：矩阵覆盖与发现结果一一对应、`aliyun-gray` 恰好排除既定的 Mac-only 集合、
Blackbox 发现结果与 `delivery/` 目录集合一致。数量由被测数据推导，不写死。

## 与 harness 运行期状态问题的关系

[另一份设计](2026-08-22-harness-runtime-state-externalization.md) 处理的是 harness 向 release 树
读写的问题，其中 `_data_bridge_provenance` 是 v131 入库当前的实际阻断。

两者的优先级关系：

- 该阻断**必须先修**，否则任何方案都无法在 ECS 完成 `all`；
- 但它属于平台代码变更，走正常发布节奏，是**一次性成本**；
- 本设计处理的是**每方案重复成本**，A 与 D 实施后即长期生效。

因此顺序是：先修阻断（一次），再落 A 与 D（长期）。两者不冲突，也不应合并为一个变更单元。

## 验收

- A：新方案在矩阵中登记为 `["aliyun-gray"]` 后，ECS 完成入库全过程，Mac3 无任何动作；
  两台机器各自的 discovery 与矩阵覆盖校验均通过。
- D：新增或移除一个方案目录后，测试无需修改即通过；故意制造矩阵与方案集合不一致时测试失败。

## 停止条件

- 需要改变 Gate 判定、授权模型、独立回滚粒度或算法保真规则；
- 需要把多个方案合并为一个发布或回滚单元；
- 发现 Mac3 落后 release 会破坏其自身 discovery、矩阵覆盖或运行中的方案。
