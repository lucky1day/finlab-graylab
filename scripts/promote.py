"""开发机上的发布流程：窗口保护 → 构建 → 预装 → 验证 → 切换（可带迁移）→ 回滚 → 清理 → 状态。

只固化已经手工验证过的步骤，不新增机制；每个会改变主机状态的子命令都先检查调度窗口，
切换以 `--expected-current` 为围栏，迁移前备份全部平台表并带库身份围栏。主机、路径与端口见
docs/operations/DEPLOYMENT_ACCESS.md；两端调度时刻与 deploy/ 下的模板一致。

    python -B scripts/promote.py status
    python -B scripts/promote.py window
    python -B scripts/promote.py observe [--date YYYY-MM-DD]       # 自然窗口之后读回两端 run 结果
    python -B scripts/promote.py build
    python -B scripts/promote.py preinstall --host all
    python -B scripts/promote.py validate
    python -B scripts/promote.py activate --host ecs [--migrate]     # 先灰度
    python -B scripts/promote.py activate --host mac3 [--migrate]
    python -B scripts/promote.py activate --host mac3 --commit <旧提交>   # 回滚：旧 release 须已构建；库 schema 领先时拒绝
    python -B scripts/promote.py cleanup --host all [--apply]
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path.home() / ".cache" / "bfl-releases"
TZ = ZoneInfo("Asia/Shanghai")
SSH = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
       "-o", "UserKnownHostsFile=/Users/macstudio0/.ssh/known_hosts", "-o", "ConnectTimeout=15",
       "-i", "/Users/macstudio0/.ssh/finlab-key.pem", "root@47.103.45.193"]
SCP = ["scp", "-q", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
       "-o", "UserKnownHostsFile=/Users/macstudio0/.ssh/known_hosts", "-i", "/Users/macstudio0/.ssh/finlab-key.pem"]
HOSTS = {
    "ecs": {
        "remote": True, "root": "/opt/bond-factor-lab", "runtime": "/var/lib/bond-factor-lab/state",
        "incoming": "/opt/bond-factor-lab/incoming", "backups": "/var/lib/bond-factor-lab/deployment-backups",
        "python": "/opt/miniconda3/envs/bond_factor_lab_service/bin/python", "port": 8100,
        "restart": "systemctl restart bond-factor-lab-backend.service",
        "backend_cwd": "ls -l /proc/$(systemctl show -p MainPID --value bond-factor-lab-backend.service)/cwd | awk '{print $NF}'",
        "last_runs": "for u in data-bridge prediction-daily prediction-weekly prediction-monthly actuals; do "
                     "echo $u $(systemctl show bond-factor-lab-$u.service -p Result,ExecMainStartTimestamp | tr '\\n' ' '); done",
    },
    "mac3": {
        "remote": False, "root": "/Users/macstudio0/bond-factor-lab-production",
        "runtime": "/Users/macstudio0/bond-factor-lab-production/runtime",
        "incoming": "/Users/macstudio0/bond-factor-lab-production/runtime/releases",
        "backups": "/Users/macstudio0/bond-factor-lab-production/runtime/backups",
        "python": "/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python", "port": 8101,
        "restart": "launchctl kickstart -k gui/501/com.bond-factor-lab.cutover-candidate.backend",
        "backend_cwd": "lsof -p $(lsof -nP -iTCP:8101 -sTCP:LISTEN -t | head -1) 2>/dev/null | awk '$4==\"cwd\"{print $9}'",
        "last_runs": "for j in data-bridge-refresh daily-predictions weekly-predictions monthly-predictions actuals; do "
                     "echo $j $(launchctl list com.bond-factor-lab.$j | grep LastExitStatus); done",
    },
}
# (小时, 分钟, 星期集合 Mon=0；None 为每天)：data-bridge、日/周/月预测、actuals，两端相同
TRIGGERS = [(6, 30, None), (7, 3, {0, 1, 2, 3, 4}), (8, 30, None), (11, 30, {5}), (18, 0, None), (19, 0, None), (23, 45, None)]
GUARD_BEFORE, GUARD_AFTER = timedelta(minutes=10), timedelta(minutes=60)


def _window_end(now: datetime) -> datetime | None:
    end = None
    for offset in (-1, 0, 1):
        day = (now + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        for hour, minute, weekdays in TRIGGERS:
            if weekdays is not None and day.weekday() not in weekdays:
                continue
            trigger = day.replace(hour=hour, minute=minute)
            if trigger - GUARD_BEFORE <= now < trigger + GUARD_AFTER:
                end = max(end or now, trigger + GUARD_AFTER)
    return end


def busy_until(now: datetime) -> datetime | None:
    """now 落在任一触发点的 [-10, +60] 分钟内时，返回连续保护期结束后的最早可操作时刻，否则 None。"""
    end = _window_end(now)
    while end is not None:
        following = _window_end(end)
        if following is None:
            return end
        end = following
    return None


class Host:
    def __init__(self, name: str) -> None:
        self.name, self.cfg = name, HOSTS[name]

    def sh(self, script: str, check: bool = True) -> str:
        command = [*SSH, script] if self.cfg["remote"] else ["bash", "-c", script]
        result = subprocess.run(command, capture_output=True, text=True)
        if check and result.returncode:
            raise SystemExit(f"[{self.name}] failed: {script[:120]}\n{(result.stderr or result.stdout).strip()[-600:]}")
        return result.stdout.strip()

    def put(self, files: list[Path], directory: str) -> None:
        self.sh(f"mkdir -p {shlex.quote(directory)}/installer")
        for path in files:
            if self.cfg["remote"]:
                subprocess.run([*SCP, str(path), f"root@47.103.45.193:{directory}/"], check=True)
            else:
                subprocess.run(["cp", str(path), directory], check=True)

    def release_ops(self, commit: str, *args: str) -> str:
        directory = f"{self.cfg['root']}/releases/{commit}"
        return self.sh(f"cd {directory} && {self.cfg['python']} -B scripts/release_ops.py {' '.join(shlex.quote(a) for a in args)}")

    def link(self, name: str) -> str | None:
        out = self.sh(f"readlink {self.cfg['root']}/{name}", check=False)
        return out.rsplit("/", 1)[-1] or None

    def health(self) -> str:
        port = self.cfg["port"]
        return self.sh(f"for p in /api/factor-lab/dashboard /api/factor-lab/dashboard /api/factor-lab/dashboard /; do curl -s -o /dev/null -w '%{{http_code}} ' http://127.0.0.1:{port}$p; sleep 1; done")


def hosts_for(selection: str) -> list[Host]:
    return [Host(name) for name in (HOSTS if selection == "all" else [selection])]


def built(commit: str | None) -> tuple[str, Path]:
    commit = commit or subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    directory = CACHE / commit
    if not (directory / f"{commit}.source.tar.gz").is_file():
        raise SystemExit(f"release {commit[:8]} not built; run `promote.py build` first")
    return commit, directory


VALIDATED_MARKER = "validated.json"


def require_validated(directory: Path, commit: str, skip: bool) -> None:
    """切换前必须有该提交的验证通过标记；防止把验证失败当成可继续（例如管道吞掉退出码）。"""
    if skip:
        return
    marker = directory / VALIDATED_MARKER
    try:
        recorded = json.loads(marker.read_text())
    except (OSError, ValueError):
        recorded = {}
    if recorded.get("commit") != commit or recorded.get("result") != "PASS":
        raise SystemExit(f"refusing: {commit[:8]} has no passing `promote.py validate` on record (use --skip-validation only with explicit authorization)")


def guard_window(force: bool) -> None:
    until = busy_until(datetime.now(TZ))
    if until and not force:
        raise SystemExit(f"refusing: a scheduled trigger is near; safe again at {until:%H:%M} (use --force only with explicit authorization)")


def cmd_status(args: argparse.Namespace) -> None:
    until = busy_until(datetime.now(TZ))
    print(f"now {datetime.now(TZ):%F %T} window: {'BUSY until %s' % until.strftime('%H:%M') if until else 'clear'}")
    for host in hosts_for("all"):
        print(f"== {host.name}: current {(host.link('current') or '-')[:8]} previous {(host.link('previous') or '-')[:8]}"
              f" | health {host.health()} | backend cwd {host.sh(host.cfg['backend_cwd']).rsplit('/', 1)[-1][:8]}")
        print("   releases:", host.sh(f"ls {host.cfg['root']}/releases").replace("\n", " ")[:200])
        print("   last natural runs:", *[f"\n     {line}" for line in host.sh(host.cfg["last_runs"]).splitlines()])


def cmd_observe(args: argparse.Namespace) -> None:
    """自然窗口之后读回两端的 run 结果：按阶段与状态计数、仍无成功 run 的方案及错误摘要、当日事实条数。"""
    day = args.date or datetime.now(TZ).strftime("%Y-%m-%d")
    for host in hosts_for("all"):
        current = host.link("current")
        print(f"== {host.name} (release {current[:8]}) predict_date {day}")
        print(host.release_ops(current, "probe", "runs", "--date", day))
        print("   last natural runs:", *[f"\n     {line}" for line in host.sh(host.cfg["last_runs"]).splitlines()])


def cmd_window(args: argparse.Namespace) -> None:
    until = busy_until(datetime.now(TZ))
    print("clear" if until is None else f"BUSY until {until:%H:%M}")
    raise SystemExit(0 if until is None else 3)


def cmd_build(args: argparse.Namespace) -> None:
    status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, check=True).stdout
    if status.strip():
        raise SystemExit("working tree has uncommitted tracked changes; commit first (release is built from HEAD)")
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    directory = CACHE / commit
    if not (directory / f"{commit}.source.tar.gz").is_file():
        subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "build_source_release.py"), "--output-dir", str(directory)], check=True, stdout=subprocess.DEVNULL)
    print(f"built {commit[:8]} archive_sha256 {_archive_sha(directory, commit)[:12]} -> {directory}")


def _archive_sha(directory: Path, commit: str) -> str:
    import hashlib
    return hashlib.sha256((directory / f"{commit}.source.tar.gz").read_bytes()).hexdigest()


def _install(host: Host, commit: str, directory: Path, extra: str = "") -> str:
    tag = f"{host.cfg['incoming']}/promote-{commit[:8]}"
    archive, manifest = directory / f"{commit}.source.tar.gz", directory / f"{commit}.manifest.json"
    host.put([archive, manifest], tag)
    host.sh(f"cd {tag} && tar -xzf {archive.name} -C installer")
    return host.sh(
        f"cd {tag}/installer/source && {host.cfg['python']} -I scripts/install_source_release.py --manifest ../../{manifest.name} "
        f"--archive ../../{archive.name} --expected-archive-sha256 {_archive_sha(directory, commit)} "
        f"--deploy-root {host.cfg['root']} --runtime-root {host.cfg['runtime']} {extra}")


def cmd_preinstall(args: argparse.Namespace) -> None:
    commit, directory = built(args.commit)
    for host in hosts_for(args.host):
        print(f"[{host.name}] preinstall:", json.loads(_install(host, commit, directory).splitlines()[-1])["status"])


def cmd_validate(args: argparse.Namespace) -> None:
    """Mac3 上用候选 release 真实重放当日预测，并与当前 release 同时刻比较 Dashboard 摘要。"""
    commit, _ = built(args.commit)
    mac = Host("mac3")
    release = f"{mac.cfg['root']}/releases/{commit}"
    refresh = mac.release_ops(commit, "probe", "refresh-date")
    schemes = args.scheme_id or ["daily_10y_lgbm_10y04_0629", "v8_daily_3y_t5", "daily_t5_3y_position_unwind_lag1_v3"]
    env = "CONDA_EXE=/Users/macstudio0/miniconda3/bin/conda PATH=/Users/macstudio0/miniconda3/bin:$PATH BFL_DEPLOYMENT_TARGET=mac3-production"
    flags = " ".join(f"--scheme-id {s}" for s in schemes)
    out = mac.sh(f"cd {release} && {env} {mac.cfg['python']} -I {ROOT}/scripts/replay_live_prediction.py --release . "
                 f"--predict-date {refresh} --with-source-tables {flags} 2>&1 | tail -3", check=False)
    print("replay:", out.splitlines()[-1] if out else "(no output)")
    replay_ok = "IDENTICAL" in out
    cur = mac.link("current")
    digest = {}
    for label, release_commit in (("candidate", commit), ("current", cur)):
        try:
            digest[label] = mac.release_ops(release_commit, "probe", "dashboard-digest", "--out", f"/tmp/promote-{label}.json").split()[-1]
        except SystemExit:
            digest[label] = "unavailable"
    print("dashboard digest:", digest)
    same = digest["candidate"] == digest["current"] != "unavailable"
    print("RESULT", "PASS" if replay_ok and same else "FAIL (replay identical: %s, digest identical: %s)" % (replay_ok, same))
    marker = CACHE / commit / VALIDATED_MARKER
    if replay_ok and same:
        marker.write_text(json.dumps({"commit": commit, "result": "PASS", "at": datetime.now(TZ).isoformat(timespec="seconds")}))
    else:
        marker.unlink(missing_ok=True)
    raise SystemExit(0 if replay_ok and same else 1)


def _migrate(host: Host, commit: str, stamp: str) -> None:
    state = host.release_ops(commit, "probe", "migration-state").split()
    applied, latest = int(state[1]), int(state[3])
    if applied >= latest:
        print(f"[{host.name}] no pending migration (applied {applied})")
        return
    name, uuid = host.release_ops(commit, "probe", "db-identity").split()
    backup = f"{host.cfg['backups']}/promote-{commit[:8]}-{stamp}"
    print(f"[{host.name}] backup:", host.release_ops(commit, "probe", "backup-tables", "--out", backup))
    host.release_ops(commit, "script", "apply_migrations.py", "--", "--apply", "--expected-database-name", name, "--expected-server-uuid", uuid)
    print(f"[{host.name}] migrated {applied} -> {latest}")


def cmd_activate(args: argparse.Namespace) -> None:
    guard_window(args.force)
    commit, directory = built(args.commit)
    require_validated(directory, commit, args.skip_validation)
    host = Host(args.host)
    previous_current = host.link("current")
    if previous_current == commit:
        raise SystemExit(f"[{host.name}] {commit[:8]} is already current")
    if not host.sh(f"test -d {host.cfg['root']}/releases/{commit} && echo yes", check=False):
        _install(host, commit, directory)
    state = host.release_ops(commit, "probe", "migration-state").split()
    applied, latest = int(state[1]), int(state[3])
    if applied > latest:  # 回滚到不认识新 schema 的旧 release
        raise SystemExit(f"[{host.name}] database schema v{applied} is ahead of release {commit[:8]} (v{latest}); "
                         f"restore the platform tables from the latest backup under {host.cfg['backups']} first")
    if applied < latest and not args.migrate:
        raise SystemExit(f"[{host.name}] release has pending migrations (v{applied} -> v{latest}); rerun with --migrate")
    if args.migrate:
        _migrate(host, commit, datetime.now(TZ).strftime("%Y%m%dT%H%M%S"))
    print(f"[{host.name}] activate:", json.loads(_install(host, commit, directory, f"--activate --expected-current {previous_current}").splitlines()[-1])["status"])
    host.sh(host.cfg["restart"])
    time.sleep(10)
    cwd = host.sh(host.cfg["backend_cwd"]).rsplit("/", 1)[-1]
    health = host.health()
    ok = host.link("current") == commit and cwd == commit and health.split()[:3] == ["401", "401", "401"] and health.split()[3:] == ["200"]
    print(f"[{host.name}] current {host.link('current')[:8]} cwd {cwd[:8]} health {health} -> {'OK' if ok else 'CHECK FAILED'}")
    raise SystemExit(0 if ok else 1)


def cmd_cleanup(args: argparse.Namespace) -> None:
    for host in hosts_for(args.host):
        keep = {host.link("current"), host.link("previous")}
        extra = [name for name in host.sh(f"ls {host.cfg['root']}/releases").split() if name not in keep]
        print(f"[{host.name}] keep {[k[:8] for k in keep if k]}; remove {[e[:8] for e in extra]}")
        if args.apply:
            for name in extra:
                host.sh(f"chmod -R u+w {host.cfg['root']}/releases/{name} && rm -rf {host.cfg['root']}/releases/{name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("status", cmd_status), ("window", cmd_window), ("build", cmd_build)):
        sub.add_parser(name).set_defaults(func=func)
    observe = sub.add_parser("observe")
    observe.set_defaults(func=cmd_observe)
    observe.add_argument("--date", help="预测日 YYYY-MM-DD，缺省为今天")
    for name, func in (("preinstall", cmd_preinstall), ("validate", cmd_validate), ("activate", cmd_activate),
                       ("cleanup", cmd_cleanup)):
        item = sub.add_parser(name)
        item.set_defaults(func=func)
        item.add_argument("--commit")
        if name in ("preinstall", "cleanup"):
            item.add_argument("--host", choices=["ecs", "mac3", "all"], default="all")
        if name == "activate":
            item.add_argument("--host", choices=["ecs", "mac3"], required=True)
            item.add_argument("--force", action="store_true", help="跳过调度窗口保护（须有明确授权）")
            item.add_argument("--skip-validation", action="store_true", help="跳过\"已验证\"标记检查（须有明确授权，例如回滚到已运行过的旧 release）")
        if name == "activate":
            item.add_argument("--migrate", action="store_true", help="先备份全部平台表并应用候选 release 的待执行迁移")
        if name == "validate":
            item.add_argument("--scheme-id", action="append")
        if name == "cleanup":
            item.add_argument("--apply", action="store_true", help="实际删除；缺省只列出")
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
