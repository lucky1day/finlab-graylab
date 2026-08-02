# T1 Daily SHAP Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以一个不可拆分的 L0 Native 候选发布单元退役 `t1_daily` 不可达 SHAP 实现，并让源码证据、精确版本、daily-gray policy 和自动化门禁保持一致。

**Architecture:** 原始模块逐字节移出 active runtime tree，进入 `model_muti_0529` 批次的非运行证据目录；Native 版本继续由现有 `predict.py + core/**/*.py` 哈希自然派生，不修改 versioning 实现。仓库 policy 与新版本同步更新，但生产 activation、installed plist 和 launchctl 操作保持在本计划之外。

**Tech Stack:** Python 3.12、unittest/pytest、JSON manifest、Native V1 Harness、launchd daily-gray policy。

---

## 文件边界

**创建：**

- `tests/test_t1_daily_shap_retirement.py`：退役证据、未改算法文件和精确身份合同。
- `source_evidence/benchmark_batches/model_muti_0529/retired/t1_daily/shap_analysis.py.source`：删除前模块的逐字节归档。

**修改：**

- `tests/test_daily_gray_launchd_policy.py`：冻结新精确版本。
- `source_evidence/benchmark_batches/model_muti_0529/manifest.json`：登记退役证据元数据。
- `source_evidence/benchmark_batches/model_muti_0529/README.md`：说明审计用途与零运行时依赖。
- `deploy/daily_gray_launchd_policy_v1.json`：只更新 `t1_daily.scheme_version`。

**删除：**

- `schemes/t1_daily/core/shap_analysis.py`。

**禁止修改：**

- `schemes/t1_daily/predict.py`
- `schemes/t1_daily/config.yaml`
- `schemes/t1_daily/core/__init__.py`
- `schemes/t1_daily/core/config.py`
- `schemes/t1_daily/core/feature_engineering.py`
- `schemes/t1_daily/core/lgbm_predictor.py`
- `schemes/t1_daily/benchmarks/*`
- `shared/versioning.py`
- Native Gate 实现、依赖冻结文件、API、frontend、installed plist 和生产状态

### 执行补充：ApiReadinessGate 响应范围

上面的“禁止修改 Native Gate 实现”适用于原始 SHAP 退役提交。执行 Step 10 时，
`ApiReadinessGate` 的未过滤 `/api/backtests/factor-lab` 响应约 2.86 MB，按设计被 1 MiB
probe 上限阻断；`?benchmark_id=model_muti_0529` 响应约 621 KB。该现象确认后端过滤与
1 MiB 防护工作正常，并非后端 API bug，也不应通过放宽上限或改变展示响应解决。

获准的 follow-up 仅修改 `harness/probes/api_probe.py`、
`harness/gates/api_readiness_gate.py` 及对应测试：URL helper 同时支持可选
`data_source`/`benchmark_id`，Native readiness 从最新成功回测行传播 `benchmark_id` 并
记录证据，字段缺失时保持未过滤回退。通用 `ApiGate`、Blackbox gate、后端、frontend、
算法、policy 和生产控制面均不改变。修正后必须重新运行 Step 10 与全量测试。

### Task 1: 以 TDD 完成协调退役发布单元

**Files:**

- Create: `tests/test_t1_daily_shap_retirement.py`
- Create: `source_evidence/benchmark_batches/model_muti_0529/retired/t1_daily/shap_analysis.py.source`
- Modify: `tests/test_daily_gray_launchd_policy.py`
- Modify: `source_evidence/benchmark_batches/model_muti_0529/manifest.json`
- Modify: `source_evidence/benchmark_batches/model_muti_0529/README.md`
- Modify: `deploy/daily_gray_launchd_policy_v1.json`
- Delete: `schemes/t1_daily/core/shap_analysis.py`

- [ ] **Step 1: 写入会在旧状态失败的专用退役合同测试**

创建 `tests/test_t1_daily_shap_retirement.py`，内容如下：

```python
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from scheduler.discovery import load_scheme_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEME_DIR = PROJECT_ROOT / "schemes" / "t1_daily"
SOURCE_EVIDENCE_ROOT = (
    PROJECT_ROOT
    / "source_evidence"
    / "benchmark_batches"
    / "model_muti_0529"
)
ARCHIVE_PATH = (
    SOURCE_EVIDENCE_ROOT
    / "retired"
    / "t1_daily"
    / "shap_analysis.py.source"
)
SHAP_SHA256 = (
    "34597d21b02cfa852c8cadcbcb93ab5f"
    "47a17f52f3a400e8557dcfc79fddcdaf"
)
EXPECTED_RUNTIME_HASHES = {
    "predict.py": "3a34afb34326a5923cef81ba6939b84c77e5e0b3617849708a3b37fbe938946f",
    "core/__init__.py": "46c7bc0dc84f3f1b3330138205b0366e288b135fa6918bb61cbae5ee3ecb25a7",
    "core/config.py": "d93c2c42c97e5b2db6e08253ada6cc58f52507b6585b90181e71c3989e5a3c7b",
    "core/feature_engineering.py": "10d0be9e840cf28f8bb623412bcdf92d807193fcf33aeaeced0b17641fc11b0a",
    "core/lgbm_predictor.py": "955be5036d3a8def22f87cdaeddd352e718654be5498f59e04f6bdf67229508f",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class T1DailyShapRetirementTests(unittest.TestCase):
    def test_runtime_module_is_replaced_by_exact_non_runtime_evidence(self) -> None:
        self.assertFalse((SCHEME_DIR / "core" / "shap_analysis.py").exists())
        self.assertTrue(ARCHIVE_PATH.is_file())
        self.assertEqual(ARCHIVE_PATH.suffix, ".source")
        self.assertEqual(_sha256(ARCHIVE_PATH), SHAP_SHA256)
        self.assertNotIn(SCHEME_DIR, ARCHIVE_PATH.parents)

    def test_manifest_binds_retired_artifact_to_original_runtime_path(self) -> None:
        manifest = json.loads(
            (SOURCE_EVIDENCE_ROOT / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["retired_artifacts"],
            [
                {
                    "original_runtime_path": "schemes/t1_daily/core/shap_analysis.py",
                    "archive_path": (
                        "source_evidence/benchmark_batches/model_muti_0529/"
                        "retired/t1_daily/shap_analysis.py.source"
                    ),
                    "sha256": SHAP_SHA256,
                    "retirement_reason": (
                        "Runtime-unreachable SHAP explanation implementation retired "
                        "from the active Native bundle; retained for source-fidelity "
                        "audit only."
                    ),
                }
            ],
        )

    def test_retirement_does_not_change_remaining_algorithm_files(self) -> None:
        actual = {
            relative: _sha256(SCHEME_DIR / relative)
            for relative in EXPECTED_RUNTIME_HASHES
        }
        self.assertEqual(actual, EXPECTED_RUNTIME_HASHES)

    def test_discovery_derives_the_coordinated_exact_version(self) -> None:
        config = load_scheme_config(SCHEME_DIR / "config.yaml")
        self.assertEqual(
            config.config_hash,
            "e56ff6c4379ae4d65cfad24d327d250442358093606924f86f0efae0a7c02f29",
        )
        self.assertEqual(
            config.code_hash,
            "f86ef896620f3793df6bfaefe8063776f2203762f3e20ff61c1d32336be340e5",
        )
        self.assertEqual(config.scheme_version, "7898b9e47a9a")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 将 daily-gray 精确版本测试先改为候选版本**

在 `tests/test_daily_gray_launchd_policy.py` 中将测试改为：

```python
def test_t1_version_is_exact_shap_retired_version(self) -> None:
    policy = self.module.load_daily_gray_launchd_policy(
        discovered=self.active_daily,
    )

    self.assertEqual(
        policy.schemes["t1_daily"].scheme_version,
        "7898b9e47a9a",
    )
```

- [ ] **Step 3: 运行 RED 测试并确认失败原因正确**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_t1_daily_shap_retirement.py \
  tests/test_daily_gray_launchd_policy.py::DailyGrayLaunchdPolicyTests::test_t1_version_is_exact_shap_retired_version
```

Expected: FAIL；失败必须来自 runtime SHAP 文件仍存在、archive/manifest 尚不存在或
policy/discovery 仍为 `bdf54ed4cfc0`。若是 import、拼写或 fixture 错误，先修正测试并
重新观察正确的 RED。

- [ ] **Step 4: 使用 `apply_patch` 建立逐字节证据归档**

读取 `schemes/t1_daily/core/shap_analysis.py`，使用 `apply_patch` 将其完整原文新增到
`source_evidence/benchmark_batches/model_muti_0529/retired/t1_daily/shap_analysis.py.source`。
不得用 `cp`、Python 写文件或 shell 重定向。删除 runtime 文件前运行：

```bash
cmp -s \
  schemes/t1_daily/core/shap_analysis.py \
  source_evidence/benchmark_batches/model_muti_0529/retired/t1_daily/shap_analysis.py.source
shasum -a 256 \
  schemes/t1_daily/core/shap_analysis.py \
  source_evidence/benchmark_batches/model_muti_0529/retired/t1_daily/shap_analysis.py.source
```

Expected: `cmp` exit 0；两行 SHA-256 都是
`34597d21b02cfa852c8cadcbcb93ab5f47a17f52f3a400e8557dcfc79fddcdaf`。

- [ ] **Step 5: 登记 manifest 与 README 的非运行证据语义**

在 `manifest.json` 根对象中加入：

```json
"retired_artifacts": [
  {
    "original_runtime_path": "schemes/t1_daily/core/shap_analysis.py",
    "archive_path": "source_evidence/benchmark_batches/model_muti_0529/retired/t1_daily/shap_analysis.py.source",
    "sha256": "34597d21b02cfa852c8cadcbcb93ab5f47a17f52f3a400e8557dcfc79fddcdaf",
    "retirement_reason": "Runtime-unreachable SHAP explanation implementation retired from the active Native bundle; retained for source-fidelity audit only."
  }
]
```

在 README 追加以下当前事实：

```markdown
- `retired/t1_daily/shap_analysis.py.source` 是从 active Native runtime tree 退役的原始
  SHAP explanation 实现，只用于来源保真审计和历史解释输出复核；它不是平台输入、
  不得被方案 import，也不表示当前版本仍生产 SHAP 输出。
```

- [ ] **Step 6: 删除 runtime 模块并同步唯一 policy 字段**

使用 `apply_patch` 删除 `schemes/t1_daily/core/shap_analysis.py`。在
`deploy/daily_gray_launchd_policy_v1.json` 中只把 `t1_daily` 的：

```json
"scheme_version": "bdf54ed4cfc0"
```

改为：

```json
"scheme_version": "7898b9e47a9a"
```

运行以下检查，确保没有夹带算法改动：

```bash
git diff --check
git diff -- \
  schemes/t1_daily/predict.py \
  schemes/t1_daily/config.yaml \
  schemes/t1_daily/core/__init__.py \
  schemes/t1_daily/core/config.py \
  schemes/t1_daily/core/feature_engineering.py \
  schemes/t1_daily/core/lgbm_predictor.py \
  schemes/t1_daily/benchmarks
```

Expected: `git diff --check` exit 0；第二个命令没有输出。

- [ ] **Step 7: 运行 GREEN 合同测试**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_t1_daily_shap_retirement.py \
  tests/test_daily_gray_launchd_policy.py \
  tests/test_daily_gray_runner.py
```

Expected: PASS，0 failures。

- [ ] **Step 8: 运行 Native 身份、输入、Compare 和静态边界回归**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q \
  tests/test_versioning.py \
  tests/test_scheduler_discovery.py \
  tests/test_native_activation_lifecycle.py \
  tests/test_cli_activate.py \
  tests/test_repository_registry.py \
  tests/test_daily_input_data_service.py \
  tests/test_native_generation_t1_t5.py \
  tests/test_compare_gate.py \
  tests/test_harness_static_gate.py
```

Expected: PASS，0 failures。

- [ ] **Step 9: 运行无持久化 T1 历史复现**

先确认 CLI 参数：

```bash
conda run -n bond_factor_lab_service python -m backtests.daily_0529_reproduction --help
```

随后运行：

```bash
conda run -n bond_factor_lab_service python -m backtests.daily_0529_reproduction \
  --no-persist --skip-t5
```

Expected: exit 0；T1 original/current benchmark 通过，且无业务表写入。

- [ ] **Step 10: 运行零持久化 Native automatic Gate**

Run:

```bash
conda run -n bond_factor_lab_service python -m harness onboard t1_daily \
  --predict-date 2026-07-30 --stage all --check-only
```

Expected: automatic Gate 全部 PASS，版本为 `7898b9e47a9a`，控制面和业务表零写入。
如果目标环境或只读输入不可用，保留完整失败证据并将其视为发布阻塞，不得跳过后宣称
Native 发布单元完成。

- [ ] **Step 11: 运行全量测试并审阅最终差异**

Run:

```bash
conda run -n bond_factor_lab_service python -m pytest -q
git diff --check
git status --short
```

Expected: 全量测试 0 failures；差异只包含本计划文件边界，无 `outputs/`、reports、
backtest artifacts、凭据、installed plist 或其它草稿。

- [ ] **Step 12: 提交协调发布单元**

提交前再次核对 `git status --short` 和分支列表，只暂存本计划列出的文件：

```bash
git status --short
git branch --list master codex/audit-bugfixes-20260613
git add \
  tests/test_t1_daily_shap_retirement.py \
  tests/test_daily_gray_launchd_policy.py \
  source_evidence/benchmark_batches/model_muti_0529/retired/t1_daily/shap_analysis.py.source \
  source_evidence/benchmark_batches/model_muti_0529/manifest.json \
  source_evidence/benchmark_batches/model_muti_0529/README.md \
  deploy/daily_gray_launchd_policy_v1.json \
  schemes/t1_daily/core/shap_analysis.py
git diff --cached --check
git commit -m "refactor: retire t1 shap runtime module"
```

Expected: commit 成功；不推送、不合并 `master`、不执行生产 activation、installed plist、
`launchctl` 或服务重启。

## 任务完成判定

只有以下条件全部满足才可称“僵尸代码清理 P0 的开发治理闭环完成”：

1. runtime SHAP 文件已删除，原字节证据可由 manifest 和 SHA-256 独立核验；
2. 非 SHAP 算法文件与 config 保持固定哈希；
3. discovery 和 repository policy 同为 `t1_daily@7898b9e47a9a`；
4. 相关回归、无持久化复现、check-only automatic Gate 和全量测试全部通过；
5. 两阶段 subagent review 无未解决问题；
6. 未触碰生产数据库、installed plist、launchctl 或服务进程。

生产激活不属于本计划完成判定。若后续授权生产切换，必须另建受控操作清单，绑定相同
精确版本、目标数据库身份、一次性 activation token 和避开 07:00 的维护窗口。
