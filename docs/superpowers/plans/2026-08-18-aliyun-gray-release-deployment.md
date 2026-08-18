# Aliyun Gray Release Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将单一源码候选 release 安全部署到阿里云 ECS，复用既有热缓存完成手工真写库验收，并在所有 systemd timer 继续关闭的条件下形成可审阅的灰度启动证据。

**Architecture:** 从活动集成分支的一个精确提交构建唯一、带 SHA-256 的 `git archive`，ECS 只接收 release bytes，不保存 Git checkout。部署目标固定为 `aliyun-gray`，discovery 只允许 56 个 base / 60 个 composite；安装与手工验收、timer 启用、Mac3 晋级是三道互不外推的授权门。

**Tech Stack:** Python 3.12/3.13、pytest、Git archive、SSH/SCP、systemd、FastAPI/Uvicorn、SQLAlchemy、MySQL 8.4、conda-forge。

---

## 0. 执行边界与当前基线

计划基线是 `codex/aliyun-db-clone-20260816@59c02c38b00b4271c7af84fe68caf04f899be81b`。实施 Task 1 后必须重新锁定最终提交，后续所有命令使用该最终提交，不把本基线 SHA 误当成最终 release SHA。

当前已验证事实：

- 本地 `master@2b62a2e9ae7c661f4a7f1741b5ff6c351819a4ad` 保持冻结，不合并、不移动、不推送。
- canonical 配置为 65 个 active base；`mac3-production` 为 65/69，`aliyun-gray` 为 56/60。
- ECS 的 9 个 Mac-only composite Registry 行保持 `paused`。
- ECS 三套 conda 环境、`cryptography`、MySQL、本地 DataBridge、热缓存重绑定、日/周/月预测与 Actuals 手工真写库已有历史通过证据。
- 当前 systemd service/timer 已安装，但五个项目 timer 必须在本计划 Task 1–6 全程保持 `disabled/inactive`。
- Backend 只监听 `127.0.0.1:8100`；不设置开机自启，不安装 Nginx/TLS，不修改 DNS、安全组、Mac3 或生产域名。
- 不增加 `MemoryMax`、`MemoryHigh`、Swap 或其它批次内存保护。
- 不做 Linux/macOS 数值等价验证，不重跑 CompareGate，不重建热缓存。

本计划存在两道独立生产授权：

1. **部署与手工写库授权**：允许上传 release、安装 service 模板、`daemon-reload`、原子切换 `current`、手工启动 service 和读回数据库；不允许启用 timer。
2. **自然灰度授权**：只在 Task 1–6 全部通过并再次取得用户明确授权后，允许启用五个 timer。

任何一步失败都停止后续写操作，保留现场证据并执行该任务定义的回滚。不得因为前一步获得授权而外推下一步权限。

## 1. 文件职责

- Modify `deploy/systemd/bond-factor-lab-data-bridge.service`：DataBridge one-shot 总时限 1 小时，停止宽限 5 分钟。
- Modify `deploy/systemd/bond-factor-lab-prediction-daily.service`：daily one-shot 总时限 2 小时，停止宽限 5 分钟。
- Modify `deploy/systemd/bond-factor-lab-prediction-weekly.service`：weekly one-shot 总时限 2 小时，停止宽限 5 分钟。
- Modify `deploy/systemd/bond-factor-lab-prediction-monthly.service`：monthly one-shot 总时限 2 小时，停止宽限 5 分钟。
- Modify `deploy/systemd/bond-factor-lab-actuals.service`：Actuals one-shot 总时限 1 小时，停止宽限 5 分钟。
- Modify `tests/test_systemd_control_plane.py`：锁定五个 one-shot 的精确时限、`KillMode=control-group`、无内存限制和 timer 继续 disabled-first 的契约。
- Modify `docs/operations/ALIYUN_MIGRATION_ASSESSMENT.md`：把本计划登记为 v1.27 当前执行入口。

不修改 Backend service 的启停策略，不修改任何 `.timer`，不修改方案 `timeout_sec`，不修改算法、Registry、数据库 schema、repository、executor、Harness 或 Mac3 launchd。

### Task 1: 给所有 ECS one-shot 设置总运行时限

**Files:**

- Modify: `tests/test_systemd_control_plane.py`
- Modify: `deploy/systemd/bond-factor-lab-data-bridge.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-daily.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-weekly.service`
- Modify: `deploy/systemd/bond-factor-lab-prediction-monthly.service`
- Modify: `deploy/systemd/bond-factor-lab-actuals.service`

- [ ] **Step 1: 写入失败契约测试**

在 `tests/test_systemd_control_plane.py` 增加精确断言：

```python
EXPECTED_ONE_SHOT_TIMEOUTS = {
    "bond-factor-lab-data-bridge.service": "1h",
    "bond-factor-lab-prediction-daily.service": "2h",
    "bond-factor-lab-prediction-weekly.service": "2h",
    "bond-factor-lab-prediction-monthly.service": "2h",
    "bond-factor-lab-actuals.service": "1h",
}


def test_systemd_one_shots_have_total_runtime_limits() -> None:
    for name, timeout in EXPECTED_ONE_SHOT_TIMEOUTS.items():
        text = (SYSTEMD_DIR / name).read_text(encoding="utf-8")
        assert f"TimeoutStartSec={timeout}" in text
        assert "TimeoutStopSec=300" in text
        assert "KillMode=control-group" in text
        assert "RuntimeMaxSec=" not in text
        assert "MemoryMax=" not in text
        assert "MemoryHigh=" not in text
```

- [ ] **Step 2: 运行测试并确认失败原因**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_systemd_control_plane.py::test_systemd_one_shots_have_total_runtime_limits
```

Expected: FAIL，因为现有五个 one-shot 仍为 `TimeoutStartSec=infinity`，且缺少 `TimeoutStopSec=300`。

- [ ] **Step 3: 只修改五个 one-shot service**

DataBridge 和 Actuals 使用：

```ini
TimeoutStartSec=1h
TimeoutStopSec=300
KillMode=control-group
```

daily、weekly、monthly 使用：

```ini
TimeoutStartSec=2h
TimeoutStopSec=300
KillMode=control-group
```

`Type=oneshot` 的整个运行阶段由 `TimeoutStartSec` 计时；不使用对 oneshot 无效的 `RuntimeMaxSec`。daily 到 2 小时仍未退出即判定算法效率或任务卡死，由 systemd 先发终止信号并在 300 秒后清理整个 control group。

- [ ] **Step 4: 验证 service 和 timer 契约**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q \
  tests/test_systemd_control_plane.py \
  tests/test_launchd_prediction_runner.py \
  tests/test_deployment_scope.py
```

Expected: PASS；timer 文件零变化，Backend service 零变化。

- [ ] **Step 5: 提交时限变更**

```bash
git status --short
git diff --check
git add tests/test_systemd_control_plane.py \
  deploy/systemd/bond-factor-lab-data-bridge.service \
  deploy/systemd/bond-factor-lab-prediction-daily.service \
  deploy/systemd/bond-factor-lab-prediction-weekly.service \
  deploy/systemd/bond-factor-lab-prediction-monthly.service \
  deploy/systemd/bond-factor-lab-actuals.service
git commit -m "deploy(systemd): bound gray one-shot runtimes"
```

提交前必须确认暂存区只包含五个 one-shot service 和对应测试，不能把 Backend、timer、输出或临时文件混入。

### Task 2: 锁定并构建唯一 release

**Files:**

- Read: repository tracked files at final `HEAD`
- Create outside repository: `/Users/macstudio0/bond-factor-lab-migration-docs/artifacts/releases/`

- [ ] **Step 1: 运行候选完整验证**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m pytest -q
for plist in deploy/launchd/*.plist; do plutil -lint "$plist"; done
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python -m json.tool \
  deploy/scheme_deployment_matrix_v1.json >/dev/null
cmp AGENTS.md CLAUDE.md
git diff --check
git status --short --branch
```

Expected: pytest 0 failed；所有 plist、JSON 和文档一致性检查通过；工作树干净。

- [ ] **Step 2: 重新读回三种 discovery 口径**

```bash
/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python - <<'PY'
import os
from scheduler.discovery import discover_schemes

for target in (None, "mac3-production", "aliyun-gray"):
    os.environ.pop("BFL_DEPLOYMENT_TARGET", None)
    if target is not None:
        os.environ["BFL_DEPLOYMENT_TARGET"] = target
    configs = discover_schemes(strict=True)
    print(target or "unscoped", len(configs), sum(len(c.tenors) for c in configs))
PY
```

Expected exactly:

```text
unscoped 65 69
mac3-production 65 69
aliyun-gray 56 60
```

- [ ] **Step 3: 生成确定性 archive 与摘要**

```bash
release_commit=$(git rev-parse HEAD)
release_root=/Users/macstudio0/bond-factor-lab-migration-docs/artifacts/releases
release_archive="$release_root/bond-factor-lab-${release_commit}.tar.gz"
mkdir -p "$release_root"
git archive --format=tar --prefix=bond-factor-lab/ "$release_commit" \
  | gzip -n -9 >"$release_archive"
(cd "$release_root" && shasum -a 256 "$(basename "$release_archive")" \
  >"$(basename "$release_archive").sha256")
if tar -tzf "$release_archive" \
  | rg '(^|/)(\.git|\.env|outputs|backtest_artifacts)(/|$)'; then
  exit 1
fi
test "$(tar -tzf "$release_archive" | rg '(^|/)reports/' | wc -l | tr -d ' ')" -eq 2
tar -tzf "$release_archive" | rg '(^|/)reports/README\.md$'
printf '%s\n' "$release_commit"
cat "${release_archive}.sha256"
```

Expected: archive 只含 tracked source；不含 `.git`、本地 `.env`、缓存、日志、`outputs/`、
`backtest_artifacts/` 或工作树未跟踪文件。`reports/` 只允许 tracked 的目录项与
`reports/README.md`，不得包含运行报告。最终 commit 和 SHA-256 写入部署记录。

- [ ] **Step 4: 建立不变的候选记录**

记录 final commit、archive 文件名、archive bytes、SHA-256、本地全量测试结果、生成时间和生成主机。记录中不得出现数据库 DSN、密码、Token、私钥内容或环境文件内容。

### Task 3: ECS 只读预检

**Files:**

- Read only: `/opt/bond-factor-lab/current`
- Read only: `/etc/systemd/system/bond-factor-lab-*`
- Read only: `/etc/bond-factor-lab/bond-factor-lab.env` metadata only
- Read only: `/var/lib/bond-factor-lab/cache-builds/`
- Read only: ECS MySQL Registry and recent run metadata

- [ ] **Step 1: 固定 SSH 身份并只读登录**

```bash
chmod 600 /Users/macstudio0/.ssh/finlab-key.pem
ssh -i /Users/macstudio0/.ssh/finlab-key.pem \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  root@47.103.45.193
```

连接前核对迁移评估中已经固定的 Host Key。不得使用 `StrictHostKeyChecking=no`。

- [ ] **Step 2: 读回主机、current、依赖和资源**

```bash
hostnamectl
timedatectl
readlink -f /opt/bond-factor-lab/current
find /opt/bond-factor-lab/current -maxdepth 1 -name .git -print
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -c \
  'import cryptography, sqlalchemy; print(cryptography.__version__, sqlalchemy.__version__)'
/opt/miniconda3/envs/bond_factor_lab_service/bin/python -m pip check
df -hT /
free -h
systemctl --failed --no-pager
systemctl is-active mysql
```

Expected: current 指向既有 immutable release；没有 `.git`；`cryptography` import 和 `pip check` 通过；MySQL active；资源无新增硬阻断。

- [ ] **Step 3: 读回 installed unit 和 timer 状态**

```bash
for unit in \
  bond-factor-lab-data-bridge.service \
  bond-factor-lab-prediction-daily.service \
  bond-factor-lab-prediction-weekly.service \
  bond-factor-lab-prediction-monthly.service \
  bond-factor-lab-actuals.service \
  bond-factor-lab-backend.service; do
  systemctl cat "$unit"
done
systemctl list-timers --all 'bond-factor-lab-*' --no-pager
for timer in \
  bond-factor-lab-data-bridge.timer \
  bond-factor-lab-prediction-daily.timer \
  bond-factor-lab-prediction-weekly.timer \
  bond-factor-lab-prediction-monthly.timer \
  bond-factor-lab-actuals.timer; do
  systemctl is-enabled "$timer" || test "$?" -eq 1
  systemctl is-active "$timer" || test "$?" -eq 3
done
```

Expected: 五个 timer 全部 disabled/inactive。不得读取或打印 `/etc/bond-factor-lab/bond-factor-lab.env` 内容。

- [ ] **Step 4: 读回 ECS discovery 和 Registry**

```bash
cd /opt/bond-factor-lab/current
BFL_DEPLOYMENT_TARGET=aliyun-gray \
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python - <<'PY'
from scheduler.discovery import discover_schemes

configs = discover_schemes(strict=True)
print(len(configs), sum(len(c.tenors) for c in configs))
PY

BFL_DATABASE_ENV_FILE=/etc/bond-factor-lab/bond-factor-lab.env \
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python - <<'PY'
from sqlalchemy import text
from scheduler.repository import create_engine_from_env

engine = create_engine_from_env()
with engine.connect() as connection:
    rows = connection.execute(text(
        "SELECT status, COUNT(*) AS count "
        "FROM t_scheme_registry GROUP BY status ORDER BY status"
    )).mappings().all()
    active_bases = connection.execute(text(
        "SELECT COUNT(DISTINCT base_scheme_id) FROM t_scheme_registry "
        "WHERE status='active'"
    )).scalar_one()
print([dict(row) for row in rows], int(active_bases))
PY
```

Expected: discovery `56 60`；Registry 为 60 active composite、9 paused composite、56 active base。若读回不一致，停止部署并先处理 Registry，不靠矩阵自动改数据库状态。

- [ ] **Step 5: 验证缓存挂载和既有重绑定闭包**

```bash
find /var/lib/bond-factor-lab/cache-builds -maxdepth 3 \
  -type f \( -name current.json -o -name manifest.json \) \
  -printf '%u %m %s %p\n' | sort
find /var/lib/bond-factor-lab/cache-builds \
  \( -name '*.lock' -o -name '.invalid-*' \) -print
```

Expected: 7 个 Liwei family 的 current/lineage 文件由当前运行用户拥有且不可组写/他写；没有活跃锁、`.invalid-*` 或重建进程。只验证既有 Linux input-state rebind，不再次运行迁移 rebind CLI。

### Task 4: 安装 release 和 service，但保持 timer 关闭

**Authorization gate:** 执行本任务前必须取得“部署与手工写库授权”。

**Files:**

- Create: `/opt/bond-factor-lab/releases/$release_commit/`
- Modify symlink: `/opt/bond-factor-lab/current`
- Replace: six `/etc/systemd/system/bond-factor-lab-*.service` files
- Do not modify: `/etc/systemd/system/bond-factor-lab-*.timer`

- [ ] **Step 1: 上传 archive 和摘要**

从本地重新读回 Task 2 的精确 final commit，并只上传该文件：

```bash
release_commit=$(git rev-parse HEAD)
release_root=/Users/macstudio0/bond-factor-lab-migration-docs/artifacts/releases
release_archive="$release_root/bond-factor-lab-${release_commit}.tar.gz"
ssh -i /Users/macstudio0/.ssh/finlab-key.pem \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  root@47.103.45.193 \
  'install -d -o root -g root -m 0700 /opt/bond-factor-lab/incoming /run/bond-factor-lab'
scp -i /Users/macstudio0/.ssh/finlab-key.pem \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  "$release_archive" "${release_archive}.sha256" \
  root@47.103.45.193:/opt/bond-factor-lab/incoming/
ssh -i /Users/macstudio0/.ssh/finlab-key.pem \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  root@47.103.45.193 \
  "cd /opt/bond-factor-lab/incoming && \
   sha256sum -c 'bond-factor-lab-${release_commit}.tar.gz.sha256' && \
   install -o root -g root -m 0600 /dev/null /run/bond-factor-lab/deployment-release.env && \
   printf 'release_commit=%s\\nrelease_archive=%s\\n' \
     '$release_commit' \
     '/opt/bond-factor-lab/incoming/bond-factor-lab-${release_commit}.tar.gz' \
     >/run/bond-factor-lab/deployment-release.env"
```

Expected: 远端 `sha256sum -c` 通过，并形成 root-only 的非敏感运行变量文件。失败时删除不完整 incoming 文件，不触碰 current。

- [ ] **Step 2: 解压到新的不可变 release 目录**

```bash
source /run/bond-factor-lab/deployment-release.env
release_dir="/opt/bond-factor-lab/releases/$release_commit"
test ! -e "$release_dir"
mkdir -m 0755 "$release_dir"
tar -xzf "$release_archive" --strip-components=1 -C "$release_dir"
test ! -e "$release_dir/.git"
test "$(find "$release_dir/schemes" -mindepth 2 -maxdepth 2 \
  -name config.yaml | wc -l)" -eq 65
```

Existing release 目录不得覆盖；若同 commit 目录已存在，先按 SHA 清单逐文件证明完全一致，否则停止。

- [ ] **Step 3: 备份 installed service 并安装六个 service 模板**

```bash
source /run/bond-factor-lab/deployment-release.env
release_dir="/opt/bond-factor-lab/releases/$release_commit"
backup_dir="/opt/bond-factor-lab/unit-backups/${release_commit}"
mkdir -m 0700 "$backup_dir"
cp -a /etc/systemd/system/bond-factor-lab-*.service "$backup_dir/"
for service in "$release_dir"/deploy/systemd/*.service; do
  install -o root -g root -m 0644 \
    "$service" "/etc/systemd/system/$(basename "$service")"
done
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/bond-factor-lab-*.service
```

Expected: verify 通过；五个 one-shot 读回精确总时限；Backend 没有 enable 动作；timer 文件和状态零变化。

- [ ] **Step 4: 原子切换 current**

```bash
source /run/bond-factor-lab/deployment-release.env
release_dir="/opt/bond-factor-lab/releases/$release_commit"
previous_release=$(readlink -f /opt/bond-factor-lab/current)
printf 'previous_release=%s\n' "$previous_release" \
  >>/run/bond-factor-lab/deployment-release.env
ln -s "$release_dir" /opt/bond-factor-lab/current.next
mv -Tf /opt/bond-factor-lab/current.next /opt/bond-factor-lab/current
readlink -f /opt/bond-factor-lab/current
```

Expected: current 精确指向 final commit 目录。把 `previous_release` 写入不含 Secret 的部署记录，用于回滚。

- [ ] **Step 5: 只重启 localhost Backend，不 enable**

```bash
systemctl restart bond-factor-lab-backend.service
systemctl is-enabled bond-factor-lab-backend.service || test "$?" -eq 1
systemctl is-active bond-factor-lab-backend.service
ss -lntp | rg '127\.0\.0\.1:8100'
curl --fail --silent http://127.0.0.1:8100/api/health
```

Expected: Backend active 且仅监听 loopback；service 不是 enabled；健康响应声明 systemd one-shot 控制面和 ECS 灰度目标。

- [ ] **Step 6: 再次读回 timer 和范围**

重复 Task 3 Step 3–4。Expected: 五个 timer 仍 disabled/inactive；新 current discovery 仍为 56/60；Registry 仍为 60 active、9 paused。

### Task 5: 先验证热缓存复用，再手工真写库

**Authorization gate:** 本任务属于“部署与手工写库授权”，但每个业务日期必须在执行前由只读日历查询确认。不得沿用过期日期或构造源数据。

- [ ] **Step 1: 固定验收开始水位**

先保存只读健康和缺口基线，不输出数据库凭据：

```bash
cd /opt/bond-factor-lab/current
acceptance_dir="/var/lib/bond-factor-lab/acceptance/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -o root -g root -m 0700 "$acceptance_dir"
BFL_DATABASE_ENV_FILE=/etc/bond-factor-lab/bond-factor-lab.env \
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python \
  scripts/check_production_daily_health.py \
  >"$acceptance_dir/daily-health-before.json" || test "$?" -eq 1
BFL_DATABASE_ENV_FILE=/etc/bond-factor-lab/bond-factor-lab.env \
  /opt/miniconda3/envs/bond_factor_lab_service/bin/python \
  scripts/report_signal_gaps.py --as-of "$(date +%F)" \
  >"$acceptance_dir/signal-gaps-before.json"
```

Expected: 两份 root-only JSON 可解析；warning 可以作为基线，CLI error 或不可解析输出会阻断后续写入。

- [ ] **Step 2: 手工发布当前 DataBridge**

```bash
systemctl start bond-factor-lab-data-bridge.service
systemctl show bond-factor-lab-data-bridge.service \
  -p Result -p ExecMainStatus -p ActiveEnterTimestamp -p InactiveEnterTimestamp
journalctl -u bond-factor-lab-data-bridge.service --since today --no-pager
```

Expected: `Result=success`、`ExecMainStatus=0`；新 generation 的 `refresh_date`、`business_digest` 和 ready gate 可读回；timer 未启动。

- [ ] **Step 3: 选择三个合法手工日期**

使用 ECS 本地日历和平台日期函数选择日期，并在同一 root shell 中保存三个变量：

```bash
mapfile -t manual_dates < <(
  BFL_DATABASE_ENV_FILE=/etc/bond-factor-lab/bond-factor-lab.env \
    /opt/miniconda3/envs/bond_factor_lab_service/bin/python - <<'PY'
from datetime import date, timedelta

from scheduler.repository import create_engine_from_env
from shared.calendar_service import get_calendar
from shared.prediction_context import (
    build_daily_live_context,
    build_monthly_live_context,
    build_weekly_live_context,
    is_weekly_signal_date,
)

calendar = get_calendar(create_engine_from_env())
today = date.today()

daily = None
weekly = None
monthly = None
for offset in range(0, 370):
    candidate = today - timedelta(days=offset)
    value = candidate.isoformat()
    if daily is None and calendar.covers(value) and calendar.is_trading_day(value):
        try:
            build_daily_live_context(calendar, value, horizon=5)
            daily = value
        except ValueError:
            pass
    if weekly is None and candidate.weekday() == 5 and calendar.covers(value):
        try:
            if is_weekly_signal_date(calendar, value):
                build_weekly_live_context(calendar, value)
                weekly = value
        except ValueError:
            pass
    if monthly is None and candidate.day == 15 and calendar.covers(value):
        try:
            build_monthly_live_context(calendar, value)
            monthly = value
        except ValueError:
            pass
    if daily and weekly and monthly:
        break

if not all((daily, weekly, monthly)):
    raise SystemExit("no complete covered manual-date set")
print(daily)
print(weekly)
print(monthly)
PY
)
test "${#manual_dates[@]}" -eq 3
daily_predict_date=${manual_dates[0]}
weekly_predict_date=${manual_dates[1]}
monthly_predict_date=${manual_dates[2]}
printf '%s\n' \
  "daily=$daily_predict_date" \
  "weekly=$weekly_predict_date" \
  "monthly=$monthly_predict_date" \
  | tee "$acceptance_dir/manual-dates.txt"
```

Expected: 三个日期都通过当前 ECS 日历与平台语义函数。找不到完整集合时停止，不用固定旧日期伪造成功。

- [ ] **Step 4: 手工执行 daily 并验证缓存命中**

```bash
install -o root -g root -m 0600 /dev/null /run/bond-factor-lab/manual-run.env
trap 'rm -f /run/bond-factor-lab/manual-run.env' EXIT
printf 'BFL_SYSTEMD_PREDICT_DATE=%s\n' "$daily_predict_date" \
  >/run/bond-factor-lab/manual-run.env
systemctl start bond-factor-lab-prediction-daily.service
rm -f /run/bond-factor-lab/manual-run.env
trap - EXIT
systemctl show bond-factor-lab-prediction-daily.service \
  -p Result -p ExecMainStatus -p ActiveEnterTimestamp -p InactiveEnterTimestamp
journalctl -u bond-factor-lab-prediction-daily.service --since today --no-pager
```

Expected:

- 总墙钟不超过 2 小时；exit 0。
- 7 个 Liwei family 走普通 cache-hit 路径，`training_calls=0`，不产生新 rebind generation，不发生全量训练。
- 只对 ECS 允许的 daily schemes 创建 run；3 个 Mac-only daily schemes 零新增 run/prediction。
- `t_scheme_runs`、`t_scheme_run_log` 和 `t_scheme_predictions` 均能按本次开始水位读回成功证据。

若任一 Liwei cache miss 或出现训练调用，立即停止后续 weekly/monthly，不通过“让它重建一次”绕过缓存问题。

- [ ] **Step 5: 手工执行 weekly 和 monthly**

分别把 `$weekly_predict_date`、`$monthly_predict_date` 写入同一个 root-only manual env 文件，单批执行并确保失败时也清理：

```bash
for cadence in weekly monthly; do
  if test "$cadence" = weekly; then
    selected_date=$weekly_predict_date
  else
    selected_date=$monthly_predict_date
  fi
  install -o root -g root -m 0600 /dev/null \
    /run/bond-factor-lab/manual-run.env
  trap 'rm -f /run/bond-factor-lab/manual-run.env' EXIT
  printf 'BFL_SYSTEMD_PREDICT_DATE=%s\n' "$selected_date" \
    >/run/bond-factor-lab/manual-run.env
  systemctl start "bond-factor-lab-prediction-${cadence}.service"
  rm -f /run/bond-factor-lab/manual-run.env
  trap - EXIT
  systemctl show "bond-factor-lab-prediction-${cadence}.service" \
    -p Result -p ExecMainStatus -p ActiveEnterTimestamp -p InactiveEnterTimestamp
done
```

每次启动后立即删除 manual env 并读回 unit Result、exit code、运行时长、run IDs、业务日期和 prediction keys。Expected: 每批不超过 2 小时；ECS 允许范围内成功；3 个 Mac-only weekly-average 和 3 个 Mac-only monthly schemes 零新增。

- [ ] **Step 6: 手工执行 Actuals**

```bash
systemctl start bond-factor-lab-actuals.service
systemctl show bond-factor-lab-actuals.service \
  -p Result -p ExecMainStatus -p ActiveEnterTimestamp -p InactiveEnterTimestamp
journalctl -u bond-factor-lab-actuals.service --since today --no-pager
```

Expected: 1 小时内 exit 0；daily/weekly/monthly actuals 水位按现有源数据正常推进或幂等保持，不构造缺失源数据。

- [ ] **Step 7: 数据库闭环和负面验收**

从 Task 5 Step 1 的开始水位读回：

- 本次 daily、weekly、monthly 的每个应运行 base 都有 terminal successful run。
- prediction 行与 run linkage 完整，同键重跑不产生重复业务键。
- Actuals 三类表的变化与源水位一致。
- 9 个 Mac-only base 在本次时间窗内新增 run 数和 prediction 数均为 0。
- Registry 仍为 60 active、9 paused。
- 所有 timer 仍 disabled/inactive。

任何一项不成立，本次部署验收失败，不进入自然灰度。

### Task 6: 回滚演练与部署验收记录

- [ ] **Step 1: 验证回滚材料完整**

```bash
source /run/bond-factor-lab/deployment-release.env
backup_dir="/opt/bond-factor-lab/unit-backups/${release_commit}"
test -d "$previous_release"
test -d "$backup_dir"
test "$(find "$backup_dir" -maxdepth 1 -name '*.service' | wc -l)" -eq 6
```

Expected: 旧 release 和六个旧 service 文件均可用。

- [ ] **Step 2: 定义失败时的回滚动作**

失败时按此顺序执行，不启用 timer：

```bash
systemctl stop bond-factor-lab-backend.service
cp -a "$backup_dir"/*.service /etc/systemd/system/
systemctl daemon-reload
ln -s "$previous_release" /opt/bond-factor-lab/current.rollback
mv -Tf /opt/bond-factor-lab/current.rollback /opt/bond-factor-lab/current
systemctl start bond-factor-lab-backend.service
```

回滚后重验 localhost 健康、五个 timer disabled/inactive、旧 current 和数据库状态。已经成功写入的幂等业务行不做手工 DELETE；若出现非幂等错误，停止并单独设计数据库恢复，不临场删除。

- [ ] **Step 3: 形成脱敏验收记录**

记录 final commit、archive SHA-256、新旧 current、installed unit hash、五个时限、timer 状态、56/60、Registry 60/9、每批 Result/exit/runtime/run IDs、缓存 7/7 hit 与 `training_calls=0`、Backend 健康、磁盘前后水位和是否回滚。

记录不得包含私钥、数据库凭据、完整环境、Token 或 DSN。

### Task 7: 自然灰度启用——独立后续授权，不属于当前部署执行

只有 Task 1–6 全部通过、验收记录经用户审阅，并取得新的明确授权后，才制定并执行 timer enable 变更。届时只允许启用：

```text
bond-factor-lab-data-bridge.timer
bond-factor-lab-prediction-daily.timer
bond-factor-lab-prediction-weekly.timer
bond-factor-lab-prediction-monthly.timer
bond-factor-lab-actuals.timer
```

Backend 继续不设置开机自启；Mac3、Nginx、DNS、域名和生产流量继续不变。自然灰度至少观察多个连续交易日、一次自然周频、一次自然月频及对应 Actuals；缓存 miss、超时、日期链错误、OOM、磁盘不足或 localhost 前端不可用都会中断稳定窗口。

灰度通过后，Mac3 只接收与 ECS 完全相同的 archive；Mac3 安装、launchd 替换和域名切换仍分别需要独立计划与授权，不创建第二条源码分支。
