# Five-Algorithm Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify one `bond_algorithms_5` tarball containing five gray-tested algorithm directories in the exact non-shared layout approved by the user.

**Architecture:** Extract every source byte from gray R4 commit `92a93713656d8534e68312f123676b5d2054d8a6` into a temporary staging tree, then assemble an external handoff directory without modifying repository source. Copy the original weekly `forecast_project` three times so each weekly directory is complete, retain each Macro two-file delivery unchanged, verify all copies and entry points, and archive the result with a recorded SHA-256.

**Tech Stack:** Git archive, macOS shell tools, tar, SHA-256, Conda `forecast_env`, CPython 3.13 on Darwin arm64

---

## File map

Repository files created by this plan:

- None. The approved specification and this plan are the only version-controlled documentation changes.

External artifact created by this plan:

```text
/Users/macstudio0/bond-factor-lab-handoffs/
├── bond_algorithms_5_92a93713656d/
│   ├── requirements.txt
│   ├── weekly_avg_1y_lgbm_0529/
│   │   ├── config.yaml
│   │   └── forecast_project/
│   ├── weekly_avg_5y_lgbm_0529/
│   │   ├── config.yaml
│   │   └── forecast_project/
│   ├── weekly_avg_10y_lgbm_0529/
│   │   ├── config.yaml
│   │   └── forecast_project/
│   ├── cgb_a4_fundseason_5y/
│   │   ├── config.yaml
│   │   └── delivery/
│   │       ├── cgb_a4_fundseason_5y.py
│   │       └── cgb_a4_fundseason_5y.json
│   └── cgb_a4_fundseason_10y/
│       ├── config.yaml
│       └── delivery/
│           ├── cgb_a4_fundseason_10y.py
│           └── cgb_a4_fundseason_10y.json
└── bond_algorithms_5_92a93713656d.tar.gz
```

The uncompressed directory is retained beside the archive for inspection. Neither artifact is added to Git.

### Task 1: Preflight the exact gray source

**Files:**

- Read: `docs/superpowers/specs/2026-08-22-five-algorithm-handoff-design.md`
- Read from Git object: `92a93713656d8534e68312f123676b5d2054d8a6`

- [ ] **Step 1: Verify repository and source identity**

Run:

```bash
git status --short
git rev-parse --verify 92a93713656d8534e68312f123676b5d2054d8a6^{commit}
```

Expected: the status is clean and `rev-parse` prints `92a93713656d8534e68312f123676b5d2054d8a6`.

- [ ] **Step 2: Verify every required R4 path exists**

Run:

```bash
for path in \
  source_evidence/benchmark_batches/model_muti_0529/weekly_average_0529/source_package/forecast_project \
  schemes/weekly_avg_1y_lgbm_0529/config.yaml \
  schemes/weekly_avg_5y_lgbm_0529/config.yaml \
  schemes/weekly_avg_10y_lgbm_0529/config.yaml \
  schemes/cgb_a4_fundseason_5y/config.yaml \
  schemes/cgb_a4_fundseason_5y/delivery/cgb_a4_fundseason_5y.py \
  schemes/cgb_a4_fundseason_5y/delivery/cgb_a4_fundseason_5y.json \
  schemes/cgb_a4_fundseason_10y/config.yaml \
  schemes/cgb_a4_fundseason_10y/delivery/cgb_a4_fundseason_10y.py \
  schemes/cgb_a4_fundseason_10y/delivery/cgb_a4_fundseason_10y.json
do
  git cat-file -e "92a93713656d8534e68312f123676b5d2054d8a6:$path"
done
```

Expected: exit code `0` with no output.

- [ ] **Step 3: Verify the shared environment baseline**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env/bin/python -c \
  'import lightgbm,numpy,pandas,pymysql,sklearn,sqlalchemy,xgboost; import platform,sys; print(sys.version.split()[0], platform.machine(), platform.system())'
```

Expected: imports succeed and the final line is `3.13.12 arm64 Darwin`.

### Task 2: Assemble the approved non-shared directory layout

**Files:**

- Create externally: `/Users/macstudio0/bond-factor-lab-handoffs/bond_algorithms_5_92a93713656d/`
- Read from Git object: the exact Task 1 paths

- [ ] **Step 1: Create fresh staging and destination directories**

Run:

```bash
BFL_HANDOFF_STAGING="$(mktemp -d /tmp/bfl-five-algorithms.XXXXXX)"
BFL_HANDOFF_PARENT=/Users/macstudio0/bond-factor-lab-handoffs
BFL_HANDOFF_ROOT="$BFL_HANDOFF_PARENT/bond_algorithms_5_92a93713656d"
BFL_HANDOFF_ARCHIVE="$BFL_HANDOFF_PARENT/bond_algorithms_5_92a93713656d.tar.gz"
test ! -e "$BFL_HANDOFF_ROOT"
test ! -e "$BFL_HANDOFF_ARCHIVE"
mkdir -p "$BFL_HANDOFF_STAGING/r4" "$BFL_HANDOFF_PARENT" "$BFL_HANDOFF_ROOT"
```

Expected: all commands exit `0`; existing output makes the task stop rather than overwrite it.

- [ ] **Step 2: Extract only the approved files from R4**

Run in the same shell session as Step 1:

```bash
git archive 92a93713656d8534e68312f123676b5d2054d8a6 \
  source_evidence/benchmark_batches/model_muti_0529/weekly_average_0529/source_package/forecast_project \
  schemes/weekly_avg_1y_lgbm_0529/config.yaml \
  schemes/weekly_avg_5y_lgbm_0529/config.yaml \
  schemes/weekly_avg_10y_lgbm_0529/config.yaml \
  schemes/cgb_a4_fundseason_5y/config.yaml \
  schemes/cgb_a4_fundseason_5y/delivery/cgb_a4_fundseason_5y.py \
  schemes/cgb_a4_fundseason_5y/delivery/cgb_a4_fundseason_5y.json \
  schemes/cgb_a4_fundseason_10y/config.yaml \
  schemes/cgb_a4_fundseason_10y/delivery/cgb_a4_fundseason_10y.py \
  schemes/cgb_a4_fundseason_10y/delivery/cgb_a4_fundseason_10y.json \
  | tar -xf - -C "$BFL_HANDOFF_STAGING/r4"
```

Expected: exit code `0`; the staging tree contains only the listed R4 paths.

- [ ] **Step 3: Copy the common requirements and three independent weekly backends**

Run in the same shell session:

```bash
BFL_WEEKLY_SOURCE="$BFL_HANDOFF_STAGING/r4/source_evidence/benchmark_batches/model_muti_0529/weekly_average_0529/source_package/forecast_project"
cp "$BFL_WEEKLY_SOURCE/requirements.txt" "$BFL_HANDOFF_ROOT/requirements.txt"
for scheme in weekly_avg_1y_lgbm_0529 weekly_avg_5y_lgbm_0529 weekly_avg_10y_lgbm_0529
do
  mkdir -p "$BFL_HANDOFF_ROOT/$scheme"
  cp "$BFL_HANDOFF_STAGING/r4/schemes/$scheme/config.yaml" "$BFL_HANDOFF_ROOT/$scheme/config.yaml"
  cp -R "$BFL_WEEKLY_SOURCE" "$BFL_HANDOFF_ROOT/$scheme/forecast_project"
done
```

Expected: each weekly directory contains `config.yaml` and its own complete `forecast_project` directory.

- [ ] **Step 4: Copy the two Macro deliveries unchanged**

Run in the same shell session:

```bash
for scheme in cgb_a4_fundseason_5y cgb_a4_fundseason_10y
do
  mkdir -p "$BFL_HANDOFF_ROOT/$scheme/delivery"
  cp "$BFL_HANDOFF_STAGING/r4/schemes/$scheme/config.yaml" "$BFL_HANDOFF_ROOT/$scheme/config.yaml"
  cp "$BFL_HANDOFF_STAGING/r4/schemes/$scheme/delivery/$scheme.py" "$BFL_HANDOFF_ROOT/$scheme/delivery/$scheme.py"
  cp "$BFL_HANDOFF_STAGING/r4/schemes/$scheme/delivery/$scheme.json" "$BFL_HANDOFF_ROOT/$scheme/delivery/$scheme.json"
done
```

Expected: each Macro directory contains exactly its config and two delivery files.

### Task 3: Verify byte identity, scope, and entry points

**Files:**

- Verify externally: `/Users/macstudio0/bond-factor-lab-handoffs/bond_algorithms_5_92a93713656d/`

- [ ] **Step 1: Verify the three weekly copies are byte-identical to R4**

Run in the same shell session:

```bash
for scheme in weekly_avg_1y_lgbm_0529 weekly_avg_5y_lgbm_0529 weekly_avg_10y_lgbm_0529
do
  diff -qr "$BFL_WEEKLY_SOURCE" "$BFL_HANDOFF_ROOT/$scheme/forecast_project"
done
```

Expected: exit code `0` with no output.

- [ ] **Step 2: Verify every config and Macro delivery is byte-identical to R4**

Run in the same shell session:

```bash
for scheme in weekly_avg_1y_lgbm_0529 weekly_avg_5y_lgbm_0529 weekly_avg_10y_lgbm_0529 cgb_a4_fundseason_5y cgb_a4_fundseason_10y
do
  cmp "$BFL_HANDOFF_STAGING/r4/schemes/$scheme/config.yaml" "$BFL_HANDOFF_ROOT/$scheme/config.yaml"
done
for scheme in cgb_a4_fundseason_5y cgb_a4_fundseason_10y
do
  cmp "$BFL_HANDOFF_STAGING/r4/schemes/$scheme/delivery/$scheme.py" "$BFL_HANDOFF_ROOT/$scheme/delivery/$scheme.py"
  cmp "$BFL_HANDOFF_STAGING/r4/schemes/$scheme/delivery/$scheme.json" "$BFL_HANDOFF_ROOT/$scheme/delivery/$scheme.json"
done
```

Expected: exit code `0` with no output.

- [ ] **Step 3: Verify the exact output scope**

Run:

```bash
find "$BFL_HANDOFF_ROOT" -type l -print
find "$BFL_HANDOFF_ROOT" -type f \( -name '.env' -o -name '*.log' -o -name '*.pyc' \) -print
find "$BFL_HANDOFF_ROOT" -maxdepth 1 -mindepth 1 -print | sort
```

Expected: the first two commands print nothing. The last command prints exactly `requirements.txt` and the five approved scheme directories; no top-level `source_evidence` path exists.

- [ ] **Step 4: Smoke-test each weekly backend copy**

Run in the same shell session:

```bash
for scheme in weekly_avg_1y_lgbm_0529 weekly_avg_5y_lgbm_0529 weekly_avg_10y_lgbm_0529
do
  BFL_WEEKLY_ROOT="$BFL_HANDOFF_ROOT/$scheme/forecast_project"
  BFL_WEEKLY_PROJECT="$BFL_WEEKLY_ROOT/weekly_project"
  PYTHONPATH="$BFL_WEEKLY_PROJECT/src:$BFL_WEEKLY_PROJECT:$BFL_WEEKLY_ROOT" \
    /Users/macstudio0/miniconda3/envs/forecast_env/bin/python -B \
    -m weekly.run_weekly --help
done
```

Expected: all three commands exit `0` and advertise `--frequencies` with `W1Y,W5Y,W10Y` support. This smoke test does not connect to MySQL or run predictions.

- [ ] **Step 5: Smoke-test both Macro deliveries**

Run:

```bash
/Users/macstudio0/miniconda3/envs/forecast_env/bin/python -B \
  "$BFL_HANDOFF_ROOT/cgb_a4_fundseason_5y/delivery/cgb_a4_fundseason_5y.py" --version
/Users/macstudio0/miniconda3/envs/forecast_env/bin/python -B \
  "$BFL_HANDOFF_ROOT/cgb_a4_fundseason_10y/delivery/cgb_a4_fundseason_10y.py" --version
```

Expected:

```text
cgb_a4_fundseason_5y 1.0.0
cgb_a4_fundseason_10y 1.0.0
```

### Task 4: Archive and record the handoff identity

**Files:**

- Create externally: `/Users/macstudio0/bond-factor-lab-handoffs/bond_algorithms_5_92a93713656d.tar.gz`

- [ ] **Step 1: Create the compressed handoff archive without macOS metadata**

Run in the same shell session:

```bash
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata -czf "$BFL_HANDOFF_ARCHIVE" \
  -C "$BFL_HANDOFF_PARENT" bond_algorithms_5_92a93713656d
```

Expected: exit code `0` and the archive exists as a regular file.

- [ ] **Step 2: Inspect the archive layout**

Run:

```bash
tar -tzf "$BFL_HANDOFF_ARCHIVE" | sed -n '1,80p'
tar -tzf "$BFL_HANDOFF_ARCHIVE" | rg '(^|/)source_evidence/'
/Users/macstudio0/miniconda3/envs/forecast_env/bin/python -B - "$BFL_HANDOFF_ARCHIVE" <<'PY'
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    forbidden = [
        (member.name, key)
        for member in archive.getmembers()
        for key in member.pax_headers
        if "xattr" in key.lower() or "provenance" in key.lower()
    ]
if forbidden:
    raise SystemExit(f"forbidden PAX metadata: {forbidden[:5]}")
print("forbidden_pax_metadata=0")
PY
```

Expected: the first command shows the approved root and scheme paths. The second command exits `1` with no output because `source_evidence/` is absent from the archive. The Python audit prints `forbidden_pax_metadata=0`.

- [ ] **Step 3: Record size and SHA-256**

Run:

```bash
ls -lh "$BFL_HANDOFF_ARCHIVE"
shasum -a 256 "$BFL_HANDOFF_ARCHIVE"
```

Expected: both commands succeed. Preserve the exact byte size and 64-character SHA-256 in the final handoff response.

- [ ] **Step 4: Confirm the repository remained unchanged during artifact generation**

Run:

```bash
git status --short
```

Expected: clean output. The external handoff directory and archive are not part of the repository.
