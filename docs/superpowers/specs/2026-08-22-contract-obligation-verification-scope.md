# 契约义务的验证范围与频率设计

## 背景

`docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md` 是上游算法工程师**唯一需要阅读的文档**，其中已明文
要求上游保证以下性质：

| 契约条款 | 要求 |
|---|---|
| 第 472 行 | 任一 Request 在不同**批次大小、分区边界或输入顺序**下都必须得到相同结果 |
| 第 614 行 | 抽样选取必须确定性，不得使用随机数，以免破坏**重复执行一致性** |
| 第 600–604 行 | 保留第一行至**截止键所在行**；不得用文件最后一行代替截止键 |
| 第 616 行 | 重申批次大小/切分/顺序不变量 |

CompareGate 的 6 条断言中，`repeat_deterministic`、`batch_split_invariant`、
`request_order_invariant`、`future_row_isolation` 共 4 条正是对上述契约条款的重新验证；
`predict_backtest_equal` 是同一组契约的合理推论。只有 `platform_input_hashes_unchanged`
属于平台自检。

实测成本（`weekly_1y_causal_v1_31_0_standalone`，2026-08-22）：

```
dry-run     1 次全量拟合    239s   → 抓到真实缺陷（predict 无条件崩溃）
compare    10 次全量拟合   2400s   → 6 条断言全过，0 命中
```

当日 `all` 共运行 4 轮，而交付字节只变化 1 次。即该套不变量对**完全相同的字节**验证了 4 遍，
其中 3 遍为纯重复，约 2 小时。

## 第一性判断

检查当前按「运行」计费，而不是按「被验证的对象」计费。缺少的是一条约束：

> 每个检查必须回答两个问题——**它验证的义务归谁？多久需要重验一次？**

没有这条约束，检查只会累加：每一条单独看都合理，但没人核对它是否已被契约覆盖、是否每轮都值得重跑。

契约已明文要求的性质是**代码的结构性质**：交付字节不变，这些性质不可能改变。因此重验频率应
绑定交付内容指纹，而不是绑定 `all` 的运行次数。

## 目标

在不降低任一断言强度的前提下，把 `all` 的重复运行成本与交付变更解耦。

不改变：任何断言的判定逻辑、失败条件与错误文案；fail-closed 语义；持久化回测、激活与 live 的
授权要求；上游契约本身。

## 最小设计

### 1. 缓存键：复用既有交付哈希，不引入新身份

`scheduler/discovery.py` 对 Blackbox 已经计算并记录：

```python
code_hash     = _hash_file(script_path)     # delivery/{scheme_id}.py 的 SHA-256
manifest_hash = _hash_file(metadata_path)   # delivery/{scheme_id}.json 的 SHA-256
```

两者精确覆盖交付两文件，因此 `(code_hash, manifest_hash)` 直接作为缓存键，**不新增任何身份概念**。

> **更正记录（2026-08-22）**：本节初稿曾断言「Blackbox 的 `code_hash` 恒为空字符串 SHA-256，
> `scheme_version` 完全不反映交付字节」，并据此提出新增 `delivery_digest`。该结论来自直接调用
> `shared.versioning.compute_code_hash`——但生产路径不走该函数。实测
> `code_hash == sha256(delivery/{scheme_id}.py)`，交付字节**本来就**参与 `scheme_version`。
> 不存在追溯缺口，`delivery_digest` 提案随之取消。

### 2. 把 `all` 的自动段分成两层

**每轮必跑（平台侧，与交付是否变化无关）：**

```
static → input → unit → smoke → platform-self-check
```

- `smoke`：一次 `predict`，验证交付在当前输入下能否产出合法 Result。这不是重验契约，是
  「东西能不能用」。今日证据表明它是唯一命中真实缺陷的算法段。
- `platform-self-check`：`platform_input_hashes_unchanged`，以及平台截断是否真的生效。

**指纹变更才跑（契约义务重验）：**

```
contract-invariants: repeat_deterministic / predict_backtest_equal
                     batch_split_invariant / request_order_invariant
                     future_row_isolation
```

### 3. 缓存查找，无需 DDL

CompareGate 在 evidence 中输出 `code_hash` 与 `manifest_hash`。`t_harness_gate_results.summary_json` 是 JSON
列且完整保存 evidence，因此后续运行可查询「是否存在同一 `scheme_id` + 同一 `(code_hash, manifest_hash)` 且
`status='passed'` 的 compare 结果」。命中则跳过并在本次 evidence 中记录
`contract_invariants_reused_from`（被复用的 `harness_run_id`）。

未命中、查询失败、digest 无法计算或存在任何歧义时，**一律执行完整不变量套件**（fail-closed）。

### 4. dry-run 与 no-persist backtest 的归并

- `dry-run` 与 CompareGate 的 `baseline` 是逐参数相同的同一次 `predict` 调用。归并为上文的
  `smoke`，并由其输出既有的 `prediction_record` 证据与 `dry_run_prediction_record.json`。
- `no-persist backtest` 的 `batch_split_invariant` 与 CompareGate 的 `unsplit`/`split`
  （`batch=100` vs `batch=1`）是同一命题，且 CompareGate 版本更强、更便宜。将其
  `subprocesses_started` / `max_subprocesses` / `total_deadline_sec` 证据移至 CompareGate 的
  `split` 调用上断言。
- `gate dry-run` 与 `gate backtest` 作为**独立命令保留**，不从 CLI 移除。
- **`backtest --persist` 完全不动**：它是产出 `t_backtest_*` 历史复现数据的本职路径，
  属独立授权步骤，与本设计无关。

## 预期成本变化

```
现状                     19 次拟合   ≈ 76 分钟   （每轮）
交付字节变化时           10 次拟合   ≈ 40 分钟
交付字节未变时            1 次拟合   ≈  5 分钟
```

当日 4 轮的实际形态将变为：1 轮全量 + 3 轮轻量，约 2 小时降至约 55 分钟。

## 需要同步更新的文档

- `CLAUDE.md` 与 `AGENTS.md`（两者必须逐字一致）中的自动段描述
  `static → input → unit → dry-run → compare → backtest`；
- `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md` 的平台操作序列；
- `docs/blackbox_v2/PRODUCTION_READINESS.md` 第 2 条对「六段 `all`」的表述；
- `docs/architecture/HARNESS_ARCHITECTURE.md` 中的 Gate 编排说明。

上游契约 `BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md` **不改**——本设计不改变对上游的任何要求。

## 测试与验收

1. 每条断言的失败路径逐条保持：构造违反 determinism / batch / order / cutoff isolation 的交付，
   错误文案与判定不变。
2. 指纹命中时跳过不变量套件，且 evidence 记录被复用的 `harness_run_id`；交付字节改动一个字节
   即不命中。
3. 缓存查询失败、哈希不可计算或记录歧义时执行完整套件（fail-closed），并有对应用例。
4. `smoke` 产出的 `prediction_record` 与原 dry-run 逐字段一致。
5. `gate dry-run`、`gate backtest` 单独调用行为不变。
6. `backtest --persist` 路径完全不受影响。
7. 全量回归对照纯净 HEAD 无新增失败。

## 停止条件

- 需要削弱任一断言的判定逻辑或失败条件；
- 需要 DDL 才能承载缓存键；
- 缓存命中判定存在任何无法 fail-closed 的歧义；
- 需要修改上游契约对交付的要求。
