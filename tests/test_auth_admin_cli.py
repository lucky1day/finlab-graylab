from __future__ import annotations

import os

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
