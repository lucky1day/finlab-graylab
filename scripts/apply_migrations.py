from __future__ import annotations

import argparse as _argparse
import json as _json
from pathlib import Path as _Path
import re as _re
import sys as _sys
from typing import Iterable as _Iterable


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
    recover_applying_migration_017,
    recover_applying_migration_018,
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
    "main",
    "recover_applying_migration_017",
    "recover_applying_migration_018",
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
    parser.add_argument(
        "--apply",
        action="store_true",
        help="explicitly authorize a database-changing mode",
    )
    parser.add_argument(
        "--state-digest",
        help="canonical 64-hex digest required by recovery",
    )
    args = parser.parse_args(argv)
    if args.inspect_applying_017:
        if args.apply or args.state_digest:
            parser.error(
                "read-only inspection does not accept --apply or "
                "--state-digest"
            )
        return args
    if args.recover_applying_017:
        if not args.apply or not args.state_digest:
            parser.error(
                "recovery requires both --apply and --state-digest"
            )
        if _re.fullmatch(r"[0-9a-f]{64}", args.state_digest) is None:
            parser.error(
                "--state-digest must be 64 lowercase hex characters"
            )
        return args
    if args.inspect_applying_018:
        if args.apply or args.state_digest:
            parser.error(
                "read-only inspection does not accept --apply or "
                "--state-digest"
            )
        return args
    if args.recover_applying_018:
        if not args.apply or not args.state_digest:
            parser.error(
                "recovery requires both --apply and --state-digest"
            )
        if _re.fullmatch(r"[0-9a-f]{64}", args.state_digest) is None:
            parser.error(
                "--state-digest must be 64 lowercase hex characters"
            )
        return args
    if args.state_digest:
        parser.error(
            "--state-digest is only valid with "
            "--recover-applying-017 or --recover-applying-018"
        )
    if not args.apply:
        parser.error("--apply is required to change the database")
    return args


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
        else:
            apply_pending_migration_files(engine, paths)  # noqa: F405
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
