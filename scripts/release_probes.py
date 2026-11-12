"""release_ops.py probe 的只读探针，只在已绑定目标环境的子进程里运行。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def refresh_date() -> None:
    """打印本机 DataBridge current 的 refresh_date（回测 --predict-date 与重放的数据日）。"""
    state = Path(os.environ["BFL_RUNTIME_ROOT"]) / "data-bridge" / "refresh" / "state.json"
    print(json.loads(state.read_text(encoding="utf-8"))["refresh_date"])


def facts(scheme_ids: list[str], since: str) -> None:
    """读回方案自 since 起的预测事实与当日 run，核对方向、exact、分区。"""
    from sqlalchemy import text

    from scheduler.repository import create_engine_from_env

    with create_engine_from_env().connect() as connection:
        for scheme_id in scheme_ids:
            rows = connection.execute(
                text(
                    "SELECT target_tenor, horizon, feature_date, predict_date, target_date, "
                    "predicted_direction, scheme_version, result_segment FROM t_scheme_predictions "
                    "WHERE scheme_id=:s AND predict_date>=:d ORDER BY predict_date, target_date"
                ),
                {"s": scheme_id, "d": since},
            ).fetchall()
            print(f"# {scheme_id}: {len(rows)} facts")
            for row in rows:
                print(*row, sep=" | ")
            runs = connection.execute(
                text(
                    "SELECT run_id, status, predict_date, LEFT(error_message, 200) FROM t_scheme_runs "
                    "WHERE scheme_id=:s AND predict_date>=:d ORDER BY run_id"
                ),
                {"s": scheme_id, "d": since},
            ).fetchall()
            print(f"# runs: {len(runs)}")
            for row in runs:
                print(*row, sep=" | ")


def dashboard_digest(out: Path) -> None:
    """构建 Dashboard 摘要与各方案最近一个月 Detail，去掉随机字段后写 JSON 并打印摘要哈希。

    同一时刻在同一库上对新旧 release 各运行一次并比较输出，用于证明展示结果不变。
    """
    from backend.factor_lab_dashboard import (
        build_factor_lab_dashboard,
        build_factor_lab_dashboard_detail,
        encode_canonical_snapshot,
    )
    from scheduler.repository import create_engine_from_env

    engine = create_engine_from_env()

    def clean(payload):
        payload = json.loads(encode_canonical_snapshot(payload).raw_body)
        if isinstance(payload, dict):
            payload.pop("snapshot_id", None)
            payload.pop("generated_at", None)
        return payload

    summary = clean(build_factor_lab_dashboard(engine))
    details = {}
    for scheme in summary["schemes"]:
        months = sorted({row[0] for row in scheme["monthly_rows"] if row and row[0]})
        for month in months[-1:]:
            key = f"{scheme['scheme_id']}|{month}"
            try:
                details[key] = clean(build_factor_lab_dashboard_detail(engine, scheme_id=scheme["scheme_id"], month=month, source="all"))
            except Exception as exc:  # 逐项记录，便于比较两个 release
                details[key] = f"ERROR {type(exc).__name__}"
    blob = json.dumps({"summary": summary, "details": details}, sort_keys=True, ensure_ascii=False)
    out.write_text(blob, encoding="utf-8")
    errors = sum(isinstance(value, str) for value in details.values())
    print(f"schemes {len(summary['schemes'])} details {len(details)} errors {errors} sha256 {hashlib.sha256(blob.encode()).hexdigest()[:16]}")


def db_identity() -> None:
    """打印本机库名与 server_uuid，供迁移的写入围栏参数使用（运行时输出，不入文档）。"""
    from sqlalchemy import text

    from scheduler.repository import create_engine_from_env

    with create_engine_from_env().connect() as connection:
        name, uuid = connection.execute(text("SELECT DATABASE(), @@server_uuid")).one()
    print(name, uuid)


def migration_state() -> None:
    """打印库内已应用的最大迁移版本与本 release manifest 的最新版本；前者小于后者即有待应用迁移。"""
    from sqlalchemy import text

    from scheduler.repository import create_engine_from_env

    manifest = json.loads((Path(__file__).resolve().parent.parent / "migrations" / "release_manifest.json").read_text(encoding="utf-8"))
    latest = max([manifest["baseline"]["through_version"], *(item["version"] for item in manifest["migrations"])])
    with create_engine_from_env().connect() as connection:
        applied = connection.execute(text("SELECT MAX(version) FROM t_schema_migrations WHERE state = 'APPLIED'")).scalar()
    print(f"applied_max {applied} manifest_latest {latest}")


def backup_tables(out: Path) -> None:
    """用 0600 临时 cnf 备份全部平台 t_* 表（结构+数据，不含 api_* 大表）到 out，并写 SHA256SUMS。"""
    import gzip
    import shutil
    import subprocess
    import tempfile

    from sqlalchemy import text

    from scheduler.repository import create_engine_from_env
    from shared.db_config import DatabaseConfig

    config = DatabaseConfig.from_env()
    with create_engine_from_env().connect() as connection:
        tables = [row[0] for row in connection.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
            "AND table_type = 'BASE TABLE' AND table_name LIKE 't\\_%' ORDER BY table_name"))]
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    dump = shutil.which("mysqldump") or "/usr/local/mysql/bin/mysqldump"
    target = out / "platform_tables.sql.gz"
    with tempfile.TemporaryDirectory() as tmp:
        cnf = Path(tmp) / "client.cnf"
        descriptor = os.open(cnf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write('[client]\nuser=%s\npassword="%s"\nhost=%s\nport=%s\ndefault-character-set=%s\n' % (
                config.user, config.password, config.host, config.port, config.charset))
        result = subprocess.run(
            [dump, f"--defaults-extra-file={cnf}", "--single-transaction", "--no-tablespaces", "--skip-triggers",
             "--set-gtid-purged=OFF", config.database, *tables],
            stdout=subprocess.PIPE, check=True)
    with gzip.open(target, "wb") as handle:
        handle.write(result.stdout)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (out / "SHA256SUMS").write_text(f"{digest}  {target.name}\n")
    os.chmod(target, 0o600)
    os.chmod(out / "SHA256SUMS", 0o600)
    print(f"{out} tables {len(tables)} bytes {target.stat().st_size} sha256 {digest[:12]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", choices=["refresh-date", "facts", "dashboard-digest", "db-identity", "migration-state", "backup-tables"])
    parser.add_argument("--scheme-id", action="append", default=[])
    parser.add_argument("--since", default="1970-01-01")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.name == "refresh-date":
        refresh_date()
    elif args.name == "facts":
        if not args.scheme_id:
            parser.error("facts requires --scheme-id")
        facts(args.scheme_id, args.since)
    elif args.name == "db-identity":
        db_identity()
    elif args.name == "migration-state":
        migration_state()
    else:
        if args.out is None:
            parser.error(f"{args.name} requires --out")
        (dashboard_digest if args.name == "dashboard-digest" else backup_tables)(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
