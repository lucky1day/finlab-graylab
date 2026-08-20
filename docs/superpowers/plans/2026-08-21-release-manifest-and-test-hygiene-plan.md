# Release Manifest And Test Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除无效 Git tree manifest 证据并让 release 测试可靠清理只读临时目录。

**Architecture:** manifest v2 只保留实际参与校验的 commit、archive 和 source digest 链；测试通过 module-local teardown 恢复自己 tmp_path 下目录的 owner-write。生产只读策略和发布控制面保持不变。

**Tech Stack:** Python 3.12、pytest、Git archive、tarfile、Markdown。

---

### Task 1: 删除 manifest tree 字段

**Files:**
- Modify: `scripts/build_source_release.py`
- Modify: `scripts/install_source_release.py`
- Test: `tests/test_source_release_tools.py`

- [ ] **Step 1: 写 RED 合同**

  将 deterministic build 测试收紧为 manifest 精确字段集合不含 `tree`；增加一个 v2 manifest
  携带额外 `tree` 时 installer 拒绝的测试；手工 tar fixture 删除 `tree`。

- [ ] **Step 2: 运行 RED**

  Run: `python -m pytest -q tests/test_source_release_tools.py -k 'identical_release or tree_field'`

  Expected: builder manifest 仍包含 `tree`，测试失败。

- [ ] **Step 3: 最小实现**

  schema 改为 `bfl-source-release-v2`；删除 builder 的 Git tree 查询、dataclass/manifest/CLI
  tree 输出；installer 精确 manifest 字段与格式循环只保留 commit。

- [ ] **Step 4: 运行 GREEN 并提交**

  Run: `python -m pytest -q tests/test_source_release_tools.py`

  Expected: 全部通过。

  Commit: `refactor(release): remove redundant Git tree manifest field`

### Task 2: 恢复 release 测试临时目录权限

**Files:**
- Modify: `tests/test_source_release_tools.py`

- [ ] **Step 1: 增加最小 finalizer**

  增加 module-local autouse fixture，`yield` 后遍历该测试的 `tmp_path`，仅为目录补
  `stat.S_IWUSR`，保留其余 mode bits。

- [ ] **Step 2: 验证清理行为**

  在唯一隔离 `--basetemp` 下连续运行 source-release 与 launchd-release 测试两次；两次均应
  exit 0、无 `PytestWarning`，且测试结束后 release 目录不存在 owner-unwritable directory。

- [ ] **Step 3: 精确清理旧 pytest garbage 并提交**

  只删除已核验属于当前用户、位于 pytest temp root、内容闭包为旧 release 测试残留的六个
  `garbage-*` 目录；不得使用仓库路径或宽泛 glob 作为删除目标。

  Commit: `test(release): restore temporary directory permissions`

### Task 3: 更新长期文档并清除临时计划

**Files:**
- Modify: `deploy/README.md`
- Modify: `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md`
- Modify: `docs/CURRENT_STATUS.md`
- Delete: `docs/superpowers/specs/2026-08-21-release-manifest-and-test-hygiene-design.md`
- Delete: `docs/superpowers/plans/2026-08-21-release-manifest-and-test-hygiene-plan.md`

- [ ] **Step 1: 更新真实规则**

  删除所有“commit/tree manifest”“archive/tree 校验”和“固定 commit、tree”表述；改为
  archive SHA-256、archive commit marker、source digest 三项真实校验链。CURRENT 只记录候选
  未部署边界。

- [ ] **Step 2: 删除临时文档并验证**

  删除本 spec/plan，空目录精确 `rmdir`；运行 Markdown links、architecture/docs tests、
  `git diff --check`、`cmp AGENTS.md CLAUDE.md` 和 `rg` 残留扫描。

- [ ] **Step 3: 提交**

  Commit: `docs: simplify source release evidence contract`

### Task 4: 最终验证

**Files:** No changes.

- [ ] **Step 1: 聚焦和全量验证**

  运行 source-release、launchd-release、docs/architecture 聚焦测试，随后运行全量 pytest、
  compileall、diff-check、工作树与分支审计。

- [ ] **Step 2: 整体独立审查**

  审查净变更是否只有 manifest dead field 删除、测试 teardown 和长期文档；确认没有降低生产
  权限、没有兼容层或新控制面、没有误称已部署。
