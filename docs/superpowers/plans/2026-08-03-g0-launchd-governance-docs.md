# G0 Launchd Governance Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every current operational document describe launchd plus installed plists as the only production scheduling control plane, while preserving ledger/daily-gray material solely as historical or retirement context.

**Architecture:** Add one concise current governance contract and make the current indexes, status page, TODO, architecture, SOP, and deployment guidance point to it. Preserve dated evidence in `docs/records/`; do not change scheduler code, database state, installed plists, or loaded services in G0.

**Tech Stack:** Markdown documentation, Python `unittest` documentation-contract tests, existing repository links.

---

### Task 1: Establish a failing current-document governance contract

**Files:**
- Modify: `tests/test_onboarding_docs.py`
- Test: `tests/test_onboarding_docs.py`

- [x] Add focused assertions that current status, TODO, architecture index, scheduling governance document, Native maintenance SOP, deployment runbook, and documentation index:
  - name launchd plus installed plist as the only production scheduling control plane;
  - prohibit ledger/occurrence/epoch and daily-gray from being a new or transition production path;
  - keep Native activation independent of frozen daily-gray policy maintenance;
  - distinguish historical `gray_live` repair from naturally scheduled `scheduled_live`;
  - index the 2026-08-03 governance record.
- [x] Run the focused documentation test and confirm it fails against the pre-G0 documents.

### Task 2: Move current documentation to the launchd-only contract

**Files:**
- Create: `docs/architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md`
- Modify: `docs/README.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/TODO.md`
- Modify: `docs/architecture/README.md`
- Modify: `docs/architecture/ARCHITECTURE.md`
- Modify: `docs/architecture/CODE_ARCHITECTURE.md`
- Modify: `docs/architecture/BLACKBOX_V2_PLATFORM.md`
- Modify: `docs/architecture/HARNESS_ARCHITECTURE.md`
- Modify: `docs/architecture/DAILY_SIGNAL_SLA.md`
- Modify: `docs/blackbox_v2/README.md`
- Modify: `docs/sop/NATIVE_V1_MAINTENANCE_SOP.md`
- Modify: `docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md`
- Modify: `deploy/README.md`
- Modify: `docs/records/status/README.md`
- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`

- [x] Create the concise, `CURRENT` governance contract with the one-writer, date/phase, fail-closed freshness, installed-plist proof, and explicit production-authorization rules.
- [x] Update current indexes, status and TODO to list G0 → G1/G2 → G3/G4/G5 → G6; record the completed 7Y gray/formal-API evidence while stating that scheduler admission and `scheduled_live` remain ungranted.
- [x] Replace normative ledger/daily-gray/epoch instructions in the listed current documents with the governance contract. Retain only clearly labeled historical or pending-retirement references where source history needs to remain readable.
- [x] Do not edit the user-owned dirty handoff `docs/records/status/KNOWN_ISSUES_HANDOFF_20260802.md`; instead make the status-record index explicit that records never define current production governance.
- [x] Remove the Native activation requirement to update the frozen daily-gray policy. Do not alter its existing Gate/authorization requirements.
- [x] Ensure deployment guidance treats installed plist replacement, `bootstrap`/`bootout`/`kickstart`, and scheduler changes as separately authorized production actions; do not change any plist.
- [x] Index the dated governance plan under `docs/records/status/README.md` and make the plan record describe the formal 7Y API Gate closure accurately.

### Task 3: Reconcile documentation tests and verify links/wording

**Files:**
- Modify: `tests/test_onboarding_docs.py`
- Test: `tests/test_onboarding_docs.py`

- [x] Replace obsolete assertions that describe ledger/epoch/daily-gray as the approved current target with assertions for the new launchd-only governance contract. Preserve unrelated onboarding and historical-evidence assertions.
- [x] Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python \
    -m pytest tests/test_onboarding_docs.py -q
  ```

  Expected: all documentation-contract tests pass.
- [x] Run a repository link/format sanity check using `git diff --check` and a scoped search confirming no `CURRENT` document presents ledger, occurrence, epoch, or daily-gray as an allowed production/transition control plane.
- [x] Do not stage the user-owned `docs/records/status/KNOWN_ISSUES_HANDOFF_20260802.md` or either untracked diagnostic script.
