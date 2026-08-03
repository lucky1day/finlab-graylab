# Blackbox Formal API Gate Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 formal Blackbox API Gate 只读取已验证 benchmark 的回测卡片，并在用户授权下通过受控 backend 重启完成端到端验证。

**Architecture:** Harness 从唯一 persisted evidence 中取得 `benchmark_id`，并将其作为
factor-lab API 的唯一回测过滤器；若 evidence 不存在，Gate 保留失败且不发全量回测请求。服务
实例身份校验不改动，重启只发生在代码提交与现场只读核对之后。

**Tech Stack:** Python 3.12、unittest、FastAPI HTTP API、MySQL、launchd。

---

### Task 1: 用精确 benchmark URL 固定 Harness 行为

**Files:**
- Create: `tests/test_blackbox_v2_api_gate.py`
- Modify: `harness/blackbox_v2/api_gate.py:185-188`

- [ ] **Step 1: 写失败测试**

构造最小 `BlackboxApiGate` 上下文，并 mock registry、persisted backtest、live evidence、服务身份和
四个 HTTP 响应；断言第四个请求是：

```python
"http://api.test/api/backtests/factor-lab?benchmark_id=verified-benchmark"
```

再构造 persisted evidence 读取失败的场景，断言 Gate 失败且没有任何
`/api/backtests/factor-lab` 请求。

- [ ] **Step 2: 验证测试为红**

运行：

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_blackbox_v2_api_gate.py -q
```

预期：精确 URL 断言失败，因为当前实现请求 `?data_source=blackbox_v2_current_snapshot_as_of`。

- [ ] **Step 3: 最小实现**

在 `BlackboxApiGate._run` 中仅当 `expected_backtest` 带非空 `benchmark_id` 时调用：

```python
factor_lab_url(base_url, benchmark_id=expected_benchmark_id)
```

否则不请求 factor-lab，并将明确的 fail-closed error 加入结果。保持现有
`_backtest_contract_errors` 的 run、benchmark、version、snapshot 与 harness 校验不变。

- [ ] **Step 4: 验证为绿**

运行 Task 1 测试和相邻 API probe/readiness 测试，预期全部通过。

- [ ] **Step 5: 独立规格与质量审查**

审查范围限定为未暂存 Harness 修订和测试；阻断项必须在提交前修复。

### Task 2: 提交 Harness 修订

**Files:**
- Modify: `docs/superpowers/specs/2026-08-03-blackbox-api-gate-repair-design.md`
- Modify: `docs/superpowers/plans/2026-08-03-blackbox-api-gate-repair.md`
- Modify: `harness/blackbox_v2/api_gate.py`
- Create: `tests/test_blackbox_v2_api_gate.py`

- [ ] **Step 1: 运行提交前验证**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python -m pytest tests/test_blackbox_v2_api_gate.py \
  tests/test_api_probe.py tests/test_api_readiness_gate.py -q
git diff --check
```

- [ ] **Step 2: 精选暂存并提交**

仅暂存 Task 2 文件；不得纳入现有未提交的 D-overlay 诊断或临时脚本。

### Task 3: 受控 backend 重启与 formal Gate

**Files:**
- Read only: `deploy/launchd/*backend*.plist`、对应 installed plist、`launchctl` loaded state

- [ ] **Step 1: 只读 preflight**

核对仓库 plist、installed plist、label、ProgramArguments、WorkingDirectory、监听 8100 的进程归属和
instance nonce 的存在性（不记录其值）。若出现 drift 或无法确认 service owner，停止。

- [ ] **Step 2: 受控重启**

仅对已确认 label 执行对应的 `launchctl kickstart -k`；不 bootstrap/bootout、不改 plist、不重启
scheduler。

- [ ] **Step 3: 端到端验证**

等待 `/api/health` 恢复 HTTP 200 后，用不输出 nonce/token 的方式重跑两个 7Y v2 的 formal API Gate。
验证服务指纹一致、factor-lab 读取的 exact benchmark 小于 1 MiB、两个 Gate 均通过。
