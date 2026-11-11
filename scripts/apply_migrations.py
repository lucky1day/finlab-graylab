from __future__ import annotations

import argparse as _argparse
import json as _json
from pathlib import Path as _Path
import re as _re
import sys as _sys
from typing import Iterable as _Iterable

from sqlalchemy import text as _text


PROJECT_ROOT = _Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(PROJECT_ROOT))

from migrations.runner import (  # noqa: E402
    MIGRATIONS_DIR,
    RELEASE_MIGRATION_MANIFEST_PATH,
    apply_pending_migration_files,
    inspect_applying_migration,
    recover_applying_migration,
    validate_release_migration_manifest,
)
from scheduler.repository import create_engine_from_env  # noqa: E402


def _parse_args(
    argv: _Iterable[str] | None = None,
) -> _argparse.Namespace:
    """解析 apply / 只读 inspect / 带围栏 recovery 三种模式。"""
    parser = _argparse.ArgumentParser(
        description=(
            "Apply or inspect Bond Factor Lab schema migrations. "
            "Write paths require explicit --apply authorization."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inspect-applying",
        action="store_true",
        help=(
            "read the interrupted (APPLYING) migration and its state digest "
            "using SELECT plus the migration named lock only; no DDL/DML"
        ),
    )
    mode.add_argument(
        "--recover-applying",
        action="store_true",
        help=(
            "re-run the single APPLYING migration when it is declared "
            "rerunnable; requires --apply and the digest from --inspect-applying"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="explicitly authorize a database-changing mode",
    )
    parser.add_argument(
        "--state-digest",
        help="64-hex digest returned by --inspect-applying",
    )
    parser.add_argument(
        "--expected-database-name",
        help="exact DATABASE() value required by database-changing modes",
    )
    parser.add_argument(
        "--expected-server-uuid",
        help="exact lowercase canonical @@server_uuid required by writes",
    )
    args = parser.parse_args(argv)
    if args.inspect_applying:
        if args.apply or args.state_digest:
            parser.error(
                "read-only inspection does not accept --apply or --state-digest"
            )
        return args
    if args.recover_applying:
        if not args.apply or not args.state_digest:
            parser.error("recovery requires both --apply and --state-digest")
        if _re.fullmatch(r"[0-9a-f]{64}", args.state_digest) is None:
            parser.error("--state-digest must be 64 lowercase hex characters")
    elif args.state_digest:
        parser.error("--state-digest is only valid with --recover-applying")
    elif not args.apply:
        parser.error("--apply is required to change the database")
    if (
        not args.expected_database_name
        or not args.expected_database_name.strip()
        or not args.expected_server_uuid
    ):
        parser.error(
            "write database identity requires non-empty "
            "--expected-database-name and --expected-server-uuid"
        )
    if (
        _re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            args.expected_server_uuid,
        )
        is None
    ):
        parser.error("--expected-server-uuid must be a canonical lowercase UUID")
    return args


def _assert_write_database_identity(
    engine,
    *,
    expected_database_name: str,
    expected_server_uuid: str,
) -> None:
    """在任何 migration 写操作前精确核验连接目标。"""
    with engine.connect() as connection:
        database_name, server_uuid = connection.execute(
            _text("SELECT DATABASE(), @@server_uuid")
        ).one()
    if (
        database_name != expected_database_name
        or server_uuid != expected_server_uuid
    ):
        raise RuntimeError("database identity mismatch; refusing migration write")


def main(argv: _Iterable[str] | None = None) -> None:
    """执行显式选择的 migration apply / inspect / recovery 模式。"""
    args = _parse_args(argv)
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    validate_release_migration_manifest(
        paths,
        manifest_path=RELEASE_MIGRATION_MANIFEST_PATH,
    )
    engine = create_engine_from_env()
    try:
        if args.inspect_applying:
            result = inspect_applying_migration(engine, paths)
        else:
            _assert_write_database_identity(
                engine,
                expected_database_name=args.expected_database_name,
                expected_server_uuid=args.expected_server_uuid,
            )
            if args.recover_applying:
                result = recover_applying_migration(
                    engine,
                    paths,
                    expected_state_digest=args.state_digest,
                )
            else:
                apply_pending_migration_files(engine, paths)
                return
        print(_json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
