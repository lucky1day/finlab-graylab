# T+5 No-Foreign LGBM Ablation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Run an isolated, read-only comparison for all 20 active daily T+5 configurations, reusing original Phase A results through the last March target and retraining LightGBM without foreign factors from the first April target, then generate one combined Markdown report.

**Architecture:** Add an experiment-only Python package and CLI on the isolated codex/t5-foreign-ablation-20260721 branch. The runner reads the main checkout's frozen DataBridge CSV files, calendar CSV, and Phase A cache envelopes without mutating them; all intermediate cache files go under the isolated worktree's ignored backtest_artifacts/experiments directory. Feature removal is injected at runtime around each imported source core, before feature screening, so production scheme files and non-LGBM signal paths remain unchanged.

**Tech Stack:** Python 3.12, pandas 2.3.3, NumPy 2.3.5, LightGBM 4.6.0, standard-library unittest, existing scheme cores and Phase A cache envelopes.

---

## File map

- Create experiments/t5_no_foreign_lgbm/__init__.py: public experiment package exports.
- Create experiments/t5_no_foreign_lgbm/policy.py: foreign source registry, derived-column provenance, temporary feature-builder patching, and audit records.
- Create experiments/t5_no_foreign_lgbm/phase_cache.py: cache validation, target-date boundary replacement, atomic experiment-only checkpoints.
- Create experiments/t5_no_foreign_lgbm/metrics.py: target-date row alignment and correct/trade/eligible metrics.
- Create experiments/t5_no_foreign_lgbm/report.py: deterministic single-Markdown renderer.
- Create experiments/t5_no_foreign_lgbm/runner.py: frozen 20-config manifest and family adapters for V28, liwei_0616, 1Y Blackbox, and t5_daily.
- Create scripts/run_t5_no_foreign_lgbm_ablation.py: CLI with explicit read-only source root and isolated output paths.
- Create tests/test_t5_no_foreign_lgbm_ablation.py: unit and contract coverage without model-scale execution.
- Create reports/experiments/2026-07-21-t5-no-foreign-lgbm-ablation.md: generated final report.

### Task 1: Foreign-factor policy and source provenance

**Files:**

- Create: experiments/t5_no_foreign_lgbm/__init__.py
- Create: experiments/t5_no_foreign_lgbm/policy.py
- Test: tests/test_t5_no_foreign_lgbm_ablation.py

- [ ] **Step 1: Write failing policy tests**

Add standard-library unittest cases that prove:

1. The daily registry includes S0031525, M0000005, M0000271, USDCNH0C, SX5EDF0C, G0003892, G0006352, G0006353, G0003956, B2559386, DRS00001, and DRS00002.
2. The weekly registry includes HWW00001, HWW00002, and HWW00003.
3. U.S. Treasury aliases match 美国国债收益率, 美债, U.S. Treasury, and Treasury yield.
4. A V31-style direct or derived feature whose source is foreign is removed before screening.
5. Domestic bond, money-market, equity, commodity, weekly, and monthly factors remain.
6. The context manager restores all imported module globals after exit.

Use a fake module with build_mf_features and build_wkmo_features functions so the tests do not import LightGBM.

- [ ] **Step 2: Run tests and verify the failure**

Run:

    /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python3.12 -m unittest tests.test_t5_no_foreign_lgbm_ablation.ForeignPolicyTests -v

Expected: import failure for experiments.t5_no_foreign_lgbm.policy.

- [ ] **Step 3: Implement the policy**

Implement immutable registries and an audit dataclass:

    FOREIGN_DAILY_CODES = frozenset({
        "S0031525", "M0000005", "M0000271", "USDCNH0C",
        "SX5EDF0C", "G0003892", "G0006352", "G0006353",
        "G0003956", "B2559386", "DRS00001", "DRS00002",
    })
    FOREIGN_WEEKLY_CODES = frozenset({
        "HWW00001", "HWW00002", "HWW00003",
    })
    UST_NAME_PATTERN = re.compile(
        r"(美国.*国债.*收益率|美债|u\.?s\.?\s*treasury|treasury\s+yield)",
        re.IGNORECASE,
    )

    @dataclass
    class FeatureAudit:
        foreign_sources_seen: set[str] = field(default_factory=set)
        candidate_columns: list[str] = field(default_factory=list)
        removed_columns: list[str] = field(default_factory=list)
        retained_columns: list[str] = field(default_factory=list)

Implement feature_source so mf_CODE_suffix maps to CODE, wk_CODE_suffix maps to CODE, and one-year CODE_retN maps to CODE. Implement patch_core_feature_builders as a context manager that:

- calls the original builder once;
- records all candidate column names;
- drops foreign daily features returned by build_mf_features;
- drops foreign weekly features returned by build_wkmo_features;
- removes dropped names from the category map;
- never changes build_all_signals or any scheme configuration;
- restores the original functions in finally.

- [ ] **Step 4: Run policy tests**

Run the Task 1 unittest command.

Expected: all ForeignPolicyTests pass.

- [ ] **Step 5: Commit**

    git add experiments/t5_no_foreign_lgbm/__init__.py experiments/t5_no_foreign_lgbm/policy.py tests/test_t5_no_foreign_lgbm_ablation.py
    git commit -m "test: define foreign factor ablation policy"

### Task 2: Target-date boundaries and Phase A cache splicing

**Files:**

- Create: experiments/t5_no_foreign_lgbm/phase_cache.py
- Modify: tests/test_t5_no_foreign_lgbm_ablation.py

- [ ] **Step 1: Write failing cache tests**

Add cases with a synthetic trading calendar and two configs proving:

- feature dates are assigned to months by the fifth later trading date;
- a March feature date whose target is 2026-04-01 is in the recompute set;
- the last feature whose target is 2026-03-31 remains baseline;
- splice_phase_cache preserves baseline values before the boundary and replaces both preds and probs from the first April target through the last June target;
- config dictionaries must match exactly;
- missing dates, duplicate dates, length mismatch, or non-finite probabilities fail closed;
- atomic checkpoints are written only below the caller-supplied experiment output directory.

- [ ] **Step 2: Run tests and verify the failure**

Run:

    /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python3.12 -m unittest tests.test_t5_no_foreign_lgbm_ablation.PhaseCacheTests -v

Expected: import failure for experiments.t5_no_foreign_lgbm.phase_cache.

- [ ] **Step 3: Implement cache validation and splice**

Implement:

    def target_map(calendar: pd.DataFrame, horizon: int = 5) -> dict[str, str]

using the sorted rdate column only.

Implement:

    def splice_phase_cache(
        baseline: Mapping[str, Any],
        ablation: Mapping[str, Any],
        *,
        feature_to_target: Mapping[str, str],
        target_start: str,
        target_end: str,
    ) -> dict[str, Any]

The returned cache uses baseline dates/config order, replaces only dates whose mapped targets are within 2026-04-01..2026-06-30, and raises RuntimeError unless every replacement date is present once in both caches. Deep-copy all result arrays so source cache objects remain immutable.

Implement atomic JSON/pickle checkpoint writers with tempfile.NamedTemporaryFile, flush, os.fsync, and os.replace. Resolve both temporary and final paths and reject any path outside the experiment output root.

- [ ] **Step 4: Run cache tests**

Run the Task 2 unittest command.

Expected: all PhaseCacheTests pass.

- [ ] **Step 5: Commit**

    git add experiments/t5_no_foreign_lgbm/phase_cache.py tests/test_t5_no_foreign_lgbm_ablation.py
    git commit -m "test: splice Phase A cache at April target boundary"

### Task 3: Metrics and deterministic combined report

**Files:**

- Create: experiments/t5_no_foreign_lgbm/metrics.py
- Create: experiments/t5_no_foreign_lgbm/report.py
- Modify: tests/test_t5_no_foreign_lgbm_ablation.py

- [ ] **Step 1: Write failing metric and report tests**

Add cases proving:

- eligible rows are identical across branches and mismatches fail closed;
- trade means direction != 0;
- accuracy equals correct trades divided by trades;
- trade rate equals trades divided by eligible rows;
- zero-trade accuracy renders N/A;
- April, May, June use target_date;
- April-June aggregate is recomputed from counts rather than averaging percentages;
- all 20 frozen configuration IDs occur exactly once in the overview;
- the report contains original and no-foreign counts, accuracy, trade rate, percentage-point changes, removed factors, U.S. Treasury scan result, changed target dates, snapshot hashes, runtime versions, and no success metric for a failed configuration.

- [ ] **Step 2: Run tests and verify the failure**

Run:

    /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python3.12 -m unittest tests.test_t5_no_foreign_lgbm_ablation.MetricAndReportTests -v

Expected: imports for metrics/report fail.

- [ ] **Step 3: Implement metrics**

Implement MetricCounts with eligible, trades, correct, accuracy, and trade_rate properties. align_branches must compare scheme_id, tenor, feature_date, target_date, and label columns exactly before computing counts. monthly_comparison returns April, May, June, and 2026-04..06 aggregate rows, with delta values as ablation minus baseline.

- [ ] **Step 4: Implement report renderer**

render_report(result_bundle) returns one Markdown string in the approved section order. Format rates to two decimal percentage points, keep numerator/denominator counts, and render None accuracy as N/A. Sort configurations by frozen manifest order, never alphabetically infer scope, and add a visible statement that the gray lab database/configuration was not written.

- [ ] **Step 5: Run metric/report tests**

Run the Task 3 unittest command.

Expected: all MetricAndReportTests pass.

- [ ] **Step 6: Commit**

    git add experiments/t5_no_foreign_lgbm/metrics.py experiments/t5_no_foreign_lgbm/report.py tests/test_t5_no_foreign_lgbm_ablation.py
    git commit -m "test: render T+5 ablation comparison report"

### Task 4: Read-only experiment runner and family adapters

**Files:**

- Create: experiments/t5_no_foreign_lgbm/runner.py
- Create: scripts/run_t5_no_foreign_lgbm_ablation.py
- Modify: tests/test_t5_no_foreign_lgbm_ablation.py

- [ ] **Step 1: Write failing runner contract tests**

Add tests proving:

- the frozen manifest contains exactly the 20 approved composite configuration IDs;
- one_y_t5_liq_excess_a_v1 is marked rule-only;
- t5_daily tenors are marked zero-foreign;
- source_data_root and source_cache_root are opened read-only and never passed to a writer;
- output/cache paths must resolve below backtest_artifacts/experiments/t5_no_foreign_lgbm in the isolated worktree;
- baseline and ablation checkpoints have different keys;
- a completed family checkpoint can resume without model execution;
- V31 adapters train filtered Phase A only for feature dates whose target is April-June, splice with the baseline cache, then run unchanged downstream core;
- V28 adapters run each source feature-month once per branch and select requested feature rows;
- one-year adapters temporarily remove USDCNH0C and S0031525 only from MARKET_FACTORS used by build_features;
- t5_daily runs baseline once and copies it as a mathematically identical zero-removal branch;
- no adapter calls a repository, scheduler, API write endpoint, or SQL write method.

- [ ] **Step 2: Run tests and verify the failure**

Run:

    /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python3.12 -m unittest tests.test_t5_no_foreign_lgbm_ablation.RunnerContractTests -v

Expected: import failure for experiments.t5_no_foreign_lgbm.runner.

- [ ] **Step 3: Implement the frozen manifest and input loader**

Define 20 SchemeSpec entries in the same order as the approved design. Load:

- daily_output.csv, weekly_output.csv, monthly_output.csv from the explicit main-checkout source data root;
- api_wind_date.csv from the explicit calendar path;
- existing Phase A envelopes from the explicit main-checkout source cache root.

Hash every file before execution and re-hash after execution; fail if any source hash changes. Do not instantiate a database engine.

- [ ] **Step 4: Implement family adapters**

For liwei_0616, group schemes by shared cache family:

- 5Y v31: STD, DIV, ACCWT.
- 7Y v31: STD, DIV, ACCWT, CROSS_5Y.
- 10Y v61: STD, DIV, ACCWT, V55_7Y.
- 5Y ALL_K10 auc/static, auc/yearly, ic/yearly: four baselines each.

For each group, patch only the LGBM feature builders, call phase_a_only for the April-June recompute feature dates, atomically checkpoint the filtered cache, splice it into a copied baseline cache, and pass the copied caches into each scheme's unchanged run_*_for_feature_window function.

For V28, execute one window for each feature month represented by April-June target rows, once for baseline and once under the feature patch.

For the three 1Y hybrid models, import each delivery module, run all requested feature dates in a single process, and temporarily set MARKET_FACTORS to the domestic subset only while build_features is called. Run the rule-only scheme once.

For t5_daily, call the existing common_utils run_prediction for 3Y, 5Y, 7Y, and 10Y. Since those model features use only Chinese government-bond tenors and domestic spreads, set ablation rows equal to baseline rows with removed_count=0.

- [ ] **Step 5: Implement resumable CLI**

The CLI requires:

    --source-data-root /Users/macstudio0/bond-factor-lab/data/data_bridge/current
    --source-cache-root /Users/macstudio0/bond-factor-lab/backtest_artifacts/runtime_cache/liwei_0616
    --calendar-path /Users/macstudio0/bond-factor-lab/backtest_artifacts/prod_screen_july/input/api_wind_date.csv
    --output-root <isolated-worktree>/backtest_artifacts/experiments/t5_no_foreign_lgbm
    --report-path <isolated-worktree>/reports/experiments/2026-07-21-t5-no-foreign-lgbm-ablation.md
    --workers 10
    --resume

Print progress to stderr and final artifact paths to stdout. On SIGINT or a family failure, keep only validated completed checkpoints and mark the configuration failed in run_state.json.

- [ ] **Step 6: Run runner tests**

Run the Task 4 unittest command.

Expected: all RunnerContractTests pass.

- [ ] **Step 7: Run the complete regression suite**

Run:

    /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python3.12 -m unittest discover -s tests -q

Expected: at least 1146 existing tests plus new tests pass, 0 failures.

- [ ] **Step 8: Commit**

    git add experiments/t5_no_foreign_lgbm/runner.py scripts/run_t5_no_foreign_lgbm_ablation.py tests/test_t5_no_foreign_lgbm_ablation.py
    git commit -m "feat: add isolated T+5 foreign factor ablation runner"

### Task 5: Execute, verify, and deliver the single Markdown report

**Files:**

- Create: reports/experiments/2026-07-21-t5-no-foreign-lgbm-ablation.md
- Runtime-only: backtest_artifacts/experiments/t5_no_foreign_lgbm/**

- [ ] **Step 1: Run the experiment**

Run the CLI from Task 4 with the service Python and the explicit main-checkout read-only inputs. Use --resume so an interrupted family continues from validated checkpoints.

Expected: 20 configuration results; source hashes unchanged; no SQL engine created; completed model families checkpointed under the isolated output root.

- [ ] **Step 2: Validate structured results**

Run the CLI's --validate-only mode against run_state.json.

Expected:

- 20 configurations present.
- April, May, June, and aggregate rows for each.
- Baseline/ablation sample keys and labels exact.
- Report rates reverse-calculate from counts.
- Filtered V31/V28 cores have no retained foreign source.
- 1Y hybrid models remove USDCNH0C and S0031525.
- t5_daily and rule-only cases are explicitly unchanged/not applicable.
- U.S. Treasury usage count is reported from the frozen active factor lists.
- Main-checkout source hashes equal their pre-run values.

- [ ] **Step 3: Generate and inspect the Markdown**

Generate reports/experiments/2026-07-21-t5-no-foreign-lgbm-ablation.md from the validated JSON bundle. Search for unfinished-template markers and unvalidated result states; any reported failure must identify a concrete configuration and reason.

- [ ] **Step 4: Run final verification**

Run:

    git diff --check
    /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python3.12 -m unittest discover -s tests -q
    /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python3.12 scripts/run_t5_no_foreign_lgbm_ablation.py --validate-only --output-root backtest_artifacts/experiments/t5_no_foreign_lgbm --report-path reports/experiments/2026-07-21-t5-no-foreign-lgbm-ablation.md

Expected: clean diff check, all tests pass, validation succeeds.

- [ ] **Step 5: Commit the generated report**

    git add reports/experiments/2026-07-21-t5-no-foreign-lgbm-ablation.md
    git commit -m "docs: report T+5 no-foreign LGBM comparison"
