#!/usr/bin/env python3
"""发布前实盘重放：在一次性 MySQL 中用生产数据副本真实重跑某日日频预测，并与生产当日结果逐条比对。

只读取 Mac3 生产库与运行目录（mysqldump、复制 DataBridge current），所有写入都发生在临时目录和一次性
MySQL 内，不触碰生产。用于在不等待自然窗口的情况下验证候选 release 的整条预测链路。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROD = Path("/Users/macstudio0/bond-factor-lab-production")
RUNTIME = PROD / "runtime"
MYSQL_BIN = Path("/usr/local/mysql-8.0.45-macos15-arm64/bin")
MYSQLDUMP = Path("/usr/local/mysql/bin/mysqldump")
SERVICE_PY = "/Users/macstudio0/miniconda3/envs/bond_factor_lab_service/bin/python"
CONDA = "/Users/macstudio0/miniconda3/bin/conda"
PLATFORM_TABLES = (
    "api_wind_date t_trade_calendar t_target_registry t_scheme_registry t_scheme_versions "
    "t_scheme_runs t_scheme_run_log t_scheme_predictions t_scheme_actuals t_scheme_weekly_actuals "
    "t_scheme_monthly_actuals t_scheme_period_average_actuals t_backtest_runs t_backtest_predictions "
    "t_backtest_monthly_metrics t_input_artifacts t_schema_migrations"
).split()
WIND_TABLES = (
    "api_wind_weekly api_wind_monthly api_wind_indicators_all api_wind_indicators_all_new "
    "api_wind_daily_indicators api_wind_weekly_indicators api_wind_monthly_indicators "
    "api_wind_derivative_weekly api_wind_derivative_monthly"
).split()

SOURCE_TABLES = ["api_wind_daily", "api_wind_derivative_daily"]  # W4 Native 方案读取的大体量源表（约 800MB）
_SOURCE_CONFIG = Path("/Users/macstudio0/.config/bond-factor-lab/source-runtime-db.json")

_CNF_SNIPPET = r"""
import os, sys
root, runtime, out = sys.argv[1:4]
sys.path.insert(0, root)
from scripts.run_launchd_release import load_service_environment
from shared.db_config import DatabaseConfig
cfg = DatabaseConfig.from_mapping(load_service_environment(runtime))
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write('[client]\nuser=%s\npassword="%s"\nhost=%s\nport=%s\ndefault-character-set=%s\n' % (cfg.user, cfg.password, cfg.host, cfg.port, cfg.charset))
print(cfg.database)
"""
_PREPARE_SNAPSHOT = r"""
import sys
sys.path.insert(0, sys.argv[1])
from scripts.refresh_data_bridge_current import check_current
from shared.data_bridge.refresh import DataBridgeRefreshConfig
from shared.input_artifacts import prepare_blackbox_generation_snapshot
cur = check_current(refresh_date=sys.argv[2])
prepare_blackbox_generation_snapshot(state=cur.state, dataset=cur.dataset, schema_path=DataBridgeRefreshConfig.from_env().schema_path)
"""


def _release_env(release: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (release / ".bfl-release.env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value.strip('"')
    return env


def _sql(port: int, statement: str) -> list[list[str]]:
    out = subprocess.run(
        [str(MYSQL_BIN / "mysql"), "--host=127.0.0.1", f"--port={port}", "--user=root", "-N", "-B", "bond_db", "-e", statement],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=(PROD / "current").resolve())
    parser.add_argument("--predict-date", required=True, help="要重放的日频预测日，必须等于 DataBridge current 的 refresh_date，且生产当日已有这些方案的预测")
    parser.add_argument("--scheme-id", action="append", required=True, dest="scheme_ids")
    parser.add_argument("--port", type=int, default=3396)
    parser.add_argument("--with-source-tables", action="store_true", help="同时导出大体量 Wind 源表并配置 W4 源库（重放 W4 Native 方案时需要）")
    parser.add_argument("--keep", action="store_true", help="结束后保留临时目录与 MySQL 以便排查")
    args = parser.parse_args(argv)
    release = args.release.resolve()
    current_refresh = json.loads((RUNTIME / "data-bridge" / "refresh" / "state.json").read_text(encoding="utf-8"))["refresh_date"]
    if current_refresh != args.predict_date:
        print(f"--predict-date {args.predict_date} 与 DataBridge current 的 refresh_date {current_refresh} 不一致：重放只能使用当前数据日", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="bfl-replay-", dir=Path.home() / ".cache"))
    port, mysqld = args.port, None
    in_list = ",".join(f"'{s}'" for s in args.scheme_ids)
    try:
        data, sock = work / "mysql", work / "mysql.sock"
        subprocess.run([str(MYSQL_BIN / "mysqld"), "--no-defaults", "--initialize-insecure", f"--datadir={data}", "--lower-case-table-names=2"], check=True, capture_output=True)
        mysqld = subprocess.Popen([
            str(MYSQL_BIN / "mysqld"), "--no-defaults", f"--datadir={data}", "--lower-case-table-names=2", "--bind-address=127.0.0.1",
            f"--port={port}", "--mysqlx=OFF", f"--socket={sock}", f"--pid-file={work / 'mysql.pid'}", f"--log-error={work / 'mysql.log'}",
            "--sql-mode=STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(90):
            if subprocess.run([str(MYSQL_BIN / "mysqladmin"), "--host=127.0.0.1", f"--port={port}", "--user=root", "ping"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        password = os.urandom(12).hex()
        root_cli = [str(MYSQL_BIN / "mysql"), "--host=127.0.0.1", f"--port={port}", "--user=root", "-e"]
        subprocess.run(root_cli + [f"CREATE DATABASE bond_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; CREATE USER 'bfl_replay'@'127.0.0.1' IDENTIFIED BY '{password}'; GRANT ALL ON bond_db.* TO 'bfl_replay'@'127.0.0.1';"], check=True)
        db_env = work / "db.env"
        db_env.write_text(f"BOND_DB_USER=bfl_replay\nBOND_DB_PASSWORD={password}\nBOND_DB_HOST=127.0.0.1\nBOND_DB_PORT={port}\nBOND_DB_NAME=bond_db\nBOND_DB_CHARSET=utf8mb4\n")
        db_env.chmod(0o600)

        # 1) 只读导出生产表并载入一次性库
        cnf = work / "prod.my.cnf"
        prod_db = subprocess.run([SERVICE_PY, "-I", "-B", "-c", _CNF_SNIPPET, str(release), str(RUNTIME), str(cnf)], check=True, capture_output=True, text=True).stdout.strip()
        dump = work / "prod.sql"
        with dump.open("wb") as handle:
            subprocess.run([str(MYSQLDUMP), f"--defaults-extra-file={cnf}", "--single-transaction", "--no-tablespaces", "--skip-triggers", "--set-gtid-purged=OFF", prod_db, *PLATFORM_TABLES, *WIND_TABLES, *(SOURCE_TABLES if args.with_source_tables else [])], check=True, stdout=handle)
        cnf.unlink()
        with dump.open("rb") as handle:
            subprocess.run([str(MYSQL_BIN / "mysql"), "--host=127.0.0.1", f"--port={port}", "--user=root", "bond_db"], check=True, stdin=handle)

        # 2) 存档生产当日结果并删除，让候选 release 重新产生
        _sql(port, f"CREATE TABLE _orig AS SELECT * FROM t_scheme_predictions WHERE predict_date='{args.predict_date}' AND scheme_id IN ({in_list});")
        expected = int(_sql(port, "SELECT COUNT(*) FROM _orig")[0][0])
        if expected == 0:
            print("生产库中这些方案当日没有预测，无法比对", file=sys.stderr)
            return 2
        _sql(port, f"DELETE FROM t_scheme_predictions WHERE predict_date='{args.predict_date}' AND scheme_id IN ({in_list});")
        base_run = int(_sql(port, "SELECT COALESCE(MAX(run_id),0) FROM t_scheme_runs")[0][0])

        # 3) 沙箱运行目录：复制 DataBridge current/状态/门禁，并按 producer 同一函数重新封存就绪快照
        sandbox = work / "runtime"
        (sandbox / "data-bridge" / "refresh").mkdir(parents=True)
        shutil.copytree(RUNTIME / "data-bridge" / "data", sandbox / "data-bridge" / "data")
        shutil.copy2(RUNTIME / "data-bridge" / "refresh" / "state.json", sandbox / "data-bridge" / "refresh" / "state.json")
        shutil.copytree(RUNTIME / "data-bridge" / "refresh" / "v2_scheduler_gate", sandbox / "data-bridge" / "refresh" / "v2_scheduler_gate")
        lock = sandbox / "data-bridge" / "refresh" / "refresh.lock"
        lock.touch(mode=0o600)
        env = {**os.environ, **_release_env(release)}
        env.update({
            "BFL_RUNTIME_ROOT": str(sandbox), "BFL_DATABASE_ENV_FILE": str(db_env), "BFL_DEPLOYMENT_TARGET": "mac3-production",
            "BOND_ALGO_CONDA_ENV": "forecast_env", "PYTHONNOUSERSITE": "1", "CONDA_EXE": CONDA,
            "PATH": "/Users/macstudio0/miniconda3/condabin:/Users/macstudio0/miniconda3/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        })
        if args.with_source_tables:
            source_dir = work / "source-cfg"
            source_dir.mkdir(mode=0o700)
            template = json.loads(_SOURCE_CONFIG.read_text(encoding="utf-8"))
            template.update({"user": "bfl_replay", "password": password, "host": "127.0.0.1", "port": port, "database": "bond_db"})
            target = source_dir / "source-runtime-db.json"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(template, handle)
            env["DAILY_0629_SOURCE_PYTHON"] = "/Users/macstudio0/miniconda3/envs/forecast_env/bin/python"  # 沙箱 HOME 与生产用户不同，显式指定
            env.update({"BFL_SOURCE_DB_CONFIG_ROOT": str(source_dir), "BFL_SOURCE_DB_CONFIG_PATH": str(target)})
        refresh_date = subprocess.run(
            [SERVICE_PY, "-I", "-B", "-c", "import json,sys;print(json.load(open(sys.argv[1]))['refresh_date'])", str(sandbox / "data-bridge/refresh/state.json")],
            check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run([SERVICE_PY, "-B", "-c", _PREPARE_SNAPSHOT, str(release), refresh_date], check=True, env=env, cwd=release)

        # 4) 以 launchd 同一入口真实运行；任一方案失败即停止（避免重试间隔）
        cmd = [SERVICE_PY, "-B", "-m", "scheduler.launchd_prediction_runner", "--cadence", "daily", "--predict-date", args.predict_date, "--algo-env", "forecast_env"]
        for scheme_id in args.scheme_ids:
            cmd += ["--scheme-id", scheme_id]
        runner_log = work / "runner.log"
        runner = subprocess.Popen(cmd, env=env, cwd=release, stdout=subprocess.PIPE, stderr=runner_log.open("w"), text=True)
        while runner.poll() is None:
            time.sleep(3)
            failed = _sql(port, f"SELECT scheme_id, LEFT(error_message,200) FROM t_scheme_runs WHERE run_id>{base_run} AND status='failed'")
            if failed:
                runner.kill()
                for row in failed:
                    print("FAILED", *row, file=sys.stderr)
                print("runner stderr tail:", *runner_log.read_text().splitlines()[-15:], sep="\n", file=sys.stderr)
                return 1

        # 5) 逐条比对
        diff = _sql(port, "SELECT o.scheme_id, o.target_tenor, o.target_date, o.predicted_direction, n.predicted_direction FROM _orig o "
                    "LEFT JOIN t_scheme_predictions n ON n.scheme_id=o.scheme_id AND n.target_tenor=o.target_tenor AND n.horizon=o.horizon AND n.target_date=o.target_date "
                    "WHERE NOT (o.predicted_direction <=> n.predicted_direction AND o.feature_date <=> n.feature_date AND o.predict_date <=> n.predict_date "
                    "AND o.scheme_version <=> n.scheme_version AND o.result_segment <=> n.result_segment)")
        print(f"replayed {expected} facts for {len(args.scheme_ids)} schemes: {'IDENTICAL' if not diff else 'DIFFERENT'}")
        for row in diff:
            print("DIFF", *row, file=sys.stderr)
        return 0 if not diff else 1
    finally:
        if mysqld is not None:
            subprocess.run([str(MYSQL_BIN / "mysqladmin"), "--host=127.0.0.1", f"--port={port}", "--user=root", "shutdown"], capture_output=True)
            mysqld.wait(timeout=60)
        if args.keep:
            print(f"kept {work}", file=sys.stderr)
        else:
            subprocess.run(["chmod", "-R", "u+w", str(work)], capture_output=True)
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
