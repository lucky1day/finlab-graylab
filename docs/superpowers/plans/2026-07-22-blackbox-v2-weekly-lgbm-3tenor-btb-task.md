# Blackbox V2 Weekly LightGBM Three-Tenor BTB Task Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a completely standalone BTB task that converts one three-tenor weekly LightGBM source algorithm into three Blackbox V2 Contract 1.0 schemes, ships six reference deliverables, and grades submissions with 150 fine-grained rubrics.

**Architecture:** Keep the implementation outside Bond Factor Lab at `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task`. Vendor ordinary copies of every input and contract asset into the task, generate three self-contained reference scripts from one solution builder, and run all Intake/Gate/shadow behavior through a task-local verifier and synthetic state. No task file imports, mounts, symlinks, or runtime paths may reference the Bond Factor Lab repository.

**Tech Stack:** Ubuntu 24.04, Python 3.13.12, NumPy 2.3.5, Pandas 2.3.3, LightGBM 4.6.0, pytest, Docker/Harbor BTB format, gandalf-the-grader 1.0.0, TOML, JSON, CSV.

---

## File map

All implementation paths below are relative to:

```text
/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task
```

The only Bond Factor Lab file changed by this work is this implementation plan. The standalone task contains:

| Path | Responsibility |
|---|---|
| `instruction.md` | Chinese agent task statement, six-file output contract, and offline technical-onboarding goal |
| `task.toml` | BTB timeouts, resources, internet permission, and empty MCP configuration |
| `environment/Dockerfile` | Reproducible agent/verifier image with frozen Python and grader dependencies |
| `environment/docker-compose.yaml` | Empty standalone service override; no repository or database volumes |
| `environment/agent-python-requirements.txt` | Frozen algorithm and inspection packages |
| `environment/input/source/*` | Ordinary copies of the source script and 841-row weekly dataset |
| `environment/input/contract/*` | Self-contained Contract 1.0 schema, requests, and three-file sample data |
| `environment/input/benchmarks/*` | Visible source-original benchmark rows for the three tenors |
| `solution/build_reference.py` | Generates three standalone scripts plus three Metadata files |
| `solution/reference_deliverables/*` | The six requested reference output files |
| `solution/generate_fixtures.py` | Generates visible source-original and hidden live-safe fixtures |
| `solution/solve.sh` | Copies exactly six reference files into the agent deliverables directory |
| `tests/rubric_catalog.py` | Single source of truth for 150 rubric IDs, text, weights, and categories |
| `tests/rubric.json` | Gandalf-compatible generated rubric list |
| `tests/deterministic_verifier.py` | CLI entry point that emits evidence for R001–R146 and manual markers for R147–R150 |
| `tests/verifier_lib/discovery.py` | Six-file discovery, Metadata parsing, and identity mapping |
| `tests/verifier_lib/process.py` | Isolated subprocess execution and Output parsing |
| `tests/verifier_lib/static_checks.py` | Structure, source scan, and Metadata evidence |
| `tests/verifier_lib/contract_checks.py` | Request/Result, failure, atomicity, cutoff, and determinism evidence |
| `tests/verifier_lib/fidelity_checks.py` | Tenor configuration and hidden oracle direction evidence |
| `tests/verifier_lib/gate_checks.py` | Synthetic Intake through shadow + paused evidence |
| `tests/verifier_lib/evidence.py` | Evidence dataclass, registry, completeness checks, and JSON writer |
| `tests/hidden_fixtures/*` | Hidden requests, expected directions, and synthetic-state seed |
| `tests/test_*.py` | Development tests for task assets, reference scripts, rubric catalog, verifier, and isolation |
| `tests/grader.toml` | Hybrid grader configuration |
| `tests/judge-guidance.md` | Actual-file-first rules and deterministic-evidence authority |
| `tests/test.sh` | Harbor verifier entry point |

## Task 1: Bootstrap the standalone task root and immutable inputs

**Files:**
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/.gitignore`
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/tests/test_task_assets.py`
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/environment/input/source/weekly_lgbm_predict_3tenors.py`
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/environment/input/source/weekly_output0613.csv`
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/environment/input/contract/data_bridge_v1_schema.json`
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/environment/input/contract/sample_data/daily_output.csv`
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/environment/input/contract/sample_data/weekly_output.csv`
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/environment/input/contract/sample_data/monthly_output.csv`
- Create: `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/environment/agent-python-requirements.txt`

- [ ] **Step 1: Create the empty directory tree and independent Git repository**

Create the directories with explicit paths, then run:

```bash
git init -b main /Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task
```

Expected: a new repository whose top level is the standalone task root, not Bond Factor Lab.

- [ ] **Step 2: Pin dependencies and create a task-local development environment**

Write `environment/agent-python-requirements.txt` with:

```text
numpy==2.3.5
pandas==2.3.3
scipy==1.16.3
scikit-learn==1.8.0
lightgbm==4.6.0
joblib==1.5.3
pyarrow==23.0.0
pytest==8.4.2
```

Then run:

```bash
uv venv /Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/.venv --python 3.13.12
uv pip install --python /Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/.venv/bin/python -r /Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/environment/agent-python-requirements.txt
```

Expected: the task-local interpreter imports Pandas 2.3.3 and LightGBM 4.6.0.

- [ ] **Step 3: Write the failing immutable-asset test**

Create `tests/test_task_assets.py` with these assertions:

```python
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def test_source_assets_are_exact_regular_copies() -> None:
    script = ROOT / "environment/input/source/weekly_lgbm_predict_3tenors.py"
    data = ROOT / "environment/input/source/weekly_output0613.csv"
    assert script.is_file() and not script.is_symlink()
    assert data.is_file() and not data.is_symlink()
    assert digest(script) == "950fedc4b3bcd25ef6dde973c40f61cf8dea579cc8c6552f3892032dc6d730d8"
    assert digest(data) == "59049ce46a1f3f1efaa8f3a7c6a135349860f0f4ed84fdca3523fcc366287c7c"


def test_weekly_asset_shape_and_targets() -> None:
    data = ROOT / "environment/input/source/weekly_output0613.csv"
    frame = pd.read_csv(data, dtype={"week_id": "string"})
    assert frame.shape == (841, 575)
    assert frame["week_id"].iloc[0] == "201001"
    assert frame["week_id"].iloc[-1] == "202622"
    assert {"TB1YWI1C", "TB5YWI1C", "TB0YWI1C"} <= set(frame.columns)


def test_contract_sample_contains_three_files() -> None:
    sample = ROOT / "environment/input/contract/sample_data"
    assert sorted(path.name for path in sample.iterdir()) == [
        "daily_output.csv",
        "monthly_output.csv",
        "weekly_output.csv",
    ]
```

- [ ] **Step 4: Run the asset test to verify it fails**

Run:

```bash
/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/.venv/bin/python -m pytest /Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task/tests/test_task_assets.py -q
```

Expected: FAIL because the input assets do not exist.

- [ ] **Step 5: Copy the immutable assets as ordinary files**

Copy the provided source script and CSV into `environment/input/source/`. Copy the current machine schema from `shared/blackbox_v2/data_bridge_v1_schema.json` and the three current sample CSVs from `docs/blackbox_v2/data_bridge_v1/samples/`, renaming `*.sample.csv` to the Contract filenames. Confirm each destination is a regular file with `test ! -L`.

- [ ] **Step 6: Add a focused `.gitignore`**

Use:

```gitignore
__pycache__/
.pytest_cache/
.venv/
*.pyc
*.log
build/
dist/
recalc/
tmp/
```

Do not ignore `environment/input`, `solution/reference_deliverables`, `tests/hidden_fixtures`, or `tests/rubric.json`.

- [ ] **Step 7: Run the asset test**

Run the command from Step 3.

Expected: `3 passed`.

- [ ] **Step 8: Commit the standalone bootstrap**

```bash
git -C /Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task add .gitignore environment/agent-python-requirements.txt environment/input tests/test_task_assets.py
git -C /Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task commit -m "chore: seed standalone blackbox task inputs"
```

## Task 2: Specify the Chinese task instruction and BTB runtime metadata

**Files:**
- Create: `instruction.md`
- Create: `task.toml`
- Create: `tests/test_task_config.py`

- [ ] **Step 1: Write the failing task-config test**

```python
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_task_configuration_is_standalone_and_network_enabled() -> None:
    task = tomllib.loads((ROOT / "task.toml").read_text(encoding="utf-8"))
    assert task["version"] == "1.0"
    assert task["environment"]["allow_internet"] is True
    assert task["environment"].get("mcp_servers", []) == []
    assert task["agent"]["timeout_sec"] >= 7200
    assert task["verifier"]["timeout_sec"] >= 3600


def test_instruction_requires_three_pairs_and_no_extra_deliverables() -> None:
    text = (ROOT / "instruction.md").read_text(encoding="utf-8")
    for token in ("1Y", "5Y", "10Y", "weekly_point", "horizon", "六个文件"):
        assert token in text
    assert "/home/agent/workspace/blackbox_workspace/deliverables" in text
    assert "shadow + paused" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_config.py -q
```

Expected: FAIL because `task.toml` and `instruction.md` do not exist.

- [ ] **Step 3: Write `task.toml`**

Use these exact resource boundaries:

```toml
version = "1.0"

[metadata]

[verifier]
timeout_sec = 3600.0
user = "verifier"

[verifier.env]
LLM_API_KEY = "${DEEPSEEK_API_KEY}"

[agent]
timeout_sec = 7200.0
user = "agent"

[environment]
build_timeout_sec = 1800.0
cpus = 4
memory_mb = 16384
storage_mb = 20480
gpus = 0
allow_internet = true
mcp_servers = []

[solution.env]
```

- [ ] **Step 4: Write `instruction.md`**

The instruction must state, in Chinese and without revealing accepted IDs or hidden expected directions:

1. Inspect the source script and infer that it contains 1Y, 5Y, and 10Y weekly-point tasks.
2. Produce three independent Contract 1.0 pairs in the exact deliverables directory.
3. Require legal and unique new IDs, exact Metadata/file identity, `weekly_point`, `horizon=1`, and the weekly-point target rule.
4. Require the two CLI commands, exact Request/Result fields, stdout/stderr behavior, atomic Output, deterministic results, per-Request cutoff isolation, and no network/database runtime dependency.
5. Explain that the technical onboarding conclusion is only simulated `shadow + paused`, never production activation or live.
6. State that the final directory must contain exactly six regular files and nothing else.

- [ ] **Step 5: Run the task-config test**

Expected: `2 passed`.

- [ ] **Step 6: Commit the task definition**

```bash
git add instruction.md task.toml tests/test_task_config.py
git commit -m "feat: define three-tenor conversion task"
```

## Task 3: Build the six Contract 1.0 reference deliverables

**Files:**
- Create: `solution/build_reference.py`
- Create: `solution/reference_deliverables/weekly_1y_lgbm_calibrated_trial_v1.py`
- Create: `solution/reference_deliverables/weekly_1y_lgbm_calibrated_trial_v1.json`
- Create: `solution/reference_deliverables/weekly_5y_lgbm_calibrated_trial_v1.py`
- Create: `solution/reference_deliverables/weekly_5y_lgbm_calibrated_trial_v1.json`
- Create: `solution/reference_deliverables/weekly_10y_lgbm_calibrated_trial_v1.py`
- Create: `solution/reference_deliverables/weekly_10y_lgbm_calibrated_trial_v1.json`
- Create: `tests/test_reference_deliverables.py`

- [ ] **Step 1: Write failing reference-deliverable tests**

The test must discover pairs by Metadata rather than fixed filenames and assert:

```python
EXPECTED = {
    "1Y": ("TB1YWI1C", 0, 160),
    "5Y": ("TB5YWI1C", 156, 160),
    "10Y": ("TB0YWI1C", 104, 80),
}


def test_reference_directory_contains_exactly_three_pairs():
    files = sorted(path.name for path in REFERENCE.iterdir())
    assert len(files) == 6
    assert sum(name.endswith(".py") for name in files) == 3
    assert sum(name.endswith(".json") for name in files) == 3


def test_metadata_contract_and_identity():
    for metadata_path in REFERENCE.glob("*.json"):
        raw = metadata_path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        metadata = json.loads(raw)
        assert set(metadata) == {
            "schema_version", "scheme_id", "name", "description",
            "algorithm_version", "target_tenor", "task_type",
            "horizon", "target_rule",
        }
        assert metadata_path.stem == metadata["scheme_id"]
        assert (REFERENCE / f"{metadata_path.stem}.py").is_file()
        assert metadata["target_tenor"] in EXPECTED
        assert metadata["task_type"] == "weekly_point"
        assert metadata["horizon"] == 1
        assert metadata["target_rule"] == "target_week_end_yield_vs_feature_week_end_yield"
```

- [ ] **Step 2: Run the reference test to verify it fails**

Expected: FAIL because no reference deliverables exist.

- [ ] **Step 3: Implement `solution/build_reference.py`**

The builder must render one self-contained script per entry in this exact configuration:

```python
SCHEMES = {
    "1Y": {
        "scheme_id": "weekly_1y_lgbm_calibrated_trial_v1",
        "close_col": "TB1YWI1C",
        "window": 0,
        "max_extra_cols": 160,
    },
    "5Y": {
        "scheme_id": "weekly_5y_lgbm_calibrated_trial_v1",
        "close_col": "TB5YWI1C",
        "window": 156,
        "max_extra_cols": 160,
    },
    "10Y": {
        "scheme_id": "weekly_10y_lgbm_calibrated_trial_v1",
        "close_col": "TB0YWI1C",
        "window": 104,
        "max_extra_cols": 80,
    },
}
```

Each generated script must embed, rather than import, these source-compatible constants:

```python
FEATURE_MODE = "diff"
TEST_START_WEEK = "202401"
CALIBRATION_SPLIT = 0.78
THRESHOLD_BALANCE_PENALTY = 0.25
THRESHOLD_GRID_LOW = 0.30
THRESHOLD_GRID_HIGH = 0.70
THRESHOLD_GRID_STEP = 0.01
NUM_LEAVES = 2
MIN_CHILD_SAMPLES = 12
LEARNING_RATE = 0.01
N_ESTIMATORS = 120
REG_ALPHA = 0.5
REG_LAMBDA = 2.0
SUBSAMPLE = 0.85
COLSAMPLE_BYTREE = 0.85
RANDOM_STATE = 42
```

The rendered script must define these focused functions and exact return types:

| Function | Required behavior |
|---|---|
| `parse_date(value: object, field: str) -> str` | Return a canonical `YYYY-MM-DD` string or raise `ValueError`. |
| `validate_request(raw: dict[str, object]) -> dict[str, str]` | Validate the exact seven fields and return their normalized string mapping. |
| `load_weekly(data_dir: Path) -> pd.DataFrame` | Load and validate the consumed weekly file without touching daily or monthly data. |
| `truncate_weekly(frame: pd.DataFrame, cutoff: str) -> pd.DataFrame` | Return a new frame ending at the unique exact cutoff row. |
| `make_labels(frame: pd.DataFrame) -> np.ndarray` | Return source-compatible next-row direction labels. |
| `weekly_target_features(frame: pd.DataFrame) -> pd.DataFrame` | Return the lag, sign, momentum, volatility, and z-score block. |
| `weekly_features(frame: pd.DataFrame) -> pd.DataFrame` | Add eligible raw and first-difference business features. |
| `choose_threshold(cal_prob: np.ndarray, cal_labels: np.ndarray, historical_up_rate: float) -> float` | Return the balanced-calibration grid optimum. |
| `predict_one(request: dict[str, str], full_frame: pd.DataFrame) -> dict[str, object]` | Return exactly the five Contract Result fields. |
| `write_json_atomic(path: Path, payload: dict[str, object]) -> None` | Create one JSON Output atomically. |
| `write_csv_atomic(path: Path, rows: list[dict[str, object]]) -> None` | Create one ordered CSV Output atomically. |
| `run_predict(args: argparse.Namespace) -> None` | Validate one Request and write one JSON Result. |
| `run_backtest(args: argparse.Namespace) -> None` | Validate 1–100 Requests and write ordered CSV Results. |
| `main(argv: list[str] | None = None) -> int` | Dispatch commands and return a process exit code. |

`validate_request` must require exactly seven fields, canonical dates, `feature_date <= predict_date <= target_date`, `feature_date < target_date`, daily `YYYY-MM-DD`, weekly/monthly six digits, and non-empty unique batch IDs. `truncate_weekly` must exact-match a unique cutoff and return a copy ending at that row. `predict_one` must compute features after truncation, train only with labels available before the cutoff row, preserve all source hyperparameters, and echo the four Request fields unchanged.

Both atomic writers must reject a pre-existing Output, write to a same-directory `NamedTemporaryFile(delete=False)`, flush and close it, call `os.replace`, and unlink the temporary file on exceptions. The top-level `main` must catch exceptions, write one concise error to stderr, leave stdout empty, and return nonzero.

- [ ] **Step 4: Generate the six files**

Run:

```bash
.venv/bin/python solution/build_reference.py
```

Expected: three `.py` files and three `.json` files under `solution/reference_deliverables/`.

- [ ] **Step 5: Run static and import tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_reference_deliverables.py -q
.venv/bin/python -m py_compile solution/reference_deliverables/*.py
```

Expected: all tests pass and all scripts compile.

- [ ] **Step 6: Commit the reference implementation**

```bash
git add solution/build_reference.py solution/reference_deliverables tests/test_reference_deliverables.py
git commit -m "feat: add six blackbox reference deliverables"
```

## Task 4: Add request samples, visible benchmarks, and sealed live-safe fixtures

**Files:**
- Create: `environment/input/contract/request.sample.json`
- Create: `environment/input/contract/requests.sample.csv`
- Create: `environment/input/benchmarks/weekly_1y_reference.csv`
- Create: `environment/input/benchmarks/weekly_5y_reference.csv`
- Create: `environment/input/benchmarks/weekly_10y_reference.csv`
- Create: `solution/generate_fixtures.py`
- Create: `tests/hidden_fixtures/requests.json`
- Create: `tests/hidden_fixtures/live_safe_oracle.json`
- Create: `tests/hidden_fixtures/synthetic_state.json`
- Create: `tests/test_fixtures.py`

- [ ] **Step 1: Write failing fixture tests**

Assert the visible benchmarks contain ten sorted rows per tenor with these columns:

```python
VISIBLE_COLUMNS = [
    "benchmark_role", "tenor", "week_id", "predicted_direction",
    "prob_up", "threshold_used", "source_script_sha256", "source_data_sha256",
]
```

Assert hidden fixtures cover the six cutoff weeks `202512`, `202526`, `202540`, `202601`, `202610`, and `202620` for all three tenors, and each expected direction is integer `-1` or `1`.

- [ ] **Step 2: Run fixture tests to verify they fail**

Expected: FAIL because fixture files do not exist.

- [ ] **Step 3: Implement `solution/generate_fixtures.py`**

The generator must:

1. Import the provided source script by file path and run its original full-batch `run_tenor` functions to create visible `source_original_full_batch` rows.
2. Run each reference script through its public CLI for the six hidden cutoffs to create `current_snapshot_as_of_not_historical_vintage` expected directions.
3. Create canonical Request dates that are echoed but whose model cutoff authority is `weekly_cutoff_key`.
4. Write hidden expected values only under `tests/hidden_fixtures`; do not copy them into `environment/input`.
5. Hash and record the source script and data in every visible benchmark.

Use this exact hidden request shape:

```json
{
  "request_id": "hidden-202620",
  "predict_date": "2026-05-16",
  "feature_date": "2026-05-15",
  "target_date": "2026-05-22",
  "daily_cutoff_key": "2026-05-15",
  "weekly_cutoff_key": "202620",
  "monthly_cutoff_key": "202605"
}
```

Dates for the other cutoffs must be fixed in the generator, not inferred at grader runtime.

- [ ] **Step 4: Generate and test fixtures**

Run:

```bash
.venv/bin/python solution/generate_fixtures.py
.venv/bin/python -m pytest tests/test_fixtures.py -q
```

Expected: visible and hidden fixture tests pass.

- [ ] **Step 5: Commit fixtures and their generator**

```bash
git add environment/input/contract environment/input/benchmarks solution/generate_fixtures.py tests/hidden_fixtures tests/test_fixtures.py
git commit -m "test: add source and live-safe benchmark fixtures"
```

## Task 5: Define exactly 150 fine-grained rubrics

**Files:**
- Create: `tests/rubric_catalog.py`
- Create: `tests/rubric.json`
- Create: `tests/test_rubric_catalog.py`

- [ ] **Step 1: Write the failing rubric-catalog test**

```python
from collections import Counter

from rubric_catalog import RUBRICS


def test_rubric_catalog_has_exact_ids_and_counts() -> None:
    assert len(RUBRICS) == 150
    assert [item["id"] for item in RUBRICS] == [f"R{i:03d}" for i in range(1, 151)]
    counts = Counter(item["group"] for item in RUBRICS)
    assert counts == {
        "structure": 15,
        "metadata": 20,
        "contract": 25,
        "cutoff": 20,
        "fidelity": 25,
        "determinism": 18,
        "gates": 15,
        "failure": 8,
        "manual": 4,
    }


def test_machine_and_manual_boundary() -> None:
    assert all(item["mode"] == "machine" for item in RUBRICS[:146])
    assert all(item["mode"] == "manual" for item in RUBRICS[146:])
```

- [ ] **Step 2: Run the catalog test to verify it fails**

Expected: FAIL because `rubric_catalog.py` does not exist.

- [ ] **Step 3: Implement the rubric catalog with the fixed ID map**

Use one criterion per fact and the following exact ID allocation:

```text
R001–R015 structure:
six files; three Python; three JSON; no extras; no symlinks; paired basenames;
legal IDs; unique IDs; no known collision; regular Python files; regular JSON files;
each file below 50 MiB; UTF-8 scripts; no-BOM JSON; no forbidden absolute path.

R016–R035 metadata:
three parseable objects; exact required keys; schema 1.0; filename identity;
1Y present; 5Y present; 10Y present; target tenors unique; weekly_point for all;
horizon integer 1 for all; weekly target rule for all; non-empty algorithm versions;
non-empty names; names omit redundant tenor; names omit redundant task wording;
descriptions non-empty; descriptions <=300 chars; descriptions single-line plain text;
composite identities unique; exactly one pair per tenor.

R036–R060 contract:
--help; predict command; backtest command; exact seven request fields;
reject missing request field; reject extra request field; reject empty request ID;
reject duplicate batch IDs; canonical three dates; date ordering; daily cutoff format;
weekly cutoff format; monthly cutoff format; exact JSON result fields;
JSON direction integer; JSON direction domain; echoed predict date; echoed feature date;
echoed target date; echoed request ID; exact CSV header; batch row count;
batch row order; CSV direction text domain; successful business stdout empty.

R061–R080 cutoff:
reads weekly_output.csv; does not require unused daily file; does not require unused monthly file;
exact cutoff lookup; includes cutoff row; rejects missing weekly file; rejects missing cutoff;
rejects duplicate week_id; rejects blank week_id; rejects invalid consumed business value;
1Y future-row isolation; 5Y future-row isolation; 10Y future-row isolation;
1Y unused-column tolerance; 5Y unused-column tolerance; 10Y unused-column tolerance;
1Y post-cutoff mutation isolation; 5Y post-cutoff mutation isolation;
10Y post-cutoff mutation isolation; input snapshot remains byte-identical.

R081–R105 fidelity:
1Y target column; 5Y target column; 10Y target column; 1Y all-history window;
5Y 156 window; 10Y 104 window; 1Y max 160 extras; 5Y max 160 extras;
10Y max 80 extras; diff feature mode; calibration split 0.78; balance penalty 0.25;
threshold grid `(0.30, 0.70, 0.01)`; num_leaves 2; min_child_samples 12;
learning_rate 0.01; n_estimators 120; reg_alpha 0.5; reg_lambda 2.0;
subsample 0.85; colsample_bytree 0.85; random_state 42; 1Y hidden directions;
5Y hidden directions; 10Y hidden directions.

R106–R123 determinism:
1Y repeated predict; 5Y repeated predict; 10Y repeated predict;
1Y predict/backtest equality; 5Y predict/backtest equality; 10Y predict/backtest equality;
1Y reordered batch; 5Y reordered batch; 10Y reordered batch;
1Y split batch; 5Y split batch; 10Y split batch;
1Y 100-row batch accepted; 5Y 100-row batch accepted; 10Y 100-row batch accepted;
empty batch rejected; 101-row batch rejected; no cross-process state.

R124–R138 gates:
Intake pass; static pass; input pass; unit pass; dry-run pass; compare pass;
backtest-no-persist pass; api-readiness pass; three synthetic SchemeConfigs;
three composite Registry IDs; shadow registration pass; status paused for all;
version_status shadow for all; active API excludes all; scheduler excludes all.

R139–R146 failure:
failed predict nonzero; failed backtest nonzero; failed run stderr non-empty;
failed run stdout empty; failed run leaves no Output; failed run cleans temporary Output;
pre-existing Output rejected without overwrite; no network/database/subprocess runtime dependency.

R147–R150 manual:
1Y name/description semantic quality; 5Y name/description semantic quality;
10Y name/description semantic quality; code clarity and tenor isolation.
```

Assign weights `10` to exact identity, Result, cutoff, hidden direction, and synthetic-state safety; `5` to other behavioral criteria; and `1` to presentation/manual criteria. Prefix every criterion string with `[Rnnn]` so Gandalf output can be joined to deterministic evidence without adding nonstandard JSON fields.

- [ ] **Step 4: Generate `rubric.json` from the catalog**

The generated file must remain a JSON list of objects with only the sample-compatible fields:

```json
{
  "criterion": "[R001] 最终目录恰好包含六个文件",
  "weight": 10,
  "category": "指令遵循"
}
```

- [ ] **Step 5: Run rubric tests**

Expected: exactly 150 sequential rubric IDs and the nine exact group counts pass.

- [ ] **Step 6: Commit the rubric catalog**

```bash
git add tests/rubric_catalog.py tests/rubric.json tests/test_rubric_catalog.py
git commit -m "test: define 150 granular onboarding rubrics"
```

## Task 6: Implement evidence infrastructure, discovery, and static checks

**Files:**
- Create: `tests/verifier_lib/__init__.py`
- Create: `tests/verifier_lib/evidence.py`
- Create: `tests/verifier_lib/discovery.py`
- Create: `tests/verifier_lib/static_checks.py`
- Create: `tests/test_static_verifier.py`

- [ ] **Step 1: Write failing evidence and discovery tests**

Test that `EvidenceBook.record("R001", True, {"count": 6})` rejects duplicate IDs, JSON-serializes observations, and requires all R001–R146 before completion. Test that discovery maps arbitrary legal scheme IDs to exactly one 1Y, one 5Y, and one 10Y pair by Metadata.

- [ ] **Step 2: Run the tests to verify they fail**

Expected: import failures for `verifier_lib`.

- [ ] **Step 3: Implement `EvidenceBook`**

Use this immutable evidence value:

```python
@dataclass(frozen=True)
class Evidence:
    rubric_id: str
    passed: bool
    observed: object
    message: str = ""
```

`EvidenceBook` must expose `record`, `fill_unrecorded_as_failed`, `assert_complete`, and `write_json`. `record` rejects duplicate or malformed IDs; `fill_unrecorded_as_failed` records only missing IDs; `assert_complete` compares exact ordered ID sets; `write_json` writes a list of dataclass dictionaries through a temporary file and `os.replace`.

- [ ] **Step 4: Implement deliverable discovery**

Return a `Submission` containing the deliverables root and `dict[str, SchemePair]` keyed by tenor. Parse JSON without BOM, require object values, pair basenames, and retain parse errors as evidence rather than crashing the whole verifier.

- [ ] **Step 5: Implement R001–R035 static evidence**

Scan file count/types, symlinks, sizes, UTF-8, scheme regex, known collision set including `weekly_10y_lgbm_point_v1`, Metadata fields, unique tenor mapping, names, descriptions, and forbidden path strings. Record exactly R001–R035.

- [ ] **Step 6: Run static verifier tests**

Expected: reference deliverables pass R001–R035; fixtures with an extra README, symlink, conflicting ID, BOM Metadata, or repeated tenor fail only their corresponding rubric evidence.

- [ ] **Step 7: Commit static verifier code**

```bash
git add tests/verifier_lib tests/test_static_verifier.py
git commit -m "test: add deterministic intake and static evidence"
```

## Task 7: Implement subprocess, Contract, cutoff, and failure checks

**Files:**
- Create: `tests/verifier_lib/process.py`
- Create: `tests/verifier_lib/contract_checks.py`
- Create: `tests/test_contract_verifier.py`

- [ ] **Step 1: Write failing process and Contract tests**

Cover a successful reference `predict`, a two-row `backtest`, missing/extra fields, duplicate IDs, invalid dates, missing cutoff, future-row mutation, unused-column addition, pre-existing Output, and failed-output cleanup.

- [ ] **Step 2: Run the tests to verify they fail**

Expected: import failures for the new modules.

- [ ] **Step 3: Implement the isolated process runner**

Use this concrete return value:

```python
@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    output_exists: bool
    output_bytes: bytes | None
    duration_sec: float
```

Implement `run_scheme(script: Path, command: str, request_path: Path, data_dir: Path, output_path: Path, timeout_sec: int) -> RunResult` and return every field above for success, controlled failure, and timeout.

Invoke `sys.executable`, a minimal environment containing only PATH, locale, and fixed thread variables, `cwd` equal to a temporary directory, `capture_output=True`, and no shell. Never import submission scripts.

- [ ] **Step 4: Implement R036–R080 and R139–R146 checks**

Generate all invalid inputs in verifier-owned temporary directories. For future isolation, make two copies of the full weekly file, alter only rows whose `week_id` is greater than the Request cutoff in one copy, and require byte-identical five-field Result values. For unused-column tolerance, append one finite `UNUSED_ADDITIVE_FIXTURE` column to every row. Hash all three input files before and after every run.

For runtime dependency checks, statically reject imports and tokens for `requests`, `urllib`, `httpx`, `pymysql`, `sqlalchemy`, `mysql`, `subprocess`, socket creation, and absolute source paths; dynamically run with proxy variables removed and no credentials.

- [ ] **Step 5: Run Contract verifier tests**

Expected: the six reference files pass every R036–R080 and R139–R146 test; targeted broken fixtures fail the intended evidence IDs.

- [ ] **Step 6: Commit Contract checks**

```bash
git add tests/verifier_lib/process.py tests/verifier_lib/contract_checks.py tests/test_contract_verifier.py
git commit -m "test: verify blackbox contract and cutoff isolation"
```

## Task 8: Implement source-fidelity and determinism checks

**Files:**
- Create: `tests/verifier_lib/fidelity_checks.py`
- Create: `tests/test_fidelity_verifier.py`

- [ ] **Step 1: Write failing fidelity tests**

Require the reference scripts to expose the configured constants and match all hidden expected directions. Create mutations for wrong 5Y window, wrong 10Y column, random state 41, and one hard-coded hidden direction; each mutation must fail the corresponding fidelity or isolation evidence.

- [ ] **Step 2: Run fidelity tests to verify they fail**

Expected: missing `fidelity_checks` import.

- [ ] **Step 3: Implement R081–R123 evidence**

Parse constants with Python AST rather than importing submissions. Compare target columns, windows, feature caps, feature mode, calibration values, threshold grid, LightGBM hyperparameters, and random seed against the exact source configuration. Execute the six hidden Requests per tenor and compare directions to `live_safe_oracle.json`.

For determinism, compare canonical JSON objects by field, not byte formatting. For reordered batches, align by Request ID before comparing. For split batches, concatenate the independent results in original Request order. Build a valid 100-row batch by repeating distinct canonical Requests with unique IDs, and require 101 rows and empty batches to fail.

- [ ] **Step 4: Run fidelity and determinism tests**

Expected: reference scripts pass R081–R123; each mutation fails the specific intended criterion.

- [ ] **Step 5: Commit fidelity checks**

```bash
git add tests/verifier_lib/fidelity_checks.py tests/test_fidelity_verifier.py
git commit -m "test: verify source fidelity and deterministic batching"
```

## Task 9: Implement synthetic Gate and shadow + paused simulation

**Files:**
- Create: `tests/verifier_lib/gate_checks.py`
- Create: `tests/test_gate_simulation.py`

- [ ] **Step 1: Write failing Gate simulation tests**

Seed synthetic state with empty drafts, versions, Registry, predictions, backtests, active API IDs, and scheduler IDs. Assert the reference submission produces three paused/shadow rows, no protected-table deltas, and no active API or scheduler visibility. Assert any upstream evidence failure blocks shadow registration.

- [ ] **Step 2: Run Gate tests to verify they fail**

Expected: missing `gate_checks` import.

- [ ] **Step 3: Implement the task-local synthetic lifecycle**

Use plain dataclasses and JSON copies only. The state value is:

```python
@dataclass
class SyntheticScheme:
    base_scheme_id: str
    registry_scheme_id: str
    target_tenor: str
    status: str = "paused"
    version_status: str = "draft"
```

Implement `simulate_onboarding(submission: Submission, evidence: EvidenceBook, seed: dict) -> dict` by deep-copying the seed, rejecting any failed prerequisite evidence, adding exactly three `SyntheticScheme` dictionaries, setting each `version_status` from `draft` to `shadow`, retaining `status=paused`, and deriving empty active API and scheduler projections.

The seven gate results are derived from already-recorded machine evidence. Shadow may run only when Intake and R001–R123 plus R139–R146 are all passing. Register composite IDs as `{base_scheme_id}__h1__{target_tenor}`, change only draft/version/Registry synthetic control state, leave predictions/backtests empty, and filter paused rows from active API/scheduler projections.

- [ ] **Step 4: Record R124–R138 evidence**

Record one fact per rubric: Intake, seven gates, three SchemeConfigs, three composite IDs, shadow transition, paused status, shadow version, API invisibility, scheduler invisibility.

- [ ] **Step 5: Run Gate tests**

Expected: reference submission passes all 15 Gate criteria and broken submissions never reach shadow.

- [ ] **Step 6: Commit Gate simulation**

```bash
git add tests/verifier_lib/gate_checks.py tests/test_gate_simulation.py
git commit -m "test: simulate isolated shadow onboarding"
```

## Task 10: Assemble the deterministic verifier and hybrid judge

**Files:**
- Create: `tests/deterministic_verifier.py`
- Create: `tests/test_deterministic_verifier.py`
- Create: `tests/grader.toml`
- Create: `tests/judge-guidance.md`
- Create: `tests/test.sh`

- [ ] **Step 1: Write a failing end-to-end verifier test**

Invoke the verifier against a temporary copy of `solution/reference_deliverables`, assert exit code zero, assert evidence contains exactly R001–R150 in order, assert R001–R146 pass, and assert R147–R150 carry `manual=true` rather than fabricated machine results.

- [ ] **Step 2: Run the verifier test to verify it fails**

Expected: missing entry point.

- [ ] **Step 3: Implement `deterministic_verifier.py`**

Use CLI arguments:

```text
--deliverables <path>
--task-root <path>
--evidence-output <path>
```

Run static, Contract, cutoff, fidelity, determinism, failure, and Gate checks in order. Catch check-group exceptions, fill only that group's missing IDs as failed with the exception type, always write evidence, append manual markers for R147–R150, and return zero only when all R001–R146 pass.

- [ ] **Step 4: Write `judge-guidance.md`**

Require the judge to inspect actual files, run `/tests/deterministic_verifier.py` if evidence is absent, accept deterministic pass/fail as authoritative for R001–R146, and independently evaluate only R147–R150. Explicitly prohibit giving credit based on agent narrative, filenames alone, or visible benchmark hard-coding.

- [ ] **Step 5: Write `grader.toml` and `test.sh`**

Use the sample Gandalf batch configuration with the new Chinese instructions, `/tests/rubric.json`, `/tests/judge-guidance.md`, output `/logs/verifier`, and workdir `/home/agent/workspace/blackbox_workspace/deliverables`. `test.sh` must snapshot the agent workspace, return reward 0 when the final directory is absent or empty, run deterministic evidence first, then run gandalf with a verifier-owned trajectory file.

- [ ] **Step 6: Run end-to-end verifier tests**

Expected: R001–R146 pass for the reference solution and evidence has exactly 150 sequential IDs.

- [ ] **Step 7: Commit verifier and grader integration**

```bash
git add tests/deterministic_verifier.py tests/test_deterministic_verifier.py tests/grader.toml tests/judge-guidance.md tests/test.sh
git commit -m "test: assemble hybrid task grader"
```

## Task 11: Add the oracle solution entry point

**Files:**
- Create: `solution/solve.sh`
- Create: `tests/test_solution_entrypoint.py`

- [ ] **Step 1: Write the failing oracle-entry test**

Run `solve.sh` with a temporary workspace override and assert the destination contains exactly the six reference basenames with byte-identical contents.

- [ ] **Step 2: Run the test to verify it fails**

Expected: `solution/solve.sh` is missing.

- [ ] **Step 3: Implement `solve.sh`**

The script must use `set -euo pipefail`, resolve its own directory, default the destination to `/home/agent/workspace/blackbox_workspace/deliverables`, accept `DELIVERABLES_DIR` only as a task-specific test override, create the destination, verify it is empty, and copy the six reference files. It must not invoke or reference Bond Factor Lab.

- [ ] **Step 4: Run solution tests and deterministic verifier**

Expected: copied outputs are byte-identical and pass R001–R146.

- [ ] **Step 5: Commit the oracle entry point**

```bash
git add solution/solve.sh tests/test_solution_entrypoint.py
git commit -m "feat: add oracle solution entrypoint"
```

## Task 12: Build the standalone Docker environment

**Files:**
- Modify: `environment/agent-python-requirements.txt`
- Create: `environment/Dockerfile`
- Create: `environment/docker-compose.yaml`
- Create: `tests/test_environment_files.py`

- [ ] **Step 1: Write failing environment-file tests**

Assert requirements pin Python-facing packages, Dockerfile creates `agent`, `verifier`, and `sandbox` users, copies only `environment/input` into the agent workspace, installs gandalf for verifier, and contains no `bond-factor-lab`, MySQL, MCP, or external volume path. Assert compose has a `main` service and no `volumes` key.

- [ ] **Step 2: Run tests to verify they fail**

Expected: missing environment files.

- [ ] **Step 3: Re-verify the frozen algorithm and inspection dependencies**

Confirm the existing file still contains exactly:

```text
numpy==2.3.5
pandas==2.3.3
scipy==1.16.3
scikit-learn==1.8.0
lightgbm==4.6.0
joblib==1.5.3
pyarrow==23.0.0
pytest==8.4.2
```

- [ ] **Step 4: Implement the Dockerfile**

Base it on Ubuntu 24.04. Install bash, curl, git, nodejs, npm, sudo, bzip2, libgomp1, tmux, and uv. Create the four sample-compatible users and shared workspace permissions. Install Python 3.13.12 under `/opt/agent-python`, the pinned requirements, and `gandalf-the-grader[pinned]==1.0.0` under `/opt/uv-tools`. Copy `input/` to `/home/agent/workspace/blackbox_workspace/input/`, create an empty writable deliverables directory, set `WORKDIR /home/agent/workspace/blackbox_workspace`, and run as `agent`.

Do not copy `solution/` or `tests/` into the agent-visible image layer; Harbor supplies tests separately to verifier. Do not configure any MCP server or repository volume.

- [ ] **Step 5: Write standalone compose configuration**

Use:

```yaml
services:
  main: {}
```

- [ ] **Step 6: Run environment-file tests and build the image**

Run:

```bash
.venv/bin/python -m pytest tests/test_environment_files.py -q
docker build -t blackbox-v2-weekly-lgbm-3tenor-task:local environment
```

Expected: tests pass and Docker build completes.

- [ ] **Step 7: Inspect the container**

Run the image with no repository mounts and verify Python/LightGBM versions, internet-capable agent runtime, the two input files, contract assets, visible benchmarks, an empty deliverables directory, and absence of `/workspace/bond-factor-lab` or `/Users/macstudio0/bond-factor-lab`.

- [ ] **Step 8: Commit the environment**

```bash
git add environment tests/test_environment_files.py
git commit -m "build: add standalone BTB execution image"
```

## Task 13: Prove standalone isolation and complete the package

**Files:**
- Create: `tests/test_isolation.py`
- Create: `MANIFEST.sha256`

- [ ] **Step 1: Write the failing isolation test**

Recursively inspect all task files except `.git`, reject symlinks, reject the strings `/Users/macstudio0/bond-factor-lab`, `../bond-factor-lab`, and imports beginning with the project package names. Permit the design-time source asset to contain its own local `ROOT`, but reject external absolute paths in generated deliverables, task config, solution, tests, and Docker files.

- [ ] **Step 2: Run isolation tests to verify current gaps**

Expected: FAIL until all accidental paths or links are removed.

- [ ] **Step 3: Fix every reported path and dependency**

Replace any design-machine path with task-root-relative discovery. Copy rather than link every required asset. Ensure Docker and test commands accept only task-local or container paths.

- [ ] **Step 4: Generate `MANIFEST.sha256`**

Hash every regular task file except `.git`, transient caches, and `MANIFEST.sha256` itself. Sort entries by POSIX relative path. Add a test that recomputes and matches the manifest.

- [ ] **Step 5: Run the full local test suite**

Run:

```bash
.venv/bin/python -m pytest tests -q
```

Expected: all development tests pass.

- [ ] **Step 6: Run the oracle in a clean temporary workspace**

Create a fresh temporary directory, run `solution/solve.sh` with the task-specific destination override, then run `tests/deterministic_verifier.py` against it.

Expected: R001–R146 all pass; R147–R150 are manual markers.

- [ ] **Step 7: Run from a copied standalone root**

Copy the task directory, excluding `.git` and caches, to a fresh `mktemp -d` path. From that copy, run the full tests, oracle, deterministic verifier, and Docker build using only the copied directory as context.

Expected: all commands pass with no Bond Factor Lab path mounted or readable.

- [ ] **Step 8: Verify the existing repository is untouched by implementation**

From `/Users/macstudio0/bond-factor-lab`, run `git status --short` and inspect `schemes/`, Registry configuration, deployment files, and gray records. Expected: no implementation changes; only the already committed design/plan history exists.

- [ ] **Step 9: Commit the final isolation proof**

```bash
git add tests/test_isolation.py MANIFEST.sha256
git commit -m "test: prove standalone task isolation"
```

- [ ] **Step 10: Produce the handoff archive**

Create `/Users/macstudio0/Downloads/blackbox-v2-weekly-lgbm-3tenor-task.tar.gz` from the standalone root while excluding `.git`, caches, and temporary logs. List the archive and verify its embedded `MANIFEST.sha256` before handoff.

## Final verification checklist

- [ ] Standalone task root contains the sample-compatible task, environment, solution, and tests domains.
- [ ] Agent runtime permits internet access.
- [ ] Final algorithm runtime requires no network, database, external package install, repository, or hidden local file.
- [ ] Reference solution contains exactly three `.py` and three `.json` files.
- [ ] Three Metadata files represent 1Y, 5Y, and 10Y weekly-point horizon-1 tasks.
- [ ] Visible benchmarks are labeled source-original; hidden expected values are labeled live-safe current-snapshot as-of.
- [ ] `rubric.json` contains exactly 150 sequential, single-fact criteria.
- [ ] Deterministic evidence covers R001–R146; only R147–R150 use LLM judgment.
- [ ] Synthetic onboarding ends with three `shadow + paused` schemes absent from active API/scheduler views.
- [ ] Oracle output passes all machine rubrics.
- [ ] Docker build and clean-copy test pass without mounting Bond Factor Lab.
- [ ] Existing `schemes/`, Registry, database, scheduler, deployment, and gray state remain unchanged.
