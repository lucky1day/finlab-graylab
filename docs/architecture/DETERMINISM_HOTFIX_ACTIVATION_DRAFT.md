# Determinism Hotfix Activation（未来生效）设计草案

> 状态：**BLOCKED_DRAFT**（未批准设计，不构成操作流程、不授予生产权限）
> 日期：2026-10-21
> 范围：仅设计，不修改生产代码、release、Registry 或数据库
> 关联：`harness/blackbox_v2/activation.py`、`scheduler/repository.py`、`migrations/005_lifecycle.sql`、`docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`

---

## 1. 问题

当前 `harness activate` 的硬门禁是：

```text
候选 exact 必须有一条与之精确匹配的成功持久化回测
scheme_version + code_hash + config_hash + manifest_hash
+ script_validator_policy_digest + runtime_profile
+ environment_fingerprint + generation_id + data_snapshot_id
```

因此任何字节变化（包括只固定排序、采样、解析、线程等确定性控制）都会：

1. 生成新 exact；
2. 要求一次完整持久化回测；
3. 要求一次新 release 和激活。

对“只影响未来实盘、历史结果保持不变”的确定性热修，这个门禁过重。

同时要澄清：**revision 激活本身不写 `t_scheme_predictions`**（`business_tables_written=False`），315 条历史实盘不会被替换。真正需要改造的是“激活证据合同”，不是历史数据。

---

## 2. 目标与非目标

### 目标

- 新增一条**显式、可选**的激活路径：`determinism_hotfix`。
- 用**全量可执行范围的双端确定性证据**替代完整持久化回测。
- 新 exact 只对 `predict_date >= effective_from` 生效；历史预测保持在旧 exact。
- 历史实盘表 `t_scheme_predictions` 不写、不改、不回填。
- 默认 `strict_backtest` 路径完全不变。

### 非目标

- 不是通用免回测通道；不适用于算法逻辑改版、新方案、首次激活。
- 不替代严格回测路径。
- 不允许修改业务规则：阈值、模型族/超参、特征集合、融合权重、标签/目标定义。
- 不做历史补缺或回填。

---

## 3. 术语与准入

### 3.1 Determinism hotfix

允许的改动只限于“未约定执行细节”：

| 类别 | 示例 |
|---|---|
| 排序/并列 | stable sort、显式二级键、阈值网格并列规则 |
| 随机/采样 | 确定性采样、RNG 生命周期、种子派生 |
| 解析/精度 | `float_precision`、dtype、NA 语义 |
| 线程/指令 | `n_jobs`、`num_threads`、CPU 派发、FMA |
| 列序/迭代 | set/dict 推导顺序、显式列序 |
| 可选依赖分支 | 强制同一实现分支、禁用 fallback |

禁止：

- 改阈值、margin、方向定义；
- 改模型族、超参、特征集合、融合权重；
- 改标签、target、日历规则；
- 改业务后处理/投票规则。

### 3.2 Future-only

- 新 exact 从 `effective_from` 起用于未来预测；
- `predict_date < effective_from` 的历史行继续属于旧 exact；
- 不做回填；`signal-gap-fill` 对早于 `effective_from` 的日期拒绝或显式要求旧 exact。

### 3.3 Evidence bundle

不可变 JSON + 外部证据工件（observer receipts、JSONL、报告）。平台只信任：

- 明确的 schema；
- 候选 exact 身份匹配；
- 全量 scope 匹配；
- 证据哈希/工件哈希匹配；
- 独立复核人签字。

---

## 4. 证据合同

Schema：`bfl-determinism-evidence-v1`

```json
{
  "schema": "bfl-determinism-evidence-v1",
  "policy": "determinism_hotfix",
  "scheme_id": "five_y_factor_rule_online_v1",
  "baseline": {
    "exact": "55f0e753b18e",
    "code_hash": "...",
    "config_hash": "...",
    "manifest_hash": "..."
  },
  "candidate": {
    "exact": "84fd449c5b02a356402f133eb574c4ca8fe9feac",
    "code_hash": "...",
    "config_hash": "...",
    "manifest_hash": "...",
    "runtime_profile": "blackbox-v2-v1",
    "environment_fingerprint": "..."
  },
  "scope": {
    "scope_sha256": "...",
    "case_count": 307,
    "first_date": "2025-07-01",
    "last_date": "2026-10-08",
    "scope_source": "discovery static admissible cases"
  },
  "determinism": {
    "hosts": ["mac-arm64", "ecs-x86_64"],
    "direction_mismatches": 0,
    "max_abs_final_scalar_delta": 1.42e-14,
    "tau": 1e-9,
    "raw_factor_bit_identical": true,
    "posterior_mismatches": 0,
    "repeat_rounds": {"eight_days": 3, "full_range": 2},
    "s0_consumed_values_identical": true,
    "s0_cutoff": "2026-10-19"
  },
  "old_vs_new": {
    "direction_changed_count": 25,
    "dates": ["2025-07-01", "..."],
    "business_approval": "..."
  },
  "hotfix_manifest": {
    "diff_sha256": "...",
    "files": [
      {
        "path": "schemes/five_y_factor_rule_online_v1/delivery/five_y_factor_rule_online_v1.py",
        "lines": "make_regressor; factor_forecast",
        "mechanism": "XGB column sampling",
        "before": "XGBRegressor(...) without feature_weights",
        "after": "XGBRegressor(..., feature_weights=np.ones(n_features))"
      }
    ]
  },
  "artifacts": [
    {"path": "...", "sha256": "..."}
  ],
  "approvals": {
    "verified_by": "...",
    "approved_by": "...",
    "approved_at": "2026-10-21T...",
    "policy_version": "1"
  }
}
```

### 4.1 平台校验项

1. schema/字段完整；
2. `candidate` 与当前 canonical 重算身份逐项相等；
3. `scope_sha256`/`case_count` 与 discovery 当前全量 scope 相等；
4. `direction_mismatches == 0`；
5. `max_abs_final_scalar_delta <= tau`；
6. `tau` 不超过该方案声明的验收上限；
7. 重复轮次不低于最低要求（8 天 ≥3；全量 ≥2）；
8. `raw_factor_bit_identical`、`posterior_mismatches == 0`（按方案适用项）；
9. `s0_consumed_values_identical == true`；
10. 工件路径存在且 SHA256 匹配；
11. `hotfix_manifest.diff_sha256` 与基线→候选的实际 diff 匹配；
12. `verified_by`/`approved_by` 非空，且不是同一人（独立复核）；
13. 证据年龄/输入 schema/环境指纹仍在允许窗口内。

> 平台不重新执行双端对照；它校验证据合同、身份、scope、阈值和签字，并保存证据哈希。

---

## 5. 数据模型

### 5.1 新表 `t_scheme_activation_evidence`

| 列 | 说明 |
|---|---|
| `id` | PK |
| `scheme_id` | 方案 |
| `policy` | `determinism_hotfix` |
| `prior_scheme_version` | 旧 active exact |
| `candidate_scheme_version` | 新 exact |
| `effective_from` | 未来生效日 |
| `evidence_schema_version` | `bfl-determinism-evidence-v1` |
| `evidence_sha256` | bundle 哈希 |
| `evidence_summary` | 校验后的摘要 JSON（不含大工件） |
| `evidence_uri` | 外部证据目录/路径 |
| `code_hash/config_hash/manifest_hash` | 候选身份 |
| `runtime_profile/environment_fingerprint` | 运行环境 |
| `input_scope_sha256/input_case_count/input_first_date/input_last_date` | scope |
| `verified_by/approved_by/approved_at` | 复核/批准 |
| `created_at` | 时间 |

### 5.2 `t_scheme_versions` 扩展

| 列 | 说明 |
|---|---|
| `activation_policy` | `ENUM('strict_backtest','determinism_hotfix')`，默认 `strict_backtest` |
| `effective_from` | `DATE NULL`；hotfix 必填 |
| `activation_evidence_id` | `BIGINT NULL`，FK → `t_scheme_activation_evidence.id` |

- 仍然每个方案只有一个 active exact；
- 旧 exact 被 retired；历史 `t_scheme_predictions` 行保留其 `scheme_version`；
- `t_scheme_predictions` 不新增列、不写历史。

---

## 6. 激活流程

### 6.1 新命令

```bash
python -B -m harness hotfix-activate \
  --scheme-id {scheme_id} \
  --evidence /path/to/evidence.json \
  --effective-from YYYY-MM-DD \
  --operator {operator}
```

- 独立命令，不改 `harness activate` 的默认严格语义；
- 仅允许 revision（必须存在 prior active exact）；
- 首次激活仍走严格回测路径。

### 6.2 前置条件

- canonical 已定稿 `status: active`、`version_status: active`；
- 候选 exact 与 prior active 不同；
- `effective_from` 由业务提供，且 `>= 今天`（禁止回填）；
- 证据合同全部校验通过；
- runtime profile/environment 与当前 release 一致；
- 输入 ready gate/generation 满足当前未来预测；
- 独立复核人/批准人齐备。

### 6.3 事务

```text
锁方案
  → 重读 preflight（prior active、pending）
  → 插入 evidence 行
  → upsert 候选版本 active（policy=determinism_hotfix, effective_from, evidence_id）
  → retire prior active（保留历史行）
  → 不写 t_scheme_predictions
  → 不写 t_backtest_*（hotfix 不产生回测证据）
  → 读回并校验
```

返回证据：

- `activation_mode=determinism_hotfix`
- `business_tables_written=false`
- `evidence_sha256`
- `effective_from`
- `prior_scheme_version` / `retired_pending_versions`

---

## 7. 运行时与补缺

- 日/周/月 runner 只跑当前日期，天然满足 future-only；无需改调度。
- `signal-gap-fill` 遇到 `predict_date < effective_from`：
  - 推荐：**拒绝**，要求显式旧 exact/严格路径；
  - 不允许用 hotfix exact 回填历史。
- Dashboard 显示 active exact、policy、effective_from；历史行仍显示旧 `scheme_version`。
- `production_schemes.json` 与激活正交；本次不改名单。

---

## 8. 代码改动点

| 位置 | 改动 |
|---|---|
| `harness/blackbox_v2/activation.py` | 新增 `hotfix_activate_blackbox`；复用 `_reload_pinned_canonical`；用 evidence 校验替代 `_verify_passed_backtest` |
| `harness/cli.py` | 注册 `hotfix-activate`；参数 `--evidence`、`--effective-from`、`--operator` |
| `scheduler/repository.py` | 新增 `activate_blackbox_determinism_hotfix`（或扩展 revision）；evidence insert/read；preflight 增加 policy/effective_from |
| `migrations/` | 新迁移：`t_scheme_activation_evidence` + `t_scheme_versions` 三列 |
| `harness/blackbox_v2/evidence.py`（新） | schema、validator、哈希、scope 校验 |
| `scripts/` 或 `harness` | evidence 生成器（只读），输出 bundle + artifacts 哈希 |
| `scheduler/discovery.py` | 暴露 `activation_policy`/`effective_from` |
| `harness/signal_gap_fill.py` | 拒绝早于 `effective_from` 的 hotfix 日期 |
| `tests/` | evidence 合同、身份/scope/τ 校验、事务、无业务表写入、effective_from 守卫、回滚 |

---

## 9. 安全与回滚

- 默认严格路径不变；hotfix 需要显式命令 + 独立复核 + 批准。
- 证据不可变、按哈希引用；工件保留在外部证据目录。
- 候选身份、scope、runtime、environment、输入 schema 不匹配直接拒绝。
- `effective_from >= 今天`，禁止回填。
- 回滚：
  - 用旧 exact 重新走一次激活（严格或 hotfix），保留历史行；
  - 新 exact 已产生的未来行按 insert-only 保留，不删除；
  - 回滚也必须留下证据/操作记录。

---

## 10. 实施阶段

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | 设计评审（本文） | 批准/修改意见 |
| P1 | 迁移 + schema | 新表/列 + sqlite/MySQL 测试 |
| P2 | evidence schema/validator + 生成器 | 只读证据工具 + 测试 |
| P3 | hotfix 激活事务 | repository/harness + 测试 |
| P4 | discovery/补缺守卫 | effective_from 行为 + 测试 |
| P5 | 本方案 dry-run | 用已有 307 天证据，不写生产 |
| P6 | 生产上线（另行授权） | 用 hotfix 路径激活本方案 |

---

## 11. 与本方案的映射

| 项 | 值 |
|---|---|
| 候选 release | `84fd449c5b02a356402f133eb574c4ca8fe9feac`（A2 补丁 + active config） |
| Archive SHA | `2b658da44ede96e3aeabdd040353376e8763a123d2baa4f33a3bc4d2d091f142` |
| 旧 active exact | `55f0e753b18e` |
| 历史实盘 | 315 条，2025-07-01 ～ 2026-10-20，保留不动 |
| 证据 | 307/307 方向一致；max Δ 1.42e-14；raw_factor 0；posterior 0；8 天 3 轮；全量 2 轮；S0 消费值到 2026-10-19 一致 |
| 旧新变化 | 25/307 方向变化，需业务批准并记录 |
| effective_from | 2026-10-21（今天；旧实盘保留到 10-20） |
| 激活后 | 新 exact 只为未来预测生效；不写历史；不产生新回测 run |

---

## 12. 待确认问题

1. 证据签字/信任模型：操作人 + 独立复核人是否足够？是否需要密钥签名？
2. 证据工件存放位置/保留期：DB 只存摘要 + 哈希，工件放外置证据目录？
3. τ 策略：每方案声明上限，平台校验不超过该上限；是否还需平台全局上限？
4. scope 定义：由 discovery 静态准入清单生成 `scope_sha256`，证据工具与激活共用同一算法。
5. 早于 `effective_from` 的补缺：拒绝，还是允许显式旧 exact？建议拒绝。
6. 是否允许 hotfix 伴随行为变化（本方案 25/307）：允许，但必须记录并业务批准。
7. 证据新鲜度：输入 schema/环境指纹变化后是否要求重新取证？
8. `production_schemes.json` 是否需要在激活时同步？建议保持正交。
