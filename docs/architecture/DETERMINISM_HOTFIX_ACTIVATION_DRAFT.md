# Determinism Hotfix Activation（未来生效）设计草案

> 状态：**BLOCKED_DRAFT，主体已被取代**（未批准设计，不构成操作流程、不授予生产权限；未实施）
> 日期：2026-10-21（2026-10-24 精简）
> **当前方案**：全局目标文档 `/Users/macstudio0/最终提示词-20261022.md` 第 3 节 D.3 / D.3.1（Mac 全量回测 → 带审计导入 ECS → ECS 抽查；增量缓存只追加；状态行身份去架构绑定）。本文只保留可供 D.3.1 复用的设计输入。
> 关联：`harness/blackbox_v2/activation.py`、`scheduler/repository.py`、`migrations/005_lifecycle.sql`、`docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`、[MAC_ECS_PARITY_DRAFT](MAC_ECS_PARITY_DRAFT.md)

---

## 1. 原问题与当前结论

`harness activate` 的硬门禁（`harness/blackbox_v2/gates.py::verify_passed_blackbox_backtest`，2026-10-24 核对未变）：

```text
候选 exact 必须有一条与之精确匹配的成功持久化回测
scheme_version + code_hash + config_hash + manifest_hash
+ script_validator_policy_digest + runtime_profile
+ environment_fingerprint + generation_id + data_snapshot_id
```

因此任何字节变化（包括只固定排序、采样、解析、线程等确定性控制）都会生成新 exact，并要求一次完整持久化回测、一次新 release 和激活。**revision 激活本身不写 `t_scheme_predictions`**（`business_tables_written=false`），历史实盘不会被替换；要改造的是“激活证据合同”，不是历史数据。

10-21 的提议：新增显式、可选的 `determinism_hotfix` 激活路径，用全量双端确定性证据替代持久化回测。

**当前结论（2026-10-24）**：该路径未实施，被 D.3 取代——仍要求候选 exact 有匹配的成功回测，但回测在 Mac 全量完成、经带审计（哈希清单、来源记录）的导入进入 ECS，ECS 只抽查最近一段窗口并逐日比对方向；只改平台代码（exact 不变）不需要回测（全局目标文档决策 8）。Phase 1 中需要新 exact 的方案均按严格回测 + revision 激活完成。

已删除的部分：`harness hotfix-activate` 命令、`activation_policy=determinism_hotfix` 免回测路径及其事务（“不写 `t_backtest_*`”）、P0–P6 实施阶段、`five_y_factor_rule_online_v1` 的映射（该方案已于 10-21 以严格回测 + revision 激活 `bc0ef4e31a64`）。

---

## 2. 仍有效的约束

### 2.1 确定性改动准入

允许的改动只限于“未约定执行细节”（与全局目标文档决策 5 一致）：

| 类别 | 示例 |
|---|---|
| 排序/并列 | stable sort、显式二级键、阈值网格并列规则 |
| 随机/采样 | 确定性采样、RNG 生命周期、种子派生 |
| 解析/精度 | `float_precision`、dtype、NA 语义 |
| 线程/指令 | `n_jobs`、`num_threads`、CPU 派发、FMA |
| 列序/迭代 | set/dict 推导顺序、显式列序 |
| 可选依赖分支 | 强制同一实现分支、禁用 fallback |

禁止：改阈值、margin、方向定义；改模型族、超参、特征集合、融合权重；改标签、target、日历规则；改业务后处理/投票规则。只适用于已有 prior active exact 的 revision；新方案、首次激活仍走严格路径。

### 2.2 Future-only

- 新 exact 从 `effective_from` 起用于未来预测；`predict_date < effective_from` 的历史行继续属于旧 exact，不回填。
- `signal-gap-fill` 遇到早于 `effective_from` 的日期：建议拒绝，不允许用新 exact 回填历史。
- 日/周/月 runner 只跑当前日期，天然满足 future-only，无需改调度。
- 设计建议：Dashboard 显示 active exact、`effective_from`；历史行仍显示旧 `scheme_version`。

---

## 3. 可供 D.3.1 复用的设计输入

### 3.1 证据清单字段

不可变 JSON + 外部工件（receipts、JSONL、报告），按哈希引用：

| 组 | 字段 |
|---|---|
| 身份 | `scheme_id`；baseline / candidate 的 `exact`、`code_hash`、`config_hash`、`manifest_hash`；candidate 的 `runtime_profile`、`environment_fingerprint` |
| 范围 | `scope_sha256`、`case_count`、`first_date`、`last_date`、`scope_source` |
| 结果 | `hosts`、`direction_mismatches`、`max_abs_final_scalar_delta`、`tau`、重复轮次、S0 消费值是否一致及 cutoff |
| 旧新变化 | 方向变化条数、日期、业务批准 |
| 改动 | 基线 → 候选 diff 的 SHA256，逐文件说明机制与前后差异 |
| 工件 | 每个工件的 `path` + `sha256` |
| 签字 | `verified_by`、`approved_by`、`approved_at`、策略版本 |

### 3.2 平台校验项

平台不重新执行双端对照，只校验合同并保存证据哈希：

1. schema/字段完整；
2. candidate 身份与当前 canonical 重算身份逐项相等；
3. `scope_sha256`/`case_count` 与 discovery 当前 scope 相等；
4. `direction_mismatches == 0`；
5. `max_abs_final_scalar_delta <= tau`，`tau` 不超过该方案登记值（见 MAC_ECS_PARITY_DRAFT 6.3）；
6. 工件路径存在且 SHA256 匹配；diff SHA256 与实际 diff 匹配；
7. `verified_by`/`approved_by` 非空且不是同一人；
8. 证据年龄、输入 schema、环境指纹仍在允许窗口内；
9. 身份、scope、runtime、environment、输入 schema 任一不匹配直接拒绝。

### 3.3 审计记录的数据模型

- 新表（原名 `t_scheme_activation_evidence`）：方案、prior/candidate exact、`effective_from`、证据 schema 版本、`evidence_sha256`、校验后摘要 JSON（不含大工件）、外部证据路径、候选身份哈希、runtime/environment、scope、复核/批准人与时间。
- `t_scheme_versions` 可扩展 `effective_from`、证据外键；仍然每个方案只有一个 active exact，旧 exact retired，`t_scheme_predictions` 不新增列、不写历史。

### 3.4 回滚

- 用旧 exact 重新走一次激活，保留历史行；
- 新 exact 已产生的未来行按 insert-only 保留，不删除；
- 回滚也必须留下证据/操作记录。

### 3.5 涉及的代码位置

`harness/blackbox_v2/activation.py`（`_verify_passed_backtest` 是现行回测证据校验点）、`harness/cli.py`、`scheduler/repository.py`、`migrations/`、`scheduler/discovery.py`、`harness/signal_gap_fill.py`、`tests/`。

---

## 4. 待确认问题（仍相关）

1. 证据签字/信任模型：操作人 + 独立复核人是否足够？是否需要密钥签名？
2. 工件存放位置/保留期：DB 只存摘要 + 哈希，工件放外置证据目录？
3. scope 定义：由 discovery 静态准入清单生成 `scope_sha256`，生成工具与激活共用同一算法。
4. 早于 `effective_from` 的补缺：拒绝，还是允许显式旧 exact？建议拒绝。
5. 伴随方向变化的确定性修复：允许，但必须记录并业务批准。
6. 证据新鲜度：输入 schema/环境指纹变化后是否要求重新取证？
7. `production_schemes.json` 与激活保持正交。
