from __future__ import annotations

import os
from argparse import Namespace

import pytest

from scripts import manage_auth_admin


UUID = "12345678-1234-1234-1234-123456789abc"


def test_cli_requires_mode_and_canonical_database_identity() -> None:
    with pytest.raises(SystemExit, match="2"):
        manage_auth_admin._parse_args([])
    with pytest.raises(SystemExit, match="2"):
        manage_auth_admin._parse_args(
            [
                "--initialize",
                "--expected-database-name",
                "bond_db",
                "--expected-server-uuid",
                "NOT-A-UUID",
            ]
        )
    args = manage_auth_admin._parse_args(
        [
            "--reset-protected-admin",
            "--expected-database-name",
            "bond_db",
            "--expected-server-uuid",
            UUID,
        ]
    )
    assert args.reset_protected_admin


def test_secret_reader_requires_regular_owner_only_file(tmp_path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("Admin123", encoding="utf-8")
    secret.chmod(0o600)
    assert manage_auth_admin._read_secret(secret) == "Admin123"
    secret.chmod(0o644)
    with pytest.raises(RuntimeError, match="0400 or 0600"):
        manage_auth_admin._read_secret(secret)


def test_secret_reader_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("Admin123", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "secret-link"
    os.symlink(target, link)
    with pytest.raises(RuntimeError, match="regular file"):
        manage_auth_admin._read_secret(link)


def test_secret_path_is_fixed_by_deployment_target(monkeypatch) -> None:
    monkeypatch.setenv("BFL_DEPLOYMENT_TARGET", "aliyun-gray")
    assert str(manage_auth_admin._secret_path()) == (
        "/etc/bond-factor-lab/secrets/auth-bootstrap-password"
    )
    monkeypatch.setenv("BFL_DEPLOYMENT_TARGET", "unknown")
    with pytest.raises(RuntimeError, match="unsupported"):
        manage_auth_admin._secret_path()


def test_main_suppresses_database_errors_and_secret_material(
    monkeypatch,
    capsys,
) -> None:
    plaintext = "Secret123"
    password_hash = "$argon2id$v=19$m=19456,t=2,p=1$private"
    database_error = (
        "INSERT INTO t_auth_users password_hash="
        + password_hash
        + " mysql://user:password@host/bond_db "
        + plaintext
    )
    monkeypatch.setattr(
        manage_auth_admin,
        "_parse_args",
        lambda _argv: Namespace(
            initialize=True,
            reset_protected_admin=False,
            expected_database_name="bond_db",
            expected_server_uuid=UUID,
        ),
    )
    monkeypatch.setattr(manage_auth_admin, "_secret_path", lambda: object())
    monkeypatch.setattr(
        manage_auth_admin, "_read_secret", lambda _path: plaintext
    )
    monkeypatch.setattr(
        manage_auth_admin, "hash_password", lambda _value: password_hash
    )

    def fail_engine():
        raise RuntimeError(database_error)

    monkeypatch.setattr(
        manage_auth_admin, "create_engine_from_env", fail_engine
    )
    assert manage_auth_admin.main([]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        '{"error_code": "auth_admin_operation_failed", '
        '"status": "error"}\n'
    )
