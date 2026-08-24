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

from migrations.runner import (
    MIGRATIONS_DIR,
    RELEASE_MIGRATION_MANIFEST_PATH,
    MigrationHistoryError,
    MigrationPartialApplyError,
    MigrationPreflightError,
    MigrationSQLParseError,
    apply_migration_files,
    apply_pending_migration_files,
    inspect_applying_migration_017,
    inspect_applying_migration_018,
    inspect_applying_migration_019,
    inspect_applying_migration_021,
    recover_applying_migration_017,
    recover_applying_migration_018,
    recover_applying_migration_019,
    recover_applying_migration_021,
    split_sql_statements,
    validate_release_migration_manifest,
)
from scheduler.repository import create_engine_from_env


__all__ = (
    "MigrationHistoryError",
    "MigrationPartialApplyError",
    "MigrationPreflightError",
    "MigrationSQLParseError",
    "apply_migration_files",
    "apply_pending_migration_files",
    "inspect_applying_migration_017",
    "inspect_applying_migration_018",
    "inspect_applying_migration_019",
    "inspect_applying_migration_021",
    "main",
    "recover_applying_migration_017",
    "recover_applying_migration_018",
    "recover_applying_migration_019",
    "recover_applying_migration_021",
    "split_sql_statements",
    "validate_release_migration_manifest",
)


def _parse_args(
    argv: _Iterable[str] | None = None,
) -> _argparse.Namespace:
    """解析 apply / read-only inspect / fenced recovery 三种模式。"""
    parser = _argparse.ArgumentParser(
        description=(
            "Apply or inspect Bond Factor Lab schema migrations. "
            "Write paths require explicit --apply authorization."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inspect-applying-017",
        action="store_true",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "017 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
    )
    mode.add_argument(
        "--recover-applying-017",
        action="store_true",
        help=(
            "recover migration 017 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
    )
    mode.add_argument(
        "--inspect-applying-018",
        action="store_true",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "018 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
    )
    mode.add_argument(
        "--recover-applying-018",
        action="store_true",
        help=(
            "recover migration 018 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
    )
    mode.add_argument(
        "--inspect-applying-019",
        action="store_true",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "019 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
    )
    mode.add_argument(
        "--recover-applying-019",
        action="store_true",
        help=(
            "recover migration 019 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
    )
    mode.add_argument(
        "--inspect-applying-021",
        action="store_true",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "021 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
    )
    mode.add_argument(
        "--recover-applying-021",
        action="store_true",
        help=(
            "recover migration 021 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="explicitly authorize a database-changing mode",
    )
    parser.add_argument(
        "--state-digest",
        help="canonical 64-hex digest required by recovery",
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
    if args.inspect_applying_017:
        if args.apply or args.state_digest:
            parser.error(
                "read-only inspection does not accept --apply or "
                "--state-digest"
            )
        return args
    elif args.recover_applying_017:
        if not args.apply or not args.state_digest:
            parser.error(
                "recovery requires both --apply and --state-digest"
            )
        if _re.fullmatch(r"[0-9a-f]{64}", args.state_digest) is None:
            parser.error(
                "--state-digest must be 64 lowercase hex characters"
            )
    elif args.inspect_applying_018:
        if args.apply or args.state_digest:
            parser.error(
                "read-only inspection does not accept --apply or "
                "--state-digest"
            )
        return args
    elif args.recover_applying_018:
        if not args.apply or not args.state_digest:
            parser.error(
                "recovery requires both --apply and --state-digest"
            )
        if _re.fullmatch(r"[0-9a-f]{64}", args.state_digest) is None:
            parser.error(
                "--state-digest must be 64 lowercase hex characters"
            )
    elif args.inspect_applying_019:
        if args.apply or args.state_digest:
            parser.error(
                "read-only inspection does not accept --apply or "
                "--state-digest"
            )
        return args
    elif args.recover_applying_019:
        if not args.apply or not args.state_digest:
            parser.error(
                "recovery requires both --apply and --state-digest"
            )
        if _re.fullmatch(r"[0-9a-f]{64}", args.state_digest) is None:
            parser.error(
                "--state-digest must be 64 lowercase hex characters"
            )
    elif args.inspect_applying_021:
        if args.apply or args.state_digest:
            parser.error(
                "read-only inspection does not accept --apply or "
                "--state-digest"
            )
        return args
    elif args.recover_applying_021:
        if not args.apply or not args.state_digest:
            parser.error(
                "recovery requires both --apply and --state-digest"
            )
        if _re.fullmatch(r"[0-9a-f]{64}", args.state_digest) is None:
            parser.error(
                "--state-digest must be 64 lowercase hex characters"
            )
    elif args.state_digest:
        parser.error(
            "--state-digest is only valid with "
            "--recover-applying-017, --recover-applying-018 or "
            "--recover-applying-019 or --recover-applying-021"
        )
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
            (
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}"
            ),
            args.expected_server_uuid,
        )
        is None
    ):
        parser.error(
            "--expected-server-uuid must be a canonical lowercase UUID"
        )
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
        raise RuntimeError(
            "database identity mismatch; refusing migration write"
        )


def main(argv: _Iterable[str] | None = None) -> None:
    """执行显式选择的 migration apply / inspect / recovery 模式。"""
    args = _parse_args(argv)
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    validate_release_migration_manifest(  # noqa: F405
        paths,
        manifest_path=RELEASE_MIGRATION_MANIFEST_PATH,
    )
    engine = create_engine_from_env()
    try:
        if not (
            args.inspect_applying_017
            or args.inspect_applying_018
            or args.inspect_applying_019
            or args.inspect_applying_021
        ):
            _assert_write_database_identity(
                engine,
                expected_database_name=args.expected_database_name,
                expected_server_uuid=args.expected_server_uuid,
            )
        if args.inspect_applying_017:
            result = inspect_applying_migration_017(  # noqa: F405
                engine,
                paths,
            )
            print(
                _json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.recover_applying_017:
            result = recover_applying_migration_017(  # noqa: F405
                engine,
                paths,
                expected_state_digest=args.state_digest,
            )
            print(
                _json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.inspect_applying_018:
            result = inspect_applying_migration_018(  # noqa: F405
                engine,
                paths,
            )
            print(
                _json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.recover_applying_018:
            result = recover_applying_migration_018(  # noqa: F405
                engine,
                paths,
                expected_state_digest=args.state_digest,
            )
            print(
                _json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.inspect_applying_019:
            result = inspect_applying_migration_019(  # noqa: F405
                engine,
                paths,
            )
            print(
                _json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.recover_applying_019:
            result = recover_applying_migration_019(  # noqa: F405
                engine,
                paths,
                expected_state_digest=args.state_digest,
            )
            print(
                _json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.inspect_applying_021:
            result = inspect_applying_migration_021(  # noqa: F405
                engine,
                paths,
            )
            print(
                _json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        elif args.recover_applying_021:
            result = recover_applying_migration_021(  # noqa: F405
                engine,
                paths,
                expected_state_digest=args.state_digest,
            )
            print(
                _json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            apply_pending_migration_files(engine, paths)  # noqa: F405
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
