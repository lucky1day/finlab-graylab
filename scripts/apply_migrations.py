from __future__ import annotations

import argparse as _argparse
from dataclasses import dataclass as _dataclass
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
    apply_pending_migration_files,
    inspect_applying_migration_017,
    inspect_applying_migration_018,
    inspect_applying_migration_019,
    inspect_applying_migration_021,
    inspect_applying_migration_022,
    inspect_applying_migration_023,
    inspect_applying_migration_024,
    inspect_applying_migration_025,
    recover_applying_migration_017,
    recover_applying_migration_018,
    recover_applying_migration_019,
    recover_applying_migration_021,
    recover_applying_migration_022,
    recover_applying_migration_023,
    recover_applying_migration_024,
    recover_applying_migration_025,
    validate_release_migration_manifest,
)
from scheduler.repository import create_engine_from_env


@_dataclass(frozen=True, slots=True)
class _MigrationMode:
    """单个 CLI migration 模式的声明式合同。"""

    option: str
    dest: str
    action: str
    help: str
    handler_name: str


_MIGRATION_MODES = (
    _MigrationMode(
        option="--inspect-applying-017",
        dest="inspect_applying_017",
        action="inspect",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "017 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
        handler_name="inspect_applying_migration_017",
    ),
    _MigrationMode(
        option="--recover-applying-017",
        dest="recover_applying_017",
        action="recover",
        help=(
            "recover migration 017 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
        handler_name="recover_applying_migration_017",
    ),
    _MigrationMode(
        option="--inspect-applying-018",
        dest="inspect_applying_018",
        action="inspect",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "018 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
        handler_name="inspect_applying_migration_018",
    ),
    _MigrationMode(
        option="--recover-applying-018",
        dest="recover_applying_018",
        action="recover",
        help=(
            "recover migration 018 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
        handler_name="recover_applying_migration_018",
    ),
    _MigrationMode(
        option="--inspect-applying-019",
        dest="inspect_applying_019",
        action="inspect",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "019 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
        handler_name="inspect_applying_migration_019",
    ),
    _MigrationMode(
        option="--recover-applying-019",
        dest="recover_applying_019",
        action="recover",
        help=(
            "recover migration 019 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
        handler_name="recover_applying_migration_019",
    ),
    _MigrationMode(
        option="--inspect-applying-021",
        dest="inspect_applying_021",
        action="inspect",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "021 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
        handler_name="inspect_applying_migration_021",
    ),
    _MigrationMode(
        option="--recover-applying-021",
        dest="recover_applying_021",
        action="recover",
        help=(
            "recover migration 021 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
        handler_name="recover_applying_migration_021",
    ),
    _MigrationMode(
        option="--inspect-applying-022",
        dest="inspect_applying_022",
        action="inspect",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "022 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
        handler_name="inspect_applying_migration_022",
    ),
    _MigrationMode(
        option="--recover-applying-022",
        dest="recover_applying_022",
        action="recover",
        help=(
            "recover migration 022 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
        handler_name="recover_applying_migration_022",
    ),
    _MigrationMode(
        option="--inspect-applying-023",
        dest="inspect_applying_023",
        action="inspect",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "023 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
        handler_name="inspect_applying_migration_023",
    ),
    _MigrationMode(
        option="--recover-applying-023",
        dest="recover_applying_023",
        action="recover",
        help=(
            "recover migration 023 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
        handler_name="recover_applying_migration_023",
    ),
    _MigrationMode(
        option="--inspect-applying-024",
        dest="inspect_applying_024",
        action="inspect",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "024 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
        handler_name="inspect_applying_migration_024",
    ),
    _MigrationMode(
        option="--recover-applying-024",
        dest="recover_applying_024",
        action="recover",
        help=(
            "recover migration 024 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
        handler_name="recover_applying_migration_024",
    ),
    _MigrationMode(
        option="--inspect-applying-025",
        dest="inspect_applying_025",
        action="inspect",
        help=(
            "connect to the live DB and classify an interrupted migration "
            "025 using SELECT plus a named advisory lock only; no DDL/DML"
        ),
        handler_name="inspect_applying_migration_025",
    ),
    _MigrationMode(
        option="--recover-applying-025",
        dest="recover_applying_025",
        action="recover",
        help=(
            "recover migration 025 using --apply and the exact digest "
            "from a prior read-only inspection"
        ),
        handler_name="recover_applying_migration_025",
    ),
)


def _selected_mode(args: _argparse.Namespace) -> _MigrationMode | None:
    """返回 argparse 已选的唯一版本模式。"""
    return next(
        (
            mode
            for mode in _MIGRATION_MODES
            if getattr(args, mode.dest)
        ),
        None,
    )


def _mode_handler(mode: _MigrationMode):
    """延迟解析 handler，保留既有模块级 patch 边界。"""
    return globals()[mode.handler_name]


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
    for migration_mode in _MIGRATION_MODES:
        mode.add_argument(
            migration_mode.option,
            dest=migration_mode.dest,
            action="store_true",
            help=migration_mode.help,
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
    selected_mode = _selected_mode(args)
    if selected_mode is not None and selected_mode.action == "inspect":
        if args.apply or args.state_digest:
            parser.error(
                "read-only inspection does not accept --apply or "
                "--state-digest"
            )
        return args
    if selected_mode is not None and selected_mode.action == "recover":
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
            "--recover-applying-019/021/022/023/024/025"
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
    validate_release_migration_manifest(
        paths,
        manifest_path=RELEASE_MIGRATION_MANIFEST_PATH,
    )
    engine = create_engine_from_env()
    try:
        selected_mode = _selected_mode(args)
        if selected_mode is None or selected_mode.action != "inspect":
            _assert_write_database_identity(
                engine,
                expected_database_name=args.expected_database_name,
                expected_server_uuid=args.expected_server_uuid,
            )
        if selected_mode is None:
            apply_pending_migration_files(engine, paths)
        else:
            handler = _mode_handler(selected_mode)
            if selected_mode.action == "inspect":
                result = handler(engine, paths)
            else:
                result = handler(
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
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
