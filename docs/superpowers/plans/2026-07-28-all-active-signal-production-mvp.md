# 全部 Active 方案实盘信号 MVP 实施计划

> **执行要求：** 每次只处理一个根因，先红测、后最小实现、再完整验证和
> 独立 commit。可并行的工作仅限独立 worktree 中的只读审查或不共享状态的
> 测试；生产写入和最终合并始终串行。

**目标：** 补齐真实信号缺口，并以现有 daily ledger 在当前 Mac Studio
上自动生产全部到期方案；首个合法ledger交易日的日频目标为
25 execution / 29 target在08:00前可见。2026-07-28是决策和目标日期，
不是允许倒签`scheduled_live`的承诺。

**架构：** 扩展现有 exact admission 和 daily policy，不新建调度队列。
Native/DataBridge generation 分别就绪后进入 2+2 执行池，所有新实盘结果
写 `scheduled_live`，由 ledger 完成幂等、原子提交、恢复和 SLA 验收。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy、APScheduler、MySQL 8.0、
launchd。

---

### Task 0：冻结基线和权威文档

**Files:**

- Create:
  `docs/superpowers/specs/2026-07-28-all-active-signal-production-mvp-design.md`
- Create:
  `docs/superpowers/plans/2026-07-28-all-active-signal-production-mvp.md`
- Modify: `docs/TODO.md`
- Modify: `docs/CURRENT_STATUS.md`
- Modify: `docs/superpowers/specs/README.md`
- Modify: `docs/superpowers/plans/README.md`
- Modify:
  `docs/superpowers/specs/2026-07-27-blackbox-scheduler-admission-mvp-design.md`
- Modify:
  `docs/superpowers/plans/2026-07-27-blackbox-scheduler-admission-mvp.md`

- [ ] 记录 44 target / 40 execution 和日频 25/29 的精确矩阵。
- [ ] 记录 canonical 回测完整、live 仅缺 29 点。
- [ ] 删除“gray 永远不能 scheduled”和“20+20 是 MVP 发布前置”的旧表述。
- [ ] 明确 20+20 仍是后续增强项，不构成统计可靠性声明。
- [ ] 文档契约测试结构化锁定：
  - 25/29身份矩阵；
  - 四个daily gray仅允许ledger；
  - 五个monthly gray只走自然频率recurring admission；
  - 最终admission在production identity生成且保留expiry；
  - scheduled起点不得早于实际ledger epoch。
- [ ] 运行文档门禁和完整 pytest。
- [ ] 提交：

```text
docs: define all-active signal production mvp
```

### Task 1：建立只读缺口计划器

**Files:**

- Create: `harness/signal_gap_plan.py`
- Modify: `harness/cli.py`
- Create: `tests/test_signal_gap_plan.py`
- Modify: `tests/test_harness_cli.py`

- [ ] 先写红测，覆盖 active Registry、canonical case builder、gray边界、
  target业务唯一键和 `SKIP_PRESENT`。
- [ ] 计划器必须在一个只读一致性事务中生成 versioned JSON plan 和 SHA。
- [ ] 计划器不得按简单工作日制造假缺口；必须复用日/周/月既有日期构造器。
- [ ] 输出 action：
  `SKIP_PRESENT|GRAY_LIVE_GAP|FULL_CANONICAL_RUN_REQUIRED|BLOCKED_NO_GENERATION`。
- [ ] 默认只读，禁止携带任何写库能力。
- [ ] 固化当前验收：backtest 10,309、live 1,314、open gap 29。
- [ ] 提交：

```text
feat: plan exact active signal gaps
```

### Task 2：建立按控制面隔离的 gray 自动准入

**Files:**

- Modify: `scheduler/blackbox_scheduler_admission.py`
- Modify: `scheduler/main.py`
- Modify: `tests/test_blackbox_scheduler_admission.py`
- Modify: `tests/test_scheduler_main.py`

- [ ] 红测证明四个exact daily gray只允许进入ledger daily policy。
- [ ] legacy scheduler、startup catch-up和scheduled wrapper继续拒绝daily
  gray，防止回退时重新注册per-scheme daily cron。
- [ ] 为未来monthly gray定义独立recurring admission能力，但本Task不
  开启自然月频任务。
- [ ] 未列入、version/runtime 漂移仍必须 fail-closed。
- [ ] gray业务标签保持不变，执行 phase 为 `scheduled_live`。
- [ ] `run_all_prediction_jobs` 和 `--run-once predictions` 必须经过同一
  admission，关闭既有 aggregate 绕过。
- [ ] Native、actual、health 和 watchdog 行为不变。
- [ ] 提交：

```text
feat: admit gray blackbox by scheduler control plane
```

### Task 3：扩展 daily policy 到 25/29

**Files:**

- Create: `deploy/daily_scheduler_policy_v2.json`
- Modify: `scheduler/daily_policy.py`
- Modify: `scheduler/daily_coordinator.py`
- Modify: `scheduler/capacity_candidate_runtime.py`
- Modify: `tests/test_daily_policy.py`
- Modify: `tests/test_daily_coordinator.py`
- Modify: `tests/test_capacity_candidate_runtime.py`

- [ ] 先红测当前 policy 无法接收 active daily 25/29。
- [ ] 新 policy 精确包含 17 Native + 8 Blackbox。
- [ ] Blackbox release offset 固定为
  `0/2/4/6/8/10/12/14`，全部绑定同日 generation。
- [ ] Native/V2池继续为2，retry继续只允许一次
  `TRANSIENT_INFRA`。
- [ ] policy/count/version、candidate fingerprint、Registry digest漂移均
  fail-closed。
- [ ] 提交：

```text
feat: expand daily ledger policy to twenty nine targets
```

### Task 4：证明 25/29 ledger 功能正确

**Files:**

- Modify: `scheduler/daily_runtime.py`
- Modify: `scheduler/repository.py`
- Modify: `tests/test_daily_coordinator_mvp_mysql.py`
- Create: `tests/test_daily_coordinator_25_29_mysql.py`
- Modify: `tests/test_repository_daily_ledger.py`

- [ ] 临时 MySQL 精确展开 25 item / 29 target。
- [ ] 17 Native和8 Blackbox各一个 winning attempt。
- [ ] 两个池各自最大并发2。
- [ ] 单项失败不阻塞其余24项。
- [ ] transient只重试一次；算法、数据、timeout不盲目重试。
- [ ] 重入不增加 run/prediction。
- [ ] 28/29在08:00永久 `BREACHED`，晚到不洗回 `MET`。
- [ ] 07:45除V2 start guardrail外，还精确投影全25 item的
  pending/running/failed和当前missing target。
- [ ] 提交：

```text
test: prove daily ledger handles twenty nine targets
```

### Task 5：调整完整 capacity admission 的最小证据

**Files:**

- Modify: `scheduler/capacity_attestation.py`
- Modify: `scheduler/capacity_admission.py`
- Modify: `scheduler/capacity_runtime_admission.py`
- Modify: `tests/test_capacity_attestation.py`
- Modify: `tests/test_capacity_admission.py`
- Modify: `tests/test_capacity_runtime_admission.py`

- [ ] 红测证明不再要求20+20或连续交易日。
- [ ] admission仍精确绑定Mac、MySQL、commit、policy、环境和scheme摘要。
- [ ] 必须有一次真实 forced-cold 25/29 observation。
- [ ] 硬验收为29/29、最后可见不晚于07:55、总耗时不超过85分钟、无
  partial/orphan/duplicate。
- [ ] observation/candidate任何漂移使 admission失效。
- [ ] admission直接进入`ADMITTED`，不引入临时/降级状态。
- [ ] 继续强制签名、`not_before/expires_at`（最长31天）、decision
  sequence和replay floor；不得取消expiry。
- [ ] 三个0629 compatibility方案逐项绑定只读连接资格、数据水位、
  version和结果契约，不能用聚合29/29替代。
- [ ] 文档/API不得把一次 observation描述为P95统计证明。
- [ ] 提交：

```text
fix: admit exact daily capacity from candidate replay
```

### Task 6：生产同构 migration 与控制面预检

**Files:**

- Modify only if red tests expose a defect:
  `migrations/runner.py`, `tests/test_migration_018_mysql.py`
- Evidence remains ignored and is not committed.

- [ ] 在临时MySQL 8.0.45从017演练018正常apply、重复执行和断连恢复。
- [ ] 验证ledger复合关系、started_at定义和生产关系摘要不变。
- [ ] 验证runtime/storage根目录权限、无symlink、空间充足。
- [ ] 验证backend、scheduler、v2-preflight installed mode可原子一致切换。
- [ ] 验证epoch operator在服务未静默时拒绝执行。
- [ ] 无代码变化时不制造空commit；发现根因时一个根因一个`fix:` commit。

### Task 7：补齐现有信号缺口

**Files:**

- Existing controlled Blackbox gate:
  `harness/gates/gray_backfill_gate.py`
- Create only for remaining Native weekly gap:
  `harness/gates/native_gray_gap_gate.py`
- Create: `tests/test_native_gray_gap_gate.py`
- Update: `docs/CURRENT_STATUS.md`

- [ ] 使用Task 1冻结plan，已有键全部`SKIP_PRESENT`。
- [ ] 当前DataBridge合法覆盖的12个Blackbox点逐点并行计算、串行原子写入。
- [ ] 新同日generation就绪后补其余13个Blackbox点。
- [ ] Native周频4点必须等待week/calendar契约修复并绑定合法Native
  generation；不得回退live DB。
- [ ] 全部补缺写`gray_live`，不得倒签`scheduled_live`。
- [ ] 最终open gap为0；若Native上游契约仍阻断，必须精确报告4点
  `BLOCKED_DATA_CONTRACT`，不得伪造完成。
- [ ] Gate实现提交：

```text
feat: add exact native live gap gate
```

- [ ] 数据写入不制造空代码commit。

### Task 8：同机真实 forced-cold 25/29 隔离 rehearsal

**Files:**

- Modify only if replay exposes one root cause.
- Evidence goes to ignored `reports/` or `outputs/`.

- [ ] 使用生产同构隔离MySQL、独立cache/root和真实算法。
- [ ] 06:30语义下构建Native generation并刷新同日DataBridge。
- [ ] 真实运行17 Native +8 Blackbox。
- [ ] 验证25 winning attempts、29 canonical predictions、29 accepted
  targets。
- [ ] 验证最后target投影不晚于07:55、总耗时不超过85分钟。
- [ ] 验证重入、重复触发、一次transient和进程清理。
- [ ] 生成隔离rehearsal evidence；不得把隔离MySQL identity签成最终生产
  admission。
- [ ] 任一项失败则停止生产切换；一个根因、一个测试、一个commit后重跑。

### Task 9：受控生产切换

**前置：** 需要独立生产部署/重启授权。

- [ ] 冻结操作前DB、代码、policy、installed plist和进程快照。
- [ ] 补齐当天可合法补齐的历史live缺口。
- [ ] 停止BFL backend、scheduler、v2-preflight和相关算法子进程。
- [ ] 确认没有BFL生产写入后完成一致性备份。
- [ ] 仅通过migration runner应用018。
- [ ] migration018后生成绑定精确生产MySQL identity的capacity
  candidate。
- [ ] 使用production audit-readonly source和受保护隔离sink执行
  production-identity execute-only observation；若现有工具不能表达双
  identity，必须先红测并实现该边界，禁止复用隔离candidate。
- [ ] 复核隔离rehearsal与production-identity observation，签发保留
  expiry的完整`ADMITTED` capacity artifact。
- [ ] 安装精确25/29 policy和上述production-bound admission。
- [ ] 原子发布ledger epoch并使三个BFL服务mode一致。
- [ ] 启动服务并验证只存在一个`daily:coordinator`，不存在per-scheme
  daily jobs；周/月自然cron仍在。
- [ ] 不修改、不停止BondProjectPro。
- [ ] 任一前置失败保持legacy；不得半切换。

### Task 10：首个合法 ledger 交易日日频生产验收

- [ ] 06:30后等待T-1数据齐备。
- [ ] Native和DataBridge输入独立构建；禁止旧generation fallback。
- [ ] 07:45检查25 item启动/完成和29 target缺失清单。
- [ ] 08:00数据库必须有：
  - 25个成功item；
  - 29个accepted target；
  - 29个`scheduled_live` canonical prediction；
  - SLA=`MET`。
- [ ] `/api/schemes`保持44 active。
- [ ] 29个日频metrics全部显示最新信号；没有actual的显示pending。
- [ ] 无重复prediction、orphan run、partial acceptance或legacy双写。
- [ ] 断言`scheduled_live`起点不早于实际machine-global ledger epoch；
  epoch之前的缺口只能是`gray_live`。
- [ ] 更新状态文档并提交：

```text
docs: record all-active daily production mvp acceptance
```

### Task 11：周频/月频后续收口

- [ ] 补齐周频5个历史live缺口；月频当前无缺口。
- [ ] 为五个monthly gray开启独立recurring admission；不得改变legacy
  daily或ledger daily选择语义。
- [ ] 到期时周频7/7、月频8/8自动写`scheduled_live`。
- [ ] 非到期日只检查最近一期，不新增记录。
- [ ] 最终每日巡检44 target，按频率验收到期集合。
- [ ] 20+20、长期归档、019/020和连续稳定性作为上线后增强项继续保留，
  不阻塞本MVP。

## 每个 commit 的固定门禁

1. 当前任务单文件测试；
2. 相关scheduler/repository/harness测试；
3. 对应显式临时MySQL测试，独占执行；
4. 完整pytest；
5. `compileall`和`git diff --check`；
6. production只读行数和rollout/admission边界复核；
7. 只读P0/P1审查；
8. 只暂存当前任务文件并提交；
9. 提交后确认工作区干净；
10. 未经明确授权不推送、部署、重启或修改BondProjectPro。
