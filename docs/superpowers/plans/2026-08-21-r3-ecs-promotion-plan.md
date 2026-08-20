# R3 ECS Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `codex/develop` 收口为远程唯一源码 authority，并把精确源码提交 `08645a87852bbc307d3f6def22d7d457b1014bcb` 构建、预安装、激活为 ECS R3 immutable release。

**Architecture:** 源码只由 `codex/develop` 管理；release 从独立 detached clean worktree 构建，避免把本临时计划纳入 archive。ECS 只接受候选 archive 同版本 installer，先预安装和完整性验证，再在 writer idle/timer 有余量的窗口 CAS 切换 `current` 并重启 loopback Backend。Mac3、`master`、生产域名、Writer、数据库 schema 和 installed systemd unit 均不在本次变更范围。

**Tech Stack:** Git、Python 3.12、deterministic tar.gz、manifest v2、SSH、systemd、MySQL 8.0、pytest。

---

## 固定身份与停止条件

- Source commit：`08645a87852bbc307d3f6def22d7d457b1014bcb`
- Source branch：`codex/develop`
- ECS authority：`47.103.45.193`
- Expected ECS current：`5c5603a23266e563e142e319d4e5d13907649598`
- Expected ECS previous：`e692285d47e41c384dc915758abe0c51f9ac3aaf`
- Release tag：`bfl-source-r3-20260821`

任一条件不符必须 fail-closed：本地工作树不干净；远程 develop 不是本地祖先；source commit 不存在；
双构建不一致；archive/manifest/source digest 不一致；ECS host key 不匹配；current/previous 漂移；
候选目录存在但无法精确复验；Backend 不健康；任一 writer active；timer 窗口不足；migration 出现
`APPLYING`/drift；DB 快照在激活窗口发生变化；installed unit 被意外修改。

### Task 1: 收口远程 develop authority

**Files:**
- Create temporarily: `docs/superpowers/plans/2026-08-21-r3-ecs-promotion-plan.md`

- [ ] **Step 1: 本地与远程只读预检**

  核对 branch、HEAD、status、`master`、远程 `codex/develop`；使用 `git ls-remote` 取得实际远程值，
  并证明远程 develop 是本地 develop 的祖先。

- [ ] **Step 2: 非 force 推送 develop**

  Run: `git push origin codex/develop:codex/develop`

  Expected: fast-forward；`master` 未变化。

- [ ] **Step 3: 独立读回**

  `git ls-remote` 必须返回本地 `codex/develop` 精确 SHA；记录远程 master 前后 SHA 相同。

### Task 2: 构建并审查精确 R3 artifact

**Files:**
- No repository changes.
- Temporary build root: unique directory under `/private/tmp`.

- [ ] **Step 1: 建立 detached clean worktree**

  从 source commit `08645a8...` 创建唯一临时 worktree；验证 HEAD/tree、tracked clean、无 untracked。

- [ ] **Step 2: 双构建 deterministic archive**

  使用 source commit 内 `scripts/build_source_release.py` 分别构建 run1/run2；验证 archive 与 manifest
  字节一致、schema 为 `bfl-source-release-v2`、manifest commit 精确、无 `tree` 字段、archive filename
  与 SHA-256 精确一致。

- [ ] **Step 3: 独立 source digest 与 installer identity**

  使用同一 source commit 的 installer 算法从 archive 计算 source digest；记录 archive SHA、manifest
  SHA、source digest、installer SHA 和 builder SHA。不得使用当前 ECS release 的旧 installer。

### Task 3: ECS 只读 preflight 与候选预安装

**Files:**
- Remote temporary inbound: unique root-only directory outside `/opt/bond-factor-lab/releases`.

- [ ] **Step 1: 全量只读门禁**

  严格 SSH 到 ECS authority，验证 current/previous、current install record/只读完整性、candidate absent、
  Backend active/health、五 timer enabled/active、五 writer inactive、磁盘/inode、三 conda env、
  `cryptography`、DB identity、17 migration APPLIED 且无 APPLYING、Registry 56 active base/60 composite。
  同时记录 installed 11 unit hashes和关键业务表只读快照；不得输出 DSN、密码、token 或 server UUID。

- [ ] **Step 2: 安全传输**

  创建唯一 root-owned `0700` inbound；仅传 archive 与 manifest；远端重新计算 archive SHA，并精确校验
  manifest v2 字段、commit、filename、SHA。失败时不创建 candidate。

- [ ] **Step 3: 候选同版本 installer 预安装**

  从 approved archive 外部解出 builder/installer 到 root-only runner；使用 service Python `-B`、clean
  env、无 `--activate`、无 `--expected-current` 执行预安装。

- [ ] **Step 4: 候选完整性读回**

  验证 candidate commit、install record、source digest、`.bfl-release.env`、root/dir/file mode、owner、
  0 writable、0 symlink、0 special、0 `.git`、0 bytecode；再次执行 no-activate 复验。current/previous、
  Backend、units、timers、DB 快照必须未变化。

### Task 4: CAS 激活并重启 ECS Backend

**Files:**
- Remote mutable state limited to immutable pointers, revision log and Backend process state.

- [ ] **Step 1: 激活窗口 fresh gate**

  重新检查 expected current/previous、candidate exact、五 writer idle、timer 余量、Backend health、DB 快照、
  installed unit hash。任何漂移停止，不禁用 timer。

- [ ] **Step 2: 原子激活**

  使用候选 archive 同版本 external installer，显式 `--activate`、
  `--expected-current 5c5603a...` 与批准 archive SHA。权威结果以 pointer/revision 读回为准：
  `current=08645a8...`、`previous=5c5603a...`。

- [ ] **Step 3: 重启 Backend**

  不替换 unit、不 daemon-reload；只重启 `bond-factor-lab-backend.service`。验证 active/running/result、
  MainPID 更新、cwd 解析到 R3、release commit env 为 R3、loopback health 与 service fingerprint 一致。

- [ ] **Step 4: 激活后不变量**

  验证五 timer 仍 enabled/active/waiting，五 writer idle，installed unit hashes 不变，DB 关键表快照与
  migration history 未变化，candidate/source tree 仍只读且无 bytecode。清理精确 inbound 与本地临时
  worktree/build root；不得删除任何 release 目录。

### Task 5: 冻结并推送 R3 tag

**Files:** No repository content changes.

- [ ] **Step 1: tag 冲突预检**

  本地与远程均不得已有 `bfl-source-r3-20260821`；若存在且不精确指向 source commit，停止。

- [ ] **Step 2: 创建并推送 annotated tag**

  tag 精确指向 source commit `08645a8...`，message 记录 ECS gray R3 immutable source release；只推送该
  tag，不移动 `master`。

- [ ] **Step 3: 远程读回**

  验证 tag peeled commit 精确为 source commit；再次确认 remote develop 与 master authority。

### Task 6: 文档真实性与仓库治理

**Files:**
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/TODO.md`
- Modify: `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md`
- Modify: `deploy/README.md`
- Delete: `docs/superpowers/plans/2026-08-21-r3-ecs-promotion-plan.md`

- [ ] **Step 1: 更新精确现场事实**

  ECS current 更新为 R3/source commit，previous 更新为 5c，记录 archive/source digest/tag、Backend/timer/
  DB 激活不变量。Mac3 仍为 e692，域名/DB authority/Writer不变。删除“ECS current=5c/previous=e692”
  以及“remote develop 尚未同步”等陈旧表述。

- [ ] **Step 2: 更新回滚和后续计划**

  Mac3 下一夜间窗口目标改为 ECS 已验证 R3 archive；ECS previous=5c 已有 insert-only 语义，但 source-only
  rollback 仍不能撤销 R3 writer 后的 DB/runtime 状态。installed 四个 natural unit 的旧 manual env 引用
  继续作为独立后续项，不能误称已替换。

- [ ] **Step 3: 删除临时计划与冗余审计**

  删除本计划，确认 `docs/superpowers` 无其他文件后精确 rmdir；全仓扫描旧 commit/current/previous、旧
  archive、旧 tag、过时 tree 校验、一次性测试脚本/日志、重复 CURRENT/TODO 状态和悬空链接。只删除本次
  已完成计划和本次变更导致的陈旧表述，不重写必要历史证据。

- [ ] **Step 4: 文档测试、提交和最终推送**

  运行 docs/architecture/release/systemd 测试、Markdown link、cmp、diff-check；提交长期文档并再次非 force
  push `codex/develop`，远程读回精确等于本地 HEAD。

### Task 7: 最终独立验证

**Files:** No changes.

- [ ] **Step 1: 本地 fresh 全量**

  运行 full pytest、compileall、status、branch、remote develop/master/tag、净 diff 和计划目录 absent。

- [ ] **Step 2: 双主机只读状态**

  Mac3 必须仍为 e692、Backend healthy；ECS 必须为 R3/previous 5c、Backend healthy、五 timer waiting、
  五 writer idle。两端 production release 均无 `.git`。

- [ ] **Step 3: 整体审查**

  独立审查 source authority、artifact、preinstall、activation、tag、文档和清理证据；无未闭合
  Critical/Important 后才宣称阶段一和阶段二完成。
