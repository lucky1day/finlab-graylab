# 日频 legacy → daily_ledger 切换运行手册

**目标读者**：生产机 operator。
**目标**：把日频调度从 legacy 切到 daily_ledger，让 25 个日频方案（21 formal + 4
gray `ten_y_t5_*`）每天在同一套 ledger 调度下出信号，08:00 前完成。

本手册的命令与行号均已在生产完整副本上核实。凡涉及签名、root 发布 epoch、应用
migration 的步骤都是 operator 动作，无代码捷径——这是**正当的安全门禁，本手册的目的
是满足它，不是绕过**。

---

## 0. 为什么现在能切（背景）

生产 legacy 模式的三个后果：`MAX_CONCURRENCY=1` 串行队列、当天仅 06:00/06:35 两次
DataBridge 刷新、blackbox 按 `legacy_automatic` 过滤（4 个 gray `ten_y_t5_*` 因此不跑）。

ledger 协调器（06:30 coordinator、native 2 + V2 2 并发池、occurrence 账本、08:30
recovery）早已建成，被四道门禁拦住。本手册逐个解除。

**已实证、本次切换不需要做的两件事**：
- 4 个 gray `ten_y_t5_*` 在 `DAILY_LEDGER` 平面准入已是 True → **不需要 gray→formal**
- ledger 固定加载 `daily_scheduler_policy_v2.json`（25 item/29 target，已含 4 个
  ten_y_t5）→ **不需要切 policy 版本**

**为什么之前那次容量 rehearsal 超时（关键）**：`docs/CURRENT_STATUS.md` 记录的
2026-07-29 隔离 rehearsal 用**空缓存 forced-cold** 跑了 **95.8 分钟**，最后可见
08:02:44，`within_capacity_limit=false`。原因是 liwei 缓存全量冷重建（每个 family
20–40 分钟）。本轮工作把 liwei 缓存 bootstrap 到 schema 3 后，实测 **7/7 family 全部
命中缓存、零模型训练、约 80 秒/个**（见 S3）。**S3 的 bootstrap 是让容量门禁能过的
前提，不是可选优化。**

---

## 步骤总览与门禁映射

| 步 | 动作 | 解除的门禁 | 类别 |
|---|---|---|---|
| S1 | 前置核验 | — | 只读 |
| S2 | 应用 migration 018 | ② started_at NOT NULL | operator（写库） |
| S3 | storage 预检 + liwei 7 family bootstrap | ① 缓存全量冷重建 | operator（算法） |
| S4 | 钉值一致性核验 | ① 钉值过期 | 只读 |
| S5 | 证据采集 + 容量门禁评估 | ③ admission BLOCKED（证据） | operator |
| S6 | 双 CMS 签名仪式 | ③ admission BLOCKED（签名） | operator（root） |
| S7 | 7 个 storage roots（0700） | ledger 启动预检 | operator |
| S8 | 切换维护窗口 + 发布 genesis epoch | ④ epoch chain | operator（root） |
| S9 | 切换后验证 | — | 只读 |
| S10 | 回退（如需） | — | operator（root） |

**顺序不可换的硬约束**：S2 必须先于 S5。capacity candidate 的 `_validate_ledger_schema`
精确要求 `t_scheme_runs.started_at = datetime(6)/NULL/CURRENT_TIMESTAMP(6)`
（`scheduler/capacity_candidate_runtime.py`，`LEDGER_SCHEMA_VERSION="daily-ledger-schema-v018"`）。
**017 状态下 candidate 根本构造不出来**——这不是签名失效，是候选无法生成。

**前置**：以下 PR 必须已合入生产分支——`fix: derive liwei cache spec pins`（钉值重算
工具 + 已更新 v2 钉值与 admission 绑定）、`fix: fail closed when the ledger claim path
predates migration 018`（018 预检）。生产机必须 checkout 到 `git status --porcelain`
为空的明确 commit。

---

## S1. 前置核验（只读）

```bash
cd /Users/macstudio0/bond-factor-lab

# 1) 工作树必须干净、在预期 commit
git status --porcelain            # 必须无输出
git rev-parse HEAD

# 2) 服务现状：只有 backend 允许在线，scheduler/preflight 必须未加载
launchctl print gui/$(id -u)/com.bond-factor-lab.scheduler    2>&1 | grep -q "could not find" && echo "scheduler 未加载 ✓"
launchctl print gui/$(id -u)/com.bond-factor-lab.v2-preflight 2>&1 | grep -q "could not find" && echo "preflight 未加载 ✓"

# 3) rollout / admission 契约现状
python3 -c "import json;print('rollout mode =', json.load(open('deploy/daily_coordinator_rollout_v1.json'))['mode'])"   # legacy
python3 -c "import json;d=json.load(open('deploy/daily_capacity_admission_v2.json'));print('admission status =', d['status'], ' policy_sha256 =', d['policy_sha256'][:16])"
```

`--inspect-applying-018` 只用于已经留下 `018=APPLYING` 的中断恢复，不是普通
pending migration 的状态检查；不得在正常 017 状态下把它的 `UNSAFE` 输出误判为
数据库漂移。当前生产 migration 状态以 `docs/CURRENT_STATUS.md` 和获准窗口内的受控
只读核验为准，真正 apply 时 runner 还会再次 fail-closed 校验完整 history。

**S1.5 强制检查（钉值刷新的连带影响）**：`require_daily_capacity_admission` **先比
`policy_sha256`、后判 `status`**（`scheduler/capacity_admission.py`）。钉值刷新改变了
policy 字节，若 `daily_capacity_admission_v2.json.policy_sha256` 未同步更新，生产会报
`policy_sha256 mismatch` 而不是预期的 BLOCKED，把故障引偏。钉值 PR 已同步该绑定，此处
只需确认二者一致：

```bash
python3 - <<'PY'
import hashlib, json
digest = hashlib.sha256(open("deploy/daily_scheduler_policy_v2.json","rb").read()).hexdigest()
bound = json.load(open("deploy/daily_capacity_admission_v2.json"))["policy_sha256"]
print("一致 ✓" if digest == bound else f"不一致 ✗  policy={digest[:16]} admission={bound[:16]}")
PY
```

---

## S2. 应用 migration 018（operator，写库）

**必须先于 S5，并在获准的 maintenance 子窗口内执行。** S1 允许在线的 backend
也必须先 bootout；连同 scheduler、preflight、算法、refresh 和 API background
子进程一起核验为零 writer 后才能 apply。018 前向兼容，apply 和 postcondition
核验完成后可只恢复 legacy backend，scheduler/preflight 继续保持未加载。

identity 两个参数只能从受控只读 inspect JSON 或获准的只读 identity query 取得；
不得在运行手册中拼接 DSN，也不得把生产
database name、server UUID、DSN 或凭据写入文档、报告和 shell history。以下用交互
输入把值只保存在当前 shell 变量中：

```bash
# 先在获准的只读流程中核对 identity，再在提示符输入；输入值不进入 history。
read -r "DBNAME?expected database name: "
read -rs "SRVUUID?expected server UUID: "
echo

# 应用（--apply 是不可省略的写库授权）
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/apply_migrations.py --apply \
  --expected-database-name "$DBNAME" --expected-server-uuid "$SRVUUID"
unset DBNAME SRVUUID
```

若 runner 因断连留下 `018=APPLYING`，先 `--inspect-applying-018`（只读）再
`--recover-applying-018 --apply --state-digest <inspect 输出的 digest>` 加同样两个
identity 参数。详见 `deploy/README.md` 的 recovery 章节。

**验证**：`t_scheme_runs.started_at` 变为 `datetime(6) NULL DEFAULT CURRENT_TIMESTAMP(6)`。

**前向兼容**：018 只放宽可空性，legacy 的 writer 仍正常。**回退不需撤销 018。**

---

## S3. liwei 7 family 窗口外 bootstrap（operator，算法）

**必须先于 S5**（容量证据要在缓存已迁移的状态下采集）。本步先以服务环境执行
7 个 storage roots 的统一安全预检，确保 liwei cache root 在算法首次创建缓存前已经是
服务账号所有的精确 0700；S7 在切换窗口前再次执行同一预检。

预热清单来自代码派生（`scripts/prewarm_liwei_0616_phase_a_cache.py` 的
`PREWARM_SCHEMES`，逐 family 的 publisher）。正式手册只使用仓库提供的串行入口，
不在生产窗口临时编写并行脚本。

```bash
cd /Users/macstudio0/bond-factor-lab
set -a; . .env; set +a
export PYTHONNOUSERSITE=1
export BFL_SOURCE_DB_CONFIG_ROOT=/Users/macstudio0/.config/bond-factor-lab
export BFL_SOURCE_DB_CONFIG_PATH=$BFL_SOURCE_DB_CONFIG_ROOT/source-runtime-db.json

# 平台存储预检使用服务环境；会安全创建缺失根，但拒绝既有 owner/mode/symlink 漂移。
conda run --no-capture-output -n bond_factor_lab_service \
  python -c 'from shared.daily_storage_preflight import preflight_daily_storage as p; print(p().labels)'

# Native 算法只允许使用 forecast_env；串行入口不写业务库。
conda run --no-capture-output -n forecast_env \
  python scripts/prewarm_liwei_0616_phase_a_cache.py --predict-date <下一交易日>
```

**本机观察耗时（只供窗口预算，不替代 S5 的 attested evidence）**：

| family | 冷重建（首次 bootstrap） | bootstrap 后单日 |
|---|---|---|
| liwei_0616_10y_v61 | 42.5 min | 81.9 s（hit，训练 0） |
| liwei_0616_5y_allk10_auc_static/yearly/ic × 3 | 各 ~30 min | 各 ~82 s |
| liwei_0616_5y_v31 | 23.4 min | 81.5 s |
| liwei_0616_7y01_v31 | 4.4 min | 78.7 s |
| liwei_0616_7y03_v31 | 4.5 min | 82.1 s |

bootstrap 后 7 个串行合计约 9.5 分钟、全部
`hit`/`missing_dates=[]`/零训练。首次冷重建必须为串行官方入口预留充足维护窗口；
不得把未受控并行观察当作生产执行方式或容量门禁证据。

**验证**：每个 family 的 `current.json` 指向的 generation manifest
`input_state.schema_version == 3` 且含 `effective_auxiliary`；再跑一次任一 publisher
应得 `phase_a_cache.status=="hit"`、`build_mode=="hit"`、`missing_dates==[]`。

---

## S4. 钉值一致性核验（只读）

bootstrap 后确认 policy 钉值与实算一致（钉值 PR 已把 v2 更新为实算值；此处核验缓存
迁移后仍一致）：

```bash
conda run --no-capture-output -n forecast_env \
  python scripts/refresh_liwei_cache_spec_fingerprints.py --check
# 退出码 0 = 全部一致；非 0 不得继续
```

**必须在 `forecast_env`（算法环境）运行**——服务环境算出的钉值不同，工具会拒绝。
生产**禁止**用 `--write`（那会改 policy 字节并连带 admission 绑定，需重走容量准入）。

若不一致：说明缓存对应的算法配置或算法环境依赖变了。停止切换，回到代码线用
`--write` 重算并重走容量准入。

---

## S5. 证据采集 + 离线容量门禁评估（operator）

**S2、S3 完成后执行。** 采集一次真实 forced-cold execute-only observation，用离线
gate 评估。

**三种口径并列（治理由项目负责人裁定，本手册不替裁剪）**：

- **【文档口径】** `deploy/README.md` /
  `docs/architecture/DAILY_SIGNAL_SLA.md`：20 次 forced-cold +
  20 次真实 revision + 连续 10 交易日 25/25 + P95 ≤80 分钟。
- **【代码口径】** `scheduler/capacity_gate.py` 实际强制：`MIN_FORCED_COLD_SAMPLES=1`、
  `MIN_REVISION_SAMPLES=0`、`MIN_PRODUCTION_OBSERVATIONS=1`、`MAX_FORCED_COLD_MINUTES=85.0`；
  P95 **不评估**（`forced_cold_p95_claim="NOT_EVALUATED"`）。
- **【已批准口径】** `docs/CURRENT_STATUS.md`：用户已明确取消 20+20 与连续观察作为 MVP
  阻断，改为"一次真实 forced-cold 25/29 隔离 rehearsal + 生产 identity execute-only
  observation，要求 29/29、最后可见 ≤07:55、总耗时 ≤85 分钟；admission 保留签名和
  expiry"。**这份与代码口径吻合。**

```bash
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/evaluate_daily_capacity_gate.py <attested-evidence.json> \
    --expected-machine-id <本机 IOPlatformUUID> \
    --expected-policy-version daily-scheduler-policy-v2 \
    --expected-policy-sha256 <policy v2 的 sha256>
```

**两个参数陷阱**：
- `--expected-machine-id` CLI 默认是 `socket.gethostname()`，但 runtime 用
  **IOPlatformUUID** 且拒绝降级为 hostname → 必须显式传 UUID
  （`ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/{print $4}'`）。
- `--expected-policy-version` 必须是 `daily-scheduler-policy-v2`（README 示例写的 v1
  已过期）。

**关键预期**：bootstrap 后 forced-cold 端到端应远低于 85 分钟上限（此前空缓存 rehearsal
是 95.8 分钟，超限）。`<待生产回填>` 记录实际分钟数。

---

## S6. 双 CMS 签名仪式（operator，root）

capacity admission 的 `ADMITTED` 需要两套独立签名（collector 对完整 evidence、operator
对 canonical decision），两套 root-owned 专用 keychain 与 trust 配置，逐证书固定 DER
SHA-256，禁止同一签名者兼两角色。签名链在运行时路径强制
（`scheduler/capacity_admission.py` 的 `require_daily_capacity_admission` →
`MacOSCmsVerifier`），**无代码旁路**。

完整命令序列见 `deploy/README.md` 的容量准入章节；本步产出把
`daily_capacity_admission_v2.json` 从 `BLOCKED` 变为带 `decision_id`/`decision_sequence`/
有效期/双签名 URI 的 `ADMITTED`。

**槽位**：`candidate_fingerprint`、`evidence_uri`/`evidence_sha256`、collector 与
operator 证书 DER SHA-256 与 keychain 路径、有效期窗口、`admitted_by`。

---

## S7. 再次预检 7 个 ledger storage roots（operator，0700）

ledger 每个日频入口启动都会预检 7 个根
（`shared/daily_storage_preflight.py`，调用于 `scheduler/main.py`、
`scheduler/daily_runtime.py`）。要求：叶子根**恰好 0700**、属主为服务账号；祖先链
owner ∈ {root, 服务账号}、非 group/other 可写；全链无 symlink、无 `..`。

S3 已在 bootstrap 前执行同一统一预检；本步必须再次核验切换窗口当下状态。cache
模块自建目录用默认 umask
（通常 0755），自身只检查"非 group/world 可写"（0755 通得过），而 storage preflight
要求**精确 0700** 且**绝不 chmod 既有目录**；任一根发生漂移都必须停止切换。

```bash
cd /Users/macstudio0/bond-factor-lab
set -a; . .env; set +a
PYTHONNOUSERSITE=1 \
  conda run --no-capture-output -n bond_factor_lab_service \
  python -c 'from shared.daily_storage_preflight import preflight_daily_storage as p; print(p().labels)'
```

该预检会以 no-follow/openat 语义安全创建缺失目录；不会修复既有错误 owner、mode 或
symlink。若失败，停止切换，按错误 label 在获准维护流程中逐级核验并修复，禁止使用
会跟随 symlink 的通用 `mkdir -p`/`chmod` 循环批量处理这些根。

---

## S8. 切换维护窗口（operator，root）

在获准窗口一次完成。以下"已安装 plist"指 `~/Library/LaunchAgents/` 下的实际文件。

1. **停 legacy 三件套**：`launchctl bootout` backend / scheduler / v2-preflight，等待或
   终止 per-scheme scheduled-live、refresh、API background 子进程；确认无遗留。
2. **三份已安装 plist 的 `BOND_DAILY_COORDINATOR_MODE` 同时改为 `ledger`**，用
   `plutil`/`PlistBuddy` 逐份读回确认；此时服务仍停。仓库模板与
   `deploy/daily_coordinator_rollout_v1.json` 保持 legacy 不动。
3. **root 发布 genesis epoch**（`scripts/daily_coordinator_epoch_operator.py`，
   `--expected-current-epoch 0 --mode ledger`）。该工具的前置全部代码强制：三份
   LaunchAgent 已 bootout、三份已装 plist mode 全为 ledger、进程表无遗留、DB 无非终态
   occurrence；target mode=ledger 时还会重算 candidate 并验证当前签名 admission
   （S6 未完成则拒绝）。完整命令见 `deploy/README.md`。
4. **先起 backend，再只起一个 scheduler**；v2-preflight 保持 bootout 或验证其 ledger
   配置启动后返回 `disabled`。

---

## S9. 切换后验证（只读）

```bash
# 1) health 报 ledger
curl -fsS http://127.0.0.1:8100/api/health | python3 -m json.tool | grep -A3 daily_schedule
#   期望 mode=ledger、overall 非 not_enabled

# 2) 生产日频巡检（退出码 0=ok / 1=warning / 2=error）
cd /Users/macstudio0/bond-factor-lab && set -a && . .env && set +a
conda run --no-capture-output -n bond_factor_lab_service \
  python scripts/check_production_daily_health.py --coordinator-mode auto --predict-date "$(date +%F)"
echo "退出码 $?"

# 3) occurrence 基数（当日 25 item / 29 target）、heartbeat 唯一、DataBridge refresh owner 唯一
```

**注意**：`deploy/README.md` 与 `DAILY_SIGNAL_SLA.md` 里的 "21 item/25 target"、
"08:00 时 24/25 报 error" 是过期口径；当前 policy v2 与代码是 **25 item/29 target**、
**8 个 V2**（4 formal + 4 gray），应为 28/29 报 error。以代码/policy 为准。

**首日观察**：连续观察日批，25/25 出信号、最后可见 ≤07:55（目标 08:00 SLA）、无
`CACHE_PUBLISHER_REQUIRED` / 资格校验 / candidate 漂移异常。

---

## S10. 回退（operator，root，如需）

任一步失败回到 legacy 21 方案可用态：

1. 阻断 admin/operator recovery，bootout 三件套，确认无遗留进程。
2. 全部业务日期 occurrence/item 终态、无 running run、无未清孤儿。
3. 三份**已安装** plist 改回 `legacy` 并读回；仓库模板与 rollout 保持 legacy。
4. 若已发布 genesis epoch：用同一 root operator **追加更高 `N+1/legacy` epoch**
   （不得删 chain、不得复用旧 epoch）；若未发布：直接恢复。
5. `N+1` chain 校验通过后才起 legacy 服务。

**018 与 liwei 缓存前向兼容，回退不需撤销。**

---

## 附：本手册未回填的槽位（生产执行时填写）

服务 UID、`database_name`、`server_uuid`（只在窗口内 shell 变量，不落盘）、
IOPlatformUUID、7 路并行墙钟、**bootstrap 后 forced-cold 端到端分钟数（须 ≤85.0）**、
`candidate_fingerprint`/`evidence_uri`/`evidence_sha256`、`decision_id`/
`decision_sequence`/有效期/`admitted_by`、两个签名者证书 DER SHA-256 与 keychain 路径、
回滚变更单号、首日 accepted/expected。
