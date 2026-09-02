from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import sys
import re
from typing import Iterable

from sqlalchemy import text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.auth import repository
from backend.auth.security import hash_password
from scheduler.repository import create_engine_from_env


SECRET_PATHS = {
    "aliyun-gray": Path(
        "/etc/bond-factor-lab/secrets/auth-bootstrap-password"
    ),
    "mac3-production": Path(
        "/Users/macstudio0/bond-factor-lab-runtime/config/"
        "auth-bootstrap-password"
    ),
}
_LOCK_NAME = "bfl:auth:protected-admin"


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Initialize or reset the unique protected Bond Factor Lab admin."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--initialize", action="store_true")
    mode.add_argument("--reset-protected-admin", action="store_true")
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument("--expected-server-uuid", required=True)
    args = parser.parse_args(argv)
    if not args.expected_database_name.strip():
        parser.error("--expected-database-name must be non-empty")
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}",
        args.expected_server_uuid,
    ) is None:
        parser.error("--expected-server-uuid must be a lowercase UUID")
    return args


def _secret_path() -> Path:
    deployment_target = os.getenv("BFL_DEPLOYMENT_TARGET", "")
    try:
        return SECRET_PATHS[deployment_target]
    except KeyError:
        raise RuntimeError(
            "unsupported BFL_DEPLOYMENT_TARGET for auth administrator"
        ) from None


def _read_secret(path: Path) -> str:
    """以 owner-only、no-follow 语义读取密码文件。"""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise RuntimeError("auth password secret must be a regular file")
    if before.st_uid != os.geteuid():
        raise RuntimeError("auth password secret owner mismatch")
    if stat.S_IMODE(before.st_mode) not in {0o400, 0o600}:
        raise RuntimeError("auth password secret mode must be 0400 or 0600")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("O_NOFOLLOW is required for auth secret reads")
    descriptor = os.open(path, flags | nofollow)
    try:
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise RuntimeError("auth password secret changed during open")
        raw = os.read(descriptor, 2049)
        if len(raw) > 2048:
            raise RuntimeError("auth password secret is too large")
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("auth password secret must be valid UTF-8") from None


def _assert_database_identity(
    connection,
    *,
    expected_database_name: str,
    expected_server_uuid: str,
) -> None:
    database_name, server_uuid = connection.execute(
        text("SELECT DATABASE(), @@server_uuid")
    ).one()
    if (
        database_name != expected_database_name
        or server_uuid != expected_server_uuid
    ):
        raise RuntimeError("database identity mismatch; refusing auth write")


def _initialize(connection, password_hash: str) -> int:
    rows = connection.execute(
        text("SELECT id FROM t_auth_users ORDER BY id FOR UPDATE")
    ).all()
    if rows:
        raise RuntimeError("auth initialization requires an empty user table")
    user_id = repository.initialize_protected_admin(
        connection,
        password_hash=password_hash,
    )
    repository.insert_audit(
        connection,
        event_type="protected_admin_initialized",
        actor_user_id=None,
        target_user_id=user_id,
        request_id="offline-auth-admin-cli",
        detail={},
    )
    return user_id


def _reset(connection, password_hash: str) -> int:
    rows = connection.execute(
        text(
            """
            SELECT id, role, status FROM t_auth_users
            WHERE is_protected_admin = 1
            ORDER BY id FOR UPDATE
            """
        )
    ).all()
    if len(rows) != 1:
        raise RuntimeError("expected exactly one protected administrator")
    if str(rows[0][1]) != "admin" or str(rows[0][2]) != "active":
        raise RuntimeError("protected administrator identity is invalid")
    user_id = int(rows[0][0])
    repository.reset_protected_admin_password(
        connection,
        user_id=user_id,
        password_hash=password_hash,
    )
    repository.revoke_all_admin_sessions(connection)
    repository.insert_audit(
        connection,
        event_type="protected_admin_offline_reset",
        actor_user_id=None,
        target_user_id=user_id,
        request_id="offline-auth-admin-cli",
        detail={},
    )
    return user_id


@contextmanager
def _auth_admin_lock(connection):
    acquired = int(
        connection.execute(
            text("SELECT GET_LOCK(:lock_name, 5)"),
            {"lock_name": _LOCK_NAME},
        ).scalar_one()
        or 0
    )
    connection.commit()
    if acquired != 1:
        raise RuntimeError("could not acquire auth administrator lock")
    body_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        try:
            released = int(
                connection.execute(
                    text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": _LOCK_NAME},
                ).scalar_one()
                or 0
            )
            if released != 1:
                raise RuntimeError("auth administrator lock release failed")
        except BaseException as release_error:
            if body_error is None:
                raise
            body_error.add_note(
                "auth administrator lock release failed: "
                f"{type(release_error).__name__}"
            )


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        password_hash = hash_password(_read_secret(_secret_path()))
        engine = create_engine_from_env()
        try:
            with engine.connect() as connection:
                _assert_database_identity(
                    connection,
                    expected_database_name=args.expected_database_name,
                    expected_server_uuid=args.expected_server_uuid,
                )
                with _auth_admin_lock(connection):
                    with connection.begin():
                        _assert_database_identity(
                            connection,
                            expected_database_name=args.expected_database_name,
                            expected_server_uuid=args.expected_server_uuid,
                        )
                        user_id = (
                            _initialize(connection, password_hash)
                            if args.initialize
                            else _reset(connection, password_hash)
                        )
        finally:
            engine.dispose()
    except Exception:
        print(
            json.dumps(
                {
                    "error_code": "auth_admin_operation_failed",
                    "status": "error",
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "operation": (
                    "initialize"
                    if args.initialize
                    else "reset_protected_admin"
                ),
                "status": "ok",
                "user_id": user_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
