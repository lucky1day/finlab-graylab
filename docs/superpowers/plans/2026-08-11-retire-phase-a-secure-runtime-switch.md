# Phase-A Secure Runtime Switch Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 Phase-A 缓存中无法启用的 `secure_runtime`、mutation-only helper 的死 `secure` 分支和无用内部参数，同时保持 consumer 安全读取、缓存格式和运行结果不变。

**Architecture:** `prepare_phase_a_caches()` 只保留两条固定路径：正式 publisher 或 `private_build` 组成的 `can_mutate_cache` 路径可以在独占锁内构建和发布；普通 consumer 只能通过 `secure=True` 的共享 reader 读取 current 并验证 lineage。仅写 helper 不再保留不可达安全模式，共享 reader 的安全模式完整保留。

**Tech Stack:** Python 3.12、pandas、pytest、`unittest.mock`、本地不可变 Phase-A generation cache。

---

## 文件范围

- Modify: `shared/liwei_0616_phase_a_cache.py` — 删除假控制面、精确命名可写路径、保留共享 reader 安全模式。
- Modify: `tests/test_liwei_0616_private_cache.py` — 先增加 consumer characterization，再同步内部参数断言。
- Delete after completion: `docs/superpowers/specs/2026-08-11-retire-phase-a-secure-runtime-switch-design.md` — 已实施设计不留在当前文档树。
- Delete after completion: `docs/superpowers/plans/2026-08-11-retire-phase-a-secure-runtime-switch.md` — 已实施计划由 Git 历史追溯。

不修改 `shared/liwei_0616_cache_contract.py`、任何 `schemes/` 文件、`docs/CURRENT_STATUS.md`、Harness、数据库、Registry、plist、Backend 或前端。

### Task 1: 冻结并补齐 consumer 安全读取 characterization

**Files:**
- Modify: `tests/test_liwei_0616_private_cache.py:114-315`
- Test: `tests/test_liwei_0616_private_cache.py`

- [x] **Step 1: 核对工作树并冻结现有 GREEN 基线**

Run:

```bash
git status --short --branch
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_liwei_0616_private_cache.py
```

Expected:

```text
当前分支为 codex/audit-bugfixes-20260613，仅领先尚未推送的设计/计划提交
6 passed
```

- [x] **Step 2: 增加修改前即可通过的 consumer characterization test**

在 `tests/test_liwei_0616_private_cache.py` 的 private-build 测试之后增加：

```python
def test_consumer_securely_reads_current_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(CACHE_MUTATION_POLICY_ENV, raising=False)
    root = tmp_path.resolve()
    root.chmod(0o700)
    daily = pd.DataFrame(
        {"date": ["2026-01-02"], "close": [2.0]}
    )
    weekly = pd.DataFrame(
        {"week_id": [202601], "value": [1.0]}
    )
    monthly = pd.DataFrame(
        {"month_id": ["2026-01"], "value": [1.0]}
    )
    spec = PhaseACacheSpec(
        cache_family="test_consumer_secure",
        tenor="5Y",
        publisher_consumer_id="publisher",
        baselines=("baseline",),
        baseline_configs={"baseline": {"close": "close"}},
        source_ic_screen_start="2020-01-01",
        horizon=5,
        purge_gap=5,
    )
    cache = {
        "test_dates": ["2026-01-02"],
        "results": [
            {
                "config": {"name": "baseline"},
                "preds": np.asarray([1], dtype=np.int32),
                "probs": np.asarray([0.75], dtype=np.float64),
            }
        ],
    }
    train_calls: list[str] = []

    def train_missing(
        baseline: str,
        _ranges: tuple[tuple[str, str], ...],
    ) -> dict[str, object]:
        train_calls.append(baseline)
        return cache

    _publisher_caches, publisher_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=(("2026-01-02", "2026-01-02"),),
        train_missing=train_missing,
        cache_consumer_id="publisher",
        cache_root=root,
    )
    assert publisher_audit["status"] == "cold_build"
    assert train_calls == ["baseline"]

    _consumer_caches, consumer_audit = prepare_phase_a_caches(
        spec=spec,
        daily_df=daily,
        weekly_df=weekly,
        monthly_df=monthly,
        test_ranges=(("2026-01-02", "2026-01-02"),),
        train_missing=train_missing,
        cache_consumer_id="consumer",
        cache_root=root,
    )
    assert consumer_audit["status"] == "hit"
    assert consumer_audit["build_reason"] == "consumer_validated_hit"
    assert train_calls == ["baseline"]

    current_path = (
        root
        / spec.cache_family
        / spec.tenor.lower()
        / "current.json"
    )
    external_pointer = root / "external-current.json"
    external_pointer.write_bytes(current_path.read_bytes())
    current_path.unlink()
    current_path.symlink_to(external_pointer)

    with pytest.raises(RuntimeError, match="CACHE_PUBLISHER_REQUIRED"):
        prepare_phase_a_caches(
            spec=spec,
            daily_df=daily,
            weekly_df=weekly,
            monthly_df=monthly,
            test_ranges=(("2026-01-02", "2026-01-02"),),
            train_missing=train_missing,
            cache_consumer_id="consumer",
            cache_root=root,
        )
```

该测试使用内容合法的 symlink pointer：普通文件读取会跟随并成功，只有现有 secure reader 会拒绝，因此它能够真实锁定 `secure=True`，而不是只测试 JSON 损坏。

- [x] **Step 3: 运行 characterization 并确认修改前仍为 GREEN**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_liwei_0616_private_cache.py
```

Expected:

```text
7 passed
```

- [x] **Step 4: 提交 characterization**

```bash
git add tests/test_liwei_0616_private_cache.py \
  docs/superpowers/plans/2026-08-11-retire-phase-a-secure-runtime-switch.md
git diff --cached --check
git commit -m "test(cache): characterize phase-a consumer safety"
```

### Task 2: 删除上层假控制面并修正可写路径术语

**Files:**
- Modify: `shared/liwei_0616_phase_a_cache.py:226-689`
- Modify: `tests/test_liwei_0616_private_cache.py:114-155`
- Test: `tests/test_liwei_0616_private_cache.py`

- [x] **Step 1: 将 `is_publisher` 精确改为 `can_mutate_cache`**

对 `prepare_phase_a_caches()` 和 `_prepare_under_family_lock()` 应用以下语义等价变更：

```diff
-    is_publisher = (
+    can_mutate_cache = (
         cache_consumer_id == spec.publisher_consumer_id
         or mutation_policy == CACHE_MUTATION_POLICY_PRIVATE_BUILD
     )
-    if not is_publisher:
+    if not can_mutate_cache:
         return _prepare_under_family_lock(
             spec=spec,
             cache_consumer_id=cache_consumer_id,
-            is_publisher=False,
-            root=root,
+            can_mutate_cache=False,
             family_root=family_root,
```

锁内 mutation-only helper 的参数在 Task 3 一次性删除，因此本任务暂时保留其当前显式 false：

```python
_cleanup_staging_directories(
    family_root,
    secure=False,
)
```

内部准备调用改为真实能力名称：

```diff
         return _prepare_under_family_lock(
             spec=spec,
             cache_consumer_id=cache_consumer_id,
-            is_publisher=is_publisher,
-            root=root,
+            can_mutate_cache=can_mutate_cache,
             family_root=family_root,
```

- [x] **Step 2: 从 `_prepare_under_family_lock()` 删除 `root` 与 `secure_runtime`**

签名和 consumer 分派改为：

```diff
 def _prepare_under_family_lock(
     *,
     spec: PhaseACacheSpec,
     cache_consumer_id: str,
-    is_publisher: bool,
-    root: Path,
+    can_mutate_cache: bool,
     family_root: Path,
@@
-    secure_runtime: bool,
 ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
@@
     current, current_error = _load_current_generation(
         family_root,
-        secure=secure_runtime or not is_publisher,
+        secure=not can_mutate_cache,
     )
@@
-    if not is_publisher:
+    if not can_mutate_cache:
         return _validated_consumer_hit(
             spec=spec,
             cache_consumer_id=cache_consumer_id,
             current=current,
             current_error=current_error,
             requested_by_baseline=requested_by_baseline,
             input_state=input_state,
             input_change=input_change,
             family_root=family_root,
-            secure_runtime=secure_runtime,
         )
```

同时删除两个 `_prepare_under_family_lock()` 调用中的：

```python
secure_runtime=False,
```

- [x] **Step 3: 删除截断输入的不可达分支并固定 mutation helper 的中间态参数**

删除以下整个不可达块：

```python
if secure_runtime:
    _verify_generation_acceptance_lineage(
        current,
        spec=spec,
    )
```

在 Task 3 删除 mutation-only helper 签名前，其调用固定使用当前真实值：

```python
secure=False,
```

- [x] **Step 4: 删除 `_validated_consumer_hit()` 的未使用参数**

```diff
 def _validated_consumer_hit(
     *,
     spec: PhaseACacheSpec,
     cache_consumer_id: str,
     current: _LoadedGeneration | None,
     current_error: str | None,
     requested_by_baseline: Mapping[str, list[str]],
     input_state: Mapping[str, Any],
     input_change: Mapping[str, Any],
     family_root: Path,
-    secure_runtime: bool,
 ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
```

函数体的无 current、输入等价、覆盖范围和 `_verify_generation_acceptance_lineage()` 逻辑保持原字节不动。

- [x] **Step 5: 同步 private-build 内部契约测试**

```diff
     assert actual == expected
-    assert prepare.call_args.kwargs["is_publisher"] is True
+    assert prepare.call_args.kwargs["can_mutate_cache"] is True
     assert prepare.call_args.kwargs["cache_consumer_id"] == (
         "ordinary_consumer"
     )
-    assert prepare.call_args.kwargs["root"] == root
+    assert "root" not in prepare.call_args.kwargs
```

- [x] **Step 6: 运行聚焦测试**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_liwei_0616_private_cache.py
```

Expected:

```text
7 passed
```

- [x] **Step 7: 提交上层控制面清理**

```bash
git add shared/liwei_0616_phase_a_cache.py \
  tests/test_liwei_0616_private_cache.py \
  docs/superpowers/plans/2026-08-11-retire-phase-a-secure-runtime-switch.md
git diff --cached --check
git commit -m "refactor(cache): remove phase-a secure runtime switch"
```

### Task 3: 删除 mutation-only helper 的不可达安全分支

**Files:**
- Modify: `shared/liwei_0616_phase_a_cache.py:1917-2181,3704-3995`
- Test: `tests/test_liwei_0616_private_cache.py`

- [x] **Step 1: 收敛 generation 创建与 staged validation**

从 `_create_generation()` 签名删除：

```python
secure: bool,
```

删除该函数内仅由 `secure` 控制的 `family_identity`、`generation_root_identity`、目标预检查、目录 identity 复核和发布后目录复核分支。保留以下固定流程和顺序：

```python
family_root.mkdir(parents=True, exist_ok=True)
staging = Path(
    tempfile.mkdtemp(
        dir=family_root,
        prefix=".building-",
    )
)
```

staged generation 继续完整校验，但不再传入不可达参数：

```diff
-        validated = _validate_staged_generation(
-            staging,
-            secure=secure,
-        )
+        validated = _validate_staged_generation(staging)
```

generation publication 保留为：

```python
generation_root = family_root / "generations"
generation_root.mkdir(exist_ok=True)
target = generation_root / generation_id
os.replace(staging, target)
_fsync_directory(generation_root)
finalized = True
```

将 staged validator 收敛为：

```python
def _validate_staged_generation(
    staging: Path,
) -> _LoadedGeneration:
    return _load_generation_directory(staging)
```

- [x] **Step 2: 收敛 current pointer 原子切换**

从 `_switch_current_generation()` 删除 `secure` 参数以及所有 `if secure:` 分支。以下 publication commit point 必须保持原顺序：

```python
pointer_path = family_root / "current.json"
pointer_path.parent.mkdir(parents=True, exist_ok=True)
encoded = (
    _canonical_json(
        {
            "schema_version": CURRENT_POINTER_SCHEMA_VERSION,
            "generation_id": generation.generation_id,
            "manifest_sha256": generation.manifest_sha256,
            "switched_at": _utc_now(),
        }
    )
    + "\n"
).encode("utf-8")
temp_path: Path | None = None
try:
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=pointer_path.parent,
        prefix=f".{pointer_path.name}.",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    json.loads(temp_path.read_text(encoding="utf-8"))
    os.replace(temp_path, pointer_path)
    temp_path = None
finally:
    if temp_path is not None:
        temp_path.unlink(missing_ok=True)

try:
    _fsync_directory(pointer_path.parent)
except OSError:
    return
```

保留现有注释：`os.replace()` 是唯一 publication commit point，replace 后的目录 fsync 失败不能向调用方报告发布失败。

- [x] **Step 3: 收敛 staging cleanup**

替换为：

```python
def _cleanup_staging_directories(family_root: Path) -> None:
    for path in family_root.glob(".building-*"):
        if path.is_dir():
            shutil.rmtree(path)
```

- [x] **Step 4: 收敛未发布 candidate 清理**

函数签名改为：

```python
def _discard_unpublished_generation(
    family_root: Path,
    generation_id: str,
) -> None:
```

pointer 读取固定为当前实际路径：

```python
pointer_path = family_root / "current.json"
if pointer_path.exists():
    try:
        pointer_bytes = pointer_path.read_bytes()
        pointer = json.loads(pointer_bytes.decode("utf-8"))
        current_generation_id = str(
            pointer.get("generation_id") or ""
        )
    except (OSError, AttributeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "cannot prove cache candidate is unpublished"
        ) from error
    if current_generation_id == generation_id:
        raise RuntimeError(
            "refusing to delete the published current generation"
        )
```

candidate 删除固定保留为：

```python
generation_root = family_root / "generations"
candidate = generation_root / generation_id
if candidate.exists():
    shutil.rmtree(candidate)
    _fsync_directory(generation_root)
```

- [x] **Step 5: 收敛 generation prune**

从 `_prune_generations()` 删除 `secure` 参数以及两个 `if secure:` 目录检查块。以下保护规则必须保持不变：

```python
protected = {
    str(generation_id)
    for generation_id in protected_generation_ids
    if str(generation_id)
}
if not protected:
    raise ValueError(
        "cache generation pruning requires protected generations"
    )
```

保留现有 retention、family byte limit、protected generation 排除、`CacheCapacityError` 和删除后 `_fsync_directory(generation_root)` 逻辑。

- [x] **Step 6: 运行聚焦测试**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_liwei_0616_private_cache.py
```

Expected:

```text
7 passed
```

- [x] **Step 7: 运行静态边界验证**

Run:

```bash
python - <<'PY'
import ast
from pathlib import Path

path = Path("shared/liwei_0616_phase_a_cache.py")
source = path.read_text(encoding="utf-8")
tree = ast.parse(source)
functions = {
    node.name: node
    for node in tree.body
    if isinstance(node, ast.FunctionDef)
}

assert "secure_runtime" not in source
assert "is_publisher" not in source

prepare_args = {
    item.arg
    for item in functions["_prepare_under_family_lock"].args.kwonlyargs
}
assert "can_mutate_cache" in prepare_args
assert "root" not in prepare_args

mutation_helpers = {
    "_create_generation",
    "_validate_staged_generation",
    "_cleanup_staging_directories",
    "_discard_unpublished_generation",
    "_prune_generations",
    "_switch_current_generation",
}
for name in mutation_helpers:
    arguments = {
        item.arg
        for item in functions[name].args.kwonlyargs
    }
    assert "secure" not in arguments, name

for name in ("_load_current_generation", "_load_generation_directory"):
    arguments = {
        item.arg
        for item in functions[name].args.kwonlyargs
    }
    assert "secure" in arguments, name
PY
```

Expected: exit code `0`，无输出。

- [x] **Step 8: 提交 mutation-only helper 清理**

```bash
git add shared/liwei_0616_phase_a_cache.py \
  docs/superpowers/plans/2026-08-11-retire-phase-a-secure-runtime-switch.md
git diff --cached --check
git commit -m "refactor(cache): remove unreachable mutation security branches"
```

### Task 4: 全量验证并清理已实施设计/计划

**Files:**
- Delete: `docs/superpowers/specs/2026-08-11-retire-phase-a-secure-runtime-switch-design.md`
- Delete: `docs/superpowers/plans/2026-08-11-retire-phase-a-secure-runtime-switch.md`
- Test: repository-wide pytest suite

- [x] **Step 1: 运行最终聚焦与全量回归**

Run:

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q tests/test_liwei_0616_private_cache.py
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest -q
```

Expected:

```text
Phase-A: 7 passed
Full suite: 614 passed, 393 subtests passed, 0 failed
```

现有 SQLAlchemy/SQLite deprecation warning 可以保留；不得新增资源泄漏、未关闭连接或本模块 warning。

- [x] **Step 2: 核对 diff 与静态引用**

Run:

```bash
git diff --check
rg -n "secure_runtime|\bis_publisher\b" \
  shared/liwei_0616_phase_a_cache.py \
  tests/test_liwei_0616_private_cache.py
git status --short
```

Expected:

```text
git diff --check 无输出
rg 无结果并返回 1
工作树只包含本计划明确列出的文件
```

- [ ] **Step 3: 删除已完成设计和计划文档**

使用 `apply_patch` 删除：

```text
docs/superpowers/specs/2026-08-11-retire-phase-a-secure-runtime-switch-design.md
docs/superpowers/plans/2026-08-11-retire-phase-a-secure-runtime-switch.md
```

不修改 `docs/CURRENT_STATUS.md`、`docs/TODO.md` 或其它权威文档，因为当前文档从未定义该内部假控制面。

- [ ] **Step 4: 提交文档生命周期清理**

```bash
git add -u docs/superpowers
git diff --cached --check
git commit -m "docs: remove completed phase-a cleanup records"
```

- [ ] **Step 5: 最终只读交付核验**

Run:

```bash
git status --short --branch
git log --oneline origin/codex/audit-bugfixes-20260613..HEAD
```

Expected:

```text
工作树干净
仅列出本模块设计、测试、重构和文档生命周期提交
```

## 发布边界

- 仅在 `codex/audit-bugfixes-20260613` 当前工作树实施。
- 不创建额外 worktree，不修改 `master`。
- 不推送远程；提交、合并和推送等待用户后续明确授权。
- 共享模块不进入当前 Native exact version hash；本次不产生新 exact version，不执行 onboarding、activation、持久化回测或 Registry 修改。
- 不运行真实方案、生产缓存读取、Harness lifecycle、数据库写入、plist 或服务操作。
- 不扩大 Native exact version hash。
