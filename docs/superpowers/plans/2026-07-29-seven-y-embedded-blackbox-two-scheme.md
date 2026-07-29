# 7Y Two Standalone Embedded Blackbox Schemes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build two independent Blackbox V2 two-file deliveries for `7y-cfc-0084` and `7y-cfc-0156`, each embedding everything needed from 5Y10 and 10Y04 and preserving the audited 320-row actions.

**Architecture:** A new build-only package freezes the two approved configurations, selects and hashes the audited native payload, and renders a self-contained Python runner plus metadata for each scheme. Each generated runner validates and extracts its own private payload, runs both anchors against request-cutoff data, computes its own 7Y member pair, and emits the Blackbox V2 result contract. Verification occurs in a blank directory with no source package, external scheme configuration, or sibling delivery available.

**Tech Stack:** Python 3.13.12, macOS arm64 Mach-O extension modules, standard library (`argparse`, `base64`, `hashlib`, `importlib`, `json`, `lzma`, `pathlib`, `tempfile`), pandas 2.3.3, numpy 2.3.5, existing Blackbox V2 Intake/gates/runtime profile, `unittest`.

## Global Constraints

- Treat all existing gray-lab configurations, registration records, enablement state, source evidence, and audit outputs as read-only.
- Create no scheme under `schemes/` and perform no Intake, registration, activation, scheduler, or production mutation.
- Final output root is `/Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries/`.
- Final output contains exactly two directories, each containing only one same-ID `.py` and one same-ID `.json`.
- Scheme IDs are exactly `seven_y_t1_cfc_0084_embedded_v1` and `seven_y_t1_cfc_0156_embedded_v1`.
- A delivery must not import from or read the source package, 5Y10/10Y04 scheme directories, the sibling delivery, or any fixed absolute local path.
- Each delivery may depend only on the Blackbox V2 Python runtime, platform input data, and its writable private run directory.
- Runtime target is CPython 3.13 on macOS arm64; fail closed on ABI or architecture mismatch.
- `predict` and `backtest` use the same inference function; batch size is 1 through 100.
- Request cutoffs are independent; future rows and later batch members cannot affect an earlier request.
- Standard output is empty; diagnostics go to standard error; output is atomic; errors never become `action = 0`.
- Each scheme must match its own audited 320 actions exactly and reproduce SIM accuracy `0.6262626262626263`, SIM trade rate `0.8461538461538461`, REAL accuracy `0.60431654676259`, and combined trade rate `0.74375`.
- Each scheme, including its extracted payload and run artifacts, must remain below the 64 MiB run-directory limit; result output remains below 50 MiB.
- The embedded-binary form is an explicitly approved exception to the SOP's human-readable pure-source interpretation; all machine-enforced Blackbox V2 contracts still apply.

---

## File Structure

Create the following build-only package and tests in the repository:

```text
tools/embedded_7y_blackbox/
├── __init__.py
├── frozen_schemes.py
├── payload.py
├── renderer.py
├── verifier.py
└── templates/
    └── runner.py.tmpl
scripts/
└── build_embedded_7y_blackboxes.py
tests/
├── test_embedded_7y_frozen_schemes.py
├── test_embedded_7y_payload.py
├── test_embedded_7y_renderer.py
├── test_embedded_7y_runtime.py
└── test_embedded_7y_parity.py
```

Responsibilities:

- `frozen_schemes.py`: immutable scheme/member/anchor identities and metadata values only.
- `payload.py`: audited source selection, path safety, hashing, compression, and manifest production only.
- `renderer.py`: deterministic rendering of two final files from a frozen scheme and payload.
- `runner.py.tmpl`: all self-contained runtime behavior that will appear in each generated `.py`.
- `verifier.py`: read-only structural, isolation, parity, metric, and sandbox verification.
- `build_embedded_7y_blackboxes.py`: CLI orchestration; it never registers or activates a scheme.
- Tests never modify any existing gray-lab file and use temporary directories for all runtime mutations.

---

### Task 1: Freeze the Two Approved Scheme Specifications

**Files:**
- Create: `tools/embedded_7y_blackbox/__init__.py`
- Create: `tools/embedded_7y_blackbox/frozen_schemes.py`
- Create: `tests/test_embedded_7y_frozen_schemes.py`

**Interfaces:**
- Produces: `FrozenScheme`, `AnchorIdentity`, `SCHEMES`, `get_scheme(scheme_id: str) -> FrozenScheme`
- Consumes: no earlier task

- [ ] **Step 1: Write the failing identity and immutability tests**

```python
from dataclasses import FrozenInstanceError
import unittest

from tools.embedded_7y_blackbox.frozen_schemes import SCHEMES, get_scheme


class FrozenSchemeTests(unittest.TestCase):
    def test_exact_approved_scheme_identities(self) -> None:
        self.assertEqual(
            set(SCHEMES),
            {
                "seven_y_t1_cfc_0084_embedded_v1",
                "seven_y_t1_cfc_0156_embedded_v1",
            },
        )
        self.assertEqual(
            get_scheme("seven_y_t1_cfc_0084_embedded_v1").candidate_hash,
            "69bf3a432784639d",
        )
        self.assertEqual(
            get_scheme("seven_y_t1_cfc_0156_embedded_v1").candidate_hash,
            "ad0d94dc15febd8a",
        )

    def test_only_member_difference_is_frozen_explicitly(self) -> None:
        fast = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
        standard = get_scheme("seven_y_t1_cfc_0156_embedded_v1")
        self.assertEqual(fast.curve_member_id, "7y-fcco-08756")
        self.assertEqual(fast.edge_minimum_history, 21)
        self.assertEqual(standard.curve_member_id, "7y-cco-03620")
        self.assertEqual(standard.edge_minimum_history, 14)
        self.assertEqual(fast.member_weights, (1, 2))
        self.assertEqual(standard.member_weights, (1, 2))

    def test_scheme_is_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            get_scheme("seven_y_t1_cfc_0084_embedded_v1").threshold = 1.0
```

- [ ] **Step 2: Run the test and confirm it fails because the module is absent**

Run:

```bash
python -m unittest tests.test_embedded_7y_frozen_schemes -v
```

Expected: `ModuleNotFoundError: No module named 'tools.embedded_7y_blackbox'`.

- [ ] **Step 3: Implement frozen dataclasses and exact constants**

Use frozen dataclasses and tuples; do not expose mutable dictionaries as canonical state:

```python
@dataclass(frozen=True)
class AnchorIdentity:
    label: str
    source_scheme_id: str
    config_sha256: str
    runner_sha256: str


@dataclass(frozen=True)
class FrozenScheme:
    scheme_id: str
    candidate_id: str
    candidate_hash: str
    curve_family: str
    curve_member_id: str
    curve_member_hash: str
    edge_minimum_history: int
    hfas_member_id: str = "7y-hfas-10173"
    hfas_member_hash: str = "a8cbf01f35327bb7"
    member_weights: tuple[int, int] = (1, 2)
    threshold: float = 0.0
    tie_rule: str = "abstain"
```

Populate the common curve parameters exactly:

```python
DIRECT_YIELD_COLUMNS = ("TB5YWI0C", "TB7YWI0C", "TB0YWI0C")
FAIR_VALUE_WEIGHTS = (0.6, 0.4)
EDGE_WINDOW = 42
GAP_Z_MINIMUM_HISTORY = 42
GAP_Z_WINDOW = 84
CURVE_THRESHOLD_WINDOW = 252
CURVE_ACTIVE_RATE = 0.55
HFAS_ACTIVE_RATE = 0.60
HFAS_THRESHOLD_WINDOW = 42
HFAS_STATE_Z_WINDOW = 84
ANCHOR_WEIGHTS = (1.0, -1.0)
```

Populate both anchor identities with the SHA256 values in the approved design.

- [ ] **Step 4: Run the tests**

Run:

```bash
python -m unittest tests.test_embedded_7y_frozen_schemes -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/embedded_7y_blackbox/__init__.py tools/embedded_7y_blackbox/frozen_schemes.py tests/test_embedded_7y_frozen_schemes.py
git commit -m "test: freeze approved embedded 7y schemes"
```

---

### Task 2: Build a Safe, Audited Native Payload

**Files:**
- Create: `tools/embedded_7y_blackbox/payload.py`
- Create: `tests/test_embedded_7y_payload.py`

**Interfaces:**
- Consumes: `AnchorIdentity` and the read-only daily_0629 source package
- Produces:
  - `PayloadEntry(relative_path: str, raw_size: int, raw_sha256: str, compressed_sha256: str, encoded_chunks: tuple[str, ...])`
  - `collect_payload(source_root: Path) -> tuple[PayloadEntry, ...]`
  - `decode_entry(entry: PayloadEntry) -> bytes`

- [ ] **Step 1: Write failing payload safety and round-trip tests**

```python
class EmbeddedPayloadTests(unittest.TestCase):
    def test_payload_contains_both_anchor_implementations(self) -> None:
        entries = collect_payload(SOURCE_ROOT)
        paths = {entry.relative_path for entry in entries}
        self.assertIn(
            "daily_project/src/daily/selected_models/5y10/_run_impl.cpython-313-darwin.so",
            paths,
        )
        self.assertIn(
            "daily_project/src/daily/selected_models/10y04/_run_impl.cpython-313-darwin.so",
            paths,
        )

    def test_payload_excludes_unrelated_and_database_modules(self) -> None:
        paths = {entry.relative_path for entry in collect_payload(SOURCE_ROOT)}
        self.assertFalse(any("1y13" in path for path in paths))
        self.assertFalse(any("db_writer" in path for path in paths))
        self.assertFalse(any("db_config" in path for path in paths))

    def test_entry_round_trip_matches_source_bytes(self) -> None:
        for entry in collect_payload(SOURCE_ROOT):
            self.assertEqual(
                decode_entry(entry),
                (SOURCE_ROOT / entry.relative_path).read_bytes(),
            )

    def test_rejects_unsafe_relative_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe payload path"):
            validate_relative_path("../escape.so")
```

- [ ] **Step 2: Run the test and confirm missing interfaces**

Run:

```bash
python -m unittest tests.test_embedded_7y_payload -v
```

Expected: import failure for `tools.embedded_7y_blackbox.payload`.

- [ ] **Step 3: Implement the exact payload allowlist**

Include:

```python
PAYLOAD_PATTERNS = (
    "audit_logging.cpython-313-darwin.so",
    "data_service.cpython-313-darwin.so",
    "runtime_common.cpython-313-darwin.so",
    "shap_policy.cpython-313-darwin.so",
    "daily_project/src/daily/__init__.py",
    "daily_project/src/daily/selected_models/5y10/__init__.py",
    "daily_project/src/daily/selected_models/5y10/run.py",
    "daily_project/src/daily/selected_models/5y10/_run_impl.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/5y10/combo_overlay.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/5y10/fixed_lgbm.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/5y10/strategy_signals.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/10y04/run.py",
    "daily_project/src/daily/selected_models/10y04/_run_impl.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/common/__init__.py",
    "daily_project/src/daily/selected_models/common/build_weekmap_features.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/common/daily_utils.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/common/pandas_compat.cpython-313-darwin.so",
    "daily_project/src/daily/selected_models/common/week_map_alignment.cpython-313-darwin.so",
)
```

If isolated anchor import later proves a missing transitive module, add only that exact module with a failing test proving the need. Do not include `1y13`, DB writers, top-level backtest runners, shell files, README files, bytecode caches, or absolute paths.

Compress each file with deterministic `lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME)`, encode with Base85, and split into fixed 16,384-character chunks. Hash both raw and compressed bytes with SHA256. Sort entries by relative path.

- [ ] **Step 4: Add source identity and size assertions**

Test that the collected raw payload is below 8 MiB, every Mach-O filename contains `cpython-313-darwin`, and repeated collection produces byte-for-byte identical serialized manifests.

- [ ] **Step 5: Run the payload tests**

Run:

```bash
python -m unittest tests.test_embedded_7y_payload -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tools/embedded_7y_blackbox/payload.py tests/test_embedded_7y_payload.py
git commit -m "feat: package audited 7y anchor payload"
```

---

### Task 3: Render Deterministic Two-File Deliveries

**Files:**
- Create: `tools/embedded_7y_blackbox/renderer.py`
- Create: `tools/embedded_7y_blackbox/templates/runner.py.tmpl`
- Create: `tests/test_embedded_7y_renderer.py`

**Interfaces:**
- Consumes: `FrozenScheme`, `tuple[PayloadEntry, ...]`
- Produces:
  - `render_runner(scheme: FrozenScheme, payload: tuple[PayloadEntry, ...]) -> str`
  - `render_metadata(scheme: FrozenScheme) -> dict[str, object]`
  - `write_delivery(output_root: Path, scheme: FrozenScheme, payload: tuple[PayloadEntry, ...]) -> Path`

- [ ] **Step 1: Write failing deterministic structure tests**

```python
class EmbeddedRendererTests(unittest.TestCase):
    def test_writes_exactly_two_same_id_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheme = get_scheme("seven_y_t1_cfc_0084_embedded_v1")
            delivery = write_delivery(root, scheme, tiny_payload())
            self.assertEqual(
                sorted(path.name for path in delivery.iterdir()),
                [f"{scheme.scheme_id}.json", f"{scheme.scheme_id}.py"],
            )

    def test_schemes_do_not_reference_each_other(self) -> None:
        text_0084 = render_runner(SCHEMES["seven_y_t1_cfc_0084_embedded_v1"], tiny_payload())
        text_0156 = render_runner(SCHEMES["seven_y_t1_cfc_0156_embedded_v1"], tiny_payload())
        self.assertNotIn("seven_y_t1_cfc_0156_embedded_v1", text_0084)
        self.assertNotIn("seven_y_t1_cfc_0084_embedded_v1", text_0156)

    def test_render_is_deterministic(self) -> None:
        scheme = SCHEMES["seven_y_t1_cfc_0156_embedded_v1"]
        self.assertEqual(
            render_runner(scheme, tiny_payload()),
            render_runner(scheme, tiny_payload()),
        )
```

- [ ] **Step 2: Run tests and confirm renderer imports fail**

Run:

```bash
python -m unittest tests.test_embedded_7y_renderer -v
```

Expected: import failure for `renderer`.

- [ ] **Step 3: Implement metadata rendering**

Render the exact SOP keys:

```python
{
    "schema_version": "1.0",
    "scheme_id": scheme.scheme_id,
    "name": "CFC0084_EMBEDDED_ANCHOR_CONSENSUS"
        if scheme.candidate_id == "7y-cfc-0084"
        else "CFC0156_EMBEDDED_ANCHOR_CONSENSUS",
    "algorithm_version": "1.0.0",
    "target_tenor": "7Y",
    "task_type": "T+1",
    "horizon": 1,
    "target_rule": "target_date_yield_vs_feature_date_yield",
    "description": (
        f"Standalone embedded delivery preserving {scheme.candidate_id} "
        f"({scheme.candidate_hash}) with private 5Y10 and 10Y04 anchors."
    ),
}
```

Reject writes if the destination scheme directory already contains anything other than the exact expected pair. Write both files through same-directory temporary files followed by `os.replace`.

- [ ] **Step 4: Render frozen scheme and payload literals without code execution**

Use JSON literals inserted into sentinel markers in `runner.py.tmpl`. Do not use `eval`, `exec`, `compile`, `pickle`, generated imports, or a runtime template engine in the final runner. Verify the rendered script with `ast.parse`.

- [ ] **Step 5: Run renderer tests**

Run:

```bash
python -m unittest tests.test_embedded_7y_renderer -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tools/embedded_7y_blackbox/renderer.py tools/embedded_7y_blackbox/templates/runner.py.tmpl tests/test_embedded_7y_renderer.py
git commit -m "feat: render standalone 7y blackbox pairs"
```

---

### Task 4: Implement Fail-Closed Runtime Extraction and Anchor Loading

**Files:**
- Modify: `tools/embedded_7y_blackbox/templates/runner.py.tmpl`
- Create: `tests/test_embedded_7y_runtime.py`

**Interfaces:**
- Produces inside the generated runner:
  - `_validate_runtime() -> None`
  - `_safe_payload_path(root: Path, relative_path: str) -> Path`
  - `_extract_payload(run_root: Path) -> Path`
  - `_anchor_modules(payload_root: Path) -> tuple[ModuleType, ModuleType]`
- Consumes: rendered `_PAYLOAD_MANIFEST`

- [ ] **Step 1: Write failing corruption, traversal, ABI, and import tests**

Generate a real runner into a temporary directory and run it in the Blackbox environment:

```python
def test_corrupted_payload_fails_before_extraction(self) -> None:
    script = self.render_real_runner().replace('"compressed_sha256":"', '"compressed_sha256":"0', 1)
    completed = self.run_script(script, request=self.valid_request())
    self.assertNotEqual(completed.returncode, 0)
    self.assertFalse(self.output_path.exists())

def test_payload_path_escape_is_rejected(self) -> None:
    module = self.load_generated_module()
    with self.assertRaisesRegex(ValueError, "unsafe payload path"):
        module._safe_payload_path(Path(self.tempdir), "../escape.so")

def test_real_anchor_modules_import_from_extracted_tree(self) -> None:
    module = self.load_generated_module()
    payload_root = module._extract_payload(Path(self.tempdir))
    five, ten = module._anchor_modules(payload_root)
    self.assertTrue(str(five.__file__).startswith(str(payload_root)))
    self.assertTrue(str(ten.__file__).startswith(str(payload_root)))
```

- [ ] **Step 2: Run only these tests and observe failures**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest tests.test_embedded_7y_runtime.EmbeddedRuntimeTests -v
```

Expected: missing runtime functions or failed extraction.

- [ ] **Step 3: Implement runtime validation and extraction**

Runtime validation must enforce:

```python
if sys.version_info[:2] != (3, 13):
    raise RuntimeError("requires CPython 3.13")
if platform.system() != "Darwin" or platform.machine() != "arm64":
    raise RuntimeError("requires macOS arm64")
```

Decode each Base85 chunk, verify compressed SHA256, decompress, verify raw size and SHA256, reject unsafe paths, create parents with mode `0o700`, write a same-directory temporary file with mode `0o600`, then `os.replace`. Reject existing symlinks and verify the final resolved path remains under the private extraction root.

- [ ] **Step 4: Implement explicit module loading**

Add only the extracted package root and `daily_project/src` to `sys.path`, invalidate import caches, and call:

```python
five = importlib.import_module("daily.selected_models.5y10.run")
ten = importlib.import_module("daily.selected_models.10y04.run")
```

Verify `five.__file__` and `ten.__file__` are inside the extraction root. Remove inserted paths and unload the extracted `daily.*` modules during cleanup.

- [ ] **Step 5: Run runtime tests**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest tests.test_embedded_7y_runtime.EmbeddedRuntimeTests -v
```

Expected: corruption/path/ABI/import tests pass.

- [ ] **Step 6: Commit**

```bash
git add tools/embedded_7y_blackbox/templates/runner.py.tmpl tests/test_embedded_7y_runtime.py
git commit -m "feat: load private embedded 7y anchor runtime"
```

---

### Task 5: Implement Causal Platform-Input Adaptation and Anchor Replay

**Files:**
- Modify: `tools/embedded_7y_blackbox/templates/runner.py.tmpl`
- Modify: `tests/test_embedded_7y_runtime.py`

**Interfaces:**
- Produces inside generated runner:
  - `_materialize_cutoff_data(data_dir: Path, feature_date: pd.Timestamp, work_dir: Path) -> Path`
  - `_run_5y10(module: ModuleType, protected_root: Path, output_dir: Path) -> pd.DataFrame`
  - `_run_10y04(module: ModuleType, protected_root: Path, output_dir: Path, feature_date: pd.Timestamp) -> pd.DataFrame`
  - `_anchor_score(frame: pd.DataFrame, anchor: str, feature_date: pd.Timestamp) -> pd.Series`

- [ ] **Step 1: Add failing cutoff and future-row-isolation tests**

Build a runtime input fixture containing one future row beyond the request cutoff. Assert:

```python
baseline = run_predict(data_dir=without_future, feature_date="2025-07-15")
with_future = run_predict(data_dir=with_appended_future, feature_date="2025-07-15")
self.assertEqual(baseline, with_future)
```

Patch the generated module's file open hook during unit tests and assert no read path starts with:

```python
FORBIDDEN_ROOTS = (
    "/Users/macstudio0/bond-factor-lab/schemes/daily_5y_lgbm_5y10_0629",
    "/Users/macstudio0/bond-factor-lab/schemes/daily_10y_lgbm_10y04_0629",
    "/Users/macstudio0/bond-factor-lab/source_evidence/benchmark_batches/daily_0629",
    "/Users/macstudio0/Desktop/方案/0629/forecast_project",
)
```

- [ ] **Step 2: Run the new tests and confirm future isolation is not implemented**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest tests.test_embedded_7y_runtime.EmbeddedRuntimeTests.test_future_rows_do_not_change_result -v
```

Expected: failure because the anchor replay functions are missing.

- [ ] **Step 3: Implement per-request materialization**

Read platform files only from the supplied `--data-dir`. Normalize and filter:

- daily rows by `date <= feature_date`;
- `api_wind_date.csv` rows by `rdate <= feature_date`;
- weekly rows to authoritative `week_id` values present in the truncated calendar;
- monthly rows by `month_id <= int(feature_date.strftime("%Y%m"))`.

Write exactly `daily_output.csv`, `weekly_output.csv`, `monthly_output.csv`, and `api_wind_date.csv` under a request-private `data/` directory. Validate monotonic dates, required columns, exact cutoff coverage, and finite `TB5YWI0C`, `TB7YWI0C`, and `TB0YWI0C`.

- [ ] **Step 4: Port the already-audited anchor invocation contract**

Use the exact replay calls from `round6_runner.py`:

```python
five_result = five.run(protected_root / "data", five_output, n_jobs=1)

ten_args = argparse.Namespace(
    data=protected_root / "data/daily_output.csv",
    output_dir=ten_output,
    test_end=feature_date.strftime("%Y-%m-%d"),
    n_jobs=1,
)
ten_result = ten.run(ten_args)
```

Temporarily override and restore:

```python
"daily.selected_models.5y10.fixed_lgbm": {
    "TEST_START": pd.Timestamp("2024-07-01"),
    "SCREEN_END": pd.Timestamp("2024-07-01"),
}
"daily.selected_models.5y10.combo_overlay": {
    "FULL_START": pd.Timestamp("2024-07-01"),
    "SIM_START": pd.Timestamp("2024-07-01"),
}
"daily.selected_models.10y04._run_impl": {
    "TEST_START": pd.Timestamp("2024-07-01"),
    "SCREEN_END": pd.Timestamp("2024-07-01"),
}
```

Find the longest dated CSV containing one of `prob_up`, `model_prob_up`, `candidate_score`, or `active_score`. For probability fields compute `2 * probability - 1`; otherwise use the raw score. Keep unique normalized dates from 2024-07-01 through the request cutoff.

- [ ] **Step 5: Run anchor and isolation tests**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest tests.test_embedded_7y_runtime -v
```

Expected: both anchors produce finite cutoff scores, future-row isolation passes, and no forbidden root is read.

- [ ] **Step 6: Commit**

```bash
git add tools/embedded_7y_blackbox/templates/runner.py.tmpl tests/test_embedded_7y_runtime.py
git commit -m "feat: replay embedded anchors with causal cutoffs"
```

---

### Task 6: Port and Lock the Two 7Y Consensus Algorithms

**Files:**
- Modify: `tools/embedded_7y_blackbox/templates/runner.py.tmpl`
- Modify: `tests/test_embedded_7y_runtime.py`

**Interfaces:**
- Produces inside generated runner:
  - `_curve_orientation_actions(daily: pd.DataFrame, scheme: dict[str, object]) -> pd.Series`
  - `_historical_anchor_actions(daily: pd.DataFrame, five: pd.Series, ten: pd.Series) -> pd.Series`
  - `_weighted_vote(curve: pd.Series, anchor: pd.Series) -> pd.Series`
  - `_infer_one(data_dir: Path, feature_date: pd.Timestamp) -> dict[str, object]`

- [ ] **Step 1: Write failing golden-vector tests**

Build golden inputs and expected member/action vectors directly from the existing audited implementation:

```python
REFERENCE_SIGNALS = Path(
    "/Users/macstudio0/Documents/liwei/src/gray_state_gate/signals.py"
)

def test_0084_and_0156_use_different_frozen_curve_history(self) -> None:
    result_0084 = self.module_0084._curve_orientation_actions(self.daily, self.scheme_0084)
    result_0156 = self.module_0156._curve_orientation_actions(self.daily, self.scheme_0156)
    self.assertEqual(self.scheme_0084["edge_minimum_history"], 21)
    self.assertEqual(self.scheme_0156["edge_minimum_history"], 14)
    self.assertFalse(result_0084.equals(result_0156))

def test_weighted_vote_is_one_to_two_with_abstain_ties(self) -> None:
    curve = pd.Series([1, -1, 1, 0])
    anchor = pd.Series([1, 1, -1, 0])
    expected = pd.Series([1, 1, -1, 0])
    pd.testing.assert_series_equal(_weighted_vote(curve, anchor), expected)
```

- [ ] **Step 2: Run the golden-vector tests and confirm missing algorithm functions**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest tests.test_embedded_7y_runtime.EmbeddedRuntimeTests.test_weighted_vote_is_one_to_two_with_abstain_ties -v
```

Expected: missing `_weighted_vote`.

- [ ] **Step 3: Port only the required causal helpers**

Copy the behavior, not imports or global registries, from these exact source functions:

- `causal_rolling_quantile_thresholds`
- `causal_anchor_transform`
- `causal_direction_edges`
- `strict_prior_z`
- `fair_value_error_matrix`
- `_historical_fair_value_state_mask`
- `_historical_anchor_state_fusion_scores`
- `_causal_curve_orientation_scores`
- `_cross_family_consensus_scores`

Use explicit arguments and the frozen constants from Task 1. Do not carry candidate registries, MAY/REAL readers, search logic, file outputs, or external config loading into the generated runner.

The historical anchor member is fixed to:

```python
anchor_score = causal_anchor_transform(
    1.0 * five_score - 1.0 * ten_score,
    transform="delta5",
    history_rule="strict_prior",
)
state_z_window = 84
threshold_window = 42
active_rate = 0.60
true_orientation = 1
false_orientation = -1
```

The final action is:

```python
vote = 1 * curve_action + 2 * anchor_action
action = np.sign(vote).astype("int64")
```

A zero vote is the configured abstention, while exceptions propagate as failures.

- [ ] **Step 4: Test both scheme literals and member vectors**

Assert each generated runner contains only its own candidate/member hashes and produces the same curve, HFAS, vote, and action vectors as the audited signal functions on the SIM and REAL inputs.

- [ ] **Step 5: Run all runtime tests**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest tests.test_embedded_7y_runtime -v
```

Expected: all algorithm and runtime tests pass.

- [ ] **Step 6: Commit**

```bash
git add tools/embedded_7y_blackbox/templates/runner.py.tmpl tests/test_embedded_7y_runtime.py
git commit -m "feat: preserve approved 7y consensus actions"
```

---

### Task 7: Complete the Blackbox V2 CLI and Failure Contract

**Files:**
- Modify: `tools/embedded_7y_blackbox/templates/runner.py.tmpl`
- Modify: `tests/test_embedded_7y_runtime.py`

**Interfaces:**
- Produces generated CLI:
  - `python <scheme>.py predict --request <json> --data-dir <dir> --output <json>`
  - `python <scheme>.py backtest --request <json> --data-dir <dir> --output <json>`
- Consumes: `_infer_one`

- [ ] **Step 1: Write failing CLI contract tests**

Cover single request, 100 requests, duplicate cutoffs, reordered batches, predict/backtest parity, empty stdout, atomic failures, and exact five-field results:

```python
self.assertEqual(
    set(result),
    {"request_id", "feature_date", "target_date", "action", "score"},
)
self.assertIn(result["action"], (-1, 0, 1))
self.assertEqual(completed.stdout, "")
```

For a batch failure, pre-create no output, inject an invalid date in item 50, and assert nonzero return code plus absent output.

- [ ] **Step 2: Run CLI tests and observe failures**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest tests.test_embedded_7y_runtime.EmbeddedRuntimeTests.test_predict_backtest_parity -v
```

Expected: CLI parsing or output contract failure.

- [ ] **Step 3: Implement one shared request executor**

Both subcommands call:

```python
def _execute_requests(
    requests: list[dict[str, object]],
    data_dir: Path,
) -> list[dict[str, object]]:
    return [_execute_one(request, data_dir) for request in requests]
```

Validate 1–100 requests, unique/nonempty request IDs according to the SOP, dates, finite output score, and exact target-date resolution using the supplied `api_wind_date-v1`. Use a per-process cache only for identical `(feature_date, input_file_hashes)` keys.

- [ ] **Step 4: Implement atomic output and stderr logging**

Serialize compact UTF-8 JSON into a same-directory temporary file, flush and `os.fsync`, then `os.replace`. On any exception, remove the temporary file, leave the final output absent, write one concise error to stderr, and exit nonzero.

- [ ] **Step 5: Run CLI tests**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest tests.test_embedded_7y_runtime -v
```

Expected: all contract, batch, parity, ordering, and failure tests pass.

- [ ] **Step 6: Commit**

```bash
git add tools/embedded_7y_blackbox/templates/runner.py.tmpl tests/test_embedded_7y_runtime.py
git commit -m "feat: satisfy embedded 7y blackbox cli contract"
```

---

### Task 8: Add Reproducible Build and Read-Only Verification Commands

**Files:**
- Create: `tools/embedded_7y_blackbox/verifier.py`
- Create: `scripts/build_embedded_7y_blackboxes.py`
- Create: `tests/test_embedded_7y_parity.py`

**Interfaces:**
- Produces:
  - `build_all(source_root: Path, output_root: Path) -> tuple[Path, Path]`
  - `verify_structure(delivery: Path) -> None`
  - `verify_independence(delivery: Path, fixture_root: Path) -> None`
  - `verify_parity(delivery: Path, candidate_id: str, audit_root: Path) -> dict[str, float]`
- Consumes: Tasks 1–7

- [ ] **Step 1: Write failing orchestration tests**

```python
def test_build_all_creates_only_two_delivery_directories(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        paths = build_all(SOURCE_ROOT, Path(tmp))
        self.assertEqual({path.name for path in paths}, set(SCHEMES))
        self.assertEqual(
            {path.name for path in Path(tmp).iterdir()},
            set(SCHEMES),
        )

def test_verifier_rejects_external_absolute_path(self) -> None:
    with self.assertRaisesRegex(AssertionError, "absolute dependency"):
        verify_no_forbidden_paths("open('/Users/macstudio0/Desktop/方案/0629/x')")
```

- [ ] **Step 2: Run orchestration tests and confirm missing verifier**

Run:

```bash
python -m unittest tests.test_embedded_7y_parity -v
```

Expected: import failure for `verifier`.

- [ ] **Step 3: Implement the build CLI**

CLI:

```bash
python scripts/build_embedded_7y_blackboxes.py \
  --source-root source_evidence/benchmark_batches/daily_0629/source_package/forecast_project \
  --output-root /Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries
```

Before writing, validate that the output root is exactly the approved path or an explicit temporary path supplied by tests. Remove no unrelated path. If an approved scheme directory already exists, allow replacement only when it contains exactly the expected `.py + .json`; replace through a sibling temporary directory followed by directory rename.

- [ ] **Step 4: Implement structural and independence verification**

`verify_structure` checks exact files, names, metadata, AST parse, forbidden calls, absolute paths, symlinks, file permissions, script size, and the 64 MiB projected extraction size.

`verify_independence` copies only the tested pair and platform fixtures to a fresh temporary root, clears source-package and scheme roots from `PYTHONPATH`, runs with working directory equal to that root, and traces opened files. It fails if any read targets a forbidden root or the sibling delivery.

- [ ] **Step 5: Implement exact 320-row parity**

Read:

- `/Users/macstudio0/Documents/liwei/outputs/gray_lab_t1_7y_cross_family_consensus_20260726/sim_predictions.csv`
- `/Users/macstudio0/Documents/liwei/outputs/gray_lab_t1_7y_cross_family_consensus_20260726/real_predictions.csv`

Filter by the scheme's `candidate_id` and `config_hash`, sort by phase and feature date, and require 320 rows total. Run the delivery at every feature date and assert exact `action` equality. Recompute:

```python
traded = actual_action != 0
accuracy = (actual_action[traded] == label[traded]).mean()
trade_rate = traded.mean()
```

Assert the frozen SIM, REAL, and combined values exactly within `1e-12`.

- [ ] **Step 6: Run tests**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env_blackbox_v1/bin/python \
  -m unittest \
  tests.test_embedded_7y_frozen_schemes \
  tests.test_embedded_7y_payload \
  tests.test_embedded_7y_renderer \
  tests.test_embedded_7y_runtime \
  tests.test_embedded_7y_parity -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add tools/embedded_7y_blackbox/verifier.py scripts/build_embedded_7y_blackboxes.py tests/test_embedded_7y_parity.py
git commit -m "feat: build and verify standalone 7y deliveries"
```

---

### Task 9: Build the Two Final Deliveries and Run Full Acceptance

**Files:**
- Generate only:
  - `/Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries/seven_y_t1_cfc_0084_embedded_v1/seven_y_t1_cfc_0084_embedded_v1.py`
  - `/Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries/seven_y_t1_cfc_0084_embedded_v1/seven_y_t1_cfc_0084_embedded_v1.json`
  - `/Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries/seven_y_t1_cfc_0156_embedded_v1/seven_y_t1_cfc_0156_embedded_v1.py`
  - `/Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries/seven_y_t1_cfc_0156_embedded_v1/seven_y_t1_cfc_0156_embedded_v1.json`
- Do not modify any gray-lab configuration or create any other output artifact.

**Interfaces:**
- Consumes: complete builder and verifier
- Produces: two final delivery directories and a console-only verification summary

- [ ] **Step 1: Confirm protected paths are unchanged before build**

Record SHA256s for the two final passer JSON files and both source anchor configurations, then run `git status --short`. Store these values only in shell output, not in an output file.

- [ ] **Step 2: Build both deliveries**

Run the exact build command from Task 8.

Expected: two directory paths printed to stderr; stdout remains empty.

- [ ] **Step 3: Verify exact output shape and hashes**

Run:

```bash
find /Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries \
  -mindepth 1 -maxdepth 2 -print | sort
shasum -a 256 \
  /Users/macstudio0/Documents/liwei/outputs/blackbox_v2_deliveries/*/*
```

Expected: exactly two directories and four files.

- [ ] **Step 4: Run all repository tests for the new package**

Run the five-module `unittest` command from Task 8.

Expected: all pass.

- [ ] **Step 5: Run existing Blackbox V2 static and behavioral gates in an isolated temporary project root**

Use the existing `BlackboxStaticGate` and runtime runner with `api-wind-date-v1`. Do not call Intake against the real repository and do not persist registration state.

Expected for each scheme: static contract passes, predict and backtest pass, stdout is empty, and no platform file is mutated.

- [ ] **Step 6: Run 320-row parity separately for 0084 and 0156**

Expected for both: 320/320 exact action matches and all four frozen metrics match within `1e-12`.

- [ ] **Step 7: Run resource and independence acceptance**

For each scheme separately:

- remove source-package paths and sibling delivery from the child environment;
- use a blank working directory;
- execute cold start twice;
- run a 100-request batch;
- record wall time, peak resident memory, runner size, extracted payload size, run-directory size, and output size to the console;
- assert the applicable Blackbox limits.

If either scheme fails any limit or parity check, stop and report that scheme as not passing. Do not weaken the test and do not add a shared dependency.

- [ ] **Step 8: Recheck protected paths**

Recompute the SHA256s from Step 1 and require exact equality. Run:

```bash
git status --short
```

Expected: no gray-lab configuration, source evidence, scheme, registry, or activation file changed.

- [ ] **Step 9: Request code review**

Invoke `superpowers:requesting-code-review` over the implementation commits and address only findings that remain within the approved new-package and new-delivery scope.

- [ ] **Step 10: Run verification-before-completion**

Invoke `superpowers:verification-before-completion`, rerun the full acceptance commands, and report:

- both delivery paths;
- four SHA256 values;
- exact directory contents;
- parity counts and frozen metrics;
- static/behavioral/sandbox results;
- resource measurements;
- confirmation that protected gray-lab hashes did not change.

Do not claim either scheme passes unless the final command outputs prove every item.

