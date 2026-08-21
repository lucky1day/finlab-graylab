# Harness 运行期状态外置实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 harness 在不可变 release 上运行时不再向 `project_root` 读写运行期状态，解除
`weekly_1y_causal_v1_31_0_standalone` 的 InputGate 阻断，并用一条可在 Mac 开发工作树执行的断言，
使这一整类问题在上机之前就暴露。同时解除测试与方案清单的耦合。

**Architecture:** 复用既有 `shared/runtime_paths.resolve_runtime_state_path()`，不新增第二处路径
解析实现。所有 `development_default` 与当前实现逐字节等价，因此开发工作树行为不变。核心交付是
Task 1 的「生产形状」断言——它设定生产 target 与 runtime root 后执行真实 Gate，并比对
`project_root` 整树 digest，因而能覆盖静态扫描抓不到的运行时写入。

**Tech Stack:** Python 3.12、pytest/unittest、既有 harness Gate、immutable source release、systemd。

**范围外（不在本计划内）：** `schemes/{id}/config.yaml` 的 `status` 改写（Native `_set_status` 与
Blackbox `perform_lifecycle_transition`）、`resolve_runtime_artifact_root()` 的 macOS 约定、
方案内容移出源码树。三者各需独立设计。

---

### Task 1: 建立「生产形状」断言

**Files:**
- Create: `tests/test_harness_release_immutability.py`

- [ ] **Step 1: 写整树 digest 辅助**

在测试内实现与 `scripts/install_source_release._source_tree_sha256` 同语义的整树摘要：遍历全部文件、
按相对路径排序、记录 `path/executable/sha256`、canonical JSON 后取 SHA-256。**不 import 安装器**，
避免测试与发布工具耦合。

- [ ] **Step 2: 写 StaticGate 生产形状用例（决定性 RED）**

StaticGate 不需要数据库、不执行算法，但会写报告，因此是最干净的载体：

```python
project_root = copy_repo_to(tmp / "release")      # 模拟 release 树
runtime_root = tmp / "state"
env = {"BFL_DEPLOYMENT_TARGET": "aliyun-gray", "BFL_RUNTIME_ROOT": str(runtime_root)}

before = tree_digest(project_root)
run_gate("static", scheme_id=..., project_root=project_root, env=env)   # 不传 --report-dir
after = tree_digest(project_root)

assert after == before                    # release 树零副作用
assert report_written_under(runtime_root) # 报告落在外置 runtime root
```

今天必然失败：`report_dir` 默认是 `project_root/reports/harness/{scheme_id}/{ts}`。

- [ ] **Step 3: 写三个路径点的单元断言**

对 `used_tokens_path()`、backtest baseline、`signal-gap-fill` report root 分别断言：

```python
# 生产：落在 runtime root 下
with env(BFL_DEPLOYMENT_TARGET="aliyun-gray", BFL_RUNTIME_ROOT=str(rt)):
    assert used_tokens_path(project_root).is_relative_to(rt)

# 仅设 target 不设 runtime root：fail-closed，且无任何文件系统副作用
with env(BFL_DEPLOYMENT_TARGET="aliyun-gray"):
    with pytest.raises(RuntimeError):
        used_tokens_path(project_root)
    assert tree_digest(project_root) == before

# 开发（两者皆不设）：与改动前逐字节一致
assert used_tokens_path(project_root) == project_root / "reports" / "harness" / ".used_authorization_tokens.json"
```

- [ ] **Step 4: 写跨 release 重放保护用例**

```python
mark_token_used(auth, used_tokens_path(release_a))     # runtime root 固定
with pytest.raises(...):                                # 换一个 project_root 仍须拒绝
    verify_authorization(auth, used_store_path=used_tokens_path(release_b))
```

- [ ] **Step 5: 运行 RED，记录失败清单**

Run: `python -m pytest tests/test_harness_release_immutability.py -v`

**这一步的产出不是绿灯，是清单。** 记录所有失败项——其中可能包含本计划未预见的写点。若出现
未列出的写点，先补充计划再继续，不得就地扩大改动范围。

---

### Task 2: 修 `report_dir` 默认值

**Files:**
- Modify: `harness/cli.py`

- [ ] **Step 1: 三处默认值改用 resolver**

`gate` / `onboard` / `activate`（约 338 / 389 / 423 行）的默认值改为：

```python
report_dir = args.report_dir or resolve_runtime_state_path(
    relative_path=f"reports/harness/{args.scheme_id}/{_timestamp()}",
    development_default=project_root / "reports" / "harness" / args.scheme_id / _timestamp(),
)
```

保留 `--report-dir` 参数不变。

- [ ] **Step 2: 验证 Task 1 Step 2 转绿**

Run: `python -m pytest tests/test_harness_release_immutability.py -k static -v`

---

### Task 3: 修 DataBridge provenance 读路径（当前实际阻断）

**Files:**
- Modify: `harness/blackbox_v2/gates.py`

- [ ] **Step 1: 写 RED**

构造 runtime root 下的 `data-bridge/refresh/state.json`，且 `project_root/backtest_artifacts/`
不存在，断言 `_data_bridge_provenance` 能读到前者。今天会抛
`Blackbox all-stage requires readable DataBridge provenance`。

- [ ] **Step 2: 改用 canonical 入口**

删除硬编码的 `ctx.project_root / "backtest_artifacts" / "data_bridge_refresh" / "state.json"`，
改为经 `DataBridgeRefreshConfig.from_env().state_path` 取得。**不新增第二处路径解析**，
不改变 provenance 的校验字段（`generation_id`/`refresh_date`/`refreshed_at`/`business_digest`）
与判定逻辑。

- [ ] **Step 3: 验证**

Run: `python -m pytest tests/ -k data_bridge -v`

---

### Task 4: 修授权重放存储路径

**Files:**
- Modify: `harness/authorization.py`

- [ ] **Step 1: `used_tokens_path()` 改用 resolver**

```python
def used_tokens_path(project_root: Path) -> Path:
    return resolve_runtime_state_path(
        relative_path="reports/harness/.used_authorization_tokens.json",
        development_default=project_root / "reports" / "harness" / ".used_authorization_tokens.json",
    )
```

19 个调用点签名不变，无需逐个修改。

- [ ] **Step 2: 验证 Task 1 Step 3、Step 4 转绿**

Run: `python -m pytest tests/test_harness_release_immutability.py -k "token or replay" -v`

---

### Task 5: 修 backtest baseline 与 signal-gap-fill report root

**Files:**
- Modify: `harness/gates/backtest_gate.py`
- Modify: `harness/cli.py`

- [ ] **Step 1: baseline 路径改用 resolver**

`relative_path=f"reports/refactor_baseline/{ctx.scheme_id}/backtest_no_persist.json"`，
`development_default` 保持原路径。自举写入逻辑与判定不变。

- [ ] **Step 2: `signal-gap-fill` report root 改用 resolver**

`relative_path="reports/harness/signal-gap-fill"`，`development_default` 保持原路径。
`tempfile.mkdtemp(dir=report_root)` 逻辑不变。

- [ ] **Step 3: 验证**

Run: `python -m pytest tests/test_harness_release_immutability.py -v`

**全部转绿是本计划前半段的完成条件。**

---

### Task 6: 解除测试与方案清单的耦合

**Files:**
- Modify: `tests/test_blackbox_v2_discovery.py`
- Modify: `tests/test_deployment_scope.py`

- [ ] **Step 1: 用不变量替换写死计数**

移除 `len(...) == 39/40`、`== 65/66`、`== 56/57` 一类断言，以及函数名中的数字
（`test_aliyun_target_keeps_56_and_excludes_exact_nine`）。改为断言：

- 部署矩阵的 scheme 集合与 discovery 结果**完全一致**（既不多也不少）；
- `aliyun-gray` 过滤后的集合 == 全集减去矩阵中标记为仅 `mac3-production` 的集合；
- Blackbox 发现结果与 `schemes/*/delivery/` 目录集合一致。

数量一律从被测数据推导，不出现字面量。

- [ ] **Step 2: 双向验证**

Run: `python -m pytest tests/test_blackbox_v2_discovery.py tests/test_deployment_scope.py -v`

再手工制造一次矩阵与方案集合不一致（临时增删一个方案目录），确认测试**失败**；恢复后确认通过。
只有同时满足「新增方案无需改测试」和「不一致必然失败」，本任务才算完成。

---

### Task 7: 全量回归与发布

- [ ] **Step 1: 本机全量回归**

Run: `python -m pytest tests/ -q`

- [ ] **Step 2: 复审**

REQUIRED SUB-SKILL: `superpowers:requesting-code-review`。重点核对：所有
`development_default` 与改动前逐字节等价、无第二处路径解析、Gate 判定与授权语义未变。

- [ ] **Step 3: 从精确提交构建 release**

clean HEAD → `scripts/build_source_release.py` → 记录 commit、archive SHA-256、source tree SHA-256。
独立构建两次确认字节一致。

- [ ] **Step 4: 晋级 ECS 并现场验收**

预安装 → CAS → 仅重启 Backend。随后：

- 重算 `source_tree_sha256`，与 `.bfl-release-install.json` 精确一致；
- 执行一次受控 harness 运行，**运行后再次重算 digest 仍须一致**（这是本计划的核心验收）；
- 重跑 `weekly_1y_causal_v1_31_0_standalone` 的 `all`，确认已越过 InputGate。

- [ ] **Step 5: Mac3 晋级判定**

本计划属于平台变更，按止损规则**应当晋级两端**，使用 ECS 已验证的同一份 archive。
晋级前需确认：Mac3 当前是否从生产 release 执行过 `signal-gap-fill`（该命令是 Mac3 唯一的暴露点）。
Mac3 晋级是独立生产操作，须单独授权，不由本计划推断。

---

## 停止条件

- Task 1 Step 5 暴露出本计划未列出的写点 → 先补充计划，不得就地扩大范围；
- 需要改变 Gate 判定、授权语义、数据库 schema 或 Registry 生命周期；
- 需要 fallback、双读或隐式兼容才能让测试通过；
- 越过 InputGate 后的 unit / dry-run / compare / backtest 暴露新的不可变性冲突 → 单独立项，
  不并入本计划。
